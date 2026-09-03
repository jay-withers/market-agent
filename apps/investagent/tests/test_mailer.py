"""Tests for the daily email.

The governing rule is that a mail failure must not lose the day's write-up: the
summary is stored either way and the dashboard renders it from the database, so
`send` reports what happened rather than raising.
"""

from __future__ import annotations

import json

from investagent import settings as settings_module
from investagent.mailer import send
from tests.helpers import json_client, sequence_client


def _configure(monkeypatch, to: str = "me@example.com") -> None:
    # The recipient resolves through optional_secret, which reads the
    # environment before Key Vault — so the same variable name still works.
    if to:
        monkeypatch.setenv("SUMMARY_EMAIL_TO", to)
    else:
        monkeypatch.delenv("SUMMARY_EMAIL_TO", raising=False)
    monkeypatch.setenv("SUMMARY_EMAIL_FROM", "InvestAgent <bot@example.com>")
    settings_module.settings.cache_clear()
    settings_module.secret.cache_clear()
    settings_module.optional_secret.cache_clear()


def test_no_recipient_means_skipped_not_failed(monkeypatch):
    """The default: a job that emails on every development run is worse.

    Absence must be `skipped`, not a failure — the recipient is an opt-in
    switch, not something the job cannot run without.
    """
    _configure(monkeypatch, to="")

    result = send("subject", "<p>html</p>", "text")

    assert result.status == "skipped"
    assert result.provider_id is None


def test_a_successful_send_records_the_provider_id(monkeypatch):
    _configure(monkeypatch)

    result = send("subject", "<p>html</p>", "text", client=json_client({"id": "re_123"}))

    assert result.status == "sent"
    assert result.provider_id == "re_123"


def test_the_request_carries_the_bearer_token_and_both_bodies(monkeypatch):
    _configure(monkeypatch)
    captured: list = []

    send("Daily", "<p>html</p>", "text", client=json_client({"id": "x"}, capture=captured))

    request = captured[0]
    assert request.headers["authorization"] == "Bearer test-resend-key"
    body = json.loads(request.read())
    assert body["from"] == "InvestAgent <bot@example.com>"
    assert body["to"] == ["me@example.com"]
    assert body["subject"] == "Daily"
    assert body["html"] == "<p>html</p>"
    assert body["text"] == "text"


def test_several_recipients_are_split_and_trimmed(monkeypatch):
    _configure(monkeypatch, to="a@example.com, b@example.com")
    captured: list = []

    send("s", "h", "t", client=json_client({"id": "x"}, capture=captured))

    assert json.loads(captured[0].read())["to"] == ["a@example.com", "b@example.com"]


def test_a_provider_failure_is_reported_not_raised(monkeypatch):
    """Losing the day's summary because a mail provider had a bad minute would
    be a poor trade."""
    _configure(monkeypatch)

    result = send("s", "h", "t", client=sequence_client([(422, {"message": "bad from"})]))

    assert result.status == "failed"
    assert "422" in result.error


def test_a_send_is_never_retried(monkeypatch):
    """post_json does not retry, so a duplicate email is impossible."""
    _configure(monkeypatch)
    calls: list = []

    send("s", "h", "t", client=sequence_client([(503, {}), (200, {"id": "x"})], calls=calls))

    assert len(calls) == 1
