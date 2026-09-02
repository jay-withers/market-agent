"""Tests for the money scale and the derived portfolio figures."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from investagent.models import PortfolioState, Position, Recommendation, money

D = Decimal


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (D("10"), D("10.0000")),
        (D("10.00005"), D("10.0000")),  # down, not to nearest
        (D("10.99999"), D("10.9999")),
        (D("0.00009"), D("0.0000")),
        (D("-10.00009"), D("-10.0000")),  # towards zero
    ],
)
def test_money_quantizes_downwards(value, expected):
    assert money(value) == expected


def test_money_never_rounds_a_value_up_past_a_limit():
    limit = D("99.9999")
    assert money(limit * 1) <= limit


def _state() -> PortfolioState:
    return PortfolioState(
        cash_gbp=D("100.5000"),
        positions=(
            Position(ticker="NVDA", quantity=D("0.5"), value_gbp=D("80"), avg_cost_gbp=D("75")),
            Position(ticker="AAPL", quantity=D("1.5"), value_gbp=D("45.25"), avg_cost_gbp=D("40")),
        ),
    )


def test_invested_is_the_sum_of_position_values():
    assert _state().invested_gbp == D("125.2500")


def test_total_value_is_cash_plus_positions():
    assert _state().total_value_gbp == D("225.7500")


def test_position_value_finds_a_holding():
    assert _state().position_value("NVDA") == D("80.0000")


def test_position_value_of_something_not_held_is_zero():
    assert _state().position_value("TSLA") == D("0.0000")


def test_an_empty_portfolio_is_all_cash():
    empty = PortfolioState(cash_gbp=D("500"))
    assert empty.invested_gbp == D("0.0000")
    assert empty.total_value_gbp == D("500.0000")


def test_portfolio_state_is_frozen():
    with pytest.raises(ValidationError) as caught:
        _state().cash_gbp = D("1")

    assert caught.value.errors()[0]["type"] == "frozen_instance"


def test_confidence_outside_zero_to_one_is_rejected():
    with pytest.raises(ValidationError) as caught:
        Recommendation(ticker="NVDA", action="BUY", confidence=1.5, reasoning="r", risks="x")

    assert caught.value.errors()[0]["type"] == "less_than_equal"


def test_an_unknown_action_is_rejected():
    with pytest.raises(ValidationError) as caught:
        Recommendation(ticker="NVDA", action="buy", confidence=0.9, reasoning="r", risks="x")

    assert caught.value.errors()[0]["type"] == "literal_error"
