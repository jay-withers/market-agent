"""Trades, runs, and the daily summary."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ... import queries
from ..deps import connection

router = APIRouter(prefix="/api", tags=["activity"])


@router.get("/trades")
def list_trades(
    limit: int = Query(default=50, ge=1, le=500),
    conn: Any = Depends(connection),
) -> list[dict[str, Any]]:
    return queries.trades(conn, limit=limit)


@router.get("/runs")
def list_runs(
    limit: int = Query(default=30, ge=1, le=200),
    conn: Any = Depends(connection),
) -> list[dict[str, Any]]:
    return queries.runs(conn, limit=limit)


@router.get("/summaries/latest")
def latest_summary(conn: Any = Depends(connection)) -> dict[str, Any]:
    found = queries.latest_summary(conn)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no summary yet")
    return found
