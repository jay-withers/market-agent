"""The daily summary job: reconcile, value, compare, write, send.

Runs at 21:00 UTC, and its first job is the one the agent could not do. The
agent submits at 06:00 and the US market opens at 14:30, so a scheduled run's
orders are still resting when it finishes — **this** is where a fill becomes
known, cash and positions move, and the day gets a valuation.

One rule shapes the rest: **every figure in the email comes from the database.**
The model writes the commentary and is shown the numbers as a table it is told
not to restate. Nothing it writes can become a reported balance.

Combined across both accounts rather than one email each: the whole point of
running 'static-100' and 'dynamic-500' side by side is to compare them, and
that comparison is easiest to make in one email that shows both, not two
emails a reader has to hold in their head at once.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal

import markdown as markdown_lib

from .. import alpaca_api
from .. import repository as repo
from ..benchmarks import CASH_SYMBOL, fetch_benchmark_bars
from ..benchmarks import build as build_benchmarks
from ..broker.alpaca import AlpacaBroker
from ..broker.base import Broker
from ..db import pool
from ..fetch import FetchError
from ..llm.base import PROMPT_VERSION, Llm
from ..llm.deepseek_provider import DeepseekLlm, balance_usd
from ..mailer import MailResult, send
from ..marketdata import fetch_daily_bars, latest_close
from ..models import money
from ..settings import settings
from ..sync import synchronize

logger = logging.getLogger(__name__)

PRICE_HISTORY_DAYS = 7

ACCOUNTS = ("static-100", "dynamic-500")

# Cost columns are NUMERIC(18,6), because a single filter call costs a fraction
# of a cent. That scale is right for the ledger and wrong for an email, where
# "$0.190000" reads as a machine talking to itself.
CENTS = Decimal("0.01")


def run(
    as_of: date | None = None,
    llm: Llm | None = None,
    broker: Broker | None = None,
) -> int:
    """Produce and send one day's combined summary. Returns the `daily_summaries` id."""
    cfg = settings()
    as_of = as_of or datetime.now(UTC).date()
    llm = llm or DeepseekLlm()
    # The real broker even in a dry run: reconciliation only ever *reads*
    # orders, and a dry run has no submitted trades to reconcile anyway. One
    # instance serves both accounts — it is stateless with respect to which
    # account it talks to, reading `alpaca_api.headers()` fresh on every call,
    # so switching accounts is just calling `use_account` before each block.
    broker = broker or AlpacaBroker()

    benchmark_symbols = [s.strip() for s in cfg.benchmark_symbols.split(",") if s.strip()]
    accounts: dict[str, dict] = {}

    for name in ACCOUNTS:
        alpaca_api.use_account(name)
        with pool().connection() as conn:
            pid = repo.portfolio_id(conn, name=name)
        synchronize(pid, broker, pool())
        with pool().connection() as conn:
            inception = repo.portfolio_inception(conn, pid)
            initial = repo.initial_cash(conn, pid)

        filled = _reconcile(pid, broker)
        synchronize(pid, broker, pool())

        with pool().connection() as conn:
            holdings = [t for t, *_ in repo.load_positions(conn, pid)]

        bars = fetch_daily_bars(holdings, days=PRICE_HISTORY_DAYS) if holdings else []
        benchmark_bars = fetch_benchmark_bars(benchmark_symbols, days=PRICE_HISTORY_DAYS)

        with pool().connection() as conn:
            repo.save_prices(conn, bars + benchmark_bars)
            conn.commit()

        with pool().connection() as conn:
            state, unpriced = repo.build_state(conn, pid, latest_close(bars))
            if unpriced:
                logger.warning(
                    "%s: valuing without a current price for: %s", name, ", ".join(unpriced)
                )

            total = state.total_value_usd
            pnl = money(total - initial)
            pnl_pct = money(pnl / initial * 100) if initial else Decimal(0)

            repo.save_daily_performance(
                conn,
                pid,
                as_of,
                cash_usd=state.cash_usd,
                positions_value_usd=state.invested_usd,
                total_value_usd=total,
                pnl_usd=pnl,
                pnl_pct=pnl_pct,
            )

            # Benchmarks are indexed from the first close at or after this
            # account's own inception, so the comparison starts from the same
            # day and the same notional the account itself did.
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
                notional_usd=initial,
                apr_pct=cfg.cash_benchmark_apr_pct,
                days_held=(as_of - inception).days,
                as_of=as_of,
            )
            repo.save_benchmarks(conn, pid, points)

            activity = repo.day_activity(conn, pid, as_of)
            account_spend = repo.spend(conn, as_of, pid)
            conn.commit()

        accounts[name] = {
            "pid": pid,
            "state": state,
            "initial": initial,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "points": points,
            "filled": filled,
            "activity": activity,
            "spend": account_spend,
        }

    # The combined total, not either account's own: the two accounts share one
    # DeepSeek API key and one account balance, so runway is computed once.
    with pool().connection() as conn:
        total_spend = repo.spend(conn, as_of, pid=None)

    facts = _facts_table(as_of, accounts, total_spend, _credit_usd())
    totals = {n: accounts[n]["state"].total_value_usd for n in ACCOUNTS}
    subject = (
        f"MarketAgent {as_of}: static-100 ${totals['static-100']} "
        f"vs dynamic-500 ${totals['dynamic-500']}"
    )
    # Built from stored figures, never by the model — and that is exactly why
    # it has to carry this: a day an account's agent died still values that
    # account and would otherwise be indistinguishable in an inbox from a day
    # it worked.
    if alert := _run_alert(accounts):
        subject += f" — {alert}"

    narrative = llm.narrate(_prompt(facts, accounts))
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

    total_filled = sum(accounts[n]["filled"] for n in ACCOUNTS)
    logger.info(
        "summary %d for %s: %s, %d fill(s) reconciled across both accounts",
        summary_id,
        as_of,
        subject,
        total_filled,
    )
    return summary_id


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
                filled += 1
            conn.commit()

    return filled


