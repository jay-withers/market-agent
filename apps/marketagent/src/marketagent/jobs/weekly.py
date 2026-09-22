"""The weekly review: how the experiment is doing, and what to change.

Runs at 22:00 UTC on Sunday, an hour after the last daily summary of the week,
because it reads that summary's valuation as the week's closing figure.

It answers a question a single day cannot. The daily email says what happened;
this says whether the *machinery* is working — which risk limit is actually
shaping each account, which watchlist names never produce anything, whether
the jobs ran and the emails sent — and proposes changes. Covers both accounts
in one review, with a comparison between them, for the same reason the daily
summary does: the point of running them side by side is to compare them.

Three rules shape it:

* **Reads only.** No prices, no news, no broker. The agent and the summary
  have already written the week down, and a review that re-fetched could
  report figures the stored series disagrees with.
* **Every figure comes from the database.** The model is shown the numbers as
  tables it is told not to restate, exactly as in the daily summary, and the
  subject line is built from the stored totals. Nothing it writes can become a
  reported balance.
* **Its proposals are advisory.** They are stored and emailed for a human to
  act on; nothing reads them back and applies them. A risk limit changes when
  someone edits `risklimits.py`, which is the entire point of the limits being
  deterministic and outside the model's reach.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import markdown as markdown_lib

from .. import repository as repo
from ..benchmarks import CASH_SYMBOL
from ..db import pool
from ..llm.anthropic_provider import AnthropicLlm
from ..llm.base import PROMPT_VERSION, Llm
from ..mailer import MailResult, send
from ..models import ProposedChange, RiskLimits, money
from ..risklimits import limits as risk_limits

logger = logging.getLogger(__name__)

# Seven days, inclusive of both ends, so a Sunday run covers the Monday before.
REVIEW_DAYS = 7

ACCOUNTS = ("static-100", "dynamic-500")


def run(
    as_of: date | None = None,
    llm: Llm | None = None,
) -> int | None:
    """Produce and send one week's combined review.

    Returns the `weekly_reviews` id, or None on a week with no agent runs at
    all, for either account — there is nothing to review, and an LLM call to
    say so is worth neither the money nor the credibility of a row that
    reviews nothing.
    """
    as_of = as_of or datetime.now(UTC).date()
    start = as_of - timedelta(days=REVIEW_DAYS - 1)
    llm = llm or AnthropicLlm()

    accounts: dict[str, dict] = {}
    with pool().connection() as conn:
        previous = repo.last_recommendations(conn, before=start)
        for name in ACCOUNTS:
            pid = repo.portfolio_id(conn, name=name)
            accounts[name] = {
                "pid": pid,
                "initial": repo.initial_cash(conn, pid),
                "inception": repo.portfolio_inception(conn, pid),
                "tickers": repo.active_tickers(conn, pid),
                "metrics": repo.week_metrics(conn, pid, start, as_of),
            }

    total_runs = sum(accounts[n]["metrics"]["runs"].get("runs") or 0 for n in ACCOUNTS)
    if not total_runs:
        logger.info(
            "no agent runs for either account between %s and %s, nothing to review", start, as_of
        )
        return None

    # After the short-circuit, not before: a week with no runs at all is not
    # reviewed, so there is nowhere to report a check and nothing to spend the
    # query on. (That week is itself the loudest possible failure and this job
    # stays silent through it — the daily email is what covers it, one day at
    # a time.)
    #
    # Stored alongside the rest, so "has this fired three weeks running" is a
    # query against weekly_reviews.metrics rather than a re-read of the prose.
    with pool().connection() as conn:
        for name in ACCOUNTS:
            data = accounts[name]
            data["metrics"]["integrity"] = repo.week_integrity(conn, data["pid"], start, as_of)

    # The limits each account's engine would actually build, not a second read
    # of the same environment variables: the table has to show what actually
    # bounded the week's decisions, and two readers of one config can drift.
    for name in ACCOUNTS:
        data = accounts[name]
        data["lim"] = risk_limits(frozenset(data["tickers"]))

    facts = _facts_table(start, as_of, accounts)
    review = llm.review(_prompt(facts, previous))

    proposals = list(review.value.proposals)
    body_markdown = f"{facts}\n\n{review.value.assessment}\n\n{_proposals_table(proposals)}"
    body_html = markdown_lib.markdown(body_markdown, extensions=["tables"])
    subject = _subject(as_of, accounts, proposals)

    result: MailResult = send(subject, body_html, body_markdown)
    logger.info("weekly review email %s", result.status)

    with pool().connection() as conn:
        review_id = repo.save_weekly_review(
            conn,
            period_start=start,
            period_end=as_of,
            subject=subject,
            assessment=review.value.assessment,
            body_markdown=body_markdown,
            body_html=body_html,
            metrics={name: accounts[name]["metrics"] for name in ACCOUNTS},
            recommendations=[p.model_dump() for p in proposals],
            model=review.model,
            prompt_version=PROMPT_VERSION,
            email_status=result.status,
            provider_id=result.provider_id,
            error=result.error,
            cost_usd=review.cost_usd,
        )
        conn.commit()

    logger.info(
        "weekly review %d for %s..%s: %s, %d proposal(s), $%s",
        review_id,
        start,
        as_of,
        subject,
        len(proposals),
        review.cost_usd,
    )
    return review_id


def _subject(as_of: date, accounts: dict[str, dict], proposals: list[ProposedChange]) -> str:
    """The subject line, from each account's stored total and the proposal count.

    The model writes none of it. An account with no valuation says so rather
    than carrying a figure invented to fill the slot.
    """
    count = f"{len(proposals)} proposal{'' if len(proposals) == 1 else 's'}"
    parts = []
    for name in ACCOUNTS:
        metrics = accounts[name]["metrics"]
        valuation = metrics["valuation"]
        if valuation is None:
            parts.append(f"{name} no valuation")
            continue
        change, change_pct = _week_change(metrics)
        if change is None:
            parts.append(f"{name} ${valuation['total_value_usd']}")
        else:
            parts.append(
                f"{name} ${valuation['total_value_usd']} "
                f"({'+' if change >= 0 else ''}{change_pct}%)"
            )
    subject = f"MarketAgent week to {as_of}: " + ", ".join(parts) + f", {count}"

    # Appended last so it survives every branch above. A week that lost a day's
    # valuation still produces a perfectly ordinary-looking subject otherwise,
    # which is the one case where ordinary-looking is wrong — the same reason
    # the daily email carries `no agent run`.
    failed = sum(
        len([c for c in (accounts[n]["metrics"].get("integrity") or []) if not c["ok"]])
        for n in ACCOUNTS
    )
    if failed:
        subject += f" — {failed} data check{'' if failed == 1 else 's'} failed"
    return subject


def _week_change(metrics: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    """The week's movement, or (None, None) where it cannot be computed.

    Measured against the last valuation *before* the window rather than the
    first one inside it, so a week is the seven days it says it is. Absent in
    the experiment's first week, which is reported as absent rather than as a
    change of zero.
    """
    valuation = metrics["valuation"]
    opening = metrics["opening_total_usd"]
    if valuation is None or opening is None or not opening:
        return None, None

    change = money(valuation["total_value_usd"] - opening)
    return change, money(change / opening * 100)


def _signed(value: Decimal) -> str:
    return f"{'+' if value >= 0 else ''}{value}"


def _comparison_section(accounts: dict[str, dict]) -> list[str]:
    """Which account is ahead this week, and which limit is shaping each one."""
    totals = {
        n: (accounts[n]["metrics"]["valuation"] or {}).get("total_value_usd") for n in ACCOUNTS
    }
    changes = {n: _week_change(accounts[n]["metrics"])[1] for n in ACCOUNTS}

    def top_constraint(name: str) -> str:
        rows = accounts[name]["metrics"]["constraints"]
        if not rows:
            return "no decisions"
        top = max(rows, key=lambda r: r["decisions"])
        return f"{top['binding'] or '—'} ({top['decisions']})"

    def idle_names(name: str) -> str:
        rows = accounts[name]["metrics"]["tickers"]
        if not rows:
            return "n/a"
        idle = sum(1 for r in rows if r["decisions"] == 0)
        return f"{idle} of {len(rows)}"

    lines = [
        "## static-100 vs dynamic-500",
        "",
        "| | " + " | ".join(ACCOUNTS) + " |",
        "| --- | " + " | ".join(["---"] * len(ACCOUNTS)) + " |",
        "| Total value | "
        + " | ".join(f"${totals[n]}" if totals[n] is not None else "not recorded" for n in ACCOUNTS)
        + " |",
        "| Change this week | "
        + " | ".join(
            f"{_signed(changes[n])}%" if changes[n] is not None else "not computable"
            for n in ACCOUNTS
        )
        + " |",
        "| Most frequent binding constraint | "
        + " | ".join(top_constraint(n) for n in ACCOUNTS)
        + " |",
        "| Watchlist names with no decisions | "
        + " | ".join(idle_names(n) for n in ACCOUNTS)
        + " |",
        "",
    ]
    if all(t is not None for t in totals.values()):
        leader = max(ACCOUNTS, key=lambda n: totals[n])
        trailer = next(n for n in ACCOUNTS if n != leader)
        gap = totals[leader] - totals[trailer]
        lines.append(
            f"**{leader}** is ahead of **{trailer}** by ${gap} this week."
            if gap
            else "The two accounts are exactly level this week."
        )
        lines.append("")
    return lines


def _account_section(name: str, data: dict) -> list[str]:
    """One account's week, as a labelled subsection under its own heading."""
    metrics = data["metrics"]
    initial, inception, lim = data["initial"], data["inception"], data["lim"]
    valuation = metrics["valuation"]
    change, change_pct = _week_change(metrics)

    lines = [
        f"## {name}",
        "",
        f"Began {inception} with ${initial}.",
        "",
        "### Where the money is",
        "",
        "| | |",
        "| --- | --- |",
    ]
    if valuation is None:
        # No summary ran in the window, so there is no valuation to report. Said
        # out loud, because a missing total is itself the week's finding.
        lines.append("| Total value | not recorded this week |")
    else:
        lines += [
            f"| Total value | ${valuation['total_value_usd']} (as at {valuation['as_of']}) |",
            f"| Cash | ${valuation['cash_usd']} |",
            f"| Positions | ${valuation['positions_value_usd']} |",
            f"| Since inception | ${_signed(valuation['pnl_usd'])} "
            f"({_signed(valuation['pnl_pct'])}%) |",
        ]
    if change is None:
        lines.append("| Change this week | no valuation before this week to compare against |")
    else:
        lines.append(f"| Change this week | ${_signed(change)} ({_signed(change_pct)}%) |")

    lines += _benchmark_section(metrics, change_pct)
    lines += _activity_section(metrics["runs"], metrics)
    lines += _risk_section(metrics, lim)
    lines += _trades_section(metrics)
    lines += _watchlist_section(metrics)
    lines += _reporting_section(metrics)
    # Last, and deliberately after every figure above: these say whether the
    # figures above can be believed, which only means something once they have
    # been stated.
    lines += _integrity_section(metrics)
    return lines


