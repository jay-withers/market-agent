"""The daily summary job: reconcile, value, compare, write, send.

Runs at 21:00 UTC, and its first job is the one the agent could not do. The
agent submits at 06:00 and the US market opens at 14:30, so a scheduled run's
orders are still resting when it finishes — **this** is where a fill becomes
known, cash and positions move, and the day gets a valuation.

One rule shapes the rest: **every figure in the email comes from the database.**
The model writes the commentary and is shown the numbers as a table it is told
not to restate. Nothing it writes can become a reported balance.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal

import markdown as markdown_lib

from .. import repository as repo
from ..benchmarks import CASH_SYMBOL, fetch_benchmark_bars
from ..benchmarks import build as build_benchmarks
from ..broker.alpaca import AlpacaBroker
from ..broker.base import Broker
from ..db import pool
from ..fetch import FetchError
from ..fx import FxRate, fetch_gbp_usd
from ..llm.anthropic_provider import AnthropicLlm
from ..llm.base import PROMPT_VERSION, Llm
from ..mailer import MailResult, send
from ..marketdata import fetch_daily_bars, latest_close
from ..models import money
from ..settings import optional_secret, settings

logger = logging.getLogger(__name__)

PRICE_HISTORY_DAYS = 7

# Cost columns are NUMERIC(18,6), because a single filter call costs a fraction
# of a cent. That scale is right for the ledger and wrong for an email, where
# "$0.190000" reads as a machine talking to itself.
CENTS = Decimal("0.01")


def run(
    as_of: date | None = None,
    llm: Llm | None = None,
    broker: Broker | None = None,
) -> int:
    """Produce and send one day's summary. Returns the `daily_summaries` id."""
    cfg = settings()
    as_of = as_of or datetime.now(UTC).date()
    llm = llm or AnthropicLlm()
    # The real broker even in a dry run: reconciliation only ever *reads*
    # orders, and a dry run has no submitted trades to reconcile anyway.
    broker = broker or AlpacaBroker()

    with pool().connection() as conn:
        pid = repo.portfolio_id(conn)
        inception = repo.portfolio_inception(conn, pid)
        initial = repo.initial_cash(conn, pid)

    filled = _reconcile(pid, broker)

    rate = _fx_rate(pid)
    benchmark_symbols = [s.strip() for s in cfg.benchmark_symbols.split(",") if s.strip()]

    with pool().connection() as conn:
        holdings = [t for t, *_ in repo.load_positions(conn, pid)]

    bars = fetch_daily_bars(holdings, days=PRICE_HISTORY_DAYS) if holdings else []
    benchmark_bars = fetch_benchmark_bars(benchmark_symbols, days=PRICE_HISTORY_DAYS)

    with pool().connection() as conn:
        repo.save_prices(conn, bars + benchmark_bars)
        conn.commit()

    with pool().connection() as conn:
        state, unpriced = repo.build_state(conn, pid, latest_close(bars), rate.gbp_usd)
        if unpriced:
            logger.warning("valuing without a current price for: %s", ", ".join(unpriced))

        total = state.total_value_gbp
        pnl = money(total - initial)
        pnl_pct = money(pnl / initial * 100) if initial else Decimal(0)

        repo.save_daily_performance(
            conn,
            pid,
            as_of,
            cash_gbp=state.cash_gbp,
            positions_value_gbp=state.invested_gbp,
            total_value_gbp=total,
            pnl_gbp=pnl,
            pnl_pct=pnl_pct,
            fx_rate=rate.gbp_usd,
            fx_rate_as_of=rate.as_of,
        )

        # Benchmarks are indexed from the first close at or after inception, so
        # the comparison starts from the same day and the same notional as the
        # portfolio does.
        inception_closes = {
            symbol: close
            for symbol in benchmark_symbols
            if symbol != CASH_SYMBOL
            and (close := repo.close_on(conn, symbol, inception)) is not None
        }
        points = build_benchmarks(
            benchmark_symbols,
            benchmark_bars,
            inception_closes,
            notional_gbp=initial,
            apr_pct=cfg.cash_benchmark_apr_pct,
            days_held=(as_of - inception).days,
            as_of=as_of,
        )
        repo.save_benchmarks(conn, points)

        activity = repo.day_activity(conn, pid, as_of)
        spend = repo.spend(conn, as_of)
        conn.commit()

    facts = _facts_table(
        as_of, state, initial, pnl, pnl_pct, points, filled, activity, spend, _credit_usd()
    )
    subject = f"InvestAgent {as_of}: £{total} ({'+' if pnl >= 0 else ''}{pnl_pct}%)"
    # Built from stored figures, never by the model — and that is exactly why
    # it has to carry this: a day the agent died still values the portfolio and
    # would otherwise be indistinguishable in an inbox from a day it worked.
    if alert := _run_alert(activity["runs"]):
        subject += f" — {alert}"

    narrative = llm.narrate(_prompt(facts, activity))
    body_markdown = f"{facts}\n\n{narrative.value.body_markdown}"
    body_html = markdown_lib.markdown(body_markdown, extensions=["tables"])

    result: MailResult = send(subject, body_html, body_markdown)
    logger.info("summary email %s", result.status)

    with pool().connection() as conn:
        summary_id = repo.save_summary(
            conn,
            as_of,
            subject,
            body_markdown,
            body_html,
            model=narrative.model,
            prompt_version=PROMPT_VERSION,
            email_status=result.status,
            provider_id=result.provider_id,
            error=result.error,
            cost_usd=narrative.cost_usd,
        )
        conn.commit()

    logger.info("summary %d for %s: %s, %d fill(s) reconciled", summary_id, as_of, subject, filled)
    return summary_id


