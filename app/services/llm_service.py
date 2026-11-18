"""Gemini integration with structured (schema-enforced) JSON output.

* One call produces all five sections (cheaper and faster than five calls,
  and the sections stay consistent with each other).
* `response_schema=LLMAnalysis` makes Gemini emit JSON matching our Pydantic
  model; we validate it again on our side.
* Transient errors (429 / 5xx / timeouts / invalid JSON) are retried with
  exponential backoff, then we fall back to a cheaper, faster model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.schemas import LLMAnalysis

log = logging.getLogger(__name__)

# USD per 1M tokens (paid tier, standard). Source: https://ai.google.dev/gemini-api/docs/pricing
# (checked 18 Nov 2026). Output price includes thinking tokens.
GEMINI_PRICING: dict[str, tuple[float, float]] = {
    "gemini-3.8-flash": (0.75, 3.75),  # intro price until 31 Dec 2026, then 1.50 / 7.50
    "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-4-hydrogen": (1.50, 7.50),
}

SYSTEM_PROMPT = """You are a senior digital-marketing strategist, SEO specialist and market analyst.
You receive text extracted from a company's website and return a structured business analysis.

Rules:
- Ground every statement in the provided website content. Do not produce generic filler that
  would fit any company. Quote real product/service names from the content.
- Services: list the 5 most important services/products. Set evidence="from_website" only if the
  service is explicitly present in the content; otherwise evidence="inferred".
- Competitors: name at least 3 real, well-known competitors in the same category and market.
  They come from your general knowledge, so always set source="ai_suggested". Never list the
  analyzed brand itself. If you are unsure a company exists, leave it out.
- Comparison table: 5-7 rows comparing the brand with its single closest competitor.
- Search queries: exactly 5 queries real customers would type. Cover Google, Facebook, Instagram,
  LinkedIn and X: each must be the primary_platform of exactly one query. Never include search
  volumes, rankings or any numbers you cannot verify.
- SEO: recommended_title 50-60 characters; recommended_meta_description 140-160 characters.
  Copy current_title / current_meta_description exactly from the input (empty string if absent).
  technical_observations must describe issues actually visible in the input.
- Target audience: geographic_focus must come from the content (currency, addresses, language,
  country pages); otherwise "Not specified". Give 5 recommended_marketing_segments.
- Write in English, be specific and concise. Output JSON only."""


class LLMError(Exception):
    """The LLM call failed after retries and fallback."""

    def __init__(self, message: str, code: str = "llm_error"):
        super().__init__(message)
        self.code = code


@dataclass
class LLMResult:
    analysis: LLMAnalysis
    model: str
    used_fallback: bool
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int

    @property
    def estimated_cost_usd(self) -> float | None:
        price = GEMINI_PRICING.get(self.model)
        if not price or self.input_tokens is None or self.output_tokens is None:
            return None
        return round((self.input_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000, 6)


def build_user_prompt(website_text: str, brand_hint: str) -> str:
    return (
        f"Analyze this website. Likely brand name: {brand_hint or 'unknown'}.\n\n"
        f"<website_content>\n{website_text}\n</website_content>"
    )


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, genai_errors.ServerError):
        return True
    if isinstance(exc, genai_errors.ClientError):
        return getattr(exc, "code", None) in (408, 429)
    return isinstance(exc, (asyncio.TimeoutError, json.JSONDecodeError, ValidationError))


class GeminiLLMService:
    provider = "Google Gemini"

    def __init__(self, settings: Settings | None = None, client: genai.Client | None = None):
        self.settings = settings or get_settings()
        if client is None and not self.settings.gemini_api_key:
            raise LLMError("GEMINI_API_KEY is not configured on the server.", code="llm_not_configured")
        self.client = client or genai.Client(api_key=self.settings.gemini_api_key)

    def _config(self) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=LLMAnalysis,
            temperature=self.settings.llm_temperature,
            thinking_config=types.ThinkingConfig(thinking_level=self.settings.gemini_thinking_level)
            if self.settings.gemini_thinking_level
            else None,
        )

    async def _call(self, model: str, prompt: str) -> LLMResult:
        start = time.perf_counter()
        resp = await asyncio.wait_for(
            self.client.aio.models.generate_content(model=model, contents=prompt, config=self._config()),
            timeout=self.settings.llm_timeout_seconds,
        )
        text = resp.text or ""
        analysis = LLMAnalysis.model_validate(json.loads(text))
        usage = resp.usage_metadata
        out_tokens = None
        if usage:
            out_tokens = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
        return LLMResult(
            analysis=analysis,
            model=model,
            used_fallback=False,
            input_tokens=usage.prompt_token_count if usage else None,
            output_tokens=out_tokens,
            latency_ms=int((time.perf_counter() - start) * 1000),
        )

    async def analyze(self, website_text: str, brand_hint: str = "") -> LLMResult:
        prompt = build_user_prompt(website_text, brand_hint)
        models = [self.settings.gemini_model]
        if self.settings.gemini_fallback_model and self.settings.gemini_fallback_model not in models:
            models.append(self.settings.gemini_fallback_model)

        last_exc: Exception | None = None
        for idx, model in enumerate(models):
            for attempt in range(self.settings.llm_max_retries + 1):
                try:
                    result = await self._call(model, prompt)
                    result.used_fallback = idx > 0
                    return result
                except Exception as exc:  # noqa: BLE001 - classified below
                    last_exc = exc
                    log.warning("LLM call failed (model=%s attempt=%d): %r", model, attempt + 1, exc)
                    if isinstance(exc, genai_errors.ClientError) and exc.code in (401, 403):
                        raise LLMError("The LLM API key is invalid or lacks permission.", "llm_auth_error") from exc
                    if isinstance(exc, genai_errors.ClientError) and exc.code == 404:
                        break  # model not available for this key -> try fallback
                    if not _retryable(exc):
                        break
                    if attempt < self.settings.llm_max_retries:
                        await asyncio.sleep(min(2**attempt, 8))

        if isinstance(last_exc, genai_errors.ClientError) and last_exc.code == 429:
            raise LLMError("LLM rate limit / quota exceeded. Please try again later.", "llm_rate_limited") from last_exc
        if isinstance(last_exc, asyncio.TimeoutError):
            raise LLMError("The LLM took too long to respond.", "llm_timeout") from last_exc
        if isinstance(last_exc, (json.JSONDecodeError, ValidationError)):
            raise LLMError("The LLM returned an invalid structured response.", "llm_invalid_output") from last_exc
        raise LLMError(f"LLM request failed: {last_exc.__class__.__name__ if last_exc else 'unknown'}.") from last_exc
