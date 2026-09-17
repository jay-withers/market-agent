"""Authenticated access to Alpaca's three surfaces.

One credential pair covers market data, news and paper execution, which is why
this lives in one place rather than three. The surfaces differ only by host:
trading on `paper-api.alpaca.markets`, data and news on `data.alpaca.markets`.

Worth knowing before trusting anything this returns: the paper account is
funded with **$100,000 and $400,000 of buying power**, against an experiment
denominated at a notional £500. Alpaca will happily execute orders hundreds of
times larger than intended, so its balance is not a safety net and nothing here
treats it as one — the risk engine and our own `portfolio` table are the only
things bounding position size.
"""

from __future__ import annotations

from typing import Any

import httpx

from .fetch import get_json, post_json
from .settings import secret, settings

# Alpaca caps a data page at 1000 bars and 50 news articles, so anything that
# could exceed either has to follow `next_page_token` or silently truncate.
NEWS_PAGE_LIMIT = 50
BARS_PAGE_LIMIT = 1000


def headers() -> dict[str, str]:
    return {
        "APCA-API-KEY-ID": secret("ALPACA-API-KEY"),
        "APCA-API-SECRET-KEY": secret("ALPACA-SECRET-KEY"),
        "accept": "application/json",
    }


def get_trading(path: str, params: dict[str, Any] | None = None, client: Any = None) -> Any:
    url = f"{settings().alpaca_trading_base_url}{path}"
    return get_json(url, headers=headers(), params=params, client=client)


def post_trading(path: str, body: dict[str, Any], client: Any = None) -> Any:
    url = f"{settings().alpaca_trading_base_url}{path}"
    return post_json(url, body=body, headers=headers(), client=client)


def get_data(path: str, params: dict[str, Any] | None = None, client: Any = None) -> Any:
    url = f"{settings().alpaca_data_base_url}{path}"
    return get_json(url, headers=headers(), params=params, client=client)


def paginate_data(
    path: str,
    params: dict[str, Any],
    key: str,
    client: httpx.Client | None = None,
    max_pages: int = 20,
) -> list[Any] | dict[str, Any]:
    """Follow `next_page_token` and merge every page's `key`.

    Alpaca returns news as a list and bars as a dict keyed by symbol, so this
    merges whichever it finds. `max_pages` is a stop rather than a limit: an
    unbounded loop against a paginated API is how an unattended daily job turns
    one bad parameter into a very large bill and a timed-out run.
    """
    merged: list[Any] | dict[str, Any] | None = None
    token: str | None = None

    for _ in range(max_pages):
        page_params = dict(params)
        if token:
            page_params["page_token"] = token
        payload = get_data(path, page_params, client=client)
        chunk = payload.get(key)

        if isinstance(chunk, dict):
            if merged is None:
                merged = {}
            assert isinstance(merged, dict)
            for symbol, rows in chunk.items():
                merged.setdefault(symbol, []).extend(rows)
        elif isinstance(chunk, list):
            if merged is None:
                merged = []
            assert isinstance(merged, list)
            merged.extend(chunk)

        token = payload.get("next_page_token")
        if not token:
            break

    return merged if merged is not None else []
