"""Configuration and secret resolution.

`Settings` is this app's own. Secret resolution comes from jay-withers/appkit
(`appkit.keyvault`), shared with career, finances, gym-log and the other agent;
fix a bug there, once. The wrappers below bind it to these settings and cache
it, so a test can clear them.

Every secret is read from an environment variable first, then `.env`, and only
then from Key Vault. That ordering is what makes a local run work with no
Azure involved, and it is why Terraform manages no Container Apps Key Vault
reference: a revision carrying one hard-fails if the secret is absent, whereas
this resolves at runtime and reports what is missing.

The name mapping is mechanical: `secret("RESEND-API-KEY")` reads
`$RESEND_API_KEY`, then `.env`, then the Key Vault secret named
`RESEND-API-KEY`. Key Vault forbids underscores in names, environment variables
conventionally forbid hyphens, so one of the two has to be rewritten.
"""

from __future__ import annotations

from decimal import Decimal
from functools import cache, lru_cache
from typing import Any

from appkit import keyvault

# Re-exported: the tests (and conftest's cache clearing) reach them here.
from appkit.keyvault import KEY_VAULT_SCOPE as KEY_VAULT_SCOPE
from appkit.keyvault import StaticTokenCredential as StaticTokenCredential
from appkit.keyvault import dotenv as dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Non-secret configuration, all of it injected by Terraform's `common_env`.

    Secrets deliberately do not live here — see `secret()` below. Anything in
    this class is safe in a log line.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "dev"

    # Where the DuckDB file lives (see db.py). With a blob URL, the file at
    # database_path is this process's working copy of the blob; without one it
    # is the database itself, which is what the tests and a plain local run use.
    # Terraform sets the blob URL; /tmp because the image runs as uid 10001 and
    # /app is not its to write.
    database_blob_url: str = ""
    database_path: str = "marketagent.duckdb"
    # Only for the Azurite emulator in docker compose, which takes its
    # well-known development key rather than an Entra token. Never set in Azure.
    azure_storage_connection_string: str = ""

    key_vault_uri: str = ""
    # The managed identity's client id. Empty locally, which is the signal to
    # fall back to DefaultAzureCredential.
    azure_client_id: str = ""
    # A pre-fetched Key Vault access token, for a container that has no `az`.
    # See StaticTokenCredential — short-lived and Key Vault-scoped, which is
    # why this is preferable to writing the API keys into a .env.
    azure_keyvault_token: str = ""

    applicationinsights_connection_string: str = ""

    # Paper orders are submitted by default. Set DRY_RUN=true to record
    # simulated fills without sending orders to Alpaca.
    dry_run: bool = False

    api_require_token: bool = False
    # The dashboard's origins for CORS, comma-separated: its azurecontainerapps.io
    # FQDN plus its custom domain, if it has one. Terraform seeds it and `make
    # deploy` keeps it current. Empty falls back to allowing any origin, which
    # is right for a local `docker compose` run and harmless for a read-only API
    # that returns no secret and sets no cookie.
    dashboard_origins: str = ""

    # The model cascade: something cheap screens a large batch of news, and
    # something capable reasons about what survives. One env var each.
    filter_model: str = "deepseek-flash"
    analysis_model: str = "deepseek-v4-pro"
    # low | high | max — DeepSeek's own reasoning_effort scale, sent only on
    # the analysis stage; the filter stage disables reasoning outright.
    analysis_effort: str = "high"

    # A hard ceiling on what one agent run may spend with the model, checked as
    # each call's cost lands. It exists to stop a runaway loop, not to shape
    # ordinary spending — DeepSeek's rates run roughly an order of magnitude
    # below the Anthropic cascade this replaced. Crossing it fails the run,
    # which closes the `agent_runs` row with the reason — an unbounded bill
    # accumulating quietly is the outcome worth preventing.
    #
    # `gt=0` rather than treating 0 as "no ceiling": a guard that switches off
    # at the value which reads like "spend nothing" is the wrong footgun to
    # leave lying around. Raise it if you mean to spend more.
    max_run_cost_usd: Decimal = Field(default=Decimal("1.00"), gt=0)

    # At most this many analyses per run, the expensive stage (~$0.008 each).
    # What bounds the cost of a 500-name watchlist: sp500 sets 30 in
    # Terraform's local.pots. Unset means every ticker with relevant news is
    # analysed, as before. Which ones make the cut is `agent._ranked`.
    max_analyses_per_run: int | None = Field(default=None, gt=0)

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

    # Resend refuses a `from` on an unverified domain, so the default is their
    # shared testing sender — which only delivers to the address that owns the
    # Resend account. A real domain replaces it later.
    summary_email_from: str = "MarketAgent <onboarding@resend.dev>"

    # The *recipient* is deliberately not here. It lives in Key Vault, read via
    # optional_secret("SUMMARY-EMAIL-TO"), for two reasons: this repository is
    # public and so are terraform/environments/*.tfvars, so a personal address
    # in either would be committed permanently; and a value read at runtime can
    # be changed with `az keyvault secret set` and picked up by the next run,
    # with no new revision. Absent means compose and store the summary but send
    # nothing, which is the right default — a job that emails on every
    # development run is worse than one switched on deliberately.

    # Honest proxies, since Alpaca only covers US-listed instruments: SPY for
    # the S&P 500, VT for a global index, and EWU as a UK proxy — *not* the
    # FTSE 100, and labelled as a proxy wherever it is shown.
    benchmark_symbols: str = "SPY,VT,EWU"
    # A savings account at 5% APR, compounded daily and computed arithmetically
    # rather than fetched. The "or leave it in the bank" arm of the experiment.
    cash_benchmark_apr_pct: Decimal = Decimal("5.0")

    log_level: str = Field(default="INFO")


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()


@cache
def secret(name: str) -> str:
    """Resolve a secret by its hyphenated Key Vault name, raising if it is absent."""
    return keyvault.resolve(name, key_vault_uri=settings().key_vault_uri, credential=credential)


@cache
def optional_secret(name: str) -> str | None:
    """Resolve a secret that is allowed not to exist, returning None if it does not.

    An authentication or network error still raises: "the credential is broken"
    must not look like "not configured".
    """
    return keyvault.resolve_optional(
        name, key_vault_uri=settings().key_vault_uri, credential=credential
    )


@lru_cache(maxsize=1)
def credential() -> Any:
    """The credential for Key Vault and the database blob (`appkit.keyvault.make_credential`)."""
    cfg = settings()
    return keyvault.make_credential(
        keyvault_token=cfg.azure_keyvault_token, client_id=cfg.azure_client_id
    )
