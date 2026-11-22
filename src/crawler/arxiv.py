"""arXiv Atom API collector (deterministic XML parsing, no LLM)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional
from xml.etree import ElementTree as ET

from ..common.logging import get_logger
from .fetcher import AsyncFetcher

log = get_logger("crawler.arxiv")

ATOM = "http://www.w3.org/2005/Atom"
ARXIV = "http://arxiv.org/schemas/atom"
NS = {"a": ATOM, "arxiv": ARXIV}

DEFAULT_CATEGORIES = ("cs.AI", "cs.LG", "cs.CL", "cs.CV", "stat.ML")
_VERSION_RE = re.compile(r"v\d+$")


@dataclass
class ArxivPaper:
    arxiv_id: str
    title: str
    authors: List[str]
    published: datetime          # tz-aware UTC
    abs_url: str
    pdf_url: Optional[str] = None
    abstract: str = ""
    comment: Optional[str] = None
    journal_ref: Optional[str] = None

    @property
    def search_text(self) -> str:
        """Text searched for GitHub links: abstract + comment."""
        return f"{self.abstract}\n{self.comment or ''}"


def build_query(categories=DEFAULT_CATEGORIES, extra: str = "abs:github") -> str:
    """Build an arXiv search_query biased toward papers that mention GitHub."""
    cats = " OR ".join(f"cat:{c}" for c in categories)
    query = f"({cats})"
    if extra:
        query += f" AND {extra}"
    return query


def _strip_version(arxiv_id: str) -> str:
    """Strip a trailing version suffix (e.g. 2401.01234v2 -> 2401.01234)."""
    return _VERSION_RE.sub("", arxiv_id)


def _id_from_url(id_url: str) -> str:
    tail = id_url.rsplit("/abs/", 1)[-1]
    return _strip_version(tail.strip())


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_atom(xml_text: str) -> List[ArxivPaper]:
    """Parse an arXiv Atom feed into ArxivPaper objects."""
    root = ET.fromstring(xml_text)
    papers: List[ArxivPaper] = []
    for entry in root.findall("a:entry", NS):
        id_url = entry.findtext("a:id", default="", namespaces=NS).strip()
        arxiv_id = _id_from_url(id_url)

        raw_title = entry.findtext("a:title", default="", namespaces=NS) or ""
        title = " ".join(raw_title.split())

        authors = [
            " ".join((a.findtext("a:name", default="", namespaces=NS) or "").split())
            for a in entry.findall("a:author", NS)
        ]
        authors = [a for a in authors if a]

        published_text = entry.findtext("a:published", default="", namespaces=NS)
        published = _parse_dt(published_text) if published_text else datetime.now(timezone.utc)

        abstract = " ".join(
            (entry.findtext("a:summary", default="", namespaces=NS) or "").split()
        )

        abs_url = id_url  # the <id> is the canonical abs URL
        pdf_url = None
        for link in entry.findall("a:link", NS):
            if link.get("type") == "text/html" and link.get("rel") == "alternate":
                abs_url = link.get("href", abs_url)
            if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                pdf_url = link.get("href")

        comment = entry.findtext("arxiv:comment", default=None, namespaces=NS)
        comment = " ".join(comment.split()) if comment else None
        journal_ref = entry.findtext("arxiv:journal_ref", default=None, namespaces=NS)
        journal_ref = " ".join(journal_ref.split()) if journal_ref else None

        if not arxiv_id or not abs_url:
            log.warning("skipping entry without id/url", extra={"extra": {"id": id_url}})
            continue

        papers.append(ArxivPaper(
            arxiv_id=arxiv_id, title=title, authors=authors, published=published,
            abs_url=abs_url, pdf_url=pdf_url, abstract=abstract,
            comment=comment, journal_ref=journal_ref,
        ))
    return papers


class ArxivCollector:
    """Pages the arXiv API sequentially, newest first."""

    def __init__(self, base_url: str = "https://export.arxiv.org/api/query",
                 categories=DEFAULT_CATEGORIES, extra: str = "abs:github") -> None:
        self.base_url = base_url
        self.query = build_query(categories, extra)

    async def fetch_page(self, fetcher: AsyncFetcher, start: int,
                         page_size: int = 100) -> List[ArxivPaper]:
        params = {
            "search_query": self.query,
            "start": str(start),
            "max_results": str(page_size),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        # arXiv API is a sanctioned endpoint; its website robots.txt disallows
        # crawlers, so we skip robots here and honor the 1-req/3s rate instead.
        res = await fetcher.fetch_text(self.base_url, params=params, respect_robots=False)
        if not res.ok or not res.text:
            log.warning("arxiv page fetch failed", extra={"extra": {
                "start": start, "status": res.status, "error": res.error}})
            return []
        return parse_atom(res.text)
