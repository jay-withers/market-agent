"""Shared HTTP plumbing for the outbound APIs.

Every outbound call this project makes — Alpaca, Frankfurter, Wikipedia,
DeepSeek — is ordinary JSON (or, for Wikipedia, HTML) over HTTPS with no SDK
worth taking on, DeepSeek included: its API is a plain OpenAI-shaped REST
endpoint, and a dedicated SDK would be one more package's version drift to
track for what `httpx` already does. What they have in common is that the
agent job runs unattended once a day, so a transient failure that a human
would simply retry has to be retried here or it costs a whole day's run.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Generous, because nothing here is user-facing and a slow answer beats no
# answer, but bounded so a hung connection cannot eat the job's
# `replica_timeout_in_seconds`.
TIMEOUT_SECONDS = 30.0

DEFAULT_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0

# Worth retrying: the server said it was briefly unable, or asked us to slow
# down. A 4xx other than 429 is our fault and will fail identically next time.
#
# 520-524 are Cloudflare's own inventions, not IANA codes, and they mean the
# edge answered but its origin did not — the same transient class as a 502. A
# standard-codes-only list silently misses them, which is how a Cloudflare 522
# from Frankfurter took a whole daily summary down the un-retried path on
# 2026-09-14. Both of this project's data sources sit behind Cloudflare.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524})


class FetchError(RuntimeError):
    """A request that failed after exhausting its retries."""


def get_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    client: httpx.Client | None = None,
    attempts: int = DEFAULT_ATTEMPTS,
) -> Any:
    """GET `url` and return the decoded JSON, retrying transient failures.

    `client` is injectable so tests can supply a `MockTransport` and so a
    caller making many requests can reuse one connection pool.
    """
    owned = client is None
    session = client or httpx.Client(timeout=TIMEOUT_SECONDS)
    try:
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = session.get(url, headers=headers, params=params)
                if response.status_code in RETRYABLE_STATUS:
                    raise FetchError(f"{response.status_code} from {url}")
                response.raise_for_status()
                return response.json()
            except (httpx.TransportError, FetchError) as exc:
                last = exc
                if attempt == attempts:
                    break
                # Linear rather than exponential: three attempts a couple of
                # seconds apart covers a blip, and anything longer is better
                # reported as a failed run than slept through.
                delay = BACKOFF_SECONDS * attempt
                logger.warning(
                    "%s (attempt %d/%d), retrying in %.0fs", exc, attempt, attempts, delay
                )
                time.sleep(delay)
            except httpx.HTTPStatusError as exc:
                # Not retried: a 401 or a 404 will say the same thing again.
                raise FetchError(f"{exc.response.status_code} from {url}") from exc

        raise FetchError(f"{url} failed after {attempts} attempts: {last}") from last
    finally:
        if owned:
            session.close()


def get_text(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    client: httpx.Client | None = None,
    attempts: int = DEFAULT_ATTEMPTS,
) -> str:
    """GET `url` and return the response body as text, retrying transient failures.

    Same retry policy as `get_json`, split out rather than parameterised over
    it: the only caller today is `indices.py` scraping a Wikipedia page, which
    is HTML, not JSON, and forcing that through `.json()` would just fail.
    """
    owned = client is None
    session = client or httpx.Client(timeout=TIMEOUT_SECONDS)
    try:
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = session.get(url, headers=headers)
                if response.status_code in RETRYABLE_STATUS:
                    raise FetchError(f"{response.status_code} from {url}")
                response.raise_for_status()
                return response.text
            except (httpx.TransportError, FetchError) as exc:
                last = exc
                if attempt == attempts:
                    break
                delay = BACKOFF_SECONDS * attempt
                logger.warning(
                    "%s (attempt %d/%d), retrying in %.0fs", exc, attempt, attempts, delay
                )
                time.sleep(delay)
            except httpx.HTTPStatusError as exc:
                raise FetchError(f"{exc.response.status_code} from {url}") from exc

        raise FetchError(f"{url} failed after {attempts} attempts: {last}") from last
    finally:
        if owned:
            session.close()


def post_json_retrying(
    url: str,
    *,
    body: dict[str, Any],
    headers: dict[str, str] | None = None,
    client: httpx.Client | None = None,
    attempts: int = DEFAULT_ATTEMPTS,
    timeout: float = TIMEOUT_SECONDS,
) -> Any:
    """POST `body` as JSON and return the decoded response, retrying transient failures.

    This is the *other* kind of POST this project makes, alongside the never-
    retried `post_json` below — and the two exist separately because they are
    not safe to treat the same way. An order submission cannot be retried: a
    503 that actually executed before failing to answer is indistinguishable
    from one that did not, so retrying risks a duplicated trade. An LLM chat
    completion has no such side effect — a retried call just asks the same
    question again — so this one gets `get_json`'s retry loop instead of
    `post_json`'s single attempt.

    `timeout` is overridable, unlike the other functions here: a reasoning
    LLM call is a fundamentally slower request than an Alpaca/Frankfurter/
    Wikipedia GET, and `TIMEOUT_SECONDS` (30s, sized for those) leaves a
    reasoning-heavy DeepSeek call almost no margin — a real run has already
    timed out here with calls that ordinarily take 15-30s to answer.
    """
    owned = client is None
    session = client or httpx.Client(timeout=timeout)
    try:
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = session.post(url, json=body, headers=headers)
                if response.status_code in RETRYABLE_STATUS:
                    raise FetchError(f"{response.status_code} from {url}")
                response.raise_for_status()
                return response.json()
            except (httpx.TransportError, FetchError) as exc:
                last = exc
                if attempt == attempts:
                    break
                delay = BACKOFF_SECONDS * attempt
                logger.warning(
                    "%s (attempt %d/%d), retrying in %.0fs", exc, attempt, attempts, delay
                )
                time.sleep(delay)
            except httpx.HTTPStatusError as exc:
                detail = exc.response.text[:200]
                raise FetchError(f"{exc.response.status_code} from {url}: {detail}") from exc

        raise FetchError(f"{url} failed after {attempts} attempts: {last}") from last
    finally:
        if owned:
            session.close()


def post_json(
    url: str,
    *,
    body: dict[str, Any],
    headers: dict[str, str] | None = None,
    client: httpx.Client | None = None,
) -> Any:
    """POST `body` as JSON and return the decoded response. **Never retried.**

    Deliberately not sharing `get_json`'s retry: the only POST this covers is
    an order submission, and a retried order is a duplicated trade. A
    503 that actually executed before failing to answer is indistinguishable
    from one that did not, so the safe reading is "assume it happened". A
    failed submission still leaves the decision recorded; a double submission
    would corrupt the experiment.
    """
    owned = client is None
    session = client or httpx.Client(timeout=TIMEOUT_SECONDS)
    try:
        response = session.post(url, json=body, headers=headers)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:200]
        raise FetchError(f"{exc.response.status_code} from {url}: {detail}") from exc
    except httpx.TransportError as exc:
        raise FetchError(f"{url} failed: {exc}") from exc
    finally:
        if owned:
            session.close()
