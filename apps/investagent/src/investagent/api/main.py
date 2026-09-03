"""The read-only API behind the dashboard.

Nothing here writes. The agent and summary jobs own every mutation, so this
process cannot place a trade, change a limit or alter a decision however it is
called — which is most of the reason it is comfortable being publicly reachable.

**Both `/healthz` and `/readyz` exist, and the difference matters.** Container
Apps uses the liveness probe to decide whether to restart the container; if that
probe touched the database, a database blip would restart every replica and turn
an outage into a crash loop. So `/healthz` answers from memory alone and
`/readyz` is the one that checks the connection.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware

from ..db import close_pool
from ..settings import settings
from .deps import connection, require_token
from .routers import activity, decisions, portfolio

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """The pool is opened on first use, not here.

    `min_replicas = 0` means every request may be a cold start, so startup does
    as little as possible; and a database that is briefly unreachable should
    fail a request rather than prevent the app from starting at all.
    """
    yield
    close_pool()


def create_app() -> FastAPI:
    cfg = settings()
    app = FastAPI(
        title="InvestAgent API",
        description="Read-only view of an AI paper-trading experiment.",
        version="0.1.0",
        lifespan=lifespan,
    )

    # The dashboard learns the API's address at runtime and is served from its
    # own origin, so it needs an explicit CORS allowance. Only the dashboard's
    # origin, and only GET — there is nothing here to POST to.
    origins = [o for o in (cfg.dashboard_origin,) if o]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["authorization", "content-type"],
    )

    @app.get("/healthz", tags=["health"])
    def healthz() -> dict[str, str]:
        """Liveness. Deliberately does not touch the database — see the module docstring."""
        return {"status": "ok", "environment": cfg.environment}

    @app.get("/readyz", tags=["health"])
    def readyz(response: Response, conn: Any = Depends(connection)) -> dict[str, Any]:
        """Readiness: can this replica actually serve a request?"""
        try:
            conn.execute("SELECT 1")
        except Exception as exc:
            logger.warning("readiness check failed: %s", exc)
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "unavailable", "database": False}
        return {"status": "ok", "database": True}

    for router in (portfolio.router, decisions.router, activity.router):
        app.include_router(router, dependencies=[Depends(require_token)])

    return app


app = create_app()
