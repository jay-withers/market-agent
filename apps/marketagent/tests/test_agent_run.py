"""jobs.agent.run() end to end, against a real Postgres.

`test_agent.py` covers the two pure pieces of the loop (the decision history
and the spend ceiling) with no database at all. Everything else — the two
LLM stages, the risk engine wired to a real `agent_runs`/`ai_decisions`/
`trades` write path, the budget guard actually stopping a run — only runs
against Postgres, which is why it sat at 32% coverage with nothing here.

`repository.py`'s own tests share one connection across the whole session and
rely on rollback for isolation (see `conftest.conn`) — a real function there
never commits, which is what makes rollback-only isolation work. `run()`
cannot use that connection: it commits at each stage on purpose, so a crash
partway leaves whatever already ran as evidence. Reusing the shared session
connection here would let a committed row from this file leak into every
other test file sharing it. `agent_dsn` below gives `run()` its own fresh
connection per `pool().connection()` call instead — the same granularity
production's real pool gives it — and resets the mutated tables by row
afterwards, back to what `sql/003-seed-watchlist.sql` leaves behind.

`DryRunBroker` is the real implementation, not a fake: it does no I/O, so
nothing is lost by using it, and it is what exercises the fill path `_execute`
takes on every scheduled run.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import psycopg
import pytest

from marketagent import repository as repo
from marketagent.broker.dryrun import DryRunBroker
from marketagent.fx import FxRate
from marketagent.jobs import agent
from marketagent.jobs.agent import BudgetExceeded
from marketagent.llm.base import LlmResult, Usage
from marketagent.marketdata import Bar
from marketagent.models import NewsRelevance, Recommendation
from marketagent.news import Article

D = Decimal

# One of the names sql/011-sector-pots.sql puts on the tech pot's
# watchlist — this file exercises the loop against that account throughout.
TICKER = "NVDA"
PORTFOLIO = "tech"
RATE = FxRate(gbp_usd=D("1.3400"), as_of=date(2026, 9, 14))
BAR_DATE = date(2026, 9, 14)


def _reset(dsn: str) -> None:
    """Back to what 003-seed-watchlist.sql leaves: ten companies, one
    portfolio at its initial cash, nothing else. Deletes rather than rollback
    because run() commits for real — see the module docstring."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM trades")
        conn.execute("DELETE FROM ai_decisions")
        conn.execute("DELETE FROM news_analysis")
        conn.execute("DELETE FROM news")
        conn.execute("DELETE FROM prices")
        conn.execute("DELETE FROM agent_runs")
        conn.execute("DELETE FROM positions")
        conn.execute(
            "UPDATE portfolio SET initial_cash_usd=100000,cash_usd=100000,equity_usd=NULL,"
            "buying_power_usd=NULL,broker_account_id=NULL,broker_synced_at=NULL,"
            "is_active = name NOT IN ('static-100', 'dynamic-500')"
        )


@pytest.fixture
def agent_dsn(database_dsn):
    """The scratch database, reset both before and after — a previous test's
    failure must not leave the next one a dirty baseline, matching why
    conftest.conn rolls back on both sides of the shared connection."""
    _reset(database_dsn)
    yield database_dsn
    _reset(database_dsn)