def _fx_rate(pid: int) -> FxRate:
    """Today's published rate, or the last one we stored if that is unreachable.

    A Cloudflare 522 from Frankfurter cost the 2026-09-14 summary entirely — no
    valuation, no benchmark arms, no row. On a series read over months a hole
    distorts more than a rate a day or two old does, and the stored figure stays
    honest either way: `fx_rate_as_of` records the day the rate is *for*, so a
    fallback row is indistinguishable in shape from the weekend runs that
    already carry Friday's rate because the ECB does not publish at weekends.

    Deliberately not done in the agent job, which converts an approved GBP
    amount into the USD notional it actually submits. Mis-sizing a real order
    against an unknown-age rate works against the risk engine; skipping a
    morning does not.
    """
    try:
        return fetch_gbp_usd()
    except FetchError as exc:
        with pool().connection() as conn:
            previous = repo.last_fx_rate(conn, pid)
        if previous is None:
            # Nothing to fall back to, so the original failure is the honest
            # thing to report.
            raise
        rate, as_of = previous
        logger.warning("fx lookup failed (%s); valuing at the stored rate from %s", exc, as_of)
        return FxRate(gbp_usd=rate, as_of=as_of, source="frankfurter (stored)")


def _reconcile(pid: int, broker: Broker) -> int:
    """Ask the broker what became of yesterday's submissions.

    Cash and positions move here, not at submission — a resting order has
    changed nothing. Each trade is committed on its own: one order the broker
    cannot answer for must not roll back the fills already applied.
    """
    with pool().connection() as conn:
        pending = repo.unreconciled_trades(conn, pid)

    if not pending:
        return 0

    filled = 0
    for trade in pending:
        outcome = getattr(broker, "order", lambda _id: None)(trade["client_order_id"])
        if outcome is None:
            logger.warning("no broker record for %s", trade["client_order_id"])
            continue

        with pool().connection() as conn:
            repo.update_trade_outcome(
                conn,
                trade["id"],
                outcome.status,
                outcome.quantity,
                outcome.filled_avg_price_usd,
                outcome.filled_at,
            )
            if outcome.status == "filled" and outcome.quantity and outcome.filled_avg_price_usd:
                repo.apply_fill(
                    conn,
                    pid=pid,
                    ticker=trade["ticker"],
                    side=trade["side"],
                    quantity=outcome.quantity,
                    notional_gbp=trade["notional_gbp"],
                    price_usd=outcome.filled_avg_price_usd,
                    price_gbp=money(trade["notional_gbp"] / outcome.quantity),
                )
                filled += 1
            conn.commit()

    return filled


