"""Tests for the LLM boundary.

DeepSeek's API is plain OpenAI-shaped REST, so there is no SDK layer to test
separately from the wire request/response — unlike the Anthropic
implementation this replaced, which needed a fake-client double *and* a
second file driving the real SDK through a mock transport, because the SDK
sat between this code and the bytes actually sent. Here both collapse into
one: an `httpx.Client` backed by `MockTransport` captures the request bodies
that would go on the wire, the same pattern `fetch.py`'s other callers are
already tested with.

Nothing in this file touches the network or needs an API key.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from marketagent.fetch import TIMEOUT_SECONDS
from marketagent.llm import deepseek_provider
from marketagent.llm.base import PROMPT_VERSION, LlmResult, Usage
from marketagent.llm.deepseek_provider import (
    MAX_STRUCTURED_ATTEMPTS,
    REASONING_TIMEOUT_SECONDS,
    DeepseekLlm,
    StructuredOutputError,
    balance_usd,
)

D = Decimal

RECOMMENDATION_JSON = {
    "ticker": "NVDA",
    "action": "BUY",
    "confidence": 0.8,
    "suggested_amount_usd": 50,
    "reasoning": "Datacentre revenue beat.",
    "risks": "Concentration.",
}

RELEVANCE_JSON = {
    "relevant": True,
    "sentiment": "positive",
    "sentiment_score": 0.7,
    "rationale": "Earnings beat.",
}

REVIEW_JSON = {
    "assessment": "A quiet week.",
    "proposals": [
        {
            "area": "risk_limits",
            "change": "Raise RISK_MAX_DAILY_TRADES from 3 to 5.",
            "rationale": "daily_trade_limit refused 31 of 51 decisions.",
            "expected_effect": "More of the model's BUYs reach the broker.",
            "confidence": 0.7,
        }
    ],
}


def _completion(body: dict, *, prompt_tokens=9800, completion_tokens=1400, cache_hit=0) -> dict:
    return {
        "id": "chatcmpl-1",
        "model": "deepseek-v4-pro",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(body)}}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "prompt_cache_hit_tokens": cache_hit,
        },
    }


def ok(body: dict, **usage) -> tuple[int, dict]:
    return 200, _completion(body, **usage)


class Wire:
    """A scripted queue of HTTP responses, and the request bodies actually sent."""

    def __init__(self, *responses: tuple[int, dict]):
        self.requests: list[dict] = []
        self._responses = list(responses)

    def client(self) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(json.loads(request.content))
            status, body = self._responses.pop(0)
            return httpx.Response(status, json=body)

        return httpx.Client(transport=httpx.MockTransport(handler))


def llm(wire: Wire, **kwargs) -> DeepseekLlm:
    return DeepseekLlm(client=wire.client(), **kwargs)


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


def test_the_analysis_stage_sends_reasoning_effort():
    wire = Wire(ok(RECOMMENDATION_JSON))

    llm(wire, analysis_model="deepseek-v4-pro", analysis_effort="high").analyse("Assess NVDA.")

    body = wire.requests[0]
    assert body["model"] == "deepseek-v4-pro"
    assert body["reasoning_effort"] == "high"
    assert "thinking" not in body


def test_the_filter_stage_disables_reasoning_outright():
    wire = Wire(ok(RELEVANCE_JSON))

    llm(wire, filter_model="deepseek-flash").filter_news(
        "NVDA", "NVIDIA beats on datacentre revenue", None
    )

    body = wire.requests[0]
    assert body["model"] == "deepseek-flash"
    assert body["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in body


def test_a_reasoning_call_gets_a_longer_read_timeout_than_fetchs_default(monkeypatch):
    """fetch.py's 30s default is sized for Alpaca/Frankfurter/Wikipedia GETs.
    A real run failed after three retries on exactly this: a reasoning_effort
    call that ordinarily takes 15-30s to answer had almost no margin left."""
    captured: dict[str, object] = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return {
            "choices": [{"message": {"content": json.dumps(RECOMMENDATION_JSON)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        }

    monkeypatch.setattr(deepseek_provider, "post_json_retrying", fake_post)

    DeepseekLlm().analyse("Assess NVDA.")

    assert captured["timeout"] == REASONING_TIMEOUT_SECONDS
    assert REASONING_TIMEOUT_SECONDS > TIMEOUT_SECONDS


def test_the_filter_stage_keeps_fetchs_ordinary_timeout(monkeypatch):
    """Reasoning is disabled outright for the filter stage, so it stays fast
    and does not need the longer allowance the analysis stages get."""
    captured: dict[str, object] = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return {
            "choices": [{"message": {"content": json.dumps(RELEVANCE_JSON)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        }

    monkeypatch.setattr(deepseek_provider, "post_json_retrying", fake_post)

    DeepseekLlm().filter_news("NVDA", "h", None)

    assert captured["timeout"] == TIMEOUT_SECONDS


def test_both_stages_request_json_object_mode():
    wire = Wire(ok(RELEVANCE_JSON), ok(RECOMMENDATION_JSON))
    provider = llm(wire)

    provider.filter_news("NVDA", "h", None)
    provider.analyse("Assess NVDA.")

    for body in wire.requests:
        assert body["response_format"] == {"type": "json_object"}


def test_the_prompt_version_is_carried_in_both_system_prompts():
    wire = Wire(ok(RELEVANCE_JSON), ok(RECOMMENDATION_JSON))
    provider = llm(wire)

    provider.filter_news("NVDA", "h", None)
    provider.analyse("Assess NVDA.")

    for body in wire.requests:
        system = next(m["content"] for m in body["messages"] if m["role"] == "system")
        assert PROMPT_VERSION in system


def test_a_summary_is_included_in_the_filter_prompt_when_there_is_one():
    wire = Wire(ok(RELEVANCE_JSON), ok(RELEVANCE_JSON))
    provider = llm(wire)

    provider.filter_news("NVDA", "Headline", "A summary.")
    provider.filter_news("NVDA", "Headline", None)

    user0 = next(m["content"] for m in wire.requests[0]["messages"] if m["role"] == "user")
    user1 = next(m["content"] for m in wire.requests[1]["messages"] if m["role"] == "user")
    assert "A summary." in user0
    assert "Summary:" not in user1


def test_the_weekly_review_goes_to_the_analysis_model_with_its_own_system_prompt():
    """One call a week where the reasoning is the product, against the filter
    stage's tens of thousands — so it gets the capable model, not the cheap one."""
    wire = Wire(ok(REVIEW_JSON))

    result = llm(
        wire,
        filter_model="deepseek-flash",
        analysis_model="deepseek-v4-pro",
        analysis_effort="high",
    ).review("The week in figures.")

    body = wire.requests[0]
    assert body["model"] == "deepseek-v4-pro"
    assert body["reasoning_effort"] == "high"
    assert result.value.proposals[0].area == "risk_limits"


