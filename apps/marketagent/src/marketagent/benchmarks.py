"""The comparison arms of the experiment.

The question is not "did the AI make money" but "did it beat the alternatives",
so every one of these tracks the same notional £500 from the same start date.

Alpaca only covers US-listed instruments, so the index arms are proxies and are
labelled as such: `EWU` is a UK equity ETF, **not** the FTSE 100. Saying so is
the difference between a proxy and a wrong number.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from .marketdata import Bar, fetch_daily_bars, latest_close
from .models import money

# The savings-account arm has no market price, so its rows carry a null
# close_usd. Named rather than left implicit because the dashboard has to know
# not to plot it as a security.
CASH_SYMBOL = "CASH5"

DAYS_PER_YEAR = Decimal(365)


@dataclass(frozen=True)
class BenchmarkPoint:
    """What the notional would be worth in one benchmark, on one day."""

    symbol: str
    as_of: date
    value_gbp: Decimal
    close_usd: Decimal | None = None
    fx_rate_gbp_usd: Decimal | None = None
    source: str = "alpaca"


def cash_value(notional_gbp: Decimal, apr_pct: Decimal, days_held: int) -> Decimal:
    """The savings-account arm, compounded daily.

    Daily rather than annual compounding because the comparison is read every
    day and a step function once a year would make the chart nonsense.
    """
    if days_held <= 0:
        return money(notional_gbp)
    daily_rate = apr_pct / 100 / DAYS_PER_YEAR
    return money(notional_gbp * (1 + daily_rate) ** days_held)


def index_value(
    notional_gbp: Decimal, close_now_usd: Decimal, close_at_inception_usd: Decimal
) -> Decimal:
    """What the notional would be worth having bought this index at inception.

    A pure ratio, so the currency cancels: buying $x of SPY with £500 and
    selling it later gives the same GBP return whichever rate is used, provided
    the same rate is used on both sides. That is why this takes no FX argument
    — an earlier version applied today's rate to both ends and produced a number
    that moved with sterling rather than with the index.
    """
    if close_at_inception_usd <= 0:
        return money(notional_gbp)
    return money(notional_gbp * close_now_usd / close_at_inception_usd)


def fetch_benchmark_bars(symbols: list[str], days: int = 7, client: Any = None) -> list[Bar]:
    """Recent bars for the index arms. `CASH5` is computed, never fetched."""
    tradeable = [s for s in symbols if s != CASH_SYMBOL]
    if not tradeable:
        return []
    return fetch_daily_bars(tradeable, days=days, client=client)


def build(
    symbols: list[str],
    bars: list[Bar],
    inception_closes: dict[str, Decimal],
    notional_gbp: Decimal,
    apr_pct: Decimal,
    days_held: int,
    as_of: date,
) -> list[BenchmarkPoint]:
    """One point per benchmark for `as_of`.

    A symbol with no current price or no inception price is skipped rather than
    plotted at the notional: a flat line at £500 would read as "the index did
    nothing", which is a different and wrong claim from "we have no data".
    """
    closes = latest_close(bars)
    points = [
        BenchmarkPoint(
            symbol=CASH_SYMBOL,
            as_of=as_of,
            value_gbp=cash_value(notional_gbp, apr_pct, days_held),
            source="computed",
        )
    ]

    for symbol in symbols:
        if symbol == CASH_SYMBOL:
            continue
        bar = closes.get(symbol)
        start = inception_closes.get(symbol)
        if bar is None or start is None:
            continue
        points.append(
            BenchmarkPoint(
                symbol=symbol,
                as_of=as_of,
                value_gbp=index_value(notional_gbp, bar.close_usd, start),
                close_usd=bar.close_usd,
            )
        )
    return points
