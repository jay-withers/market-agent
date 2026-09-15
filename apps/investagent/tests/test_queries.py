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

from investagent import queries
from investagent import repository as repo
from investagent.models import PortfolioState, Recommendation, RiskReason, RiskVerdict

D = Decimal

TICKER = "AAPL"
OTHER = "MSFT"
TODAY = date.today()


def _decision(conn, ticker: str = TICKER, action: str = "BUY", approved: bool = True) -> int:
    run_id = repo.open_run(conn, "schedule", dry_run=False, image_tag=None)
    return repo.save_decision(
        conn,
        run_id=run_id,
        rec=Recommendation(
            ticker=ticker,
            action=action,
            confidence=0.78,
            suggested_amount_gbp=40.0,
            reasoning="Because of the evidence.",
            risks="It could go down.",
        ),
        verdict=RiskVerdict(
            approved=approved,
            approved_amount_gbp=D("30.0000") if approved else None,
            reasons=(RiskReason(constraint="max_trade_gbp", detail="capped"),),
            binding_constraint="max_trade_gbp",
        ),
        state=PortfolioState(cash_gbp=D("500.0000")),
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
        notional_gbp=D("30.0000"),
        notional_usd=D("37.5000"),
        fx_rate=D(fx),
        fx_rate_as_of=TODAY,
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
    from investagent.marketdata import Bar

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
    result = queries.overview(conn)

    assert result["cash_gbp"] == 500.0
    assert result["position_count"] == 0
    assert result["pnl_gbp"] == 0.0
    assert result["last_run"] is None


def test_the_overview_values_positions_at_the_latest_close(conn):
    pid = repo.portfolio_id(conn)
    repo.apply_fill(
        conn, pid, TICKER, "BUY", D("2.000000"), D("50.0000"), D("100.0000"), D("25.0000")
    )
    _price(conn, close="100.0000")
    _trade(conn, pid, _decision(conn), fx="1.250000")

    result = queries.overview(conn)

    # 2 shares x $100 / 1.25
    assert result["positions_value_gbp"] == 160.0
    assert result["position_count"] == 1
    assert result["total_value_gbp"] == 610.0


def test_the_valuation_uses_the_most_recent_rate_and_not_the_highest_ever_seen(conn):
    """The first version used max(), which is wrong in a way that worsens: it
    locks onto the highest rate ever recorded and never moves again. A high
    historical rate must not keep deflating today's valuation."""
    pid = repo.portfolio_id(conn)
    repo.apply_fill(
        conn, pid, TICKER, "BUY", D("2.000000"), D("50.0000"), D("100.0000"), D("25.0000")
    )
    _price(conn, close="100.0000")
    # An old, high rate, then a newer and lower one on a trade recorded now.
    repo.save_daily_performance(
        conn,
        pid,
        TODAY - timedelta(days=30),
        D("500.0000"),
        D("0.0000"),
        D("500.0000"),
        D("0.0000"),
        D("0.0000"),
        D("2.000000"),
        TODAY - timedelta(days=30),
    )
    _trade(conn, pid, _decision(conn), fx="1.250000")

    result = queries.overview(conn)

    # Under max() this would be 2 x 100 / 2.0 = 100.0 for ever.
    assert result["positions_value_gbp"] == 160.0


def test_a_position_with_no_price_contributes_nothing_but_still_counts(conn):
    """The lateral join is a LEFT one so an unpriced ticker does not drop the
    row entirely."""
    pid = repo.portfolio_id(conn)
    repo.apply_fill(
        conn, pid, TICKER, "BUY", D("2.000000"), D("50.0000"), D("100.0000"), D("25.0000")
    )

    result = queries.overview(conn)

    assert result["position_count"] == 1
    assert result["positions_value_gbp"] == 0.0


def test_the_overview_carries_the_most_recent_run(conn):
    run_id = repo.open_run(conn, "schedule", dry_run=False, image_tag="8d2788f")
    repo.close_run(conn, run_id, "succeeded", {"trades_executed": 3}, 100, 20, D("0.1900"))

    result = queries.overview(conn)

    assert result["last_run"]["status"] == "succeeded"
    assert result["last_run"]["trades_executed"] == 3


def test_money_crosses_the_display_boundary_as_float_not_decimal(conn):
    """The one place the Decimal rule is relaxed: a browser charts these and
    throws them away, and a Decimal would serialise as a JSON string."""
    result = queries.overview(conn)

    assert isinstance(result["cash_gbp"], float)
    assert not isinstance(result["cash_gbp"], Decimal)


# ---------------------------------------------------------------------------
# Series
# ---------------------------------------------------------------------------


def test_the_performance_series_carries_the_portfolio_and_the_benchmarks(conn):
    from investagent.benchmarks import BenchmarkPoint

    pid = repo.portfolio_id(conn)
    repo.save_daily_performance(
        conn,
        pid,
        TODAY,
        D("450.0000"),
        D("60.0000"),
        D("510.0000"),
        D("10.0000"),
        D("2.0000"),
        D("1.250000"),
        TODAY,
    )
    repo.save_benchmarks(conn, [BenchmarkPoint(symbol="SPY", as_of=TODAY, value_gbp=D("505.0000"))])

    result = queries.performance(conn, days=30)

    assert [r["total_value_gbp"] for r in result["portfolio"]] == [510.0]
    assert [r["symbol"] for r in result["benchmarks"]] == ["SPY"]


def test_the_performance_window_excludes_anything_older(conn):
    pid = repo.portfolio_id(conn)
    repo.save_daily_performance(
        conn,
        pid,
        TODAY - timedelta(days=90),
        D("500.0000"),
        D("0.0000"),
        D("500.0000"),
        D("0.0000"),
        D("0.0000"),
        D("1.250000"),
        TODAY - timedelta(days=90),
    )

    assert queries.performance(conn, days=7)["portfolio"] == []


def test_holdings_carry_the_company_name_and_the_last_close(conn):
    pid = repo.portfolio_id(conn)
    repo.apply_fill(
        conn, pid, TICKER, "BUY", D("2.000000"), D("50.0000"), D("100.0000"), D("25.0000")
    )
    _price(conn, close="100.0000")

    rows = queries.holdings(conn)

    assert rows[0]["ticker"] == TICKER
    assert rows[0]["name"]
    assert rows[0]["last_close_usd"] == 100.0


# ---------------------------------------------------------------------------
# Decisions, news, trades, runs
# ---------------------------------------------------------------------------


def test_decisions_are_newest_first_and_report_the_engines_verdict(conn):
    _decision(conn, approved=False)

    rows = queries.decisions(conn, limit=10)

    assert rows[0]["approved"] is False
    assert rows[0]["binding_constraint"] == "max_trade_gbp"
    assert rows[0]["news_count"] == 0


def test_decisions_can_be_filtered_to_one_ticker(conn):
    _decision(conn, ticker=TICKER)
    _decision(conn, ticker=OTHER)

    rows = queries.decisions(conn, limit=10, ticker=OTHER)

    assert [r["ticker"] for r in rows] == [OTHER]


def test_the_decision_limit_is_honoured(conn):
    """Every limit is bounded — an unbounded one is how a read-only API becomes
    a denial of service."""
    for _ in range(3):
        _decision(conn)

    assert len(queries.decisions(conn, limit=2)) == 2


def test_one_decision_comes_back_with_the_articles_and_trades_behind_it(conn):
    """The endpoint the audit trail exists for."""
    from investagent.news import Article

    pid = repo.portfolio_id(conn)
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
    run_id = repo.open_run(conn, "schedule", dry_run=False, image_tag=None)
    did = repo.save_decision(
        conn,
        run_id=run_id,
        rec=Recommendation(
            ticker=TICKER,
            action="BUY",
            confidence=0.8,
            suggested_amount_gbp=40.0,
            reasoning="r",
            risks="k",
        ),
        verdict=RiskVerdict(
            approved=True,
            approved_amount_gbp=D("30.0000"),
            reasons=(RiskReason(constraint="max_trade_gbp", detail="capped"),),
            binding_constraint="max_trade_gbp",
        ),
        state=PortfolioState(cash_gbp=D("500.0000")),
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
    from investagent.news import Article

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
    from investagent.news import Article

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
    pid = repo.portfolio_id(conn)
    _trade(conn, pid, _decision(conn))

    rows = queries.trades(conn, limit=10)

    assert rows[0]["ticker"] == TICKER
    assert rows[0]["status"] == "submitted"


def test_a_run_still_open_past_the_job_timeout_is_reported_stale(conn):
    """A SIGKILL cannot be caught, so a hard kill leaves `running` behind for
    ever; the dashboard must not show a run in progress indefinitely."""
    run_id = repo.open_run(conn, "schedule", dry_run=False, image_tag=None)
    conn.execute(
        "UPDATE agent_runs SET started_at = now() - interval '2 hours' WHERE id = %s", (run_id,)
    )

    row = next(r for r in queries.runs(conn, limit=10) if r["id"] == run_id)

    assert row["stale"] is True


def test_a_run_that_has_just_started_is_not_stale(conn):
    run_id = repo.open_run(conn, "schedule", dry_run=False, image_tag=None)

    row = next(r for r in queries.runs(conn, limit=10) if r["id"] == run_id)

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
