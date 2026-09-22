"""Read queries for the API.

Separate from `repository.py`, which the agent job uses to *write*. The split is
not ceremony: these return plain JSON-ready dicts for HTTP, whereas the job's
functions return domain objects and expect to share a transaction.

**Money comes back as `float`.** Everywhere else in this system money is a
`Decimal` and that rule is load-bearing — but this is the display boundary. The
values here are read by a browser, charted, and thrown away; nothing computes
with them and no result of that computation goes back into the database. A
`Decimal` would serialise as a JSON string and make every chart library and
`toFixed` call awkward for no gain in a figure that is only ever looked at.

**Most functions here take a required `portfolio` argument, with no default.**
There are two accounts ('static-100' and 'dynamic-500') and no sense in which
either is "the" portfolio, so a forgotten argument must fail loudly rather than
silently show one account's data under the other's request. `latest_summary`,
`latest_review` and `news` are the exceptions: the daily summary and weekly
review are combined reports covering both accounts already, and news is
ticker-level reference data with no account dimension at all.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from psycopg.rows import dict_row

from .repository import JOB_TIMEOUT_SECONDS, portfolio_id

ACCOUNTS = ("static-100", "dynamic-500")


def _rows(conn: Any, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        return [_plain(r) for r in cur.execute(sql, params).fetchall()]


def _row(conn: Any, sql: str, params: tuple = ()) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        found = cur.execute(sql, params).fetchone()
    return _plain(found) if found else None


def _plain(row: dict[str, Any]) -> dict[str, Any]:
    """Convert Decimals for JSON. See the module docstring on why float is right here."""
    return {k: (float(v) if isinstance(v, Decimal) else v) for k, v in row.items()}


def overview(conn: Any, portfolio: str) -> dict[str, Any]:
    """The headline numbers: what one account is now worth, and how its run went."""
    row = _row(
        conn,
        "SELECT id, name, base_currency, initial_cash_usd, cash_usd, equity_usd, "
        "buying_power_usd, broker_synced_at, display_gbp_usd, display_fx_as_of, updated_at"
        " FROM portfolio WHERE name = %s",
        (portfolio,),
    )
    if row is None:
        return {"portfolio": None}

    valuation = _row(
        conn,
        "SELECT coalesce(sum(coalesce(p.market_value_usd,p.quantity*lc.close_usd)),0) "
        "AS positions_value_usd,count(*) AS position_count FROM positions p "
        "LEFT JOIN LATERAL (SELECT close_usd FROM prices WHERE ticker=p.ticker "
        "ORDER BY bar_date DESC LIMIT 1) lc ON true WHERE p.portfolio_id=%s",
        (row["id"],),
    )

    cash = row["cash_usd"]
    invested = valuation["positions_value_usd"] if valuation else 0.0
    total = row["equity_usd"] if row["equity_usd"] is not None else cash + invested
    initial = row["initial_cash_usd"]

    last_run = _row(
        conn,
        "SELECT id, started_at, finished_at, status, trigger, dry_run, decisions_made,"
        "       trades_executed, cost_usd"
        " FROM agent_runs WHERE portfolio_id = %s ORDER BY started_at DESC LIMIT 1",
        (row["id"],),
    )

    return {
        "portfolio": row,
        "broker_synced_at": row["broker_synced_at"],
        "buying_power_usd": row["buying_power_usd"],
        "display_gbp_usd": row["display_gbp_usd"],
        "display_fx_as_of": row["display_fx_as_of"],
        "cash_usd": cash,
        "positions_value_usd": invested,
        "total_value_usd": total,
        "position_count": valuation["position_count"] if valuation else 0,
        "pnl_usd": total - initial,
        # Percent, matching daily_performance.pnl_pct: 12.34 is +12.34%.
        "pnl_pct": ((total - initial) / initial * 100) if initial else 0.0,
        "last_run": last_run,
    }


def performance(conn: Any, portfolio: str, days: int = 180) -> dict[str, Any]:
    """One account's series and the benchmark series, for the comparison chart."""
    pid = portfolio_id(conn, portfolio)
    return {
        "portfolio": _rows(
            conn,
            "SELECT as_of, total_value_usd, cash_usd, positions_value_usd, pnl_usd, pnl_pct"
            " FROM daily_performance WHERE portfolio_id = %s"
            "   AND as_of > current_date - %s::int"
            " ORDER BY as_of",
            (pid, days),
        ),
        "benchmarks": _rows(
            conn,
            "SELECT symbol, as_of, value_usd, close_usd FROM benchmarks"
            " WHERE portfolio_id = %s AND as_of > current_date - %s::int"
            " ORDER BY symbol, as_of",
            (pid, days),
        ),
    }


