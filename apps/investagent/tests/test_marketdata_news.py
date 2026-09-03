"""Tests for the Alpaca data and news readers."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from investagent.marketdata import Bar, fetch_daily_bars, latest_close
from investagent.news import fetch_news
from tests.helpers import json_client, sequence_client

D = Decimal

BARS = {
    "bars": {
        "NVDA": [
            {
                "t": "2026-09-01T04:00:00Z",
                "o": 180.1,
                "h": 183.5,
                "l": 179.0,
                "c": 182.44,
                "v": 145000000,
            },
            {
                "t": "2026-09-02T04:00:00Z",
                "o": 183.0,
                "h": 225.0,
                "l": 182.0,
                "c": 224.41,
                "v": 157073167,
            },
        ]
    }
}

NEWS = {
    "news": [
        {
            "id": 1001,
            "headline": "NVIDIA beats",
            "summary": "Datacentre revenue up",
            "author": "A",
            "url": "https://x/1",
            "source": "benzinga",
            "created_at": "2026-09-02T19:10:00Z",
            # Ten symbols, only three of them on our watchlist.
            "symbols": [
                "AAPL",
                "AMZN",
                "BTCUSD",
                "GOOG",
                "META",
                "MSFT",
                "NVDA",
                "QQQ",
                "SPY",
                "TSLA",
            ],
        },
        {
            "id": 1002,
            "headline": "Nothing much",
            "summary": None,
            "author": None,
            "url": None,
            "source": "benzinga",
            "created_at": "2026-09-02T18:00:00Z",
            "symbols": ["NVDA"],
        },
    ]
}


# ---------------------------------------------------------------------------
# Bars
# ---------------------------------------------------------------------------


def test_bars_are_parsed_into_decimals_not_floats():
    bars = fetch_daily_bars(["NVDA"], client=json_client(BARS))

    assert [b.close_usd for b in bars] == [D("182.44"), D("224.41")]
    assert all(isinstance(b.close_usd, Decimal) for b in bars)


def test_bars_are_sorted_by_ticker_then_date():
    bars = fetch_daily_bars(["NVDA"], client=json_client(BARS))

    assert [b.bar_date for b in bars] == [date(2026, 9, 1), date(2026, 9, 2)]


def test_the_request_pins_the_feed_and_asks_for_adjusted_history():
    captured: list = []
    fetch_daily_bars(["NVDA", "AAPL"], client=json_client(BARS, capture=captured))

    params = captured[0].url.params
    assert params["timeframe"] == "1Day"
    # sip, not the account default: losing the subscription must fail visibly
    # rather than silently downgrade to one exchange's prices.
    assert params["feed"] == "sip"
    assert params["adjustment"] == "all"
    # Sorted, so the same watchlist always produces the same request.
    assert params["symbols"] == "AAPL,NVDA"


def test_no_tickers_makes_no_request():
    assert fetch_daily_bars([]) == []


def test_pagination_follows_the_next_page_token():
    page1 = {"bars": {"NVDA": [BARS["bars"]["NVDA"][0]]}, "next_page_token": "t2"}
    page2 = {"bars": {"NVDA": [BARS["bars"]["NVDA"][1]]}, "next_page_token": None}
    calls: list = []

    bars = fetch_daily_bars(
        ["NVDA"], client=sequence_client([(200, page1), (200, page2)], calls=calls)
    )

    assert len(bars) == 2
    assert len(calls) == 2
    assert calls[1].url.params["page_token"] == "t2"


def test_latest_close_takes_the_most_recent_bar_per_ticker():
    bars = fetch_daily_bars(["NVDA"], client=json_client(BARS))

    assert latest_close(bars)["NVDA"].bar_date == date(2026, 9, 2)


def test_a_ticker_with_no_bar_is_absent_rather_than_priced_at_zero():
    """Valuing an unpriced holding at zero would understate exposure."""
    closes = latest_close([Bar("NVDA", date(2026, 9, 2), D(1), D(1), D(1), D(1), 1)])

    assert "AAPL" not in closes


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------


def test_news_tickers_are_narrowed_to_the_watchlist():
    """A mega-cap roundup must not drag thirty tickers into the stored array."""
    articles = fetch_news(["NVDA", "AAPL", "MSFT"], client=json_client(NEWS))

    assert articles[0].tickers == ("AAPL", "MSFT", "NVDA")


def test_the_provider_id_becomes_the_dedup_key_as_text():
    articles = fetch_news(["NVDA"], client=json_client(NEWS))

    assert articles[0].external_id == "1001"


def test_a_missing_summary_becomes_none_not_an_empty_string():
    articles = fetch_news(["NVDA"], client=json_client(NEWS))

    assert articles[1].summary is None
    assert articles[1].author is None


def test_max_articles_bounds_the_batch():
    """Every article costs a filter call, so a noisy day must stay bounded."""
    articles = fetch_news(["NVDA"], max_articles=1, client=json_client(NEWS))

    assert len(articles) == 1


def test_the_news_request_asks_newest_first():
    captured: list = []
    fetch_news(["NVDA"], client=json_client(NEWS, capture=captured))

    assert captured[0].url.params["sort"] == "desc"
    assert captured[0].url.params["limit"] == "50"
