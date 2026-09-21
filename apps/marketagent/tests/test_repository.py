"""repository.py against a real Postgres.

Every function here is hand-written SQL, and until these tests existed none of
it had ever been executed by anything but the deployed job: the first run of a
statement was 06:00 UTC in a container, where a wrong column name costs a day
of the experiment. Twenty-eight of the module's thirty functions were never
named in a test.

A real server rather than a mocked connection, because the failures worth
catching are the ones only Postgres can raise — a column that does not exist, a
foreign key to a company nobody seeded, a NUMERIC that will not take the value,
an ON CONFLICT naming the wrong constraint. A psycopg double would accept all
four and assert nothing.

Skipped when POSTGRES_TEST_DSN is unset; `make test-db` sets it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from marketagent import repository as repo
from marketagent.benchmarks import BenchmarkPoint
from marketagent.marketdata import Bar
from marketagent.models import (
    PortfolioState,
    Recommendation,
    RiskReason,
    RiskVerdict,
)
from marketagent.news import Article

D = Decimal

# One of the tickers 003-seed-watchlist.sql seeds, so `prices.ticker` and
# `trades.ticker` have something to reference.
TICKER = "AAPL"
OTHER = "MSFT"


def _bar(ticker: str = TICKER, day: date = date(2026, 9, 14), close: str = "100.0000") -> Bar:
    return Bar(
        ticker=ticker,
        bar_date=day,
        open_usd=D("99.0000"),
        high_usd=D("101.0000"),
        low_usd=D("98.0000"),
        close_usd=D(close),
        volume=1_000_000,
    )


def _article(external_id: str = "art-1", tickers: tuple[str, ...] = (TICKER,)) -> Article:
    return Article(
        external_id=external_id,
        headline="A headline",
        published_at=datetime(2026, 9, 14, 12, 0, tzinfo=UTC),
        summary="A summary",
        url="https://example.invalid/1",
        tickers=tickers,
    )


def _recommendation(action: str = "BUY", amount: float | None = 40.0) -> Recommendation:
    return Recommendation(
        ticker=TICKER,
        action=action,
        confidence=0.78,
        suggested_amount_usd=amount,
        reasoning="Because of the evidence.",
        risks="It could go down.",
    )


def _verdict(
    approved: bool = True,
    amount: str | None = "30.0000",
    constraint: str = "max_trade_usd",
) -> RiskVerdict:
    return RiskVerdict(
        approved=approved,
        approved_amount_usd=D(amount) if amount else None,
        reasons=(RiskReason(constraint=constraint, detail="capped", cap_usd=D("30.0000")),),
        binding_constraint=constraint,
    )


def _fill(conn, pid, side: str, qty: str, notional: str, price: str, cost_usd: str) -> None:
    repo.apply_fill(conn, pid, TICKER, side, D(qty), D(notional), D(price))


def _decision(conn, run_id: int, **kwargs) -> int:
    return repo.save_decision(
        conn,
        run_id=run_id,
        rec=kwargs.pop("rec", _recommendation()),
        verdict=kwargs.pop("verdict", _verdict()),
        state=kwargs.pop("state", PortfolioState(cash_usd=D("500.0000"))),
        model="claude-sonnet-5",
        prompt_version="v1",
        news_ids=kwargs.pop("news_ids", []),
        input_tokens=100,
        output_tokens=20,
        prompt_context=kwargs.pop("prompt_context", None),
    )


# ---------------------------------------------------------------------------
# Watchlist and portfolio
# ---------------------------------------------------------------------------


def test_the_watchlist_excludes_the_benchmark_companies(conn):
    """SPY, VT and EWU sit in `companies` only so `prices.ticker` can reference
    them. Analysing one as though it were a stock pick wastes a model call at
    best and places a trade at worst."""
    tickers = repo.active_tickers(conn)

    assert tickers, "004-benchmark-companies.sql seeds benchmarks; 003 seeds the watchlist"
    assert "SPY" not in tickers
    assert TICKER in tickers
    assert tickers == sorted(tickers)


def test_an_unknown_portfolio_names_the_migration_that_creates_one(conn):
    """The failure a fresh database produces, so it has to point somewhere."""
    with pytest.raises(LookupError, match=r"003-seed-watchlist\.sql"):
        repo.portfolio_id(conn, "no-such-portfolio")


def test_the_seeded_portfolio_starts_at_the_paper_account_default(conn):
    pid = repo.portfolio_id(conn)

    assert repo.load_cash(conn, pid) == D("100000.0000")
    assert repo.initial_cash(conn, pid) == D("100000.0000")


# ---------------------------------------------------------------------------
# Market data and news
# ---------------------------------------------------------------------------


def test_storing_the_same_bar_twice_updates_rather_than_duplicates(conn):
    """The agent can be re-run on a day, and a second row for one ticker-day
    would double-count the close every query after it."""
    pid = repo.portfolio_id(conn)
    assert repo.save_prices(conn, [_bar()]) == 1
    repo.save_prices(conn, [_bar(close="123.4500")])

    rows = conn.execute(
        "SELECT close_usd FROM prices WHERE ticker = %s AND bar_date = %s",
        (TICKER, date(2026, 9, 14)),
    ).fetchall()
    assert rows == [(D("123.4500"),)]
    assert pid  # the fixture's portfolio is untouched by a price write


def test_saving_no_bars_is_not_an_error(conn):
    """A holiday, or a ticker the feed had nothing for."""
    assert repo.save_prices(conn, []) == 0


def test_news_comes_back_keyed_by_the_providers_id(conn):
    """The caller holds Alpaca's id and needs ours to write `news_analysis`."""
    ids = repo.save_news(conn, [_article("art-1"), _article("art-2")])

    assert set(ids) == {"art-1", "art-2"}
    assert all(isinstance(v, int) for v in ids.values())


