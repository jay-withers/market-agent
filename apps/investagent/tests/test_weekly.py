"""Tests for the weekly review's facts tables and its short-circuits.

The governing rule is the daily summary's: **every figure the reader sees comes
from the database**. The model writes the assessment and the proposals and is
shown the numbers as tables it is told not to restate, so what goes into those
tables decides what the review can truthfully say — and the subject line is
built here rather than by the model.

Nothing in this file touches a database or the network.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from investagent.benchmarks import CASH_SYMBOL
from investagent.jobs import weekly
from investagent.jobs.weekly import (
    _facts_table,
    _prompt,
    _proposals_table,
    _subject,
    _week_change,
)
from investagent.models import ProposedChange, RiskLimits

D = Decimal
END = date(2026, 9, 6)
START = date(2026, 8, 31)
INCEPTION = date(2026, 8, 20)


def _limits(**overrides) -> RiskLimits:
    """Built explicitly rather than through `risklimits.limits()`, which reads
    the environment and a local `.env` — a developer's own settings must not
    change what these assert."""
    defaults = dict(
        max_position_gbp=D(100),
        max_trade_gbp=D(50),
        min_trade_gbp=D(5),
        max_concentration_pct=D(25),
        max_total_exposure_pct=D(80),
        max_daily_trades=3,
        min_confidence=0.6,
        allowed_tickers=frozenset({"NVDA", "MSFT"}),
    )
    return RiskLimits(**{**defaults, **overrides})


def _metrics(**overrides) -> dict:
    defaults = dict(
        runs={
            "runs": 7,
            "succeeded": 6,
            "failed": 1,
            "unfinished": 0,
            "dry_runs": 0,
            "news_fetched": 588,
            "news_relevant": 350,
            "decisions_made": 63,
            "trades_executed": 12,
            "input_tokens": 599_501,
            "output_tokens": 81_606,
            "cost_usd": D("1.310000"),
            "avg_seconds": D("246.4"),
        },
        decisions=[
            {"action": "BUY", "decisions": 18, "avg_confidence": D("0.71"), "approved": 9},
            {"action": "HOLD", "decisions": 45, "avg_confidence": D("0.55"), "approved": 0},
        ],
        constraints=[
            {"binding": "action_is_hold", "approved": False, "decisions": 45},
            {"binding": "daily_trade_limit", "approved": False, "decisions": 9},
            {"binding": "recommended_amount", "approved": True, "decisions": 6},
            {"binding": "max_trade_gbp", "approved": True, "decisions": 3},
        ],
        trades=[
            {"status": "filled", "trades": 9, "simulated": 0, "notional_gbp": D("270.0000")},
            {"status": "submitted", "trades": 3, "simulated": 0, "notional_gbp": D("90.0000")},
        ],
        tickers=[
            {
                "ticker": "NVDA",
                "decisions": 7,
                "convictions": 3,
                "relevant_news": 41,
                "trades": 3,
            },
            {"ticker": "MSFT", "decisions": 0, "convictions": 0, "relevant_news": 0, "trades": 0},
        ],
        valuation={
            "as_of": END,
            "total_value_gbp": D("507.4000"),
            "cash_gbp": D("330.0000"),
            "positions_value_gbp": D("177.4000"),
            "pnl_gbp": D("7.4000"),
            "pnl_pct": D("1.4800"),
        },
        opening_total_gbp=D("502.0000"),
        benchmarks=[
            {"symbol": "SPY", "value_gbp": D("512.0000"), "opening_gbp": D("508.0000")},
            {"symbol": CASH_SYMBOL, "value_gbp": D("501.1000"), "opening_gbp": D("501.0000")},
        ],
        summaries={"days": 7, "sent": 6, "failed": 1, "skipped": 0},
    )
    return {**defaults, **overrides}


def _table(**overrides) -> str:
    return _facts_table(START, END, _metrics(**overrides), D("500.0000"), INCEPTION, _limits())


# ---------------------------------------------------------------------------
# The figures, all of which come from the database
# ---------------------------------------------------------------------------


def test_the_valuation_is_the_stored_one_and_says_which_day_it_is_from():
    """The last daily_performance row in the window, which on a Sunday run is
    that evening's — not a figure this job computed for itself."""
    table = _table()

    assert "| Total value | £507.4000 (as at 2026-09-06) |" in table
    assert "| Cash | £330.0000 |" in table
    assert "| Since inception | £+7.4000 (+1.4800%) |" in table


def test_the_week_is_measured_against_the_last_value_before_it():
    """£507.40 against £502.00, not against the first row inside the window —
    otherwise a "week" is six days plus whatever the first row happened to be."""
    change, pct = _week_change(_metrics())

    assert change == D("5.4000")
    assert pct == D("1.0756")
    assert "| Change this week | £+5.4000 (+1.0756%) |" in _table()


