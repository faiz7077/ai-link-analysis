"""Fetch a website safely and turn it into compact, LLM-friendly text.

Pipeline:
1. Fetch the home page with SSRF checks on every redirect hop and a size cap.
2. Parse title, meta tags, Open Graph, JSON-LD, headings, nav links and body text.
3. Discover a few high-value internal pages (about / services / products ...)
   and fetch them concurrently.
4. De-duplicate lines and trim everything to a token budget.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from app.config import Settings, get_settings
from app.utils.validators import BlockedURLError, InvalidURLError, validate_public_url

NOISE_TAGS = ["script", "style", "noscript", "svg", "iframe", "template", "form", "canvas"]
SUBPAGE_HINTS = (
    "about", "service", "solution", "product", "what-we-do", "offering",
    "pricing", "industr", "feature", "company", "who-we-are", "platform",
)


class WebsiteFetchError(Exception):
    """The website could not be fetched or did not return usable HTML."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class PageContent:
    url: str
    title: str = ""
    meta_description: str = ""
    meta_keywords: str = ""
    og: dict[str, str] = field(default_factory=dict)
    headings: list[str] = field(default_factory=list)
    text: str = ""
    json_ld_names: list[str] = field(default_factory=list)
    internal_links: list[tuple[str, str]] = field(default_factory=list)  # (url, anchor text)
    lang: str = ""


@dataclass
class WebsiteContent:
    url: str
    final_url: str
    home: PageContent
    subpages: list[PageContent] = field(default_factory=list)

    @property
    def brand_hint(self) -> str:
        return (
            self.home.og.get("site_name")
            or (self.home.json_ld_names[0] if self.home.json_ld_names else "")
            or self.home.title.split("|")[0].split(" - ")[0].strip()
        )

    def to_prompt_text(self, max_chars: int) -> str:
        """Serialize the most useful signals first, then trim to `max_chars`."""
        h = self.home
        parts = [
            f"URL: {self.final_url}",
            f"Page title: {h.title}",
            f"Meta description: {h.meta_description}",
        ]
        if h.meta_keywords:
            parts.append(f"Meta keywords: {h.meta_keywords}")
        if h.og:
            parts.append("Open Graph: " + "; ".join(f"{k}={v}" for k, v in h.og.items()))
        if h.json_ld_names:
            parts.append("Structured-data names: " + ", ".join(h.json_ld_names))
        if h.lang:
            parts.append(f"Language: {h.lang}")
        if h.internal_links:
            nav = ", ".join(t for _, t in h.internal_links[:40] if t)
            parts.append(f"Navigation / internal links: {nav}")
        parts.append("Headings:\n- " + "\n- ".join(h.headings[:40]))
        parts.append("Home page text:\n" + h.text)

        for sp in self.subpages:
            parts.append(
                f"\n--- Sub-page: {sp.url} ---\nTitle: {sp.title}\n"
                f"Headings: {' | '.join(sp.headings[:20])}\n{sp.text}"
            )
        text = "\n".join(p for p in parts if p.strip())
        if len(text) > max_chars:
            text = text[:max_chars].rsplit(" ", 1)[0] + " …[truncated]"
        return text


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def parse_html(html: str, url: str, per_page_chars: int = 6000) -> PageContent:
    soup = BeautifulSoup(html, "lxml")
    page = PageContent(url=url)

    if soup.html and soup.html.get("lang"):
        page.lang = str(soup.html.get("lang"))
    if soup.title and soup.title.string:
        page.title = _clean_text(soup.title.string)

    for meta in soup.find_all("meta"):
        name = (meta.get("name") or meta.get("property") or "").lower()
        content = _clean_text(meta.get("content") or "")
        if not content:
            continue
        if name == "description":
            page.meta_description = content
        elif name == "keywords":
            page.meta_keywords = content
        elif name.startswith("og:") and name[3:] in {"site_name", "title", "description", "type"}:
            page.og[name[3:]] = content

    # JSON-LD often carries the official organisation name.
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict) and item.get("@type") in {"Organization", "Corporation", "LocalBusiness", "WebSite"}:
                name = item.get("name")
                if isinstance(name, str) and name not in page.json_ld_names:
                    page.json_ld_names.append(_clean_text(name))

    # Internal links (collected before we strip nav/footer).
    base_host = urlsplit(url).hostname
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"]).split("#")[0]
        parts = urlsplit(href)
        if parts.scheme not in ("http", "https") or parts.hostname != base_host:
            continue
        if re.search(r"\.(pdf|jpe?g|png|gif|zip|mp4|svg|webp)$", parts.path, re.I):
            continue
        if href not in seen:
            seen.add(href)
            page.internal_links.append((href, _clean_text(a.get_text())[:60]))

    for tag in soup(NOISE_TAGS):
        tag.decompose()

    page.headings = [
        t for t in (_clean_text(h.get_text()) for h in soup.find_all(["h1", "h2", "h3"])) if t
    ][:60]

    for tag in soup(["nav", "footer", "header", "aside"]):
        tag.decompose()
    body = soup.find("main") or soup.body or soup
    lines, seen_lines = [], set()
    for chunk in body.stripped_strings:
        line = _clean_text(chunk)
        if len(line) < 3 or line.lower() in seen_lines:
            continue
        seen_lines.add(line.lower())
        lines.append(line)
    page.text = " ".join(lines)[:per_page_chars]
    return page


