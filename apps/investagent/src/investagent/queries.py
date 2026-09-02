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
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from psycopg.rows import dict_row

DEFAULT_PORTFOLIO = "default"


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


def overview(conn: Any) -> dict[str, Any]:
    """The headline numbers: what the £500 is now worth, and how the run went."""
    portfolio = _row(
        conn,
        "SELECT id, name, base_currency, initial_cash_gbp, cash_gbp, updated_at"
        " FROM portfolio WHERE name = %s",
        (DEFAULT_PORTFOLIO,),
    )
    if portfolio is None:
        return {"portfolio": None}

    # Positions are valued at the latest close, joined per ticker with a lateral
    # so a ticker with no price contributes nothing rather than dropping the row.
    #
    # The rate is the most recently *recorded* one, not max() — which was the
    # first version and is wrong in a way that worsens: max() locks onto the
    # highest rate ever seen and never moves again. The valuation is therefore
    # only as fresh as the last rate we stored, which is consistent, since the
    # prices it multiplies are daily closes rather than live quotes.
    valuation = _row(
        conn,
        "WITH fx AS ("
        "  SELECT rate FROM ("
        "    SELECT fx_rate_gbp_usd AS rate, as_of::timestamptz AS at"
        "      FROM daily_performance WHERE fx_rate_gbp_usd IS NOT NULL"
        "    UNION ALL"
        "    SELECT fx_rate_gbp_usd AS rate, created_at AS at FROM trades"
        "  ) r ORDER BY at DESC LIMIT 1"
        ")"
        " SELECT coalesce(sum(p.quantity * lc.close_usd"
        "                     / coalesce((SELECT rate FROM fx), 1)), 0) AS positions_value_gbp,"
        "        count(*) AS position_count"
        " FROM positions p"
        " LEFT JOIN LATERAL ("
        "   SELECT close_usd FROM prices WHERE ticker = p.ticker"
        "   ORDER BY bar_date DESC LIMIT 1"
        " ) lc ON true"
        " WHERE p.portfolio_id = %s",
        (portfolio["id"],),
    )

    cash = portfolio["cash_gbp"]
    invested = valuation["positions_value_gbp"] if valuation else 0.0
    total = cash + invested
    initial = portfolio["initial_cash_gbp"]

    last_run = _row(
        conn,
        "SELECT id, started_at, finished_at, status, dry_run, decisions_made,"
        "       trades_executed, cost_usd"
        " FROM agent_runs ORDER BY started_at DESC LIMIT 1",
    )

    return {
        "portfolio": portfolio,
        "cash_gbp": cash,
        "positions_value_gbp": invested,
        "total_value_gbp": total,
        "position_count": valuation["position_count"] if valuation else 0,
        "pnl_gbp": total - initial,
        # Percent, matching daily_performance.pnl_pct: 12.34 is +12.34%.
        "pnl_pct": ((total - initial) / initial * 100) if initial else 0.0,
        "last_run": last_run,
    }


def performance(conn: Any, days: int = 180) -> dict[str, Any]:
    """The portfolio series and the benchmark series, for the comparison chart."""
    return {
        "portfolio": _rows(
            conn,
            "SELECT as_of, total_value_gbp, cash_gbp, positions_value_gbp, pnl_gbp, pnl_pct"
            " FROM daily_performance WHERE as_of > current_date - %s::int"
            " ORDER BY as_of",
            (days,),
        ),
        "benchmarks": _rows(
            conn,
            "SELECT symbol, as_of, value_gbp, close_usd FROM benchmarks"
            " WHERE as_of > current_date - %s::int ORDER BY symbol, as_of",
            (days,),
        ),
    }


def holdings(conn: Any) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT p.ticker, c.name, c.sector, p.quantity, p.avg_cost_usd, p.avg_cost_gbp,"
        "       lc.close_usd AS last_close_usd, lc.bar_date AS last_close_date,"
        "       p.opened_at, p.updated_at"
        " FROM positions p"
        " JOIN companies c ON c.ticker = p.ticker"
        " LEFT JOIN LATERAL ("
        "   SELECT close_usd, bar_date FROM prices WHERE ticker = p.ticker"
        "   ORDER BY bar_date DESC LIMIT 1"
        " ) lc ON true"
        " ORDER BY p.ticker",
    )


def decisions(conn: Any, limit: int = 50, ticker: str | None = None) -> list[dict[str, Any]]:
    where = "WHERE d.ticker = %s" if ticker else ""
    params = (ticker, limit) if ticker else (limit,)
    return _rows(
        conn,
        "SELECT d.id, d.run_id, d.decided_at, d.ticker, d.action, d.confidence,"
        "       d.recommended_amount_gbp, d.approved_amount_gbp, d.model,"
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
        "SELECT id, ticker, side, status, dry_run, quantity, price_usd, notional_gbp,"
        "       notional_usd, fx_rate_gbp_usd, created_at, filled_at"
        " FROM trades WHERE decision_id = %s ORDER BY created_at",
        (decision_id,),
    )
    return found


def news(conn: Any, limit: int = 50, relevant_only: bool = False) -> list[dict[str, Any]]:
    """Recent articles with whatever the filter concluded about them.

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


def trades(conn: Any, limit: int = 50) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT id, decision_id, ticker, side, status, dry_run, quantity, price_usd,"
        "       notional_usd, notional_gbp, fx_rate_gbp_usd, fx_rate_as_of,"
        "       broker_order_id, created_at, submitted_at, filled_at"
        " FROM trades ORDER BY created_at DESC LIMIT %s",
        (limit,),
    )


def runs(conn: Any, limit: int = 30) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT id, started_at, finished_at, status, trigger, dry_run, image_tag,"
        "       tickers_considered, news_fetched, news_relevant, decisions_made,"
        "       trades_executed, input_tokens, output_tokens, cost_usd, error"
        " FROM agent_runs ORDER BY started_at DESC LIMIT %s",
        (limit,),
    )


def latest_summary(conn: Any) -> dict[str, Any] | None:
    return _row(
        conn,
        "SELECT id, as_of, subject, body_markdown, body_html, model, email_status,"
        "       sent_at, created_at"
        " FROM daily_summaries ORDER BY as_of DESC LIMIT 1",
    )
