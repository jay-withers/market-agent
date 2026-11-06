"""The passive pot: hold the S&P 500 and reinvest idle cash, with no model.

The experiment's real control. The benchmark arms in `benchmarks.py` simulate
an index from the starting equity; this pot buys one through the same broker,
risk engine and ledger as the agent's pot, so fills, fractional shares, cash
drag and the paper account's own quirks hit both sides of the comparison.

VOO rather than VUAA, the UCITS class the comparison is about: Alpaca trades
US-listed instruments only. Same index, same provider. VOO pays dividends out
where VUAA accumulates them, so each run puts any idle cash back in, which is
what makes the two equivalent.

One decision a run at most, written to `ai_decisions` like the agent's, with
`model = "rule:passive"`. Writing it there keeps one path for every order: the
risk engine sees it, the trade carries the deterministic `ia-<decision_id>`
client order id, and the ledger, the summary and the dashboard need no second
case. The pot's limits (`RISK_*` in Terraform's `local.pots`) let it hold
the whole account in one ticker, which the agent's limits never would.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from .. import alpaca_api, risklimits
from .. import repository as repo
from ..broker.alpaca import AlpacaBroker
from ..broker.base import Broker
from ..broker.dryrun import DryRunBroker
from ..db import database
from ..llm.base import PROMPT_VERSION
from ..marketdata import fetch_daily_bars, latest_close
from ..models import Recommendation
from ..pricehistory import restated_history
from ..risk import evaluate, new_buy_headroom
from ..settings import settings
from ..sync import risk_state, synchronize

logger = logging.getLogger(__name__)

MODEL = "rule:passive"
PRICE_HISTORY_DAYS = 7


def run(
    portfolio: str,
    trigger: str = "schedule",
    image_tag: str | None = None,
    broker: Broker | None = None,
) -> int:
    """One passive run for one pot. Returns the `agent_runs` id."""
    cfg = settings()
    alpaca_api.use_account(portfolio)
    if broker is None:
        broker = DryRunBroker() if cfg.dry_run else AlpacaBroker()
    paper = not isinstance(broker, DryRunBroker)
    counts = {
        "tickers_considered": 0,
        "news_fetched": 0,
        "news_relevant": 0,
        "decisions_made": 0,
        "trades_executed": 0,
    }

    with database().connection() as conn:
        pid = repo.portfolio_id(conn, name=portfolio)
        if not repo.is_active(conn, pid):
            raise RuntimeError(f"portfolio {portfolio!r} is retired; its history is read-only")
        tickers = repo.active_tickers(conn, pid)
        limits = risklimits.limits(frozenset(tickers))
        config = {
            "strategy": "passive",
            "risk": limits.model_dump(mode="json", exclude={"allowed_tickers"}),
        }
        run_id = repo.open_run(conn, pid, trigger, cfg.dry_run, image_tag, config)
        conn.commit()

    try:
        if len(tickers) != 1:
            raise RuntimeError(f"a passive pot holds exactly one ticker, not {tickers}")
        (ticker,) = tickers
        counts["tickers_considered"] = 1

        snapshot = synchronize(pid, broker, database()) if paper else None
        bars = fetch_daily_bars([ticker], days=PRICE_HISTORY_DAYS)
        history = restated_history(database(), bars)
        with database().connection() as conn:
            repo.save_prices(conn, history + bars)
            conn.commit()
        closes = latest_close(bars)
        if ticker not in closes:
            raise RuntimeError(f"no price for {ticker}")

        if snapshot and any(o.ticker == ticker for o in snapshot.open_orders):
            # Yesterday's order has not filled yet: buying again would spend
            # the same cash twice.
            logger.info("%s: an order is already open; nothing to do", ticker)
        else:
            with database().connection() as conn:
                state, unpriced = repo.build_state(conn, pid, closes)
                already = repo.trades_today(conn, pid)
            if unpriced:
                raise RuntimeError(f"holdings with no current price: {', '.join(unpriced)}")
            if snapshot:
                state = risk_state(state, snapshot)

            room = new_buy_headroom(state, limits, already, state.position_value(ticker))
            if room < limits.min_trade_usd:
                # Fully invested, or a dividend too small to buy with yet: it
                # waits in cash until the next one makes it worth a trade.
                logger.info("%s: USD %s idle, under the minimum trade; nothing to do", ticker, room)
            else:
                counts["trades_executed"] = int(
                    _buy(broker, pid, run_id, limits, ticker, room, state, closes, already)
                )
                counts["decisions_made"] = 1

        if paper:
            synchronize(pid, broker, database())
        with database().connection() as conn:
            repo.close_run(conn, run_id, "succeeded", counts, 0, 0, Decimal(0))
            conn.commit()
        logger.info("passive run %d succeeded: %s", run_id, counts)
        return run_id

    # BaseException for the same reason as the agent: a SIGTERM at the job
    # timeout must still close the row rather than leave it `running`.
    except BaseException as exc:
        with database().connection() as conn:
            repo.close_run(
                conn, run_id, "failed", counts, 0, 0, Decimal(0), f"{type(exc).__name__}: {exc}"
            )
            conn.commit()
        logger.exception("passive run %d failed", run_id)
        raise


def _buy(broker, pid, run_id, limits, ticker, room, state, closes, already) -> bool:
    """Record the rule's decision and submit it through the agent's path."""
    from .agent import _execute

    rec = Recommendation(
        ticker=ticker,
        action="BUY",
        confidence=1.0,
        suggested_amount_usd=float(room),
        reasoning=f"Passive pot: invest idle cash in {ticker}.",
        risks="None considered: the pot holds the index whatever the news.",
    )
    verdict = evaluate(rec, state, limits, already)
    with database().connection() as conn:
        decision_id = repo.save_decision(
            conn,
            pid,
            run_id,
            rec,
            verdict,
            state,
            MODEL,
            PROMPT_VERSION,
            [],
            0,
            0,
            prompt_context={"buy_headroom_usd": str(room), "trades_today": already},
        )
        executed = False
        if verdict.approved and verdict.approved_amount_usd:
            executed = _execute(
                conn, broker, pid, decision_id, rec, verdict.approved_amount_usd, closes[ticker]
            )
        logger.info(
            "%s: BUY USD %s approved=%s (%s)",
            ticker,
            room,
            verdict.approved_amount_usd,
            verdict.binding_constraint,
        )
        conn.commit()
    return executed
