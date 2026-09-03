"""A broker that records what it would have done.

`DRY_RUN` defaults on, so this is the first thing a deploy exercises. It is a
separate implementation of the same protocol rather than an `if dry_run:` in
the agent job, which keeps the execution branch out of the loop and makes the
safe path as testable as the real one.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal

from .base import BrokerPosition, OrderResult, Side

logger = logging.getLogger(__name__)

SHARES = Decimal("0.000001")


class DryRunBroker:
    """Simulates an immediate fill at the reference price."""

    def __init__(self) -> None:
        self.submitted: list[tuple[str, Side, Decimal, str]] = []

    def submit_market_order(
        self,
        ticker: str,
        side: Side,
        notional_usd: Decimal,
        client_order_id: str,
        reference_price_usd: Decimal | None = None,
    ) -> OrderResult:
        self.submitted.append((ticker, side, notional_usd, client_order_id))
        logger.info("DRY RUN: would %s $%s of %s (%s)", side, notional_usd, ticker, client_order_id)

        now = datetime.now(UTC)
        # Fills at the latest close, which is the same price the risk engine
        # valued the position at. A real fill happens at the next open and will
        # differ — that gap is the honest cost of a dry run, not something to
        # model with a fake slippage number.
        quantity = None
        if reference_price_usd and reference_price_usd > 0:
            quantity = (notional_usd / reference_price_usd).quantize(SHARES, rounding=ROUND_DOWN)

        return OrderResult(
            status="simulated",
            # No broker id: nothing was submitted, and inventing one would make
            # a simulated trade indistinguishable from a real one in `trades`.
            broker_order_id=None,
            quantity=quantity,
            filled_avg_price_usd=reference_price_usd,
            submitted_at=now,
            filled_at=now,
        )

    def positions(self) -> list[BrokerPosition]:
        return []
