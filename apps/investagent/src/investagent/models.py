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


def money(value: Decimal | int | str) -> Decimal:
    """Quantize to the money scale, always downwards.

    Rounding towards zero rather than to nearest is deliberate: every caller is
    either a limit or headroom against a limit, so rounding up could approve a
    trade a hair over a cap.
    """
    return Decimal(value).quantize(MONEY, rounding=ROUND_DOWN)


# ---------------------------------------------------------------------------
# LLM structured output
# ---------------------------------------------------------------------------


class NewsRelevance(BaseModel):
    """The cheap filter's verdict on one article/ticker pair."""

    relevant: bool
    sentiment: Sentiment
    sentiment_score: float = Field(ge=-1.0, le=1.0)
    rationale: str


class Recommendation(BaseModel):
    """The analysis model's assessment of one ticker.

    A recommendation is an *opinion*, not an accounting figure — which is why
    `suggested_amount_gbp` is allowed to arrive as a JSON number and be coerced
    to `Decimal` through a float. The imprecision is irrelevant because the risk
    engine quantizes it before doing anything with it, and because nothing
    downstream treats the suggestion as authoritative. Every exact amount in
    this system originates from the risk engine or the database, not from here.
    """

    ticker: str
    action: Action
    confidence: float = Field(ge=0.0, le=1.0)
    # None for HOLD. The engine rejects a BUY or SELL without an amount rather
    # than inventing one.
    suggested_amount_gbp: Decimal | None = None
    reasoning: str
    risks: str


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
