"""Tests for order submission.

The status mapping matters more than it looks: the agent runs at 06:00 UTC and
the US market opens at 14:30, so `submitted` with no fill is the *normal*
outcome of a scheduled run and must not be mistaken for a completed trade.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from investagent.broker.alpaca import ALPACA_STATUS, AlpacaBroker
from investagent.broker.dryrun import DryRunBroker
from tests.helpers import json_client

D = Decimal

ACCEPTED = {
    "id": "b1e2",
    "client_order_id": "ia-42",
    "status": "accepted",
    "created_at": "2026-09-02T06:00:01Z",
    "submitted_at": "2026-09-02T06:00:01Z",
    "filled_at": None,
    "filled_qty": "0",
    "filled_avg_price": None,
}

FILLED = {
    **ACCEPTED,
    "status": "filled",
    "filled_at": "2026-09-02T14:30:02Z",
    "filled_qty": "0.207440",
    "filled_avg_price": "324.96",
}


# ---------------------------------------------------------------------------
# Alpaca
# ---------------------------------------------------------------------------


def test_a_notional_market_order_is_submitted_with_our_own_order_id():
    captured: list = []
    AlpacaBroker(client=json_client(ACCEPTED, capture=captured)).submit_market_order(
        "NVDA", "BUY", D("67.41"), "ia-42", reference_price_usd=D("324.96")
    )

    body = json.loads(captured[0].read())
    assert body == {
        "symbol": "NVDA",
        # Two decimals, as dollars and cents, not a Decimal repr.
        "notional": "67.41",
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": "ia-42",
    }


def test_an_unfilled_submission_reports_no_quantity_or_price():
    result = AlpacaBroker(client=json_client(ACCEPTED)).submit_market_order(
        "NVDA", "BUY", D("67.41"), "ia-42"
    )

    assert result.status == "submitted"
    assert result.broker_order_id == "b1e2"
    assert result.quantity is None
    assert result.filled_avg_price_usd is None
    assert result.filled_at is None


def test_a_filled_order_reports_its_quantity_and_average_price():
    result = AlpacaBroker(client=json_client(FILLED)).submit_market_order(
        "NVDA", "BUY", D("67.41"), "ia-42"
    )

    assert result.status == "filled"
    assert result.quantity == D("0.207440")
    assert result.filled_avg_price_usd == D("324.96")
    assert result.filled_at is not None


@pytest.mark.parametrize(
    ("alpaca", "ours"),
    [
        ("new", "submitted"),
        ("accepted", "submitted"),
        ("pending_new", "submitted"),
        ("partially_filled", "partially_filled"),
        ("filled", "filled"),
        ("canceled", "cancelled"),
        ("expired", "cancelled"),
        ("rejected", "rejected"),
    ],
)
def test_alpaca_statuses_map_onto_our_vocabulary(alpaca, ours):
    assert ALPACA_STATUS[alpaca] == ours


def test_every_mapped_status_is_one_the_trades_check_constraint_allows():
    allowed = {
        "pending",
        "submitted",
        "filled",
        "partially_filled",
        "cancelled",
        "rejected",
        "simulated",
    }
    assert set(ALPACA_STATUS.values()) <= allowed


def test_an_unknown_status_is_treated_as_submitted_rather_than_dropped():
    """Alpaca can add a status; the order exists either way."""
    result = AlpacaBroker(
        client=json_client({**ACCEPTED, "status": "quantum"})
    ).submit_market_order("NVDA", "BUY", D("1.00"), "ia-1")

    assert result.status == "submitted"


def test_a_sell_is_lowercased_for_the_api():
    captured: list = []
    AlpacaBroker(client=json_client(ACCEPTED, capture=captured)).submit_market_order(
        "NVDA", "SELL", D("10.00"), "ia-7"
    )

    assert json.loads(captured[0].read())["side"] == "sell"


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_a_dry_run_simulates_a_fill_at_the_reference_price():
    result = DryRunBroker().submit_market_order(
        "NVDA", "BUY", D("67.41"), "ia-42", reference_price_usd=D("324.96")
    )

    assert result.status == "simulated"
    assert result.filled_avg_price_usd == D("324.96")
    # 67.41 / 324.96, floored to six places.
    assert result.quantity == D("0.207440")


def test_a_dry_run_never_invents_a_broker_order_id():
    """A simulated trade must stay distinguishable from a real one in `trades`."""
    result = DryRunBroker().submit_market_order("NVDA", "BUY", D("10.00"), "ia-1", D("100"))

    assert result.broker_order_id is None


def test_a_dry_run_records_what_it_would_have_submitted():
    broker = DryRunBroker()
    broker.submit_market_order("NVDA", "BUY", D("10.00"), "ia-1", D("100"))
    broker.submit_market_order("AAPL", "SELL", D("20.00"), "ia-2", D("300"))

    assert broker.submitted == [
        ("NVDA", "BUY", D("10.00"), "ia-1"),
        ("AAPL", "SELL", D("20.00"), "ia-2"),
    ]


def test_a_dry_run_quantity_is_floored_so_it_cannot_exceed_the_notional():
    result = DryRunBroker().submit_market_order("NVDA", "BUY", D("10.00"), "ia-1", D(3))

    assert result.quantity == D("3.333333")
    assert result.quantity * D(3) <= D("10.00")


def test_a_dry_run_with_no_reference_price_reports_no_quantity():
    result = DryRunBroker().submit_market_order("NVDA", "BUY", D("10.00"), "ia-1", None)

    assert result.quantity is None
