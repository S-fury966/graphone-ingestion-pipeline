"""Extract a PUBLICATION date from article HTML.

Only publication dates count toward freshness. Modified/updated fields
(``dateModified``, ``article:modified_time``, Atom ``<updated>``, RSS
``lastBuildDate``) are deliberately ignored — a story edited today but written
last month is not fresh. A source may set ``updated_is_published: true`` in its
config to override that, but the override is applied by the pipeline, never
here: this module never treats a modified field as a publication date.

``extract_publication_date(html, url)`` tries sources in priority order and
returns the first valid hit as ``(ParsedDate, date_source)``, or ``None``.
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Optional

from bs4 import BeautifulSoup

from ..common.dates import ParsedDate, parse_date, utc_now
from ..common.logging import get_logger
from ..schemas.enums import DateConfidence

log = get_logger("crawler.article_dates")

# date_source labels (kept in sync with freshness/export columns)
SRC_JSONLD = "jsonld"
SRC_META = "meta"
SRC_URL = "url_pattern"
SRC_RELATIVE = "relative_text"

_PUBLISHED_KEYS = ("datepublished", "datecreated", "published", "datefirstpublished")
_MODIFIED_KEYS = ("datemodified", "dateupdated", "modified")

_URL_DATE = re.compile(r"/(20\d{2})[/-](0[1-9]|1[0-2])[/-](0[1-9]|[12]\d|3[01])(?:[/-]|$)")
_RELATIVE_TEXT = re.compile(
    r"\b(?:\d+|an?)\s+(?:second|minute|hour|day)s?\s+ago\b|\bjust now\b|\byesterday\b",
    re.IGNORECASE,
)


def _iter_jsonld(soup: BeautifulSoup):
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        yield from _flatten_jsonld(data)


def _flatten_jsonld(data):
    """Yield every dict node in a possibly nested JSON-LD blob (@graph, lists)."""
    if isinstance(data, dict):
        yield data
        for v in data.values():
            if isinstance(v, (dict, list)):
                yield from _flatten_jsonld(v)
    elif isinstance(data, list):
        for item in data:
            yield from _flatten_jsonld(item)


def _jsonld_published(soup: BeautifulSoup) -> Optional[str]:
    for node in _iter_jsonld(soup):
        for key, val in node.items():
            kl = key.lower()
            if kl in _PUBLISHED_KEYS and isinstance(val, str) and val.strip():
                return val.strip()
    return None


def _meta_published(soup: BeautifulSoup) -> Optional[str]:
    # property=... first (OpenGraph/article), then name=...
    candidates = [
        ("property", "article:published_time"),
        ("property", "og:published_time"),
        ("name", "article:published_time"),
        ("itemprop", "datePublished"),
        ("name", "date"),
        ("name", "pubdate"),
        ("name", "publish-date"),
        ("name", "publication_date"),
    ]
    for attr, value in candidates:
        tag = soup.find("meta", attrs={attr: re.compile(f"^{re.escape(value)}$", re.I)})
        if tag and tag.get("content", "").strip():
            # Guard: never accept a modified field even if mislabeled here.
            if value.lower() not in _MODIFIED_KEYS:
                return tag["content"].strip()
    return None


def _time_tag_published(soup: BeautifulSoup) -> Optional[str]:
    scope = soup.find("article") or soup
    for t in scope.find_all("time"):
        dt_attr = t.get("datetime")
        if not dt_attr:
            continue
        itemprop = (t.get("itemprop") or "").lower()
        cls = " ".join(t.get("class") or []).lower()
        if "modified" in itemprop or "updated" in cls or "modified" in cls:
            continue
        return dt_attr.strip()
    return None


def _url_published(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = _URL_DATE.search(url)
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def _relative_text(soup: BeautifulSoup) -> Optional[str]:
    scope = soup.find("article") or soup
    # Look at a bounded slice of visible text so we don't scan huge pages.
    text = scope.get_text(" ", strip=True)[:4000]
    m = _RELATIVE_TEXT.search(text)
    return m.group(0) if m else None


def extract_publication_date(html: str, url: Optional[str] = None,
                             now=None) -> Optional[tuple[ParsedDate, str]]:
    """Return ``(ParsedDate, date_source)`` for the first valid publication date.

    Priority: JSON-LD ``datePublished`` → ``meta`` publication tags / ``<time>``
    → a date in the URL path → visible relative text. Returns ``None`` when no
    publication date is found. Modified/updated fields are never used.
    """
    now = now or utc_now()
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")

    # 1) JSON-LD datePublished
    raw = _jsonld_published(soup)
    if raw:
        p = parse_date(raw, now=now)
        if p is not None and p.ok:
            return p, SRC_JSONLD

    # 2) meta publication tags, then <time datetime> / itemprop inside <article>
    raw = _meta_published(soup) or _time_tag_published(soup)
    if raw:
        p = parse_date(raw, now=now)
        if p is not None and p.ok:
            return p, SRC_META

    # 3) date pattern in the URL (day-granular -> +-1 day uncertainty)
    raw = _url_published(url)
    if raw:
        p = parse_date(raw, now=now)
        if p is not None and p.ok:
            # A URL date has no time-of-day; widen the margin to a full day so
            # the freshness window test accounts for that imprecision.
            p.margin = max(p.margin, timedelta(days=1))
            return p, SRC_URL

    # 4) visible relative text ("2 hours ago", "yesterday")
    raw = _relative_text(soup)
    if raw:
        p = parse_date(raw, now=now)
        if p is not None and p.ok:
            return p, SRC_RELATIVE

    return None
