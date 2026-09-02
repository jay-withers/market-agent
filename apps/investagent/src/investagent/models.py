"""Domain models, risk configuration, and the LLM's structured output types.

Money is `Decimal`, never `float`, matching the `NUMERIC(18,4)` columns in
`sql/001-schema.sql`. The whole experiment reduces to a P&L figure, and binary
floating point cannot represent a penny exactly.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Four decimal places, matching NUMERIC(18,4). Every amount crossing into the
# database or into an arithmetic comparison is quantized to this first, so a
# limit can never be breached by a fraction of a penny that only exists in
# memory.
MONEY = Decimal("0.0001")

Action = Literal["BUY", "SELL", "HOLD"]
Sentiment = Literal["positive", "neutral", "negative"]

# Named rules the risk engine can cite. A closed set rather than free text
# because these are written to `ai_decisions.risk_verdict` and then queried:
# "how often did concentration bind?" has to be answerable.
Constraint = Literal[
    # Gates — a rejection no amount of clamping can rescue.
    "action_is_hold",
    "ticker_not_allowed",
    "confidence_below_floor",
    "daily_trade_limit",
    "no_position_to_sell",
    "no_amount_recommended",
    "below_min_trade_gbp",
    # Caps — each yields a maximum permitted trade size.
    "recommended_amount",
    "max_trade_gbp",
    "max_position_gbp",
    "max_concentration_pct",
    "max_total_exposure_pct",
    "available_cash",
    "position_size",
]


def money(value: Decimal | int | str | float) -> Decimal:
    """Quantize to the money scale, always downwards.

    Rounding towards zero rather than to nearest is deliberate: every caller is
    either a limit or headroom against a limit, so rounding up could approve a
    trade a hair over a cap.

    `float` is accepted for exactly one caller: the LLM's suggested amount,
    which arrives as a JSON number. Nothing else should be passing one.
    """
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_DOWN)


# ---------------------------------------------------------------------------
# LLM structured output
# ---------------------------------------------------------------------------


# Docstrings and Field descriptions on the two classes below are sent to the
# model: `messages.parse()` builds its JSON schema from the Pydantic model, and
# the class docstring becomes the schema's `description`. So they are written
# for the model as instructions, and anything that is really a note to a future
# maintainer goes in a comment like this one instead.
class NewsRelevance(BaseModel):
    """Whether one news article is worth analysing for one ticker."""

    relevant: bool = Field(
        description="True only if this could plausibly move the price or change the "
        "investment case."
    )
    sentiment: Sentiment = Field(description="Sentiment of the article towards the ticker.")
    sentiment_score: float = Field(
        ge=-1.0, le=1.0, description="-1.0 most negative, 0.0 neutral, 1.0 most positive."
    )
    rationale: str = Field(description="One sentence explaining the relevance decision.")


# `suggested_amount_gbp` is a `float`, the one place in this system money is
# not a Decimal. A recommendation is an *opinion*, not an accounting figure:
# the risk engine quantizes it before using it and nothing downstream treats it
# as authoritative, so the imprecision cannot reach a stored figure. Declaring
# it Decimal instead produced a three-branch `anyOf` in the schema — number,
# string with a Decimal regex, or null — which is a worse thing to hand a model
# than a plain number.
class Recommendation(BaseModel):
    """A BUY, SELL or HOLD assessment of one ticker."""

    ticker: str = Field(description="The ticker this assessment is about.")
    action: Action = Field(
        description="HOLD is a real answer and usually the right one; do not "
        "manufacture conviction."
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="0.0 no confidence, 1.0 complete confidence."
    )
    suggested_amount_gbp: float | None = Field(
        default=None,
        description="Size of the trade in GBP. Omit for HOLD. A deterministic risk "
        "engine may reduce or refuse this.",
    )
    reasoning: str = Field(description="What in the evidence drove this call. Be specific.")
    risks: str = Field(description="The strongest argument against this recommendation.")


# ---------------------------------------------------------------------------
# Portfolio state
# ---------------------------------------------------------------------------


class Position(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    quantity: Decimal
    # Marked to the latest close, so it moves without any trade happening.
    value_gbp: Decimal
    avg_cost_gbp: Decimal


class PortfolioState(BaseModel):
    """What the portfolio looks like at the moment a decision is taken.

    Frozen, and passed to the risk engine by value: the engine is a pure
    function and must not be able to mutate the state it is judging. This is
    also the object serialised into `ai_decisions.portfolio_state`, so a
    decision can be replayed later against exactly what the model was shown.
    """

    model_config = ConfigDict(frozen=True)

    cash_gbp: Decimal
    positions: tuple[Position, ...] = ()

    @property
    def invested_gbp(self) -> Decimal:
        return money(sum((p.value_gbp for p in self.positions), Decimal(0)))

    @property
    def total_value_gbp(self) -> Decimal:
        return money(self.cash_gbp + self.invested_gbp)

    def position_value(self, ticker: str) -> Decimal:
        """Current value of the holding in `ticker`, or zero if none is held."""
        return money(sum((p.value_gbp for p in self.positions if p.ticker == ticker), Decimal(0)))


# ---------------------------------------------------------------------------
# Risk configuration and verdict
# ---------------------------------------------------------------------------


class RiskLimits(BaseModel):
    """The deterministic bounds the LLM cannot talk its way past.

    Percentages are whole numbers: `max_concentration_pct = 30` is 30%.
    """

    model_config = ConfigDict(frozen=True)

    # Largest total value any single holding may reach.
    max_position_gbp: Decimal
    # Largest single trade.
    max_trade_gbp: Decimal
    # Below this a trade is not worth the spread, so it is refused outright
    # rather than clamped up.
    min_trade_gbp: Decimal
    max_concentration_pct: Decimal
    max_total_exposure_pct: Decimal
    max_daily_trades: int
    min_confidence: float

    # Fail-closed: an empty allowlist permits nothing. The watchlist is always
    # populated in practice, and the alternative — empty meaning "anything" —
    # turns a config-loading bug into unrestricted trading.
    allowed_tickers: frozenset[str] = frozenset()


class RiskReason(BaseModel):
    """One rule the engine applied, and what it permitted."""

    model_config = ConfigDict(frozen=True)

    constraint: Constraint
    detail: str
    # The maximum this rule allowed, for a cap. None for a gate, which permits
    # nothing rather than some amount.
    cap_gbp: Decimal | None = None


class RiskVerdict(BaseModel):
    """What the risk engine permits, and its full reasoning.

    Serialised into `ai_decisions.risk_verdict`. `reasons` carries every rule
    considered, so a decision stays explicable after the limits have changed.
    """

    model_config = ConfigDict(frozen=True)

    approved: bool
    # None where nothing was permitted, distinguishing "the engine refused" from
    # "the engine approved zero", which cannot happen.
    approved_amount_gbp: Decimal | None
    reasons: tuple[RiskReason, ...]
    # The rule that decided the outcome: for an approval the tightest cap, for a
    # rejection the gate that refused. Never None — every verdict has a cause.
    binding_constraint: Constraint