def test_the_review_prompt_tells_the_model_its_proposals_are_advisory():
    wire = Wire(ok(REVIEW_JSON))

    llm(wire).review("The week in figures.")

    system = next(m["content"] for m in wire.requests[0]["messages"] if m["role"] == "system")
    assert "advisory" in system
    assert "do not propose a specific trade" in system


def test_the_schema_sent_to_the_model_carries_every_fields_description():
    wire = Wire(ok(RECOMMENDATION_JSON))

    llm(wire).analyse("Assess NVDA.")

    system = next(m["content"] for m in wire.requests[0]["messages"] if m["role"] == "system")
    schema = json.loads(system.split("matching exactly this JSON schema:\n", 1)[1])
    assert all("description" in field for field in schema["properties"].values())
    assert "json" in system.lower()  # DeepSeek rejects json_object mode without it


def test_the_nested_proposal_model_reaches_the_schema_with_its_own_descriptions():
    """`proposals` is a list of a second Pydantic model, which pydantic renders
    as a `$defs` entry behind a `$ref` — easy to lose without noticing."""
    wire = Wire(ok(REVIEW_JSON))

    llm(wire).review("The week in figures.")

    system = next(m["content"] for m in wire.requests[0]["messages"] if m["role"] == "system")
    schema = json.loads(system.split("matching exactly this JSON schema:\n", 1)[1])
    proposal = schema["$defs"]["ProposedChange"]
    assert all("description" in field for field in proposal["properties"].values())
    assert "risk_limits" in proposal["properties"]["area"]["enum"]


# ---------------------------------------------------------------------------
# Results, usage and the structured-output retry
# ---------------------------------------------------------------------------


def test_the_parsed_output_is_returned_with_its_model_and_usage():
    wire = Wire(ok(RECOMMENDATION_JSON, prompt_tokens=9800, completion_tokens=1400))

    result = llm(wire, analysis_model="deepseek-v4-pro").analyse("x")

    assert isinstance(result, LlmResult)
    assert result.value.action == "BUY"
    assert result.model == "deepseek-v4-pro"
    assert result.usage.input_tokens == 9800
    assert result.usage.output_tokens == 1400


def test_cache_hit_tokens_are_a_subset_of_input_tokens_not_additional():
    wire = Wire(ok(RECOMMENDATION_JSON, prompt_tokens=9800, cache_hit=3000))

    result = llm(wire).analyse("x")

    assert result.usage.input_tokens == 9800
    assert result.usage.cache_read_tokens == 3000