def test_re_storing_an_article_keeps_the_original_row(conn):
    """Two runs in one day see the same article; a second row would let it be
    analysed and counted twice."""
    first = repo.save_news(conn, [_article("art-1")])
    second = repo.save_news(conn, [_article("art-1")])

    assert first == second


def test_the_first_close_at_or_after_a_date_is_what_indexes_a_benchmark(conn):
    """`close_on` looks forward, not backward: the inception day may be a
    weekend or a holiday, and the arm has to start from the first day that
    actually traded."""
    repo.save_prices(
        conn,
        [
            _bar(day=date(2026, 9, 10), close="90.0000"),
            _bar(day=date(2026, 9, 14), close="100.0000"),
        ],
    )

    assert repo.close_on(conn, TICKER, date(2026, 9, 11)) == D("100.0000")
    assert repo.close_on(conn, TICKER, date(2026, 9, 10)) == D("90.0000")
    assert repo.close_on(conn, TICKER, date(2026, 12, 1)) is None


# ---------------------------------------------------------------------------
# The portfolio the engine and the model are shown
# ---------------------------------------------------------------------------


def test_a_holding_with_no_price_is_reported_rather_than_valued_at_zero(conn):
    """Valuing it at zero understates exposure, which would let the engine
    approve a buy it should refuse — so the caller is told instead."""
    pid = repo.portfolio_id(conn)
    _fill(conn, pid, "BUY", "2.000000", "50.0000", "100.0000", "25.0000")

    state, unpriced = repo.build_state(
        conn,
        pid,
        {},
    )

    assert unpriced == [TICKER]
    assert state.positions == ()


def test_a_priced_holding_is_valued_in_usd(conn):
    pid = repo.portfolio_id(conn)
    _fill(conn, pid, "BUY", "2.000000", "50.0000", "100.0000", "25.0000")

    state, unpriced = repo.build_state(
        conn,
        pid,
        {TICKER: _bar(close="100.0000")},
    )

    assert unpriced == []
    # 2 shares x $100 = USD 200
    assert state.positions[0].value_usd == D("200.0000")


# ---------------------------------------------------------------------------
# Fills move the ledger
# ---------------------------------------------------------------------------


def test_a_buy_takes_cash_and_creates_the_position(conn):
    """Our tables are the ledger — Alpaca's balance describes a different
    portfolio, one with $100,000 in it."""
    pid = repo.portfolio_id(conn)

    _fill(conn, pid, "BUY", "2.000000", "50.0000", "100.0000", "25.0000")

    assert repo.load_cash(conn, pid) == D("99950.0000")
    assert repo.load_positions(conn, pid) == [(TICKER, D("2.000000"), D("100.0000"))]


def test_a_sell_returns_cash_and_reduces_the_position(conn):
    pid = repo.portfolio_id(conn)
    _fill(conn, pid, "BUY", "2.000000", "50.0000", "100.0000", "25.0000")

    _fill(conn, pid, "SELL", "1.000000", "30.0000", "120.0000", "30.0000")

    assert repo.load_cash(conn, pid) == D("99980.0000")
    assert repo.load_positions(conn, pid)[0][1] == D("1.000000")


def test_a_fully_sold_position_stops_being_a_holding(conn):
    """`load_positions` filters on quantity > 0, so a closed position must not
    keep appearing in the state the model is shown."""
    pid = repo.portfolio_id(conn)
    _fill(conn, pid, "BUY", "2.000000", "50.0000", "100.0000", "25.0000")

    _fill(conn, pid, "SELL", "2.000000", "60.0000", "120.0000", "30.0000")

    assert repo.load_positions(conn, pid) == []


# ---------------------------------------------------------------------------
# Runs, decisions and trades
# ---------------------------------------------------------------------------


