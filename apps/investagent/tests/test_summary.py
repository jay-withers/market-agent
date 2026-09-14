"""Tests for the daily summary's facts table.

The governing rule is that **every figure the reader sees comes from the
database**. The model writes commentary and is shown the numbers as a table it
is told not to restate, so what goes into that table decides what the narrative
can truthfully say.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from investagent.benchmarks import CASH_SYMBOL, BenchmarkPoint
from investagent.jobs.summary import _facts_table, _prompt, _spend_section
from investagent.models import PortfolioState, Position

D = Decimal
TODAY = date(2026, 9, 3)


def _state() -> PortfolioState:
    return PortfolioState(
        cash_gbp=D("380.0000"),
        positions=(
            Position(
                ticker="AVGO",
                quantity=D("0.146852"),
                value_gbp=D("40.0000"),
                avg_cost_gbp=D("272.3830"),
            ),
        ),
    )


def _points() -> list[BenchmarkPoint]:
    return [
        BenchmarkPoint(CASH_SYMBOL, TODAY, D("500.0684"), source="computed"),
        BenchmarkPoint("SPY", TODAY, D("512.0000"), close_usd=D("640.00")),
    ]


def _activity(trades=None, decisions=None, holdings=None) -> dict:
    return {
        "trades": trades if trades is not None else [],
        "decisions": decisions if decisions is not None else [],
        "holdings": holdings if holdings is not None else [],
    }


def _table(**kwargs) -> str:
    return _facts_table(
        TODAY,
        _state(),
        D("500.0000"),
        D("-0.0050"),
        D("-0.0010"),
        _points(),
        kwargs.pop("filled", 0),
        _activity(**kwargs),
    )


# ---------------------------------------------------------------------------
# The trades section — the regression this file exists for
# ---------------------------------------------------------------------------


def test_a_simulated_trade_is_reported_as_having_happened():
    """The bug this replaces: the table showed only a reconciliation count,
    which is zero on a dry run because a simulated trade never reaches a
    broker. The model then reported, correctly from what it was told, that cash
    and positions were unchanged — on a day three trades had executed."""
    table = _table(trades=[("AVGO", "BUY", "simulated", D("40.0000"), D("0.146852"), D("272.38"))])

    assert "## Trades today" in table
    assert "| AVGO | BUY | simulated | £40.0000 | 0.146852 |" in table
    # And the meaning of `simulated` is spelled out, not left to be inferred.
    assert "no order was sent to the broker" in table


def test_a_day_with_no_trades_says_so_explicitly():
    assert "No trades were made." in _table()


def test_an_unfilled_trade_says_the_quantity_is_not_known_yet():
    """A notional order names no quantity until it fills, and a blank cell
    would read as zero shares."""
    table = _table(trades=[("MSFT", "BUY", "submitted", D("40.0000"), None, None)])

    assert "not yet known" in table


def test_the_reconciliation_count_is_stated_alongside_the_trades():
    table = _table(
        trades=[("AVGO", "BUY", "filled", D("40.0000"), D("0.1"), D("272.38"))], filled=2
    )

    assert "2 order(s) were reconciled" in table


# ---------------------------------------------------------------------------
# The rest of the table
# ---------------------------------------------------------------------------


def test_the_headline_figures_come_from_the_database():
    table = _table()

    assert "| Total value | £499.9950 |" not in table  # derived from state, not passed
    assert "| Cash | £380.0000 |" in table
    assert "| Started with | £500.0000 |" in table


def test_benchmarks_are_labelled_and_the_cash_arm_is_named():
    table = _table()

    assert "| Savings at 5% | £500.0684 |" in table
    # "proxy" in the label, because EWU is not the FTSE 100 and SPY is not the
    # index itself.
    assert "| SPY (proxy) | £512.0000 |" in table


def test_a_refused_decision_shows_a_dash_rather_than_a_zero():
    """£0.00 approved and "refused" are different facts."""
    table = _table(decisions=[("MSFT", "BUY", D("0.62"), None, "reasons", "daily_trade_limit")])

    assert "| MSFT | BUY | 0.62 | — | daily_trade_limit |" in table


def test_the_prompt_carries_the_models_own_reasoning():
    activity = _activity(
        decisions=[("AVGO", "BUY", D("0.62"), D("40"), "Raised AI guidance.", "recommended")]
    )

    prompt = _prompt("FACTS", activity)

    assert "FACTS" in prompt
    assert "- AVGO (BUY): Raised AI guidance." in prompt


def test_the_prompt_handles_a_day_with_no_decisions():
    assert "No decisions were taken." in _prompt("FACTS", _activity())


# ---------------------------------------------------------------------------
# Model spend and the credit runway
# ---------------------------------------------------------------------------


def _spend(**overrides) -> dict:
    """A row as `repository.spend` returns it."""
    defaults = dict(
        today_usd=D("0.190000"),
        last_7_days_usd=D("1.400000"),
        to_date_usd=D("5.000000"),
        known_from=date(2026, 8, 20),
    )
    return {**defaults, **overrides}


def _spend_text(spend=None, credit=None) -> str:
    return "\n".join(_spend_section(spend or _spend(), credit))


def test_the_spend_figures_are_the_stored_ones():
    text = _spend_text()
    assert "$0.19" in text
    assert "$1.40" in text
    assert "$5.00" in text


def test_the_daily_rate_is_the_last_seven_days_not_all_time():
    # The question behind the figure is "how long does this last at the rate it
    # is going now", which an average over the whole experiment answers wrongly.
    assert "$0.20/day" in _spend_text()


def test_no_credit_configured_reports_spend_and_no_runway():
    text = _spend_text()
    assert "Runway" not in text
    assert "Credit remaining" not in text
    assert "No starting credit is configured" in text


def test_a_configured_credit_gives_what_is_left_and_a_runway():
    text = _spend_text(credit=D("25.00"))
    assert "$20.00 of $25.00" in text
    # 20.00 remaining at 0.20/day.
    assert "about 100 days" in text


def test_the_runway_is_rounded_down_rather_than_up():
    # A runway is a limit like any other here: rounding it up would promise a
    # day that is not paid for.
    # $5.00 left at $0.30/day is 16.67 days, and 16 is the honest half.
    text = _spend_text(spend=_spend(last_7_days_usd=D("2.100000")), credit=D("10.000000"))
    assert "about 16 days" in text


def test_exhausted_credit_says_so_rather_than_reporting_zero_days():
    text = _spend_text(spend=_spend(to_date_usd=D("30.000000")), credit=D("25.00"))
    assert "none — the recorded spend has reached the credit" in text
    assert "about" not in text


def test_a_week_with_no_spend_reports_no_runway_rather_than_dividing_by_zero():
    text = _spend_text(spend=_spend(last_7_days_usd=D("0")), credit=D("25.00"))
    assert "not estimable" in text


def test_the_scope_of_the_figures_is_stated_not_left_to_be_inferred():
    # These figures cannot include the call that writes the email, and the
    # credit is a number a human typed rather than anything checked against the
    # account. Both have to be on the page, or the model may describe the
    # figure as complete.
    text = _spend_text(credit=D("25.00"))
    assert "excludes the call that writes this email" in text
    assert "no balance endpoint" in text
    assert "from 2026-08-20 onwards" in text


def test_a_database_with_no_recorded_spend_says_so():
    text = _spend_text(spend=_spend(known_from=None))
    assert "No spend has been recorded yet" in text


def test_the_spend_section_is_omitted_when_the_caller_passes_none():
    # The facts table is built by the weekly review's tests and by anything
    # else that renders a day; a missing spend must not become "$0 spent".
    assert "Model spend" not in _table()


def test_the_facts_table_carries_the_spend_section_when_given_one():
    table = _facts_table(
        TODAY,
        _state(),
        D("500.0000"),
        D("-0.0050"),
        D("-0.0010"),
        _points(),
        0,
        _activity(),
        _spend(),
        D("25.00"),
    )
    assert "## Model spend" in table
    assert "Credit remaining" in table
