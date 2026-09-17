"""News headlines from Alpaca.

One article routinely mentions several watchlist names — the live feed returned
one listing ten symbols — which is why `news.tickers` is an array with a GIN
index rather than a row per ticker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .alpaca_api import NEWS_PAGE_LIMIT, paginate_data

NEWS_PATH = "/v1beta1/news"


@dataclass(frozen=True)
class Article:
    """One news article, keyed by Alpaca's own id so re-fetching deduplicates."""

    external_id: str
    headline: str
    published_at: datetime
    summary: str | None = None
    author: str | None = None
    url: str | None = None
    source: str = "alpaca"
    tickers: tuple[str, ...] = field(default_factory=tuple)


def fetch_news(
    tickers: list[str],
    hours: int = 24,
    max_articles: int = 200,
    client: Any = None,
) -> list[Article]:
    """Articles mentioning `tickers` from the last `hours`, newest first.

    `max_articles` bounds the batch because every article costs a filter call:
    the cheap model is cheap, not free, and an unusually noisy news day should
    not turn into an unusually expensive run.
    """
    if not tickers:
        return []

    start = datetime.now(UTC) - timedelta(hours=hours)
    payload = paginate_data(
        NEWS_PATH,
        {
            "symbols": ",".join(sorted(tickers)),
            "start": start.isoformat(),
            "limit": NEWS_PAGE_LIMIT,
            "sort": "desc",
        },
        key="news",
        client=client,
    )

    if not isinstance(payload, list):
        return []

    watchlist = set(tickers)
    articles = []
    for row in payload[:max_articles]:
        # Alpaca tags articles with every symbol it mentions, including ones we
        # do not follow. Narrowing to the watchlist keeps the stored array
        # meaningful and stops a mega-cap roundup dragging in 30 tickers.
        mentioned = tuple(sorted(set(row.get("symbols") or []) & watchlist))
        articles.append(
            Article(
                external_id=str(row["id"]),
                headline=row["headline"],
                published_at=datetime.fromisoformat(row["created_at"]),
                summary=(row.get("summary") or None),
                author=(row.get("author") or None),
                url=(row.get("url") or None),
                source=row.get("source") or "alpaca",
                tickers=mentioned,
            )
        )
    return articles
