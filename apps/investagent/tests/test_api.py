"""Tests for the HTTP layer: routing, validation, and the auth gate.

The connection is overridden rather than mocked at the driver, so these cover
what the API does with a request — status codes, parameter bounds, the bearer
gate — without needing a database. The SQL itself is exercised by running the
real thing against Postgres; there is no value in asserting query strings.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from investagent import queries
from investagent import settings as settings_module
from investagent.api import deps
from investagent.api.main import create_app


class FakeConn:
    """Stands in for a connection; `SELECT 1` is all /readyz needs."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.executed: list[str] = []

    def execute(self, sql: str, params: Any = None):
        self.executed.append(sql)
        if self.fail:
            raise RuntimeError("database is down")
        return self


@pytest.fixture
def client(monkeypatch):
    """An app whose queries return canned data and whose pool is never opened."""
    conn = FakeConn()
    app = create_app()
    app.dependency_overrides[deps.connection] = lambda: conn

    monkeypatch.setattr(queries, "overview", lambda c: {"portfolio": {"name": "default"}})
    monkeypatch.setattr(queries, "holdings", lambda c: [{"ticker": "NVDA"}])
    monkeypatch.setattr(queries, "performance", lambda c, days: {"days": days})
    monkeypatch.setattr(queries, "decisions", lambda c, limit, ticker: [{"limit": limit}])
    monkeypatch.setattr(queries, "decision", lambda c, i: None if i == 404 else {"id": i})
    monkeypatch.setattr(queries, "news", lambda c, limit, relevant_only: [])
    monkeypatch.setattr(queries, "trades", lambda c, limit: [])
    monkeypatch.setattr(queries, "runs", lambda c, limit: [])
    monkeypatch.setattr(queries, "latest_summary", lambda c: None)

    with TestClient(app) as test_client:
        test_client.conn = conn
        yield test_client


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def test_healthz_never_touches_the_database():
    """Liveness must not restart the container over a database blip."""
    app = create_app()
    broken = FakeConn(fail=True)
    app.dependency_overrides[deps.connection] = lambda: broken

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert broken.executed == []


def test_readyz_reports_ok_when_the_database_answers(client):
    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": True}
    assert client.conn.executed == ["SELECT 1"]


def test_readyz_reports_503_when_the_database_is_down():
    app = create_app()
    app.dependency_overrides[deps.connection] = lambda: FakeConn(fail=True)

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["database"] is False


# ---------------------------------------------------------------------------
# Routing and validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/overview",
        "/api/holdings",
        "/api/performance",
        "/api/decisions",
        "/api/news",
        "/api/trades",
        "/api/runs",
    ],
)
def test_every_read_endpoint_answers(client, path):
    assert client.get(path).status_code == 200


def test_a_missing_decision_is_a_404_not_a_null_body(client):
    assert client.get("/api/decisions/404").status_code == 404


def test_a_missing_summary_is_a_404(client):
    assert client.get("/api/summaries/latest").status_code == 404


@pytest.mark.parametrize(("limit", "expected"), [(1, 200), (500, 200), (0, 422), (501, 422)])
def test_the_limit_parameter_is_bounded(client, limit, expected):
    """An unbounded limit is how a read-only API becomes a denial of service."""
    assert client.get(f"/api/decisions?limit={limit}").status_code == expected


def test_the_limit_reaches_the_query(client):
    assert client.get("/api/decisions?limit=7").json() == [{"limit": 7}]


def test_only_get_is_allowed(client):
    """Nothing here writes, so anything else must not even route."""
    assert client.post("/api/overview").status_code == 405


# ---------------------------------------------------------------------------
# The bearer gate
# ---------------------------------------------------------------------------


def test_the_api_is_open_by_default(client):
    """Documented position: no PII, no money, no secret in any response."""
    assert client.get("/api/overview").status_code == 200


@pytest.fixture
def guarded(monkeypatch):
    monkeypatch.setenv("API_REQUIRE_TOKEN", "true")
    monkeypatch.setenv("API_BEARER_TOKEN", "sekrit")
    settings_module.settings.cache_clear()
    settings_module.secret.cache_clear()

    app = create_app()
    app.dependency_overrides[deps.connection] = lambda: FakeConn()
    monkeypatch.setattr(queries, "overview", lambda c: {"ok": True})
    with TestClient(app) as test_client:
        yield test_client

    settings_module.settings.cache_clear()
    settings_module.secret.cache_clear()


def test_a_guarded_api_refuses_a_request_with_no_token(guarded):
    assert guarded.get("/api/overview").status_code == 401


def test_a_guarded_api_refuses_a_wrong_token(guarded):
    response = guarded.get("/api/overview", headers={"Authorization": "Bearer wrong"})

    assert response.status_code == 401


def test_a_guarded_api_refuses_a_token_without_the_bearer_scheme(guarded):
    assert guarded.get("/api/overview", headers={"Authorization": "sekrit"}).status_code == 401


def test_a_guarded_api_accepts_the_right_token(guarded):
    response = guarded.get("/api/overview", headers={"Authorization": "Bearer sekrit"})

    assert response.status_code == 200


def test_health_stays_open_even_when_the_api_is_guarded(guarded):
    """A probe cannot present a token, so guarding /healthz would kill the app."""
    assert guarded.get("/healthz").status_code == 200
    assert guarded.get("/readyz").status_code == 200
