"""S&P 100 / S&P 500 constituent lists, scraped from Wikipedia.

No free API covers either index at a tier this project can use. Spiked
financialmodelingprep.com first: its constituent endpoints (S&P 500,
Nasdaq-100, Dow) require a Premium plan — free and Starter both get a 402 —
and it has no S&P 100 endpoint at all, at any tier; the S&P 100 is a
separately maintained index, not derivable from the S&P 500 list. Wikipedia's
"List of S&P 500 companies" and "S&P 100" pages both carry a
`<table id="constituents">` with the fields needed, no key and no rate limit
beyond ordinary etiquette.

Isolated behind this one module and the `IndexSource` protocol so a future
move to a paid API, if one is ever worth it, touches nothing outside this
file — nothing that calls `sp500()`/`sp100()` needs to know the source
changed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from bs4 import BeautifulSoup, Tag

from .fetch import get_text

SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
SP100_URL = "https://en.wikipedia.org/wiki/S%26P_100"

# Wikimedia's edge rejects requests with no descriptive User-Agent (a bare
# httpx default gets a 403, not a friendlier block page), per their bot
# etiquette: https://foundation.wikimedia.org/wiki/Policy:User-Agent_policy
USER_AGENT = "MarketAgentBot/1.0 (https://github.com/jay-withers/market-agent) python-httpx"

# A sanity floor on the parsed row count, not an exact figure: real index
# membership changes over time, but a page that parses to far fewer rows than
# this means the table structure moved, not that the index shrank.
SP500_MIN_ROWS = 480
SP100_MIN_ROWS = 95

_FOOTNOTE = re.compile(r"\[.*?\]")
# Wikipedia sometimes renders a class qualifier straight after the name with
# no space (e.g. a `<br>` collapses to nothing when text is extracted),
# producing "Berkshire Hathaway(Class B)". Cosmetic only, but worth fixing at
# the source rather than leaving a name that reads like a typo in every row
# that has one.
_GLUED_PAREN = re.compile(r"(?<=\S)\(")


@dataclass(frozen=True)
class Constituent:
    ticker: str
    name: str
    sector: str | None


class IndexSource(Protocol):
    def sp500(self) -> list[Constituent]: ...
    def sp100(self) -> list[Constituent]: ...


class ConstituentsParseError(RuntimeError):
    """The constituents table was missing, empty, or implausibly short.

    Raised rather than returning a short list: silently seeding or rebalancing
    a watchlist from a half-parsed page is worse than failing the job loudly.
    """


def _cell_text(cell: Tag) -> str:
    """A table cell's text, with Wikipedia's footnote markers stripped.

    A `<sup>` reference marker (e.g. a `[1]` after a ticker that changed
    recently) would otherwise land inside the ticker or name text verbatim.
    """
    for sup in cell.find_all("sup"):
        sup.decompose()
    text = _FOOTNOTE.sub("", cell.get_text(strip=True)).strip()
    return _GLUED_PAREN.sub(" (", text)


def _parse_constituents_table(html: str, url: str, min_rows: int) -> list[Constituent]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="constituents")
    if table is None:
        raise ConstituentsParseError(f"no #constituents table found at {url}")

    rows: list[Constituent] = []
    for tr in table.find_all("tr")[1:]:  # skip the header row
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        # Column order differs between the two pages (Symbol/Security/GICS
        # Sector/... on the S&P 500 page, Symbol/Name/Sector on the S&P 100
        # page), but both put the ticker first, the name second, and a sector
        # third when there is one — which is all this needs.
        ticker = _cell_text(cells[0])
        name = _cell_text(cells[1])
        sector = _cell_text(cells[2]) if len(cells) > 2 else None
        if not ticker or not name:
            continue
        rows.append(Constituent(ticker=ticker, name=name, sector=sector or None))

    if len(rows) < min_rows:
        raise ConstituentsParseError(
            f"parsed only {len(rows)} rows from {url}, expected at least {min_rows} "
            "— the page structure may have changed"
        )
    return rows


class WikipediaIndexSource:
    """The only `IndexSource` implementation today — see the module docstring.

    A handful of constituents carry a share-class ticker with a literal dot
    (BRK.B, BF.B on the S&P 500 page) — kept exactly as Wikipedia prints it
    rather than guessed at a transformation, since the correct form is
    whatever Alpaca's own symbology expects and that has to be verified
    against a real Alpaca account before either ticker is ever traded, not
    assumed here.
    """

    def sp500(self) -> list[Constituent]:
        html = get_text(SP500_URL, headers={"User-Agent": USER_AGENT})
        return _parse_constituents_table(html, SP500_URL, SP500_MIN_ROWS)

    def sp100(self) -> list[Constituent]:
        html = get_text(SP100_URL, headers={"User-Agent": USER_AGENT})
        return _parse_constituents_table(html, SP100_URL, SP100_MIN_ROWS)
