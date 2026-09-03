"""Tests for the deterministic risk engine.

The engine is the component that decides what the LLM is actually allowed to
do, so these cover every gate, every cap, and the interactions between them —
particularly the cases where a limit is already breached by a price move rather
than by a trade.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from investagent.models import (
    PortfolioState,
    Position,
    Recommendation,
    RiskLimits,
)
from investagent.risk import evaluate

D = Decimal


def limits(**overrides) -> RiskLimits:
    defaults = dict(
        max_position_gbp=D(100),
        max_trade_gbp=D(100),
        min_trade_gbp=D(5),
        max_concentration_pct=D(30),
        max_total_exposure_pct=D(80),
        max_daily_trades=5,
        min_confidence=0.5,
        allowed_tickers=frozenset({"NVDA", "AAPL", "MSFT"}),
    )
    return RiskLimits(**{**defaults, **overrides})


def state(cash: Decimal = D(420), holdings: dict[str, Decimal] | None = None) -> PortfolioState:
    holdings = {"NVDA": D(80)} if holdings is None else holdings
    return PortfolioState(
        cash_gbp=cash,
        positions=tuple(
            Position(
                ticker=ticker,
                quantity=D("1.0"),
                value_gbp=value,
                avg_cost_gbp=value,
            )
            for ticker, value in holdings.items()
        ),
    )


def rec(
    action: str = "BUY",
    ticker: str = "NVDA",
    amount: Decimal | None = D(50),
    confidence: float = 0.78,
) -> Recommendation:
    return Recommendation(
        ticker=ticker,
        action=action,
        confidence=confidence,
        suggested_amount_gbp=amount,
        reasoning="Datacentre revenue beat expectations.",
        risks="Concentration risk in a single sector.",
    )


# ---------------------------------------------------------------------------
# The worked example from the project brief
# ---------------------------------------------------------------------------


def test_brief_worked_example_buy_50_with_80_held_against_100_cap_approves_20():
    """BUY £50 NVDA, £80 already held, £100 max position -> approve £20."""
    verdict = evaluate(rec(), state(), limits(), trades_today=0)

    assert verdict.approved is True
    assert verdict.approved_amount_gbp == D("20.0000")
    assert verdict.binding_constraint == "max_position_gbp"


def test_worked_example_records_every_cap_it_considered():
    verdict = evaluate(rec(), state(), limits(), trades_today=0)

    caps = {r.constraint: r.cap_gbp for r in verdict.reasons}
    assert caps == {
        "recommended_amount": D("50.0000"),
        "max_trade_gbp": D("100.0000"),
        "max_position_gbp": D("20.0000"),
        # 30% of the £500 total, less the £80 already held.
        "max_concentration_pct": D("70.0000"),
        # 80% of £500, less £80 invested.
        "max_total_exposure_pct": D("320.0000"),
        "available_cash": D("420.0000"),
    }


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def test_hold_is_refused_without_considering_any_cap():
    verdict = evaluate(rec(action="HOLD"), state(), limits(), trades_today=0)

    assert verdict.approved is False
    assert verdict.approved_amount_gbp is None
    assert verdict.binding_constraint == "action_is_hold"
    assert [r.constraint for r in verdict.reasons] == ["action_is_hold"]


def test_ticker_off_the_allowlist_is_refused():
    verdict = evaluate(rec(ticker="GME"), state(holdings={}), limits(), trades_today=0)

    assert verdict.approved is False
    assert verdict.binding_constraint == "ticker_not_allowed"


def test_empty_allowlist_permits_nothing():
    """Fail-closed: a config bug must not become unrestricted trading."""
    verdict = evaluate(rec(), state(), limits(allowed_tickers=frozenset()), trades_today=0)

    assert verdict.approved is False
    assert verdict.binding_constraint == "ticker_not_allowed"


def test_confidence_below_the_floor_is_refused():
    verdict = evaluate(rec(confidence=0.49), state(), limits(min_confidence=0.5), trades_today=0)

    assert verdict.approved is False
    assert verdict.binding_constraint == "confidence_below_floor"


def test_confidence_exactly_at_the_floor_is_allowed():
    verdict = evaluate(rec(confidence=0.5), state(), limits(min_confidence=0.5), trades_today=0)

    assert verdict.approved is True


def test_daily_trade_budget_already_spent_is_refused():
    verdict = evaluate(rec(), state(), limits(max_daily_trades=3), trades_today=3)

    assert verdict.approved is False
    assert verdict.binding_constraint == "daily_trade_limit"


def test_last_trade_of_the_day_is_still_allowed():
    verdict = evaluate(rec(), state(), limits(max_daily_trades=3), trades_today=2)

    assert verdict.approved is True


def test_selling_something_not_held_is_refused():
    verdict = evaluate(rec(action="SELL", ticker="AAPL"), state(), limits(), trades_today=0)

    assert verdict.approved is False
    assert verdict.binding_constraint == "no_position_to_sell"


@pytest.mark.parametrize("amount", [None, D(0), D("-10")])
def test_buy_without_a_positive_amount_is_refused(amount):
    verdict = evaluate(rec(amount=amount), state(), limits(), trades_today=0)

    assert verdict.approved is False
    assert verdict.binding_constraint == "no_amount_recommended"


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------


def test_a_recommendation_inside_every_limit_passes_through_unclamped():
    verdict = evaluate(rec(amount=D(10)), state(), limits(), trades_today=0)

    assert verdict.approved is True
    assert verdict.approved_amount_gbp == D("10.0000")
    # Nothing clamped it, so the recommendation itself is what bound the size.
    assert verdict.binding_constraint == "recommended_amount"


def test_max_trade_clamps_a_large_recommendation():
    verdict = evaluate(
        rec(amount=D(200)),
        state(holdings={}),
        limits(max_trade_gbp=D(25), max_position_gbp=D(1000)),
        trades_today=0,
    )

    assert verdict.approved_amount_gbp == D("25.0000")
    assert verdict.binding_constraint == "max_trade_gbp"


def test_available_cash_clamps_the_trade():
    # The policy ceilings are lifted to 100% deliberately: below that, exposure
    # headroom is always tighter than cash (see the test below), so cash can
    # only be reported as binding when they are level with it.
    verdict = evaluate(
        rec(amount=D(50)),
        state(cash=D(12), holdings={}),
        limits(max_concentration_pct=D(100), max_total_exposure_pct=D(100)),
        trades_today=0,
    )

    assert verdict.approved_amount_gbp == D("12.0000")
    assert verdict.binding_constraint == "available_cash"


def test_the_exposure_ceiling_binds_before_cash_whenever_it_is_under_100_pct():
    """Cash is dominated by the exposure ceiling, and the ordering says so.

    Exposure headroom is `pct x total - invested` and cash is
    `total - invested`, so for any pct below 100 the former is smaller. A £12
    all-cash portfolio against a 30% concentration limit really can only buy
    £3.60, and that is the honest answer rather than "you have £12".
    """
    verdict = evaluate(
        rec(amount=D(50)),
        state(cash=D(12), holdings={}),
        limits(min_trade_gbp=D(1)),
        trades_today=0,
    )

    assert verdict.approved_amount_gbp == D("3.6000")
    assert verdict.binding_constraint == "max_concentration_pct"


def test_concentration_clamps_before_the_absolute_position_cap():
    # £1,000 total, 10% concentration -> £100 for any one name, while the
    # absolute cap would allow £400.
    verdict = evaluate(
        rec(amount=D(500)),
        state(cash=D(1000), holdings={}),
        limits(max_concentration_pct=D(10), max_position_gbp=D(400), max_trade_gbp=D(500)),
        trades_today=0,
    )

    assert verdict.approved_amount_gbp == D("100.0000")
    assert verdict.binding_constraint == "max_concentration_pct"


def test_total_exposure_ceiling_clamps_the_trade():
    # £1,000 total, £700 already invested elsewhere, 80% ceiling -> £100 left.
    verdict = evaluate(
        rec(amount=D(500)),
        state(cash=D(300), holdings={"AAPL": D(400), "MSFT": D(300)}),
        limits(max_total_exposure_pct=D(80), max_trade_gbp=D(500), max_position_gbp=D(500)),
        trades_today=0,
    )

    assert verdict.approved_amount_gbp == D("100.0000")
    assert verdict.binding_constraint == "max_total_exposure_pct"


def test_sell_is_clamped_by_the_size_of_the_holding():
    verdict = evaluate(
        rec(action="SELL", amount=D(500)),
        state(),
        limits(max_trade_gbp=D(1000)),
        trades_today=0,
    )

    assert verdict.approved_amount_gbp == D("80.0000")
    assert verdict.binding_constraint == "position_size"


def test_sell_is_not_constrained_by_cash_concentration_or_exposure():
    """A sell reduces exposure, so those caps must not apply to it."""
    verdict = evaluate(
        rec(action="SELL", amount=D(50)),
        state(cash=D(0), holdings={"NVDA": D(400)}),
        limits(max_concentration_pct=D(1), max_total_exposure_pct=D(1)),
        trades_today=0,
    )

    assert verdict.approved is True
    assert verdict.approved_amount_gbp == D("50.0000")
    considered = {r.constraint for r in verdict.reasons}
    assert considered == {"recommended_amount", "max_trade_gbp", "position_size"}


# ---------------------------------------------------------------------------
# Minimum trade size, and limits already breached without a trade
# ---------------------------------------------------------------------------


def test_a_trade_clamped_below_the_minimum_is_refused():
    verdict = evaluate(
        rec(amount=D(50)),
        state(holdings={"NVDA": D("98")}),
        limits(min_trade_gbp=D(5)),
        trades_today=0,
    )

    assert verdict.approved is False
    assert verdict.approved_amount_gbp is None
    # The minimum is what turned this into a refusal...
    assert verdict.binding_constraint == "below_min_trade_gbp"
    # ...but the £2 of headroom that caused it is still on the record.
    caps = {r.constraint: r.cap_gbp for r in verdict.reasons}
    assert caps["max_position_gbp"] == D("2.0000")


def test_a_recommendation_below_the_minimum_is_refused_outright():
    verdict = evaluate(rec(amount=D(2)), state(), limits(min_trade_gbp=D(5)), trades_today=0)

    assert verdict.approved is False
    assert verdict.binding_constraint == "below_min_trade_gbp"


def test_a_trade_exactly_at_the_minimum_is_allowed():
    verdict = evaluate(rec(amount=D(5)), state(), limits(min_trade_gbp=D(5)), trades_today=0)

    assert verdict.approved is True
    assert verdict.approved_amount_gbp == D("5.0000")


def test_a_position_already_at_its_cap_leaves_no_headroom():
    verdict = evaluate(
        rec(amount=D(50)),
        state(holdings={"NVDA": D(100)}),
        limits(max_position_gbp=D(100)),
        trades_today=0,
    )

    assert verdict.approved is False
    assert verdict.approved_amount_gbp is None
    assert verdict.binding_constraint == "max_position_gbp"


def test_a_position_pushed_over_its_cap_by_a_price_rise_never_yields_a_negative_cap():
    """A £150 holding against a £100 cap is -£50 of headroom, not a £50 trade."""
    verdict = evaluate(
        rec(amount=D(50)),
        state(holdings={"NVDA": D(150)}),
        limits(max_position_gbp=D(100)),
        trades_today=0,
    )

    assert verdict.approved is False
    assert verdict.approved_amount_gbp is None
    caps = {r.constraint: r.cap_gbp for r in verdict.reasons}
    assert caps["max_position_gbp"] == D("0.0000")
    assert all(cap >= 0 for cap in caps.values())


def test_exposure_already_over_the_ceiling_yields_no_headroom():
    verdict = evaluate(
        rec(ticker="MSFT", amount=D(50)),
        state(cash=D(10), holdings={"AAPL": D(500), "NVDA": D(490)}),
        limits(max_total_exposure_pct=D(50), max_position_gbp=D(1000)),
        trades_today=0,
    )

    assert verdict.approved is False
    assert verdict.binding_constraint == "max_total_exposure_pct"


# ---------------------------------------------------------------------------
# Arithmetic and purity
# ---------------------------------------------------------------------------


def test_percentage_caps_round_down_so_a_limit_is_never_exceeded():
    # 30% of £333.3333 is £99.99999, which must floor to £99.9999 rather than
    # rounding up past the ceiling.
    verdict = evaluate(
        rec(amount=D(500)),
        state(cash=D("333.3333"), holdings={}),
        limits(max_concentration_pct=D(30), max_trade_gbp=D(500), max_position_gbp=D(500)),
        trades_today=0,
    )

    assert verdict.approved_amount_gbp == D("99.9999")


def test_the_approved_amount_never_exceeds_available_cash():
    verdict = evaluate(
        rec(amount=D(50)),
        state(cash=D("19.99999"), holdings={}),
        limits(),
        trades_today=0,
    )

    assert verdict.approved_amount_gbp is not None
    assert verdict.approved_amount_gbp <= D("19.99999")


def test_evaluate_does_not_mutate_the_state_it_is_given():
    original = state()
    before = original.model_dump()

    evaluate(rec(), original, limits(), trades_today=0)

    assert original.model_dump() == before


def test_evaluate_is_deterministic():
    args = (rec(), state(), limits(), 0)

    assert evaluate(*args) == evaluate(*args)


def test_a_refusal_never_reports_a_constraint_twice():
    """`risk_verdict` must not contain a cap and a bare repeat of its name.

    The refusal path used to append a fresh reason for the binding constraint
    on top of the cap already recorded, which put a contradictory pair into the
    audit record — the cap with its value, and the same name again with none.
    """
    verdict = evaluate(
        rec(amount=D(50)),
        state(holdings={"NVDA": D(150)}),
        limits(max_position_gbp=D(100)),
        trades_today=0,
    )

    cited = [r.constraint for r in verdict.reasons]
    assert len(cited) == len(set(cited))


def test_every_verdict_cites_its_binding_constraint_in_its_reasons():
    scenarios = [
        (rec(action="HOLD"), state(), limits(), 0),
        (rec(ticker="GME"), state(), limits(), 0),
        (rec(confidence=0.1), state(), limits(), 0),
        (rec(), state(), limits(max_daily_trades=1), 1),
        (rec(action="SELL", ticker="AAPL"), state(), limits(), 0),
        (rec(amount=None), state(), limits(), 0),
        (rec(amount=D(10)), state(), limits(), 0),
        (rec(amount=D(500)), state(), limits(), 0),
        (rec(amount=D(50)), state(holdings={"NVDA": D(150)}), limits(), 0),
        (rec(amount=D(1)), state(), limits(), 0),
    ]

    for args in scenarios:
        verdict = evaluate(*args)
        assert verdict.binding_constraint in {r.constraint for r in verdict.reasons}


def test_an_approved_amount_never_exceeds_the_binding_cap():
    verdict = evaluate(rec(), state(), limits(), trades_today=0)

    caps = {r.constraint: r.cap_gbp for r in verdict.reasons}
    assert verdict.approved_amount_gbp == caps[verdict.binding_constraint]
    assert all(verdict.approved_amount_gbp <= cap for cap in caps.values())
