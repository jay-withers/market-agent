"""The audit trail: what the AI decided, and what the risk engine allowed."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ... import queries
from ..deps import connection

router = APIRouter(prefix="/api", tags=["decisions"])


@router.get("/decisions")
def list_decisions(
    limit: int = Query(default=50, ge=1, le=500),
    ticker: str | None = Query(default=None, max_length=10),
    conn: Any = Depends(connection),
) -> list[dict[str, Any]]:
    return queries.decisions(conn, limit=limit, ticker=ticker)


@router.get("/decisions/{decision_id}")
def get_decision(decision_id: int, conn: Any = Depends(connection)) -> dict[str, Any]:
    found = queries.decision(conn, decision_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such decision")
    return found


@router.get("/news")
def list_news(
    limit: int = Query(default=50, ge=1, le=500),
    relevant_only: bool = Query(default=False),
    conn: Any = Depends(connection),
) -> list[dict[str, Any]]:
    return queries.news(conn, limit=limit, relevant_only=relevant_only)
