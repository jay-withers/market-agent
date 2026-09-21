"""Tests for the two parts of the agent loop that need no database.

The loop itself reads and writes Postgres at every step, so it is not
exercised here. What *is* exercised is the pair of things a wrong answer would
be expensive: the decision history the model is shown, and the spend ceiling
that stops a run that has started calling in a circle.

Nothing in this file touches a database or the network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from marketagent.jobs.agent import (
    BudgetExceeded,
    _check_budget,
    _history_lines,
    _prompt,
)
from marketagent.marketdata import Bar
from marketagent.models import PortfolioState, RiskLimits
from marketagent.news import Article

D = Decimal


def _decision(**overrides) -> dict:
    """One row as `repository.recent_decisions` returns it.

    `confidence` is a `Decimal` because the column is NUMERIC(5,4) and that is
    what comes back — not the float the model produced. Verified against a real
    Postgres; a fixture using a float here would not exercise the formatting
    that actually runs.
    """
    defaults = dict(
        on_date=date(2026, 9, 12),
        action="BUY",
        confidence=D("0.7800"),
        recommended_amount_usd=D("40.0000"),
        approved_amount_usd=D("30.0000"),
        binding_constraint="max_trade_usd",
        trade_status="submitted",
    )
    return {**defaults, **overrides}


def _state() -> PortfolioState:
    return PortfolioState(cash_usd=D("500.0000"))


def _limits() -> RiskLimits:
    return RiskLimits(
        max_position_usd=D(100),
        max_trade_usd=D(50),
        min_trade_usd=D(5),
        max_concentration_pct=D(25),
        max_total_exposure_pct=D(80),
        max_daily_trades=3,
        min_confidence=0.6,
        allowed_tickers=frozenset({"NVDA"}),
    )


def _rendered(history) -> str:
    return _prompt(
        "NVDA",
        [
            Article(
                external_id="1",
                headline="NVDA beats on earnings",
                published_at=datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
            )
        ],
        [
            Bar(
                ticker="NVDA",
                bar_date=date(2026, 9, 12),
                open_usd=D("220.00"),
                high_usd=D("226.00"),
                low_usd=D("219.00"),
                close_usd=D("224.41"),
                volume=157_000_000,
            )
        ],
        _state(),
        _limits(),
        history,
    )


# ---------------------------------------------------------------------------
# The decision history
# ---------------------------------------------------------------------------


def test_every_field_of_a_prior_decision_is_shown():
    line = _history_lines([_decision()])
    assert "2026-09-12" in line
    assert "BUY" in line
    assert "confidence 0.78" in line
    assert "asked USD 40.0000" in line
    assert "approved USD 30.0000" in line
    assert "binding constraint max_trade_usd" in line
    assert "order submitted" in line


def test_a_first_assessment_says_so_rather_than_showing_an_empty_list():
    # An empty block reads as "nothing was decided", which is a different and
    # wrong claim from "this ticker has never been assessed".
    assert "first assessment" in _history_lines([])


def test_a_refused_decision_shows_no_approved_amount_rather_than_zero():
    # NULL and 0 are different facts in `ai_decisions`: the engine refusing is
    # not the engine approving nothing.
    line = _history_lines([_decision(approved_amount_usd=None, trade_status=None)])
    assert "approved none" in line
    assert "order none placed" in line


def test_a_hold_is_shown_with_no_amounts_and_still_names_its_gate():
    line = _history_lines(
        [
            _decision(
                action="HOLD",
                recommended_amount_usd=None,
                approved_amount_usd=None,
                binding_constraint="action_is_hold",
                trade_status=None,
            )
        ]
    )
    assert "HOLD" in line
    assert "asked none" in line
    assert "binding constraint action_is_hold" in line


def test_the_order_the_rows_arrive_in_is_the_order_shown():
    # `recent_decisions` orders most recent first and the prompt says so, so
    # re-sorting here would make that sentence a lie.
    lines = _history_lines(
        [_decision(on_date=date(2026, 9, 12)), _decision(on_date=date(2026, 9, 11))]
    ).splitlines()
    assert "2026-09-12" in lines[0]
    assert "2026-09-11" in lines[1]


def test_the_prompt_carries_the_history_and_says_what_it_does_not_mean():
    prompt = _rendered([_decision()])
    assert "Your own recent decisions on NVDA" in prompt
    assert "binding constraint max_trade_usd" in prompt
    # The model must not read a string of refusals as a verdict on its
    # analysis, nor an order's status as an outcome.
    assert "not about your reasoning" in prompt
    assert "not its outcome" in prompt


def test_the_prompt_still_renders_with_no_history_at_all():
    prompt = _rendered([])
    assert "first assessment" in prompt
    assert "Assess NVDA." in prompt


def test_the_price_history_survived_the_decision_history_being_added():
    # The two nearly collided: the local holding the price bars was called
    # `history` before the parameter of that name existed.
    prompt = _rendered([_decision()])
    assert "2026-09-12 close $224.41" in prompt
    assert "volume 157,000,000" in prompt


# ---------------------------------------------------------------------------
# The spend ceiling
# ---------------------------------------------------------------------------


def test_spending_under_the_ceiling_is_permitted():
    assert _check_budget(D("0.19"), D("1.00")) is None


def test_spending_exactly_the_ceiling_is_permitted():
    # The ceiling is what a run may spend, not what it must stay under: a run
    # failed at its exact budget would be failed for arithmetic.
    assert _check_budget(D("1.00"), D("1.00")) is None


def test_crossing_the_ceiling_stops_the_run():
    with pytest.raises(BudgetExceeded):
        _check_budget(D("1.01"), D("1.00"))


def test_the_failure_names_both_figures_and_the_setting_that_moves_them():
    # This message becomes `agent_runs.error`, which is where anyone will read
    # it — so it has to say what was spent, what the limit was, and which knob
    # changes it, without a second lookup.
    with pytest.raises(BudgetExceeded) as raised:
        _check_budget(D("2.50"), D("1.00"))
    message = str(raised.value)
    assert "$2.50" in message
    assert "$1.00" in message
    assert "MAX_RUN_COST_USD" in message