def _credit_usd() -> Decimal | None:
    """The API credit the experiment started with, if anyone has said.

    There is **no Anthropic endpoint that reports a remaining balance** — the
    Usage and Cost Admin API reports spend, needs an Admin API key, and is not
    available to individual accounts at all. So a runway figure can only come
    from a starting number a human supplies plus the spend this database has
    recorded itself.

    In Key Vault rather than `common_env`, for the reason the email recipient
    is: this repository and `terraform/environments/*.tfvars` are public, and
    what someone has put on their account is theirs. Absent means report the
    spend and no runway — opt-in, like the email.

    A value that is not a number is ignored rather than fatal: a mistyped
    credit figure must not cost the day its summary.
    """
    raw = optional_secret("ANTHROPIC-CREDIT-USD")
    if not raw:
        return None
    try:
        return Decimal(raw.strip().lstrip("$"))
    except (ArithmeticError, ValueError):
        logger.warning("ANTHROPIC-CREDIT-USD is not a number, ignoring it")
        return None


def _usd(value: Decimal) -> str:
    """One dollar figure, for a human to read.

    Display only — every calculation below works on the unrounded values, so
    rounding here can never move the runway or the remaining balance.
    """
    return f"${value.quantize(CENTS)}"


def _spend_section(spend: dict, credit: Decimal | None) -> list[str]:
    """What the experiment has spent with the model, and what that leaves.

    The scope is stated in the table rather than left to be inferred: these
    figures cannot include the call that writes this email, because that call
    has not happened when the table is built. Understating today's spend by one
    Sonnet call is fine; letting the model describe the figure as complete is
    not.

    A daily average over the last seven days rather than over all time, because
    the question behind it is "how long does this last at the rate it is going
    now" and an average that includes the first week of manual runs answers a
    different one.
    """
    to_date = spend["to_date_usd"]
    daily = money(spend["last_7_days_usd"] / 7)

    lines = [
        "",
        "## Model spend",
        "",
        "| | |",
        "| --- | --- |",
        f"| Spent today | {_usd(spend['today_usd'])} |",
        f"| Last 7 days | {_usd(spend['last_7_days_usd'])} ({_usd(daily)}/day) |",
        f"| Spent in total | {_usd(to_date)} |",
    ]

    if credit is not None:
        remaining = credit - to_date
        lines.append(f"| Credit remaining | {_usd(remaining)} of {_usd(credit)} |")
        if remaining <= 0:
            lines.append("| Runway | none — the recorded spend has reached the credit |")
        elif daily > 0:
            # Whole days, rounded towards zero by the int() — the same
            # direction money() rounds, and the safe one for a runway.
            lines.append(f"| Runway | about {int(remaining / daily)} days at that rate |")
        else:
            lines.append("| Runway | not estimable — nothing was spent in the last 7 days |")

    known_from = spend["known_from"]
    scope = (
        f"Spend is what this database recorded, from {known_from} onwards"
        if known_from
        else "No spend has been recorded yet"
    )
    lines += [
        "",
        f"{scope}. It excludes the call that writes this email, which has not been "
        "made when these figures are read, and anything else on the same API key. "
        + (
            "The credit figure is a number configured by hand: Anthropic publishes no "
            "balance endpoint, so nothing here has checked it against the account."
            if credit is not None
            else "No starting credit is configured, so there is no runway to report."
        ),
        "",
    ]
    return lines


