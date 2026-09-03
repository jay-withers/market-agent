"""Postgres access, authenticated with an Entra token instead of a password.

The server has no password at all: `password_auth_enabled = false`, so
`administrator_login`/`administrator_password` are unset and no database
password exists in source, state or Key Vault. The username is the database
role — `uai-marketagent-dev` in Azure, your own UPN locally — and the password
is a short-lived access token.

Token expiry is the only genuinely tricky part, and it matters because the API
holds a pool open for hours while a token lasts about one. Two things handle it:

* a `psycopg.Connection` subclass fetches a token inside `connect()`, so every
  new connection authenticates with a fresh one rather than with whatever was
  valid when the pool was built; and
* `max_lifetime` retires connections well inside a token's life, so a
  long-idle connection is never handed out with credentials that expired while
  it sat in the pool.

`azure-identity` caches and refreshes internally, so the per-connect token call
is cheap. Pool freely: the connection-pool trap that made a long-lived pool
expensive belonged to Azure SQL's auto-pause, and this server is always on.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg import Connection
from psycopg_pool import ConnectionPool

from .settings import credential, settings

# The scope Azure Database for PostgreSQL accepts. Note it is the
# `database.windows.net` audience despite this not being Azure SQL.
POSTGRES_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"

# Comfortably inside a token's ~60 minutes, leaving room for a connection to be
# checked out and held for a while after it was created.
MAX_CONNECTION_LIFETIME_SECONDS = 1800


def access_token() -> str:
    return credential().get_token(POSTGRES_SCOPE).token


class EntraConnection(psycopg.Connection):
    """A connection that supplies a fresh Entra token as its password."""

    @classmethod
    def connect(cls, conninfo: str = "", **kwargs: Any) -> EntraConnection:
        kwargs["password"] = access_token()
        return super().connect(conninfo, **kwargs)  # type: ignore[return-value]


def uses_password() -> bool:
    """Whether to authenticate with a password rather than an Entra token.

    The Azure server has no password — `password_auth_enabled` is off — so an
    empty setting means "use a token". A local Postgres, which `docker compose`
    runs for the offline loop, has nothing else to offer.
    """
    return bool(settings().postgres_password)


def conninfo() -> str:
    cfg = settings()
    parts = [
        f"host={cfg.postgres_host}",
        f"port={cfg.postgres_port}",
        f"dbname={cfg.postgres_database}",
        f"user={cfg.postgres_user}",
    ]
    if uses_password():
        parts.append(f"password={cfg.postgres_password}")
        # A local container has no certificate worth verifying, and requiring
        # TLS there just fails the connection.
        parts.append("sslmode=prefer")
    else:
        # require, not verify-full: Azure terminates TLS with a public CA, but
        # the container image carries no CA bundle pinning and a certificate
        # change would take the agent down silently at 06:00 UTC.
        parts.append("sslmode=require")
    return " ".join(parts)


_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    """The process-wide connection pool, created on first use.

    Lazy rather than created at import: the agent and summary jobs import this
    module for its types and would otherwise open connections — and demand a
    credential — merely by being imported, which breaks `--help` and the tests.
    """
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo(),
            # Only wrap the connection when there is a token to inject; with a
            # password configured the subclass would replace it with an Entra
            # token the local server knows nothing about.
            connection_class=Connection if uses_password() else EntraConnection,
            min_size=0,
            max_size=4,
            max_lifetime=MAX_CONNECTION_LIFETIME_SECONDS,
            # The pool is opened here rather than at first checkout so a
            # credential or network failure surfaces at startup.
            open=True,
        )
    return _pool


def close_pool() -> None:
    """Release the pool. For the jobs, which are short-lived processes."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
