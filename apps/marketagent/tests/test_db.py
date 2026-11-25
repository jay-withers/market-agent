"""The database blob: the lease, the copy each process works on, and readers.

`Database` keeps a local DuckDB file and mirrors it to one blob, so these run
the real class against a real DuckDB file and an in-memory stand-in for the
Azure SDK's `BlobClient` and `BlobLeaseClient`: the edge that would otherwise
need a storage account. The fakes model what the code relies on (ETags that
change on every upload, a lease only one holder gets, a 404 for a blob that
was never written), so the assertions are about what reached the blob, not
which method was called.
"""

from __future__ import annotations

import itertools
import time
import types
from pathlib import Path
from typing import Any

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

from marketagent import db

_etags = itertools.count(1)


class FakeBlob:
    """One blob: its bytes, an ETag that moves on each upload, and a lease flag."""

    def __init__(self) -> None:
        self.data: bytes | None = None
        self.etag = ""
        self.leased = False
        self.uploads = 0
        self.upload_leases: list[Any] = []
        self.property_reads = 0

    def _write(self, data: bytes) -> dict:
        self.data = data
        self.etag = f'"{next(_etags)}"'
        return {"etag": self.etag}

    def get_blob_properties(self, lease: Any = None) -> Any:
        self.property_reads += 1
        if self.data is None:
            raise ResourceNotFoundError("BlobNotFound")
        return types.SimpleNamespace(etag=self.etag, size=len(self.data))

    def download_blob(self, lease: Any = None) -> Any:
        data = self.data or b""
        return types.SimpleNamespace(readinto=lambda f: f.write(data))

    def upload_blob(self, data: Any, overwrite: bool = False, lease: Any = None) -> dict:
        if not overwrite and self.data is not None:
            raise ResourceExistsError("BlobAlreadyExists")
        if self.leased and lease is None:
            # Azure's 412 LeaseIdMissing: a leased blob takes writes only
            # from the holder.
            raise ResourceExistsError("LeaseIdMissing")
        self.uploads += 1
        self.upload_leases.append(lease)
        return self._write(data if isinstance(data, bytes) else data.read())


class FakeLease:
    """`BlobLeaseClient`: acquire fails while another holder has the lease."""

    def __init__(self, blob: FakeBlob) -> None:
        self.blob = blob

    def acquire(self, lease_duration: int) -> None:
        if self.blob.data is None:
            raise ResourceNotFoundError("BlobNotFound")
        if self.blob.leased:
            raise ResourceExistsError("LeaseAlreadyPresent")
        self.blob.leased = True

    def renew(self) -> None:
        pass

    def release(self) -> None:
        self.blob.leased = False


@pytest.fixture(autouse=True)
def fake_lease(monkeypatch):
    # Imported inside `_leased`, so patched where it is looked up.
    monkeypatch.setattr("azure.storage.blob.BlobLeaseClient", FakeLease)


@pytest.fixture
def blob() -> FakeBlob:
    return FakeBlob()


def _write_probe(database: db.Database, value: int) -> None:
    with database.connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS probe (x INTEGER)")
        conn.execute("INSERT INTO probe VALUES (%s)", (value,))


def _probe(database: db.Database) -> list[int]:
    with database.connection() as conn:
        return [r[0] for r in conn.execute("SELECT x FROM probe ORDER BY x").fetchall()]


def test_the_first_writer_creates_the_blob_and_uploads_its_work(tmp_path, blob):
    writer = db.Database(tmp_path / "a.duckdb", blob=blob)
    _write_probe(writer, 1)
    assert blob.data  # the placeholder was replaced by a real file
    # The empty placeholder went up with no lease (there was no blob to lease);
    # the real file went up under the lease the block held.
    placeholder, work = blob.upload_leases
    assert placeholder is None
    assert isinstance(work, FakeLease)
    assert blob.leased is False


def test_a_second_process_downloads_what_the_first_wrote(tmp_path, blob):
    first = db.Database(tmp_path / "a.duckdb", blob=blob)
    _write_probe(first, 1)
    other = db.Database(tmp_path / "b.duckdb", blob=blob)
    _write_probe(other, 2)
    # The same long-lived instance sees the other's write: its local copy is
    # stale because the ETag moved, so it downloads before writing over it.
    _write_probe(first, 3)
    assert _probe(other) == [1, 2, 3]


def test_a_block_that_changes_nothing_uploads_nothing(tmp_path, blob):
    writer = db.Database(tmp_path / "a.duckdb", blob=blob)
    _write_probe(writer, 1)
    uploads = blob.uploads
    assert _probe(writer) == [1]
    assert blob.uploads == uploads