def test_a_missing_cache_hit_field_is_treated_as_zero():
    wire = Wire(ok(RECOMMENDATION_JSON))  # no cache_hit kwarg -> reported as 0

    result = llm(wire).analyse("x")

    assert result.usage.cache_read_tokens == 0


def test_a_response_failing_schema_validation_is_retried_with_the_error():
    """json_object mode guarantees valid JSON, not schema conformance — a
    response missing a required field parses as JSON but not as the model."""
    wire = Wire(ok({"ticker": "NVDA"}), ok(RECOMMENDATION_JSON))

    result = llm(wire).analyse("Assess NVDA.")

    assert len(wire.requests) == 2
    assert result.value.action == "BUY"
    # The retry carries the bad response and a correction, on top of the
    # original system+user pair.
    second_call_messages = wire.requests[1]["messages"]
    assert [m["role"] for m in second_call_messages] == ["system", "user", "assistant", "user"]


def test_two_failed_validations_raise_structured_output_error():
    wire = Wire(*[ok({"ticker": "NVDA"}, prompt_tokens=500)] * MAX_STRUCTURED_ATTEMPTS)

    with pytest.raises(StructuredOutputError, match="Recommendation") as excinfo:
        llm(wire).analyse("Assess NVDA.")

    assert len(wire.requests) == MAX_STRUCTURED_ATTEMPTS
    # The failed attempts still cost real tokens; the exception carries them
    # so a caller can still account for the spend.
    assert excinfo.value.usage.input_tokens == 500 * MAX_STRUCTURED_ATTEMPTS


def test_usage_across_a_failed_then_retried_call_is_still_accumulated():
    """Every attempt costs real tokens, including the one that failed to parse."""
    wire = Wire(
        ok({"ticker": "NVDA"}, prompt_tokens=1000, completion_tokens=50),
        ok(RECOMMENDATION_JSON, prompt_tokens=1200, completion_tokens=60),
    )

    result = llm(wire).analyse("Assess NVDA.")

    assert result.usage.input_tokens == 1000 + 1200
    assert result.usage.output_tokens == 50 + 60


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def test_deepseek_v4_pro_cost_is_computed_from_the_published_peak_rate():
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)

    assert usage.cost_usd("deepseek-v4-pro") == D("5.280000")


def test_deepseek_flash_cost_is_computed_from_the_published_peak_rate():
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)

    assert usage.cost_usd("deepseek-flash") == D("1.500000")


def test_cache_hit_tokens_are_priced_at_the_hit_rate_not_the_miss_rate():
    # All 1M input tokens were a cache hit: priced at $0.006/MTok, not $0.30.
    usage = Usage(input_tokens=1_000_000, output_tokens=0, cache_read_tokens=1_000_000)

    assert usage.cost_usd("deepseek-flash") == D("0.006000")


def test_a_realistic_run_costs_a_fraction_of_a_cent():
    # 9800 x $0.30 miss-rate input + 1400 x $1.20 output, per million tokens.
    usage = Usage(input_tokens=9800, output_tokens=1400)

    assert usage.cost_usd("deepseek-flash") == D("0.004620")


def test_an_unpriced_model_costs_zero_rather_than_failing_the_run():
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)

    assert usage.cost_usd("deepseek-something-unreleased") == D(0)


def test_usage_accumulates_across_the_calls_in_a_run():
    total = Usage(100, 10) + Usage(200, 20) + Usage(0, 0, cache_read_tokens=50)

    assert total == Usage(300, 30, cache_write_tokens=0, cache_read_tokens=50)


def test_cost_is_quantized_to_the_six_places_the_column_holds():
    usage = Usage(input_tokens=1, output_tokens=1)

    assert usage.cost_usd("deepseek-flash").as_tuple().exponent == -6


# ---------------------------------------------------------------------------
# Account balance
# ---------------------------------------------------------------------------


def test_balance_usd_returns_the_usd_denominated_entry():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-deepseek-key"
        return httpx.Response(
            200,
            json={
                "is_available": True,
                "balance_infos": [
                    {
                        "currency": "CNY",
                        "total_balance": "12.34",
                        "granted_balance": "0",
                        "topped_up_balance": "12.34",
                    },
                    {
                        "currency": "USD",
                        "total_balance": "42.50",
                        "granted_balance": "0",
                        "topped_up_balance": "42.50",
                    },
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    assert balance_usd(client=client) == D("42.50")


def test_balance_usd_raises_when_the_account_has_no_usd_entry():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "is_available": True,
                "balance_infos": [
                    {
                        "currency": "CNY",
                        "total_balance": "12.34",
                        "granted_balance": "0",
                        "topped_up_balance": "12.34",
                    }
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(Exception, match="USD"):
        balance_usd(client=client)
