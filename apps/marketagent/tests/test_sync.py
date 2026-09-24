"""Account authority, idempotence, pending orders and snapshot failures."""

from dataclasses import replace
from decimal import Decimal as D

import httpx
import pytest

from marketagent import alpaca_api, queries
from marketagent import repository as repo
from marketagent.broker.alpaca import AlpacaBroker
from marketagent.broker.base import BrokerAccount, BrokerPosition, BrokerSnapshot, OpenOrder
from marketagent.models import PortfolioState, Position
from marketagent.sync import risk_state
from tests.helpers import POTS

# Neither of these tests depends on watchlist contents, only on the portfolio
# row itself, so any seeded pot would do.
PORTFOLIO = POTS[0]


def snapshot(cash="100000", equity="100000", positions=(), account_id="paper-account"):
    return BrokerSnapshot(
        BrokerAccount(account_id, D(cash), D(equity), D("400000")), list(positions), []
    )


def test_first_sync_uses_account_equity_as_baseline_and_preserves_it(conn):
    pid = repo.portfolio_id(conn, PORTFOLIO)
    repo.save_broker_snapshot(conn, pid, snapshot())
    repo.save_broker_snapshot(conn, pid, snapshot(cash="99000", equity="100025"))
    result = queries.overview(conn, PORTFOLIO)
    assert result["portfolio"]["initial_cash_usd"] == 100000
    assert result["cash_usd"] == 99000
    assert result["total_value_usd"] == 100025
    assert result["pnl_usd"] == 25
    assert result["buying_power_usd"] == 400000
    assert result["broker_synced_at"] is not None


def test_sync_imports_manual_holdings_and_removes_closed_positions(conn):
    pid = repo.portfolio_id(conn, PORTFOLIO)
    position = BrokerPosition("MANUAL", D("1.123456789"), D("120.25"), D("100.123456789"))
    data = snapshot("99880", "100000.25", [position])
    repo.save_broker_snapshot(conn, pid, data)
    repo.save_broker_snapshot(conn, pid, data)
    assert repo.load_positions(conn, pid) == [
        ("MANUAL", position.quantity, position.avg_entry_price_usd)
    ]
    assert "MANUAL" not in repo.active_tickers(conn, pid)
    state, missing = repo.build_state(conn, pid, {})
    assert not missing
    assert state.positions[0].value_usd == D("120.25")
    repo.save_broker_snapshot(conn, pid, snapshot())
    assert not repo.load_positions(conn, pid)
    assert repo.load_cash(conn, pid) == D("100000")


def test_sync_refuses_to_overwrite_with_another_account(conn):
    pid = repo.portfolio_id(conn, PORTFOLIO)
    repo.save_broker_snapshot(conn, pid, snapshot())
    with pytest.raises(RuntimeError, match="account changed"):
        repo.save_broker_snapshot(conn, pid, snapshot(account_id="different"))
    assert repo.load_cash(conn, pid) == D("100000")


def test_risk_reserves_open_buys_without_using_margin_buying_power():
    state = PortfolioState(
        cash_usd=D("1000"),
        positions=(Position(ticker="AAPL", quantity=D(2), value_usd=D(200), avg_cost_usd=D(100)),),
        equity_usd=D(1200),
    )
    data = snapshot("1000", "1200")
    data = replace(
        data,
        open_orders=[
            OpenOrder("AAPL", "buy", D(100)),
            OpenOrder("NVDA", "buy", D(200)),
            OpenOrder("MSFT", "sell", D(0)),
        ],
    )
    reserved = risk_state(state, data)
    assert reserved.cash_usd == D(700)
    assert reserved.invested_usd == D(500)
    assert reserved.total_value_usd == D(1200)
    assert reserved.position_value("AAPL") == D(300)
    assert state.cash_usd == D(1000)


def test_blocked_account_cannot_be_used_for_new_orders():
    data = snapshot()
    data = replace(data, account=replace(data.account, trading_blocked=True))
    with pytest.raises(RuntimeError, match="not enabled"):
        risk_state(PortfolioState(cash_usd=D(100000)), data)


def test_snapshot_parses_the_account_and_pending_orders_without_posting(monkeypatch):
    # AlpacaBroker authenticates through alpaca_api.headers(), which needs a
    # pot selected first. monkeypatch rather than `use_account` directly so the
    # module-level state does not leak into whatever test runs next; conftest
    # already sets every pot's credential pair.
    monkeypatch.setattr(alpaca_api, "_account", PORTFOLIO)

    def handler(request):
        assert request.method == "GET"
        assert request.url.host == "paper-api.alpaca.markets"
        if request.url.path == "/v2/positions":
            return httpx.Response(200, json=[])
        if request.url.path == "/v2/orders":
            assert request.url.params["status"] == "open"
            return httpx.Response(
                200,
                json=[{"symbol": "AAPL", "side": "buy", "notional": "50.00", "filled_qty": "0.1"}],
            )
        return httpx.Response(
            200,
            json={
                "id": "paper",
                "currency": "USD",
                "cash": "100000.00",
                "equity": "100000.00",
                "buying_power": "400000.00",
                "status": "ACTIVE",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = AlpacaBroker(client).snapshot()
    assert result.account.cash_usd == D(100000)
    assert result.open_orders[0].reserved_usd == D(50)


def test_failed_snapshot_does_not_overwrite_existing_balances(conn):
    pid = repo.portfolio_id(conn, PORTFOLIO)
    repo.save_broker_snapshot(conn, pid, snapshot())
    from marketagent.sync import synchronize

    class BrokenBroker:
        def snapshot(self):
            raise RuntimeError("broker unavailable")

    class Connection:
        def execute(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Pool:
        def connection(self):
            return Connection()

    with pytest.raises(RuntimeError, match="unavailable"):
        synchronize(pid, BrokenBroker(), Pool())
    assert repo.load_cash(conn, pid) == D(100000)
