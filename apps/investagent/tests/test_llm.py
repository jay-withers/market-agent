"""Tests for the LLM boundary.

The request *shape* is what these mostly assert, because the two stages of the
cascade need different shapes and getting one wrong is a 400 from the API
rather than a wrong answer — which means it fails at 06:00 UTC in a container,
not here.

Nothing in this file touches the network or needs an API key: the provider
takes a client, so the tests pass a double that records what it was called
with.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from investagent.llm.anthropic_provider import AnthropicLlm
from investagent.llm.base import PROMPT_VERSION, LlmResult, Usage
from investagent.models import NewsRelevance, Recommendation

D = Decimal


class FakeUsage:
    def __init__(self, input_tokens=1000, output_tokens=200, **extra):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        for key, value in extra.items():
            setattr(self, key, value)


class FakeResponse:
    def __init__(self, parsed, usage=None):
        self.parsed_output = parsed
        self.usage = usage or FakeUsage()


class FakeMessages:
    def __init__(self, responses: list[Any]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class FakeClient:
    def __init__(self, *responses: Any):
        self.messages = FakeMessages(list(responses))


def relevance(**overrides) -> NewsRelevance:
    defaults = dict(
        relevant=True, sentiment="positive", sentiment_score=0.7, rationale="Earnings beat."
    )
    return NewsRelevance(**{**defaults, **overrides})


def recommendation(**overrides) -> Recommendation:
    defaults = dict(
        ticker="NVDA",
        action="BUY",
        confidence=0.8,
        suggested_amount_gbp=D(50),
        reasoning="Datacentre revenue beat.",
        risks="Concentration.",
    )
    return Recommendation(**{**defaults, **overrides})


# ---------------------------------------------------------------------------
# Request shape — the part that differs per stage
# ---------------------------------------------------------------------------


def test_the_analysis_stage_sends_adaptive_thinking_and_effort():
    client = FakeClient(FakeResponse(recommendation()))
    llm = AnthropicLlm(client=client, analysis_model="claude-sonnet-5", analysis_effort="high")

    llm.analyse("Assess NVDA.")

    call = client.messages.calls[0]
    assert call["model"] == "claude-sonnet-5"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"] == {"effort": "high"}


def test_the_filter_stage_sends_neither_thinking_nor_effort():
    """`effort` errors on claude-haiku-4-5, and adaptive thinking is not available."""
    client = FakeClient(FakeResponse(relevance()))
    llm = AnthropicLlm(client=client, filter_model="claude-haiku-4-5")

    llm.filter_news("NVDA", "NVIDIA beats on datacentre revenue", None)

    call = client.messages.calls[0]
    assert call["model"] == "claude-haiku-4-5"
    assert "thinking" not in call
    assert "output_config" not in call


@pytest.mark.parametrize("legacy", ["claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-4-5"])
def test_a_pre_46_model_never_gets_effort_even_as_the_analysis_model(legacy):
    client = FakeClient(FakeResponse(recommendation()))

    AnthropicLlm(client=client, analysis_model=legacy).analyse("Assess NVDA.")

    assert "output_config" not in client.messages.calls[0]


def test_a_current_model_gets_effort_even_as_the_filter_model():
    client = FakeClient(FakeResponse(relevance()))

    AnthropicLlm(client=client, filter_model="claude-sonnet-5").filter_news("NVDA", "h", None)

    assert client.messages.calls[0]["thinking"] == {"type": "adaptive"}


@pytest.mark.parametrize("rejected", ["temperature", "top_p", "top_k", "budget_tokens"])
def test_no_stage_sends_a_parameter_the_current_models_reject(rejected):
    client = FakeClient(FakeResponse(relevance()), FakeResponse(recommendation()))
    llm = AnthropicLlm(client=client)

    llm.filter_news("NVDA", "h", None)
    llm.analyse("Assess NVDA.")

    for call in client.messages.calls:
        assert rejected not in call
        assert rejected not in call.get("thinking", {})


def test_neither_stage_sends_an_assistant_prefill():
    """A trailing assistant message is a 400 on both models."""
    client = FakeClient(FakeResponse(relevance()), FakeResponse(recommendation()))
    llm = AnthropicLlm(client=client)

    llm.filter_news("NVDA", "h", None)
    llm.analyse("Assess NVDA.")

    for call in client.messages.calls:
        assert [m["role"] for m in call["messages"]] == ["user"]


def test_both_stages_request_a_validated_pydantic_model():
    client = FakeClient(FakeResponse(relevance()), FakeResponse(recommendation()))
    llm = AnthropicLlm(client=client)

    llm.filter_news("NVDA", "h", None)
    llm.analyse("Assess NVDA.")

    assert client.messages.calls[0]["output_format"] is NewsRelevance
    assert client.messages.calls[1]["output_format"] is Recommendation


def test_the_prompt_version_is_carried_in_both_system_prompts():
    client = FakeClient(FakeResponse(relevance()), FakeResponse(recommendation()))
    llm = AnthropicLlm(client=client)

    llm.filter_news("NVDA", "h", None)
    llm.analyse("Assess NVDA.")

    for call in client.messages.calls:
        assert PROMPT_VERSION in call["system"]


def test_a_summary_is_included_in_the_filter_prompt_when_there_is_one():
    client = FakeClient(FakeResponse(relevance()), FakeResponse(relevance()))
    llm = AnthropicLlm(client=client)

    llm.filter_news("NVDA", "Headline", "A summary.")
    llm.filter_news("NVDA", "Headline", None)

    assert "A summary." in client.messages.calls[0]["messages"][0]["content"]
    assert "Summary:" not in client.messages.calls[1]["messages"][0]["content"]


# ---------------------------------------------------------------------------
# Results and cost
# ---------------------------------------------------------------------------


def test_the_parsed_output_is_returned_with_its_model_and_usage():
    client = FakeClient(FakeResponse(recommendation(), FakeUsage(9800, 1400)))

    result = AnthropicLlm(client=client, analysis_model="claude-sonnet-5").analyse("x")

    assert isinstance(result, LlmResult)
    assert result.value.action == "BUY"
    assert result.model == "claude-sonnet-5"
    assert result.usage.input_tokens == 9800
    assert result.usage.output_tokens == 1400


def test_a_wrong_parsed_type_fails_loudly_rather_than_downstream():
    client = FakeClient(FakeResponse(relevance()))

    with pytest.raises(TypeError, match="expected Recommendation"):
        AnthropicLlm(client=client).analyse("x")


def test_missing_cache_usage_fields_are_treated_as_zero():
    client = FakeClient(FakeResponse(recommendation(), FakeUsage(100, 10)))

    result = AnthropicLlm(client=client).analyse("x")

    assert result.usage.cache_write_tokens == 0
    assert result.usage.cache_read_tokens == 0


def test_cache_usage_fields_reported_as_none_are_treated_as_zero():
    """The SDK reports these as None rather than 0 on an uncached response."""
    usage = FakeUsage(100, 10, cache_creation_input_tokens=None, cache_read_input_tokens=None)
    client = FakeClient(FakeResponse(recommendation(), usage))

    result = AnthropicLlm(client=client).analyse("x")

    assert result.cost_usd > 0


def test_sonnet_cost_is_computed_from_the_published_rate():
    # 1M input at $2 + 1M output at $10.
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)

    assert usage.cost_usd("claude-sonnet-5") == D("12.000000")


def test_haiku_cost_is_computed_from_the_published_rate():
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)

    assert usage.cost_usd("claude-haiku-4-5") == D("6.000000")


def test_a_realistic_run_costs_a_fraction_of_a_cent():
    usage = Usage(input_tokens=9800, output_tokens=1400)

    assert usage.cost_usd("claude-sonnet-5") == D("0.033600")


def test_cached_tokens_are_priced_at_their_multipliers():
    usage = Usage(
        input_tokens=0, output_tokens=0, cache_write_tokens=1_000_000, cache_read_tokens=1_000_000
    )

    # $2 x 1.25 written + $2 x 0.10 read.
    assert usage.cost_usd("claude-sonnet-5") == D("2.700000")


def test_an_unpriced_model_costs_zero_rather_than_failing_the_run():
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)

    assert usage.cost_usd("claude-something-unreleased") == D(0)


def test_usage_accumulates_across_the_calls_in_a_run():
    total = Usage(100, 10) + Usage(200, 20) + Usage(0, 0, cache_read_tokens=50)

    assert total == Usage(300, 30, cache_write_tokens=0, cache_read_tokens=50)


def test_cost_is_quantized_to_the_six_places_the_column_holds():
    usage = Usage(input_tokens=1, output_tokens=1)

    assert usage.cost_usd("claude-sonnet-5").as_tuple().exponent == -6
