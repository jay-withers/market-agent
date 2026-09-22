"""The rebalance job: keep dynamic-500's watchlist in sync with the S&P 500.

Runs monthly, not daily: this is a slow-moving membership diff against an
external index provider, not the daily market/news/LLM loop, and folding it
into the agent job would pay its cost and risk on every single run instead of
once a month.

Touches `dynamic-500` only — `static-100` is seeded once from a frozen
snapshot (sql/010-seed-sp100-static.sql) and this job never runs against it.

Never auto-sells. A ticker dropped from the index is deactivated on the
watchlist (`portfolio_watchlist.is_active = false`) so the agent stops
analysing and trading it from its next run onward, but any existing position
is left exactly as it is — tracked, valued, sellable only by a human going
directly through Alpaca — the same treatment a manually-held ticker already
gets. See `repository.remove_from_watchlist`.
"""

from __future__ import annotations

import logging

from .. import repository as repo
from ..db import pool
from ..indices import IndexSource, WikipediaIndexSource

logger = logging.getLogger(__name__)

PORTFOLIO = "dynamic-500"


def run(source: IndexSource | None = None) -> dict[str, int]:
    """Diff the current S&P 500 constituent list against the watchlist.

    `source` is injectable so this can be exercised against a fixed list in
    tests rather than a live Wikipedia fetch. Returns counts of tickers added
    and removed, for the caller to log.
    """
    source = source or WikipediaIndexSource()
    constituents = source.sp500()
    live = {c.ticker: c for c in constituents}

    with pool().connection() as conn:
        pid = repo.portfolio_id(conn, name=PORTFOLIO)
        current = repo.watchlist_tickers(conn, pid)

        to_add = [t for t in live if t not in current]
        to_remove = [t for t in current if t not in live]

        repo.add_to_watchlist(
            conn, pid, [(live[t].ticker, live[t].name, live[t].sector) for t in to_add]
        )
        repo.remove_from_watchlist(conn, pid, to_remove)
        conn.commit()

    logger.info(
        "rebalance: %d constituents fetched, %d added, %d removed (watchlist now %d)",
        len(constituents),
        len(to_add),
        len(to_remove),
        len(current) + len(to_add) - len(to_remove),
    )
    return {"fetched": len(constituents), "added": len(to_add), "removed": len(to_remove)}