class _FreshConnectionPool:
    """Stands in for the real psycopg_pool.ConnectionPool: a genuinely new
    connection per checkout, which is the granularity `pool().connection()`
    gives run() in production. Each connection commits and closes on its own
    `with` block exit, same as a pooled one returning to the pool."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def connection(self):
        return psycopg.connect(self._dsn)


def _bar(ticker: str, close: str = "150.0000") -> Bar:
    return Bar(
        ticker=ticker,
        bar_date=BAR_DATE,
        open_usd=D("149.0000"),
        high_usd=D("151.0000"),
        low_usd=D("148.0000"),
        close_usd=D(close),
        volume=1_000_000,
    )


def _article(ticker: str) -> Article:
    return Article(
        external_id="art-1",
        headline=f"{ticker} beats on earnings",
        published_at=datetime(2026, 9, 14, 6, 0, tzinfo=UTC),
        summary="Strong quarter, raised guidance.",
        url="https://example.invalid/1",
        tickers=(ticker,),
    )


class _FakeLlm:
    """Relevant for exactly one ticker, so every other seeded ticker is
    skipped before it ever reaches `analyse` — and would raise if it didn't,
    since `calls` records every ticker actually analysed."""

    def __init__(self, relevant_ticker: str, action: str = "BUY", amount_usd: float = 40.0):
        self.relevant_ticker = relevant_ticker
        self.action = action
        self.amount_usd = amount_usd
        self.analysed: list[str] = []

    def filter_news(self, ticker: str, headline: str, summary: str | None) -> LlmResult:
        return LlmResult(
            value=NewsRelevance(
                relevant=ticker == self.relevant_ticker,
                sentiment="positive",
                sentiment_score=0.6,
                rationale="Earnings beat.",
            ),
            model="deepseek-flash",
            usage=Usage(input_tokens=100, output_tokens=20),
        )

    def analyse(self, prompt: str) -> LlmResult:
        self.analysed.append(self.relevant_ticker)
        return LlmResult(
            value=Recommendation(
                ticker=self.relevant_ticker,
                action=self.action,
                confidence=0.8,
                suggested_amount_usd=self.amount_usd if self.action != "HOLD" else None,
                reasoning="Strong quarter and a market that has not caught up.",
                risks="Guidance could disappoint next quarter.",
            ),
            model="deepseek-v4-pro",
            usage=Usage(input_tokens=500, output_tokens=150),
        )

    def narrate(self, prompt: str):
        raise AssertionError("run() never narrates")

    def review(self, prompt: str):
        raise AssertionError("run() never reviews")


@pytest.fixture(autouse=True)
def _patch_market_data(monkeypatch, agent_dsn):
    """Everything run() fetches directly rather than through an injected
    parameter — fx, prices and news are all outbound network calls the loop
    makes for itself, unlike llm and broker."""
    monkeypatch.setenv("RISK_MAX_TRADE_USD", "50")
    monkeypatch.setenv("RISK_MAX_POSITION_USD", "100")
    monkeypatch.setattr(agent, "pool", lambda: _FreshConnectionPool(agent_dsn))
    monkeypatch.setattr(agent, "fetch_daily_bars", lambda tickers, days: [_bar(t) for t in tickers])
    monkeypatch.setattr(agent, "fetch_news", lambda tickers, hours: [_article(TICKER)])


def _portfolio_cash(dsn: str) -> Decimal:
    with psycopg.connect(dsn) as conn:
        return repo.load_cash(conn, repo.portfolio_id(conn, PORTFOLIO))


# ---------------------------------------------------------------------------
# The happy path: BUY, approved, filled
# ---------------------------------------------------------------------------


def test_a_buy_is_analysed_approved_and_filled(agent_dsn, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    llm = _FakeLlm(TICKER, action="BUY", amount_usd=40.0)

    run_id = agent.run(portfolio=PORTFOLIO, trigger="manual", llm=llm, broker=DryRunBroker())

    # Only the ticker with relevant news was ever sent to the expensive model —
    # the other nine seeded tickers must never reach it.
    assert llm.analysed == [TICKER]

    with psycopg.connect(agent_dsn) as conn:
        run_row = conn.execute(
            "SELECT status, dry_run, trigger, decisions_made, trades_executed, error "
            "FROM agent_runs WHERE id = %s",
            (run_id,),
        ).fetchone()
        assert run_row == ("succeeded", True, "manual", 1, 1, None)

        decision = conn.execute(
            "SELECT action, approved_amount_usd, model FROM ai_decisions WHERE run_id = %s",
            (run_id,),
        ).fetchone()
        assert decision == ("BUY", D("40.0000"), "deepseek-v4-pro")

        trade = conn.execute(
            "SELECT status, side, dry_run, notional_usd FROM trades t "
            "JOIN ai_decisions d ON d.id = t.decision_id WHERE d.run_id = %s",
            (run_id,),
        ).fetchone()
        assert trade == ("simulated", "BUY", True, D("40.0000"))

        position = conn.execute(
            "SELECT quantity FROM positions WHERE ticker = %s", (TICKER,)
        ).fetchone()
        assert position is not None and position[0] > 0

    # Cash moved by the fill — a dry run still updates our own ledger, only the
    # broker call itself is simulated.
    assert _portfolio_cash(agent_dsn) == D("99960.0000")


def test_a_decision_is_persisted_with_the_history_it_was_shown(agent_dsn):
    """The point of `prompt_context`: a decision has to carry what the model
    was shown, not just what it answered."""
    agent.run(portfolio=PORTFOLIO, llm=_FakeLlm(TICKER), broker=DryRunBroker())

    with psycopg.connect(agent_dsn) as conn:
        pid = repo.portfolio_id(conn, PORTFOLIO)
        history = repo.recent_decisions(conn, pid, TICKER, limit=5)

    assert history[0]["action"] == "BUY"
    assert history[0]["binding_constraint"] == "recommended_amount"


# ---------------------------------------------------------------------------
# HOLD: persisted, nothing traded
# ---------------------------------------------------------------------------


def test_a_hold_is_persisted_with_no_trade_and_cash_untouched(agent_dsn):
    run_id = agent.run(
        portfolio=PORTFOLIO, llm=_FakeLlm(TICKER, action="HOLD"), broker=DryRunBroker()
    )

    with psycopg.connect(agent_dsn) as conn:
        decision = conn.execute(
            "SELECT action, approved_amount_usd FROM ai_decisions WHERE run_id = %s",
            (run_id,),
        ).fetchone()
        assert decision == ("HOLD", None)

        trade_count = conn.execute(
            "SELECT count(*) FROM trades t JOIN ai_decisions d ON d.id = t.decision_id "
            "WHERE d.run_id = %s",
            (run_id,),
        ).fetchone()[0]
        assert trade_count == 0

    assert _portfolio_cash(agent_dsn) == D("100000.0000")


# ---------------------------------------------------------------------------
# The budget ceiling stops the run and the failure is recorded
# ---------------------------------------------------------------------------


def test_crossing_the_budget_ceiling_fails_the_run_with_evidence(agent_dsn, monkeypatch):
    """`MAX_RUN_COST_USD` set below what even the cheap filter call costs, so
    the very first call trips it — proving the check runs per call rather
    than once per stage, which is the whole reason it exists."""
    monkeypatch.setenv("MAX_RUN_COST_USD", "0.0000001")

    with pytest.raises(BudgetExceeded):
        agent.run(portfolio=PORTFOLIO, llm=_FakeLlm(TICKER), broker=DryRunBroker())

    with psycopg.connect(agent_dsn) as conn:
        run_row = conn.execute(
            "SELECT status, error FROM agent_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        status, error = run_row
        assert status == "failed"
        assert "BudgetExceeded" in error
        assert "MAX_RUN_COST_USD" in error

        # The failure happened before any decision was reached — closing the
        # row must not fabricate one.
        assert conn.execute("SELECT count(*) FROM ai_decisions").fetchone()[0] == 0

    # Untouched: the run failed before the risk engine or the broker ever saw
    # a recommendation.
    assert _portfolio_cash(agent_dsn) == D("100000.0000")


def test_paper_fills_are_mirrored_without_applying_cash_twice(agent_dsn):
    from marketagent.broker.base import BrokerAccount, BrokerPosition, BrokerSnapshot, OrderResult

    class PaperBroker:
        filled = False

        def snapshot(self):
            cash = D(99960) if self.filled else D(100000)
            positions = [BrokerPosition(TICKER, D("0.4"), D(40), D(100))] if self.filled else []
            return BrokerSnapshot(BrokerAccount("paper", cash, D(100000), D(400000)), positions, [])

        def submit_market_order(self, **kwargs):
            assert kwargs["notional_usd"] == D(40)
            self.filled = True
            return OrderResult(
                status="filled",
                broker_order_id="paper-fill",
                quantity=D("0.4"),
                filled_avg_price_usd=D(100),
            )

    agent.run(portfolio=PORTFOLIO, llm=_FakeLlm(TICKER, amount_usd=40), broker=PaperBroker())
    with psycopg.connect(agent_dsn) as conn:
        assert repo.load_cash(conn, repo.portfolio_id(conn, PORTFOLIO)) == D(99960)
        assert conn.execute("SELECT count(*) FROM positions").fetchone()[0] == 1
        assert conn.execute("SELECT notional_usd FROM trades").fetchone()[0] == D(40)


# ---------------------------------------------------------------------------
# The pre-check: pay the model only for what the risk engine could act on
# ---------------------------------------------------------------------------


def _hold(dsn: str, ticker: str, value: str, cash: str) -> None:
    """Leave the pot holding `ticker` at `value` with `cash` left over."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        pid = repo.portfolio_id(conn, PORTFOLIO)
        conn.execute("UPDATE portfolio SET cash_usd = %s WHERE id = %s", (D(cash), pid))
        conn.execute(
            "INSERT INTO positions (portfolio_id, ticker, quantity, avg_cost_usd, market_value_usd)"
            " VALUES (%s, %s, 1, %s, %s)",
            (pid, ticker, D(value), D(value)),
        )


