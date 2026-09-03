"""Tests for the comparison arms.

The experiment's question is "did the AI beat the alternatives", so a benchmark
that is subtly wrong is worse than no benchmark at all.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from investagent.benchmarks import (
    CASH_SYMBOL,
    build,
    cash_value,
    fetch_benchmark_bars,
    index_value,
)
from investagent.marketdata import Bar
from tests.helpers import json_client

D = Decimal
TODAY = date(2026, 9, 2)


def bar(ticker: str, close: Decimal, day: date = TODAY) -> Bar:
    return Bar(ticker, day, close, close, close, close, 1_000_000)


# ---------------------------------------------------------------------------
# The savings arm
# ---------------------------------------------------------------------------


def test_cash_at_day_zero_is_the_notional():
    assert cash_value(D(500), D(5), 0) == D("500.0000")


def test_cash_compounds_daily_not_annually():
    """A step function once a year would make the daily chart nonsense."""
    one_day = cash_value(D(500), D(5), 1)

    assert one_day > D(500)
    assert one_day < D("500.10")


def test_cash_over_a_year_is_close_to_the_apr():
    # Daily compounding at 5% gives slightly more than 5% simple.
    assert D("525") < cash_value(D(500), D(5), 365) < D("526.5")


def test_a_negative_holding_period_is_treated_as_day_zero():
    assert cash_value(D(500), D(5), -3) == D("500.0000")


# ---------------------------------------------------------------------------
# The index arms
# ---------------------------------------------------------------------------


def test_an_index_that_doubled_doubles_the_notional():
    assert index_value(D(500), close_now_usd=D(200), close_at_inception_usd=D(100)) == D(
        "1000.0000"
    )


def test_an_index_that_is_flat_returns_the_notional():
    assert index_value(D(500), D(100), D(100)) == D("500.0000")


def test_an_index_that_halved_halves_the_notional():
    assert index_value(D(500), D(50), D(100)) == D("250.0000")


def test_a_zero_inception_price_falls_back_to_the_notional():
    """Rather than dividing by zero and failing the whole summary."""
    assert index_value(D(500), D(100), D(0)) == D("500.0000")


def test_the_index_ratio_needs_no_fx_because_the_currency_cancels():
    """Both ends are USD, so the GBP return is the ratio times the notional.

    An earlier version applied today's rate to both ends, producing a series
    that moved with sterling rather than with the index.
    """
    assert index_value(D(500), D(220), D(200)) == D("550.0000")


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def test_the_cash_arm_is_always_present_and_carries_no_price():
    points = build(["SPY", CASH_SYMBOL], [], {}, D(500), D(5), 0, TODAY)

    cash = next(p for p in points if p.symbol == CASH_SYMBOL)
    assert cash.close_usd is None
    assert cash.source == "computed"


def test_an_index_with_no_current_price_is_skipped_not_flat_lined():
    """A flat £500 would read as "the index did nothing", which is a different
    and wrong claim from "we have no data"."""
    points = build(["SPY"], [], {"SPY": D(400)}, D(500), D(5), 10, TODAY)

    assert [p.symbol for p in points] == [CASH_SYMBOL]


def test_an_index_with_no_inception_price_is_skipped():
    points = build(["SPY"], [bar("SPY", D(500))], {}, D(500), D(5), 10, TODAY)

    assert [p.symbol for p in points] == [CASH_SYMBOL]


def test_a_full_set_indexes_every_arm_from_inception():
    points = build(
        ["SPY", "EWU", CASH_SYMBOL],
        [bar("SPY", D(600)), bar("EWU", D(40))],
        {"SPY": D(500), "EWU": D(50)},
        D(500),
        D(5),
        0,
        TODAY,
    )
    values = {p.symbol: p.value_gbp for p in points}

    assert values["SPY"] == D("600.0000")  # up 20%
    assert values["EWU"] == D("400.0000")  # down 20%
    assert values[CASH_SYMBOL] == D("500.0000")


def test_the_cash_symbol_is_never_requested_from_the_market(monkeypatch):
    captured: list = []
    fetch_benchmark_bars(["SPY", CASH_SYMBOL], client=json_client({"bars": {}}, capture=captured))

    assert captured[0].url.params["symbols"] == "SPY"


def test_a_cash_only_benchmark_list_makes_no_request():
    assert fetch_benchmark_bars([CASH_SYMBOL]) == []


@pytest.mark.parametrize("symbol", ["SPY", "VT", "EWU"])
def test_the_planned_proxies_all_index_cleanly(symbol):
    points = build([symbol], [bar(symbol, D(110))], {symbol: D(100)}, D(500), D(5), 1, TODAY)

    assert next(p for p in points if p.symbol == symbol).value_gbp == D("550.0000")