def comparison(conn: Any, days: int = 180) -> dict[str, Any]:
    """Both accounts' valuation series in one payload, for the Compare view.

    Genuinely new shape rather than two calls to `performance`: the dashboard
    wants both series keyed by account name in one response to plot on one
    chart, not two separate documents it has to merge itself.
    """
    return {
        name: _rows(
            conn,
            "SELECT dp.as_of, dp.total_value_usd, dp.pnl_usd, dp.pnl_pct"
            " FROM daily_performance dp JOIN portfolio p ON p.id = dp.portfolio_id"
            " WHERE p.name = %s AND dp.as_of > current_date - %s::int"
            " ORDER BY dp.as_of",
            (name, days),
        )
        for name in ACCOUNTS
    }


def holdings(conn: Any, portfolio: str) -> list[dict[str, Any]]:
    pid = portfolio_id(conn, portfolio)
    return _rows(
        conn,
        "SELECT p.ticker, c.name, c.sector, p.quantity, p.avg_cost_usd, p.market_value_usd,"
        "       lc.close_usd AS last_close_usd, lc.bar_date AS last_close_date,"
        "       p.opened_at, p.updated_at"
        " FROM positions p"
        " JOIN companies c ON c.ticker = p.ticker"
        " LEFT JOIN LATERAL ("
        "   SELECT close_usd, bar_date FROM prices WHERE ticker = p.ticker"
        "   ORDER BY bar_date DESC LIMIT 1"
        " ) lc ON true"
        " WHERE p.portfolio_id = %s"
        " ORDER BY p.ticker",
        (pid,),
    )


def price_history(conn: Any, portfolio: str, days: int = 90) -> list[dict[str, Any]]:
    """Daily closes per held ticker, behind the per-holding trend panels.

    Held tickers only: a watchlist name nobody owns has no panel to draw. Flat
    rather than grouped, matching how `performance` returns its benchmark arms
    — the client groups both the same way.

    History reaches back only as far as the job has ever fetched, so an early
    window is short rather than empty, and the panels say so.
    """
    pid = portfolio_id(conn, portfolio)
    return _rows(
        conn,
        "SELECT ticker, bar_date, close_usd FROM prices"
        " WHERE ticker IN (SELECT ticker FROM positions WHERE portfolio_id = %s)"
        "   AND bar_date > current_date - %s::int"
        " ORDER BY ticker, bar_date",
        (pid, days),
    )


def decisions(
    conn: Any, portfolio: str, limit: int = 50, ticker: str | None = None
) -> list[dict[str, Any]]:
    pid = portfolio_id(conn, portfolio)
    where = "WHERE d.portfolio_id = %s AND d.ticker = %s" if ticker else "WHERE d.portfolio_id = %s"
    params = (pid, ticker, limit) if ticker else (pid, limit)
    return _rows(
        conn,
        "SELECT d.id, d.run_id, d.decided_at, d.ticker, d.action, d.confidence,"
        "       d.recommended_amount_usd, d.approved_amount_usd, d.model,"
        "       d.risk_verdict->>'binding_constraint' AS binding_constraint,"
        "       (d.risk_verdict->>'approved')::boolean AS approved,"
        "       cardinality(d.news_ids) AS news_count"
        f" FROM ai_decisions d {where}"
        " ORDER BY d.decided_at DESC LIMIT %s",
        params,
    )


