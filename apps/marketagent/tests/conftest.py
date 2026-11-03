"""Test-wide fixtures."""

from __future__ import annotations

import pathlib
from collections.abc import Iterator
from typing import Any

import pytest

from marketagent import alpaca_api, db
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
# The database
# ---------------------------------------------------------------------------
#
# repository.py and queries.py are hand-written SQL, and these fixtures are
# what executes it: a mock would assert the mock, and the errors worth catching
# (a wrong column name, an ON CONFLICT with no matching constraint, a DECIMAL
# that will not take a value) are exactly the ones it cannot raise. DuckDB is
# embedded, so the real engine runs in every `make test` and in CI, with no
# server to start.


@pytest.fixture(scope="session")
def database_path(tmp_path_factory) -> pathlib.Path:
    """A scratch database file with every schema file applied, once per session."""
    path = tmp_path_factory.mktemp("db") / "marketagent.duckdb"
    db.open_file(path).close()
    return path


@pytest.fixture
def conn(database_path: pathlib.Path) -> Iterator[Any]:
    """A connection whose work is rolled back when the test ends.

    One connection for the whole session, isolated by rollback rather than by
    rebuilding the schema: no function in repository.py commits — the caller
    owns the transaction, which is what lets the agent put a decision and its
    trade in one — so discarding the transaction discards the test.
    """
    global _session_conn
    if _session_conn is None:
        _session_conn = db.open_file(database_path)
    # Before as well as after: a test that failed mid-statement leaves the
    # transaction aborted, and the next one should not inherit it.
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
