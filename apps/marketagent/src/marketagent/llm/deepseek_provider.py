"""The DeepSeek implementation of the two-stage cascade.

DeepSeek's API is OpenAI-shaped REST, not Anthropic's Messages API, and it has
no schema-guaranteed structured output — only `response_format: {"type":
"json_object"}`, which guarantees valid JSON but not conformance to any
particular schema. The Anthropic implementation this replaced got that
guarantee for free from `messages.parse()`; here, every call embeds the
Pydantic model's own JSON schema in the prompt, parses the response by hand,
and retries once with the validation error fed back to the model if it
doesn't conform. That retry loop is the one piece of genuinely new complexity
this migration introduces, and the first place to look if a scheduled run
starts failing after it went in.

Reasoning is a request-level dial here, not a per-model fact: the filter
stage disables it outright (`"thinking": {"type": "disabled"}`), matching the
old cascade's cheap/fast stage, and the analysis/narrate/review stages ask
for it at `reasoning_effort` (`settings().analysis_effort` — one of
low/high/max, DeepSeek's own scale, not Anthropic's five-step one). Neither
stage uses an assistant prefill — DeepSeek supports it (unlike Anthropic),
but nothing here needs it.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from ..accounts import SHARED_DEEPSEEK_KEY
from ..fetch import TIMEOUT_SECONDS, FetchError, get_json, post_json_retrying
from ..models import DailyNarrative, NewsRelevance, Recommendation, WeeklyReview
from ..settings import secret, settings
from .base import PROMPT_VERSION, LlmResult, Usage

API_BASE_URL = "https://api.deepseek.com"

# fetch.py's TIMEOUT_SECONDS (30s) is sized for Alpaca/Frankfurter/Wikipedia
# GETs, not this: a reasoning_effort="high" analysis/narrate/review call
# routinely takes 15-30s to answer even when nothing is wrong, which leaves a
# 30s read timeout almost no margin — confirmed by a real run failing on
# exactly that after three retries. The filter stage disables reasoning
# outright and stays fast, so it keeps fetch.py's ordinary default instead.
REASONING_TIMEOUT_SECONDS = 180.0

# One retry: json_object mode guarantees valid JSON but not schema
# conformance, so a response that parses fine as JSON can still be missing a
# required field or hold the wrong type. Fed the validation error back and
# asked again once before giving up — twice failing to produce a conforming
# object from a fixed schema reads as a model or prompt problem worth
# surfacing as a failed run, not something a third attempt is likely to fix.
MAX_STRUCTURED_ATTEMPTS = 2


class StructuredOutputError(RuntimeError):
    """DeepSeek never returned JSON conforming to the requested schema.

    Carries `usage` for what the failed attempts actually cost: real tokens
    are spent on every attempt, including the ones that failed to parse, and
    a caller that wants that reflected in a cost total (`agent_runs.cost_usd`,
    say) has nowhere else to read it from once this is raised instead of a
    normal `LlmResult`.
    """

    def __init__(self, message: str, usage: Usage) -> None:
        super().__init__(message)
        self.usage = usage


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
system. Prompt version {PROMPT_VERSION}.

Recommend BUY, SELL or HOLD for the single ticker given, with a confidence \
between 0 and 1 and, for BUY or SELL, a suggested size in USD.

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


NARRATIVE_SYSTEM = f"""You write the daily email for an automated paper-trading \
experiment. Prompt version {PROMPT_VERSION}.

You are given the day's figures as a table. They are already correct and are \
shown to the reader above your text, so do not repeat them and never restate a \
number in a different form — no rounding, no percentages you worked out \
yourself, no totals. If a figure is not in the table, you do not know it.

Write a few short paragraphs covering what the AI decided and why, anything the \
risk engine refused or reduced, and what is worth watching tomorrow. Be plain \
and specific. The reader is the person running the experiment, so no \
salesmanship and no financial advice.

The "Agent run" table says whether the agent ran at all. If it failed, was \
abandoned, or is absent, say so first and plainly: the empty sections below it \
then mean the agent did not run, not that it chose to do nothing. Do not \
reason about why it held positions on a day it never ran."""


REVIEW_SYSTEM = f"""You review one week of an automated paper-trading \
experiment, and propose what to change. Prompt version {PROMPT_VERSION}.

