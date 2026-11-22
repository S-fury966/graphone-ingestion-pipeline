"""Fetch a startup's own website + find its pricing page (robots respected)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import yaml
from bs4 import BeautifulSoup

from ..common.logging import get_logger
from ..llm.chunker import html_to_text
from ..resolver.normalize import registrable_domain
from .fetcher import AsyncFetcher

log = get_logger("crawler.product_pages")

MAX_PAGES = 3
JS_ONLY_MIN_CHARS = 300
FILTERS_PATH = "config/product_filters.yaml"
_PRICING_RE = re.compile(r"pricing|plans|price", re.I)

# crawl_status values
OK = "ok"
BLOCKED = "blocked"
JS_ONLY = "js_only"
ROBOTS = "robots_disallowed"
HTTP_ERROR = "http_error"
NO_WEBSITE = "no_website"
PARKED = "parked"


@dataclass
class CrawlResult:
    slug: str
    status: str
    pages: List[Tuple[str, str]] = field(default_factory=list)  # (url, visible_text)
    pages_fetched: int = 0
    error: Optional[str] = None
    html_pages: List[Tuple[str, str]] = field(default_factory=list)  # (url, raw_html)

    @property
    def combined_text(self) -> str:
        return "\n\n".join(f"[PAGE: {u}]\n{t}" for u, t in self.pages)


@lru_cache(maxsize=1)
def _load_parked_config(path: str = FILTERS_PATH) -> Tuple[frozenset, Tuple[str, ...]]:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        hosts = frozenset(str(h).strip().lower() for h in data.get("parked_hosts", []))
        phrases = tuple(str(p).strip().lower() for p in data.get("parked_phrases", []))
        return hosts, phrases
    except Exception:  # noqa: BLE001
        return frozenset(), tuple()


def is_parked(final_url: str, text: str,
              hosts: Optional[frozenset] = None,
              phrases: Optional[Tuple[str, ...]] = None) -> bool:
    """True if the resolved host is a known parking/marketplace host, or the page
    text carries a domain-for-sale phrase."""
    if hosts is None or phrases is None:
        hosts, phrases = _load_parked_config()
    host = urlsplit(final_url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if any(host == h or host.endswith("." + h) for h in hosts):
        return True
    low = (text or "").lower()
    return any(p in low for p in phrases)


def _ensure_scheme(url: str) -> str:
    return url if "://" in url else "https://" + url


def _visible_text(html: str) -> str:
    header, blocks = html_to_text(html)
    return "\n".join([header] + [b.text for b in blocks]).strip()


def find_pricing_links(html: str, base_url: str) -> List[str]:
    """Same-domain links whose href or anchor text mentions pricing/plans/price."""
    soup = BeautifulSoup(html, "lxml")
    base_domain = registrable_domain(base_url)
    out: List[str] = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        text = a.get_text(" ", strip=True)
        if not (_PRICING_RE.search(href) or _PRICING_RE.search(text)):
            continue
        full = urljoin(base_url, href)
        if urlsplit(full).scheme not in ("http", "https"):
            continue
        if registrable_domain(full) != base_domain:   # same registrable domain only
            continue
        norm = full.split("#")[0]
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


async def crawl_startup(fetcher: AsyncFetcher, slug: str, website: Optional[str]) -> CrawlResult:
    """Fetch homepage, then (at most) a pricing page. Classify the outcome."""
    if not website:
        return CrawlResult(slug, NO_WEBSITE)

    home_url = _ensure_scheme(website.strip())
    res = await fetcher.fetch_text(home_url, respect_robots=True)

    if res.error == "disallowed by robots.txt":
        return CrawlResult(slug, ROBOTS, error=res.error)
    if res.blocked:
        return CrawlResult(slug, BLOCKED, pages_fetched=1, error=f"blocked ({res.status})")
    if not res.ok or not res.text:
        return CrawlResult(slug, HTTP_ERROR, pages_fetched=1, error=res.error or f"HTTP {res.status}")

    final_home = res.final_url or home_url
    home_text = _visible_text(res.text)

    # Parked / domain-for-sale page: no LLM call, no product.
    if is_parked(final_home, home_text):
        return CrawlResult(slug, PARKED, pages_fetched=1,
                           error="parked / domain-for-sale page")

    pages: List[Tuple[str, str]] = [(final_home, home_text)]
    html_pages: List[Tuple[str, str]] = [(final_home, res.text)]
    fetched = 1

    # Discover a pricing page on the homepage; else try /pricing once.
    candidates = find_pricing_links(res.text, final_home)
    if not candidates:
        candidates = [urljoin(final_home, "/pricing")]

    for url in candidates:
        if fetched >= MAX_PAGES:
            break
        pr = await fetcher.fetch_text(url, respect_robots=True)
        fetched += 1
        if pr.ok and pr.text and not pr.blocked:
            text = _visible_text(pr.text)
            if text:
                pages.append((pr.final_url or url, text))
                html_pages.append((pr.final_url or url, pr.text))
                break  # one good pricing page is enough

    combined_len = sum(len(t) for _, t in pages)
    status = JS_ONLY if combined_len < JS_ONLY_MIN_CHARS else OK
    return CrawlResult(slug, status, pages=pages, pages_fetched=fetched,
                       html_pages=html_pages)