def _facts_table(start: date, end: date, accounts: dict[str, dict]) -> str:
    """The week, rendered deterministically. The model never touches these."""
    lines = [
        f"# MarketAgent — week to {end}",
        "",
        f"Covering {start} to {end} inclusive.",
        "",
    ]
    lines += _comparison_section(accounts)
    for name in ACCOUNTS:
        lines += _account_section(name, accounts[name])
    return "\n".join(lines)


def _integrity_section(metrics: dict[str, Any]) -> list[str]:
    """Whether the week's record is intact, stated as pass or fail per check.

    Every check is listed even when it passes. A section that appears only on
    a bad week is one nobody learns to read, and its absence is then
    indistinguishable from the job having skipped it.
    """
    checks = metrics.get("integrity") or []
    if not checks:
        return []

    failed = [c for c in checks if not c["ok"]]
    lines = [
        "",
        "### Data integrity",
        "",
    ]
    if failed:
        # Stated before the table, in the same spirit as the run section in the
        # daily email: the reader should not have to scan a list of ticks to
        # discover that one of them is a cross.
        lines += [
            f"**{len(failed)} of {len(checks)} checks failed.** The figures above "
            "are reported as stored; where a check below failed, what is stored "
            "may not be what happened.",
            "",
        ]
    lines += ["| Check | Result |", "| --- | --- |"]
    for check in checks:
        # A word, not a colour or a symbol alone — this is read in an email
        # client whose rendering we do not control.
        state = "OK" if check["ok"] else "FAILED"
        lines.append(f"| {check['check']} | **{state}** — {check['detail']} |")
    lines.append("")
    return lines


