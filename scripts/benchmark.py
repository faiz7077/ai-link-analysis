"""Measure latency, token usage, cost and output quality across Gemini models.

Usage (needs GEMINI_API_KEY in .env):
    python -m scripts.benchmark
    python -m scripts.benchmark --models gemini-3.8-flash gemini-3.5-flash-lite --urls stripe.com zoho.com/crm

Writes docs/benchmark_results.md and docs/benchmark_results.json.
Quality checks are automatic heuristics (schema-valid, counts, platform coverage,
title/meta length, grounded services). They are not a human accuracy rating.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from pathlib import Path

from app.config import get_settings
from app.services.llm_service import GeminiLLMService, LLMError
from app.services.website_extractor import WebsiteExtractor

DEFAULT_MODELS = ["gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
DEFAULT_URLS = ["https://stripe.com", "https://www.zoho.com/crm/", "https://www.freshworks.com", "https://www.shopify.com", "https://www.razorpay.com"]
OUT = Path(__file__).resolve().parent.parent / "docs"


def quality(a) -> dict:
    s = a.seo_improvements
    platforms = {q.primary_platform for q in a.target_search_queries}
    checks = {
        "5_queries": len(a.target_search_queries) == 5,
        "all_5_platforms": len(platforms) == 5,
        "3plus_competitors": len([c for c in a.competitor_analysis.competitors
                                  if c.name.lower() != a.brand.name.lower()]) >= 3,
        "5_services": len(a.top_services) == 5,
        "title_50_60": 45 <= len(s.recommended_title) <= 65,
        "meta_140_160": 130 <= len(s.recommended_meta_description) <= 170,
        "no_volume_claims": not any(w in json.dumps(a.model_dump()).lower()
                                    for w in ("searches per month", "monthly searches", "search volume:")),
    }
    grounded = sum(1 for x in a.top_services if x.evidence == "from_website")
    return {"checks": checks, "score": f"{sum(checks.values())}/{len(checks)}", "grounded_services": grounded}


async def main(models: list[str], urls: list[str]) -> None:
    settings = get_settings()
    extractor = WebsiteExtractor(settings)
    sites = {}
    for u in urls:
        try:
            site = await extractor.extract(u)
            sites[u] = (site.to_prompt_text(settings.max_content_chars), site.brand_hint)
            print(f"extracted {u}: {len(sites[u][0])} chars")
        except Exception as exc:  # noqa: BLE001
            print(f"skip {u}: {exc}")

    rows = []
    for model in models:
        svc = GeminiLLMService(settings.model_copy(update={"gemini_model": model, "gemini_fallback_model": ""}))
        for u, (text, hint) in sites.items():
            try:
                r = await svc.analyze(text, hint)
                q = quality(r.analysis)
                rows.append({"model": model, "url": u, "ok": True, "latency_ms": r.latency_ms,
                             "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                             "cost_usd": r.estimated_cost_usd, **q})
                sample = OUT / "samples" / f"{model}__{u.split('//')[-1].strip('/').replace('/', '_')}.json"
                sample.write_text(r.analysis.model_dump_json(indent=2))
                print(f"{model:24} {u:32} {r.latency_ms:>6} ms  in={r.input_tokens} out={r.output_tokens} "
                      f"${r.estimated_cost_usd}  quality={q['score']}")
            except LLMError as exc:
                rows.append({"model": model, "url": u, "ok": False, "error": f"{exc.code}: {exc}"})
                print(f"{model:24} {u:32} FAILED {exc.code}")
            await asyncio.sleep(4)  # stay inside free-tier RPM limits

    OUT.mkdir(exist_ok=True)
    (OUT / "benchmark_results.json").write_text(json.dumps(rows, indent=2))

    lines = ["| Model | Runs OK | Median latency | Avg input tok | Avg output tok | Avg cost / analysis | Quality checks passed |",
             "|---|---|---|---|---|---|---|"]
    for model in models:
        ok = [r for r in rows if r["model"] == model and r["ok"]]
        total = len([r for r in rows if r["model"] == model])
        if not ok:
            lines.append(f"| `{model}` | 0/{total} | – | – | – | – | – |")
            continue
        passed = sum(sum(r["checks"].values()) for r in ok)
        possible = sum(len(r["checks"]) for r in ok)
        lines.append(
            f"| `{model}` | {len(ok)}/{total} | {statistics.median(r['latency_ms'] for r in ok) / 1000:.1f} s | "
            f"{statistics.mean(r['input_tokens'] for r in ok):,.0f} | {statistics.mean(r['output_tokens'] for r in ok):,.0f} | "
            f"${statistics.mean(r['cost_usd'] for r in ok):.4f} | {passed}/{possible} |"
        )
    (OUT / "benchmark_results.md").write_text("\n".join(lines) + "\n")
    print("\n" + "\n".join(lines))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    p.add_argument("--urls", nargs="+", default=DEFAULT_URLS)
    args = p.parse_args()
    asyncio.run(main(args.models, args.urls))
