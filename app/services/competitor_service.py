"""Post-processing for competitor data returned by the LLM.

LLMs occasionally list the analyzed brand as its own competitor, repeat a
company twice, or return URLs in inconsistent shapes. This module cleans that
up deterministically so the API output is trustworthy.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from app.schemas import CompetitorAnalysis


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _norm_domain(site: str) -> str:
    site = (site or "").strip()
    if not site:
        return ""
    host = urlsplit(site if "://" in site else f"https://{site}").hostname or ""
    return host.removeprefix("www.")


def clean_competitors(analysis: CompetitorAnalysis, brand_name: str, brand_domain: str) -> list[str]:
    """Mutates `analysis` in place. Returns human-readable warnings."""
    warnings: list[str] = []
    brand_key = _norm_name(brand_name)
    brand_domain = _norm_domain(brand_domain)

    seen: set[str] = set()
    cleaned = []
    for comp in analysis.competitors:
        comp.website = _norm_domain(comp.website)
        key = _norm_name(comp.name)
        if not key or key in seen:
            continue
        if key == brand_key or (brand_domain and comp.website == brand_domain):
            continue
        seen.add(key)
        comp.source = "ai_suggested"
        cleaned.append(comp)
    analysis.competitors = cleaned

    if len(cleaned) < 3:
        warnings.append(
            f"Only {len(cleaned)} reliable competitor(s) could be identified for this website."
        )
    return warnings
