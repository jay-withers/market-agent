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

from .. import alpaca_api, risklimits
from .. import repository as repo
from ..broker.alpaca import AlpacaBroker
from ..broker.base import Broker
from ..broker.dryrun import DryRunBroker
from ..db import pool
from ..llm.anthropic_provider import AnthropicLlm
from ..llm.base import PROMPT_VERSION, Llm, Usage
from ..marketdata import Bar, fetch_daily_bars, latest_close
from ..models import Recommendation
from ..news import Article, fetch_news
from ..risk import evaluate
from ..settings import settings
from ..sync import risk_state, synchronize

logger = logging.getLogger(__name__)

# How much recent price history to show the model. Five sessions is enough to
# see a move without turning the prompt into a data dump.
PRICE_HISTORY_DAYS = 7
NEWS_WINDOW_HOURS = 24
# How many of the agent's own prior decisions on a ticker it is shown. Five is
# roughly a trading week: long enough to see a thesis being repeated, short
# enough that it does not crowd out the news it is meant to be reading.
DECISION_HISTORY = 5


class BudgetExceeded(RuntimeError):
    """One run spent more with the model than `MAX_RUN_COST_USD` allows."""


def _check_budget(spent: Decimal, ceiling: Decimal) -> None:
    """Stop the run if its model spend has crossed the ceiling.

    Checked after every call rather than once per stage, because the stage that
    can run away — the filter, at one call per article/ticker pair — is exactly
    the one a per-stage check would let finish first. Raising leaves the work
    already committed in place and closes the `agent_runs` row as failed with
    this message, which is the same shape as any other failure.
    """
    if spent > ceiling:
        raise BudgetExceeded(
            f"model spend ${spent} exceeded the ${ceiling} ceiling for one run (MAX_RUN_COST_USD)"
        )


def run(
    portfolio: str,
    trigger: str = "schedule",
    image_tag: str | None = None,
    llm: Llm | None = None,
    broker: Broker | None = None,
) -> int:
    """Execute one agent run for one account. Returns the `agent_runs` id.

    `portfolio` is `'static-100'` or `'dynamic-500'` — required, no default,
    since each account runs as a separate scheduled job (`--portfolio` on the
    CLI) and a forgotten argument must fail loudly rather than silently trade
    the wrong account.

    `llm` and `broker` are injectable so the whole loop can be exercised
    against a test double — the alternative is a job that can only ever be
    tested by spending money and placing orders.
    """
    cfg = settings()
    llm = llm or AnthropicLlm()
    alpaca_api.use_account(portfolio)
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
        pid = repo.portfolio_id(conn, name=portfolio)
        run_id = repo.open_run(conn, pid, trigger, cfg.dry_run, image_tag)
        conn.commit()
        logger.info(
            "run %d started (portfolio=%s, dry_run=%s, trigger=%s)",
            run_id,
            portfolio,
            cfg.dry_run,
            trigger,
        )

    try:
        with pool().connection() as conn:
            tickers = repo.active_tickers(conn, pid)
        counts["tickers_considered"] = len(tickers)
        if not tickers:
            raise RuntimeError(f"no active tickers for {portfolio!r} — run its watchlist seed")

        paper = not isinstance(broker, DryRunBroker)
        if paper:
            synchronize(pid, broker, pool())
        else:
            with pool().connection() as conn:
                synced = conn.execute(
                    "SELECT broker_account_id FROM portfolio WHERE id=%s", (pid,)
                ).fetchone()[0]
            if synced:
                raise RuntimeError(
                    "Use an isolated database for dry runs; this portfolio mirrors Alpaca"
                )
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
                _check_budget(cost_usd, cfg.max_run_cost_usd)
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

            snapshot = synchronize(pid, broker, pool()) if paper else None
            if snapshot and any(o.ticker == ticker for o in snapshot.open_orders):
                logger.info("%s: an order is already open; skipping", ticker)
                continue
            with pool().connection() as conn:
                state, unpriced = repo.build_state(conn, pid, closes)
            if snapshot:
                state = risk_state(state, snapshot)
            with pool().connection() as conn:
                already = repo.trades_today(conn, pid)
                history = repo.recent_decisions(conn, pid, ticker, DECISION_HISTORY)
            if unpriced:
                # Refuse rather than proceed: an unvalued holding understates
                # exposure, which would let the engine approve a buy it should
                # have refused.
                raise RuntimeError(f"holdings with no current price: {', '.join(unpriced)}")

            limits = risklimits.limits(frozenset(tickers))
            result = llm.analyse(_prompt(ticker, relevant[ticker], bars, state, limits, history))
            usage += result.usage
            cost_usd += result.cost_usd
            _check_budget(cost_usd, cfg.max_run_cost_usd)
            rec = result.value
            verdict = evaluate(rec, state, limits, already)
            counts["decisions_made"] += 1

            logger.info(
                "%s: %s conf=%.2f asked=%s approved=%s (%s)",
                ticker,
                rec.action,
                rec.confidence,
                rec.suggested_amount_usd,
                verdict.approved_amount_usd,
                verdict.binding_constraint,
            )

            article_ids = [news_ids[a.external_id] for a in relevant[ticker]]
            with pool().connection() as conn:
                decision_id = repo.save_decision(
                    conn,
                    pid,
                    run_id,
                    rec,
                    verdict,
                    state,
                    result.model,
                    PROMPT_VERSION,
                    article_ids,
                    result.usage.input_tokens,
                    result.usage.output_tokens,
                    # The history is an input to the decision that
                    # `portfolio_state` does not carry, so it is stored beside
                    # it rather than left only in a prompt nobody kept.
                    prompt_context={"recent_decisions": history},
                )

                if verdict.approved and verdict.approved_amount_usd:
                    executed = _execute(
                        conn,
                        broker,
                        pid,
                        decision_id,
                        rec,
                        verdict.approved_amount_usd,
                        closes[ticker],
                    )
                    counts["trades_executed"] += int(executed)

                # One commit for the decision and its trade together.
                conn.commit()

        if paper:
            synchronize(pid, broker, pool())
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

    # BaseException, not Exception: KeyboardInterrupt and SystemExit do not
    # derive from Exception, so a Ctrl-C or a SIGTERM-driven exit used to skip
    # this handler entirely and leave the row `running` for ever. That is not a
    # local-only concern — Container Apps terminates a job that reaches
    # `replica_timeout_in_seconds`, and the timeout case is exactly the one
    # where a record of the failure matters most. Re-raised after closing the
    # row, so the exit code is unchanged.
    except BaseException as exc:
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
    approved_usd: Decimal,
    bar: Bar,
) -> bool:
    """Submit the approved trade and record it. True if it filled."""
    side = "BUY" if rec.action == "BUY" else "SELL"
    notional_usd = approved_usd

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
        notional_usd=notional_usd,
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
    if order.status == "simulated" and filled and order.filled_avg_price_usd:
        repo.apply_fill(
            conn,
            pid=pid,
            ticker=rec.ticker,
            side=side,
            quantity=order.quantity,
            notional_usd=approved_usd,
            price_usd=order.filled_avg_price_usd,
        )
    return bool(filled)