def test_a_first_week_reports_no_comparison_rather_than_a_change_of_zero():
    assert _week_change(_metrics(opening_total_gbp=None)) == (None, None)
    assert "no valuation before this week to compare against" in _table(opening_total_gbp=None)


def test_a_week_with_no_stored_valuation_says_so_instead_of_inventing_one():
    """No summary ran, so there is no total. A missing figure is the week's
    finding, and a zero in its place would be a false one."""
    table = _table(valuation=None)

    assert "| Total value | not recorded this week |" in table
    assert "£0.0000" not in table


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


def test_benchmarks_are_labelled_and_the_cash_arm_is_named():
    table = _table()

    assert "| SPY (proxy) | £512.0000 | £+4.0000 (+0.7874%) |" in table
    assert "| Savings at 5% | £501.1000 |" in table


def test_a_benchmark_with_no_earlier_value_shows_no_change_rather_than_a_wrong_one():
    table = _table(benchmarks=[{"symbol": "SPY", "value_gbp": D("512.0000"), "opening_gbp": None}])

    assert "no earlier value to compare" in table


def test_a_week_with_no_benchmark_data_says_so_rather_than_drawing_a_flat_line():
    """£500 unchanged reads as "the index did nothing", which is a different
    and wrong claim from "we have no data"."""
    assert "No benchmark data for this week." in _table(benchmarks=[])


def test_the_portfolios_own_week_is_stated_beside_the_alternatives():
    """Otherwise the model has to subtract two rows itself to answer the one
    question the section exists for."""
    assert "change over the same week was +1.0756%" in _table()


# ---------------------------------------------------------------------------
# The risk engine — the section the review exists for
# ---------------------------------------------------------------------------


def test_the_limits_in_force_are_stated_with_the_constraints_that_bound():
    """A constraint that bound nine times means nothing without the value it
    was binding against, and the value means nothing without the count."""
    table = _table()

    assert "| Trades per day | 3 | `RISK_MAX_DAILY_TRADES` |" in table
    assert "| Largest single trade | £50 | `RISK_MAX_TRADE_GBP` |" in table
    assert "| daily_trade_limit | 9 |" in table


def test_each_limit_is_shown_with_the_setting_that_changes_it():
    """A proposal has to name the knob to be actionable, and the model has no
    other way to learn what these are called."""
    table = _table()

    assert "`RISK_MAX_CONCENTRATION_PCT`" in table
    assert "`RISK_MIN_CONFIDENCE`" in table


def test_refusals_and_approvals_are_split_rather_than_pooled():
    """`recommended_amount` binding an approval and `daily_trade_limit`
    refusing one are opposite facts about the engine, and one table would read
    as a single ranking of "constraints that fired"."""
    table = _table()
    refused, approved = table.split("| Approved, bound by | Decisions |")

    assert "| action_is_hold | 45 |" in refused
    assert "| action_is_hold | 45 |" not in approved
    assert "| recommended_amount | 6 |" in approved


def test_an_approval_bound_by_the_recommendation_is_explained_as_no_clamp():
    """It is the one entry in the approved table that is not a limit biting,
    and unexplained it looks like one."""
    assert "means no limit reduced the trade" in _table()


def test_a_week_with_no_decisions_says_no_constraint_bound():
    table = _table(constraints=[], decisions=[])

    assert "no constraint bound" in table


def test_nothing_refused_and_nothing_approved_are_stated_as_zero_not_omitted():
    table = _table(
        constraints=[{"binding": "recommended_amount", "approved": True, "decisions": 4}]
    )

    assert "| nothing was refused | 0 |" in table


# ---------------------------------------------------------------------------
# Activity, trades, and the watchlist
# ---------------------------------------------------------------------------


def test_the_weeks_activity_is_stated_before_anything_readable_as_idleness():
    """The daily summary learned this the hard way: given a table that implied
    nothing had happened, the model correctly reported that nothing had."""
    table = _table()

    assert "| Decisions | 63 |" in table
    assert "(6 succeeded, 1 failed, 0 never finished)" in table
    assert "| LLM cost | $1.310000 (599501 input, 81606 output tokens) |" in table
    assert "246s against a 1800s timeout" in table


def test_a_run_that_never_finished_leaves_no_average_rather_than_a_zero():
    runs = {**_metrics()["runs"], "avg_seconds": None}

    assert "| Average run | — against a 1800s timeout |" in _table(runs=runs)


def test_a_simulated_trade_is_reported_as_having_happened():
    """A dry run moves the portfolio without sending an order, and a table that
    only counted broker fills reported a day of three trades as unchanged."""
    table = _table(
        trades=[{"status": "simulated", "trades": 3, "simulated": 3, "notional_gbp": D("90.0000")}]
    )

    assert "| simulated | 3 | 3 | £90.0000 |" in table
    assert "no order reached the broker" in table


def test_a_week_with_no_trades_says_so_explicitly():
    assert "No trades were made this week." in _table(trades=[])


