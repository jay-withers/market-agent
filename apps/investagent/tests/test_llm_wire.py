"""Tests that go through the real Anthropic SDK, with a mock HTTP transport.

Everything in `test_llm.py` passes a fake client, which proves this code calls
what it means to call but not that the SDK accepts it or sends what we expect.
These tests drive the genuine `anthropic.Anthropic` client and intercept at the
transport, so the request body asserted below is the one that would go on the
wire — no API key, no network.

This is the layer that caught two real problems: `messages.parse()` builds its
JSON schema from the Pydantic model and sends the class docstring as the
schema's `description`, so internal notes leak to the model; and a `Decimal`
field renders as a three-branch `anyOf` with a Decimal regex.

`httpx2` (not `httpx` — the 1.x SDK moved) is a required dependency of
`anthropic`, so it is always present without declaring it ourselves.
"""

from __future__ import annotations

import json

import httpx2
import pytest

from investagent.llm.anthropic_provider import AnthropicLlm

RECOMMENDATION_JSON = {
    "ticker": "NVDA",
    "action": "BUY",
    "confidence": 0.8,
    "suggested_amount_gbp": 50,
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


@pytest.fixture
def wire():
    """A real SDK client whose requests are captured instead of sent."""
    import anthropic

    captured: list[dict] = []
    payload: dict = {"body": RECOMMENDATION_JSON}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": json.dumps(payload["body"])}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 9800, "output_tokens": 1400},
            },
        )

    client = anthropic.Anthropic(
        api_key="test-key-not-used",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    return client, captured, payload


def test_the_analysis_request_the_sdk_would_send(wire):
    client, captured, _ = wire

    result = AnthropicLlm(
        client=client, analysis_model="claude-sonnet-5", analysis_effort="high"
    ).analyse("Assess NVDA.")

    body = captured[0]
    assert body["model"] == "claude-sonnet-5"
    assert body["thinking"] == {"type": "adaptive"}
    # parse() merges its own `format` into output_config without clobbering the
    # effort we set — worth pinning, since both live under the same key.
    assert body["output_config"]["effort"] == "high"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert [m["role"] for m in body["messages"]] == ["user"]
    assert result.value.action == "BUY"
    assert result.usage.input_tokens == 9800


def test_the_filter_request_the_sdk_would_send(wire):
    client, captured, payload = wire
    payload["body"] = RELEVANCE_JSON

    AnthropicLlm(client=client, filter_model="claude-haiku-4-5").filter_news(
        "NVDA", "NVIDIA beats on datacentre revenue", None
    )

    body = captured[0]
    assert body["model"] == "claude-haiku-4-5"
    # The pre-4.6 model: effort would error and adaptive thinking does not
    # exist, so neither may appear. `format` still must.
    assert "thinking" not in body
    assert "effort" not in body.get("output_config", {})
    assert body["output_config"]["format"]["type"] == "json_schema"


@pytest.mark.parametrize("rejected", ["temperature", "top_p", "top_k"])
def test_the_sdk_sends_no_parameter_the_current_models_reject(wire, rejected):
    client, captured, _ = wire

    AnthropicLlm(client=client).analyse("Assess NVDA.")

    assert rejected not in captured[0]


def test_the_schema_sent_to_the_model_leaks_no_internal_notes(wire):
    """The class docstring becomes the schema description the model reads.

    A regression test: the docstring used to explain Python Decimal coercion,
    which cost tokens and told the model about implementation details it can do
    nothing with.
    """
    client, captured, _ = wire

    AnthropicLlm(client=client).analyse("Assess NVDA.")

    schema = captured[0]["output_config"]["format"]["schema"]
    description = schema["description"]
    assert len(description) < 120
    for leak in ("Decimal", "risk engine quantizes", "float", "downstream", "anyOf"):
        assert leak not in description


def test_the_suggested_amount_is_a_plain_nullable_number_in_the_schema(wire):
    """Not a Decimal's three-branch anyOf with a regex, which a model reads badly."""
    client, captured, _ = wire

    AnthropicLlm(client=client).analyse("Assess NVDA.")

    field = captured[0]["output_config"]["format"]["schema"]["properties"]["suggested_amount_gbp"]
    assert field["anyOf"] == [{"type": "number"}, {"type": "null"}]


def test_every_field_carries_a_description_for_the_model(wire):
    client, captured, _ = wire

    AnthropicLlm(client=client).analyse("Assess NVDA.")

    properties = captured[0]["output_config"]["format"]["schema"]["properties"]
    assert all("description" in field for field in properties.values())


# ---------------------------------------------------------------------------
# The weekly review, whose schema is the only nested one in the system
# ---------------------------------------------------------------------------


def test_the_weekly_review_request_the_sdk_would_send(wire):
    client, captured, payload = wire
    payload["body"] = REVIEW_JSON

    result = AnthropicLlm(
        client=client, analysis_model="claude-sonnet-5", analysis_effort="high"
    ).review("The week in figures.")

    body = captured[0]
    assert body["model"] == "claude-sonnet-5"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["effort"] == "high"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert result.value.proposals[0].change.startswith("Raise RISK_MAX_DAILY_TRADES")


def test_the_nested_proposal_model_reaches_the_schema_with_its_own_descriptions(wire):
    """`proposals` is a list of a second Pydantic model, which pydantic renders
    as a `$defs` entry behind a `$ref` — so its field descriptions sit a level
    down from every other schema here and are easy to lose without noticing."""
    client, captured, payload = wire
    payload["body"] = REVIEW_JSON

    AnthropicLlm(client=client).review("The week in figures.")

    schema = captured[0]["output_config"]["format"]["schema"]
    assert schema["properties"]["proposals"]["items"] == {"$ref": "#/$defs/ProposedChange"}

    proposal = schema["$defs"]["ProposedChange"]
    assert all("description" in field for field in proposal["properties"].values())
    # The closed set of areas, which is what keeps the stored column groupable.
    assert "risk_limits" in proposal["properties"]["area"]["enum"]


def test_the_review_schema_descriptions_are_written_for_the_model(wire):
    """Both docstrings become schema descriptions the model reads, so neither
    may carry a note meant for a maintainer — the trap `Recommendation` fell
    into once already."""
    client, captured, payload = wire
    payload["body"] = REVIEW_JSON

    AnthropicLlm(client=client).review("The week in figures.")

    schema = captured[0]["output_config"]["format"]["schema"]
    for description in (schema["description"], schema["$defs"]["ProposedChange"]["description"]):
        for leak in ("jsonb", "Decimal", "dashboard", "column", "risklimits.py"):
            assert leak not in description


def test_an_empty_proposal_list_survives_the_round_trip(wire):
    """Proposing nothing is a real answer, and the field is optional rather
    than required — a week that supports no change must not fail validation."""
    client, captured, payload = wire
    payload["body"] = {"assessment": "Nothing happened worth changing anything over."}

    result = AnthropicLlm(client=client).review("The week in figures.")

    assert result.value.proposals == []
    assert "proposals" not in captured[0]["output_config"]["format"]["schema"]["required"]
