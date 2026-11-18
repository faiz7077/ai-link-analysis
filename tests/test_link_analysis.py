"""Tests run fully offline: DNS, HTTP and Gemini are all mocked."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import main
from app.config import Settings
from app.schemas import LLMAnalysis
from app.services import llm_service as llm_mod
from app.services.analysis_service import AnalysisService
from app.services.llm_service import GeminiLLMService, LLMError
from app.services.website_extractor import WebsiteExtractor, parse_html, pick_subpages
from app.utils import validators
from app.utils.validators import BlockedURLError, InvalidURLError, is_public_ip, normalize_url, validate_public_url

HOME_HTML = """<html lang="en"><head>
<title>Acme Web Studio | Web Development for Startups</title>
<meta name="description" content="Acme builds fast websites and e-commerce stores for startups in India.">
<meta property="og:site_name" content="Acme Web Studio">
</head><body>
<nav><a href="/about">About us</a><a href="/services">Our services</a><a href="/blog/post-1">Blog</a>
<a href="https://other.com/x">External</a></nav>
<main><h1>We build websites that convert</h1>
<h2>Web Development</h2><p>Custom websites built with React and Next.js for startups.</p>
<h2>E-commerce</h2><p>Shopify and WooCommerce stores for D2C brands.</p>
<h2>SEO</h2><p>Technical SEO audits and content strategy.</p>
<script>var tracking = 1;</script></main>
<footer>Bengaluru, India</footer></body></html>"""

SERVICES_HTML = "<html><head><title>Services</title></head><body><h1>Services</h1><p>UI/UX design, mobile apps, maintenance.</p></body></html>"


def sample_analysis(**overrides) -> dict:
    data = {
        "brand": {"name": "Acme Web Studio", "category": "Web development agency", "summary": "Builds websites for startups."},
        "competitor_analysis": {
            "brand_name": "Acme Web Studio",
            "brand_overview": "Startup-focused web agency.",
            "business_category": "Web development",
            "main_products_and_services": ["Web development", "E-commerce", "SEO"],
            "competitors": [
                {"name": c, "website": f"https://www.{c.lower()}.com/", "main_offering": "Web dev", "target_market": "SMB",
                 "strengths": ["Brand"], "weaknesses_or_gaps": ["Price"], "source": "ai_suggested"}
                for c in ["Toptal", "Acme Web Studio", "Webflow", "Toptal", "Wix"]
            ],
            "comparison_table": [{"aspect": "Main Service", "your_brand": "Web dev", "competitor": "Freelancers", "competitor_name": "Toptal"}],
            "your_competitive_advantages": ["Startup focus"],
            "market_positioning_opportunities": ["Case studies"],
        },
        "target_search_queries": [
            {"query": f"q{i}", "platforms": [p], "primary_platform": p, "intent": "Service Search",
             "business_relevance": "high", "target_audience": "Founders"}
            for i, p in enumerate(["Google", "Facebook", "Instagram", "LinkedIn", "X"])
        ],
        "seo_improvements": {
            "primary_keyword": "web development for startups",
            "secondary_keywords": ["ecommerce development"],
            "long_tail_keywords": ["affordable web development for startups in india"],
            "business_keywords": ["next.js agency"],
            "current_title": "Acme Web Studio | Web Development for Startups",
            "recommended_title": "Web Development for Startups in India | Acme Web Studio",
            "title_rationale": "Primary keyword first.",
            "current_meta_description": "Acme builds fast websites.",
            "recommended_meta_description": "Acme Web Studio builds fast websites.",
            "supporting_content": [{"type": "FAQ", "title": "Web dev FAQ", "target_keyword": "web dev cost", "reason": "Captures questions"}],
            "technical_observations": ["Only one H1"],
        },
        "top_services": [
            {"service_name": s, "description": "d", "target_industry": "Tech", "target_customer": "Startups",
             "main_benefit": "b", "evidence": "from_website"}
            for s in ["Web Development", "E-commerce", "SEO", "UI/UX", "Mobile apps"]
        ],
        "target_audience": {
            "primary_audience": ["Startup founders"], "secondary_audience": ["SMBs"], "industries": ["D2C"],
            "business_types": ["Startups"], "customer_profiles": ["Seed-stage founder"], "geographic_focus": "India",
            "customer_needs_and_challenges": ["Fast launch"], "customer_interests": ["Growth"],
            "recommended_marketing_segments": [
                {"segment": "Founders", "business_type": "Startups", "requirement": "MVP site", "recommended_channels": ["LinkedIn"]}
            ],
        },
    }
    data.update(overrides)
    return data


class FakeModels:
    def __init__(self, behaviours):
        self.behaviours = list(behaviours)
        self.calls: list[str] = []

    async def generate_content(self, model, contents, config):
        self.calls.append(model)
        b = self.behaviours.pop(0)
        if isinstance(b, Exception):
            raise b
        usage = SimpleNamespace(prompt_token_count=4000, candidates_token_count=1500, thoughts_token_count=500)
        return SimpleNamespace(text=b, usage_metadata=usage)


def fake_client(*behaviours):
    models = FakeModels(behaviours)
    return SimpleNamespace(aio=SimpleNamespace(models=models)), models


@pytest.fixture
def settings():
    return Settings(gemini_api_key="test", llm_max_retries=1, gemini_model="gemini-3.8-flash",
                    gemini_fallback_model="gemini-3.5-flash-lite", _env_file=None)


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """Resolve every test hostname to a public IP unless the name says otherwise."""

    async def resolve(host: str):
        if host.startswith("private."):
            return ["10.0.0.5"]
        if host.startswith("rebind."):
            return ["93.184.216.34", "127.0.0.1"]
        return ["93.184.216.34"]

    monkeypatch.setattr(validators, "resolve_host", resolve)
    monkeypatch.setattr(llm_mod.asyncio, "sleep", _no_sleep)


async def _no_sleep(*_):
    return None


# ---------------- URL validation / SSRF ----------------

@pytest.mark.parametrize("raw,expected", [
    ("example.com", "https://example.com/"),
    ("  HTTPS://Example.COM/path?q=1#frag ", "https://example.com/path?q=1"),
    ("http://example.com:8080", "http://example.com:8080/"),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "ftp://example.com", "javascript:alert(1)", "http://user:pw@example.com",
                                 "http://example.com:22", "http://intranet", "https://"])
def test_invalid_urls(raw):
    with pytest.raises(InvalidURLError):
        normalize_url(raw)


@pytest.mark.parametrize("ip,public", [
    ("8.8.8.8", True), ("127.0.0.1", False), ("10.1.2.3", False), ("192.168.1.1", False),
    ("172.16.0.1", False), ("169.254.169.254", False), ("0.0.0.0", False), ("100.64.0.1", False),
    ("::1", False), ("fd00::1", False), ("fe80::1", False), ("::ffff:127.0.0.1", False), ("2606:4700::1111", True),
])
def test_is_public_ip(ip, public):
    assert is_public_ip(ip) is public


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://169.254.169.254/latest/meta-data", "http://[::1]/",
                                 "http://localhost/", "http://api.internal/", "https://private.example.com",
                                 "https://rebind.example.com"])
async def test_ssrf_blocked(url):
    with pytest.raises((BlockedURLError, InvalidURLError)):
        await validate_public_url(url)


async def test_public_url_allowed():
    assert await validate_public_url("acme.com") == "https://acme.com/"


# ---------------- Extraction ----------------

def test_parse_html_extracts_signals():
    page = parse_html(HOME_HTML, "https://acme.com/")
    assert page.title.startswith("Acme Web Studio")
    assert "startups in India" in page.meta_description
    assert page.og["site_name"] == "Acme Web Studio"
    assert "We build websites that convert" in page.headings
    assert "tracking" not in page.text and "Shopify" in page.text
    assert all("other.com" not in u for u, _ in page.internal_links)


def test_pick_subpages_prefers_about_and_services():
    page = parse_html(HOME_HTML, "https://acme.com/")
    picked = pick_subpages(page.internal_links, "https://acme.com/", 3)
    assert picked == ["https://acme.com/about", "https://acme.com/services"]


@respx.mock
async def test_extractor_follows_safe_redirect_and_subpages(settings):
    respx.get("https://acme.com/").mock(return_value=httpx.Response(301, headers={"location": "https://www.acme.com/"}))
    respx.get("https://www.acme.com/").mock(return_value=httpx.Response(200, html=HOME_HTML))
    respx.get("https://www.acme.com/services").mock(return_value=httpx.Response(200, html=SERVICES_HTML))
    respx.get("https://www.acme.com/about").mock(return_value=httpx.Response(404))
    site = await WebsiteExtractor(settings).extract("https://acme.com/")
    assert site.final_url == "https://www.acme.com/"
    assert [p.url for p in site.subpages] == ["https://www.acme.com/services"]
    assert site.brand_hint == "Acme Web Studio"
    assert "UI/UX design" in site.to_prompt_text(10_000)


@respx.mock
async def test_redirect_to_private_ip_is_blocked(settings):
    respx.get("https://acme.com/").mock(return_value=httpx.Response(302, headers={"location": "http://169.254.169.254/"}))
    with pytest.raises(BlockedURLError):
        await WebsiteExtractor(settings).extract("https://acme.com/")


# ---------------- LLM service ----------------

async def test_llm_parses_structured_output_and_cost(settings):
    client, models = fake_client(json.dumps(sample_analysis()))
    res = await GeminiLLMService(settings, client=client).analyze("content", "Acme")
    assert isinstance(res.analysis, LLMAnalysis)
    assert res.input_tokens == 4000 and res.output_tokens == 2000
    assert res.estimated_cost_usd == pytest.approx((4000 * 0.75 + 2000 * 3.75) / 1e6)
    assert models.calls == ["gemini-3.8-flash"]


async def test_llm_retries_invalid_json_then_falls_back(settings):
    client, models = fake_client("not json", "{}", json.dumps(sample_analysis()))
    res = await GeminiLLMService(settings, client=client).analyze("content")
    assert models.calls == ["gemini-3.8-flash", "gemini-3.8-flash", "gemini-3.5-flash-lite"]
    assert res.used_fallback and res.model == "gemini-3.5-flash-lite"


async def test_llm_raises_clean_error_when_everything_fails(settings):
    client, _ = fake_client("bad", "bad", "bad", "bad")
    with pytest.raises(LLMError) as exc:
        await GeminiLLMService(settings, client=client).analyze("content")
    assert exc.value.code == "llm_invalid_output"


def test_llm_requires_api_key():
    with pytest.raises(LLMError) as exc:
        GeminiLLMService(Settings(gemini_api_key="", _env_file=None))
    assert exc.value.code == "llm_not_configured"


# ---------------- API ----------------

@pytest.fixture
def api(settings, monkeypatch):
    client, models = fake_client(*[json.dumps(sample_analysis())] * 3)
    svc = AnalysisService(settings, llm=GeminiLLMService(settings, client=client))
    monkeypatch.setattr(main, "service", svc)
    main.limiter.reset()
    return TestClient(main.app), models


@respx.mock
def test_link_analysis_success(api):
    client, models = api
    respx.get("https://acme.com/").mock(return_value=httpx.Response(200, html=HOME_HTML))
    respx.get(url__regex=r"https://acme.com/.+").mock(return_value=httpx.Response(200, html=SERVICES_HTML))

    r = client.post("/link-analysis", json={"url": "acme.com"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True and body["brand"]["name"] == "Acme Web Studio"
    a = body["analysis"]
    assert set(a) == {"competitor_analysis", "target_search_queries", "seo_improvements", "top_services", "target_audience"}
    # duplicates and the brand itself are removed from competitors; domains normalized
    assert [c["name"] for c in a["competitor_analysis"]["competitors"]] == ["Toptal", "Webflow", "Wix"]
    assert a["competitor_analysis"]["competitors"][0]["website"] == "toptal.com"
    assert len(a["target_search_queries"]) == 5 and len(a["top_services"]) == 5
    assert body["meta"]["cached"] is False and body["meta"]["estimated_cost_usd"] > 0

    # second call is served from cache, no new LLM call
    r2 = client.post("/link-analysis", json={"url": "https://acme.com"})
    assert r2.json()["meta"]["cached"] is True
    assert len(models.calls) == 1


def test_blocked_url_returns_400(api):
    client, _ = api
    r = client.post("/link-analysis", json={"url": "http://127.0.0.1:8080/admin"})
    assert r.status_code == 400
    assert r.json() == {"success": False, "error": {"code": "blocked_url",
                                                     "message": "Access to private or internal network addresses is not allowed."}}


def test_invalid_url_returns_400(api):
    client, _ = api
    r = client.post("/link-analysis", json={"url": "ftp://acme.com"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_url"


def test_missing_body_returns_422(api):
    client, _ = api
    r = client.post("/link-analysis", json={})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"


@respx.mock
def test_unreachable_site_returns_422(api):
    client, _ = api
    respx.get("https://down.com/").mock(side_effect=httpx.ConnectError("boom"))
    r = client.post("/link-analysis", json={"url": "down.com"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "website_unreachable"


@respx.mock
def test_http_error_site_returns_422(api):
    client, _ = api
    respx.get("https://gone.com/").mock(return_value=httpx.Response(403))
    r = client.post("/link-analysis", json={"url": "gone.com"})
    assert r.status_code == 422 and "403" in r.json()["error"]["message"]


def test_rate_limit_per_client(api, monkeypatch):
    client, _ = api
    from app.utils.rate_limit import RateLimiter
    monkeypatch.setattr(main, "limiter", RateLimiter(2))
    for _ in range(2):
        assert client.post("/link-analysis", json={"url": "ftp://x.com"}).status_code == 400
    r = client.post("/link-analysis", json={"url": "ftp://x.com"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    assert int(r.headers["Retry-After"]) > 0
    # a different client IP is not affected
    other = client.post("/link-analysis", json={"url": "ftp://x.com"}, headers={"X-Forwarded-For": "8.8.8.8"})
    assert other.status_code == 400
