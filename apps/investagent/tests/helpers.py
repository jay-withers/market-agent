"""Builders for an httpx client whose requests never leave the process."""

from __future__ import annotations

import json
from typing import Any

import httpx


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def json_client(payload: Any, status: int = 200, capture: list | None = None) -> httpx.Client:
    """A client answering every request with `payload`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        return httpx.Response(status, json=payload)

    return mock_client(handler)


def sequence_client(responses: list[tuple[int, Any]], calls: list | None = None) -> httpx.Client:
    """A client answering each request with the next entry in `responses`."""
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        status, payload = remaining.pop(0)
        if isinstance(payload, str):
            return httpx.Response(status, text=payload)
        return httpx.Response(status, content=json.dumps(payload))

    return mock_client(handler)
