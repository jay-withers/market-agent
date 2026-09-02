"""Tests for secret resolution and the credential chain."""

from __future__ import annotations

import pytest

from investagent import settings as settings_module
from investagent.settings import KEY_VAULT_SCOPE, StaticTokenCredential, secret


def _reset() -> None:
    settings_module.settings.cache_clear()
    settings_module.secret.cache_clear()


def test_a_secret_comes_from_the_environment_first(monkeypatch):
    """Env-first is what makes the local loop work with no Azure at all."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    _reset()

    assert secret("ANTHROPIC-API-KEY") == "from-env"


def test_the_hyphenated_name_maps_to_an_underscored_uppercase_variable(monkeypatch):
    monkeypatch.setenv("ALPACA_SECRET_KEY", "abc")
    _reset()

    assert secret("ALPACA-SECRET-KEY") == "abc"


def test_a_missing_secret_with_no_vault_names_both_ways_to_fix_it(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("KEY_VAULT_URI", raising=False)
    _reset()

    with pytest.raises(RuntimeError) as caught:
        secret("ANTHROPIC-API-KEY")

    # Both routes named, because the error is the only place a reader finds out
    # there are two.
    assert "$ANTHROPIC_API_KEY" in str(caught.value)
    assert "az keyvault secret set" in str(caught.value)


# ---------------------------------------------------------------------------
# The credential chain
# ---------------------------------------------------------------------------


def test_a_pre_fetched_token_is_used_when_present(monkeypatch):
    """The containerised loop: no `az` in the image, so a token is passed in."""
    monkeypatch.setenv("AZURE_KEYVAULT_TOKEN", "a-token")
    _reset()

    assert isinstance(settings_module.credential.__wrapped__(), StaticTokenCredential)


def test_the_static_credential_returns_the_token_it_was_given():
    cred = StaticTokenCredential("a-token")

    assert cred.get_token(KEY_VAULT_SCOPE).token == "a-token"
    assert cred.get_token_info(KEY_VAULT_SCOPE).token == "a-token"


def test_the_static_credential_reports_an_expiry_in_the_future():
    import time

    assert StaticTokenCredential("a-token").get_token(KEY_VAULT_SCOPE).expires_on > time.time()


@pytest.mark.parametrize("method", ["get_token", "get_token_info"])
def test_the_static_credential_refuses_a_scope_it_was_not_issued_for(method):
    """A Key Vault token handed to Postgres fails as "password authentication
    failed", which is one of the hardest errors in this project to read."""
    cred = StaticTokenCredential("a-token")

    with pytest.raises(ValueError, match="scoped to"):
        getattr(cred, method)("https://ossrdbms-aad.database.windows.net/.default")


def test_the_static_credential_accepts_a_request_with_no_scope():
    """Some SDK paths call get_token() bare; refusing that would break them."""
    assert StaticTokenCredential("a-token").get_token().token == "a-token"
