# AI Website Link Analysis API

A FastAPI service that takes any public website URL, extracts its real content, and uses **Google Gemini** to return five sections of structured, website-specific insights:

1. **Competitor vs your brand**
2. **Top 5 target search queries** (Google, Facebook, Instagram, LinkedIn, X)
3. **SEO improvements**: keywords, title, meta description, supporting content and technical observations
4. **Top 5 services**: each tagged as `from_website` or `inferred`
5. **Target audience**: primary and secondary audiences, industries, needs and marketing segments

```
POST http://localhost:4000/link-analysis
{ "url": "https://example.com" }
```

---

## Contents
- [Quick start](#quick-start)
- [Architecture](#architecture)
- [API](#api)
- [Error handling](#error-handling)
- [Security (SSRF protection)](#security-ssrf-protection)
- [Testing](#testing)
- [Deployment](#deployment)
- [LLM research & model recommendation](#llm-platform--model-research)
- [Limitations & next steps](#limitations--next-steps)

---

## Quick start

```bash
git clone <this-repo> && cd ai-link-analysis
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env        # then paste your key from https://aistudio.google.com/apikey
uvicorn app.main:app --host 0.0.0.0 --port 4000
```

- Swagger UI: http://localhost:4000/docs
- Health check: http://localhost:4000/health

```bash
curl -s -X POST http://localhost:4000/link-analysis \
  -H "Content-Type: application/json" \
  -d '{"url": "https://stripe.com"}' | python -m json.tool
```

Or with Docker:

```bash
docker build -t link-analysis . && docker run -p 4000:4000 --env-file .env link-analysis
```

---

## Architecture

```
POST /link-analysis
      │
      ▼
 validators.py ──── normalize URL, block private/internal IPs (SSRF)
      │
      ▼
 website_extractor.py
   • fetch home page (manual redirects, each hop re-validated, 2 MB cap, timeouts)
   • parse <title>, meta, Open Graph, JSON-LD org name, H1–H3, nav links, main text
   • discover up to 3 high-value internal pages (about / services / products / pricing…)
     and fetch them concurrently
   • strip scripts, styles, nav and footer; de-duplicate lines; trim to ~14k characters
      │
      ▼
 llm_service.py
   • ONE Gemini call → all five sections, consistent with each other
   • response_schema = Pydantic model → Gemini is constrained to emit valid JSON
   • retries with backoff on 429 / 5xx / timeouts / invalid JSON → fallback model
   • records tokens, latency and estimated $ cost
      │
      ▼
 competitor_service.py ── dedupe, remove the brand itself, normalize domains
 analysis_service.py ──── assemble response, add disclaimers, cache for 1 hour (LRU + TTL)
```

```
app/
├── main.py                  # FastAPI app, routes, error handlers
├── config.py                # settings from environment variables
├── schemas.py               # request/response + LLM output schema
├── services/
│   ├── website_extractor.py
│   ├── llm_service.py
│   ├── analysis_service.py
│   └── competitor_service.py
└── utils/validators.py      # URL validation + SSRF guard
tests/test_link_analysis.py  # 46 offline tests (network + LLM mocked)
scripts/benchmark.py         # measures latency / tokens / cost / quality per model
```

**Design choices**

| Decision | Why |
|---|---|
| A single LLM call for all five sections | About 3–5× cheaper and faster than one call per section. The model also sees the whole picture, so services, keywords and audience stay consistent. |
| Schema-constrained output (`response_schema`) plus Pydantic validation | Removes "almost-JSON" failures. Enums enforce the platform names and the evidence/source labels. |
| Sub-page crawling (max 3) | Home pages are often marketing-heavy. `/services` and `/about` hold the facts. |
| Content cleaning and trimming before the LLM call | Cuts input tokens by around 80–90% compared with raw HTML, which is the biggest cost lever. |
| `evidence` and `source` labels | Meets the "clearly label inferred / AI-suggested" requirement in a machine-readable way. |
| In-memory cache | Repeated analyses of the same URL are free and instant. Swap in Redis for multi-instance deploys. |

---

## API

### `POST /link-analysis`

**Request**
```json
{ "url": "https://stripe.com" }
```
The scheme is optional, so `stripe.com` also works.

**Response (abridged)**: see **[docs/SAMPLES.md](docs/SAMPLES.md)** for a complete real request/response, error examples, server logs and a screenshot.
```json
{
  "success": true,
  "website": "https://stripe.com/",
  "final_url": "https://stripe.com/in",
  "brand": { "name": "Stripe", "category": "Online payments infrastructure", "summary": "..." },
  "analysis": {
    "competitor_analysis": {
      "brand_name": "Stripe",
      "brand_overview": "...",
      "business_category": "...",
      "main_products_and_services": ["Payments", "Billing", "Connect", "..."],
      "competitors": [
        { "name": "Razorpay", "website": "razorpay.com", "main_offering": "...", "target_market": "...",
          "strengths": ["..."], "weaknesses_or_gaps": ["..."], "source": "ai_suggested" }
      ],
      "comparison_table": [
        { "aspect": "Main Service", "your_brand": "...", "competitor": "...", "competitor_name": "Razorpay" }
      ],
      "your_competitive_advantages": ["..."],
      "market_positioning_opportunities": ["..."]
    },
    "target_search_queries": [
      { "query": "best payment gateway for saas", "platforms": ["Google"], "primary_platform": "Google",
        "intent": "Service Search", "business_relevance": "...", "target_audience": "..." }
    ],
    "seo_improvements": {
      "primary_keyword": "...", "secondary_keywords": [], "long_tail_keywords": [], "business_keywords": [],
      "current_title": "...", "recommended_title": "...", "title_rationale": "...",
      "current_meta_description": "...", "recommended_meta_description": "...",
      "supporting_content": [{ "type": "FAQ", "title": "...", "target_keyword": "...", "reason": "..." }],
      "technical_observations": ["..."]
    },
    "top_services": [
      { "service_name": "...", "description": "...", "target_industry": "...", "target_customer": "...",
        "main_benefit": "...", "evidence": "from_website" }
    ],
    "target_audience": {
      "primary_audience": [], "secondary_audience": [], "industries": [], "business_types": [],
      "customer_profiles": [], "geographic_focus": "...", "customer_needs_and_challenges": [],
      "customer_interests": [], "recommended_marketing_segments": []
    }
  },
  "disclaimers": [
    "Competitors are AI-suggested from model knowledge (source='ai_suggested'), not live market research; verify before use.",
    "No search volumes or rankings are included: they require real data sources ..."
  ],
  "meta": {
    "llm_provider": "Google Gemini", "llm_model": "gemini-3.5-flash-lite", "used_fallback_model": false,
    "pages_analyzed": ["https://stripe.com/in", "https://stripe.com/in/pricing", "..."],
    "content_chars_sent": 14000, "input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0,
    "extraction_ms": 0, "llm_ms": 0, "total_ms": 0, "cached": false
  }
}
```

### `GET /health`
`{"status": "ok", "llm_configured": true, "model": "gemini-3.5-flash-lite"}`

---

## Error handling

Every error has the same shape: `{"success": false, "error": {"code": "...", "message": "..."}}`

| HTTP | `code` | When |
|---|---|---|
| 400 | `invalid_url` | Empty or malformed URL, non-http(s) scheme, embedded credentials, unusual port, unresolvable host |
| 400 | `blocked_url` | The URL or any redirect resolves to a private, loopback, link-local or metadata address |
| 422 | `invalid_request` | Missing or invalid JSON body |
| 422 | `website_unreachable` | Timeout, connection error, HTTP 4xx/5xx, non-HTML content, a JavaScript-only page with no readable text, or too many redirects |
| 429 | `rate_limited` | More than `RATE_LIMIT_PER_MINUTE` requests from one client IP (includes a `Retry-After` header) |
| 429 | `llm_rate_limited` | Gemini quota or rate limit hit, after retries and the fallback model |
| 502 | `llm_invalid_output` / `llm_error` | The LLM failed or returned invalid output after retries and the fallback model |
| 503 | `llm_not_configured` / `llm_auth_error` | Missing or invalid API key |
| 504 | `llm_timeout` | The LLM took too long |

Sub-page failures are not fatal: the analysis continues with whatever pages loaded.

---

## Security (SSRF protection)

`app/utils/validators.py` checks **every** URL the server fetches: the user's URL, each redirect hop and each sub-page.

- Only `http` and `https` are allowed. URLs with `user:pass@` are rejected, and only ports 80, 443, 8080 and 8443 are allowed.
- Hostnames like `localhost` and `*.internal`, `*.local` or `*.corp` are rejected, as are single-label hosts.
- DNS is resolved and **all** returned IPs must be globally routable. This blocks `10/8`, `172.16/12`, `192.168/16`, `127/8`, `169.254/16` (cloud metadata), `100.64/10`, `0.0.0.0`, `::1`, `fc00::/7`, `fe80::/10`, and IPv4-mapped IPv6 such as `::ffff:127.0.0.1`.
- Redirects are followed **manually** (`follow_redirects=False`), so a public site can't bounce us to `169.254.169.254`.
- Response size is capped at 2 MB, with connect and read timeouts.
- **Per-IP rate limit** (default 10 analyses per minute) protects the LLM quota.
- API keys come only from environment variables. `.env` is git-ignored and only `.env.example` is committed.

*Known residual risk:* DNS rebinding between our check and httpx's own resolution. A production hardening step would pin the resolved IP in a custom transport.

---

## Testing

```bash
pytest -q          # 46 passed, fully offline (DNS, HTTP and Gemini are mocked)
```

The tests cover URL normalization, the SSRF block list (IPv4/IPv6, metadata, redirect-to-private and rebinding-style multi-IP answers), HTML extraction and sub-page selection, structured output parsing, cost calculation, retry and fallback logic, competitor de-duplication, caching, per-IP rate limiting, and every API error path.

---

## Deployment

The app is containerized (`Dockerfile`) and includes a `render.yaml` blueprint for **Render's free tier**:

1. Push this repo to GitHub.
2. In Render, choose New → Blueprint and select the repo.
3. Set `GEMINI_API_KEY` in the dashboard. It is never committed.
4. The live endpoints are `https://<app>.onrender.com/link-analysis` and `https://<app>.onrender.com/docs`.

Any Docker host works the same way, for example Railway, Fly.io or Google Cloud Run. The app reads `$PORT`.

> **Live demo:** _add URL here after deploying_

---

## LLM platform & model research

**Goal:** accurate understanding of the website, reliable JSON, fast responses and low cost per analysis.

### A. Platform comparison

Prices are official list prices per 1M tokens, standard (non-batch) tier, **checked 18 Nov 2026**. Context windows for Gemini were read from the API (`models.get` → `input_token_limit`). The others come from provider docs.

| Provider | Model | Input $ | Output $ | Context window | Response speed | Accuracy (this task) | JSON support | Free tier | Recommendation |
|---|---|---|---|---|---|---|---|---|---|
| Google | **`gemini-3.5-flash-lite`** | 0.30 | 2.50 | 1,048,576 | **8.6 s median (measured)** | 35/35 checks, 5/5 services grounded (measured) | ✅ Native `response_schema` | ✅ | ⭐ **Primary**: best speed/cost/quality balance |
| Google | `gemini-3.1-flash-lite` | 0.25 | 1.50 | 1,048,576 | 11.9 s median (measured) | 35/35 checks (measured) | ✅ Native | ✅ | **Fallback**: cheapest, slightly slower |
| Google | `gemini-3.8-flash` | 0.75* | 3.75* | 1,048,576 | 32.9 s median, up to 73 s (measured) | 35/35 checks, slightly richer competitor detail (manual spot-check) | ✅ Native | ✅ | Optional "quality mode". Too slow for an interactive API. Free tier hit 503 "high demand" and timeouts in 3 of 8 attempts |
| Anthropic | Claude Haiku 5.5 | 0.10 (≤100k prompt) | 0.50 | 1M | Not measured | Not measured | ✅ Structured outputs | ❌ | Strongest candidate for high-volume production on price. Benchmark next |
| Anthropic | Claude Sonnet 5.5 | 2.00 | 10.00 | 1M | Not measured | Not measured | ✅ Structured outputs | ❌ | Premium tier for deep competitor research. Not cost-justified here |
| OpenAI | GPT-6 Luna | 0.10 | 0.50 | 1.05M | Not measured | Not measured | ✅ JSON schema mode | ❌ | Same price as Haiku 5.5. Benchmark next |
| OpenAI | GPT-6.1 Terra | 1.00 | 5.00 | 1.05M | Not measured | Not measured | ✅ JSON schema mode | ❌ | Mid-tier. No clear advantage for this task |

\* Gemini 3.8 Flash intro price until 31 Dec 2026, then $1.50 / $7.50.

Sources: [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing), [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing), [Claude models](https://platform.claude.com/docs/en/about-claude/models/overview), [OpenAI pricing](https://openai.com/api/pricing/), [OpenAI models](https://developers.openai.com/api/docs/models).

> **Measured vs estimated:** everything marked *measured* comes from `scripts/benchmark.py` running this exact codebase. Claude and OpenAI were **not** benchmarked (no paid keys were used for this test), so their speed and accuracy are left as "Not measured" rather than guessed.

### B. Benchmark results (measured, 18 Nov 2026, Gemini free tier)

`python -m scripts.benchmark` runs the real pipeline (fetch, extract, LLM, validate) on **5 websites** (Stripe, Zoho CRM, Freshworks, Shopify, Razorpay) per model, with identical input. It records latency, the token counts reported by the API, cost, and 7 automatic quality checks per run: schema-valid JSON, exactly 5 queries, all 5 platforms covered, ≥3 competitors (excluding the brand itself), 5 services, title and meta length in range, and no search-volume claims.

| Model | Runs OK | Median LLM latency | Avg input tok | Avg output tok (incl. thinking) | Avg cost / analysis | Quality checks |
|---|---|---|---|---|---|---|
| `gemini-3.8-flash` | 5/5 (after 3 retries) | 32.9 s | 3,305 | 3,965 | $0.0173 | 35/35 |
| **`gemini-3.5-flash-lite`** | **5/5** | **8.6 s** | 3,305 | 3,245 | $0.0091 | **35/35** |
| `gemini-3.1-flash-lite` | 5/5 | 11.9 s | 3,305 | 3,973 | $0.0068 | 35/35 |

Raw per-run data is in [`docs/benchmark_results.json`](docs/benchmark_results.json), and every model's full output for every site is in [`docs/samples/`](docs/samples/).

**Reading the results honestly**
- All three models passed every automatic check, so these checks show reliability, not fine-grained accuracy. I also compared the Zoho CRM outputs by hand. All three named correct, real competitors (Salesforce, HubSpot, plus Pipedrive / Dynamics 365 / Freshsales) and grounded services in Zoho's own product names. 3.8 Flash gave slightly more specific positioning ideas, but not enough to justify being **3.8× slower and 1.9× more expensive**.
- Website extraction adds about 1–3 s on top of LLM latency. Total end-to-end time for `3.5-flash-lite` is about 10–12 s.
- Free-tier latency varies with Google's load. These are single runs per site, not a large-scale study.

### C. Cost per analysis

Measured average for this pipeline: **~3,300 input tokens** (cleaned content of ~14k chars plus prompt) and **~3,200–4,000 output tokens** (the JSON for 5 sections plus low-level thinking).

| Model | Cost / analysis | Per 1,000 analyses | Basis |
|---|---|---|---|
| **gemini-3.5-flash-lite** | **$0.0091** | **$9.10** | measured |
| gemini-3.1-flash-lite | $0.0068 | $6.80 | measured |
| gemini-3.8-flash (intro → list) | $0.0173 → ~$0.035 | $17 → $35 | measured → estimated |
| Claude Haiku 5.5 / GPT-6 Luna | ~$0.0021 | ~$2.10 | estimated from measured token counts × list price |
| GPT-6.1 Terra | ~$0.021 | ~$21 | estimated |
| Claude Sonnet 5.5 | ~$0.042 | ~$42 | estimated |

Every API response includes `meta.estimated_cost_usd`, the real cost of that specific call based on the token counts Gemini returns.

### D. Cost optimization (implemented ✅ / recommended ➕)

- ✅ **Clean before sending:** strip scripts, styles, SVG, nav, footer and duplicate lines, and send text rather than HTML. Input is only ~3.3k tokens even though 4 pages are read.
- ✅ **Hard content budget** (`MAX_CONTENT_CHARS`), filled with the highest-signal parts first: title, meta, headings, then body text.
- ✅ **One call instead of five**, so the system prompt and website content are sent once.
- ✅ **Right-sized model:** the benchmark showed flash-lite matches the bigger model on this task, so we use it. That's 47% cheaper and 3.8× faster than 3.8 Flash.
- ✅ **Low thinking level:** the task is analysis, not multi-step reasoning, and thinking tokens are billed as output.
- ✅ **Cache** analyses per URL for 1 hour, so repeated requests cost $0 and return instantly.
- ✅ **Per-IP rate limit** (default 10 per minute) so a public demo can't drain the quota.
- ✅ **Schema-constrained output** avoids paying for "retry because the JSON was broken" calls.
- ➕ Use the **Batch API** (50% off) for bulk or offline analyses.
- ➕ **Model routing:** keep flash-lite for everything, and send only the competitor section to a stronger model when a "deep" flag is set.
- ➕ Use Redis instead of the in-memory cache, and add a content-hash cache so unchanged sites are never re-analyzed.

### E. Final recommendation

| | |
|---|---|
| **Recommended provider** | Google Gemini |
| **Recommended model** | **`gemini-3.5-flash-lite`** (thinking level: low) |
| **Fallback model** | `gemini-3.1-flash-lite`, used automatically on errors, 404s or rate limits |
| **Input tokens** | ~3,300 per analysis (measured) |
| **Output tokens** | ~3,250 per analysis (measured) |
| **Cost** | **~$0.009 per analysis** (measured at list price). $0 on the free tier |
| **Response time** | **~8.6 s LLM median, ~10–12 s end-to-end** (measured). Cached repeats take under 50 ms |

**Why this model:**
1. **Accuracy:** it passed 35/35 automatic checks, grounded 5/5 services in real website content on every site, and named correct real competitors in the manual spot-check. That's the same result as the 2.5× pricier 3.8 Flash.
2. **Speed:** it was the fastest model measured (8.6 s against 32.9 s), which matters for an interactive API.
3. **Cost:** $0.009 per analysis, and free during development and demo.
4. **JSON reliability:** native `response_schema` with Pydantic models produced 0 schema failures in 15 runs.
5. **Large content:** its 1M-token context is far more than needed. The real constraint is cost, which the content cleaning handles.

**Balancing accuracy, speed and cost:** flash-lite is the default. `GEMINI_MODEL=gemini-3.8-flash` can be set per deployment for a "quality mode". For high production volume, the next step is to benchmark **Claude Haiku 5.5** and **GPT-6 Luna**, which are about 4× cheaper per analysis on list price, using the same script. Switch only if their quality holds up. `llm_service.py` is isolated behind one interface, so adding a provider doesn't touch the API layer.

---

## Limitations & next steps

- **Competitors are AI-suggested**, not researched live. A paid-tier upgrade could add Gemini's *Grounding with Google Search* to cite real sources.
- **JavaScript-rendered sites** (pure SPAs) return little HTML. These get a clear `website_unreachable` error. A Playwright renderer would fix this.
- **Bot-protected sites** (Cloudflare / Akamai challenges, e.g. canva.com and infosys.com returned 403 in testing) are not bypassed. The crawler identifies itself honestly and returns a clear `website_unreachable` error instead.
- **No search volumes** are returned, by design. They would need Google Keyword Planner, Meta or LinkedIn APIs.
- The cache and rate limiter are in-memory and single-process. Use Redis when running more than one instance.
