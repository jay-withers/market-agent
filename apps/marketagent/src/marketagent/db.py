"""The database: one DuckDB file, kept as a blob in the project's storage account.

This replaced a PostgreSQL Flexible Server, which billed every hour for a
database the jobs touch for a few minutes a day. DuckDB rather than SQLite
because the SQL was written for Postgres: DuckDB keeps DECIMAL exact through
sum() and arithmetic ("money is Decimal, never float"), and it understands
LATERAL, FILTER, generate_series and `::` casts, so most statements carried
over unchanged.

**Each `database().connection()` block is one transaction on the blob.** A
writer takes the blob's lease, downloads the file if another process changed
it since, runs the block, commits on a clean exit, uploads the file and
releases the lease. The three pot jobs share a cron time, so they queue on the
lease block by block, much as they queued on Postgres row locks. Nothing slow
(no model call) happens inside a block, so a block holds the lease for seconds.
A crash mid-block uploads nothing and the lease lapses after `LEASE_SECONDS`:
that is the rollback.

The API is a reader. It never takes the lease. It keeps a local copy, checks
the blob's ETag at most every `READ_REFRESH_SECONDS`, and opens the copy
read-only, so a request never waits on a job.

With no blob configured (the tests, a plain local run) the file at
`DATABASE_PATH` is the database itself.

The wrapper speaks enough of psycopg's interface that repository.py and
queries.py kept their shape: `%s`/`%(name)s` placeholders, `conn.execute()`
returning a cursor, `cursor(row_factory=dict_row)`, `commit()`/`rollback()`,
and JSON columns coming back parsed.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import duckdb

from .settings import credential, settings

logger = logging.getLogger(__name__)

# The schema files, applied in order to any database that lacks them.
SCHEMA_DIR = Path(__file__).parent / "schema"

# Azure's lease length must be 15 to 60 seconds. The renewal thread keeps it
# alive while a block runs; a process that dies lets it lapse within a minute.
LEASE_SECONDS = 60
LEASE_RENEW_SECONDS = 20

# How long a writer waits for another writer's lease before failing. Three pots
# starting together queue for seconds, not minutes; ten minutes is for a stuck
# lease, which then fails the job visibly rather than hanging it.
LEASE_WAIT_SECONDS = 600
LEASE_RETRY_SECONDS = 2

# How stale the API's copy may be. Jobs write a few times a day, and checking
# the ETag on every request would be a storage call per page load.
READ_REFRESH_SECONDS = 30


def dict_row(columns: Sequence[str], row: tuple) -> dict[str, Any]:
    """Row factory returning a dict per row, as psycopg.rows.dict_row does."""
    return dict(zip(columns, row, strict=True))


def Json(value: Any) -> str:
    """Wrap a value for a JSON column."""
    return json.dumps(value)


_NAMED = re.compile(r"%\((\w+)\)s")


def _translate(sql: str, params: Any) -> tuple[str, Any]:
    """psycopg placeholders to DuckDB's: `%s` -> `?`, `%(name)s` -> `$name`.

    Named parameters are filtered to the names the statement uses, because
    DuckDB refuses unused ones and the callers share one dict across several
    statements.
    """
    if isinstance(params, dict):
        names = set(_NAMED.findall(sql))
        sql = _NAMED.sub(r"$\1", sql)
        params = {k: v for k, v in params.items() if k in names}
    else:
        sql = sql.replace("%s", "?")
    return sql.replace("%%", "%"), params


class Cursor:
    """A cursor whose results are read in full on execute.

    DuckDB has one result per connection: a second execute discards the first
    one's unread rows. Reading eagerly keeps psycopg's behaviour, where
    `conn.execute(a)` and `conn.execute(b)` can be fetched in any order. Every
    result here is small.
    """

    def __init__(self, conn: Connection, row_factory: Callable | None = None) -> None:
        self._conn = conn
        self._row_factory = row_factory
        self._rows: list[Any] = []
        self.description: list[tuple] | None = None

    def __enter__(self) -> Cursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def __iter__(self) -> Iterator[Any]:
        return iter(self.fetchall())

    def execute(self, sql: str, params: Any = None) -> Cursor:
        raw = self._conn._begin()
        sql, params = _translate(sql, params)
        if params:
            raw.execute(sql, params)
        else:
            raw.execute(sql)
        self.description = raw.description
        self._rows = self._read(raw) if raw.description else []
        return self

    def executemany(self, sql: str, seq: Sequence[Any]) -> None:
        for params in seq:
            self.execute(sql, params)

    def _read(self, raw: duckdb.DuckDBPyConnection) -> list[Any]:
        description = raw.description or []
        names = [d[0] for d in description]
        # JSON comes back from DuckDB as text; psycopg parsed jsonb, and the
        # callers read the dicts.
        json_columns = [i for i, d in enumerate(description) if str(d[1]) == "JSON"]
        rows = raw.fetchall()
        if json_columns:
            rows = [
                tuple(
                    json.loads(v) if i in json_columns and v is not None else v
                    for i, v in enumerate(row)
                )
                for row in rows
            ]
        if self._row_factory is not None:
            return [self._row_factory(names, row) for row in rows]
        return rows

    def fetchone(self) -> Any:
        return self._rows.pop(0) if self._rows else None

    def fetchall(self) -> list[Any]:
        rows, self._rows = self._rows, []
        return rows


class Connection:
    """A DuckDB connection with psycopg's transaction model.

    psycopg opens a transaction on the first statement and holds it until
    commit or rollback; DuckDB autocommits unless told otherwise. `_begin` is
    what makes the two agree, so a repository function that writes two rows
    still writes both or neither.
    """

    def __init__(self, raw: duckdb.DuckDBPyConnection) -> None:
        self._raw = raw
        self._in_transaction = False

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, exc_type: object, *_rest: object) -> None:
        """As psycopg's: commit on success, roll back on an error, then close."""
        if exc_type is None:
            self.commit()
        self.close()

    def _begin(self) -> duckdb.DuckDBPyConnection:
        if not self._in_transaction:
            self._raw.execute("BEGIN TRANSACTION")
            self._in_transaction = True
        return self._raw

    def cursor(self, row_factory: Callable | None = None) -> Cursor:
        return Cursor(self, row_factory)

    def execute(self, sql: str, params: Any = None) -> Cursor:
        return self.cursor().execute(sql, params)

    def commit(self) -> None:
        if self._in_transaction:
            self._raw.execute("COMMIT")
            self._in_transaction = False

    def rollback(self) -> None:
        if self._in_transaction:
            self._raw.execute("ROLLBACK")
            self._in_transaction = False

    def close(self) -> None:
        self.rollback()
        self._raw.close()


