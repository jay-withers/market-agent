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
from functools import cache, lru_cache

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

    key_vault_uri: str = ""
    # The managed identity's client id. Empty locally, which is the signal to
    # fall back to DefaultAzureCredential.
    azure_client_id: str = ""

    applicationinsights_connection_string: str = ""

    # Runs the full decision path and persists everything without submitting an
    # order. The safe first deploy, so it defaults on: a mistake here costs
    # nothing, whereas the reverse default trades on the first run.
    dry_run: bool = True

    api_require_token: bool = False

    # The model cascade: something cheap screens a large batch of news, and
    # something capable reasons about what survives. One env var each.
    filter_model: str = "claude-haiku-4-5"
    analysis_model: str = "claude-sonnet-5"
    # low | medium | high | xhigh | max. Only meaningful for the analysis
    # model — `effort` errors on the pre-4.6 filter model.
    analysis_effort: str = "high"

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


@lru_cache(maxsize=1)
def credential():
    """The credential for Key Vault and for Postgres tokens.

    In Azure this must be the user-assigned identity explicitly:
    `DefaultAzureCredential` would find the same identity eventually, but a
    container app with more than one identity attached picks unpredictably, and
    the failure looks like a permissions problem rather than a wrong-identity
    one.
    """
    client_id = settings().azure_client_id
    if client_id:
        from azure.identity import ManagedIdentityCredential

        return ManagedIdentityCredential(client_id=client_id)

    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()
