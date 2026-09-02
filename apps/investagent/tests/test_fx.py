"""Tests for the GBP/USD rate and the conversions built on it."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from investagent.fetch import FetchError
from investagent.fx import FxRate, fetch_gbp_usd
from tests.helpers import json_client

D = Decimal

LIVE_SHAPE = {"amount": 1.0, "base": "GBP", "date": "2026-09-02", "rates": {"USD": 1.3483}}


def test_the_published_rate_and_its_date_are_both_captured():
    rate = fetch_gbp_usd(client=json_client(LIVE_SHAPE))

    assert rate.gbp_usd == D("1.348300")
    assert rate.as_of == date(2026, 9, 2)
    assert rate.source == "frankfurter"


def test_a_404_body_is_reported_as_an_unexpected_response():
    """The wrong URL returns valid JSON with an HTTP 200, not a transport error."""
    with pytest.raises(FetchError, match="unexpected response"):
        fetch_gbp_usd(client=json_client({"status": 404, "message": "not found"}))


def test_an_implausible_rate_is_refused():
    payload = {**LIVE_SHAPE, "rates": {"USD": 0}}

    with pytest.raises(FetchError, match="implausible"):
        fetch_gbp_usd(client=json_client(payload))


def test_the_rate_is_read_via_str_so_no_float_artefact_is_stored():
    payload = {**LIVE_SHAPE, "rates": {"USD": 1.1}}

    assert fetch_gbp_usd(client=json_client(payload)).gbp_usd == D("1.100000")


def test_gbp_converts_to_whole_cents_rounding_down():
    rate = FxRate(gbp_usd=D("1.3483"), as_of=date(2026, 9, 2))

    # 20 x 1.3483 = 26.966, which must not become 26.97 and exceed the
    # approved amount.
    assert rate.to_usd(D("20.0000")) == D("26.96")


def test_usd_converts_back_to_gbp_at_the_money_scale():
    rate = FxRate(gbp_usd=D("1.3483"), as_of=date(2026, 9, 2))

    assert rate.to_gbp(D("26.91")) == D("19.9584")


def test_a_round_trip_never_returns_more_than_it_started_with():
    rate = FxRate(gbp_usd=D("1.3483"), as_of=date(2026, 9, 2))

    assert rate.to_gbp(rate.to_usd(D("50.0000"))) <= D("50.0000")
