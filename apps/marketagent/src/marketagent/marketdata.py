"""Daily price bars from Alpaca.

Daily, not intraday: the agent runs once a day and the risk engine values
positions at the latest close, so a minute bar would be more data for no better
decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from .alpaca_api import BARS_PAGE_LIMIT, paginate_data
from .settings import settings

BARS_PATH = "/v2/stocks/bars"


@dataclass(frozen=True)
class Bar:
    """One day's trading for one ticker. Prices are USD, as Alpaca reports."""

    ticker: str
    bar_date: date
    open_usd: Decimal
    high_usd: Decimal
    low_usd: Decimal
    close_usd: Decimal
    volume: int


def _decimal(value: Any) -> Decimal:
    # Via str: the JSON number is a float, and Decimal(float) imports binary
    # artefacts that would then be stored in a NUMERIC column.
    return Decimal(str(value))


def fetch_daily_bars(tickers: list[str], days: int = 5, client: Any = None) -> list[Bar]:
    """Daily bars for `tickers` over roughly the last `days` calendar days.

    Calendar days, not trading days: asking for a fixed window and taking what
    comes back is simpler than modelling the exchange calendar, and every
    caller wants "the recent past" rather than an exact count.
    """
    if not tickers:
        return []

    start = (datetime.now(UTC) - timedelta(days=days)).date()
    payload = paginate_data(
        BARS_PATH,
        {
            "symbols": ",".join(sorted(tickers)),
            "timeframe": "1Day",
            "start": start.isoformat(),
            "limit": BARS_PAGE_LIMIT,
            "feed": settings().alpaca_data_feed,
            # Splits and dividends restated into the history, so a comparison
            # across a corporate action is not nonsense.
            "adjustment": "all",
        },
        key="bars",
        client=client,
    )

    if not isinstance(payload, dict):
        return []

    bars = [
        Bar(
            ticker=ticker,
            bar_date=datetime.fromisoformat(row["t"]).date(),
            open_usd=_decimal(row["o"]),
            high_usd=_decimal(row["h"]),
            low_usd=_decimal(row["l"]),
            close_usd=_decimal(row["c"]),
            volume=int(row["v"]),
        )
        for ticker, rows in payload.items()
        for row in rows
    ]
    return sorted(bars, key=lambda b: (b.ticker, b.bar_date))


def latest_close(bars: list[Bar]) -> dict[str, Bar]:
    """The most recent bar per ticker.

    A ticker with no bar at all is simply absent, rather than present with a
    zero price — a missing price must stop a decision, not silently value a
    holding at nothing.
    """
    latest: dict[str, Bar] = {}
    for bar in bars:
        held = latest.get(bar.ticker)
        if held is None or bar.bar_date > held.bar_date:
            latest[bar.ticker] = bar
    return latest
