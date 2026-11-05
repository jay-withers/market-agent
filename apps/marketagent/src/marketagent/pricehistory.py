"""Keep stored price history in one set of terms across a corporate action.

Each run fetches only the last few days of bars, with `adjustment=all`, so after
a split Alpaca restates every earlier bar but only the newest week reaches
`prices`. Everything older stays in the old terms, and the stored history shows
a one-day cliff that never happened. The valuation is unaffected — it is the
broker's quantity times the latest close, both post-split — but the price
charts and the weekly `price_spike` check are not.

So before saving, compare the fresh bars with what is stored and re-fetch the
whole stored history of any ticker that no longer matches. A dividend restates
history too, by cents, and is repaired the same way.
"""

from __future__ import annotations

import logging
from typing import Any

from . import repository as repo
from .marketdata import Bar, fetch_daily_bars

logger = logging.getLogger(__name__)


def restated_history(db: Any, bars: list[Bar]) -> list[Bar]:
    """Bars that rewrite the stored history of any restated ticker in `bars`.

    Save them alongside `bars`. Empty, and no request made, when nothing was
    restated — the normal day. The fetch happens outside any connection block,
    since that block holds the database blob's lease.
    """
    with db.connection() as conn:
        restated = repo.restated_tickers(conn, bars)
    if not restated:
        return []

    logger.info(
        "price history restated for %s; re-fetching it",
        ", ".join(f"{t} (from {d})" for t, d in sorted(restated.items())),
    )
    return fetch_daily_bars(sorted(restated), start=min(restated.values()))