def test_an_error_inside_the_block_uploads_nothing_and_releases_the_lease(tmp_path, blob):
    writer = db.Database(tmp_path / "a.duckdb", blob=blob)
    _write_probe(writer, 1)
    etag = blob.etag
    with pytest.raises(RuntimeError, match="boom"), writer.connection() as conn:
        conn.execute("INSERT INTO probe VALUES (2)")
        raise RuntimeError("boom")
    assert blob.etag == etag
    assert blob.leased is False
    assert _probe(writer) == [1]


def test_a_writer_waits_for_a_lease_another_holds(tmp_path, blob, monkeypatch):
    writer = db.Database(tmp_path / "a.duckdb", blob=blob)
    _write_probe(writer, 1)
    blob.leased = True  # another writer has it
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        blob.leased = False  # ... and lets go while we wait

    # db's own `time`, so only its sleep is replaced, not the process's.
    monkeypatch.setattr(db, "time", types.SimpleNamespace(monotonic=time.monotonic, sleep=sleep))
    _write_probe(writer, 2)
    assert waits == [db.LEASE_RETRY_SECONDS]
    assert _probe(writer) == [1, 2]


def test_a_lease_held_past_the_wait_is_an_error_not_a_hang(tmp_path, blob, monkeypatch):
    writer = db.Database(tmp_path / "a.duckdb", blob=blob)
    _write_probe(writer, 1)
    blob.leased = True
    monkeypatch.setattr(db, "LEASE_WAIT_SECONDS", -1)
    with pytest.raises(TimeoutError, match="leased for over"):
        _write_probe(writer, 2)


def test_a_reader_downloads_once_then_rechecks_only_after_the_refresh_interval(
    tmp_path, blob, monkeypatch
):
    writer = db.Database(tmp_path / "a.duckdb", blob=blob)
    _write_probe(writer, 1)
    reader = db.Database(tmp_path / "r.duckdb", blob=blob, read_only=True)
    assert _probe(reader) == [1]

    _write_probe(writer, 2)
    reads = blob.property_reads
    # Within the interval the reader serves its copy without asking the blob.
    assert _probe(reader) == [1]
    assert blob.property_reads == reads

    monkeypatch.setattr(db, "READ_REFRESH_SECONDS", -1)
    assert _probe(reader) == [1, 2]


def test_a_reader_with_no_blob_reads_the_local_file(tmp_path):
    path = tmp_path / "a.duckdb"
    _write_probe(db.Database(path), 7)
    assert _probe(db.Database(path, read_only=True)) == [7]


def test_the_process_database_is_made_once_and_honours_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr(
        db,
        "settings",
        lambda: types.SimpleNamespace(
            database_path=str(tmp_path / "p.duckdb"), database_blob_url=""
        ),
    )
    monkeypatch.setattr(db, "_database", None)
    monkeypatch.setattr(db, "_read_only", False)

    first = db.database()
    assert db.database() is first
    assert (first.read_only, first.blob) == (False, None)

    db.use_read_only()
    db.close_database()
    assert db.database().read_only is True


def test_azurite_urls_name_the_container_after_the_account(monkeypatch):
    """Azurite puts the account first in the path; Azure does not."""
    monkeypatch.setattr(
        db,
        "settings",
        lambda: types.SimpleNamespace(azure_storage_connection_string="UseDevelopmentStorage=true"),
    )
    created: list[str] = []

    def create_container(self: Any) -> None:
        created.append(self.container_name)
        raise ResourceExistsError("ContainerAlreadyExists")  # tolerated

    monkeypatch.setattr("azure.storage.blob.ContainerClient.create_container", create_container)
    client = db.blob_client("http://127.0.0.1:10000/devstoreaccount1/database/marketagent.duckdb")
    assert (client.container_name, client.blob_name) == ("database", "marketagent.duckdb")
    assert created == ["database"]


def test_azure_urls_use_the_managed_identity(monkeypatch):
    monkeypatch.setattr(
        db, "settings", lambda: types.SimpleNamespace(azure_storage_connection_string="")
    )
    sentinel = object()
    seen: list[Any] = []

    def from_blob_url(url: str, credential: Any = None) -> Path:
        seen.append((url, credential))
        return Path(url)

    monkeypatch.setattr(db, "credential", lambda: sentinel)
    monkeypatch.setattr("azure.storage.blob.BlobClient.from_blob_url", staticmethod(from_blob_url))
    url = "https://stmarketagentdev.blob.core.windows.net/database/marketagent.duckdb"
    db.blob_client(url)
    assert seen == [(url, sentinel)]