def _credit_usd() -> Decimal | None:
    """The account's current DeepSeek balance, in USD — or None if it could not be read.

    Unlike the Anthropic credit figure this replaces — a number typed by hand
    into `ANTHROPIC-CREDIT-USD` and checked against nothing, because Anthropic
    publishes no balance endpoint at all — DeepSeek reports this directly (see
    `llm.deepseek_provider.balance_usd`), so the runway figure below is now a
    verified number rather than a guess.

    Never allowed to cost the day its summary, the same discipline
    `mailer.send` applies to email delivery: a failure here — network,
    an unexpected response shape, no USD balance on the account — is caught
    and logged rather than raised, so a balance-API hiccup degrades to "no
    runway reported" instead of losing the whole run. A broken
    `DEEPSEEK-API-KEY` is not caught here, deliberately: it would fail
    `llm.narrate()` moments later anyway, and letting it propagate from the
    first DeepSeek call in the run reports the real cause immediately rather
    than after wasted work.
    """
    try:
        return balance_usd()
    except (FetchError, ValueError) as exc:
        logger.warning("could not read the DeepSeek account balance: %s", exc)
        return None


def _usd(value: Decimal) -> str:
    """One dollar figure, for a human to read.

    Display only — every calculation below works on the unrounded values, so
    rounding here can never move the runway or the remaining balance.
    """
    return f"${value.quantize(CENTS)}"