def test_a_run_is_opened_before_any_work_and_closed_with_its_counts(conn):
    """The row exists first so a crash leaves evidence."""
    run_id = repo.open_run(conn, "schedule", dry_run=False, image_tag="8d2788f")

    repo.close_run(
        conn,
        run_id,
        "succeeded",
        {"news_fetched": 84, "news_relevant": 50, "decisions_made": 9, "trades_executed": 3},
        input_tokens=85_643,
        output_tokens=11_658,
        cost_usd=D("0.1900"),
    )

    row = conn.execute(
        "SELECT trigger, image_tag, status, news_fetched, trades_executed, cost_usd,"
        "  finished_at IS NOT NULL FROM agent_runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    assert row == ("schedule", "8d2788f", "succeeded", 84, 3, D("0.1900"), True)


def test_a_failed_run_records_its_error(conn):
    """BudgetExceeded closes the row through this path, so the failure reads
    like any other."""
    run_id = repo.open_run(conn, "manual", dry_run=True, image_tag=None)

    repo.close_run(conn, run_id, "failed", {}, 0, 0, D("0.0000"), error="BudgetExceeded: $1.00")

    row = conn.execute("SELECT status, error FROM agent_runs WHERE id = %s", (run_id,)).fetchone()
    assert row[0] == "failed"
    assert "BudgetExceeded" in row[1]


def test_a_decision_stores_the_state_and_the_verdict_it_was_made_under(conn):
    """An `ai_decisions` row claims to be a complete record of what was asked,
    which is what makes it replayable months later."""
    run_id = repo.open_run(conn, "manual", dry_run=True, image_tag=None)

    did = _decision(conn, run_id, prompt_context={"recent_decisions": [{"action": "BUY"}]})

    row = conn.execute(
        "SELECT ticker, action, recommended_amount_usd, approved_amount_usd,"
        "  portfolio_state, risk_verdict, prompt_context FROM ai_decisions WHERE id = %s",
        (did,),
    ).fetchone()
    assert row[0] == TICKER
    assert row[1] == "BUY"
    assert row[2] == D("40.0000")
    assert row[3] == D("30.0000")
    # mode="json", so the Decimal is a string rather than a float that lost it.
    assert row[4]["cash_usd"] == "500.0000"
    assert row[5]["binding_constraint"] == "max_trade_usd"
    assert row[6] == {"recent_decisions": [{"action": "BUY"}]}


def test_a_hold_stores_no_recommended_amount(conn):
    """`suggested_amount_usd` is None on a HOLD, and 0 would read as a real
    figure the model proposed."""
    run_id = repo.open_run(conn, "manual", dry_run=True, image_tag=None)

    did = _decision(
        conn,
        run_id,
        rec=_recommendation(action="HOLD", amount=None),
        verdict=_verdict(approved=False, amount=None, constraint="action_is_hold"),
    )

    row = conn.execute(
        "SELECT recommended_amount_usd, approved_amount_usd FROM ai_decisions WHERE id = %s",
        (did,),
    ).fetchone()
    assert row == (None, None)


def _trade(conn, pid, decision_id, **kwargs) -> int:
    defaults = dict(
        ticker=TICKER,
        side="BUY",
        notional_usd=D("30.0000"),
        status="submitted",
        dry_run=False,
        client_order_id=f"decision-{decision_id}",
        broker_order_id=None,
        quantity=None,
        price_usd=None,
        submitted_at=datetime(2026, 9, 14, 6, 5, tzinfo=UTC),
        filled_at=None,
    )
    return repo.save_trade(conn, pid, decision_id, **{**defaults, **kwargs})


def test_a_submitted_order_records_no_quantity(conn):
    """The agent runs at 06:00 and the market opens at 14:30, so a notional
    order rests for hours and Alpaca has not yet derived a quantity. That is
    why `trades.quantity` had to become nullable."""
    pid = repo.portfolio_id(conn)
    run_id = repo.open_run(conn, "schedule", dry_run=False, image_tag=None)
    did = _decision(conn, run_id)

    tid = _trade(conn, pid, did)

    row = conn.execute("SELECT quantity, status FROM trades WHERE id = %s", (tid,)).fetchone()
    assert row == (None, "submitted")


def test_resubmitting_one_decision_updates_its_trade_rather_than_adding_another(conn):
    """`client_order_id` is deterministic per decision precisely so a manual
    retry is idempotent — an order POST is never retried automatically, because
    a 503 that executed before failing to answer is indistinguishable from one
    that did not."""
    pid = repo.portfolio_id(conn)
    run_id = repo.open_run(conn, "schedule", dry_run=False, image_tag=None)
    did = _decision(conn, run_id)

    first = _trade(conn, pid, did)
    second = _trade(
        conn, pid, did, status="filled", quantity=D("0.300000"), price_usd=D("125.0000")
    )

    assert first == second
    count = conn.execute("SELECT count(*) FROM trades WHERE decision_id = %s", (did,)).fetchone()
    assert count == (1,)
    row = conn.execute("SELECT status, quantity FROM trades WHERE id = %s", (first,)).fetchone()
    assert row == ("filled", D("0.300000"))


def test_todays_trades_are_counted_for_the_daily_limit_but_rejections_are_not(conn):
    """The engine cannot count the table itself — `trades_today` is passed in,
    which is what keeps risk.py pure and an `ai_decisions` row replayable."""
    pid = repo.portfolio_id(conn)
    run_id = repo.open_run(conn, "schedule", dry_run=False, image_tag=None)
    before = repo.trades_today(conn, pid)

    _trade(conn, pid, _decision(conn, run_id), client_order_id="a")
    _trade(conn, pid, _decision(conn, run_id), client_order_id="b", status="rejected")

    assert repo.trades_today(conn, pid) == before + 1


def test_a_simulated_trade_still_counts_against_the_daily_limit(conn):
    """A dry run that ignored the limit would not be exercising the same code
    path as a real one."""
    pid = repo.portfolio_id(conn)
    run_id = repo.open_run(conn, "manual", dry_run=True, image_tag=None)
    before = repo.trades_today(conn, pid)

    _trade(conn, pid, _decision(conn, run_id), status="simulated", dry_run=True)

    assert repo.trades_today(conn, pid) == before + 1


def test_an_unreconciled_trade_is_one_the_broker_has_not_answered_for(conn):
    """What the summary job picks up at 21:00 to turn a submission into a fill."""
    pid = repo.portfolio_id(conn)
    run_id = repo.open_run(conn, "schedule", dry_run=False, image_tag=None)
    _trade(conn, pid, _decision(conn, run_id), client_order_id="a", broker_order_id="ord-a")
    _trade(conn, pid, _decision(conn, run_id), client_order_id="b", status="filled")

    pending = repo.unreconciled_trades(conn, pid)

    assert [t["client_order_id"] for t in pending] == ["a"]


# ---------------------------------------------------------------------------
# Decision memory
# ---------------------------------------------------------------------------


def test_recent_decisions_are_newest_first_and_carry_the_orders_status(conn):
    """Without this the model re-argues the same thesis every morning, with
    nothing telling it the engine clamped every one to the same cap."""
    pid = repo.portfolio_id(conn)
    run_id = repo.open_run(conn, "schedule", dry_run=False, image_tag=None)
    older = _decision(conn, run_id)
    conn.execute(
        "UPDATE ai_decisions SET decided_at = now() - interval '2 days' WHERE id = %s", (older,)
    )
    newer = _decision(conn, run_id)
    _trade(conn, pid, newer, status="filled")

    rows = repo.recent_decisions(conn, TICKER, limit=5)

    assert [r["trade_status"] for r in rows] == ["filled", None]
    assert rows[0]["binding_constraint"] == "max_trade_usd"
    assert rows[0]["on_date"] == date.today()


def test_a_decision_with_two_trades_still_appears_once(conn):
    """The lateral single-row join is there so a decision that somehow gained a
    second trade cannot silently double an entry in the prompt."""
    pid = repo.portfolio_id(conn)
    run_id = repo.open_run(conn, "schedule", dry_run=False, image_tag=None)
    did = _decision(conn, run_id)
    _trade(conn, pid, did, client_order_id="a")
    _trade(conn, pid, did, client_order_id="b", status="filled")

    rows = repo.recent_decisions(conn, TICKER, limit=5)

    assert len(rows) == 1


def test_the_history_is_limited_to_what_was_asked_for(conn):
    """Five in the prompt, not every decision ever made on the ticker."""
    run_id = repo.open_run(conn, "schedule", dry_run=False, image_tag=None)
    for _ in range(4):
        _decision(conn, run_id)

    assert len(repo.recent_decisions(conn, TICKER, limit=2)) == 2


def test_the_history_is_per_ticker(conn):
    run_id = repo.open_run(conn, "schedule", dry_run=False, image_tag=None)
    _decision(conn, run_id)

    assert repo.recent_decisions(conn, OTHER, limit=5) == []


# ---------------------------------------------------------------------------
# Spend
# ---------------------------------------------------------------------------


def test_spend_sums_every_job_that_calls_the_model(conn):
    """007-job-costs.sql exists because the summary and weekly jobs never
    recorded their own call, so a total from `agent_runs` alone was low by a
    call a day and a call a week — and the gap only grows."""
    run_id = repo.open_run(conn, "schedule", dry_run=False, image_tag=None)
    repo.close_run(conn, run_id, "succeeded", {}, 100, 20, D("0.1900"))
    today = date.today()
    conn.execute(
        "INSERT INTO daily_summaries (as_of, subject, body_markdown, body_html,"
        "  email_status, cost_usd) VALUES (%s, 'A subject', 'x', '<p>x</p>', 'sent', %s)",
        (today, D("0.0200")),
    )
    conn.execute(
        "INSERT INTO weekly_reviews (period_start, period_end, subject, assessment,"
        "  body_markdown, body_html, recommendations, metrics, email_status, cost_usd)"
        " VALUES (%s, %s, 'A subject', 'a', 'b', '<p>b</p>', '[]'::jsonb, '{}'::jsonb,"
        "  'sent', %s)",
        (today - timedelta(days=7), today, D("0.0500")),
    )

    result = repo.spend(conn, today)

    assert result["today_usd"] == D("0.2600")


def test_spend_is_zero_rather_than_null_on_a_day_nothing_ran(conn):
    """A None here would render as "None" in the email the summary job sends."""
    result = repo.spend(conn, date(2020, 1, 1))

    assert result["today_usd"] == D("0")


# ---------------------------------------------------------------------------
# What the summary job writes at 21:00
# ---------------------------------------------------------------------------


def test_a_fill_reported_late_updates_the_trade_in_place(conn):
    """The agent submits at 06:00 and the market opens at 14:30, so the fill
    becomes known to the summary job eight hours later."""
    pid = repo.portfolio_id(conn)
    run_id = repo.open_run(conn, "schedule", dry_run=False, image_tag=None)
    tid = _trade(conn, pid, _decision(conn, run_id))

    repo.update_trade_outcome(
        conn,
        tid,
        "filled",
        D("0.240000"),
        D("125.0000"),
        datetime(2026, 9, 14, 14, 31, tzinfo=UTC),
    )

    row = conn.execute(
        "SELECT status, quantity, price_usd, filled_at IS NOT NULL FROM trades WHERE id = %s",
        (tid,),
    ).fetchone()
    assert row == ("filled", D("0.240000"), D("125.0000"), True)


def test_re_running_a_day_overwrites_its_performance_row(conn):
    """A retry after a mail outage must not fail on the day's own row."""
    pid = repo.portfolio_id(conn)
    args = (pid, date(2026, 9, 14))
    repo.save_daily_performance(
        conn,
        *args,
        D("450.0000"),
        D("60.0000"),
        D("510.0000"),
        D("10.0000"),
        D("2.0000"),
    )
    repo.save_daily_performance(
        conn,
        *args,
        D("450.0000"),
        D("70.0000"),
        D("520.0000"),
        D("20.0000"),
        D("4.0000"),
    )

    rows = conn.execute(
        "SELECT total_value_usd FROM daily_performance WHERE portfolio_id = %s AND as_of = %s",
        args,
    ).fetchall()
    assert rows == [(D("520.0000"),)]


def test_a_benchmark_arm_is_overwritten_rather_than_duplicated_per_day(conn):
    pid = repo.portfolio_id(conn)
    point = BenchmarkPoint(
        symbol="SPY",
        as_of=date(2026, 9, 14),
        value_usd=D("505.0000"),
        close_usd=D("560.0000"),
    )
    assert repo.save_benchmarks(conn, [point]) == 1
    repo.save_benchmarks(conn, [replace(point, value_usd=D("511.0000"))])

    rows = conn.execute(
        "SELECT value_usd FROM benchmarks WHERE symbol = 'SPY' AND as_of = %s",
        (date(2026, 9, 14),),
    ).fetchall()
    assert rows == [(D("511.0000"),)]
    assert pid


def test_saving_no_benchmark_points_is_not_an_error(conn):
    """A day where every arm was missing a price. They are omitted rather than
    drawn flat at the notional, which would read as "the index did nothing"."""
    assert repo.save_benchmarks(conn, []) == 0


def test_the_days_activity_states_the_trades_and_not_just_the_decisions(conn):
    """The first version of the email listed a reconciliation count and no
    trades — and that count is zero on a dry run, so the model reported cash
    unchanged on a day three trades had executed."""
    pid = repo.portfolio_id(conn)
    run_id = repo.open_run(conn, "manual", dry_run=True, image_tag=None)
    did = _decision(conn, run_id)
    _trade(conn, pid, did, status="simulated", dry_run=True)

    activity = repo.day_activity(conn, pid, date.today())

    assert len(activity["decisions"]) == 1
    assert len(activity["trades"]) == 1
    assert activity["trades"][0][2] == "simulated"


def test_a_summary_records_when_it_was_sent_only_if_it_was(conn):
    """A mail failure never loses the summary: the row is stored either way and
    the dashboard renders from it."""
    sent = repo.save_summary(
        conn,
        date(2026, 9, 14),
        "Subject",
        "body",
        "<p>body</p>",
        "claude-sonnet-5",
        "v1",
        "sent",
        "resend-1",
        None,
        D("0.0200"),
    )
    failed = repo.save_summary(
        conn,
        date(2026, 9, 13),
        "Subject",
        "body",
        "<p>body</p>",
        "claude-sonnet-5",
        "v1",
        "failed",
        None,
        "non-ASCII address",
        D("0.0200"),
    )

    rows = conn.execute(
        "SELECT id, email_status, sent_at IS NOT NULL, email_error FROM daily_summaries"
        " WHERE id IN (%s, %s) ORDER BY id",
        (sent, failed),
    ).fetchall()
    assert rows[0][1:] == ("sent", True, None)
    assert rows[1][1:] == ("failed", False, "non-ASCII address")


def test_re_running_the_summary_for_a_day_replaces_it(conn):
    first = repo.save_summary(
        conn,
        date(2026, 9, 14),
        "First",
        "a",
        "<p>a</p>",
        None,
        None,
        "failed",
        None,
        "timeout",
        None,
    )
    second = repo.save_summary(
        conn,
        date(2026, 9, 14),
        "Second",
        "b",
        "<p>b</p>",
        None,
        None,
        "sent",
        "resend-2",
        None,
        D("0.0200"),
    )

    assert first == second
    row = conn.execute(
        "SELECT subject, email_status FROM daily_summaries WHERE id = %s", (first,)
    ).fetchone()
    assert row == ("Second", "sent")


# ---------------------------------------------------------------------------
# The weekly review
# ---------------------------------------------------------------------------


def test_the_binding_constraint_histogram_splits_approvals_from_refusals(conn):
    """The point of the whole weekly job. A cap binding an approval and a gate
    refusing one are opposite facts, and one table would read as a single
    ranking of "constraints that fired"."""
    run_id = repo.open_run(conn, "schedule", dry_run=False, image_tag=None)
    _decision(conn, run_id, verdict=_verdict(approved=True, constraint="max_trade_usd"))
    _decision(conn, run_id, verdict=_verdict(approved=True, constraint="max_trade_usd"))
    _decision(
        conn,
        run_id,
        verdict=_verdict(approved=False, amount=None, constraint="daily_trade_limit"),
    )
    pid = repo.portfolio_id(conn)
    today = date.today()

    metrics = repo.week_metrics(conn, pid, today - timedelta(days=6), today)

    by_outcome = {(c["binding"], c["approved"]): c["decisions"] for c in metrics["constraints"]}
    assert by_outcome[("max_trade_usd", True)] == 2
    assert by_outcome[("daily_trade_limit", False)] == 1


def test_a_week_with_no_runs_reports_zero_rather_than_null(conn):
    """`run()` returns None on an empty week, but the figures still have to be
    readable — a None would render as "None" in the email."""
    pid = repo.portfolio_id(conn)

    metrics = repo.week_metrics(conn, pid, date(2020, 1, 1), date(2020, 1, 7))

    assert metrics["runs"]["runs"] == 0
    assert metrics["runs"]["cost_usd"] == D("0")
    assert metrics["constraints"] == []


def test_the_weeks_runs_are_counted_by_outcome(conn):
    pid = repo.portfolio_id(conn)
    ok = repo.open_run(conn, "schedule", dry_run=False, image_tag=None)
    repo.close_run(conn, ok, "succeeded", {"news_fetched": 84}, 100, 20, D("0.1900"))
    bad = repo.open_run(conn, "schedule", dry_run=False, image_tag=None)
    repo.close_run(conn, bad, "failed", {}, 0, 0, D("0.0000"), error="boom")
    today = date.today()

    metrics = repo.week_metrics(conn, pid, today - timedelta(days=6), today)

    assert metrics["runs"]["succeeded"] == 1
    assert metrics["runs"]["failed"] == 1
    assert metrics["runs"]["news_fetched"] == 84


def test_a_review_stores_its_proposals_where_they_can_be_queried(conn):
    """`recommendations` is queryable so "has it asked for the same change
    three weeks running?" is a query rather than a re-read of the prose."""
    today = date.today()
    proposals = [
        {"area": "risk_limits", "change": "raise RISK_MAX_DAILY_TRADES", "confidence": 0.6}
    ]

    rid = repo.save_weekly_review(
        conn,
        today - timedelta(days=6),
        today,
        "Subject",
        "An assessment",
        "body",
        "<p>body</p>",
        {"runs": {"runs": 5}},
        proposals,
        "claude-sonnet-5",
        "v1",
        "sent",
        "resend-3",
        None,
        D("0.0500"),
    )

    row = conn.execute(
        "SELECT assessment, recommendations, metrics FROM weekly_reviews WHERE id = %s", (rid,)
    ).fetchone()
    assert row[0] == "An assessment"
    assert row[1][0]["area"] == "risk_limits"
    assert row[2]["runs"]["runs"] == 5


def test_re_running_a_week_overwrites_its_review(conn):
    today = date.today()
    args = (today - timedelta(days=6), today)
    first = repo.save_weekly_review(
        conn,
        *args,
        "First",
        "a",
        "b",
        "<p>b</p>",
        {},
        [],
        None,
        None,
        "failed",
        None,
        "timeout",
        None,
    )
    second = repo.save_weekly_review(
        conn,
        *args,
        "Second",
        "c",
        "d",
        "<p>d</p>",
        {},
        [],
        None,
        None,
        "sent",
        "r",
        None,
        D("0.0500"),
    )

    assert first == second


def test_the_previous_weeks_proposals_are_what_the_next_review_is_shown(conn):
    """So the review compounds instead of restarting every Sunday."""
    today = date.today()
    repo.save_weekly_review(
        conn,
        today - timedelta(days=13),
        today - timedelta(days=7),
        "Older",
        "a",
        "b",
        "<p>b</p>",
        {},
        [{"area": "watchlist", "change": "drop TSLA"}],
        None,
        None,
        "sent",
        None,
        None,
        None,
    )

    assert repo.last_recommendations(conn, today)[0]["change"] == "drop TSLA"


def test_the_first_week_is_shown_an_empty_list_rather_than_nothing(conn):
    """Empty rather than absent, so the first week needs no special case at the
    call site."""
    assert repo.last_recommendations(conn, date(2020, 1, 1)) == []


# ---------------------------------------------------------------------------
# News analysis
# ---------------------------------------------------------------------------


def test_re_analysing_with_the_same_prompt_adds_nothing(conn):
    """The unique key includes the model and prompt version, so re-running the
    same prompt is a no-op while a new prompt adds rows."""
    news_id = repo.save_news(conn, [_article("art-1")])["art-1"]
    row = {
        "news_id": news_id,
        "ticker": TICKER,
        "relevant": True,
        "sentiment": "positive",
        "sentiment_score": 0.4,
        "rationale": "Directly about the company.",
        "model": "claude-haiku-4-5",
        "prompt_version": "v1",
        "input_tokens": 300,
        "output_tokens": 30,
    }
    repo.save_news_analysis(conn, [row])
    repo.save_news_analysis(conn, [row])
    repo.save_news_analysis(conn, [{**row, "prompt_version": "v2"}])

    count = conn.execute(
        "SELECT count(*) FROM news_analysis WHERE news_id = %s", (news_id,)
    ).fetchone()
    assert count == (2,)


def test_analysing_nothing_is_not_an_error(conn):
    assert repo.save_news_analysis(conn, []) == 0


# ---------------------------------------------------------------------------
# week_integrity
#
# These checks exist to catch failures that are silent, so a test that only
# proved they run would miss the point entirely: each one below breaks the
# thing the check watches and asserts it actually fires. The week of
# 2026-09-14 lost a daily_performance row and an agent run and nothing said so.
# ---------------------------------------------------------------------------


def _week(conn, pid: int, start: date, end: date) -> dict[str, dict]:
    """week_integrity keyed by check name, for readable assertions."""
    return {c["check"]: c for c in repo.week_integrity(conn, pid, start, end)}


def _valuation(conn, pid: int, day: date) -> None:
    repo.save_daily_performance(
        conn,
        pid,
        day,
        cash_usd=D("500.0000"),
        positions_value_usd=D("0.0000"),
        total_value_usd=D("500.0000"),
        pnl_usd=D("0.0000"),
        pnl_pct=D("0.0000"),
    )


def _clean_week(conn, pid: int, start: date, end: date) -> None:
    """A week with nothing wrong with it: a valuation and a run every day."""
    conn.execute("UPDATE portfolio SET broker_synced_at=now() WHERE id=%s", (pid,))
    day = start
    while day <= end:
        _valuation(conn, pid, day)
        conn.execute(
            "INSERT INTO agent_runs (started_at, finished_at, status) VALUES (%s, %s, 'succeeded')",
            (datetime.combine(day, datetime.min.time(), UTC), datetime.now(UTC)),
        )
        day += timedelta(days=1)


def test_an_intact_week_passes_every_check(conn):
    pid = repo.portfolio_id(conn)
    end = date.today()
    start = end - timedelta(days=6)
    _clean_week(conn, pid, start, end)

    checks = _week(conn, pid, start, end)

    # Every check reports, pass or fail: a section that appears only on a bad
    # week is one nobody learns to read.
    assert set(checks) == set(repo.INTEGRITY_CHECKS)
    assert all(c["ok"] for c in checks.values()), checks


def test_a_missing_valuation_is_caught_and_named(conn):
    pid = repo.portfolio_id(conn)
    end = date.today()
    start = end - timedelta(days=6)
    _clean_week(conn, pid, start, end)
    gap = start + timedelta(days=3)
    conn.execute("DELETE FROM daily_performance WHERE portfolio_id = %s AND as_of = %s", (pid, gap))

    check = _week(conn, pid, start, end)["missing_valuations"]

    assert not check["ok"]
    assert check["count"] == 1
    # Named, not just counted: "one day is missing" sends you to the database.
    assert str(gap) in check["detail"]


def test_a_day_with_no_agent_run_is_caught(conn):
    pid = repo.portfolio_id(conn)
    end = date.today()
    start = end - timedelta(days=6)
    _clean_week(conn, pid, start, end)
    gap = start + timedelta(days=2)
    conn.execute("DELETE FROM agent_runs WHERE started_at::date = %s", (gap,))

    check = _week(conn, pid, start, end)["missing_runs"]

    assert not check["ok"]
    assert str(gap) in check["detail"]


def test_a_failed_run_is_caught(conn):
    pid = repo.portfolio_id(conn)
    end = date.today()
    start = end - timedelta(days=6)
    _clean_week(conn, pid, start, end)
    conn.execute(
        "UPDATE agent_runs SET status = 'failed' WHERE started_at::date = %s",
        (start + timedelta(days=1),),
    )

    check = _week(conn, pid, start, end)["failed_runs"]

    assert not check["ok"]
    assert "failed" in check["detail"]


def test_a_run_abandoned_past_the_timeout_is_caught_as_well_as_a_failure(conn):
    """SIGKILL cannot be caught, so the row stays 'running' for ever."""
    pid = repo.portfolio_id(conn)
    end = date.today()
    start = end - timedelta(days=6)
    _clean_week(conn, pid, start, end)
    conn.execute(
        "UPDATE agent_runs SET status = 'running', finished_at = NULL WHERE started_at::date = %s",
        (start,),
    )

    check = _week(conn, pid, start, end)["failed_runs"]

    assert not check["ok"]
    assert "abandoned" in check["detail"]


def test_a_holding_that_has_stopped_being_priced_is_caught(conn):
    """The silent one: build_state drops an unpriced holding from the total."""
    pid = repo.portfolio_id(conn)
    end = date.today()
    start = end - timedelta(days=6)
    _clean_week(conn, pid, start, end)
    repo.apply_fill(
        conn,
        pid,
        TICKER,
        "BUY",
        D("1.000000"),
        D("50.0000"),
        D("100.0000"),
    )
    repo.save_prices(conn, [_bar(day=end - timedelta(days=30))])

    check = _week(conn, pid, start, end)["stale_prices"]

    assert not check["ok"]
    assert TICKER in check["detail"]


def test_a_holding_priced_today_is_not_reported_stale(conn):
    pid = repo.portfolio_id(conn)
    end = date.today()
    start = end - timedelta(days=6)
    _clean_week(conn, pid, start, end)
    repo.apply_fill(
        conn,
        pid,
        TICKER,
        "BUY",
        D("1.000000"),
        D("50.0000"),
        D("100.0000"),
    )
    repo.save_prices(conn, [_bar(day=end)])

    assert _week(conn, pid, start, end)["stale_prices"]["ok"]


def test_a_split_sized_price_move_is_caught(conn):
    """adjustment=all restates closes across a split; our quantity does not."""
    pid = repo.portfolio_id(conn)
    end = date.today()
    start = end - timedelta(days=6)
    _clean_week(conn, pid, start, end)
    repo.apply_fill(
        conn,
        pid,
        TICKER,
        "BUY",
        D("1.000000"),
        D("50.0000"),
        D("100.0000"),
    )
    repo.save_prices(
        conn,
        [
            _bar(day=end - timedelta(days=1), close="400.0000"),
            _bar(day=end, close="100.0000"),
        ],
    )

    check = _week(conn, pid, start, end)["price_spike"]

    assert not check["ok"]
    assert TICKER in check["detail"]


def test_an_ordinary_large_move_does_not_fire_the_split_check(conn):
    """AMD moved 9% in a day this month. A check that cries wolf is ignored."""
    pid = repo.portfolio_id(conn)
    end = date.today()
    start = end - timedelta(days=6)
    _clean_week(conn, pid, start, end)
    repo.apply_fill(
        conn,
        pid,
        TICKER,
        "BUY",
        D("1.000000"),
        D("50.0000"),
        D("100.0000"),
    )
    repo.save_prices(
        conn,
        [
            _bar(day=end - timedelta(days=1), close="100.0000"),
            _bar(day=end, close="109.0000"),
        ],
    )

    assert _week(conn, pid, start, end)["price_spike"]["ok"]


def test_an_old_broker_snapshot_is_reported(conn):
    pid = repo.portfolio_id(conn)
    end = date.today()
    start = end - timedelta(days=6)
    _clean_week(conn, pid, start, end)
    assert _week(conn, pid, start, end)["broker_sync"]["ok"]
    conn.execute(
        "UPDATE portfolio SET broker_synced_at=now()-interval '2 days' WHERE id=%s", (pid,)
    )
    assert not _week(conn, pid, start, end)["broker_sync"]["ok"]
