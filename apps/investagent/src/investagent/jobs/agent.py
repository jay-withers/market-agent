"""The agent job: news and prices in, risk-checked simulated trades out.

    fetch state -> prices -> news -> cheap filter -> analysis -> risk engine
    -> paper trade -> persist

All of it inside one `agent_runs` row, opened before any work so that a crash
leaves evidence rather than nothing.

Two ordering decisions worth knowing:

* **The decision is persisted whether or not it results in a trade.** A HOLD, a
  refusal by the risk engine and a failed submission are all recorded, because
  the experiment is about what the AI decided as much as what it traded.
* **The decision and its trade are written in one transaction.** A trade with
  no decision behind it would be unauditable, and a decision claiming an
  approved amount with no trade row would misreport the portfolio.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from .. import repository as repo
from .. import risklimits
from ..broker.alpaca import AlpacaBroker
from ..broker.base import Broker
from ..broker.dryrun import DryRunBroker
from ..db import pool
from ..fx import FxRate, fetch_gbp_usd
from ..llm.anthropic_provider import AnthropicLlm
from ..llm.base import PROMPT_VERSION, Llm, Usage
from ..marketdata import Bar, fetch_daily_bars, latest_close
from ..models import Recommendation, money
from ..news import Article, fetch_news
from ..risk import evaluate
from ..settings import settings

logger = logging.getLogger(__name__)

# How much recent price history to show the model. Five sessions is enough to
# see a move without turning the prompt into a data dump.
PRICE_HISTORY_DAYS = 7
NEWS_WINDOW_HOURS = 24


def run(
    trigger: str = "schedule",
    image_tag: str | None = None,
    llm: Llm | None = None,
    broker: Broker | None = None,
) -> int:
    """Execute one agent run. Returns the `agent_runs` id.

    `llm` and `broker` are injectable so the whole loop can be exercised
    against a test double — the alternative is a job that can only ever be
    tested by spending money and placing orders.
    """
    cfg = settings()
    llm = llm or AnthropicLlm()
    if broker is None:
        broker = DryRunBroker() if cfg.dry_run else AlpacaBroker()

    counts = {
        "tickers_considered": 0,
        "news_fetched": 0,
        "news_relevant": 0,
        "decisions_made": 0,
        "trades_executed": 0,
    }
    # Tokens are accumulated for the two token columns, but **cost is
    # accumulated per call**, not derived from the totals. The two stages use
    # different models at different rates, so pricing a mixed token total at
    # either model's rate is simply wrong — measured at 30% too high when the
    # filter's Haiku tokens were priced as Sonnet. The point of the figure is
    # to notice a run that suddenly costs more, which a systematic error
    # defeats.
    usage = Usage(0, 0)
    cost_usd = Decimal(0)

    with pool().connection() as conn:
        run_id = repo.open_run(conn, trigger, cfg.dry_run, image_tag)
        conn.commit()
        logger.info("run %d started (dry_run=%s, trigger=%s)", run_id, cfg.dry_run, trigger)

    try:
        with pool().connection() as conn:
            tickers = repo.active_tickers(conn)
            pid = repo.portfolio_id(conn)
        counts["tickers_considered"] = len(tickers)
        if not tickers:
            raise RuntimeError("no active tickers — run sql/003-seed-watchlist.sql")

        rate = fetch_gbp_usd()
        bars = fetch_daily_bars(tickers, days=PRICE_HISTORY_DAYS)
        closes = latest_close(bars)
        articles = fetch_news(tickers, hours=NEWS_WINDOW_HOURS)
        counts["news_fetched"] = len(articles)

        with pool().connection() as conn:
            repo.save_prices(conn, bars)
            news_ids = repo.save_news(conn, articles)
            conn.commit()

        # --- Stage one: the cheap filter ------------------------------------
        relevant: dict[str, list[Article]] = {t: [] for t in tickers}
        analysis_rows = []
        for article in articles:
            for ticker in article.tickers:
                result = llm.filter_news(ticker, article.headline, article.summary)
                usage += result.usage
                cost_usd += result.cost_usd
                analysis_rows.append(
                    {
                        "news_id": news_ids[article.external_id],
                        "ticker": ticker,
                        "relevant": result.value.relevant,
                        "sentiment": result.value.sentiment,
                        "sentiment_score": result.value.sentiment_score,
                        "rationale": result.value.rationale,
                        "model": result.model,
                        "prompt_version": PROMPT_VERSION,
                        "input_tokens": result.usage.input_tokens,
                        "output_tokens": result.usage.output_tokens,
                    }
                )
                if result.value.relevant:
                    relevant[ticker].append(article)

        counts["news_relevant"] = sum(len(v) for v in relevant.values())
        with pool().connection() as conn:
            repo.save_news_analysis(conn, analysis_rows)
            conn.commit()

        # --- Stage two: analysis, risk, execution ---------------------------
        for ticker in tickers:
            # No relevant news means nothing has changed, so there is nothing
            # to pay the expensive model to think about. This is where the
            # cascade earns its keep.
            if not relevant[ticker]:
                logger.info("%s: no relevant news, skipping analysis", ticker)
                continue
            if ticker not in closes:
                logger.warning("%s: no price, skipping", ticker)
                continue

            with pool().connection() as conn:
                state, unpriced = repo.build_state(conn, pid, closes, rate.gbp_usd)
                already = repo.trades_today(conn, pid)
            if unpriced:
                # Refuse rather than proceed: an unvalued holding understates
                # exposure, which would let the engine approve a buy it should
                # have refused.
                raise RuntimeError(f"holdings with no current price: {', '.join(unpriced)}")

            limits = risklimits.limits(frozenset(tickers))
            result = llm.analyse(_prompt(ticker, relevant[ticker], bars, state, limits, rate))
            usage += result.usage
            cost_usd += result.cost_usd
            rec = result.value
            verdict = evaluate(rec, state, limits, already)
            counts["decisions_made"] += 1

            logger.info(
                "%s: %s conf=%.2f asked=%s approved=%s (%s)",
                ticker,
                rec.action,
                rec.confidence,
                rec.suggested_amount_gbp,
                verdict.approved_amount_gbp,
                verdict.binding_constraint,
            )

            article_ids = [news_ids[a.external_id] for a in relevant[ticker]]
            with pool().connection() as conn:
                decision_id = repo.save_decision(
                    conn,
                    run_id,
                    rec,
                    verdict,
                    state,
                    result.model,
                    PROMPT_VERSION,
                    article_ids,
                    result.usage.input_tokens,
                    result.usage.output_tokens,
                )

                if verdict.approved and verdict.approved_amount_gbp:
                    executed = _execute(
                        conn,
                        broker,
                        pid,
                        decision_id,
                        rec,
                        verdict.approved_amount_gbp,
                        closes[ticker],
                        rate,
                    )
                    counts["trades_executed"] += int(executed)

                # One commit for the decision and its trade together.
                conn.commit()

        with pool().connection() as conn:
            repo.close_run(
                conn,
                run_id,
                "succeeded",
                counts,
                usage.input_tokens,
                usage.output_tokens,
                cost_usd,
            )
            conn.commit()
        logger.info("run %d succeeded: %s", run_id, counts)
        return run_id

    except Exception as exc:
        # The run row is closed as failed with the message, so a failure is
        # visible in the dashboard rather than only in container logs that the
        # daily ingestion cap may have dropped.
        with pool().connection() as conn:
            repo.close_run(
                conn,
                run_id,
                "failed",
                counts,
                usage.input_tokens,
                usage.output_tokens,
                cost_usd,
                f"{type(exc).__name__}: {exc}",
            )
            conn.commit()
        logger.exception("run %d failed", run_id)
        raise


def _execute(
    conn,
    broker: Broker,
    pid: int,
    decision_id: int,
    rec: Recommendation,
    approved_gbp: Decimal,
    bar: Bar,
    rate: FxRate,
) -> bool:
    """Submit the approved trade and record it. True if it filled."""
    side = "BUY" if rec.action == "BUY" else "SELL"
    notional_usd = rate.to_usd(approved_gbp)

    # Deterministic, so a resubmission is idempotent at the broker and the
    # trades row it maps to is updated rather than duplicated.
    client_order_id = f"ia-{decision_id}"

    order = broker.submit_market_order(
        ticker=rec.ticker,
        side=side,
        notional_usd=notional_usd,
        client_order_id=client_order_id,
        reference_price_usd=bar.close_usd,
    )

    repo.save_trade(
        conn,
        pid=pid,
        decision_id=decision_id,
        ticker=rec.ticker,
        side=side,
        notional_gbp=approved_gbp,
        notional_usd=notional_usd,
        fx_rate=rate.gbp_usd,
        fx_rate_as_of=rate.as_of,
        status=order.status,
        dry_run=order.status == "simulated",
        client_order_id=client_order_id,
        broker_order_id=order.broker_order_id,
        quantity=order.quantity,
        price_usd=order.filled_avg_price_usd,
        submitted_at=order.submitted_at,
        filled_at=order.filled_at,
    )

    # Cash and positions move only on an actual fill. A scheduled run submits
    # before the market opens, so the usual outcome is `submitted` and the
    # ledger is untouched until the summary job reconciles.
    filled = order.status in ("filled", "simulated") and order.quantity
    if filled and order.filled_avg_price_usd:
        repo.apply_fill(
            conn,
            pid=pid,
            ticker=rec.ticker,
            side=side,
            quantity=order.quantity,
            notional_gbp=approved_gbp,
            price_usd=order.filled_avg_price_usd,
            # From cash actually spent rather than converting the USD price:
            # the share count is floored, so notional / quantity is the real
            # cost basis and it keeps the first buy consistent with the
            # weighted average applied to every subsequent one.
            price_gbp=money(approved_gbp / order.quantity),
        )
        return True
    return False


def _prompt(ticker, articles, bars, state, limits, rate) -> str:
    """Assemble what the analysis model sees.

    Everything here is also serialised into `ai_decisions.portfolio_state`, so
    a decision stays replayable against exactly this picture.
    """
    history = [b for b in bars if b.ticker == ticker][-5:]
    prices = "\n".join(f"  {b.bar_date} close ${b.close_usd} volume {b.volume:,}" for b in history)
    headlines = "\n".join(
        f"  [{a.published_at:%Y-%m-%d %H:%M} UTC] {a.headline}"
        + (f"\n     {a.summary[:300]}" if a.summary else "")
        for a in articles
    )
    held = state.position_value(ticker)

    return f"""Ticker: {ticker}
Date: {datetime.now(UTC):%Y-%m-%d} (all times UTC)
GBP/USD: {rate.gbp_usd} (ECB rate for {rate.as_of})

Recent daily closes:
{prices or "  none available"}

Relevant news from the last {NEWS_WINDOW_HOURS} hours:
{headlines or "  none"}

Portfolio (the experiment is a notional GBP 500):
  cash: GBP {state.cash_gbp}
  total value: GBP {state.total_value_gbp}
  invested: GBP {state.invested_gbp}
  held in {ticker}: GBP {held}

Risk limits that will be applied to your suggestion:
  max per position: GBP {limits.max_position_gbp}
  max per trade: GBP {limits.max_trade_gbp}
  min per trade: GBP {limits.min_trade_gbp}
  max concentration: {limits.max_concentration_pct}% of total value
  confidence floor: {limits.min_confidence}

Assess {ticker}."""
