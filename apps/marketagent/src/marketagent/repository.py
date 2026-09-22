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

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .marketdata import Bar
from .models import PortfolioState, Position, Recommendation, RiskVerdict, money
from .news import Article

# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def active_tickers(conn: Any, pid: int) -> list[str]:
    """The watchlist this account analyses and may trade.

    Scoped by `portfolio_watchlist` rather than `companies.is_active`: two
    accounts trade different, only partially overlapping universes, so which
    tickers are tradeable is a per-portfolio fact, not a global one. Benchmarks
    are excluded via `companies.is_benchmark` regardless — they exist in
    `companies` only so `prices.ticker` has something to reference, and
    analysing SPY as though it were a stock pick would waste a model call at
    best and place a trade at worst.
    """
    rows = conn.execute(
        "SELECT w.ticker FROM portfolio_watchlist w JOIN companies c ON c.ticker = w.ticker"
        " WHERE w.portfolio_id = %s AND w.is_active AND NOT c.is_benchmark ORDER BY w.ticker",
        (pid,),
    ).fetchall()
    return [r[0] for r in rows]


def watchlist_tickers(conn: Any, pid: int) -> set[str]:
    """Every ticker currently active on this account's watchlist.

    Used by the rebalance job to diff against a freshly fetched index list —
    `active_tickers` also excludes benchmarks, which is irrelevant here since
    a benchmark is never inserted into `portfolio_watchlist` in the first
    place, but this stays separate so a caller does not have to reason about
    that to trust the result.
    """
    rows = conn.execute(
        "SELECT ticker FROM portfolio_watchlist WHERE portfolio_id = %s AND is_active",
        (pid,),
    ).fetchall()
    return {r[0] for r in rows}


def add_to_watchlist(conn: Any, pid: int, tickers: list[tuple[str, str, str | None]]) -> None:
    """Add or re-activate tickers on an account's watchlist.

    `tickers` is `(ticker, name, sector)`. `companies` is upserted first since
    `portfolio_watchlist.ticker` references it — a name the rebalance job has
    never seen before must exist as reference data before it can be added to
    anyone's watchlist. Re-activating a previously dropped ticker (one back in
    the index after falling out) clears `removed_at` rather than leaving a
    stale value, since it is active again.
    """
    if not tickers:
        return
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO companies (ticker, name, sector) VALUES (%s, %s, %s)"
            " ON CONFLICT (ticker) DO UPDATE SET name = EXCLUDED.name, sector = EXCLUDED.sector",
            tickers,
        )
        cur.executemany(
            "INSERT INTO portfolio_watchlist (portfolio_id, ticker, is_active, source)"
            " VALUES (%s, %s, true, 'sp500_index')"
            " ON CONFLICT (portfolio_id, ticker) DO UPDATE SET"
            "   is_active = true, source = 'sp500_index', removed_at = NULL",
            [(pid, t[0]) for t in tickers],
        )


def remove_from_watchlist(conn: Any, pid: int, tickers: list[str]) -> None:
    """Drop tickers from an account's watchlist without touching any position.

    `is_active = false` only — never a row delete and never a change to
    `positions`/`trades`/`ai_decisions`. A ticker that falls out of the index
    simply stops being analysed and traded from its next run onward; any
    existing holding stays tracked and valued exactly as a manually-held
    ticker already is, sellable only by a human going directly through
    Alpaca. The rebalance job never auto-sells.
    """
    if not tickers:
        return
    conn.execute(
        "UPDATE portfolio_watchlist SET is_active = false, removed_at = now()"
        " WHERE portfolio_id = %s AND ticker = ANY(%s)",
        (pid, tickers),
    )


def portfolio_id(conn: Any, name: str) -> int:
    """Look up the id of one of the two accounts ('static-100'/'dynamic-500').

    No default: every caller must say which account it means, so a forgotten
    argument fails loudly instead of silently picking one.
    """
    row = conn.execute("SELECT id FROM portfolio WHERE name = %s", (name,)).fetchone()
    if row is None:
        raise LookupError(f"portfolio '{name}' does not exist — run sql/009-two-accounts.sql")
    return int(row[0])


def load_cash(conn: Any, pid: int) -> Decimal:
    row = conn.execute("SELECT cash_usd FROM portfolio WHERE id = %s", (pid,)).fetchone()
    return money(row[0])


def save_display_fx(conn: Any, pid: int, rate: Decimal, as_of: date) -> None:
    """Store the USD-per-GBP rate used only for optional dashboard display."""
    conn.execute(
        "UPDATE portfolio SET display_gbp_usd=%s, display_fx_as_of=%s WHERE id=%s",
        (rate, as_of, pid),
    )


def load_positions(conn: Any, pid: int) -> list[tuple[str, Decimal, Decimal]]:
    """Holdings as (ticker, quantity, USD average entry price)."""
    rows = conn.execute(
        "SELECT ticker, quantity, avg_cost_usd FROM positions "
        "WHERE portfolio_id = %s AND quantity <> 0 ORDER BY ticker",
        (pid,),
    ).fetchall()
    return [(r[0], Decimal(r[1]), Decimal(r[2])) for r in rows]


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


