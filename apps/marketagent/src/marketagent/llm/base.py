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

from ..models import DailyNarrative, NewsRelevance, Recommendation, WeeklyReview

# Bump when a prompt changes in a way that could change an answer. It is stored
# on every `news_analysis` and `ai_decisions` row and is part of the
# `news_analysis` unique key, so a re-analysis after a prompt change adds rows
# instead of destroying the record of what the old prompt concluded.
PROMPT_VERSION = "v5"

# US dollars per million tokens, as published on DeepSeek's pricing page.
# A snapshot, not a live lookup — it only feeds the cost figure recorded on
# each `agent_runs` row, so being a few percent stale is harmless, whereas a
# network call on the hot path is not. Re-check at api-docs.deepseek.com.
#
# Three rates, not two: DeepSeek prices cache-hit and cache-miss input
# tokens separately (not one input rate plus a multiplier, which is how
# Anthropic's pricing worked and how this table used to be shaped), and it
# has no separate charge for *writing* to the cache at all — caching is
# automatic and only ever shows up as a cheaper rate on a later hit.
#
# DeepSeek also prices by time of day. Peak is 01:00-04:00 and 06:00-10:00
# UTC, Monday to Friday excluding Chinese public holidays; every other hour,
# weekends included, is off-peak at half these rates (checked 2026-09-24).
# The figures here are the *peak* rate, used unconditionally rather than
# modelling that schedule. That makes this a deliberate upper bound, not a
# precise one — consistent with MAX_RUN_COST_USD existing to catch a runaway,
# not to meter ordinary spending to the cent. It is exact for a weekday agent
# run at the default 06:00 UTC and double the real bill for a weekend one.
PRICES_USD_PER_MTOK: dict[str, tuple[Decimal, Decimal, Decimal]] = {
    # model: (input_cache_miss, input_cache_hit, output)
    "deepseek-flash": (Decimal("0.30"), Decimal("0.006"), Decimal("1.20")),
    "deepseek-v4-pro": (Decimal("1.32"), Decimal("0.044"), Decimal("3.96")),
}

MILLION = Decimal(1_000_000)

# Cost is recorded in a NUMERIC(18,6) column. Six places matter: a single
# filter call costs a fraction of a cent, and the point of tracking it is to
# notice a run that suddenly doesn't.
COST = Decimal("0.000001")


@dataclass(frozen=True)
class Usage:
    """Token counts for one API call, as reported by the response.

    `input_tokens` is the *total* prompt token count, matching the
    `agent_runs`/`ai_decisions` columns it feeds (they record counts, not a
    cache breakdown) — `cache_read_tokens` is the subset of it DeepSeek
    served from cache at the cheaper hit rate, not an addition on top of it.
    `cache_write_tokens` has no DeepSeek equivalent and is always zero; it
    stays on the dataclass so `__add__` has one shape to accumulate rather
    than two.
    """

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

        rate_miss, rate_hit, rate_out = prices
        # cache_read_tokens is a subset of input_tokens, not additional to it.
        miss_tokens = max(self.input_tokens - self.cache_read_tokens, 0)
        total = (
            Decimal(miss_tokens) * rate_miss
            + Decimal(self.cache_read_tokens) * rate_hit
            + Decimal(self.output_tokens) * rate_out
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
    object with the right methods — no import of the provider, no API key, no
    network.
    """

    def filter_news(
        self, ticker: str, headline: str, summary: str | None
    ) -> LlmResult[NewsRelevance]:
        """Decide whether one article is worth analysing for one ticker."""
        ...

    def analyse(self, prompt: str) -> LlmResult[Recommendation]:
        """Produce a BUY/SELL/HOLD assessment from an assembled prompt."""
        ...

    def narrate(self, prompt: str) -> LlmResult[DailyNarrative]:
        """Write the daily summary's commentary. Figures are supplied, not derived."""
        ...

    def review(self, prompt: str) -> LlmResult[WeeklyReview]:
        """Assess a week of the experiment and propose changes. Advisory only."""
        ...