def pick_subpages(links: list[tuple[str, str]], home_url: str, limit: int) -> list[str]:
    home = home_url.rstrip("/")
    scored: list[tuple[int, str]] = []
    for href, text in links:
        if href.rstrip("/") == home:
            continue
        hay = (urlsplit(href).path + " " + text).lower()
        score = sum(1 for hint in SUBPAGE_HINTS if hint in hay)
        depth = urlsplit(href).path.strip("/").count("/")
        if score and depth <= 1:
            scored.append((score * 10 - depth, href))
    scored.sort(key=lambda x: -x[0])
    out: list[str] = []
    for _, href in scored:
        if href not in out:
            out.append(href)
        if len(out) >= limit:
            break
    return out


class WebsiteExtractor:
    def __init__(self, settings: Settings | None = None, client: httpx.AsyncClient | None = None):
        self.settings = settings or get_settings()
        self._client = client

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.fetch_timeout_seconds),
            follow_redirects=False,  # we follow manually so each hop is SSRF-checked
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    async def fetch_html(self, client: httpx.AsyncClient, url: str) -> tuple[str, str]:
        """Return (final_url, html). Re-validates the target on every redirect."""
        current = await validate_public_url(url)
        for _ in range(self.settings.max_redirects + 1):
            try:
                async with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise WebsiteFetchError("Redirect without a Location header.")
                        current = await validate_public_url(urljoin(current, location))
                        continue
                    if resp.status_code >= 400:
                        raise WebsiteFetchError(
                            f"Website returned HTTP {resp.status_code}.", status_code=resp.status_code
                        )
                    ctype = resp.headers.get("content-type", "")
                    if ctype and "html" not in ctype and "xml" not in ctype:
                        raise WebsiteFetchError(f"URL did not return an HTML page (content-type: {ctype}).")
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self.settings.max_page_bytes:
                            break
                    encoding = resp.encoding or "utf-8"
                    return current, bytes(body).decode(encoding, errors="replace")
            except httpx.TimeoutException as exc:
                raise WebsiteFetchError("Timed out while fetching the website.") from exc
            except httpx.HTTPError as exc:
                raise WebsiteFetchError(f"Could not connect to the website: {exc.__class__.__name__}.") from exc
        raise WebsiteFetchError("Too many redirects.")

    async def extract(self, url: str) -> WebsiteContent:
        client = self._client or self._new_client()
        try:
            final_url, html = await self.fetch_html(client, url)
            home = parse_html(html, final_url)
            if len(home.text) < 50 and not home.meta_description and not home.headings:
                raise WebsiteFetchError(
                    "The website returned almost no readable content "
                    "(it may require JavaScript or block automated access)."
                )

            sub_urls = pick_subpages(home.internal_links, final_url, self.settings.max_subpages)

            async def safe_fetch(u: str) -> PageContent | None:
                try:
                    fu, h = await self.fetch_html(client, u)
                    return parse_html(h, fu, per_page_chars=3000)
                except (WebsiteFetchError, InvalidURLError, BlockedURLError):
                    return None  # sub-pages are best effort

            subpages = [p for p in await asyncio.gather(*(safe_fetch(u) for u in sub_urls)) if p]
            return WebsiteContent(url=url, final_url=final_url, home=home, subpages=subpages)
        finally:
            if self._client is None:
                await client.aclose()