def recent_decisions(conn: Any, pid: int, ticker: str, limit: int) -> list[dict[str, Any]]:
    """The agent's own recent decisions on one ticker, for the analysis prompt.

    Without this the model re-argues the same thesis every morning: nothing in
    the prompt otherwise tells it that it recommended the same BUY four days
    running, or that the engine clamped every one of them to the same cap.

    Filtered by `pid`: with two accounts able to hold decisions on the same
    ticker (the two watchlists overlap heavily), an unfiltered query would leak
    one account's decision history into the other's prompt.

    The verdict and the resulting trade's status both come along, because
    "approved" and "filled" are different facts — a scheduled run submits
    before the market opens, so its orders rest for hours.

    `ai_decisions_portfolio_ticker_idx` on (portfolio_id, ticker, decided_at
    DESC) covers the ordering.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            "SELECT d.decided_at::date AS on_date, d.action, d.confidence,"
            "   d.recommended_amount_usd, d.approved_amount_usd,"
            "   d.risk_verdict->>'binding_constraint' AS binding_constraint,"
            "   t.status AS trade_status"
            " FROM ai_decisions d"
            # A lateral single-row join rather than a plain one: a decision has
            # at most one trade today, but a join that *could* duplicate a
            # decision would silently double an entry in the prompt if that ever
            # stopped being true.
            " LEFT JOIN LATERAL ("
            "   SELECT status FROM trades WHERE decision_id = d.id"
            "   ORDER BY created_at DESC LIMIT 1"
            " ) t ON true"
            " WHERE d.portfolio_id = %s AND d.ticker = %s"
            " ORDER BY d.decided_at DESC LIMIT %s",
            (pid, ticker, limit),
        ).fetchall()


def spend(conn: Any, as_of: date, pid: int | None = None) -> dict[str, Any]:
    """What has been spent with the model, from every job that spends.

    Three tables because three jobs call the API — the agent per run, the
    summary and the weekly review once each. Summing only `agent_runs` would
    read low by a call a day and a call a week, and the gap grows for as long
    as the experiment runs.

    `pid` scopes the total to one account's `agent_runs` spend only. With
    `pid=None`, the total additionally folds in `daily_summaries`/
    `weekly_reviews` cost, which is never split by account — those two jobs
    are combined across both portfolios (see jobs/summary.py, jobs/weekly.py),
    so their spend isn't attributable to one account. The two accounts share
    one DeepSeek API key and one account balance, so the runway figure the
    summary email shows is computed once from the `pid=None` total, not twice.

    A row with a NULL cost is one written before the column existed, or a run
    that failed before its first call. Excluded rather than counted as zero:
    the totals are what is *known* to have been spent, and `known_from` says
    since when, so an incomplete history reads as incomplete.
    """
    agent_filter = "AND portfolio_id = %(pid)s" if pid is not None else ""
    job_totals = (
        "   UNION ALL"
        "   SELECT as_of, cost_usd FROM daily_summaries WHERE cost_usd IS NOT NULL"
        "   UNION ALL"
        "   SELECT period_end, cost_usd FROM weekly_reviews WHERE cost_usd IS NOT NULL"
        if pid is None
        else ""
    )
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            "WITH all_spend AS ("
            "   SELECT started_at::date AS on_date, cost_usd FROM agent_runs"
            f"     WHERE cost_usd IS NOT NULL {agent_filter}"
            f"   {job_totals}"
            " )"
            " SELECT"
            "   coalesce(sum(cost_usd) FILTER (WHERE on_date = %(as_of)s), 0) AS today_usd,"
            "   coalesce(sum(cost_usd) FILTER (WHERE on_date >= %(as_of)s - 6), 0)"
            "     AS last_7_days_usd,"
            "   coalesce(sum(cost_usd), 0) AS to_date_usd,"
            "   min(on_date) AS known_from"
            " FROM all_spend",
            {"as_of": as_of, "pid": pid},
        ).fetchone()


def build_state(conn: Any, pid: int, closes: dict[str, Bar]) -> tuple[PortfolioState, list[str]]:
    """Use broker marks when synchronized, otherwise local closes for simulations."""
    positions = []
    unpriced = []
    rows = conn.execute(
        "SELECT ticker, quantity, avg_cost_usd, market_value_usd FROM positions "
        "WHERE portfolio_id=%s AND quantity <> 0 ORDER BY ticker",
        (pid,),
    ).fetchall()
    for ticker, quantity, average, market_value in rows:
        if market_value is None:
            bar = closes.get(ticker)
            if bar is None:
                unpriced.append(ticker)
                continue
            market_value = quantity * bar.close_usd
        positions.append(
            Position(
                ticker=ticker,
                quantity=quantity,
                value_usd=money(market_value),
                avg_cost_usd=money(average),
            )
        )
    row = conn.execute("SELECT equity_usd FROM portfolio WHERE id=%s", (pid,)).fetchone()
    return PortfolioState(
        cash_usd=load_cash(conn, pid), positions=tuple(positions), equity_usd=row[0]
    ), unpriced


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


# Matches `replica_timeout_in_seconds` on the container app jobs. A run still
# `running` past it cannot be alive: Container Apps has already terminated the
# replica. Owned here rather than in `queries.py` because both the API's run
# list and the daily summary have to draw the same line between a run in
# progress and one that was killed hard enough never to close its own row.
JOB_TIMEOUT_SECONDS = 1800


def open_run(conn: Any, pid: int, trigger: str, dry_run: bool, image_tag: str | None) -> int:
    """Open an `agent_runs` row before any work, so a crash leaves evidence."""
    row = conn.execute(
        "INSERT INTO agent_runs (portfolio_id, trigger, dry_run, image_tag)"
        " VALUES (%s, %s, %s, %s) RETURNING id",
        (pid, trigger, dry_run, image_tag),
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
    pid: int,
    run_id: int,
    rec: Recommendation,
    verdict: RiskVerdict,
    state: PortfolioState,
    model: str,
    prompt_version: str,
    news_ids: list[int],
    input_tokens: int,
    output_tokens: int,
    prompt_context: dict[str, Any] | None = None,
) -> int:
    """Store one decision.

    `prompt_context` is whatever the model was shown that `portfolio_state`
    does not already carry — today, its own decision history. Without it the
    replayability claim on `PortfolioState` quietly stops being true: the
    prompt would contain an input no stored column records.
    """
    row = conn.execute(
        "INSERT INTO ai_decisions (portfolio_id, run_id, ticker, action, confidence, reasoning,"
        "   risks, model, prompt_version, news_ids, recommended_amount_usd,"
        "   approved_amount_usd, portfolio_state, risk_verdict, prompt_context,"
        "   input_tokens, output_tokens)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " RETURNING id",
        (
            pid,
            run_id,
            rec.ticker,
            rec.action,
            rec.confidence,
            rec.reasoning,
            rec.risks,
            model,
            prompt_version,
            news_ids,
            money(rec.suggested_amount_usd) if rec.suggested_amount_usd else None,
            verdict.approved_amount_usd,
            # mode="json" so Decimals serialise as strings rather than failing:
            # jsonb has no decimal type, and float would defeat the point of
            # storing exactly what the model was shown.
            Jsonb(state.model_dump(mode="json")),
            Jsonb(verdict.model_dump(mode="json")),
            Jsonb(_jsonable(prompt_context)) if prompt_context else None,
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
    notional_usd: Decimal,
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
        " notional_usd, broker_order_id, client_order_id, status, dry_run, submitted_at, filled_at)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (client_order_id) DO UPDATE SET status=EXCLUDED.status,"
        " quantity=EXCLUDED.quantity, price_usd=EXCLUDED.price_usd,"
        " broker_order_id=EXCLUDED.broker_order_id, filled_at=EXCLUDED.filled_at RETURNING id",
        (
            pid,
            decision_id,
            ticker,
            side,
            quantity,
            price_usd,
            notional_usd,
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
    notional_usd: Decimal,
    price_usd: Decimal,
) -> None:
    """Apply a simulated fill. Paper account balances come only from broker sync."""
    signed = quantity if side == "BUY" else -quantity
    cash_delta = -notional_usd if side == "BUY" else notional_usd
    conn.execute(
        "UPDATE portfolio SET cash_usd=cash_usd+%s, equity_usd=NULL, updated_at=now() WHERE id=%s",
        (cash_delta, pid),
    )
    conn.execute(
        "INSERT INTO positions (portfolio_id,ticker,quantity,avg_cost_usd) VALUES (%s,%s,%s,%s)"
        " ON CONFLICT (portfolio_id,ticker) DO UPDATE SET"
        " avg_cost_usd=CASE WHEN %s>0 THEN"
        " (positions.avg_cost_usd*positions.quantity+%s)/NULLIF(positions.quantity+%s,0)"
        " ELSE positions.avg_cost_usd END, quantity=positions.quantity+%s,"
        " market_value_usd=NULL, updated_at=now()",
        (pid, ticker, signed, price_usd, signed, notional_usd, signed, signed),
    )
    conn.execute(
        "DELETE FROM positions WHERE portfolio_id=%s AND ticker=%s AND quantity<=0", (pid, ticker)
    )


def unreconciled_trades(conn: Any, pid: int) -> list[dict[str, Any]]:
    """Orders whose latest fill status needs checking, including partial cancellations."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            "SELECT id,ticker,side,client_order_id,notional_usd FROM trades "
            "WHERE portfolio_id=%s AND NOT dry_run AND client_order_id IS NOT NULL "
            "AND status IN ('pending','submitted','partially_filled') ORDER BY created_at",
            (pid,),
        ).fetchall()


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
    row = conn.execute("SELECT initial_cash_usd FROM portfolio WHERE id = %s", (pid,)).fetchone()
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
    cash_usd: Decimal,
    positions_value_usd: Decimal,
    total_value_usd: Decimal,
    pnl_usd: Decimal,
    pnl_pct: Decimal,
) -> None:
    conn.execute(
        "INSERT INTO daily_performance (portfolio_id,as_of,cash_usd,positions_value_usd,"
        "total_value_usd,pnl_usd,pnl_pct) VALUES (%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (portfolio_id,as_of) DO UPDATE SET cash_usd=EXCLUDED.cash_usd,"
        "positions_value_usd=EXCLUDED.positions_value_usd,total_value_usd=EXCLUDED.total_value_usd,"
        "pnl_usd=EXCLUDED.pnl_usd,pnl_pct=EXCLUDED.pnl_pct,computed_at=now()",
        (pid, as_of, cash_usd, positions_value_usd, total_value_usd, pnl_usd, pnl_pct),
    )


