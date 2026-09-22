"""Shared dependencies: a connection per request, the account selector, and
the optional bearer gate."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any, Literal

from fastapi import Header, HTTPException, Query, status

from ..db import pool
from ..settings import secret, settings

# Every per-account route requires this explicitly, with no default that would
# silently pick one account over the other — `Literal` also gives FastAPI free
# validation (an unknown value 422s) and an enum in the OpenAPI docs.
Account = Annotated[Literal["static-100", "dynamic-500"], Query()]


def connection() -> Iterator[Any]:
    """A pooled connection for the duration of one request.

    Read-only endpoints, so nothing commits; the context manager returns the
    connection to the pool either way.
    """
    with pool().connection() as conn:
        yield conn


def require_token(authorization: str | None = Header(default=None)) -> None:
    """A shared-bearer gate, off unless `API_REQUIRE_TOKEN` is set.

    The hook exists so that turning authentication on is configuration rather
    than a code change, but it is **not** the real answer: the proper fix is
    Container Apps EasyAuth with Entra, which `azurerm` does not expose and
    would need `azapi`. Off here by default because the data is paper-trading
    positions and AI reasoning, with no PII, no money, and no secret in any
    response — a deliberate, documented position that both apps can be
    publicly reachable. Terraform turns this on for the deployed API anyway,
    to close the gap the dashboard's own Basic Auth cannot: the dashboard
    forwards the same token it reads out of its own config.
    """
    if not settings().api_require_token:
        return

    expected = secret("API-BEARER-TOKEN")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")

    presented = authorization.removeprefix("Bearer ")
    # Constant-time: a plain != leaks the matching prefix length to anyone
    # willing to time the responses.
    import hmac

    if not hmac.compare_digest(presented, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid bearer token")
