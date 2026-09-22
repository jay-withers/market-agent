"""jobs.rebalance.run() against a real Postgres.

Only dynamic-500 is ever touched by this job — static-100's watchlist is
frozen at seed time (sql/010-seed-sp100-static.sql) and the job never looks
at it.
"""

from __future__ import annotations

import psycopg
import pytest

from marketagent import repository as repo
from marketagent.indices import Constituent
from marketagent.jobs import rebalance

PORTFOLIO = "dynamic-500"


def _reset(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "DELETE FROM portfolio_watchlist WHERE portfolio_id ="
            " (SELECT id FROM portfolio WHERE name = %s)",
            (PORTFOLIO,),
        )


@pytest.fixture
def rebalance_dsn(database_dsn):
    _reset(database_dsn)
    yield database_dsn
    _reset(database_dsn)


class _FreshConnectionPool:
    """Same reasoning as test_agent_run.py's: run() commits for real, so it
    needs its own connection per checkout rather than the shared session one
    repository.py's own tests roll back on."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def connection(self):
        return psycopg.connect(self._dsn)


class _FakeSource:
    def __init__(self, sp500: list[Constituent]) -> None:
        self._sp500 = sp500

    def sp500(self) -> list[Constituent]:
        return self._sp500

    def sp100(self) -> list[Constituent]:
        raise AssertionError("rebalance never asks for the S&P 100")


def _watchlist(dsn: str) -> dict[str, bool]:
    """ticker -> is_active, for dynamic-500."""
    with psycopg.connect(dsn) as conn:
        pid = repo.portfolio_id(conn, PORTFOLIO)
        rows = conn.execute(
            "SELECT ticker, is_active FROM portfolio_watchlist WHERE portfolio_id = %s", (pid,)
        ).fetchall()
    return dict(rows)


def test_new_constituents_are_added_as_active(rebalance_dsn, monkeypatch):
    monkeypatch.setattr(rebalance, "pool", lambda: _FreshConnectionPool(rebalance_dsn))
    source = _FakeSource([Constituent(ticker="NVDA", name="Nvidia", sector="Technology")])

    counts = rebalance.run(source=source)

    assert counts == {"fetched": 1, "added": 1, "removed": 0}
    assert _watchlist(rebalance_dsn) == {"NVDA": True}


def test_a_dropped_constituent_is_deactivated_not_deleted(rebalance_dsn, monkeypatch):
    """The agent stops trading it from its next run, but any existing position
    stays tracked — the same treatment a manually-held ticker already gets.
    Never a row delete, and never a touch to positions/trades/ai_decisions."""
    monkeypatch.setattr(rebalance, "pool", lambda: _FreshConnectionPool(rebalance_dsn))
    rebalance.run(source=_FakeSource([Constituent("NVDA", "Nvidia", "Technology")]))

    counts = rebalance.run(source=_FakeSource([]))

    assert counts == {"fetched": 0, "added": 0, "removed": 1}
    # Still present, just inactive — not deleted.
    assert _watchlist(rebalance_dsn) == {"NVDA": False}


def test_a_returning_constituent_is_reactivated(rebalance_dsn, monkeypatch):
    monkeypatch.setattr(rebalance, "pool", lambda: _FreshConnectionPool(rebalance_dsn))
    nvda = Constituent("NVDA", "Nvidia", "Technology")
    rebalance.run(source=_FakeSource([nvda]))
    rebalance.run(source=_FakeSource([]))

    counts = rebalance.run(source=_FakeSource([nvda]))

    assert counts == {"fetched": 1, "added": 1, "removed": 0}
    assert _watchlist(rebalance_dsn) == {"NVDA": True}


def test_an_unchanged_membership_adds_and_removes_nothing(rebalance_dsn, monkeypatch):
    monkeypatch.setattr(rebalance, "pool", lambda: _FreshConnectionPool(rebalance_dsn))
    nvda = Constituent("NVDA", "Nvidia", "Technology")
    rebalance.run(source=_FakeSource([nvda]))

    counts = rebalance.run(source=_FakeSource([nvda]))

    assert counts == {"fetched": 1, "added": 0, "removed": 0}


def test_static_100_is_never_touched(rebalance_dsn, monkeypatch):
    monkeypatch.setattr(rebalance, "pool", lambda: _FreshConnectionPool(rebalance_dsn))
    with psycopg.connect(rebalance_dsn) as conn:
        static_pid = repo.portfolio_id(conn, "static-100")
        before = conn.execute(
            "SELECT count(*) FROM portfolio_watchlist WHERE portfolio_id = %s", (static_pid,)
        ).fetchone()[0]

    rebalance.run(source=_FakeSource([Constituent("NVDA", "Nvidia", "Technology")]))

    with psycopg.connect(rebalance_dsn) as conn:
        after = conn.execute(
            "SELECT count(*) FROM portfolio_watchlist WHERE portfolio_id = %s", (static_pid,)
        ).fetchone()[0]
    assert after == before
