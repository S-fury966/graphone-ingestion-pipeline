"""Pydantic-based loader for config/sources.yaml.

Sources are data, not code: adding or scaling sources must not require code
changes (a HARD RULE). This loader validates the YAML into typed objects.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

DEFAULT_SOURCES_PATH = Path("config/sources.yaml")

SourceType = Literal["api", "rss", "html", "js"]


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: SourceType
    base_url: HttpUrl
    record_types: List[str]
    rate_limit_per_sec: float = Field(gt=0)
    max_concurrency: int = Field(gt=0)
    enabled: bool = True

    # ---- optional signal-ingestion (NEWS/JOBS) hints; all backward-compatible ----
    # IANA tz for NAIVE timestamps from this source (e.g. "America/New_York").
    # None => naive timestamps are assumed UTC (and logged).
    source_tz: Optional[str] = None
    # Treat a feed item's modified/updated date as its publication date. Off by
    # default: a modified-only item is considered undated for freshness.
    updated_is_published: bool = False
    # Items need an AI keyword filter before they count (general-tech sources).
    ai_filter: bool = False
    # The feed already carries the full article body (skip the per-article crawl).
    full_text_in_feed: bool = False
    # Crawl each item's page for publication date / full text. Off when the
    # article host blocks automated fetches (use feed text only).
    article_crawl: bool = True
    # Where the authoritative PUBLICATION date comes from:
    #   "feed"    => the feed's own pubDate/published field (default).
    #   "article" => ONLY the linked article page (JSON-LD/meta/URL/relative).
    #                Used for aggregators like Hacker News where the feed date is
    #                the SUBMISSION time, not publication. Snapshot inference is
    #                disabled for these (appearing on HN proves submission, not
    #                publication): no article date => FRESHNESS_UNPROVABLE.
    date_authority: Literal["feed", "article"] = "feed"
    # Minimum characters of real article text to keep an item; otherwise NO_TEXT.
    min_text_chars: int = Field(default=400, ge=0)
    # JOBS: "complete" = the API returns ALL open jobs (ATS boards), so a full
    # successful fetch satisfies snapshot continuity. "chronological" = a rolling
    # feed that needs the URL-overlap continuity check.
    listing_type: Literal["complete", "chronological"] = "chronological"
    # This source's items are extracted via the LLM (HN "Who is hiring"); the
    # --skip-llm-sources flag excludes it from a run.
    uses_llm: bool = False
    # ---- Phase V anti-bot ladder policy ----
    # render: when a page is js_only, may we escalate to a real browser?
    #   never  = never render (robots/terms forbid automated access or AI use)
    #   auto   = render only when the plain fetch returns too little text (default)
    #   always = always render (rare; SPA that never returns server HTML)
    render: Literal["never", "auto", "always"] = "auto"
    # Honour robots.txt for this source (always True for HTML; documented API
    # endpoints may set False where that is the sanctioned access path).
    respect_robots: bool = True


class SourcesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: List[SourceConfig] = Field(default_factory=list)

    def enabled_sources(self) -> List[SourceConfig]:
        return [s for s in self.sources if s.enabled]


def load_sources(path: str | Path = DEFAULT_SOURCES_PATH) -> SourcesConfig:
    """Load and validate the sources YAML file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"sources config not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return SourcesConfig.model_validate(raw)
