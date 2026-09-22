"""queries.py against a real Postgres.

The API's read layer, and the other half of the SQL that nothing executed until
these tests existed. `tests/test_api.py` covers what the HTTP layer does with a
request — status codes, parameter bounds, the bearer gate — against a fake
connection, and says in its own docstring that the SQL is exercised by running
the real thing. This is that.

Two properties here are worth more than the coverage: the FX rate the overview
values positions with, which was wrong in a way that worsened over time, and
`latest_review` withholding `body_html`.

Skipped when POSTGRES_TEST_DSN is unset; `make test-db` sets it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from marketagent import queries
from marketagent import repository as repo
from marketagent.models import PortfolioState, Recommendation, RiskReason, RiskVerdict

D = Decimal

TICKER = "AAPL"
OTHER = "MSFT"
TODAY = date.today()

# `queries.py` takes an account *name*, not a pid, and resolves the pid itself.
# static-100 is seeded with real watchlist data by 010-seed-sp100-static.sql;
# nothing here depends on which of the two accounts is used, so one constant
# keeps every call site consistent.
PORTFOLIO = "static-100"


def _decision(
    conn, pid: int, ticker: str = TICKER, action: str = "BUY", approved: bool = True
) -> int:
    run_id = repo.open_run(conn, pid, "schedule", dry_run=False, image_tag=None)
    return repo.save_decision(
        conn,
        pid,
        run_id=run_id,
        rec=Recommendation(
            ticker=ticker,
            action=action,
            confidence=0.78,
            suggested_amount_usd=40.0,
            reasoning="Because of the evidence.",
            risks="It could go down.",
        ),
        verdict=RiskVerdict(
            approved=approved,
            approved_amount_usd=D("30.0000") if approved else None,
            reasons=(RiskReason(constraint="max_trade_usd", detail="capped"),),
            binding_constraint="max_trade_usd",
        ),
        state=PortfolioState(cash_usd=D("500.0000")),
        model="claude-sonnet-5",
        prompt_version="v1",
        news_ids=[],
        input_tokens=100,
        output_tokens=20,
    )


def _trade(conn, pid: int, decision_id: int, fx: str = "1.250000", **kwargs) -> int:
    defaults = dict(
        ticker=TICKER,
        side="BUY",
        notional_usd=D("30.0000"),
        status="submitted",
        dry_run=False,
        client_order_id=f"order-{decision_id}",
        broker_order_id=None,
        quantity=None,
        price_usd=None,
        submitted_at=datetime.now(UTC),
        filled_at=None,
    )
    return repo.save_trade(conn, pid, decision_id, **{**defaults, **kwargs})


def _price(conn, ticker: str = TICKER, close: str = "100.0000", day: date = TODAY) -> None:
    from marketagent.marketdata import Bar

    repo.save_prices(
        conn,
        [
            Bar(
                ticker=ticker,
                bar_date=day,
                open_usd=D(close),
                high_usd=D(close),
                low_usd=D(close),
                close_usd=D(close),
                volume=1_000,
            )
        ],
    )


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


def test_the_overview_of_an_untouched_portfolio_is_the_notional_and_no_pnl(conn):
    result = queries.overview(conn, PORTFOLIO)

    assert result["cash_usd"] == 100000.0
    assert result["position_count"] == 0
    assert result["pnl_usd"] == 0.0
    assert result["last_run"] is None


def test_the_overview_values_positions_at_the_latest_close(conn):
    pid = repo.portfolio_id(conn, PORTFOLIO)
    repo.apply_fill(
        conn,
        pid,
        TICKER,
        "BUY",
        D("2.000000"),
        D("50.0000"),
        D("100.0000"),
    )
    _price(conn, close="100.0000")
    _trade(conn, pid, _decision(conn, pid), fx="1.250000")

    result = queries.overview(conn, PORTFOLIO)

    # 2 shares x $100; no FX conversion
    assert result["positions_value_usd"] == 200.0
    assert result["position_count"] == 1
    assert result["total_value_usd"] == 100150.0


def test_broker_valuation_takes_precedence_over_local_prices(conn):
    pid = repo.portfolio_id(conn, PORTFOLIO)
    repo.apply_fill(conn, pid, TICKER, "BUY", D("2"), D("200"), D("100"))
    _price(conn, close="90")
    conn.execute("UPDATE positions SET market_value_usd=220 WHERE portfolio_id=%s", (pid,))
    conn.execute("UPDATE portfolio SET equity_usd=100020 WHERE id=%s", (pid,))
    result = queries.overview(conn, PORTFOLIO)
    assert result["positions_value_usd"] == 220
    assert result["total_value_usd"] == 100020


def test_a_position_with_no_price_contributes_nothing_but_still_counts(conn):
    """The lateral join is a LEFT one so an unpriced ticker does not drop the
    row entirely."""
    pid = repo.portfolio_id(conn, PORTFOLIO)
    repo.apply_fill(
        conn,
        pid,
        TICKER,
        "BUY",
        D("2.000000"),
        D("50.0000"),
        D("100.0000"),
    )

    result = queries.overview(conn, PORTFOLIO)

    assert result["position_count"] == 1
    assert result["positions_value_usd"] == 0.0


def test_the_overview_carries_the_most_recent_run(conn):
    pid = repo.portfolio_id(conn, PORTFOLIO)
    run_id = repo.open_run(conn, pid, "schedule", dry_run=False, image_tag="8d2788f")
    repo.close_run(conn, run_id, "succeeded", {"trades_executed": 3}, 100, 20, D("0.1900"))

    result = queries.overview(conn, PORTFOLIO)

    assert result["last_run"]["status"] == "succeeded"
    assert result["last_run"]["trades_executed"] == 3


def test_money_crosses_the_display_boundary_as_float_not_decimal(conn):
    """The one place the Decimal rule is relaxed: a browser charts these and
    throws them away, and a Decimal would serialise as a JSON string."""
    result = queries.overview(conn, PORTFOLIO)

    assert isinstance(result["cash_usd"], float)
    assert not isinstance(result["cash_usd"], Decimal)


def test_overview_of_an_unknown_portfolio_is_none(conn):
    result = queries.overview(conn, "no-such-portfolio")

    assert result == {"portfolio": None}


# ---------------------------------------------------------------------------
# Series
# ---------------------------------------------------------------------------


def test_the_performance_series_carries_the_portfolio_and_the_benchmarks(conn):
    from marketagent.benchmarks import BenchmarkPoint

    pid = repo.portfolio_id(conn, PORTFOLIO)
    repo.save_daily_performance(
        conn,
        pid,
        TODAY,
        D("450.0000"),
        D("60.0000"),
        D("510.0000"),
        D("10.0000"),
        D("2.0000"),
    )
    repo.save_benchmarks(
        conn, pid, [BenchmarkPoint(symbol="SPY", as_of=TODAY, value_usd=D("505.0000"))]
    )

    result = queries.performance(conn, PORTFOLIO, days=30)

    assert [r["total_value_usd"] for r in result["portfolio"]] == [510.0]
    assert [r["symbol"] for r in result["benchmarks"]] == ["SPY"]


def test_the_performance_window_excludes_anything_older(conn):
    pid = repo.portfolio_id(conn, PORTFOLIO)
    repo.save_daily_performance(
        conn,
        pid,
        TODAY - timedelta(days=90),
        D("500.0000"),
        D("0.0000"),
        D("500.0000"),
        D("0.0000"),
        D("0.0000"),
    )

    assert queries.performance(conn, PORTFOLIO, days=7)["portfolio"] == []


def test_holdings_carry_the_company_name_and_the_last_close(conn):
    pid = repo.portfolio_id(conn, PORTFOLIO)
    repo.apply_fill(
        conn,
        pid,
        TICKER,
        "BUY",
        D("2.000000"),
        D("50.0000"),
        D("100.0000"),
    )
    _price(conn, close="100.0000")

    rows = queries.holdings(conn, PORTFOLIO)

    assert rows[0]["ticker"] == TICKER
    assert rows[0]["name"]
    assert rows[0]["last_close_usd"] == 100.0


def test_the_price_history_covers_held_tickers_only(conn):
    """A watchlist name nobody owns has no trend panel, so it has no series.

    The dashboard draws one panel per holding and looks its series up by
    ticker; a price for something unheld is weight on every response for a
    panel that is never rendered.
    """
    pid = repo.portfolio_id(conn, PORTFOLIO)
    repo.apply_fill(
        conn,
        pid,
        TICKER,
        "BUY",
        D("2.000000"),
        D("50.0000"),
        D("100.0000"),
    )
    _price(conn, ticker=TICKER, close="100.0000", day=TODAY - timedelta(days=1))
    _price(conn, ticker=TICKER, close="104.0000", day=TODAY)
    _price(conn, ticker=OTHER, close="400.0000", day=TODAY)

    rows = queries.price_history(conn, PORTFOLIO, days=30)

    assert [r["ticker"] for r in rows] == [TICKER, TICKER]
    # Ascending by date, because the client plots them in the order given.
    assert [r["close_usd"] for r in rows] == [100.0, 104.0]


def test_the_price_history_window_excludes_older_bars(conn):
    pid = repo.portfolio_id(conn, PORTFOLIO)
    repo.apply_fill(
        conn,
        pid,
        TICKER,
        "BUY",
        D("2.000000"),
        D("50.0000"),
        D("100.0000"),
    )
    _price(conn, ticker=TICKER, close="90.0000", day=TODAY - timedelta(days=40))
    _price(conn, ticker=TICKER, close="110.0000", day=TODAY)

    rows = queries.price_history(conn, PORTFOLIO, days=7)

    assert [r["close_usd"] for r in rows] == [110.0]


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def test_comparison_returns_both_accounts_keyed_by_name(conn):
    """Genuinely new shape: both accounts' series in one payload, keyed by
    account name, rather than two documents the dashboard has to merge itself."""
    static_pid = repo.portfolio_id(conn, "static-100")
    dynamic_pid = repo.portfolio_id(conn, "dynamic-500")
    repo.save_daily_performance(
        conn,
        static_pid,
        TODAY,
        D("450.0000"),
        D("60.0000"),
        D("510.0000"),
        D("10.0000"),
        D("2.0000"),
    )
    repo.save_daily_performance(
        conn,
        dynamic_pid,
        TODAY,
        D("400.0000"),
        D("120.0000"),
        D("520.0000"),
        D("20.0000"),
        D("4.0000"),
    )

    result = queries.comparison(conn, days=30)

    assert set(result) == {"static-100", "dynamic-500"}
    assert [r["total_value_usd"] for r in result["static-100"]] == [510.0]
    assert [r["total_value_usd"] for r in result["dynamic-500"]] == [520.0]


# ---------------------------------------------------------------------------
# Decisions, news, trades, runs
# ---------------------------------------------------------------------------


def test_decisions_are_newest_first_and_report_the_engines_verdict(conn):
    pid = repo.portfolio_id(conn, PORTFOLIO)
    _decision(conn, pid, approved=False)

    rows = queries.decisions(conn, PORTFOLIO, limit=10)

    assert rows[0]["approved"] is False
    assert rows[0]["binding_constraint"] == "max_trade_usd"
    assert rows[0]["news_count"] == 0


def test_decisions_can_be_filtered_to_one_ticker(conn):
    pid = repo.portfolio_id(conn, PORTFOLIO)
    _decision(conn, pid, ticker=TICKER)
    _decision(conn, pid, ticker=OTHER)

    rows = queries.decisions(conn, PORTFOLIO, limit=10, ticker=OTHER)

    assert [r["ticker"] for r in rows] == [OTHER]


def test_the_decision_limit_is_honoured(conn):
    """Every limit is bounded — an unbounded one is how a read-only API becomes
    a denial of service."""
    pid = repo.portfolio_id(conn, PORTFOLIO)
    for _ in range(3):
        _decision(conn, pid)

    assert len(queries.decisions(conn, PORTFOLIO, limit=2)) == 2


def test_one_decision_comes_back_with_the_articles_and_trades_behind_it(conn):
    """The endpoint the audit trail exists for."""
    from marketagent.news import Article

    pid = repo.portfolio_id(conn, PORTFOLIO)
    news_id = repo.save_news(
        conn,
        [
            Article(
                external_id="art-1",
                headline="A headline",
                published_at=datetime(2026, 9, 14, 12, tzinfo=UTC),
                tickers=(TICKER,),
            )
        ],
    )["art-1"]
    run_id = repo.open_run(conn, pid, "schedule", dry_run=False, image_tag=None)
    did = repo.save_decision(
        conn,
        pid,
        run_id=run_id,
        rec=Recommendation(
            ticker=TICKER,
            action="BUY",
            confidence=0.8,
            suggested_amount_usd=40.0,
            reasoning="r",
            risks="k",
        ),
        verdict=RiskVerdict(
            approved=True,
            approved_amount_usd=D("30.0000"),
            reasons=(RiskReason(constraint="max_trade_usd", detail="capped"),),
            binding_constraint="max_trade_usd",
        ),
        state=PortfolioState(cash_usd=D("500.0000")),
        model="claude-sonnet-5",
        prompt_version="v1",
        news_ids=[news_id],
        input_tokens=100,
        output_tokens=20,
    )
    _trade(conn, pid, did)

    found = queries.decision(conn, did)

    assert [n["headline"] for n in found["news"]] == ["A headline"]
    assert len(found["trades"]) == 1


def test_an_unknown_decision_is_none_rather_than_an_error(conn):
    assert queries.decision(conn, 987654321) is None


def test_an_article_analysed_for_several_tickers_appears_once(conn):
    """One analysis row per ticker, aggregated rather than joined — a join
    would multiply the article out."""
    from marketagent.news import Article

    news_id = repo.save_news(
        conn,
        [
            Article(
                external_id="art-1",
                headline="A headline",
                published_at=datetime(2026, 9, 14, 12, tzinfo=UTC),
                tickers=(TICKER, OTHER),
            )
        ],
    )["art-1"]
    repo.save_news_analysis(
        conn,
        [
            {
                "news_id": news_id,
                "ticker": t,
                "relevant": True,
                "sentiment": "positive",
                "sentiment_score": 0.4,
                "rationale": "r",
                "model": "claude-haiku-4-5",
                "prompt_version": "v1",
                "input_tokens": 10,
                "output_tokens": 2,
            }
            for t in (TICKER, OTHER)
        ],
    )

    rows = queries.news(conn, limit=10)

    assert len(rows) == 1
    assert rows[0]["analysis_count"] == 2


def test_only_relevant_articles_are_returned_when_asked(conn):
    from marketagent.news import Article

    ids = repo.save_news(
        conn,
        [
            Article(
                external_id=f"art-{i}",
                headline=f"Headline {i}",
                published_at=datetime(2026, 9, 14, 12, tzinfo=UTC),
                tickers=(TICKER,),
            )
            for i in (1, 2)
        ],
    )
    repo.save_news_analysis(
        conn,
        [
            {
                "news_id": ids[f"art-{i}"],
                "ticker": TICKER,
                "relevant": relevant,
                "sentiment": "neutral",
                "sentiment_score": 0.0,
                "rationale": "r",
                "model": "claude-haiku-4-5",
                "prompt_version": "v1",
                "input_tokens": 10,
                "output_tokens": 2,
            }
            for i, relevant in ((1, True), (2, False))
        ],
    )

    rows = queries.news(conn, limit=10, relevant_only=True)

    assert [r["headline"] for r in rows] == ["Headline 1"]


def test_trades_are_listed_newest_first(conn):
    pid = repo.portfolio_id(conn, PORTFOLIO)
    _trade(conn, pid, _decision(conn, pid))

    rows = queries.trades(conn, PORTFOLIO, limit=10)

    assert rows[0]["ticker"] == TICKER
    assert rows[0]["status"] == "submitted"


def test_a_run_still_open_past_the_job_timeout_is_reported_stale(conn):
    """A SIGKILL cannot be caught, so a hard kill leaves `running` behind for
    ever; the dashboard must not show a run in progress indefinitely."""
    pid = repo.portfolio_id(conn, PORTFOLIO)
    run_id = repo.open_run(conn, pid, "schedule", dry_run=False, image_tag=None)
    conn.execute(
        "UPDATE agent_runs SET started_at = now() - interval '2 hours' WHERE id = %s", (run_id,)
    )

    row = next(r for r in queries.runs(conn, PORTFOLIO, limit=10) if r["id"] == run_id)

    assert row["stale"] is True


def test_a_run_that_has_just_started_is_not_stale(conn):
    pid = repo.portfolio_id(conn, PORTFOLIO)
    run_id = repo.open_run(conn, pid, "schedule", dry_run=False, image_tag=None)

    row = next(r for r in queries.runs(conn, PORTFOLIO, limit=10) if r["id"] == run_id)

    assert row["stale"] is False


# ---------------------------------------------------------------------------
# Summaries and reviews
# ---------------------------------------------------------------------------


def test_there_is_no_latest_summary_before_the_first_one_is_written(conn):
    assert queries.latest_summary(conn) is None
    assert queries.latest_review(conn) is None


def test_the_latest_summary_is_the_most_recent_day(conn):
    for day, subject in ((TODAY - timedelta(days=1), "Older"), (TODAY, "Newer")):
        repo.save_summary(
            conn, day, subject, "body", "<p>body</p>", None, None, "sent", None, None, None
        )

    assert queries.latest_summary(conn)["subject"] == "Newer"


def test_the_latest_review_withholds_the_rendered_html(conn):
    """`body_html` is Markdown-rendered model output and Python-Markdown passes
    raw HTML straight through, so serving it to a browser that injects it into
    the DOM hands a model an XSS vector for nothing."""
    repo.save_weekly_review(
        conn,
        TODAY - timedelta(days=6),
        TODAY,
        "Subject",
        "An assessment",
        "body",
        "<p><script>alert(1)</script></p>",
        {},
        [{"area": "watchlist", "change": "drop TSLA"}],
        None,
        None,
        "sent",
        None,
        None,
        None,
    )

    found = queries.latest_review(conn)

    assert "body_html" not in found
    assert found["assessment"] == "An assessment"
    assert found["recommendations"][0]["change"] == "drop TSLA"
