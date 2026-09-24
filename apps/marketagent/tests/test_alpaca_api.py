"""Tests for the per-pot credential selector.

Every pot's Alpaca paper account shares one process-scoped setter rather than
an explicit argument threaded through every call site — see alpaca_api.py for
why. These tests are the guarantee that the setter actually keeps the pots'
credentials apart.
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


def test_each_pot_resolves_its_own_credential_pair():
    alpaca_api.use_account("tech")
    tech = alpaca_api.headers()

    alpaca_api.use_account("energy")
    energy = alpaca_api.headers()

    assert tech["APCA-API-KEY-ID"] == "test-alpaca-key-tech"
    assert energy["APCA-API-SECRET-KEY"] == "test-alpaca-secret-energy"
    assert tech["APCA-API-KEY-ID"] != energy["APCA-API-KEY-ID"]


def test_headers_carry_the_accept_header_alongside_the_credentials():
    alpaca_api.use_account("tech")

    assert alpaca_api.headers()["accept"] == "application/json"
