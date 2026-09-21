"""Tests for the risk limit configuration."""

from __future__ import annotations

from decimal import Decimal

from marketagent import risklimits

D = Decimal


def test_the_watchlist_becomes_the_allowlist():
    limits = risklimits.limits(frozenset({"NVDA", "AAPL"}))

    assert limits.allowed_tickers == frozenset({"NVDA", "AAPL"})


def test_an_empty_watchlist_permits_nothing():
    """Fail-closed, so an unseeded database cannot trade."""
    assert risklimits.limits(frozenset()).allowed_tickers == frozenset()


def test_the_defaults_are_sized_for_the_usd_paper_account():
    limits = risklimits.limits(frozenset({"NVDA"}))

    assert limits.max_position_usd == D(20000)
    assert limits.max_trade_usd == D(10000)
    assert limits.min_trade_usd == D(5)
    assert limits.max_daily_trades == 10


def test_limits_are_overridable_by_environment(monkeypatch):
    monkeypatch.setenv("RISK_MAX_TRADE_USD", "25")
    monkeypatch.setenv("RISK_MAX_DAILY_TRADES", "1")

    limits = risklimits.limits(frozenset({"NVDA"}))

    assert limits.max_trade_usd == D(25)
    assert limits.max_daily_trades == 1


def test_the_absolute_position_cap_binds_before_concentration_in_a_fresh_portfolio():
    """25% of USD 100,000 is USD 25,000, so the USD 20,000 cap is the tighter one."""
    limits = risklimits.limits(frozenset({"NVDA"}))
    concentration_at_100000 = D(100000) * limits.max_concentration_pct / 100

    assert limits.max_position_usd < concentration_at_100000