def open_file(path: str | Path, read_only: bool = False) -> Connection:
    """Open a database file, applying any schema file it lacks."""
    raw = duckdb.connect(str(path), read_only=read_only)
    # The jobs' dates are UTC days, as they were on the Azure server.
    raw.execute("SET TimeZone = 'UTC'")
    conn = Connection(raw)
    if not read_only:
        migrate(conn)
    return conn


def migrate(conn: Connection) -> list[str]:
    """Apply every schema file not yet recorded in schema_migrations."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    done = {r[0] for r in conn.execute("SELECT filename FROM schema_migrations").fetchall()}
    applied = []
    for path in sorted(SCHEMA_DIR.glob("*.sql")):
        if path.name not in done:
            conn._begin().execute(path.read_text())
            applied.append(path.name)
    conn.commit()
    if applied:
        logger.info("applied schema files: %s", ", ".join(applied))
    return applied


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


class Database:
    """The process's database: a local file, optionally mirrored to a blob."""

    def __init__(self, path: str | Path, blob: Any = None, read_only: bool = False) -> None:
        self.path = Path(path)
        self.blob = blob
        self.read_only = read_only
        self._lock = threading.Lock()
        self._etag: str | None = None
        self._checked = 0.0

    @contextlib.contextmanager
    def connection(self) -> Iterator[Connection]:
        """One transaction: committed on a clean exit, rolled back on an error."""
        if self.read_only:
            with self._lock:
                self._refresh()
            conn = open_file(self.path, read_only=True)
            try:
                yield conn
            finally:
                conn.close()
            return

        with self._lock, self._leased() as lease:
            before = _digest(self.path)
            conn = open_file(self.path)
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()
            if lease is not None and _digest(self.path) != before:
                self._upload(lease)

    def _refresh(self) -> None:
        """Bring the reader's copy up to date, at most every READ_REFRESH_SECONDS."""
        if self.blob is None:
            return
        if self.path.exists() and time.monotonic() - self._checked < READ_REFRESH_SECONDS:
            return
        etag = self.blob.get_blob_properties().etag
        if etag != self._etag or not self.path.exists():
            self._download()
            self._etag = etag
        self._checked = time.monotonic()

    def _download(self, lease: Any = None) -> None:
        partial = self.path.with_suffix(".download")
        with partial.open("wb") as f:
            self.blob.download_blob(lease=lease).readinto(f)
        # A replace, so a reader that has the old file open keeps reading it.
        os.replace(partial, self.path)
        Path(f"{self.path}.wal").unlink(missing_ok=True)

    def _upload(self, lease: Any) -> None:
        with self.path.open("rb") as f:
            result = self.blob.upload_blob(f, overwrite=True, lease=lease)
        self._etag = result["etag"]

    @contextlib.contextmanager
    def _leased(self) -> Iterator[Any]:
        """Hold the blob's lease for the block, with the local copy current."""
        if self.blob is None:
            yield None
            return

        from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
        from azure.storage.blob import BlobLeaseClient

        lease = BlobLeaseClient(self.blob)
        deadline = time.monotonic() + LEASE_WAIT_SECONDS
        while True:
            try:
                lease.acquire(lease_duration=LEASE_SECONDS)
                break
            except ResourceNotFoundError:
                # A new environment: the first writer creates an empty file.
                # if_none_match makes a second writer racing it lose cleanly.
                with contextlib.suppress(ResourceExistsError):
                    self.blob.upload_blob(b"", overwrite=False)
            except ResourceExistsError:
                # Another writer holds the lease (409 LeaseAlreadyPresent).
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"the database blob was leased for over {LEASE_WAIT_SECONDS}s"
                    ) from None
                time.sleep(LEASE_RETRY_SECONDS)

        stop = threading.Event()

        def renew() -> None:
            while not stop.wait(LEASE_RENEW_SECONDS):
                lease.renew()

        renewer = threading.Thread(target=renew, name="database-lease", daemon=True)
        renewer.start()
        try:
            props = self.blob.get_blob_properties(lease=lease)
            if props.size == 0:
                # The empty placeholder: start a new file and let migrate()
                # build the schema in it.
                self.path.unlink(missing_ok=True)
            elif props.etag != self._etag or not self.path.exists():
                self._download(lease)
            self._etag = props.etag
            yield lease
        finally:
            stop.set()
            renewer.join()
            lease.release()


