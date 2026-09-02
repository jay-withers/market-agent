"""The LLM boundary: what a provider must offer, and what a call costs.

Two stages, deliberately different models. A cheap one filters a large batch of
news for relevance; an expensive one reasons about what to do with what
survives. Both return a validated Pydantic object rather than prose, because
everything downstream — the risk engine, the database, the dashboard — needs
fields, not paragraphs.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from ..models import NewsRelevance, Recommendation

# Bump when a prompt changes in a way that could change an answer. It is stored
# on every `news_analysis` and `ai_decisions` row and is part of the
# `news_analysis` unique key, so a re-analysis after a prompt change adds rows
# instead of destroying the record of what the old prompt concluded.
PROMPT_VERSION = "v1"

# US dollars per million tokens, as published 2026-06-24. A snapshot, not a
# live lookup: it only feeds the cost figure recorded on each `agent_runs` row,
# so being a few percent stale is harmless, whereas a network call on the hot
# path is not. Re-check at anthropic.com/pricing.
PRICES_USD_PER_MTOK: dict[str, tuple[Decimal, Decimal]] = {
    # model: (input, output)
    "claude-sonnet-5": (Decimal("2.00"), Decimal("10.00")),
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
    "claude-opus-5": (Decimal("5.00"), Decimal("25.00")),
}

MILLION = Decimal(1_000_000)

# Multipliers on the input rate. Nothing here enables caching yet — one run a
# day shares no prefix with the previous one — but `usage` reports the fields
# regardless, and a cost that silently ignored them would be wrong the moment
# caching is switched on.
CACHE_WRITE_MULTIPLIER = Decimal("1.25")
CACHE_READ_MULTIPLIER = Decimal("0.10")

# Cost is recorded in a NUMERIC(18,6) column. Six places matter: a single
# filter call costs a fraction of a cent, and the point of tracking it is to
# notice a run that suddenly doesn't.
COST = Decimal("0.000001")


@dataclass(frozen=True)
class Usage:
    """Token counts for one API call, as reported by the response."""

    input_tokens: int
    output_tokens: int
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    def cost_usd(self, model: str) -> Decimal:
        """What this call cost, or zero for a model with no price on file.

        Unknown models cost zero rather than raising: an unpriced model is a
        stale table, and failing a whole agent run over a cost *estimate* would
        be the wrong trade.
        """
        prices = PRICES_USD_PER_MTOK.get(model)
        if prices is None:
            return Decimal(0)

        rate_in, rate_out = prices
        total = (
            Decimal(self.input_tokens) * rate_in
            + Decimal(self.output_tokens) * rate_out
            + Decimal(self.cache_write_tokens) * rate_in * CACHE_WRITE_MULTIPLIER
            + Decimal(self.cache_read_tokens) * rate_in * CACHE_READ_MULTIPLIER
        ) / MILLION
        return total.quantize(COST)

    def __add__(self, other: Usage) -> Usage:
        """Accumulate across the calls in one agent run."""
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
        )


@dataclass(frozen=True)
class LlmResult[T]:
    """A parsed result, plus what producing it cost.

    The usage travels with the result rather than being accumulated inside the
    provider: the agent job writes per-decision token counts to `ai_decisions`
    *and* a run total to `agent_runs`, so it needs both granularities.
    """

    value: T
    model: str
    usage: Usage

    @property
    def cost_usd(self) -> Decimal:
        return self.usage.cost_usd(self.model)


class NewsItem(Protocol):
    """The fields the prompts need from a news row, and nothing more."""

    headline: str
    summary: str | None
    published_at: object


class Llm(Protocol):
    """What the agent job needs from an LLM.

    A Protocol rather than an abstract base class so a test double is any
    object with the right two methods — no import of the provider, no API key,
    no network.
    """

    def filter_news(
        self, ticker: str, headline: str, summary: str | None
    ) -> LlmResult[NewsRelevance]:
        """Decide whether one article is worth analysing for one ticker."""
        ...

    def analyse(self, prompt: str) -> LlmResult[Recommendation]:
        """Produce a BUY/SELL/HOLD assessment from an assembled prompt."""
        ...
