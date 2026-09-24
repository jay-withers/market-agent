"""Test-wide fixtures."""

from __future__ import annotations

import os
import pathlib
import re
from collections.abc import Iterator
from typing import Any

import pytest

from marketagent import alpaca_api
from marketagent import settings as settings_module
from tests.helpers import POTS


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """The retry backoff is real seconds; the suite must not wait them out."""
    monkeypatch.setattr("marketagent.fetch.time.sleep", lambda _s: None)


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
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    # Every pot's secrets, named the way accounts.secret_suffix derives them —
    # a test exercising any pot (or several) needs neither Azure nor a real key.
    for pot in POTS:
        suffix = pot.upper()
        monkeypatch.setenv(f"DEEPSEEK_API_KEY_{suffix}", f"test-deepseek-key-{pot}")
        monkeypatch.setenv(f"ALPACA_API_KEY_{suffix}", f"test-alpaca-key-{pot}")
        monkeypatch.setenv(f"ALPACA_SECRET_KEY_{suffix}", f"test-alpaca-secret-{pot}")
    monkeypatch.setenv("RESEND_API_KEY", "test-resend-key")
    # Must stay empty: a configured vault URI would let a missing env var fall
    # through to a real network call.
    monkeypatch.delenv("KEY_VAULT_URI", raising=False)
    # Same reasoning: a developer with this set in their own environment would
    # otherwise have the suite configure a real exporter and ship spans.
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)

    settings_module.settings.cache_clear()
    settings_module.secret.cache_clear()
    settings_module.optional_secret.cache_clear()
    yield
    settings_module.settings.cache_clear()
    settings_module.secret.cache_clear()
    settings_module.optional_secret.cache_clear()


@pytest.fixture(autouse=True)
def default_alpaca_account():
    """`alpaca_api.headers()` refuses to answer until an account is selected.

    Most tests don't care which pot they exercise, so this picks one by
    default — a test that does care (e.g. asserting two pots' credentials are
    actually kept apart) calls `use_account` itself,
    which simply overrides this for its own duration. Reset after every test:
    `_account` is process-global module state, and a test that left it set
    must not leak into the next one.
    """
    alpaca_api.use_account(POTS[0])
    yield
    alpaca_api._account = None


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------
#
# repository.py and queries.py are hand-written SQL, and until these fixtures
# existed nothing executed any of it: the first run of a statement was the
# deployed job at 06:00 UTC, where a typo costs a day of the experiment. They
# are the one part of the suite that needs a real server — psycopg mocks would
# assert the mock, and the errors worth catching here (a wrong column name, a
# foreign key, a NUMERIC that will not accept a value) are exactly the ones a
# mock cannot raise.
#
# A missing database *skips* rather than fails. The suite has to stay runnable
# on a clone with no Docker, which is also why `make test` does not set the
# variable and `make test-db` does.

# Named for the shared CI workflow, which exports exactly this when its
# `postgres` input is set. Deliberately not DATABASE_URL: a developer with that
# pointing at something real would have the suite CREATE and DROP databases on
# it, and the fixtures below do both.
TEST_DSN_ENV = "POSTGRES_TEST_DSN"

# Created and dropped per session rather than reusing the compose database, so
# a test run never touches rows a developer was looking at.
SCRATCH_DATABASE = "marketagent_test"


def _migrations() -> list[pathlib.Path]:
    """The numbered migrations, in the order Invoke-DbSql.ps1 applies them."""
    return sorted((pathlib.Path(__file__).parents[3] / "sql").glob("0*.sql"))


def _sql_only(text: str) -> str:
    r"""Drop psql meta-commands, which the server cannot parse.

    Each migration ends with `\echo` lines and a verification query, for the
    benefit of whoever runs it by hand. Stripping them is what lets psycopg
    apply the files directly, so these fixtures need no `psql` on PATH — worth
    having, since CLAUDE.md records psql going missing across a dev container
    rebuild. Nothing schema-bearing starts with a backslash.
    """
    return "\n".join(line for line in text.splitlines() if not line.startswith("\\"))


@pytest.fixture(scope="session")
def database_dsn() -> Iterator[str]:
    """A scratch database with every migration applied.

    Session-scoped: applying seven migrations per test would dominate the run.
    """
    admin_dsn = os.environ.get(TEST_DSN_ENV)
    if not admin_dsn:
        pytest.skip(f"{TEST_DSN_ENV} is not set; run `make test-db` for a local Postgres")

    import psycopg

    # autocommit because CREATE/DROP DATABASE cannot run inside a transaction.
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DATABASE}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{SCRATCH_DATABASE}"')

    scratch_dsn = re.sub(r"/[^/?]*(\?|$)", f"/{SCRATCH_DATABASE}\\1", admin_dsn, count=1)
    with psycopg.connect(scratch_dsn) as conn:
        for path in _migrations():
            conn.execute(_sql_only(path.read_text()))
        conn.commit()

    yield scratch_dsn

    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DATABASE}" WITH (FORCE)')


@pytest.fixture
def conn(database_dsn: str) -> Iterator[Any]:
    """A connection whose work is rolled back when the test ends.

    One connection for the whole session, isolated by rollback rather than by
    rebuilding the schema: no function in repository.py commits — the caller
    owns the transaction, which is what lets the agent put a decision and its
    trade in one — so discarding the transaction discards the test.
    """
    import psycopg

    global _session_conn
    if _session_conn is None:
        _session_conn = psycopg.connect(database_dsn)
    # Before as well as after: a test that failed mid-statement leaves the
    # connection in an aborted state, and the next one should not inherit it.
    _session_conn.rollback()
    yield _session_conn
    _session_conn.rollback()


_session_conn: Any = None


@pytest.fixture(scope="session", autouse=True)
def _close_session_conn() -> Iterator[None]:
    yield
    global _session_conn
    if _session_conn is not None:
        _session_conn.close()
        _session_conn = None
