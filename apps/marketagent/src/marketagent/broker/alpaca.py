"""Paper execution through Alpaca."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from ..alpaca_api import get_trading, post_trading
from .base import (
    BrokerAccount,
    BrokerPosition,
    BrokerSnapshot,
    OpenOrder,
    OrderResult,
    Side,
    Status,
)

ORDERS_PATH = "/v2/orders"
POSITIONS_PATH = "/v2/positions"

# Alpaca's order statuses mapped onto ours. Everything pre-fill collapses to
# `submitted`, since the distinction between `new`, `accepted` and `pending_new`
# is Alpaca's internal plumbing and not something the experiment reasons about.
# `expired` becomes `cancelled` — an order that never filled, either way.
ALPACA_STATUS: dict[str, Status] = {
    "new": "submitted",
    "accepted": "submitted",
    "pending_new": "submitted",
    "accepted_for_bidding": "submitted",
    "held": "submitted",
    "calculated": "submitted",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "canceled": "cancelled",
    "pending_cancel": "cancelled",
    "expired": "cancelled",
    "rejected": "rejected",
    "suspended": "rejected",
    "stopped": "cancelled",
    "replaced": "cancelled",
    "pending_replace": "submitted",
}


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


class AlpacaBroker:
    """Implements the `Broker` protocol against Alpaca's paper endpoint."""

    def __init__(self, client: Any = None) -> None:
        self._client = client

    def submit_market_order(
        self,
        ticker: str,
        side: Side,
        notional_usd: Decimal,
        client_order_id: str,
        reference_price_usd: Decimal | None = None,
    ) -> OrderResult:
        # Notional rather than qty: the risk engine approves an *amount*, and
        # converting that to a share count ourselves would either need a price
        # we do not have at submission or round the approved amount upwards.
        body = {
            "symbol": ticker,
            "notional": f"{notional_usd:.2f}",
            "side": side.lower(),
            "type": "market",
            # `day`, so an order that never fills expires with the session
            # rather than resting and surprising a later run.
            "time_in_force": "day",
            "client_order_id": client_order_id,
        }
        payload = post_trading(ORDERS_PATH, body, client=self._client)

        raw = payload.get("status", "")
        return OrderResult(
            # An unrecognised status is treated as `submitted` rather than
            # dropped: Alpaca can add one, and the order exists regardless.
            status=ALPACA_STATUS.get(raw, "submitted"),
            broker_order_id=payload.get("id"),
            quantity=_decimal(payload.get("filled_qty")) or None,
            filled_avg_price_usd=_decimal(payload.get("filled_avg_price")),
            submitted_at=_timestamp(payload.get("submitted_at") or payload.get("created_at")),
            filled_at=_timestamp(payload.get("filled_at")),
        )

    def order(self, client_order_id: str) -> OrderResult | None:
        """Look an order up by our own id, for reconciling a fill later.

        The scheduled agent submits before the market opens, so the fill is not
        known during the run that caused it — the summary job reconciles.
        """
        try:
            payload = get_trading(
                f"{ORDERS_PATH}:by_client_order_id",
                {"client_order_id": client_order_id},
                client=self._client,
            )
        except Exception:
            return None

        return OrderResult(
            status=ALPACA_STATUS.get(payload.get("status", ""), "submitted"),
            broker_order_id=payload.get("id"),
            quantity=_decimal(payload.get("filled_qty")) or None,
            filled_avg_price_usd=_decimal(payload.get("filled_avg_price")),
            submitted_at=_timestamp(payload.get("submitted_at") or payload.get("created_at")),
            filled_at=_timestamp(payload.get("filled_at")),
        )

    def positions(self) -> list[BrokerPosition]:
        payload = get_trading(POSITIONS_PATH, client=self._client)
        return [
            BrokerPosition(
                ticker=row["symbol"],
                quantity=Decimal(str(row["qty"])),
                market_value_usd=Decimal(str(row["market_value"])),
                avg_entry_price_usd=Decimal(str(row["avg_entry_price"])),
            )
            for row in payload
        ]

    def snapshot(self) -> BrokerSnapshot:
        positions = self.positions()
        orders = get_trading(ORDERS_PATH, {"status": "open", "limit": 500}, client=self._client)
        if len(orders) >= 500:
            raise RuntimeError("Open order list may be truncated; refusing incomplete snapshot")
        open_orders = []
        for row in orders:
            reserved = Decimal(0)
            if row["side"] == "buy":
                # Reserve the full original notional even on a partial fill.
                # Over-reserving is preferable to spending unsettled cash twice.
                if row.get("notional"):
                    reserved = Decimal(row["notional"])
                elif row.get("qty") and row.get("limit_price"):
                    reserved = Decimal(row["qty"]) * Decimal(row["limit_price"])
                else:
                    raise RuntimeError("Cannot size an open buy order; wait for it to finish")
            open_orders.append(OpenOrder(row["symbol"], row["side"], reserved))
        row = get_trading("/v2/account", client=self._client)
        account = BrokerAccount(
            id=row["id"],
            currency=row["currency"],
            cash_usd=Decimal(row["cash"]),
            equity_usd=Decimal(row["equity"]),
            buying_power_usd=Decimal(row["buying_power"]),
            trading_blocked=bool(
                row.get("trading_blocked")
                or row.get("account_blocked")
                or row.get("trade_suspended_by_user")
                or row.get("status") != "ACTIVE"
            ),
        )
        if account.currency != "USD":
            raise RuntimeError("Only USD Alpaca accounts are supported")
        return BrokerSnapshot(account, positions, open_orders)
