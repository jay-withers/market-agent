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
from ..fx import fetch_gbp_usd
from ..llm.anthropic_provider import AnthropicLlm
from ..llm.base import PROMPT_VERSION, Llm
from ..mailer import MailResult, send
from ..marketdata import fetch_daily_bars, latest_close
from ..models import money
from ..settings import settings

logger = logging.getLogger(__name__)

PRICE_HISTORY_DAYS = 7


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

    rate = fetch_gbp_usd()
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
        conn.commit()

    facts = _facts_table(as_of, state, initial, pnl, pnl_pct, points, filled, activity)
    subject = f"InvestAgent {as_of}: £{total} ({'+' if pnl >= 0 else ''}{pnl_pct}%)"

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
        )
        conn.commit()

    logger.info("summary %d for %s: %s, %d fill(s) reconciled", summary_id, as_of, subject, filled)
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


def _facts_table(as_of, state, initial, pnl, pnl_pct, points, filled, activity) -> str:
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

    return "\n".join(lines)


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
