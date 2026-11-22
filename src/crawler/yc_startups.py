"""YC startup collector using the open yc-oss JSON mirror (deterministic)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from ..common.logging import get_logger
from ..resolver.normalize import normalize_name, registrable_domain
from .fetcher import AsyncFetcher

log = get_logger("crawler.yc_startups")

MIRROR = "https://yc-oss.github.io/api"
AI_TAG_URL = f"{MIRROR}/tags/artificial-intelligence.json"
ALL_URL = f"{MIRROR}/companies/all.json"
SOURCE_NAME = "Y Combinator"


@dataclass
class StartupRecord:
    slug: str
    entity_name: str
    normalized_name: str
    employee_count: Optional[int]
    website: Optional[str]
    domain: Optional[str]
    one_liner: Optional[str]
    batch: Optional[str]
    tags: List[str]
    source_name: str
    source_url: str

    def data_score(self) -> int:
        """How much data this record carries (for collision resolution)."""
        return sum(bool(x) for x in (
            self.employee_count, self.website, self.one_liner, self.batch, self.tags))


def _clean_ws(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    cleaned = " ".join(str(text).split())
    return cleaned or None


def map_company(raw: dict[str, Any]) -> Optional[StartupRecord]:
    """Map a raw YC mirror record to a StartupRecord. None if unusable."""
    name = _clean_ws(raw.get("name"))
    slug = raw.get("slug")
    if not name or not slug:
        return None

    team_size = raw.get("team_size")
    employee_count = team_size if isinstance(team_size, int) and team_size > 0 else None

    website = _clean_ws(raw.get("website"))
    # Most specific real URL: the YC company page; fall back to the mirror JSON.
    source_url = raw.get("url") or raw.get("api") or AI_TAG_URL

    return StartupRecord(
        slug=slug,
        entity_name=name,
        normalized_name=normalize_name(name),
        employee_count=employee_count,
        website=website,
        domain=registrable_domain(website),
        one_liner=_clean_ws(raw.get("one_liner")),
        batch=_clean_ws(raw.get("batch")),
        tags=[t for t in (raw.get("tags") or []) if isinstance(t, str)],
        source_name=SOURCE_NAME,
        source_url=source_url,
    )


def _name_key(rec: StartupRecord) -> str:
    return rec.normalized_name or f"__slug__{rec.slug}"


def dedup(records: List[StartupRecord]) -> Tuple[List[StartupRecord], int]:
    """Dedup by normalized_name AND by domain. Keep the record with more data.

    Order-independent: on a collision the richer record wins and the loser's
    index entries are removed. Returns (kept, duplicates_removed).
    """
    winners: dict[int, StartupRecord] = {}
    name_index: dict[str, int] = {}
    domain_index: dict[str, int] = {}
    removed = 0

    for rec in records:
        nkey = _name_key(rec)
        existing_id = name_index.get(nkey)
        if existing_id is None and rec.domain:
            existing_id = domain_index.get(rec.domain)

        if existing_id is None:
            rid = id(rec)
            winners[rid] = rec
            name_index[nkey] = rid
            if rec.domain:
                domain_index[rec.domain] = rid
            continue

        removed += 1
        existing = winners[existing_id]
        if rec.data_score() > existing.data_score():
            log.info("duplicate startup collapsed", extra={"extra": {
                "kept": rec.slug, "dropped": existing.slug,
                "normalized_name": rec.normalized_name, "domain": rec.domain}})
            # Remove the loser's index entries, then install the winner.
            del winners[existing_id]
            if name_index.get(_name_key(existing)) == existing_id:
                del name_index[_name_key(existing)]
            if existing.domain and domain_index.get(existing.domain) == existing_id:
                del domain_index[existing.domain]
            rid = id(rec)
            winners[rid] = rec
            name_index[nkey] = rid
            if rec.domain:
                domain_index[rec.domain] = rid
        else:
            log.info("duplicate startup collapsed", extra={"extra": {
                "kept": existing.slug, "dropped": rec.slug,
                "normalized_name": rec.normalized_name, "domain": rec.domain}})
            # Existing stays; point rec's keys at it so later dups still match.
            name_index[nkey] = existing_id
            if rec.domain:
                domain_index[rec.domain] = existing_id

    return list(winners.values()), removed


class YCCollector:
    def __init__(self, ai_url: str = AI_TAG_URL, all_url: str = ALL_URL) -> None:
        self.ai_url = ai_url
        self.all_url = all_url

    async def _fetch_list(self, fetcher: AsyncFetcher, url: str) -> List[dict]:
        res = await fetcher.fetch_json(url, respect_robots=False)
        if not res.ok or not isinstance(res.json, list):
            log.warning("yc list fetch failed", extra={"extra": {
                "url": url, "status": res.status, "error": res.error}})
            return []
        return res.json

    async def collect(self, fetcher: AsyncFetcher, target: int = 1000,
                      min_margin: int = 100) -> Tuple[List[StartupRecord], dict]:
        """Collect AI companies, topping up from all.json to clear target+margin.

        Returns (deduped_records, stats).
        """
        comfortable = target + min_margin
        raw_ai = await self._fetch_list(fetcher, self.ai_url)
        records = [m for m in (map_company(r) for r in raw_ai) if m]
        fetched = len(records)

        kept, removed = dedup(records)
        topped_up = 0

        if len(kept) < comfortable:
            raw_all = await self._fetch_list(fetcher, self.all_url)
            have_slugs = {r.slug for r in kept}
            extra: List[StartupRecord] = []
            for r in raw_all:
                m = map_company(r)
                if m and m.slug not in have_slugs:
                    extra.append(m)
                    have_slugs.add(m.slug)
                if len(kept) + len(extra) >= comfortable + 50:  # small buffer, then dedup
                    break
            fetched += len(extra)
            topped_up = len(extra)
            kept, removed = dedup(records + extra)

        stats = {"fetched": fetched, "duplicates_removed": removed,
                 "topped_up": topped_up, "after_dedup": len(kept)}
        log.info("yc collect done", extra={"extra": stats})
        return kept, stats