def save_benchmarks(conn: Any, pid: int, points: list[Any]) -> int:
    """Upsert benchmark points for one account.

    Keyed `(portfolio_id, symbol, as_of)`: `close_usd` (the raw market price)
    ends up harmlessly duplicated across both accounts' rows for the same
    symbol/day, but `value_usd` ("what this account's notional would be worth")
    is portfolio-dependent and would otherwise collide between the two
    accounts under the old `(symbol, as_of)` key.
    """
    if not points:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO benchmarks (portfolio_id,symbol,as_of,close_usd,value_usd,source) "
            "VALUES (%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (portfolio_id,symbol,as_of) DO UPDATE SET"
            " close_usd=EXCLUDED.close_usd,value_usd=EXCLUDED.value_usd,fetched_at=now()",
            [(pid, p.symbol, p.as_of, p.close_usd, p.value_usd, p.source) for p in points],
        )
    return len(points)


def day_activity(conn: Any, pid: int, as_of: date) -> dict[str, Any]:
    """What happened on `as_of`, as the narrative's raw material.

    `runs` is here because "no trades" and "the agent never ran" produce an
    identical trades list, and the summary is the only thing that reaches a
    human unprompted. Without it the email reports a quiet day on a day the
    06:00 job died — see `_run_section`.
    """
    runs = conn.execute(
        "SELECT started_at, finished_at, status, trigger, dry_run, error,"
        "       (status = 'running'"
        "        AND started_at < now() - make_interval(secs => %s)) AS stale"
        " FROM agent_runs WHERE portfolio_id = %s AND started_at::date = %s ORDER BY started_at",
        (JOB_TIMEOUT_SECONDS, pid, as_of),
    ).fetchall()

    decisions = conn.execute(
        "SELECT ticker, action, confidence, approved_amount_usd, reasoning,"
        "       risk_verdict->>'binding_constraint'"
        " FROM ai_decisions WHERE portfolio_id = %s AND decided_at::date = %s ORDER BY ticker",
        (pid, as_of),
    ).fetchall()

    trades = conn.execute(
        "SELECT ticker, side, status, notional_usd, quantity, price_usd"
        " FROM trades WHERE portfolio_id = %s AND created_at::date = %s ORDER BY id",
        (pid, as_of),
    ).fetchall()

    holdings = conn.execute(
        "SELECT p.ticker, p.quantity, p.avg_cost_usd, lc.close_usd"
        " FROM positions p"
        " LEFT JOIN LATERAL ("
        "   SELECT close_usd FROM prices WHERE ticker = p.ticker ORDER BY bar_date DESC LIMIT 1"
        " ) lc ON true"
        " WHERE p.portfolio_id = %s ORDER BY p.ticker",
        (pid,),
    ).fetchall()

    return {"runs": runs, "decisions": decisions, "trades": trades, "holdings": holdings}


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
    cost_usd: Decimal | None = None,
) -> int:
    row = conn.execute(
        "INSERT INTO daily_summaries (as_of, subject, body_markdown, body_html, model,"
        "   prompt_version, email_status, email_provider_id, email_error, cost_usd, sent_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
        "         CASE WHEN %s = 'sent' THEN now() ELSE NULL END)"
        " ON CONFLICT (as_of) DO UPDATE SET"
        "   subject = EXCLUDED.subject, body_markdown = EXCLUDED.body_markdown,"
        "   body_html = EXCLUDED.body_html, email_status = EXCLUDED.email_status,"
        "   email_provider_id = EXCLUDED.email_provider_id,"
        "   email_error = EXCLUDED.email_error, cost_usd = EXCLUDED.cost_usd,"
        "   sent_at = EXCLUDED.sent_at"
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
            cost_usd,
            email_status,
        ),
    ).fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# The weekly review, owned by the weekly job
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Make a metrics structure safe for `jsonb`.

    The same conversion `model_dump(mode="json")` does for the Pydantic objects
    in `ai_decisions`, but these rows come straight from SQL and are plain
    dicts: `Decimal` becomes a string rather than a float, because the column
    is a record of what the model was shown and a float would quietly lose the
    last penny.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


