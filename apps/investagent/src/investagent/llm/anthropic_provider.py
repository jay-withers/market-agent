"""The Anthropic implementation of the two-stage cascade.

**The two stages need different request shapes, and that is the whole reason
this file is more than a single generic method.**

`claude-sonnet-5` is a current model: it takes `thinking={"type": "adaptive"}`
and `output_config={"effort": ...}`, and it rejects `budget_tokens`,
`temperature`, `top_p` and `top_k` with a 400. `claude-haiku-4-5` predates
4.6 and is the mirror image: `effort` errors, adaptive thinking does not exist
for it, and sampling parameters are allowed. So the filter stage sends neither
thinking nor effort, and only the analysis stage sends both. Neither model
accepts an assistant prefill.

Structured output uses the `messages.parse()` helper with `output_format=` a
Pydantic model, reading `response.parsed_output`. Note that the `output_format`
*parameter* on `messages.create()` is deprecated in favour of
`output_config={"format": ...}` — that is a different thing from the
`output_format=` keyword on `parse()`, which is current.
"""

from __future__ import annotations

from typing import Any

import anthropic

from ..models import NewsRelevance, Recommendation
from ..settings import secret, settings
from .base import PROMPT_VERSION, LlmResult, Usage

# Models that predate the 4.6 family, where `effort` errors and adaptive
# thinking is unavailable. Checked by prefix rather than an exact list so a
# dated snapshot id does not silently fall through to the wrong request shape.
LEGACY_MODEL_PREFIXES = ("claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-4-5")

FILTER_SYSTEM = f"""You screen financial news for an automated equity research \
system. Prompt version {PROMPT_VERSION}.

For the given ticker and article, decide whether the article is worth the cost \
of a full investment analysis. Relevant means it could plausibly move the \
price or change the investment case: earnings, guidance, regulation, \
litigation, products, management, or a material sector event.

Not relevant: listicles, "top N stocks" content, generic market commentary, \
price-movement recaps with no cause, and articles that only mention the \
company in passing.

Be strict. A false positive costs an expensive analysis call; a false negative \
costs one day's awareness of one article."""

ANALYSIS_SYSTEM = f"""You are the analysis stage of an automated paper-trading \
system running a notional GBP 500 portfolio. Prompt version {PROMPT_VERSION}.

Recommend BUY, SELL or HOLD for the single ticker given, with a confidence \
between 0 and 1 and, for BUY or SELL, a suggested size in GBP.

Three things to understand about your role:

1. A deterministic risk engine runs after you and has the final say. It will \
clamp or refuse your suggestion against position limits, concentration limits, \
available cash, a daily trade budget and a minimum trade size. Nothing you \
write can raise a limit, so do not argue with them — suggest what you think is \
right and let the engine decide what is permitted.
2. HOLD is a real answer and usually the correct one. You are asked about \
every ticker every day; most days, on most tickers, nothing has happened that \
justifies a trade. Do not manufacture conviction.
3. Your reasoning and risks are stored and read by a human later. Be specific \
about what in the evidence drove the call. State the strongest argument \
against your own recommendation in the risks field."""


class AnthropicLlm:
    """Implements the `Llm` protocol against the Anthropic API."""

    def __init__(
        self,
        client: Any | None = None,
        filter_model: str | None = None,
        analysis_model: str | None = None,
        analysis_effort: str | None = None,
    ) -> None:
        cfg = settings()
        self.filter_model = filter_model or cfg.filter_model
        self.analysis_model = analysis_model or cfg.analysis_model
        self.analysis_effort = analysis_effort or cfg.analysis_effort
        # Constructed lazily so importing this module needs neither an API key
        # nor a network, and so tests can pass a double.
        self._client = client
        # max_tokens is generous for the analysis stage because a truncated
        # response is a wasted call, and small for the filter because its
        # output is four short fields.
        self._filter_max_tokens = 1024
        self._analysis_max_tokens = 8192

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=secret("ANTHROPIC-API-KEY"))
        return self._client

    def filter_news(
        self, ticker: str, headline: str, summary: str | None
    ) -> LlmResult[NewsRelevance]:
        content = f"Ticker: {ticker}\nHeadline: {headline}"
        if summary:
            content += f"\nSummary: {summary}"

        response = self.client.messages.parse(
            model=self.filter_model,
            max_tokens=self._filter_max_tokens,
            system=FILTER_SYSTEM,
            messages=[{"role": "user", "content": content}],
            output_format=NewsRelevance,
            **self._reasoning_params(self.filter_model),
        )
        return self._result(response, self.filter_model, NewsRelevance)

    def analyse(self, prompt: str) -> LlmResult[Recommendation]:
        response = self.client.messages.parse(
            model=self.analysis_model,
            max_tokens=self._analysis_max_tokens,
            system=ANALYSIS_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_format=Recommendation,
            **self._reasoning_params(self.analysis_model),
        )
        return self._result(response, self.analysis_model, Recommendation)

    def _reasoning_params(self, model: str) -> dict[str, Any]:
        """Thinking and effort, or nothing at all on a pre-4.6 model.

        Sending `effort` to `claude-haiku-4-5` is an error, and adaptive
        thinking is not available for it — so the filter stage gets an empty
        dict rather than a downgraded equivalent.
        """
        if model.startswith(LEGACY_MODEL_PREFIXES):
            return {}
        return {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.analysis_effort},
        }

    @staticmethod
    def _result[T](response: Any, model: str, expected: type[T]) -> LlmResult[T]:
        parsed = response.parsed_output
        if not isinstance(parsed, expected):
            # Belt and braces: parse() validates against the schema, so this
            # fires only if the SDK's contract changes under us. Better here
            # than as an AttributeError three frames into the risk engine.
            raise TypeError(f"expected {expected.__name__}, got {type(parsed).__name__}")

        usage = response.usage
        return LlmResult(
            value=parsed,
            model=model,
            usage=Usage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                # Absent on a response that used no caching, and reported as
                # None rather than 0 by some SDK versions.
                cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            ),
        )
