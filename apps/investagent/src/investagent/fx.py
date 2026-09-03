"""The GBP/USD rate.

Alpaca prices and executes in USD; the experiment is denominated in GBP. Every
converted figure is therefore only meaningful alongside the rate that produced
it, which is why the rate is stored on every `trades`, `daily_performance` and
`benchmarks` row rather than looked up again at read time. A rate fetched later
cannot reconstruct what a past trade cost in pounds.

Frankfurter serves ECB reference rates, free and without a key. Note the
endpoint: the `frankfurter.app` host redirects and the path needs a `/v1`
prefix, so the obvious URL returns a **404 with an HTTP 200-shaped JSON body**
rather than a transport error.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from typing import Any

from .fetch import FetchError, get_json

FRANKFURTER_LATEST = "https://api.frankfurter.dev/v1/latest"

# Matches the NUMERIC(18,6) rate columns. Half-even on the rate itself because
# it is an observation rather than a limit — the directional rounding belongs
# on the amounts derived from it.
RATE = Decimal("0.000001")

# Alpaca takes a notional order size in dollars and cents.
USD = Decimal("0.01")


@dataclass(frozen=True)
class FxRate:
    """USD per 1 GBP, and the date the rate is actually for."""

    gbp_usd: Decimal
    # ECB publishes on weekdays, so a Saturday run gets Friday's rate. Storing
    # the date the rate belongs to, rather than the date we asked, is what
    # makes a weekend run explicable later.
    as_of: date
    source: str = "frankfurter"

    def to_usd(self, amount_gbp: Decimal) -> Decimal:
        """Convert GBP to USD, rounding down to whole cents.

        Down, so a converted order can never come out larger than the amount
        the risk engine approved.
        """
        return (amount_gbp * self.gbp_usd).quantize(USD, rounding=ROUND_DOWN)

    def to_gbp(self, amount_usd: Decimal) -> Decimal:
        """Convert USD back to GBP, rounding down to the money scale."""
        return (amount_usd / self.gbp_usd).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)


def fetch_gbp_usd(client: Any | None = None) -> FxRate:
    """The latest published GBP/USD reference rate."""
    payload = get_json(FRANKFURTER_LATEST, params={"base": "GBP", "symbols": "USD"}, client=client)

    try:
        rate = Decimal(str(payload["rates"]["USD"]))
        as_of = date.fromisoformat(payload["date"])
    except (KeyError, TypeError, ValueError) as exc:
        # The 404 body is valid JSON, so a wrong URL arrives here as a missing
        # key rather than as an HTTP error. Say so plainly.
        raise FetchError(f"unexpected response from {FRANKFURTER_LATEST}: {payload}") from exc

    if rate <= 0:
        raise FetchError(f"implausible GBP/USD rate: {rate}")

    return FxRate(gbp_usd=rate.quantize(RATE, rounding=ROUND_HALF_EVEN), as_of=as_of)