def decision(conn: Any, decision_id: int) -> dict[str, Any] | None:
    """One decision in full, with the articles the model was actually shown.

    This is the endpoint the audit trail exists for — everything needed to
    replay a call against what the AI saw and what the risk engine did to it.
    Not portfolio-scoped: a decision id is already globally unique, and the
    caller reached this from a list that was itself scoped.
    """
    found = _row(
        conn,
        "SELECT * FROM ai_decisions WHERE id = %s",
        (decision_id,),
    )
    if found is None:
        return None

    found["news"] = _rows(
        conn,
        "SELECT id, headline, summary, url, source, published_at, tickers"
        " FROM news WHERE id = ANY(%s) ORDER BY published_at DESC",
        (found.get("news_ids") or [],),
    )
    found["trades"] = _rows(
        conn,
        "SELECT id, ticker, side, status, dry_run, quantity, price_usd, notional_usd,"
        "       created_at, filled_at"
        " FROM trades WHERE decision_id = %s ORDER BY created_at",
        (decision_id,),
    )
    return found


def news(conn: Any, limit: int = 50, relevant_only: bool = False) -> list[dict[str, Any]]:
    """Recent articles with whatever the filter concluded about them.

    Not portfolio-scoped: news is ticker-level reference data fetched once and
    shared by whichever account's watchlist mentions the ticker.

    An article can have several analysis rows — one per ticker — so they are
    aggregated rather than joined, which would multiply the article out.
    """
    having = "HAVING bool_or(a.relevant)" if relevant_only else ""
    return _rows(
        conn,
        "SELECT n.id, n.headline, n.summary, n.url, n.source, n.published_at, n.tickers,"
        "       bool_or(a.relevant) AS any_relevant,"
        "       avg(a.sentiment_score) AS avg_sentiment,"
        "       count(a.id) AS analysis_count"
        " FROM news n LEFT JOIN news_analysis a ON a.news_id = n.id"
        " GROUP BY n.id"
        f" {having}"
        " ORDER BY n.published_at DESC LIMIT %s",
        (limit,),
    )


def trades(conn: Any, portfolio: str, limit: int = 50) -> list[dict[str, Any]]:
    pid = portfolio_id(conn, portfolio)
    return _rows(
        conn,
        "SELECT id, decision_id, ticker, side, status, dry_run, quantity, price_usd,"
        "       notional_usd,"
        "       broker_order_id, created_at, submitted_at, filled_at"
        " FROM trades WHERE portfolio_id = %s ORDER BY created_at DESC LIMIT %s",
        (pid, limit),
    )


def runs(conn: Any, portfolio: str, limit: int = 30) -> list[dict[str, Any]]:
    """Recent runs for one account, with a computed `stale` flag.

    The job closes its own row on failure, including on SIGTERM — but SIGKILL
    cannot be caught, so a hard kill still leaves `running` behind for ever.
    Rather than have the dashboard show a run in progress indefinitely, anything
    older than the job timeout is reported as stale. Computed rather than
    written back, because a read should not mutate and because the truth is
    derivable from the timestamp.
    """
    pid = portfolio_id(conn, portfolio)
    return _rows(
        conn,
        "SELECT id, started_at, finished_at, status, trigger, dry_run, image_tag,"
        "       tickers_considered, news_fetched, news_relevant, decisions_made,"
        "       trades_executed, input_tokens, output_tokens, cost_usd, error,"
        "       (status = 'running'"
        "        AND started_at < now() - make_interval(secs => %s)) AS stale"
        " FROM agent_runs WHERE portfolio_id = %s ORDER BY started_at DESC LIMIT %s",
        (JOB_TIMEOUT_SECONDS, pid, limit),
    )


def latest_summary(conn: Any) -> dict[str, Any] | None:
    """The most recent daily summary — one combined email covering both accounts."""
    return _row(
        conn,
        "SELECT id, as_of, subject, body_markdown, body_html, model, email_status,"
        "       sent_at, created_at"
        " FROM daily_summaries ORDER BY as_of DESC LIMIT 1",
    )


def latest_review(conn: Any) -> dict[str, Any] | None:
    """The most recent weekly review, for the dashboard.

    One combined review covering both accounts. Returns the model's prose and
    its structured proposals, not `body_html`. That column is Markdown-rendered
    model output and Python-Markdown passes raw HTML straight through, so
    serving it to a browser that would inject it into the DOM hands a model an
    XSS vector for no gain — the dashboard already renders the week's figures
    itself.
    """
    return _row(
        conn,
        "SELECT id, period_start, period_end, subject, assessment, recommendations,"
        "       model, email_status, sent_at, created_at"
        " FROM weekly_reviews ORDER BY period_end DESC LIMIT 1",
    )