The system you are reviewing has three parts: an LLM that recommends \
BUY/SELL/HOLD per ticker from news, a deterministic risk engine that decides \
what is actually permitted, and a paper broker that executes what survives. \
The experiment's question is whether this beats a passive index or a savings \
account over months.

You are given the week's figures as tables, including the risk engine's limits \
and a count of which constraint bound or refused each decision. They are \
faithfully read from the database and are shown to the reader above your text, \
so do not repeat them and never restate a number in a different form. If a \
figure is not in the tables, you do not know it.

Five things to understand about your role:

1. **One week is a very short sample.** Say so where it matters. Distinguish \
between what the figures show about the *machinery* — a constraint that \
refuses most decisions, a ticker that never produces relevant news, a job that \
failed — which one week evidences well, and what they show about the \
*strategy*, which one week barely evidences at all.
2. **The risk engine's refusals are the most informative column you have.** A \
constraint that binds constantly is either doing its job or is mis-sized, and \
which of those it is depends on what it refused. Take a view.
3. **Your proposals are advisory.** A person reads them and edits the code; \
nothing here is applied automatically. So propose changes to configuration, \
the watchlist, the prompts or the schedule — concretely, naming values — and \
do not propose a specific trade. Individual positions are the daily job's \
business, not yours.
4. **Proposing nothing is a real answer.** A week that supports no change \
should produce an empty list, not five weak suggestions.
5. **The Data integrity table is about the record, not the experiment.** A \
failed check there means a figure above may not describe what actually \
happened — a missing valuation leaves a gap the chart draws straight across, \
an unpriced holding drops out of the total entirely, and either can move the \
reported value with no trade behind it. Where a check failed, lead with it, \
say which figures it undermines, and do not explain a move that a broken \
record may have invented. Do not soften it and do not restate the table; it \
is already shown. Where every check passed, say nothing about it at all."""


def _schema_instructions(model: type[BaseModel]) -> str:
    """The instruction appended to a system prompt, standing in for `messages.parse()`'s
    schema guarantee.

    The literal word "json" has to appear somewhere in the messages sent, or
    DeepSeek's API rejects a `response_format: {"type": "json_object"}`
    request outright — this satisfies that requirement as a side effect of
    stating the actual schema.
    """
    schema = model.model_json_schema()
    return (
        "\n\nRespond with a single JSON object and nothing else — no prose, no "
        "markdown code fence — matching exactly this JSON schema:\n"
        f"{json.dumps(schema)}"
    )


class DeepseekLlm:
    """Implements the `Llm` protocol against the DeepSeek API."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        filter_model: str | None = None,
        analysis_model: str | None = None,
        analysis_effort: str | None = None,
        api_key_secret: str = SHARED_DEEPSEEK_KEY,
    ) -> None:
        cfg = settings()
        # Which Key Vault secret holds the key: each pot's agent passes its own,
        # so DeepSeek's usage page attributes that pot's spend to its key.
        self.api_key_secret = api_key_secret
        self.filter_model = filter_model or cfg.filter_model
        self.analysis_model = analysis_model or cfg.analysis_model
        self.analysis_effort = analysis_effort or cfg.analysis_effort
        # Injectable so tests can supply a MockTransport-backed httpx.Client,
        # the same pattern fetch.py's own callers already use — None means the
        # real network, resolved lazily so importing this module needs
        # neither an API key nor a connection.
        self._client = client
        # max_tokens is generous for the analysis stage because a truncated
        # response is a wasted call, and small for the filter because its
        # output is four short fields.
        self._filter_max_tokens = 1024
        self._analysis_max_tokens = 8192

    def filter_news(
        self, ticker: str, headline: str, summary: str | None
    ) -> LlmResult[NewsRelevance]:
        content = f"Ticker: {ticker}\nHeadline: {headline}"
        if summary:
            content += f"\nSummary: {summary}"
        return self._structured(
            model=self.filter_model,
            system=FILTER_SYSTEM,
            user=content,
            schema_model=NewsRelevance,
            max_tokens=self._filter_max_tokens,
            reasoning=False,
        )

    def analyse(self, prompt: str) -> LlmResult[Recommendation]:
        return self._structured(
            model=self.analysis_model,
            system=ANALYSIS_SYSTEM,
            user=prompt,
            schema_model=Recommendation,
            max_tokens=self._analysis_max_tokens,
            reasoning=True,
        )

    def narrate(self, prompt: str) -> LlmResult[DailyNarrative]:
        return self._structured(
            model=self.analysis_model,
            system=NARRATIVE_SYSTEM,
            user=prompt,
            schema_model=DailyNarrative,
            max_tokens=self._analysis_max_tokens,
            reasoning=True,
        )

    def review(self, prompt: str) -> LlmResult[WeeklyReview]:
        """The weekly review. Same model and shape as `narrate`, different system prompt.

        On the analysis model rather than the filter model: this is the one
        call a week where the reasoning is the product, and it runs 52 times a
        year against the filter stage's tens of thousands.
        """
        return self._structured(
            model=self.analysis_model,
            system=REVIEW_SYSTEM,
            user=prompt,
            schema_model=WeeklyReview,
            max_tokens=self._analysis_max_tokens,
            reasoning=True,
        )

    def _structured[T: BaseModel](
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema_model: type[T],
        max_tokens: int,
        reasoning: bool,
    ) -> LlmResult[T]:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system + _schema_instructions(schema_model)},
            {"role": "user", "content": user},
        ]

        usage = Usage(0, 0)
        last_error: Exception | None = None
        for attempt in range(1, MAX_STRUCTURED_ATTEMPTS + 1):
            body: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            }
            # Reasoning is a request-level dial, not a per-model fact: the
            # filter stage turns it off outright rather than asking for a
            # lower effort, matching the old cascade's cheap/fast stage.
            if reasoning:
                body["reasoning_effort"] = self.analysis_effort
            else:
                body["thinking"] = {"type": "disabled"}

            response = post_json_retrying(
                f"{API_BASE_URL}/chat/completions",
                body=body,
                headers=self._headers(),
                client=self._client,
                timeout=REASONING_TIMEOUT_SECONDS if reasoning else TIMEOUT_SECONDS,
            )
            content = response["choices"][0]["message"]["content"]
            reported = response["usage"]
            # prompt_tokens is the *total*, and prompt_cache_hit_tokens a
            # subset of it — not additional to it, unlike Anthropic's shape.
            usage += Usage(
                input_tokens=reported["prompt_tokens"],
                output_tokens=reported["completion_tokens"],
                cache_read_tokens=reported.get("prompt_cache_hit_tokens", 0) or 0,
            )

            try:
                parsed = schema_model.model_validate_json(content)
            except (ValidationError, ValueError) as exc:
                last_error = exc
                if attempt == MAX_STRUCTURED_ATTEMPTS:
                    break
                messages = [
                    *messages,
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": (
                            f"That response did not parse as valid JSON matching the "
                            f"schema: {exc}. Respond again with ONLY the corrected JSON "
                            "object, matching the schema exactly."
                        ),
                    },
                ]
                continue

            return LlmResult(value=parsed, model=model, usage=usage)

        raise StructuredOutputError(
            f"{model} did not return JSON matching {schema_model.__name__} after "
            f"{MAX_STRUCTURED_ATTEMPTS} attempt(s): {last_error}",
            usage,
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {secret(self.api_key_secret)}"}


def balance_usd(client: httpx.Client | None = None) -> Decimal:
    """The account's current prepaid balance, in USD, read live from DeepSeek.

    Unlike Anthropic, which publishes no balance endpoint at all, DeepSeek
    reports this directly — `GET /user/balance` — which is what replaces the
    old hand-typed `ANTHROPIC-CREDIT-USD` secret with a figure checked
    against the account itself rather than a guess nothing verified.

    Raises if the account has no USD-denominated entry: every stored figure
    and every price in this project is USD, and silently converting a
    CNY balance would need a live exchange rate this function has no
    business fetching.
    """
    response = get_json(
        f"{API_BASE_URL}/user/balance",
        headers={"Authorization": f"Bearer {secret(SHARED_DEEPSEEK_KEY)}"},
        client=client,
    )
    for info in response.get("balance_infos", []):
        if info.get("currency") == "USD":
            return Decimal(info["total_balance"])
    raise FetchError("DeepSeek account has no USD-denominated balance to report")
