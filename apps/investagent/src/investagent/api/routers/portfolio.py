"""What the £500 is worth, and how it got there."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from ... import queries
from ..deps import connection

router = APIRouter(prefix="/api", tags=["portfolio"])


@router.get("/overview")
def overview(conn: Any = Depends(connection)) -> dict[str, Any]:
    return queries.overview(conn)


@router.get("/performance")
def performance(
    days: int = Query(default=180, ge=1, le=1000),
    conn: Any = Depends(connection),
) -> dict[str, Any]:
    return queries.performance(conn, days=days)


@router.get("/holdings")
def holdings(conn: Any = Depends(connection)) -> list[dict[str, Any]]:
    return queries.holdings(conn)