def blob_client(url: str) -> Any:
    """The blob client for DATABASE_BLOB_URL.

    In Azure, the managed identity's credential. Against the Azurite emulator
    in docker compose, its well-known connection string instead, since Azurite
    takes no Entra token.
    """
    from azure.storage.blob import BlobClient

    connection_string = settings().azure_storage_connection_string
    if connection_string:
        container, _, name = urlparse(url).path.lstrip("/").partition("/")
        # Azurite puts the account name first in the path.
        if container == "devstoreaccount1":
            container, _, name = name.partition("/")
        client = BlobClient.from_connection_string(connection_string, container, name)
        # Terraform makes the container in Azure; a fresh Azurite has none.
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.blob import ContainerClient

        with contextlib.suppress(ResourceExistsError):
            ContainerClient.from_connection_string(connection_string, container).create_container()
        return client
    return BlobClient.from_blob_url(url, credential=credential())


_database: Database | None = None
_read_only = False


def use_read_only() -> None:
    """Make this process a reader. The API calls this before its first request."""
    global _read_only
    _read_only = True


def database() -> Database:
    """The process-wide database, created on first use.

    Lazy rather than at import, so `--help` and the tests import the jobs
    without needing a credential or a blob.
    """
    global _database
    if _database is None:
        cfg = settings()
        blob = blob_client(cfg.database_blob_url) if cfg.database_blob_url else None
        _database = Database(cfg.database_path, blob=blob, read_only=_read_only)
    return _database


def close_database() -> None:
    """Forget the process's database. Each block closes its own connection."""
    global _database
    _database = None
