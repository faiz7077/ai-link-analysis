"""Request / response models.

The `LLM*` models double as the JSON schema we hand to Gemini
(`response_schema`), so the model is forced to return exactly this shape and
Pydantic validates it on the way back.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Platform = Literal["Google", "Facebook", "Instagram", "LinkedIn", "X"]
Evidence = Literal["from_website", "inferred"]


# ---------- Request ----------

class LinkAnalysisRequest(BaseModel):
    url: str = Field(..., examples=["https://stripe.com"], description="Public website URL to analyze.")

    @field_validator("url")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("url must not be empty")
        return v.strip()


# ---------- LLM output (also used as Gemini response_schema) ----------

class Brand(BaseModel):
    name: str = Field(description="Brand / company name as shown on the website.")
    category: str = Field(description="Main business category, e.g. 'Online payments infrastructure'.")
    summary: str = Field(description="2-3 sentence business overview grounded in the website content.")


class Competitor(BaseModel):
    name: str
    website: str = Field(description="Competitor homepage domain if known, else empty string.")
    main_offering: str
    target_market: str
    strengths: list[str]
    weaknesses_or_gaps: list[str]
    source: Literal["ai_suggested"] = Field(
        description="Always 'ai_suggested': competitors come from model knowledge, not live research."
    )


class ComparisonRow(BaseModel):
    aspect: str = Field(description="e.g. Main Service, Target Market, Key Strength, Pricing, Opportunity.")
    your_brand: str
    competitor: str
    competitor_name: str


class CompetitorAnalysis(BaseModel):
    brand_name: str
    brand_overview: str
    business_category: str
    main_products_and_services: list[str]
    competitors: list[Competitor] = Field(description="At least 3 relevant competitors.")
    comparison_table: list[ComparisonRow]
    your_competitive_advantages: list[str]
    market_positioning_opportunities: list[str]


class SearchQuery(BaseModel):
    query: str
    platforms: list[Platform] = Field(description="Platforms where this query is a good fit.")
    primary_platform: Platform
    intent: str = Field(description="e.g. Service Search, Business Discovery, Inspiration, B2B Search, Comparison.")
    business_relevance: str
    target_audience: str


class SEOImprovements(BaseModel):
    primary_keyword: str
    secondary_keywords: list[str]
    long_tail_keywords: list[str]
    business_keywords: list[str]
    current_title: str = Field(description="The page's current <title>, copied from the input.")
    recommended_title: str = Field(description="50-60 characters, starts with or contains the primary keyword.")
    title_rationale: str
    current_meta_description: str = Field(description="The page's current meta description, or empty.")
    recommended_meta_description: str = Field(description="140-160 characters, human friendly, includes keywords.")
    supporting_content: list[SupportingContent]
    technical_observations: list[str] = Field(
        description="Concrete on-page issues seen in the input (missing meta description, multiple/missing H1, thin content...)."
    )


class SupportingContent(BaseModel):
    type: Literal[
        "Service landing page", "Industry page", "FAQ", "Blog topic",
        "Location page", "Business article", "Comparison page", "Case study",
    ]
    title: str
    target_keyword: str
    reason: str


class Service(BaseModel):
    service_name: str
    description: str
    target_industry: str
    target_customer: str
    main_benefit: str
    evidence: Evidence = Field(
        description="'from_website' if the service is explicitly mentioned in the content, otherwise 'inferred'."
    )


class AudienceSegment(BaseModel):
    segment: str
    business_type: str
    requirement: str
    recommended_channels: list[Platform]


class TargetAudience(BaseModel):
    primary_audience: list[str]
    secondary_audience: list[str]
    industries: list[str]
    business_types: list[str]
    customer_profiles: list[str]
    geographic_focus: str = Field(description="Location focus if stated or strongly implied, else 'Not specified'.")
    customer_needs_and_challenges: list[str]
    customer_interests: list[str]
    recommended_marketing_segments: list[AudienceSegment]


class LLMAnalysis(BaseModel):
    brand: Brand
    competitor_analysis: CompetitorAnalysis
    target_search_queries: list[SearchQuery] = Field(description="Exactly 5 queries.")
    seo_improvements: SEOImprovements
    top_services: list[Service] = Field(description="Exactly 5 services, most important first.")
    target_audience: TargetAudience


SEOImprovements.model_rebuild()


# ---------- API response ----------

class AnalysisMeta(BaseModel):
    llm_provider: str
    llm_model: str
    used_fallback_model: bool
    pages_analyzed: list[str]
    content_chars_sent: int
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost_usd: float | None
    extraction_ms: int
    llm_ms: int
    total_ms: int
    cached: bool


class AnalysisSections(BaseModel):
    competitor_analysis: CompetitorAnalysis
    target_search_queries: list[SearchQuery]
    seo_improvements: SEOImprovements
    top_services: list[Service]
    target_audience: TargetAudience


class LinkAnalysisResponse(BaseModel):
    success: Literal[True] = True
    website: str
    final_url: str
    brand: Brand
    analysis: AnalysisSections
    disclaimers: list[str]
    meta: AnalysisMeta


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    success: Literal[False] = False
    error: ErrorDetail