def _spend_section(
    spend_by_account: dict[str, dict], total_spend: dict, credit: Decimal | None
) -> list[str]:
    """What has been spent with the model, and what that leaves.

    Per account (agent-run calls only) and combined — the combined column also
    includes the summary and weekly review calls, which are single calls that
    serve both accounts at once and so are not attributable to one of them.
    The combined figure is what the runway is computed from, since both
    accounts spend against the same DeepSeek API key and account balance.

    The scope is stated in the table rather than left to be inferred: these
    figures cannot include the call that writes this email, because that call
    has not happened when the table is built. Understating today's spend by
    one Sonnet call is fine; letting the model describe the figure as
    complete is not.

    A daily average over the last seven days rather than over all time, because
    the question behind it is "how long does this last at the rate it is going
    now" and an average that includes the first week of manual runs answers a
    different one.
    """
    daily = money(total_spend["last_7_days_usd"] / 7)

    lines = [
        "",
        "## Model spend",
        "",
        "| | " + " | ".join(ACCOUNTS) + " | Combined |",
        "| --- | " + " | ".join(["---"] * len(ACCOUNTS)) + " | --- |",
        "| Spent today | "
        + " | ".join(_usd(spend_by_account[n]["today_usd"]) for n in ACCOUNTS)
        + f" | {_usd(total_spend['today_usd'])} |",
        "| Last 7 days | "
        + " | ".join(_usd(spend_by_account[n]["last_7_days_usd"]) for n in ACCOUNTS)
        + f" | {_usd(total_spend['last_7_days_usd'])} ({_usd(daily)}/day) |",
        "| Spent in total | "
        + " | ".join(_usd(spend_by_account[n]["to_date_usd"]) for n in ACCOUNTS)
        + f" | {_usd(total_spend['to_date_usd'])} |",
    ]

    if credit is not None:
        remaining = credit - total_spend["to_date_usd"]
        lines.append(f"| Credit remaining | | | {_usd(remaining)} of {_usd(credit)} |")
        if remaining <= 0:
            lines.append("| Runway | | | none — the recorded spend has reached the credit |")
        elif daily > 0:
            # Whole days, rounded towards zero by the int() — the same
            # direction money() rounds, and the safe one for a runway.
            lines.append(f"| Runway | | | about {int(remaining / daily)} days at that rate |")
        else:
            lines.append("| Runway | | | not estimable — nothing was spent in the last 7 days |")

    known_from = total_spend["known_from"]
    scope = (
        f"Spend is what this database recorded, from {known_from} onwards"
        if known_from
        else "No spend has been recorded yet"
    )
    lines += [
        "",
        f"{scope}. It excludes the call that writes this email, which has not been "
        "made when these figures are read, and anything else on the same API key. "
        "Both accounts share one DeepSeek API key and one account balance, so the "
        "combined column, not either account's own, is what the runway is computed "
        "from. "
        + (
            "The credit figure is DeepSeek's own reported account balance, read live "
            "and checked against the account rather than typed by hand."
            if credit is not None
            else "The account balance could not be read this time, so there is no runway to report."
        ),
        "",
    ]
    return lines


def _comparison_section(accounts: dict[str, dict]) -> list[str]:
    """Which account is ahead, by how much — the reason this report exists."""
    totals = {n: accounts[n]["state"].total_value_usd for n in ACCOUNTS}
    pnls = {n: accounts[n]["pnl_pct"] for n in ACCOUNTS}
    leader = max(ACCOUNTS, key=lambda n: totals[n])
    trailer = next(n for n in ACCOUNTS if n != leader)
    gap = totals[leader] - totals[trailer]

    lines = [
        "## static-100 vs dynamic-500",
        "",
        "| | " + " | ".join(ACCOUNTS) + " |",
        "| --- | " + " | ".join(["---"] * len(ACCOUNTS)) + " |",
        "| Total value | " + " | ".join(f"${totals[n]}" for n in ACCOUNTS) + " |",
        "| P&L since inception | "
        + " | ".join(f"{'+' if pnls[n] >= 0 else ''}{pnls[n]}%" for n in ACCOUNTS)
        + " |",
        "",
    ]
    if gap == 0:
        lines.append("The two accounts are exactly level today.")
    else:
        lines.append(f"**{leader}** is ahead of **{trailer}** by ${gap} today.")
    lines.append("")
    return lines