def test_a_watchlist_name_the_agent_never_reached_appears_as_a_row_of_zeros():
    """Driven by `companies`, not by the decisions — a ticker with nothing
    against it is precisely the row worth reading, and a join from
    `ai_decisions` would omit it."""
    table = _table()

    assert "| MSFT | 0 | 0 | 0 | 0 |" in table


def test_a_week_of_failed_emails_is_reported():
    """The 2026-09-03 send was lost to a bad recipient and nothing but this
    line would have said so."""
    assert "7 of 7 days have a stored summary: 6 emailed, 1 failed to send" in _table()


# ---------------------------------------------------------------------------
# The subject line and the proposals, neither written by the model
# ---------------------------------------------------------------------------


def _proposal(**overrides) -> ProposedChange:
    defaults = dict(
        area="risk_limits",
        change="Raise RISK_MAX_DAILY_TRADES from 3 to 5.",
        rationale="daily_trade_limit refused nine of eighteen BUYs.",
        expected_effect="More approvals; more exposure sooner.",
        confidence=0.7,
    )
    return ProposedChange(**{**defaults, **overrides})


def test_the_subject_carries_the_stored_total_and_the_count_of_proposals():
    subject = _subject(END, _metrics(), [_proposal(), _proposal()])

    assert subject == "InvestAgent week to 2026-09-06: £507.4000 (+1.0756% this week), 2 proposals"


def test_the_subject_is_singular_for_one_proposal():
    assert _subject(END, _metrics(), [_proposal()]).endswith("1 proposal")


def test_a_subject_with_no_valuation_says_so_rather_than_carrying_a_figure():
    subject = _subject(END, _metrics(valuation=None), [])

    assert subject == "InvestAgent week to 2026-09-06: no valuation recorded, 0 proposals"


def test_a_first_week_subject_carries_the_total_without_a_change():
    subject = _subject(END, _metrics(opening_total_gbp=None), [])

    assert subject == "InvestAgent week to 2026-09-06: £507.4000, 0 proposals"


def test_every_field_of_a_proposal_is_rendered():
    """Rendered here rather than written as prose by the model, so the email
    and `weekly_reviews.recommendations` cannot disagree."""
    rendered = _proposals_table([_proposal()])

    assert "### 1. Raise RISK_MAX_DAILY_TRADES from 3 to 5." in rendered
    assert "**Area:** risk_limits · **Confidence:** 0.70" in rendered
    assert "**Why:** daily_trade_limit refused nine of eighteen BUYs." in rendered
    assert "**Expected effect:** More approvals" in rendered


@pytest.mark.parametrize("proposals", [[], [_proposal()]])
def test_the_proposals_are_always_labelled_advisory(proposals):
    """Nothing reads them back and applies them, and a reader who assumed
    otherwise would be waiting for a change that never comes."""
    assert "advisory" in _proposals_table(proposals)


def test_no_proposals_is_reported_as_a_conclusion_not_as_a_gap():
    assert "did not support a change" in _proposals_table([])


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------


def test_the_prompt_carries_the_facts_and_asks_for_the_assessment():
    prompt = _prompt("FACTS", [])

    assert "FACTS" in prompt
    assert "propose the changes" in prompt
    assert "previous review" not in prompt


def test_last_weeks_proposals_are_included_so_the_review_compounds():
    prompt = _prompt("FACTS", [{"area": "watchlist", "change": "Drop MSFT."}])

    assert "- (watchlist) Drop MSFT." in prompt
    # And the model is told what the list cannot tell it: a proposal that was
    # rejected and one nobody read look identical here.
    assert "does not say whether any of them were acted on" in prompt


# ---------------------------------------------------------------------------
# run(), far enough to prove it does not pay for a review of nothing
# ---------------------------------------------------------------------------


class _ExplodingLlm:
    def review(self, _prompt: str):
        raise AssertionError("the model must not be called on a week with no runs")


def test_a_week_with_no_agent_runs_is_not_reviewed_at_all(monkeypatch):
    """An LLM call to say nothing happened is worth neither the money nor the
    credibility of a stored row that reviews nothing."""
    import contextlib

    class _Pool:
        def connection(self):
            return contextlib.nullcontext(object())

    monkeypatch.setattr(weekly, "pool", lambda: _Pool())
    monkeypatch.setattr(weekly.repo, "portfolio_id", lambda _c: 1)
    monkeypatch.setattr(weekly.repo, "initial_cash", lambda _c, _p: D("500.0000"))
    monkeypatch.setattr(weekly.repo, "portfolio_inception", lambda _c, _p: INCEPTION)
    monkeypatch.setattr(weekly.repo, "active_tickers", lambda _c: ["NVDA"])
    monkeypatch.setattr(weekly.repo, "last_recommendations", lambda _c, before: [])
    monkeypatch.setattr(
        weekly.repo,
        "week_metrics",
        lambda _c, _p, _s, _e: _metrics(runs={"runs": 0}),
    )

    assert weekly.run(as_of=END, llm=_ExplodingLlm()) is None