def _facts_table(
    as_of, state, initial, pnl, pnl_pct, points, filled, activity, spend=None, credit=None
) -> str:
    """The figures, rendered deterministically. The model never touches these."""
    lines = [
        f"# InvestAgent — {as_of}",
        "",
        "| | |",
        "| --- | --- |",
        f"| Total value | £{state.total_value_gbp} |",
        f"| Cash | £{state.cash_gbp} |",
        f"| Positions | £{state.invested_gbp} |",
        f"| P&L | £{pnl} ({'+' if pnl >= 0 else ''}{pnl_pct}%) |",
        f"| Started with | £{initial} |",
        "",
        "## Against the alternatives",
        "",
        "| Benchmark | Value of £" + str(initial) + " |",
        "| --- | --- |",
    ]
    for point in sorted(points, key=lambda p: p.symbol):
        label = "Savings at 5%" if point.symbol == CASH_SYMBOL else f"{point.symbol} (proxy)"
        lines.append(f"| {label} | £{point.value_gbp} |")

    lines += _run_section(activity["runs"])

    # Today's trades, stated before anything else the model might read as
    # "nothing happened". An earlier version showed only a reconciliation
    # count, which is zero on a dry run because a simulated trade never reaches
    # a broker — and the model correctly reported what it was told: that cash
    # and positions were unchanged, on a day three trades had executed.
    trades = activity["trades"]
    if trades:
        lines += [
            "## Trades today",
            "",
            "| Ticker | Side | Status | Amount | Quantity |",
            "| --- | --- | --- | --- | --- |",
        ]
        for ticker, side, status, notional, quantity, _price in trades:
            shares = quantity if quantity is not None else "not yet known"
            lines.append(f"| {ticker} | {side} | {status} | £{notional} | {shares} |")
        lines += [
            "",
            f"`simulated` means a dry run: the decision path ran in full and the "
            f"portfolio moved, but no order was sent to the broker. "
            f"{filled} order(s) were reconciled with the broker today.",
            "",
        ]
    else:
        lines += ["## Trades today", "", "No trades were made.", ""]

    if activity["holdings"]:
        lines += ["", "## Holdings", "", "| Ticker | Quantity | Avg cost |", "| --- | --- | --- |"]
        for ticker, quantity, avg_gbp, _close in activity["holdings"]:
            lines.append(f"| {ticker} | {quantity} | £{avg_gbp} |")

    if activity["decisions"]:
        lines += [
            "",
            "## Decisions",
            "",
            "| Ticker | Action | Confidence | Approved | Bound by |",
            "| --- | --- | --- | --- | --- |",
        ]
        for ticker, action, confidence, approved, _reasoning, binding in activity["decisions"]:
            amount = f"£{approved}" if approved is not None else "—"
            lines.append(f"| {ticker} | {action} | {confidence} | {amount} | {binding} |")

    if spend is not None:
        lines += _spend_section(spend, credit)

    return "\n".join(lines)


def _run_alert(runs) -> str | None:
    """A few words for the subject line, or None when the day ran cleanly.

    The subject is the only part of this email that survives being read on a
    phone's lock screen, and it is built from stored figures precisely so the
    model cannot influence it. A day the agent never ran still has a valuation
    and still produces a perfectly ordinary-looking subject, which is the one
    case where ordinary-looking is wrong.
    """
    if not runs:
        return "no agent run"

    statuses = [status for _started, _finished, status, *_rest in runs]
    stale = [row[-1] for row in runs]

    # Worst outcome of the day, not the last one: a manual retry that succeeded
    # does not erase the scheduled run that did not.
    if "failed" in statuses:
        return "agent run failed"
    if any(stale):
        return "agent run abandoned"
    return None


def _run_section(runs) -> list[str]:
    """Whether the agent ran, stated before anything it did or did not do.

    `No trades were made.` is the same sentence on a day the model held every
    position and on a day the 06:00 job died before reaching the first
    analysis. The trades, decisions and holdings sections below are all silent
    in exactly the same way, so nothing further down can distinguish them
    either — this section is the only thing that can.
    """
    if not runs:
        return [
            "## Agent run",
            "",
            "**No agent run is recorded for today.** The job either did not "
            "start or died before it could open a row. Nothing below describes "
            "a decision the agent declined to take — it describes an agent "
            "that did not run.",
            "",
        ]

    lines = [
        "## Agent run",
        "",
        "| Started (UTC) | Finished | Status | Trigger |",
        "| --- | --- | --- | --- |",
    ]
    for started, finished, status, trigger, dry_run, _error, stale in runs:
        # `stale` is a run still marked `running` past the job timeout: SIGKILL
        # cannot be caught, so the row was never closed and the replica is long
        # gone. Reported as abandoned rather than in progress.
        state = "abandoned" if stale else status
        if dry_run:
            state += " (dry run)"
        lines.append(f"| {_clock(started)} | {_clock(finished)} | {state} | {trigger} |")

    errors = [error for *_head, error, _stale in runs if error]
    if errors:
        lines += ["", "The run reported: " + "; ".join(errors), ""]
    else:
        lines.append("")

    return lines


def _clock(value) -> str:
    """A timestamp as HH:MM UTC, or an em dash for a run that never finished."""
    return value.strftime("%H:%M") if value is not None else "—"


def _prompt(facts: str, activity) -> str:
    reasoning = "\n".join(
        f"- {ticker} ({action}): {text}"
        for ticker, action, _confidence, _approved, text, _binding in activity["decisions"]
        if text
    )
    return (
        f"{facts}\n\n"
        f"## The AI's own reasoning today\n\n{reasoning or 'No decisions were taken.'}\n\n"
        "Write the commentary that goes under the tables above."
    )
