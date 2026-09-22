"""Tests for the S&P 100/500 constituent parser.

No network in here: `_parse_constituents_table` is pure, and
`WikipediaIndexSource` is exercised by monkeypatching `get_text` so these
never touch Wikipedia.
"""

from __future__ import annotations

from marketagent import indices
from marketagent.indices import (
    Constituent,
    ConstituentsParseError,
    WikipediaIndexSource,
    _parse_constituents_table,
)

URL = "https://en.wikipedia.org/wiki/example"


def _table(
    rows: str, header: str = "<tr><th>Symbol</th><th>Security</th><th>Sector</th></tr>"
) -> str:
    return f"<html><body><table id='constituents'>{header}{rows}</table></body></html>"


def test_ticker_name_and_sector_are_read_in_column_order():
    html = _table("<tr><td>AAPL</td><td>Apple Inc.</td><td>Information Technology</td></tr>")

    rows = _parse_constituents_table(html, URL, min_rows=1)

    assert rows == [Constituent(ticker="AAPL", name="Apple Inc.", sector="Information Technology")]


def test_a_table_with_only_two_columns_still_parses_with_no_sector():
    html = _table(
        "<tr><td>AAPL</td><td>Apple Inc.</td></tr>",
        header="<tr><th>Symbol</th><th>Name</th></tr>",
    )

    rows = _parse_constituents_table(html, URL, min_rows=1)

    assert rows == [Constituent(ticker="AAPL", name="Apple Inc.", sector=None)]


def test_a_footnote_marker_is_stripped_from_the_ticker():
    """Wikipedia sometimes attaches a `<sup>[1]</sup>` reference marker to a
    row that changed recently — it must not end up inside the ticker text."""
    html = _table("<tr><td>AAPL<sup>[1]</sup></td><td>Apple Inc.</td><td>Tech</td></tr>")

    rows = _parse_constituents_table(html, URL, min_rows=1)

    assert rows[0].ticker == "AAPL"


def test_a_class_qualifier_glued_to_the_name_gets_a_space():
    """Seen on Berkshire Hathaway and Alphabet: a `<br>` between the name and
    a "(Class B)" qualifier collapses to nothing when text is extracted."""
    html = _table("<tr><td>BRK.B</td><td>Berkshire Hathaway(Class B)</td><td>Financials</td></tr>")

    rows = _parse_constituents_table(html, URL, min_rows=1)

    assert rows[0].name == "Berkshire Hathaway (Class B)"


def test_a_row_with_no_ticker_or_name_is_skipped():
    html = _table(
        "<tr><td></td><td></td><td>Tech</td></tr>"
        "<tr><td>AAPL</td><td>Apple Inc.</td><td>Tech</td></tr>"
    )

    rows = _parse_constituents_table(html, URL, min_rows=1)

    assert [r.ticker for r in rows] == ["AAPL"]


def test_missing_the_constituents_table_raises_rather_than_returning_empty():
    html = "<html><body><table id='something-else'></table></body></html>"

    try:
        _parse_constituents_table(html, URL, min_rows=1)
        raise AssertionError("expected ConstituentsParseError")
    except ConstituentsParseError as exc:
        assert URL in str(exc)


def test_fewer_rows_than_expected_raises_rather_than_seeding_a_short_list():
    """A page that parses far short of the expected count means the table
    structure moved, not that the index shrank — silently seeding a
    half-parsed watchlist would be worse than failing the job loudly."""
    html = _table("<tr><td>AAPL</td><td>Apple Inc.</td><td>Tech</td></tr>")

    try:
        _parse_constituents_table(html, URL, min_rows=5)
        raise AssertionError("expected ConstituentsParseError")
    except ConstituentsParseError as exc:
        assert "parsed only 1 rows" in str(exc)


def test_sp500_fetches_the_right_url_with_a_descriptive_user_agent(monkeypatch):
    """Wikimedia's edge 403s a request with no descriptive User-Agent."""
    captured = {}

    def fake_get_text(url, *, headers=None, **_kwargs):
        captured["url"] = url
        captured["headers"] = headers
        return _table("<tr><td>AAPL</td><td>Apple Inc.</td><td>Tech</td></tr>")

    monkeypatch.setattr(indices, "get_text", fake_get_text)
    monkeypatch.setattr(indices, "SP500_MIN_ROWS", 1)

    rows = WikipediaIndexSource().sp500()

    assert captured["url"] == indices.SP500_URL
    assert "MarketAgentBot" in captured["headers"]["User-Agent"]
    assert rows == [Constituent(ticker="AAPL", name="Apple Inc.", sector="Tech")]


def test_sp100_fetches_the_right_url(monkeypatch):
    monkeypatch.setattr(
        indices,
        "get_text",
        lambda url, **_kwargs: _table("<tr><td>AAPL</td><td>Apple Inc.</td><td>Tech</td></tr>"),
    )
    monkeypatch.setattr(indices, "SP100_MIN_ROWS", 1)

    rows = WikipediaIndexSource().sp100()

    assert rows == [Constituent(ticker="AAPL", name="Apple Inc.", sector="Tech")]