def _benchmark_section(metrics: dict[str, Any], change_pct: Decimal | None) -> list[str]:
    """The alternatives, and the account's own week beside them.

    A benchmark absent from `metrics` had no point in the window and is simply
    not listed. One with no earlier point shows no weekly change rather than a
    change from nothing.
    """
    if not metrics["benchmarks"]:
        return ["", "### Against the alternatives", "", "No benchmark data for this week.", ""]

    lines = [
        "",
        "### Against the alternatives",
        "",
        "| Benchmark | Value now | Change this week |",
        "| --- | --- | --- |",
    ]
    for row in metrics["benchmarks"]:
        label = "Savings at 5%" if row["symbol"] == CASH_SYMBOL else f"{row['symbol']} (proxy)"
        value = f"${row['value_usd']}" if row["value_usd"] is not None else "—"
        if row["value_usd"] is not None and row["opening_usd"]:
            delta = money(row["value_usd"] - row["opening_usd"])
            pct = money(delta / row["opening_usd"] * 100)
            movement = f"${_signed(delta)} ({_signed(pct)}%)"
        else:
            movement = "no earlier value to compare"
        lines.append(f"| {label} | {value} | {movement} |")

    ours = f"{_signed(change_pct)}%" if change_pct is not None else "not computable"
    lines += ["", f"This account's own change over the same week was {ours}.", ""]
    return lines


