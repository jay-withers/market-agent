"""Test-wide fixtures."""

from __future__ import annotations

import pytest

from investagent import settings as settings_module


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """The retry backoff is real seconds; the suite must not wait them out."""
    monkeypatch.setattr("investagent.fetch.time.sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def fake_secrets(monkeypatch):
    """Resolve every secret from the environment, never from Key Vault.

    `secret()` reads an env var before falling back to the vault, so setting
    these guarantees no test can reach Azure, need a credential, or fail on a
    machine that has neither — even though the module under test is doing
    exactly what it does in production.

    Both caches have to be cleared around the test: `settings()` and `secret()`
    memoise, so a value read before these were set would otherwise persist.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("ALPACA_API_KEY", "test-alpaca-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-alpaca-secret")
    monkeypatch.setenv("RESEND_API_KEY", "test-resend-key")
    # Must stay empty: a configured vault URI would let a missing env var fall
    # through to a real network call.
    monkeypatch.delenv("KEY_VAULT_URI", raising=False)

    settings_module.settings.cache_clear()
    settings_module.secret.cache_clear()
    settings_module.optional_secret.cache_clear()
    yield
    settings_module.settings.cache_clear()
    settings_module.secret.cache_clear()
    settings_module.optional_secret.cache_clear()
