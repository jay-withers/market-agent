"""What the agent job needs from a broker, and the shape of a submission.

**A submitted order usually has no fill yet.** The agent runs at 06:00 UTC and
the US market opens at 14:30, so a market order placed by the scheduled job
sits `accepted` for eight hours. Everything below is therefore built around
"submitted, outcome unknown" being the normal case rather than an error:
quantity and price are optional, and the notional — which *is* known at
submission — carries the size.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol

Side = Literal["BUY", "SELL"]

# Our own vocabulary, matching the CHECK constraint on `trades.status`. Alpaca's
# is larger and uses US spelling, so `ALPACA_STATUS` below maps it in one place
# rather than leaking two vocabularies through the codebase.
Status = Literal[
    "pending", "submitted", "filled", "partially_filled", "cancelled", "rejected", "simulated"
]


@dataclass(frozen=True)
class OrderResult:
    """The outcome of submitting one order."""

    status: Status
    # Absent for a dry run, and for a submission that was rejected outright.
    broker_order_id: str | None = None
    # Both None until the order actually fills. A notional order does not name
    # a quantity at all — Alpaca computes it from the dollar amount — so this is
    # unknown at submission even in principle.
    quantity: Decimal | None = None
    filled_avg_price_usd: Decimal | None = None
    submitted_at: datetime | None = None
    filled_at: datetime | None = None


@dataclass(frozen=True)
class BrokerPosition:
    """A holding as the broker sees it.

    Used for reconciliation only. Our `positions` table is the source of truth
    for the experiment, because the paper account is funded with $100,000
    against a notional £500 and its figures describe a different portfolio.
    """

    ticker: str
    quantity: Decimal
    market_value_usd: Decimal
    avg_entry_price_usd: Decimal


class Broker(Protocol):
    def submit_market_order(
        self,
        ticker: str,
        side: Side,
        notional_usd: Decimal,
        client_order_id: str,
        reference_price_usd: Decimal | None = None,
    ) -> OrderResult:
        """Place a notional market order.

        `client_order_id` must be deterministic for the decision it implements,
        so that a manual retry cannot double-submit. `reference_price_usd` is
        ignored by a real broker and used by the dry-run one to simulate a fill.
        """
        ...

    def positions(self) -> list[BrokerPosition]: ...
