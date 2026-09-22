"""Tests for the per-account credential selector.

Two Alpaca paper accounts share one process-scoped setter rather than an
explicit argument threaded through every call site — see alpaca_api.py's
module docstring for why. These tests are the guarantee that the setter
actually keeps the two accounts' credentials apart.
"""

from __future__ import annotations

from marketagent import alpaca_api


def test_headers_refuse_to_answer_before_an_account_is_selected():
    alpaca_api._account = None
    try:
        alpaca_api.headers()
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "use_account" in str(exc)


def test_an_unknown_account_name_is_rejected():
    try:
        alpaca_api.use_account("made-up")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "made-up" in str(exc)


def test_each_account_resolves_its_own_credential_pair():
    alpaca_api.use_account("static-100")
    static_headers = alpaca_api.headers()

    alpaca_api.use_account("dynamic-500")
    dynamic_headers = alpaca_api.headers()

    assert static_headers["APCA-API-KEY-ID"] != dynamic_headers["APCA-API-KEY-ID"]
    assert static_headers["APCA-API-SECRET-KEY"] != dynamic_headers["APCA-API-SECRET-KEY"]


def test_headers_carry_the_accept_header_alongside_the_credentials():
    alpaca_api.use_account("static-100")

    assert alpaca_api.headers()["accept"] == "application/json"