def _activity_section(runs: dict[str, Any], metrics: dict[str, Any]) -> list[str]:
    """What the agent did, stated before anything the model might read as idleness.

    The daily summary learned this the hard way: given a table that implied
    nothing had happened, the model correctly reported that nothing had.
    """
    duration = runs.get("avg_seconds")
    lines = [
        "",
        "### What the agent did",
        "",
        "| | |",
        "| --- | --- |",
        f"| Runs | {runs.get('runs', 0)} "
        f"({runs.get('succeeded', 0)} succeeded, {runs.get('failed', 0)} failed, "
        f"{runs.get('unfinished', 0)} never finished) |",
        f"| Runs in dry run | {runs.get('dry_runs', 0)} — a dry run persists the whole "
        f"decision path but sends no order to the broker |",
        f"| Articles fetched | {runs.get('news_fetched', 0)} |",
        f"| Articles judged relevant | {runs.get('news_relevant', 0)} |",
        f"| Decisions | {runs.get('decisions_made', 0)} |",
        f"| Trades | {runs.get('trades_executed', 0)} |",
        f"| LLM cost | ${runs.get('cost_usd', 0)} "
        f"({runs.get('input_tokens', 0)} input, {runs.get('output_tokens', 0)} output tokens) |",
        f"| Average run | {'—' if duration is None else f'{duration:.0f}s'} against its "
        f"job timeout |",
    ]

    if metrics["decisions"]:
        lines += [
            "",
            "| Action | Decisions | Average confidence | Approved by the risk engine |",
            "| --- | --- | --- | --- |",
        ]
        for row in metrics["decisions"]:
            confidence = row["avg_confidence"]
            lines.append(
                f"| {row['action']} | {row['decisions']} | "
                f"{'—' if confidence is None else f'{confidence:.2f}'} | {row['approved']} |"
            )
    lines.append("")
    return lines


