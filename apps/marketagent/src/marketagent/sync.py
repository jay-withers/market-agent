"""Synchronize the USD paper account without placing orders."""

from __future__ import annotations

from decimal import Decimal

from . import repository as repo
from .broker.base import BrokerSnapshot
from .db import pool
from .fetch import FetchError
from .fx import fetch_gbp_usd
from .models import PortfolioState, Position, money


def synchronize(pid, broker, connection_pool=None) -> BrokerSnapshot:
    with (connection_pool or pool()).connection() as conn:
        # Serialize snapshot reads as well as writes so a slower worker cannot
        # overwrite a newer snapshot captured by another job.
        conn.execute("SELECT pg_advisory_xact_lock(73011, %s::int)", (pid,))
        snapshot = broker.snapshot()
        if snapshot.account.currency != "USD":
            raise RuntimeError("Only USD accounts can be synchronized")
        repo.save_broker_snapshot(conn, pid, snapshot)
        try:
            rate = fetch_gbp_usd()
            repo.save_display_fx(conn, pid, rate.gbp_usd, rate.as_of)
        except FetchError:
            # Currency conversion is a presentation aid. A rate-provider outage
            # must not prevent the authoritative Alpaca snapshot from landing.
            pass
        conn.commit()
    return snapshot


def run() -> None:
    from .broker.alpaca import AlpacaBroker
    from .jobs.summary import _reconcile

    with pool().connection() as conn:
        pid = repo.portfolio_id(conn)
    broker = AlpacaBroker()
    _reconcile(pid, broker)
    synchronize(pid, broker)


def risk_state(state: PortfolioState, snapshot: BrokerSnapshot) -> PortfolioState:
    """Reserve open buys against cash and exposure; margin buying power is unused."""
    if snapshot.account.trading_blocked:
        raise RuntimeError("Alpaca account is not enabled for trading")
    if state.cash_usd < 0 or any(p.quantity < 0 for p in state.positions):
        raise RuntimeError("The agent requires a cash-funded, long-only account")
    positions = {p.ticker: p for p in state.positions}
    reserved = Decimal(0)
    for order in snapshot.open_orders:
        if order.side != "buy":
            continue
        reserved += order.reserved_usd
        held = positions.get(order.ticker)
        positions[order.ticker] = Position(
            ticker=order.ticker,
            quantity=held.quantity if held else Decimal(0),
            avg_cost_usd=held.avg_cost_usd if held else Decimal(0),
            value_usd=(held.value_usd if held else Decimal(0)) + order.reserved_usd,
        )
    return PortfolioState(
        cash_usd=money(max(state.cash_usd - reserved, Decimal(0))),
        positions=tuple(positions.values()),
        equity_usd=state.total_value_usd,
    )
