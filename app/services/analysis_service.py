"""Orchestrates: validate -> extract -> LLM -> post-process -> cache."""

from __future__ import annotations

import time
from collections import OrderedDict
from urllib.parse import urlsplit

from app.config import Settings, get_settings
from app.schemas import AnalysisMeta, AnalysisSections, LinkAnalysisResponse
from app.services.competitor_service import clean_competitors
from app.services.llm_service import GeminiLLMService
from app.services.website_extractor import WebsiteExtractor
from app.utils.validators import validate_public_url

BASE_DISCLAIMERS = [
    "Competitors are AI-suggested from model knowledge (source='ai_suggested'), not live market research; verify before use.",
    "No search volumes or rankings are included: they require real data sources (e.g. Google Keyword Planner, platform APIs).",
    "Services marked evidence='inferred' were not explicitly found on the website.",
]


class TTLCache:
    """Tiny in-memory LRU + TTL cache, avoids paying twice for the same URL."""

    def __init__(self, ttl: int, max_items: int):
        self.ttl, self.max_items = ttl, max_items
        self._data: OrderedDict[str, tuple[float, LinkAnalysisResponse]] = OrderedDict()

    def get(self, key: str) -> LinkAnalysisResponse | None:
        item = self._data.get(key)
        if not item:
            return None
        ts, value = item
        if time.time() - ts > self.ttl:
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: LinkAnalysisResponse) -> None:
        self._data[key] = (time.time(), value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_items:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()


class AnalysisService:
    def __init__(
        self,
        settings: Settings | None = None,
        extractor: WebsiteExtractor | None = None,
        llm: GeminiLLMService | None = None,
    ):
        self.settings = settings or get_settings()
        self.extractor = extractor or WebsiteExtractor(self.settings)
        self._llm = llm
        self.cache = TTLCache(self.settings.cache_ttl_seconds, self.settings.cache_max_items)

    @property
    def llm(self) -> GeminiLLMService:
        if self._llm is None:  # created lazily so the app boots without a key
            self._llm = GeminiLLMService(self.settings)
        return self._llm

    async def analyze(self, raw_url: str) -> LinkAnalysisResponse:
        t0 = time.perf_counter()
        url = await validate_public_url(raw_url)

        if cached := self.cache.get(url):
            hit = cached.model_copy(deep=True)
            hit.meta.cached = True
            hit.meta.total_ms = int((time.perf_counter() - t0) * 1000)
            return hit

        website = await self.extractor.extract(url)
        t1 = time.perf_counter()
        content = website.to_prompt_text(self.settings.max_content_chars)

        result = await self.llm.analyze(content, website.brand_hint)
        a = result.analysis

        disclaimers = list(BASE_DISCLAIMERS)
        disclaimers += clean_competitors(
            a.competitor_analysis, a.brand.name, urlsplit(website.final_url).hostname or ""
        )
        if len(a.target_search_queries) != 5:
            disclaimers.append(f"Model returned {len(a.target_search_queries)} search queries instead of 5.")
        if len(a.top_services) < 5:
            disclaimers.append(f"Only {len(a.top_services)} distinct services could be identified.")
        a.target_search_queries = a.target_search_queries[:5]
        a.top_services = a.top_services[:5]
        if result.used_fallback:
            disclaimers.append(f"Primary model unavailable; served by fallback model {result.model}.")

        response = LinkAnalysisResponse(
            website=url,
            final_url=website.final_url,
            brand=a.brand,
            analysis=AnalysisSections(
                competitor_analysis=a.competitor_analysis,
                target_search_queries=a.target_search_queries,
                seo_improvements=a.seo_improvements,
                top_services=a.top_services,
                target_audience=a.target_audience,
            ),
            disclaimers=disclaimers,
            meta=AnalysisMeta(
                llm_provider=self.llm.provider,
                llm_model=result.model,
                used_fallback_model=result.used_fallback,
                pages_analyzed=[website.final_url] + [p.url for p in website.subpages],
                content_chars_sent=len(content),
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                estimated_cost_usd=result.estimated_cost_usd,
                extraction_ms=int((t1 - t0) * 1000),
                llm_ms=result.latency_ms,
                total_ms=int((time.perf_counter() - t0) * 1000),
                cached=False,
            ),
        )
        self.cache.set(url, response)
        return response
