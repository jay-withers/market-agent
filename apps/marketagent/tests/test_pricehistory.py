"""pricehistory: re-fetch a restated ticker's stored history, and only then."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from marketagent import pricehistory
from marketagent.marketdata import Bar


class _Db:
    """Just enough of `database()` for `restated_history`."""

    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        conn = self._conn

        class _Block:
            def __enter__(self):
                return conn

            def __exit__(self, *exc):
                return False

        return _Block()


def _bar(day: date, close: str) -> Bar:
    c = Decimal(close)
    return Bar("AAPL", day, c, c, c, c, 1)


def test_a_restated_ticker_is_refetched_from_its_earliest_stored_bar(conn, monkeypatch):
    first = date.today() - timedelta(days=20)
    calls = []
    monkeypatch.setattr(
        pricehistory,
        "fetch_daily_bars",
        lambda tickers, start: calls.append((tickers, start)) or [_bar(first, "100")],
    )
    monkeypatch.setattr(pricehistory.repo, "restated_tickers", lambda _conn, _bars: {"AAPL": first})

    assert pricehistory.restated_history(_Db(conn), [_bar(date.today(), "100")]) == [
        _bar(first, "100")
    ]
    assert calls == [(["AAPL"], first)]


def test_nothing_restated_makes_no_request(conn, monkeypatch):
    monkeypatch.setattr(pricehistory.repo, "restated_tickers", lambda _conn, _bars: {})
    monkeypatch.setattr(
        pricehistory,
        "fetch_daily_bars",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetched")),
    )

    assert pricehistory.restated_history(_Db(conn), [_bar(date.today(), "100")]) == []
