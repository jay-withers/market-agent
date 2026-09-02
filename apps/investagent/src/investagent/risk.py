"""The deterministic risk engine.

A pure function. No I/O, no clock, no randomness, no network: given the same
recommendation, portfolio state, limits and trade count it returns the same
verdict, which is what makes it testable and what makes an `ai_decisions` row
replayable months later.

The LLM recommends. This decides. Nothing the model writes in `reasoning` can
raise a limit, and the engine never invents a trade the model did not ask for
— it can only refuse, or approve less.

## Order of operations

1. **Gates** run first and short-circuit. Each is a condition no trade size can
   satisfy: a HOLD, a ticker off the allowlist, confidence under the floor, the
   daily trade budget already spent, a SELL with nothing held, or a BUY/SELL
   with no amount attached.
2. **Caps** then each yield a maximum permitted size, and the smallest wins.
   The approved amount is that minimum; the binding constraint is the cap that
   produced it.
3. **The minimum trade size** is checked last, against the clamped amount. A
   trade that survives every cap can still be too small to be worth making.

Caps are evaluated in a fixed order and the *first* minimum wins, so ties
resolve deterministically. `recommended_amount` is first, which means an
unclamped approval is reported as bound by the recommendation itself rather
than by whichever limit happened to tie with it.
"""

from __future__ import annotations

from decimal import Decimal

from .models import (
    Constraint,
    PortfolioState,
    Recommendation,
    RiskLimits,
    RiskReason,
    RiskVerdict,
    money,
)

ZERO = Decimal(0)


def _gate(constraint: Constraint, detail: str) -> RiskVerdict:
    """A verdict permitting nothing, refused before any cap was computed."""
    return RiskVerdict(
        approved=False,
        approved_amount_gbp=None,
        reasons=(RiskReason(constraint=constraint, detail=detail),),
        binding_constraint=constraint,
    )


def _refused(constraint: Constraint, reasons: list[RiskReason]) -> RiskVerdict:
    """A verdict permitting nothing, refused by one of the caps in `reasons`.

    `reasons` is passed through untouched: the cap that refused is already in
    it, and appending a second entry for the same constraint would put a
    contradictory pair into `ai_decisions.risk_verdict` — the cap with its
    value, and the same name again with none.
    """
    return RiskVerdict(
        approved=False,
        approved_amount_gbp=None,
        reasons=tuple(reasons),
        binding_constraint=constraint,
    )


def evaluate(
    rec: Recommendation,
    state: PortfolioState,
    limits: RiskLimits,
    trades_today: int,
) -> RiskVerdict:
    """Decide what, if anything, of `rec` is permitted.

    `trades_today` is the number of trades already executed today, counted by
    the caller from the `trades` table — the engine holds no state and cannot
    look it up itself.
    """
    # --- Gates -------------------------------------------------------------

    if rec.action == "HOLD":
        return _gate("action_is_hold", "HOLD recommends no trade")

    if rec.ticker not in limits.allowed_tickers:
        return _gate("ticker_not_allowed", f"{rec.ticker} is not on the allowlist")

    if rec.confidence < limits.min_confidence:
        return _gate(
            "confidence_below_floor",
            f"confidence {rec.confidence:.2f} is below the floor of {limits.min_confidence:.2f}",
        )

    if trades_today >= limits.max_daily_trades:
        return _gate(
            "daily_trade_limit",
            f"{trades_today} of {limits.max_daily_trades} trades already made today",
        )

    held = state.position_value(rec.ticker)

    if rec.action == "SELL" and held <= ZERO:
        return _gate("no_position_to_sell", f"no {rec.ticker} held")

    if rec.suggested_amount_gbp is None or money(rec.suggested_amount_gbp) <= ZERO:
        return _gate("no_amount_recommended", f"{rec.action} without a positive amount")

    # --- Caps --------------------------------------------------------------

    requested = money(rec.suggested_amount_gbp)
    total = state.total_value_gbp

    caps: list[tuple[Constraint, Decimal, str]] = [
        ("recommended_amount", requested, f"the model asked for £{requested}"),
        (
            "max_trade_gbp",
            money(limits.max_trade_gbp),
            f"a single trade may not exceed £{money(limits.max_trade_gbp)}",
        ),
    ]

    if rec.action == "BUY":
        # Buying converts cash into stock, so it leaves total portfolio value
        # unchanged and only alters its composition. That is why the
        # concentration and exposure denominators are the *current* total and
        # no fixed-point iteration is needed to find a self-consistent size.
        position_headroom = _headroom(money(limits.max_position_gbp) - held)
        concentration_cap = _pct(total, limits.max_concentration_pct)
        exposure_headroom = _headroom(
            _pct(total, limits.max_total_exposure_pct) - state.invested_gbp
        )
        # Cash comes before the policy limits so that where it ties with one,
        # the physical constraint is the one reported. Note that cash is
        # *dominated* whenever the exposure ceiling is under 100%: exposure
        # headroom is `pct x total - invested` and cash is `total - invested`,
        # so the former is smaller for any pct below 100. It is kept as the
        # backstop for a 100% ceiling and for a state that disagrees with
        # itself, not because it is expected to bind.
        caps += [
            ("available_cash", _headroom(state.cash_gbp), f"£{money(state.cash_gbp)} cash"),
            (
                "max_position_gbp",
                position_headroom,
                f"£{held} of {rec.ticker} held against a £{money(limits.max_position_gbp)} cap",
            ),
            (
                "max_concentration_pct",
                _headroom(concentration_cap - held),
                f"{limits.max_concentration_pct}% of £{total} is £{concentration_cap}",
            ),
            (
                "max_total_exposure_pct",
                exposure_headroom,
                f"£{state.invested_gbp} invested of a "
                f"{limits.max_total_exposure_pct}% exposure ceiling",
            ),
        ]
    else:
        # A sell reduces exposure, so concentration, total exposure and cash
        # cannot constrain it. Only the size of the holding can.
        caps.append(("position_size", held, f"£{held} of {rec.ticker} held"))

    # min() over a list of tuples would compare the constraint name on a tie;
    # this keeps the fixed evaluation order as the tie-break instead.
    binding, approved, _ = min(caps, key=lambda cap: cap[1])

    reasons = [RiskReason(constraint=name, detail=text, cap_gbp=cap) for name, cap, text in caps]

    if approved <= ZERO:
        return _refused(binding, reasons)

    if approved < money(limits.min_trade_gbp):
        # The clamping cap stays in `reasons` as the root cause; the minimum
        # trade size is what actually turned this into a refusal, so it is the
        # binding constraint.
        reasons.append(
            RiskReason(
                constraint="below_min_trade_gbp",
                detail=(
                    f"£{approved} ({binding}) is below the "
                    f"£{money(limits.min_trade_gbp)} minimum trade"
                ),
            )
        )
        return _refused("below_min_trade_gbp", reasons)

    return RiskVerdict(
        approved=True,
        approved_amount_gbp=approved,
        reasons=tuple(reasons),
        binding_constraint=binding,
    )


def _headroom(value: Decimal) -> Decimal:
    """Clamp a remaining-allowance figure at zero.

    A position already over its cap — which a price rise alone can cause,
    without any trade — yields negative headroom, and a negative cap would
    otherwise read as the tightest one and be reported as an approved amount.
    """
    return money(max(value, ZERO))


def _pct(total: Decimal, pct: Decimal) -> Decimal:
    return money(total * pct / 100)
