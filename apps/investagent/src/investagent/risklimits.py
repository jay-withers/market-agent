"""Where the risk limits come from.

Config, not code: the whole point of the risk engine is that its bounds are
decided deliberately rather than by an LLM, so they are environment variables
with defaults sized for the notional GBP 500.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import RiskLimits


class RiskSettings(BaseSettings):
    """Defaults sized for a GBP 500 portfolio across a ten-name watchlist.

    GBP 100 per position is 20% of the pot, which with a 25% concentration
    ceiling means the absolute cap binds first in an untouched portfolio and
    concentration takes over as the pot grows. GBP 5 minimum keeps a trade from
    being all spread; three trades a day bounds a bad news day.
    """

    model_config = SettingsConfigDict(env_prefix="RISK_", env_file=".env", extra="ignore")

    max_position_gbp: Decimal = Decimal(100)
    max_trade_gbp: Decimal = Decimal(50)
    min_trade_gbp: Decimal = Decimal(5)
    max_concentration_pct: Decimal = Decimal(25)
    # Leaves a fifth in cash, so a sell is always possible without waiting for
    # settlement and the engine is never forced to refuse for lack of cash.
    max_total_exposure_pct: Decimal = Decimal(80)
    max_daily_trades: int = 3
    # Below this the model is guessing, and a guess that survives the caps is
    # still a trade. 0.6 is deliberately not a round 0.5.
    min_confidence: float = 0.6


def limits(allowed_tickers: frozenset[str]) -> RiskLimits:
    """Build the engine's limits, with the watchlist as the allowlist.

    The allowlist comes from the database rather than configuration so it
    cannot drift from the tickers the agent actually analyses — and an empty
    watchlist then permits nothing, which is the fail-closed behaviour the
    engine documents.
    """
    cfg = RiskSettings()
    return RiskLimits(
        max_position_gbp=cfg.max_position_gbp,
        max_trade_gbp=cfg.max_trade_gbp,
        min_trade_gbp=cfg.min_trade_gbp,
        max_concentration_pct=cfg.max_concentration_pct,
        max_total_exposure_pct=cfg.max_total_exposure_pct,
        max_daily_trades=cfg.max_daily_trades,
        min_confidence=cfg.min_confidence,
        allowed_tickers=allowed_tickers,
    )