def _risk_section(metrics: dict[str, Any], lim: RiskLimits) -> list[str]:
    """The limits in force, then what each of them actually did.

    Both halves are needed together: a constraint that binds 31 times means
    nothing without the value it was binding against, and the value means
    nothing without knowing whether it ever bound.
    """
    lines = [
        "",
        "### The risk engine",
        "",
        "These are the limits in force this week for this account. They are "
        "configuration, not something the model can change.",
        "",
        # The setting name is in the table because a proposal has to name the
        # knob to be actionable, and the model has no other way to learn what
        # these are called. `RISK_` is `RiskSettings`' env_prefix.
        "| Limit | Value | Setting |",
        "| --- | --- | --- |",
        f"| Largest single trade | ${lim.max_trade_usd} | `RISK_MAX_TRADE_USD` |",
        f"| Smallest single trade | ${lim.min_trade_usd} | `RISK_MIN_TRADE_USD` |",
        f"| Largest position | ${lim.max_position_usd} | `RISK_MAX_POSITION_USD` |",
        f"| Concentration ceiling | {lim.max_concentration_pct}% of the portfolio "
        f"| `RISK_MAX_CONCENTRATION_PCT` |",
        f"| Total exposure ceiling | {lim.max_total_exposure_pct}% of the portfolio "
        f"| `RISK_MAX_TOTAL_EXPOSURE_PCT` |",
        f"| Trades per day | {lim.max_daily_trades} | `RISK_MAX_DAILY_TRADES` |",
        f"| Confidence floor | {lim.min_confidence} | `RISK_MIN_CONFIDENCE` |",
        f"| Tradeable names | {len(lim.allowed_tickers)} | this account's watchlist |",
        "",
    ]

    if not metrics["constraints"]:
        return [*lines, "No decisions were taken this week, so no constraint bound.", ""]

    refused = [r for r in metrics["constraints"] if not r["approved"]]
    approved = [r for r in metrics["constraints"] if r["approved"]]

    lines += [
        "`binding_constraint` is the single rule that decided each verdict — the gate "
        "that refused it, or the tightest cap that clamped it.",
        "",
        "| Refused by | Decisions |",
        "| --- | --- |",
    ]
    if refused:
        lines += [f"| {r['binding'] or '—'} | {r['decisions']} |" for r in refused]
    else:
        lines.append("| nothing was refused | 0 |")

    lines += ["", "| Approved, bound by | Decisions |", "| --- | --- |"]
    if approved:
        lines += [f"| {r['binding'] or '—'} | {r['decisions']} |" for r in approved]
    else:
        lines.append("| nothing was approved | 0 |")
    # `recommended_amount` means the engine did not reduce the trade at all, so
    # it is the one entry in the approved table that is not a limit biting.
    lines += [
        "",
        "`recommended_amount` in the approved table means no limit reduced the trade: "
        "the model's own suggested size was the binding figure.",
        "",
    ]
    return lines


def _trades_section(metrics: dict[str, Any]) -> list[str]:
    if not metrics["trades"]:
        return ["", "### Trades", "", "No trades were made this week.", ""]

    lines = [
        "",
        "### Trades",
        "",
        "| Status | Trades | Of which simulated | Total notional |",
        "| --- | --- | --- | --- |",
    ]
    for row in metrics["trades"]:
        lines.append(
            f"| {row['status']} | {row['trades']} | {row['simulated']} | ${row['notional_usd']} |"
        )
    lines += [
        "",
        "`submitted` is the normal outcome of a scheduled run: the agent runs at 06:00 "
        "UTC and the US market opens at 14:30, so an order rests for hours. `simulated` "
        "means a dry run — the portfolio moved but no order reached the broker.",
        "",
    ]
    return lines


