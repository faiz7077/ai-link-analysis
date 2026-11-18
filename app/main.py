"""FastAPI entry point.

Run locally:
    uvicorn app.main:app --host 0.0.0.0 --port 4000
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import get_settings
from app.schemas import ErrorResponse, LinkAnalysisRequest, LinkAnalysisResponse
from app.services.analysis_service import AnalysisService
from app.services.llm_service import LLMError
from app.services.website_extractor import WebsiteFetchError
from app.utils.rate_limit import RateLimiter, client_ip
from app.utils.validators import BlockedURLError, InvalidURLError

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("link-analysis")

app = FastAPI(
    title="AI Website Link Analysis API",
    version="1.0.0",
    description=(
        "Analyzes a public website with Google Gemini and returns competitor, search-query, SEO, "
        "services and target-audience insights as structured JSON."
    ),
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])

service = AnalysisService(get_settings())
limiter = RateLimiter(get_settings().rate_limit_per_minute)


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"success": False, "error": {"code": code, "message": message}})


@app.exception_handler(InvalidURLError)
async def invalid_url_handler(_: Request, exc: InvalidURLError):
    return error(400, "invalid_url", str(exc))


@app.exception_handler(BlockedURLError)
async def blocked_url_handler(_: Request, exc: BlockedURLError):
    return error(400, "blocked_url", str(exc))


@app.exception_handler(WebsiteFetchError)
async def fetch_error_handler(_: Request, exc: WebsiteFetchError):
    return error(422, "website_unreachable", str(exc))


@app.exception_handler(LLMError)
async def llm_error_handler(_: Request, exc: LLMError):
    status = {"llm_rate_limited": 429, "llm_not_configured": 503, "llm_auth_error": 503, "llm_timeout": 504}.get(
        exc.code, 502
    )
    return error(status, exc.code, str(exc))


@app.exception_handler(RequestValidationError)
async def validation_handler(_: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(p) for p in first.get("loc", []) if p != "body")
    return error(422, "invalid_request", f"{field}: {first.get('msg', 'invalid request')}".strip(": "))


@app.exception_handler(Exception)
async def unhandled_handler(_: Request, exc: Exception):
    log.exception("Unhandled error")
    return error(500, "internal_error", "An unexpected error occurred.")


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/docs")


@app.get("/health", tags=["meta"])
async def health():
    s = get_settings()
    return {"status": "ok", "llm_configured": bool(s.gemini_api_key), "model": s.gemini_model}


ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Invalid URL or blocked private/internal address"},
    422: {"model": ErrorResponse, "description": "Website unreachable or invalid request body"},
    429: {"model": ErrorResponse, "description": "Too many requests from this client, or LLM rate limit"},
    502: {"model": ErrorResponse, "description": "LLM failure"},
    503: {"model": ErrorResponse, "description": "LLM not configured"},
    504: {"model": ErrorResponse, "description": "LLM timeout"},
}


@app.post("/link-analysis", response_model=LinkAnalysisResponse, responses=ERROR_RESPONSES, tags=["analysis"])
async def link_analysis(body: LinkAnalysisRequest, request: Request):
    """Fetch the website, analyze it with the LLM and return five insight sections."""
    retry_after = limiter.check(client_ip(request))
    if retry_after:
        resp = error(429, "rate_limited", f"Too many requests. Try again in {retry_after} seconds.")
        resp.headers["Retry-After"] = str(retry_after)
        return resp
    log.info("Analyzing %s", body.url)
    return await service.analyze(body.url)
