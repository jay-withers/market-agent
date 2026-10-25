"""`marketagent selfcheck`, which `make smoke` runs in every job after a deploy.

It has to fail on exactly what would break a real run, and do nothing else: a
false failure rolls back a good release, and a side effect (an LLM call, an
order, an email) would fire on every deploy.
"""

from __future__ import annotations

import logging

import pytest

from marketagent import cli, selfcheck
from tests.helpers import POTS


@pytest.fixture
def database_ok(monkeypatch):
    monkeypatch.setattr(selfcheck, "active_pots", lambda: list(POTS))


def test_passes_when_the_database_and_every_secret_resolve(database_ok):
    assert selfcheck.run() == 0


def test_checks_every_active_pots_secrets(database_ok):
    labels = [label for label, _ in selfcheck.checks()]
    for pot in POTS:
        for name in selfcheck.pot_secrets(pot):
            assert f"secret {name}" in labels


def test_fails_on_a_missing_pot_secret_and_names_it(database_ok, monkeypatch, caplog):
    monkeypatch.delenv("ALPACA_SECRET_KEY_HEALTH")
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 1
    assert "FAIL secret ALPACA-SECRET-KEY-HEALTH" in caplog.text


def test_fails_when_the_database_cannot_be_reached(monkeypatch, caplog):
    def broken():
        raise ConnectionError("no route to postgres")

    monkeypatch.setattr(selfcheck, "active_pots", broken)
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 1
    assert "FAIL database: ConnectionError" in caplog.text


def test_fails_when_no_pot_is_active(monkeypatch, caplog):
    monkeypatch.setattr(selfcheck, "active_pots", lambda: [])
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 1
    assert "FAIL database: LookupError" in caplog.text


def test_never_logs_a_secret_value(database_ok, caplog):
    with caplog.at_level(logging.DEBUG):
        selfcheck.run()
    assert "test-" not in caplog.text


def test_is_dispatched_from_the_cli(monkeypatch):
    monkeypatch.setattr(selfcheck, "run", lambda: 0)
    assert cli.main(["selfcheck"]) == 0