def _history_lines(history: list[dict]) -> str:
    """The agent's own recent decisions on this ticker, as prompt text.

    One fixed shape per row rather than prose, so an absent figure reads as an
    absence instead of changing the sentence — and so a test can assert what
    the model was told.
    """
    if not history:
        return "  none recorded — this is the first assessment of this ticker"

    def amount(value) -> str:
        return f"USD {value}" if value is not None else "none"

    lines = []
    for row in history:
        confidence = f"{row['confidence']:.2f}" if row["confidence"] is not None else "n/a"
        lines.append(
            f"  {row['on_date']} {row['action']}"
            f" confidence {confidence}"
            f" | asked {amount(row['recommended_amount_usd'])}"
            f" | approved {amount(row['approved_amount_usd'])}"
            f" | binding constraint {row['binding_constraint'] or 'none recorded'}"
            f" | order {row['trade_status'] or 'none placed'}"
        )
    return "\n".join(lines)


def _prompt(ticker, articles, bars, state, limits, history=()) -> str:
    """Assemble what the analysis model sees.

    Everything here is also serialised onto the `ai_decisions` row — the
    portfolio into `portfolio_state` and the decision history into
    `prompt_context` — so a decision stays replayable against exactly this
    picture. Anything added here needs the same treatment, or the row stops
    being a complete record of what was asked.
    """
    recent_bars = [b for b in bars if b.ticker == ticker][-5:]
    prices = "\n".join(
        f"  {b.bar_date} close ${b.close_usd} volume {b.volume:,}" for b in recent_bars
    )
    headlines = "\n".join(
        f"  [{a.published_at:%Y-%m-%d %H:%M} UTC] {a.headline}"
        + (f"\n     {a.summary[:300]}" if a.summary else "")
        for a in articles
    )
    held = state.position_value(ticker)

    return f"""Ticker: {ticker}
Date: {datetime.now(UTC):%Y-%m-%d} (all times UTC)
Currency: USD

Recent daily closes:
{prices or "  none available"}

Relevant news from the last {NEWS_WINDOW_HOURS} hours:
{headlines or "  none"}

Your own recent decisions on {ticker} (most recent first):
{_history_lines(list(history))}

What that history does and does not tell you: "approved" is what the risk
engine permitted after applying the limits below, so a refusal is a statement
about those limits and not about your reasoning. "order" is the status of any
resulting order, not its outcome — one submitted before the US open rests for
hours — and nothing here says whether a decision turned out well. Use it to
avoid re-arguing a case you have already made, not as a score.

Portfolio (mirrors the Alpaca paper account):
  cash: USD {state.cash_usd}
  total value: USD {state.total_value_usd}
  invested: USD {state.invested_usd}
  held in {ticker}: USD {held}

Risk limits that will be applied to your suggestion:
  max per position: USD {limits.max_position_usd}
  max per trade: USD {limits.max_trade_usd}
  min per trade: USD {limits.min_trade_usd}
  max concentration: {limits.max_concentration_pct}% of total value
  confidence floor: {limits.min_confidence}

Assess {ticker}."""