def _watchlist_section(metrics: dict[str, Any]) -> list[str]:
    """Per name, including the names nothing happened to.

    Driven by the watchlist rather than by the decisions, so a ticker the agent
    never reached appears as a row of zeros — which is the row worth reading.
    """
    if not metrics["tickers"]:
        return []

    lines = [
        "",
        "### By watchlist name",
        "",
        "| Ticker | Relevant articles | Decisions | BUY or SELL | Trades |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in metrics["tickers"]:
        lines.append(
            f"| {row['ticker']} | {row['relevant_news']} | {row['decisions']} | "
            f"{row['convictions']} | {row['trades']} |"
        )
    lines.append("")
    return lines


def _reporting_section(metrics: dict[str, Any]) -> list[str]:
    """Whether the daily job reported at all. A silent week is a finding."""
    summaries = metrics["summaries"]
    return [
        "",
        "### Daily reporting",
        "",
        f"{summaries.get('days', 0)} of {REVIEW_DAYS} days have a stored summary: "
        f"{summaries.get('sent', 0)} emailed, {summaries.get('failed', 0)} failed to send, "
        f"{summaries.get('skipped', 0)} with no recipient configured.",
        "",
    ]


def _proposals_table(proposals: list[ProposedChange]) -> str:
    """The model's proposals, rendered by us rather than written as prose.

    Structured on the way out for the same reason they are structured on the
    way in: the same list goes into `weekly_reviews.recommendations`, so the
    email and the stored row cannot disagree, and a proposal that reappears
    week after week stays countable.
    """
    if not proposals:
        return (
            "## Proposed changes\n\n"
            "None. The week's figures did not support a change.\n\n"
            "*Proposals are advisory: acting on one means editing the code.*"
        )

    lines = ["## Proposed changes", ""]
    for index, p in enumerate(proposals, start=1):
        lines += [
            f"### {index}. {p.change}",
            "",
            f"**Area:** {p.area} · **Confidence:** {p.confidence:.2f}",
            "",
            f"**Why:** {p.rationale}",
            "",
            f"**Expected effect:** {p.expected_effect}",
            "",
        ]
    lines.append("*Proposals are advisory: acting on one means editing the code.*")
    return "\n".join(lines)


def _prompt(facts: str, previous: list[dict[str, Any]]) -> str:
    """The facts, plus what was proposed last week.

    Last week's proposals are included so the review compounds rather than
    restarting every Sunday — the limits table above says what the settings are
    *now*, so a proposal that was acted on is visible by comparison. The model
    is told it cannot tell from the list alone, because a proposal nobody read
    and a proposal that was rejected look identical here.
    """
    if previous:
        history = "\n".join(f"- ({p.get('area')}) {p.get('change')}" for p in previous)
        context = (
            "## Proposed at the end of the previous review\n\n"
            f"{history}\n\n"
            "This list does not say whether any of them were acted on — a proposal that "
            "was rejected and one that was never read look the same here. The limits "
            "table above is the current state, so compare against that. Do not simply "
            "repeat a proposal; if it still stands, say that it does and why the "
            "evidence is now stronger.\n\n"
        )
    else:
        context = ""

    return (
        f"{facts}\n\n"
        f"{context}"
        "Write the assessment that goes under the tables above, and propose the changes "
        "the week's evidence supports. Address both accounts explicitly — which is ahead "
        "this week, and whether the evidence points to the universe (static-100 vs "
        "dynamic-500) or to the risk limits as the more likely explanation — rather than "
        "assessing them as one merged experiment."
    )
