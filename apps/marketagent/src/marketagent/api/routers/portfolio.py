"""What each paper account is worth, and how it got there."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from ... import queries
from ..deps import Account, connection

router = APIRouter(prefix="/api", tags=["portfolio"])


@router.get("/overview")
def overview(account: Account, conn: Any = Depends(connection)) -> dict[str, Any]:
    return queries.overview(conn, account)


@router.get("/performance")
def performance(
    account: Account,
    days: int = Query(default=180, ge=1, le=1000),
    conn: Any = Depends(connection),
) -> dict[str, Any]:
    return queries.performance(conn, account, days=days)


@router.get("/holdings")
def holdings(account: Account, conn: Any = Depends(connection)) -> list[dict[str, Any]]:
    return queries.holdings(conn, account)


@router.get("/prices")
def prices(
    account: Account,
    days: int = Query(default=90, ge=1, le=1000),
    conn: Any = Depends(connection),
) -> list[dict[str, Any]]:
    return queries.price_history(conn, account, days=days)


@router.get("/comparison")
def comparison(
    days: int = Query(default=180, ge=1, le=1000),
    conn: Any = Depends(connection),
) -> dict[str, Any]:
    """Both accounts' valuation series in one payload, for the Compare view."""
    return queries.comparison(conn, days=days)