def week_metrics(conn: Any, pid: int, start: date, end: date) -> dict[str, Any]:
    """Everything the weekly review is allowed to state, straight from the tables.

    Reads only. The review job fetches no prices, no news and no broker state:
    the agent and the summary have already written the week down, and a review
    that re-fetched could report figures the stored series disagrees with.

    Both ends of the window are inclusive.
    """
    window = {"pid": pid, "start": start, "end": end}

    def rows(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        with conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(sql, params or window).fetchall()

    def one(sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        found = rows(sql, params)
        return found[0] if found else None

    runs = one(
        "SELECT count(*) AS runs,"
        "       count(*) FILTER (WHERE status = 'succeeded') AS succeeded,"
        "       count(*) FILTER (WHERE status = 'failed') AS failed,"
        "       count(*) FILTER (WHERE status = 'running') AS unfinished,"
        "       count(*) FILTER (WHERE dry_run) AS dry_runs,"
        "       coalesce(sum(news_fetched), 0) AS news_fetched,"
        "       coalesce(sum(news_relevant), 0) AS news_relevant,"
        "       coalesce(sum(decisions_made), 0) AS decisions_made,"
        "       coalesce(sum(trades_executed), 0) AS trades_executed,"
        "       coalesce(sum(input_tokens), 0) AS input_tokens,"
        "       coalesce(sum(output_tokens), 0) AS output_tokens,"
        # Summed from the per-run figure, which was itself accumulated per call:
        # the two stages use different models at different rates, so pricing a
        # mixed token total at either rate is simply wrong.
        "       coalesce(sum(cost_usd), 0) AS cost_usd,"
        "       avg(extract(epoch FROM finished_at - started_at)) AS avg_seconds"
        " FROM agent_runs WHERE portfolio_id = %(pid)s"
        "   AND started_at::date BETWEEN %(start)s AND %(end)s"
    )

    decisions = rows(
        "SELECT action, count(*) AS decisions,"
        "       avg(confidence) AS avg_confidence,"
        "       count(*) FILTER (WHERE (risk_verdict->>'approved')::boolean) AS approved"
        " FROM ai_decisions WHERE portfolio_id = %(pid)s"
        "   AND decided_at::date BETWEEN %(start)s AND %(end)s"
        " GROUP BY action ORDER BY action"
    )

    # The most informative table in the review. `binding_constraint` names the
    # single rule that decided each verdict, so grouped and split by outcome it
    # answers the question a week of prose cannot: which limit is actually
    # shaping this experiment. `binding` rather than `constraint` as the alias
    # because CONSTRAINT is reserved.
    constraints = rows(
        "SELECT risk_verdict->>'binding_constraint' AS binding,"
        "       (risk_verdict->>'approved')::boolean AS approved,"
        "       count(*) AS decisions"
        " FROM ai_decisions WHERE portfolio_id = %(pid)s"
        "   AND decided_at::date BETWEEN %(start)s AND %(end)s"
        " GROUP BY binding, approved ORDER BY decisions DESC, binding"
    )

    trades = rows(
        "SELECT status, count(*) AS trades,"
        "       count(*) FILTER (WHERE dry_run) AS simulated,"
        "       coalesce(sum(notional_usd), 0) AS notional_usd"
        " FROM trades WHERE portfolio_id = %(pid)s"
        "   AND created_at::date BETWEEN %(start)s AND %(end)s"
        " GROUP BY status ORDER BY status"
    )

    # Per watchlist name, from `portfolio_watchlist` rather than from the
    # decisions: a ticker the agent never reached is exactly the row worth
    # seeing, and a join driven by `ai_decisions` would omit it. Scoped to this
    # account's watchlist, not the global `companies` table, for the same
    # reason `active_tickers` is: two accounts trade different universes.
    # Scalar subqueries because the counts come from three different tables.
    tickers = rows(
        "SELECT w.ticker,"
        "       (SELECT count(*) FROM ai_decisions d WHERE d.ticker = w.ticker"
        "          AND d.portfolio_id = %(pid)s"
        "          AND d.decided_at::date BETWEEN %(start)s AND %(end)s) AS decisions,"
        "       (SELECT count(*) FROM ai_decisions d WHERE d.ticker = w.ticker"
        "          AND d.portfolio_id = %(pid)s AND d.action <> 'HOLD'"
        "          AND d.decided_at::date BETWEEN %(start)s AND %(end)s) AS convictions,"
        "       (SELECT count(*) FROM news_analysis a WHERE a.ticker = w.ticker AND a.relevant"
        "          AND a.analysed_at::date BETWEEN %(start)s AND %(end)s) AS relevant_news,"
        "       (SELECT count(*) FROM trades t WHERE t.ticker = w.ticker"
        "          AND t.portfolio_id = %(pid)s"
        "          AND t.created_at::date BETWEEN %(start)s AND %(end)s) AS trades"
        " FROM portfolio_watchlist w JOIN companies c ON c.ticker = w.ticker"
        " WHERE w.portfolio_id = %(pid)s AND w.is_active AND NOT c.is_benchmark"
        " ORDER BY w.ticker"
    )

    valuation = one(
        "SELECT as_of, total_value_usd, cash_usd, positions_value_usd, pnl_usd, pnl_pct"
        " FROM daily_performance WHERE portfolio_id = %(pid)s"
        "   AND as_of BETWEEN %(start)s AND %(end)s"
        " ORDER BY as_of DESC LIMIT 1"
    )

    # The last valuation *before* the window, which is what the week's change is
    # measured against. Absent in the experiment's first week, and the caller
    # substitutes the notional rather than reporting a change of nothing.
    opening = one(
        "SELECT total_value_usd FROM daily_performance WHERE portfolio_id = %(pid)s"
        "   AND as_of < %(start)s ORDER BY as_of DESC LIMIT 1"
    )

    # A benchmark with no point inside the window is absent from this list, not
    # reported flat: an unchanged balance reads as "the index did nothing", which is a
    # different and wrong claim from "we have no data".
    benchmarks = rows(
        "SELECT b.symbol,"
        "       (SELECT value_usd FROM benchmarks n WHERE n.portfolio_id = %(pid)s"
        "          AND n.symbol = b.symbol"
        "          AND n.as_of <= %(end)s ORDER BY n.as_of DESC LIMIT 1) AS value_usd,"
        "       (SELECT value_usd FROM benchmarks o WHERE o.portfolio_id = %(pid)s"
        "          AND o.symbol = b.symbol"
        "          AND o.as_of < %(start)s ORDER BY o.as_of DESC LIMIT 1) AS opening_usd"
        " FROM benchmarks b WHERE b.portfolio_id = %(pid)s"
        "   AND b.as_of BETWEEN %(start)s AND %(end)s"
        " GROUP BY b.symbol ORDER BY b.symbol"
    )

    # Whether the daily job actually reported. A week of missing or failed
    # emails is a finding in its own right — the 2026-09-03 send was lost to a
    # bad recipient and nothing but this column would have said so.
    summaries = one(
        "SELECT count(*) AS days,"
        "       count(*) FILTER (WHERE email_status = 'sent') AS sent,"
        "       count(*) FILTER (WHERE email_status = 'failed') AS failed,"
        "       count(*) FILTER (WHERE email_status = 'skipped') AS skipped"
        " FROM daily_summaries WHERE as_of BETWEEN %(start)s AND %(end)s"
    )

    return {
        "runs": runs or {},
        "decisions": decisions,
        "constraints": constraints,
        "trades": trades,
        "tickers": tickers,
        "valuation": valuation,
        "opening_total_usd": opening["total_value_usd"] if opening else None,
        "benchmarks": benchmarks,
        "summaries": summaries or {},
    }


# Held apart from week_metrics' reporting figures because these answer a
# different question. Everything there describes what the experiment *did*;
# these describe whether the record of it can be trusted at all.
#
# The names are a closed set for the same reason `ProposedChange.area` is:
# `weekly_reviews.metrics` is queryable, so "has this fired three weeks
# running" stays a query rather than a re-read of the prose.
INTEGRITY_CHECKS = (
    "missing_valuations",
    "missing_runs",
    "failed_runs",
    "stale_prices",
    "price_spike",
    "broker_sync",
)

# A held ticker whose newest bar is older than this has stopped being priced.
# Four days rather than two: a Friday close is three days old by Monday's run,
# and a public holiday makes it four.
STALE_PRICE_DAYS = 4

# A one-day move past this is far more likely to be a split restating the close
# under a quantity nobody adjusted than a real move. Deliberately well above
# anything the market plausibly does to a mega-cap — AMD moved 9% in a day this
# month and must not fire it.
SPIKE_PCT = 25

# Cash is NUMERIC(18,4) and every term is stored at that scale, so an exact
# match is reasonable to expect; a penny of tolerance keeps a rounding change
# from crying wolf.
CASH_TOLERANCE_USD = Decimal("0.01")


def week_integrity(conn: Any, pid: int, start: date, end: date) -> list[dict[str, Any]]:
    """Deterministic checks that the week's record is intact.

    Every one of these exists because the failure it catches is *invisible*
    rather than loud. The week of 2026-09-14 lost a whole day's
    `daily_performance` row and a whole day's agent run, and nothing said so:
    the chart simply drew a straight line across the gap and the next email
    reported a number that had moved for no stated reason.

    Computed here and rendered by us, never handed to the model as something
    to summarise — the same discipline as the daily email's subject line. A
    failure the model could smooth over in prose is not a check.

    Both ends of the window are inclusive. Returns one row per check, always
    all of them, so a passing week records the pass rather than an absence.

    **Assumes the window has closed.** The job runs at 22:00, an hour after
    the summary writes that evening's valuation, so on the schedule every day
    in the window has one. A manual run earlier in the day will therefore
    report today as a missing valuation — which is true rather than spurious:
    the week being reviewed has not finished yet. Use `--as-of` for a past
    week and the question does not arise.
    """
    window = {"pid": pid, "start": start, "end": end}

    def scalar(sql: str, params: dict[str, Any] | None = None) -> Any:
        found = conn.execute(sql, params or window).fetchone()
        return found[0] if found else None

    def listing(sql: str, params: dict[str, Any] | None = None) -> list[tuple]:
        return conn.execute(sql, params or window).fetchall()

    results: list[dict[str, Any]] = []

    def record(check: str, ok: bool, count: int, detail: str) -> None:
        results.append({"check": check, "ok": ok, "count": count, "detail": detail})

    # 1. A day with no valuation row. The summary job writes one every evening,
    #    including weekends, so any gap is a job that did not run.
    missing_days = listing(
        "SELECT d::date FROM generate_series(%(start)s::date, %(end)s::date, '1 day') d"
        " WHERE NOT EXISTS ("
        "   SELECT 1 FROM daily_performance p"
        "    WHERE p.portfolio_id = %(pid)s AND p.as_of = d::date)"
        " ORDER BY d"
    )
    record(
        "missing_valuations",
        not missing_days,
        len(missing_days),
        "every day has a stored valuation"
        if not missing_days
        else "no daily_performance row for "
        + ", ".join(str(d[0]) for d in missing_days)
        + " — the chart draws straight across the gap",
    )

    # 2. A day with no agent run at all. The cron is daily, weekends included,
    #    so there is no trading calendar to reason about here.
    no_run_days = listing(
        "SELECT d::date FROM generate_series(%(start)s::date, %(end)s::date, '1 day') d"
        " WHERE NOT EXISTS ("
        "   SELECT 1 FROM agent_runs r WHERE r.started_at::date = d::date)"
        " ORDER BY d"
    )
    record(
        "missing_runs",
        not no_run_days,
        len(no_run_days),
        "the agent ran every day"
        if not no_run_days
        else "no agent run on " + ", ".join(str(d[0]) for d in no_run_days),
    )

    # 3. A run that failed, or one still marked running long past the job
    #    timeout — SIGKILL cannot be caught, so that row was never closed.
    bad_runs = listing(
        "SELECT started_at::date, status,"
        "       (status = 'running'"
        "        AND started_at < now() - make_interval(secs => %(timeout)s)) AS stale"
        " FROM agent_runs"
        " WHERE started_at::date BETWEEN %(start)s AND %(end)s"
        "   AND (status = 'failed'"
        "        OR (status = 'running'"
        "            AND started_at < now() - make_interval(secs => %(timeout)s)))"
        " ORDER BY started_at",
        {**window, "timeout": JOB_TIMEOUT_SECONDS},
    )
    record(
        "failed_runs",
        not bad_runs,
        len(bad_runs),
        "no failed or abandoned runs"
        if not bad_runs
        else ", ".join(f"{row[0]} {'abandoned' if row[2] else row[1]}" for row in bad_runs),
    )

    # 4. A held ticker that has stopped being priced. This is the one that
    #    silently changes the reported total: build_state drops an unpriced
    #    holding from the valuation entirely rather than carrying it, so the
    #    total understates and then rebounds when the price returns, with no
    #    trade behind either move.
    stale = listing(
        "SELECT p.ticker, max(pr.bar_date) AS newest"
        " FROM positions p LEFT JOIN prices pr ON pr.ticker = p.ticker"
        " WHERE p.portfolio_id = %(pid)s"
        " GROUP BY p.ticker"
        " HAVING max(pr.bar_date) IS NULL"
        "     OR max(pr.bar_date) < %(end)s::date - %(days)s",
        {**window, "days": STALE_PRICE_DAYS},
    )
    record(
        "stale_prices",
        not stale,
        len(stale),
        "every holding has a recent close"
        if not stale
        else ", ".join(f"{t} (newest bar {n or 'none'})" for t, n in stale)
        + " — an unpriced holding drops out of the valuation entirely",
    )

    # 5. A one-day close move large enough to suggest a corporate action. Bars
    #    are fetched with adjustment=all, which restates the price history
    #    across a split while `positions.quantity` stays exactly as it was — so
    #    a split moves the reported value with no trade behind it.
    spikes = listing(
        "SELECT ticker, bar_date, pct FROM ("
        "  SELECT pr.ticker, pr.bar_date,"
        "         round((pr.close_usd / NULLIF("
        "           lag(pr.close_usd) OVER (PARTITION BY pr.ticker ORDER BY pr.bar_date), 0)"
        "           - 1) * 100, 1) AS pct"
        "    FROM prices pr"
        "   WHERE pr.ticker IN (SELECT ticker FROM positions WHERE portfolio_id = %(pid)s)"
        ") m"
        " WHERE m.bar_date BETWEEN %(start)s AND %(end)s AND abs(m.pct) > %(pct)s"
        " ORDER BY abs(m.pct) DESC",
        {**window, "pct": SPIKE_PCT},
    )
    record(
        "price_spike",
        not spikes,
        len(spikes),
        f"no held ticker moved more than {SPIKE_PCT}% in a day"
        if not spikes
        else ", ".join(f"{t} {p}% on {d}" for t, d, p in spikes)
        + " — check for a split, which restates closes but not our quantity",
    )

    # Broker account state is authoritative, including activity outside this agent.
    age = scalar(
        "SELECT extract(epoch FROM (now()-broker_synced_at)) FROM portfolio WHERE id=%(pid)s"
    )
    record(
        "broker_sync",
        age is not None and age < 86400,
        0 if age is not None and age < 86400 else 1,
        "broker snapshot is less than a day old"
        if age is not None and age < 86400
        else "broker snapshot missing or older than a day",
    )

    return results


def save_weekly_review(
    conn: Any,
    period_start: date,
    period_end: date,
    subject: str,
    assessment: str,
    body_markdown: str,
    body_html: str,
    metrics: dict[str, Any],
    recommendations: list[dict[str, Any]],
    model: str | None,
    prompt_version: str | None,
    email_status: str,
    provider_id: str | None,
    error: str | None,
    cost_usd: Decimal | None = None,
) -> int:
    row = conn.execute(
        "INSERT INTO weekly_reviews (period_start, period_end, subject, assessment,"
        "   body_markdown, body_html, metrics, recommendations, model, prompt_version,"
        "   email_status, email_provider_id, email_error, cost_usd, sent_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
        "         CASE WHEN %s = 'sent' THEN now() ELSE NULL END)"
        # Re-running a week overwrites its review, so a retry after a mail
        # outage is safe — the same property the daily summary has.
        " ON CONFLICT (period_end) DO UPDATE SET"
        "   period_start = EXCLUDED.period_start, subject = EXCLUDED.subject,"
        "   assessment = EXCLUDED.assessment,"
        "   body_markdown = EXCLUDED.body_markdown, body_html = EXCLUDED.body_html,"
        "   metrics = EXCLUDED.metrics, recommendations = EXCLUDED.recommendations,"
        "   model = EXCLUDED.model, prompt_version = EXCLUDED.prompt_version,"
        "   email_status = EXCLUDED.email_status,"
        "   email_provider_id = EXCLUDED.email_provider_id,"
        # cost_usd is overwritten too: a re-run makes a fresh model call, so
        # keeping the first run's figure would under-report what the week cost.
        "   email_error = EXCLUDED.email_error, cost_usd = EXCLUDED.cost_usd,"
        "   sent_at = EXCLUDED.sent_at"
        " RETURNING id",
        (
            period_start,
            period_end,
            subject,
            assessment,
            body_markdown,
            body_html,
            Jsonb(_jsonable(metrics)),
            Jsonb(_jsonable(recommendations)),
            model,
            prompt_version,
            email_status,
            provider_id,
            error,
            cost_usd,
            email_status,
        ),
    ).fetchone()
    return int(row[0])


def last_recommendations(conn: Any, before: date) -> list[dict[str, Any]]:
    """The previous review's proposals, for the next review's prompt.

    Empty rather than absent when there is no earlier review, so the first week
    needs no special case at the call site.
    """
    row = conn.execute(
        "SELECT recommendations FROM weekly_reviews WHERE period_end < %s"
        " ORDER BY period_end DESC LIMIT 1",
        (before,),
    ).fetchone()
    return list(row[0]) if row and row[0] else []


def save_broker_snapshot(conn: Any, pid: int, snapshot: Any) -> None:
    """Atomically replace the account mirror, retaining its original equity baseline."""
    account = snapshot.account
    existing = conn.execute(
        "SELECT broker_account_id FROM portfolio WHERE id=%s FOR UPDATE", (pid,)
    ).fetchone()
    if existing is None:
        raise LookupError("Portfolio does not exist")
    if existing[0] is not None and existing[0] != account.id:
        raise RuntimeError("Alpaca account changed; start a separate experiment")
    conn.execute(
        "UPDATE portfolio SET cash_usd=%s,equity_usd=%s,buying_power_usd=%s,"
        "initial_cash_usd=CASE WHEN broker_account_id IS NULL THEN %s "
        "ELSE initial_cash_usd END,"
        "created_at=CASE WHEN broker_account_id IS NULL THEN now() "
        "ELSE created_at END,"
        "broker_account_id=%s,broker_synced_at=now(),updated_at=now(),"
        "base_currency='USD' WHERE id=%s",
        (
            account.cash_usd,
            account.equity_usd,
            account.buying_power_usd,
            account.equity_usd,
            account.id,
            pid,
        ),
    )
    tickers = []
    for position in snapshot.positions:
        tickers.append(position.ticker)
        # A manual position is mirrored without silently expanding the trading allowlist.
        conn.execute(
            "INSERT INTO companies(ticker,name,is_active) VALUES (%s,%s,false) "
            "ON CONFLICT(ticker) DO NOTHING",
            (position.ticker, position.ticker),
        )
        conn.execute(
            "INSERT INTO positions(portfolio_id,ticker,quantity,avg_cost_usd,market_value_usd) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT(portfolio_id,ticker) DO UPDATE SET "
            "quantity=EXCLUDED.quantity,avg_cost_usd=EXCLUDED.avg_cost_usd,"
            "market_value_usd=EXCLUDED.market_value_usd,updated_at=now()",
            (
                pid,
                position.ticker,
                position.quantity,
                position.avg_entry_price_usd,
                position.market_value_usd,
            ),
        )
    conn.execute(
        "DELETE FROM positions WHERE portfolio_id=%s AND NOT(ticker=ANY(%s))", (pid, tickers)
    )