def _record_news(monkeypatch, ticker: str) -> list[list[str]]:
    asked: list[list[str]] = []

    def fetch(tickers, hours):
        asked.append(list(tickers))
        return [_article(ticker)] if ticker in tickers else []

    monkeypatch.setattr(agent, "fetch_news", fetch)
    return asked


def test_a_pot_with_no_buying_headroom_analyses_only_its_holdings(agent_dsn, monkeypatch):
    """At the 80% exposure ceiling every BUY of an unheld ticker is refused
    whatever the model says, so only the holding is worth an analysis — for a
    possible SELL. News is not even fetched for the rest of the watchlist."""
    monkeypatch.setenv("DRY_RUN", "true")
    held = "AAPL"
    _hold(agent_dsn, held, value="390", cash="10")
    asked = _record_news(monkeypatch, held)
    llm = _FakeLlm(held, action="SELL", amount_usd=20.0)

    agent.run(portfolio=PORTFOLIO, llm=llm, broker=DryRunBroker())

    assert asked == [[held]]
    assert llm.analysed == [held]


def test_a_pot_with_headroom_analyses_its_whole_watchlist(agent_dsn, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    asked = _record_news(monkeypatch, TICKER)

    agent.run(portfolio=PORTFOLIO, llm=_FakeLlm(TICKER), broker=DryRunBroker())

    assert len(asked[0]) == 50


def test_nothing_is_analysed_once_the_daily_trade_limit_is_spent(agent_dsn, monkeypatch):
    """With the trade budget gone even a SELL is refused, so the run pays for
    no analysis at all and still closes its row as a success."""
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("RISK_MAX_DAILY_TRADES", "0")
    asked = _record_news(monkeypatch, TICKER)
    llm = _FakeLlm(TICKER)

    run_id = agent.run(portfolio=PORTFOLIO, llm=llm, broker=DryRunBroker())

    assert asked == [[]]
    assert llm.analysed == []
    with psycopg.connect(agent_dsn) as conn:
        status = conn.execute("SELECT status FROM agent_runs WHERE id = %s", (run_id,)).fetchone()
        assert status == ("succeeded",)


def test_a_run_records_the_model_and_risk_settings_it_used(agent_dsn, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("ANALYSIS_EFFORT", "low")

    run_id = agent.run(portfolio=PORTFOLIO, llm=_FakeLlm(TICKER), broker=DryRunBroker())

    with psycopg.connect(agent_dsn) as conn:
        row = conn.execute("SELECT config FROM agent_runs WHERE id = %s", (run_id,)).fetchone()
    config = row[0]
    assert config["analysis_effort"] == "low"
    assert config["risk"]["min_trade_usd"] == "5"
    assert "allowed_tickers" not in config["risk"]


def test_a_retired_pot_refuses_to_run_and_opens_no_row(agent_dsn):
    with psycopg.connect(agent_dsn, autocommit=True) as conn:
        conn.execute("UPDATE portfolio SET is_active = false WHERE name = %s", (PORTFOLIO,))

    with pytest.raises(RuntimeError, match="retired"):
        agent.run(portfolio=PORTFOLIO, llm=_FakeLlm(TICKER), broker=DryRunBroker())

    with psycopg.connect(agent_dsn) as conn:
        assert conn.execute("SELECT count(*) FROM agent_runs").fetchone()[0] == 0