def _account_section(name: str, data: dict) -> list[str]:
    """One account's figures, as a labelled subsection under its own heading."""
    state, initial, pnl, pnl_pct = data["state"], data["initial"], data["pnl"], data["pnl_pct"]
    points, filled, activity = data["points"], data["filled"], data["activity"]

    lines = [
        f"## {name}",
        "",
        "| | |",
        "| --- | --- |",
        f"| Total value | ${state.total_value_usd} |",
        f"| Cash | ${state.cash_usd} |",
        f"| Positions | ${state.invested_usd} |",
        f"| P&L | ${pnl} ({'+' if pnl >= 0 else ''}{pnl_pct}%) |",
        f"| Started with | ${initial} |",
        "",
        "### Against the alternatives",
        "",
        "| Benchmark | Value of $" + str(initial) + " |",
        "| --- | --- |",
    ]
    for point in sorted(points, key=lambda p: p.symbol):
        label = "Savings at 5%" if point.symbol == CASH_SYMBOL else f"{point.symbol} (proxy)"
        lines.append(f"| {label} | ${point.value_usd} |")

    lines += _run_section(activity["runs"])

    # Today's trades, stated before anything else the model might read as
    # "nothing happened". An earlier version showed only a reconciliation
    # count, which is zero on a dry run because a simulated trade never reaches
    # a broker — and the model correctly reported what it was told: that cash
    # and positions were unchanged, on a day three trades had executed.
    trades = activity["trades"]
    if trades:
        lines += [
            "### Trades today",
            "",
            "| Ticker | Side | Status | Amount | Quantity |",
            "| --- | --- | --- | --- | --- |",
        ]
        for ticker, side, status, notional, quantity, _price in trades:
            shares = quantity if quantity is not None else "not yet known"
            lines.append(f"| {ticker} | {side} | {status} | ${notional} | {shares} |")
        lines += [
            "",
            f"`simulated` means a dry run: the decision path ran in full and the "
            f"portfolio moved, but no order was sent to the broker. "
            f"{filled} order(s) were reconciled with the broker today.",
            "",
        ]
    else:
        lines += ["### Trades today", "", "No trades were made.", ""]

    if activity["holdings"]:
        lines += ["", "### Holdings", "", "| Ticker | Quantity | Avg cost |", "| --- | --- | --- |"]
        for ticker, quantity, avg_usd, _close in activity["holdings"]:
            lines.append(f"| {ticker} | {quantity} | ${avg_usd} |")

    if activity["decisions"]:
        lines += [
            "",
            "### Decisions",
            "",
            "| Ticker | Action | Confidence | Approved | Bound by |",
            "| --- | --- | --- | --- | --- |",
        ]
        for ticker, action, confidence, approved, _reasoning, binding in activity["decisions"]:
            amount = f"${approved}" if approved is not None else "—"
            lines.append(f"| {ticker} | {action} | {confidence} | {amount} | {binding} |")

    lines.append("")
    return lines


def _facts_table(as_of, accounts: dict[str, dict], total_spend, credit) -> str:
    """The figures, rendered deterministically. The model never touches these."""
    lines = [f"# MarketAgent — {as_of}", ""]
    lines += _comparison_section(accounts)
    for name in ACCOUNTS:
        lines += _account_section(name, accounts[name])
    lines += _spend_section({n: accounts[n]["spend"] for n in ACCOUNTS}, total_spend, credit)
    return "\n".join(lines)


def _account_run_alert(runs) -> str | None:
    """A few words for one account's contribution to the subject line."""
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


def _run_alert(accounts: dict[str, dict]) -> str | None:
    """A few words for the subject line, or None when both accounts ran cleanly.

    The subject is the only part of this email that survives being read on a
    phone's lock screen, and it is built from stored figures precisely so the
    model cannot influence it. A day an account never ran still has a
    valuation and still produces a perfectly ordinary-looking subject, which
    is the one case where ordinary-looking is wrong.
    """
    parts = []
    for name in ACCOUNTS:
        if alert := _account_run_alert(accounts[name]["activity"]["runs"]):
            parts.append(f"{name}: {alert}")
    return "; ".join(parts) if parts else None


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
            "### Agent run",
            "",
            "**No agent run is recorded for today.** The job either did not "
            "start or died before it could open a row. Nothing below describes "
            "a decision the agent declined to take — it describes an agent "
            "that did not run.",
            "",
        ]

    lines = [
        "### Agent run",
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


def _prompt(facts: str, accounts: dict[str, dict]) -> str:
    sections = []
    for name in ACCOUNTS:
        reasoning = "\n".join(
            f"- {ticker} ({action}): {text}"
            for ticker, action, _confidence, _approved, text, _binding in accounts[name][
                "activity"
            ]["decisions"]
            if text
        )
        sections.append(f"### {name}\n{reasoning or 'No decisions were taken.'}")
    return (
        f"{facts}\n\n"
        "## The AI's own reasoning today\n\n" + "\n\n".join(sections) + "\n\n"
        "Write the commentary that goes under the tables above. Address both accounts "
        "explicitly — say which is ahead today and whether that continues or reverses "
        "a trend, using only the figures above — rather than writing about them as one "
        "merged portfolio."
    )
