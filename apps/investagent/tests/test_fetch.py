"""Tests for the retry policy, which exists because the agent runs unattended."""

from __future__ import annotations

import json

import httpx
import pytest

from investagent.fetch import FetchError, get_json, post_json
from tests.helpers import json_client, mock_client, sequence_client


def test_a_successful_get_returns_the_decoded_body():
    assert get_json("https://x/y", client=json_client({"ok": True})) == {"ok": True}


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_a_transient_status_is_retried_and_can_succeed(status):
    calls: list = []
    client = sequence_client([(status, {}), (200, {"ok": True})], calls=calls)

    assert get_json("https://x/y", client=client) == {"ok": True}
    assert len(calls) == 2


def test_a_transient_status_that_never_clears_raises_after_its_attempts():
    calls: list = []
    client = sequence_client([(503, {})] * 3, calls=calls)

    with pytest.raises(FetchError, match="after 3 attempts"):
        get_json("https://x/y", client=client, attempts=3)
    assert len(calls) == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_a_permanent_status_is_not_retried(status):
    """A 401 will say the same thing next time; retrying only delays the report."""
    calls: list = []
    client = sequence_client([(status, {})], calls=calls)

    with pytest.raises(FetchError, match=str(status)):
        get_json("https://x/y", client=client)
    assert len(calls) == 1


def test_a_transport_error_is_retried():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"ok": True})

    assert get_json("https://x/y", client=mock_client(handler)) == {"ok": True}
    assert attempts["n"] == 3


def test_query_parameters_are_sent():
    captured: list = []
    get_json("https://x/y", params={"base": "GBP"}, client=json_client({}, capture=captured))

    assert captured[0].url.params["base"] == "GBP"


def test_a_post_is_never_retried():
    """A retried order submission is a duplicated trade."""
    calls: list = []
    client = sequence_client([(503, {}), (200, {"id": "1"})], calls=calls)

    with pytest.raises(FetchError, match="503"):
        post_json("https://x/orders", body={"symbol": "NVDA"}, client=client)
    assert len(calls) == 1


def test_a_post_sends_its_body_as_json():
    captured: list = []
    post_json("https://x/orders", body={"symbol": "NVDA"}, client=json_client({}, capture=captured))

    assert json.loads(captured[0].read()) == {"symbol": "NVDA"}
