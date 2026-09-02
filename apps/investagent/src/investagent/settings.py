"""Configuration and secret resolution.

Every secret is read from an environment variable first and only then from Key
Vault. That ordering is what makes `docker compose up` work against a local
`.env` with no Azure involved at all, and it is why Terraform does not manage
Container Apps Key Vault references: a revision carrying a Key Vault reference
hard-fails if the secret is absent, and CLAUDE.md is explicit that Terraform
must never own secret values.

The name mapping is mechanical: `secret("ANTHROPIC-API-KEY")` reads
`$ANTHROPIC_API_KEY`, falling back to the Key Vault secret named
`ANTHROPIC-API-KEY`. Key Vault forbids underscores in names, environment
variables conventionally forbid hyphens, so one of the two has to be rewritten.
"""

from __future__ import annotations

import os
import time
from functools import cache, lru_cache
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Non-secret configuration, all of it injected by Terraform's `common_env`.

    Secrets deliberately do not live here — see `secret()` below. Anything in
    this class is safe in a log line.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "dev"

    # Populated from locals.container-apps.tf. The defaults are what a local
    # `docker compose` run gets; nothing here is environment-specific enough to
    # be worth a required variable.
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_database: str = "investagent"
    postgres_user: str = "postgres"
    # Empty against Azure, which has no password at all — that is the signal to
    # authenticate with an Entra token instead. Set only for a local Postgres,
    # which `docker compose` runs with ordinary password auth.
    postgres_password: str = ""

    key_vault_uri: str = ""
    # The managed identity's client id. Empty locally, which is the signal to
    # fall back to DefaultAzureCredential.
    azure_client_id: str = ""
    # A pre-fetched Key Vault access token, for a container that has no `az`.
    # See StaticTokenCredential — short-lived and Key Vault-scoped, which is
    # why this is preferable to writing the API keys into a .env.
    azure_keyvault_token: str = ""

    applicationinsights_connection_string: str = ""

    # Runs the full decision path and persists everything without submitting an
    # order. The safe first deploy, so it defaults on: a mistake here costs
    # nothing, whereas the reverse default trades on the first run.
    dry_run: bool = True

    api_require_token: bool = False
    # The dashboard's origin, for CORS. Terraform passes the dashboard app's
    # ingress FQDN; empty falls back to allowing any origin, which is right for
    # a local `docker compose` run and harmless for a read-only API that
    # returns no secret and sets no cookie.
    dashboard_origin: str = ""

    # The model cascade: something cheap screens a large batch of news, and
    # something capable reasons about what survives. One env var each.
    filter_model: str = "claude-haiku-4-5"
    analysis_model: str = "claude-sonnet-5"
    # low | medium | high | xhigh | max. Only meaningful for the analysis
    # model — `effort` errors on the pre-4.6 filter model.
    analysis_effort: str = "high"

    # Alpaca. The paper host by default and nowhere else: the live host takes
    # the same credentials and the same request shapes, so a wrong base URL
    # would place real orders with no other symptom.
    alpaca_trading_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_data_base_url: str = "https://data.alpaca.markets"
    # `sip` is the consolidated tape and the right source for a closing price;
    # this account has it. Verified against the alternative: `iex` reports one
    # exchange only, which on NVDA meant 4.9M shares against SIP's 157M and a
    # close of 224.435 against 224.41. Set explicitly rather than left to the
    # account default so that losing the subscription fails the run visibly
    # instead of silently downgrading which prices the experiment ran against.
    alpaca_data_feed: str = "sip"

    log_level: str = Field(default="INFO")


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()


@cache
def secret(name: str) -> str:
    """Resolve a secret by its hyphenated Key Vault name.

    Cached: the agent job reads each secret once per process, but the API is
    long-lived and a Key Vault round trip per request would be both slow and
    rate-limited. The consequence is that rotating a secret needs a new
    revision, which for a job that runs once a day is not a real constraint.
    """
    from_env = os.environ.get(name.replace("-", "_").upper())
    if from_env:
        return from_env

    uri = settings().key_vault_uri
    if not uri:
        raise RuntimeError(
            f"{name} is not set and no KEY_VAULT_URI is configured. "
            f"Set ${name.replace('-', '_').upper()} locally, or "
            f"`az keyvault secret set --name {name}` for a deployed environment."
        )

    # Imported here rather than at module scope so the risk engine, the models
    # and the tests never need the Azure SDK installed or a credential present.
    from azure.keyvault.secrets import SecretClient

    client = SecretClient(vault_url=uri, credential=credential())
    return client.get_secret(name).value or ""


# The scope a Key Vault data-plane token is issued for. `az` calls the same
# thing `--resource https://vault.azure.net`.
KEY_VAULT_SCOPE = "https://vault.azure.net/.default"


class StaticTokenCredential:
    """A credential wrapping one pre-fetched access token.

    Exists for the containerised local loop. The application image carries no
    `az`, so `DefaultAzureCredential` has nothing to fall back to and a
    container cannot reach Key Vault on its own — the alternative was writing
    every API key to a `.env` in plaintext. Passing in a token instead is
    strictly better: it expires in about an hour, it is scoped to Key Vault
    alone, and no long-lived secret touches the filesystem.

    Never used in Azure, where the managed identity is available directly.
    """

    def __init__(self, token: str, scope: str = KEY_VAULT_SCOPE) -> None:
        self._token = token
        self._scope = scope
        # az does not report the expiry in a form worth parsing here, and the
        # SDK only uses this to decide whether to refresh — which this
        # credential cannot do. An hour matches the real lifetime; an expired
        # token then fails as a 401 from Key Vault, which is the honest
        # outcome.
        self._expires_on = int(time.time()) + 3600

    def _check(self, scopes: tuple[str, ...]) -> None:
        """Refuse a scope this token was not issued for.

        Matters because `credential()` also serves Postgres token
        authentication. Handing a Key Vault token to Postgres would fail as
        "password authentication failed", which CLAUDE.md records as one of the
        hardest errors in this project to read correctly.
        """
        if scopes and self._scope not in scopes:
            raise ValueError(f"this token is scoped to {self._scope}, not {', '.join(scopes)}")

    def get_token(self, *scopes: str, **_kwargs: Any) -> Any:
        from azure.core.credentials import AccessToken

        self._check(scopes)
        return AccessToken(self._token, self._expires_on)

    def get_token_info(self, *scopes: str, **_kwargs: Any) -> Any:
        """The newer protocol; some SDK versions call this instead."""
        from azure.core.credentials import AccessTokenInfo

        self._check(scopes)
        return AccessTokenInfo(self._token, self._expires_on)


@lru_cache(maxsize=1)
def credential():
    """The credential for Key Vault and for Postgres tokens.

    Three cases, in priority order:

    1. A pre-fetched Key Vault token from the environment — the containerised
       local loop, where there is no `az` to fall back to.
    2. The user-assigned identity, named explicitly. In Azure this must be
       explicit: `DefaultAzureCredential` would find the same identity
       eventually, but a container app with more than one identity attached
       picks unpredictably, and the failure looks like a permissions problem
       rather than a wrong-identity one.
    3. `DefaultAzureCredential`, which picks up a developer's `az login`.
    """
    cfg = settings()

    if cfg.azure_keyvault_token:
        return StaticTokenCredential(cfg.azure_keyvault_token)

    if cfg.azure_client_id:
        from azure.identity import ManagedIdentityCredential

        return ManagedIdentityCredential(client_id=cfg.azure_client_id)

    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()
