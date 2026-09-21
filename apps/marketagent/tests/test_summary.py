"""Tests for the daily summary's facts table.

The governing rule is that **every figure the reader sees comes from the
database**. The model writes commentary and is shown the numbers as a table it
is told not to restate, so what goes into that table decides what the narrative
can truthfully say.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal

from marketagent.benchmarks import CASH_SYMBOL, BenchmarkPoint
from marketagent.jobs import summary
from marketagent.jobs.summary import (
    _facts_table,
    _prompt,
    _run_alert,
    _run_section,
    _spend_section,
)
from marketagent.models import PortfolioState, Position

D = Decimal
TODAY = date(2026, 9, 3)


def _state() -> PortfolioState:
    return PortfolioState(
        cash_usd=D("380.0000"),
        positions=(
            Position(
                ticker="AVGO",
                quantity=D("0.146852"),
                value_usd=D("40.0000"),
                avg_cost_usd=D("272.3830"),
            ),
        ),
    )


def _points() -> list[BenchmarkPoint]:
    return [
        BenchmarkPoint(CASH_SYMBOL, TODAY, D("500.0684"), source="computed"),
        BenchmarkPoint("SPY", TODAY, D("512.0000"), close_usd=D("640.00")),
    ]


def _run(status="succeeded", finished=datetime(2026, 9, 3, 6, 4), error=None, stale=False):
    """One `agent_runs` row as `day_activity` returns it."""
    return (datetime(2026, 9, 3, 6, 0), finished, status, "schedule", False, error, stale)


def _activity(trades=None, decisions=None, holdings=None, runs=None) -> dict:
    return {
        # A clean scheduled run by default, so every other test in this file
        # goes on testing what it was written to test.
        "runs": runs if runs is not None else [_run()],
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
    assert "| AVGO | BUY | simulated | $40.0000 | 0.146852 |" in table
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

    assert "| Total value | $499.9950 |" not in table  # derived from state, not passed
    assert "| Cash | $380.0000 |" in table
    assert "| Started with | $500.0000 |" in table


def test_benchmarks_are_labelled_and_the_cash_arm_is_named():
    table = _table()

    assert "| Savings at 5% | $500.0684 |" in table
    # "proxy" in the label, because EWU is not the FTSE 100 and SPY is not the
    # index itself.
    assert "| SPY (proxy) | $512.0000 |" in table


def test_a_refused_decision_shows_a_dash_rather_than_a_zero():
    """$0.00 approved and "refused" are different facts."""
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


# ---------------------------------------------------------------------------
# The FX fallback: an upstream outage costs accuracy, not the whole day
# ---------------------------------------------------------------------------


@contextmanager
def _fake_pool(_conn=None):
    """Stands in for `pool().connection()`, which the fallback path reaches for."""

    class _Pool:
        @staticmethod
        @contextmanager
        def connection():
            yield _conn

    yield _Pool


def _patch_fx(monkeypatch, *, fetch, stored):
    monkeypatch.setattr(summary, "fetch_gbp_usd", fetch)
    monkeypatch.setattr(summary, "pool", lambda: _fake_pool().__enter__())
    monkeypatch.setattr(summary.repo, "last_fx_rate", lambda _conn, _pid: stored)


# ---------------------------------------------------------------------------
# The agent run section: "no trades" and "never ran" must not read alike
# ---------------------------------------------------------------------------


def test_a_day_with_no_agent_run_says_the_agent_did_not_run():
    """The gap this section exists for. `No trades were made.` is the same
    sentence whether the model held every position or the 06:00 job died
    before its first analysis, and every other section is silent in exactly
    the same way — so nothing else in the email can tell the two apart."""
    table = _table(runs=[])

    assert "No agent run is recorded for today." in table
    # And it must not be left to be inferred from the empty sections below it.
    assert "did not run" in table


def test_a_failed_run_is_reported_with_its_error():
    table = _table(runs=[_run(status="failed", error="BudgetExceeded: $1.02 > $1.00")])

    assert "| failed |" in table
    assert "BudgetExceeded: $1.02 > $1.00" in table


def test_a_run_still_running_past_the_job_timeout_reads_as_abandoned():
    """SIGKILL cannot be caught, so the row is never closed and the replica is
    long gone. Reporting it as `running` at 21:00 would claim work in progress
    that Container Apps terminated fifteen hours earlier."""
    table = _table(runs=[_run(status="running", finished=None, stale=True)])

    assert "abandoned" in table
    assert "| running |" not in table
    # No finish time to report, and a blank cell would read as a missing value.
    assert "| — |" in table


def test_a_clean_run_is_reported_without_alarm():
    table = _table(runs=[_run()])

    assert "| succeeded |" in table
    assert "No agent run is recorded" not in table


def test_a_dry_run_is_marked_as_one():
    started, finished, status, trigger, _dry, error, stale = _run()
    table = _table(runs=[(started, finished, status, trigger, True, error, stale)])

    assert "succeeded (dry run)" in table


# ---------------------------------------------------------------------------
# The subject line, which is the part that reaches a phone's lock screen
# ---------------------------------------------------------------------------


def test_a_clean_day_earns_no_subject_warning():
    assert _run_alert([_run()]) is None


def test_the_subject_warns_when_nothing_ran():
    """A day the agent died still has a valuation, so the subject is otherwise
    indistinguishable in an inbox from a day it worked."""
    assert _run_alert([]) == "no agent run"


def test_the_subject_warns_on_a_failed_run():
    assert _run_alert([_run(status="failed", error="boom")]) == "agent run failed"


def test_the_subject_warns_on_an_abandoned_run():
    assert _run_alert([_run(status="running", finished=None, stale=True)]) == "agent run abandoned"


def test_a_failure_outranks_a_success_on_the_same_day():
    """A manual re-run that succeeded does not erase the scheduled one that
    did not — the subject reports the worst outcome of the day, not the last."""
    runs = [_run(status="failed", error="boom"), _run()]

    assert _run_alert(runs) == "agent run failed"


def test_the_run_section_reaches_the_model():
    """The commentary is written from the facts table, so a failed run has to
    be visible there or the model narrates a quiet day."""
    prompt = _prompt(_table(runs=[]), _activity())

    assert "No agent run is recorded for today." in prompt


def test_every_run_of_the_day_is_listed():
    """A scheduled run and a manual retry are two rows, and collapsing them
    would hide either the failure or the recovery."""
    section = "\n".join(_run_section([_run(status="failed", error="boom"), _run()]))

    assert section.count("| 06:00 |") == 2
