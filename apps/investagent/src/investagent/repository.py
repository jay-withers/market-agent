"""Every SQL statement the application runs, in one place.

A deviation from the plan's file layout, which lists no repository module. The
alternative is SQL inside `jobs/agent.py` and again inside the API's routers,
reading the same tables two different ways — so this keeps the statements
together and lets both callers share them.

Each function takes a connection rather than reaching for the pool, so a caller
can put several writes in one transaction. The agent job relies on that: a
decision and the trade implementing it must both land or neither.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from .marketdata import Bar
from .models import PortfolioState, Position, Recommendation, RiskVerdict, money
from .news import Article

DEFAULT_PORTFOLIO = "default"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def active_tickers(conn: Any) -> list[str]:
    """The watchlist the agent analyses and may trade.

    Benchmarks are excluded explicitly as well as by `is_active`. They exist in
    `companies` only so `prices.ticker` has something to reference, and
    analysing SPY as though it were a stock pick would waste a model call at
    best and place a trade at worst.
    """
    rows = conn.execute(
        "SELECT ticker FROM companies WHERE is_active AND NOT is_benchmark ORDER BY ticker"
    ).fetchall()
    return [r[0] for r in rows]


def portfolio_id(conn: Any, name: str = DEFAULT_PORTFOLIO) -> int:
    row = conn.execute("SELECT id FROM portfolio WHERE name = %s", (name,)).fetchone()
    if row is None:
        raise LookupError(f"portfolio '{name}' does not exist — run sql/003-seed-watchlist.sql")
    return int(row[0])


def load_cash(conn: Any, pid: int) -> Decimal:
    row = conn.execute("SELECT cash_gbp FROM portfolio WHERE id = %s", (pid,)).fetchone()
    return money(row[0])


def load_positions(conn: Any, pid: int) -> list[tuple[str, Decimal, Decimal, Decimal]]:
    """Holdings as (ticker, quantity, avg_cost_usd, avg_cost_gbp).

    Deliberately not returning a `Position`: that carries `value_gbp`, which is
    a mark to the latest close and so cannot come from this table alone.
    """
    rows = conn.execute(
        "SELECT ticker, quantity, avg_cost_usd, avg_cost_gbp FROM positions "
        "WHERE portfolio_id = %s AND quantity > 0 ORDER BY ticker",
        (pid,),
    ).fetchall()
    return [(r[0], Decimal(r[1]), Decimal(r[2]), Decimal(r[3])) for r in rows]


def trades_today(conn: Any, pid: int) -> int:
    """Trades already made today, for the risk engine's daily budget.

    Counted in UTC to match the cron schedule, and counting simulated trades
    too: a dry run that ignored the daily limit would not be exercising the
    same code path as a real one.
    """
    row = conn.execute(
        "SELECT count(*) FROM trades WHERE portfolio_id = %s "
        "AND created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') "
        "AND status <> 'rejected'",
        (pid,),
    ).fetchone()
    return int(row[0])


def build_state(
    conn: Any, pid: int, closes: dict[str, Bar], rate_gbp_usd: Decimal
) -> tuple[PortfolioState, list[str]]:
    """The portfolio as the risk engine and the model should see it.

    Returns the state plus the tickers whose price is missing. A holding with no
    current price cannot be valued, and valuing it at zero would understate
    exposure and let the engine approve a buy it should refuse — so the caller
    is told rather than quietly given a wrong total.
    """
    positions = []
    unpriced = []
    for ticker, quantity, _avg_usd, avg_gbp in load_positions(conn, pid):
        bar = closes.get(ticker)
        if bar is None:
            unpriced.append(ticker)
            continue
        positions.append(
            Position(
                ticker=ticker,
                quantity=quantity,
                value_gbp=money(quantity * bar.close_usd / rate_gbp_usd),
                avg_cost_gbp=money(avg_gbp),
            )
        )

    return (
        PortfolioState(cash_gbp=load_cash(conn, pid), positions=tuple(positions)),
        unpriced,
    )


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


def save_prices(conn: Any, bars: list[Bar]) -> int:
    """Upsert daily bars. Re-running the agent on one day must not duplicate."""
    if not bars:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO prices (ticker, bar_date, open_usd, high_usd, low_usd, close_usd, volume)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (ticker, bar_date) DO UPDATE SET"
            "   open_usd = EXCLUDED.open_usd, high_usd = EXCLUDED.high_usd,"
            "   low_usd = EXCLUDED.low_usd, close_usd = EXCLUDED.close_usd,"
            "   volume = EXCLUDED.volume, fetched_at = now()",
            [
                (b.ticker, b.bar_date, b.open_usd, b.high_usd, b.low_usd, b.close_usd, b.volume)
                for b in bars
            ],
        )
    return len(bars)


def save_news(conn: Any, articles: list[Article]) -> dict[str, int]:
    """Upsert articles, returning our row id keyed by the provider's id.

    The ids are what `ai_decisions.news_ids` records, so a decision can be
    replayed against exactly the articles the model was shown.
    """
    if not articles:
        return {}
    ids: dict[str, int] = {}
    with conn.cursor() as cur:
        for a in articles:
            row = cur.execute(
                "INSERT INTO news (external_id, source, tickers, headline, summary, author,"
                "                  url, published_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                # DO UPDATE rather than DO NOTHING: an article can be revised,
                # and DO NOTHING returns no row, so we would lose the id.
                " ON CONFLICT (external_id) DO UPDATE SET"
                "   headline = EXCLUDED.headline, summary = EXCLUDED.summary,"
                "   tickers = EXCLUDED.tickers"
                " RETURNING id",
                (
                    a.external_id,
                    a.source,
                    list(a.tickers),
                    a.headline,
                    a.summary,
                    a.author,
                    a.url,
                    a.published_at,
                ),
            ).fetchone()
            ids[a.external_id] = int(row[0])
    return ids


def save_news_analysis(conn: Any, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO news_analysis (news_id, ticker, relevant, sentiment, sentiment_score,"
            "                           rationale, model, prompt_version, input_tokens,"
            "                           output_tokens)"
            " VALUES (%(news_id)s, %(ticker)s, %(relevant)s, %(sentiment)s, %(sentiment_score)s,"
            "         %(rationale)s, %(model)s, %(prompt_version)s, %(input_tokens)s,"
            "         %(output_tokens)s)"
            # The unique key includes model and prompt version, so re-running
            # the same prompt is a no-op while a new prompt adds rows.
            " ON CONFLICT (news_id, ticker, model, prompt_version) DO NOTHING",
            rows,
        )
    return len(rows)


# ---------------------------------------------------------------------------
# The run, its decisions, and its trades
# ---------------------------------------------------------------------------


def open_run(conn: Any, trigger: str, dry_run: bool, image_tag: str | None) -> int:
    """Open an `agent_runs` row before any work, so a crash leaves evidence."""
    row = conn.execute(
        "INSERT INTO agent_runs (trigger, dry_run, image_tag) VALUES (%s, %s, %s) RETURNING id",
        (trigger, dry_run, image_tag),
    ).fetchone()
    return int(row[0])


def close_run(
    conn: Any,
    run_id: int,
    status: str,
    counts: dict[str, int],
    input_tokens: int,
    output_tokens: int,
    cost_usd: Decimal,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE agent_runs SET finished_at = now(), status = %s,"
        "  tickers_considered = %s, news_fetched = %s, news_relevant = %s,"
        "  decisions_made = %s, trades_executed = %s,"
        "  input_tokens = %s, output_tokens = %s, cost_usd = %s, error = %s"
        " WHERE id = %s",
        (
            status,
            counts.get("tickers_considered", 0),
            counts.get("news_fetched", 0),
            counts.get("news_relevant", 0),
            counts.get("decisions_made", 0),
            counts.get("trades_executed", 0),
            input_tokens,
            output_tokens,
            cost_usd,
            error,
            run_id,
        ),
    )


def save_decision(
    conn: Any,
    run_id: int,
    rec: Recommendation,
    verdict: RiskVerdict,
    state: PortfolioState,
    model: str,
    prompt_version: str,
    news_ids: list[int],
    input_tokens: int,
    output_tokens: int,
) -> int:
    row = conn.execute(
        "INSERT INTO ai_decisions (run_id, ticker, action, confidence, reasoning, risks,"
        "   model, prompt_version, news_ids, recommended_amount_gbp, approved_amount_gbp,"
        "   portfolio_state, risk_verdict, input_tokens, output_tokens)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            run_id,
            rec.ticker,
            rec.action,
            rec.confidence,
            rec.reasoning,
            rec.risks,
            model,
            prompt_version,
            news_ids,
            money(rec.suggested_amount_gbp) if rec.suggested_amount_gbp else None,
            verdict.approved_amount_gbp,
            # mode="json" so Decimals serialise as strings rather than failing:
            # jsonb has no decimal type, and float would defeat the point of
            # storing exactly what the model was shown.
            Jsonb(state.model_dump(mode="json")),
            Jsonb(verdict.model_dump(mode="json")),
            input_tokens,
            output_tokens,
        ),
    ).fetchone()
    return int(row[0])


def save_trade(
    conn: Any,
    pid: int,
    decision_id: int,
    ticker: str,
    side: str,
    notional_gbp: Decimal,
    notional_usd: Decimal,
    fx_rate: Decimal,
    fx_rate_as_of: date,
    status: str,
    dry_run: bool,
    client_order_id: str,
    broker_order_id: str | None,
    quantity: Decimal | None,
    price_usd: Decimal | None,
    submitted_at: Any,
    filled_at: Any,
) -> int:
    row = conn.execute(
        "INSERT INTO trades (portfolio_id, decision_id, ticker, side, quantity, price_usd,"
        "   notional_usd, notional_gbp, fx_rate_gbp_usd, fx_rate_as_of, broker,"
        "   broker_order_id, client_order_id, status, dry_run, submitted_at, filled_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'alpaca', %s, %s, %s, %s, %s, %s)"
        # client_order_id is deterministic per decision, so a resubmission
        # updates the existing row instead of creating a second trade for one
        # decision.
        " ON CONFLICT (client_order_id) DO UPDATE SET"
        "   status = EXCLUDED.status, quantity = EXCLUDED.quantity,"
        "   price_usd = EXCLUDED.price_usd, broker_order_id = EXCLUDED.broker_order_id,"
        "   filled_at = EXCLUDED.filled_at"
        " RETURNING id",
        (
            pid,
            decision_id,
            ticker,
            side,
            quantity,
            price_usd,
            notional_usd,
            notional_gbp,
            fx_rate,
            fx_rate_as_of,
            broker_order_id,
            client_order_id,
            status,
            dry_run,
            submitted_at,
            filled_at,
        ),
    ).fetchone()
    return int(row[0])


def apply_fill(
    conn: Any,
    pid: int,
    ticker: str,
    side: str,
    quantity: Decimal,
    notional_gbp: Decimal,
    price_usd: Decimal,
    price_gbp: Decimal,
) -> None:
    """Move cash and the position to reflect a fill.

    Only called for a *filled* order — a submission still resting at the broker
    has moved nothing. Our tables are the ledger, so this is what makes the
    GBP 500 figure move; Alpaca's own balance describes a different portfolio.
    """
    signed = quantity if side == "BUY" else -quantity
    cash_delta = -notional_gbp if side == "BUY" else notional_gbp

    conn.execute(
        "UPDATE portfolio SET cash_gbp = cash_gbp + %s, updated_at = now() WHERE id = %s",
        (cash_delta, pid),
    )

    conn.execute(
        "INSERT INTO positions (portfolio_id, ticker, quantity, avg_cost_usd, avg_cost_gbp)"
        " VALUES (%s, %s, %s, %s, %s)"
        # Weighted average cost on a buy; on a sell the average is unchanged
        # and only the quantity falls, which is what makes realised and
        # unrealised return separable later.
        " ON CONFLICT (portfolio_id, ticker) DO UPDATE SET"
        "   avg_cost_usd = CASE WHEN %s > 0 THEN"
        "     (positions.avg_cost_usd * positions.quantity + %s * %s)"
        "     / NULLIF(positions.quantity + %s, 0)"
        "   ELSE positions.avg_cost_usd END,"
        "   avg_cost_gbp = CASE WHEN %s > 0 THEN"
        "     (positions.avg_cost_gbp * positions.quantity + %s)"
        "     / NULLIF(positions.quantity + %s, 0)"
        "   ELSE positions.avg_cost_gbp END,"
        "   quantity = positions.quantity + %s,"
        "   updated_at = now()",
        (
            pid,
            ticker,
            signed,
            price_usd,
            price_gbp,
            signed,
            price_usd,
            quantity,
            signed,
            signed,
            notional_gbp,
            signed,
            signed,
        ),
    )

    # A fully closed position is deleted rather than left at zero, so
    # `load_positions` and the concentration cap do not have to filter it out.
    conn.execute(
        "DELETE FROM positions WHERE portfolio_id = %s AND ticker = %s AND quantity <= 0",
        (pid, ticker),
    )


# ---------------------------------------------------------------------------
# Reconciliation and the daily rollup, both owned by the summary job
# ---------------------------------------------------------------------------


def unreconciled_trades(conn: Any, pid: int) -> list[dict[str, Any]]:
    """Trades submitted to the broker whose outcome we do not yet know.

    The agent runs at 06:00 UTC and the market opens at 14:30, so a scheduled
    run's orders are still resting when it finishes. This is the queue the
    summary job works through at 21:00.

    Dry-run trades are excluded: they never reached a broker, so there is
    nothing to ask about.
    """
    rows = conn.execute(
        "SELECT id, ticker, side, client_order_id, notional_gbp, fx_rate_gbp_usd"
        " FROM trades"
        " WHERE portfolio_id = %s AND dry_run = false"
        "   AND client_order_id IS NOT NULL"
        "   AND status IN ('pending', 'submitted', 'partially_filled')"
        " ORDER BY created_at",
        (pid,),
    ).fetchall()
    return [
        {
            "id": r[0],
            "ticker": r[1],
            "side": r[2],
            "client_order_id": r[3],
            "notional_gbp": Decimal(r[4]),
            "fx_rate_gbp_usd": Decimal(r[5]),
        }
        for r in rows
    ]


def update_trade_outcome(
    conn: Any,
    trade_id: int,
    status: str,
    quantity: Decimal | None,
    price_usd: Decimal | None,
    filled_at: Any,
) -> None:
    conn.execute(
        "UPDATE trades SET status = %s, quantity = %s, price_usd = %s, filled_at = %s"
        " WHERE id = %s",
        (status, quantity, price_usd, filled_at, trade_id),
    )


def portfolio_inception(conn: Any, pid: int) -> date:
    """The day the experiment started, which every benchmark is indexed from."""
    row = conn.execute("SELECT created_at::date FROM portfolio WHERE id = %s", (pid,)).fetchone()
    return row[0]


def initial_cash(conn: Any, pid: int) -> Decimal:
    row = conn.execute("SELECT initial_cash_gbp FROM portfolio WHERE id = %s", (pid,)).fetchone()
    return money(row[0])


def close_on(conn: Any, ticker: str, on_or_after: date) -> Decimal | None:
    """The first close at or after `on_or_after`, for indexing a benchmark.

    At or after, not on: the inception date is frequently a weekend or a
    holiday, and demanding an exact match would silently drop the benchmark.
    """
    row = conn.execute(
        "SELECT close_usd FROM prices WHERE ticker = %s AND bar_date >= %s"
        " ORDER BY bar_date LIMIT 1",
        (ticker, on_or_after),
    ).fetchone()
    return Decimal(row[0]) if row else None


def save_daily_performance(
    conn: Any,
    pid: int,
    as_of: date,
    cash_gbp: Decimal,
    positions_value_gbp: Decimal,
    total_value_gbp: Decimal,
    pnl_gbp: Decimal,
    pnl_pct: Decimal,
    fx_rate: Decimal,
    fx_rate_as_of: date,
) -> None:
    conn.execute(
        "INSERT INTO daily_performance (portfolio_id, as_of, cash_gbp, positions_value_gbp,"
        "   total_value_gbp, pnl_gbp, pnl_pct, fx_rate_gbp_usd, fx_rate_as_of)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
        # Re-running the summary for a day overwrites its row rather than
        # failing, so a retry after a mail outage is safe.
        " ON CONFLICT (portfolio_id, as_of) DO UPDATE SET"
        "   cash_gbp = EXCLUDED.cash_gbp,"
        "   positions_value_gbp = EXCLUDED.positions_value_gbp,"
        "   total_value_gbp = EXCLUDED.total_value_gbp,"
        "   pnl_gbp = EXCLUDED.pnl_gbp, pnl_pct = EXCLUDED.pnl_pct,"
        "   fx_rate_gbp_usd = EXCLUDED.fx_rate_gbp_usd,"
        "   fx_rate_as_of = EXCLUDED.fx_rate_as_of, computed_at = now()",
        (
            pid,
            as_of,
            cash_gbp,
            positions_value_gbp,
            total_value_gbp,
            pnl_gbp,
            pnl_pct,
            fx_rate,
            fx_rate_as_of,
        ),
    )


def save_benchmarks(conn: Any, points: list[Any]) -> int:
    if not points:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO benchmarks (symbol, as_of, close_usd, fx_rate_gbp_usd, value_gbp,"
            "                        source)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (symbol, as_of) DO UPDATE SET"
            "   close_usd = EXCLUDED.close_usd, value_gbp = EXCLUDED.value_gbp,"
            "   fx_rate_gbp_usd = EXCLUDED.fx_rate_gbp_usd, fetched_at = now()",
            [
                (p.symbol, p.as_of, p.close_usd, p.fx_rate_gbp_usd, p.value_gbp, p.source)
                for p in points
            ],
        )
    return len(points)


def day_activity(conn: Any, pid: int, as_of: date) -> dict[str, Any]:
    """What happened on `as_of`, as the narrative's raw material."""
    decisions = conn.execute(
        "SELECT ticker, action, confidence, approved_amount_gbp, reasoning,"
        "       risk_verdict->>'binding_constraint'"
        " FROM ai_decisions WHERE decided_at::date = %s ORDER BY ticker",
        (as_of,),
    ).fetchall()

    trades = conn.execute(
        "SELECT ticker, side, status, notional_gbp, quantity, price_usd"
        " FROM trades WHERE portfolio_id = %s AND created_at::date = %s ORDER BY id",
        (pid, as_of),
    ).fetchall()

    holdings = conn.execute(
        "SELECT p.ticker, p.quantity, p.avg_cost_gbp, lc.close_usd"
        " FROM positions p"
        " LEFT JOIN LATERAL ("
        "   SELECT close_usd FROM prices WHERE ticker = p.ticker ORDER BY bar_date DESC LIMIT 1"
        " ) lc ON true"
        " WHERE p.portfolio_id = %s ORDER BY p.ticker",
        (pid,),
    ).fetchall()

    return {"decisions": decisions, "trades": trades, "holdings": holdings}


def save_summary(
    conn: Any,
    as_of: date,
    subject: str,
    body_markdown: str,
    body_html: str,
    model: str | None,
    prompt_version: str | None,
    email_status: str,
    provider_id: str | None,
    error: str | None,
) -> int:
    row = conn.execute(
        "INSERT INTO daily_summaries (as_of, subject, body_markdown, body_html, model,"
        "   prompt_version, email_status, email_provider_id, email_error, sent_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,"
        "         CASE WHEN %s = 'sent' THEN now() ELSE NULL END)"
        " ON CONFLICT (as_of) DO UPDATE SET"
        "   subject = EXCLUDED.subject, body_markdown = EXCLUDED.body_markdown,"
        "   body_html = EXCLUDED.body_html, email_status = EXCLUDED.email_status,"
        "   email_provider_id = EXCLUDED.email_provider_id,"
        "   email_error = EXCLUDED.email_error, sent_at = EXCLUDED.sent_at"
        " RETURNING id",
        (
            as_of,
            subject,
            body_markdown,
            body_html,
            model,
            prompt_version,
            email_status,
            provider_id,
            error,
            email_status,
        ),
    ).fetchone()
    return int(row[0])
