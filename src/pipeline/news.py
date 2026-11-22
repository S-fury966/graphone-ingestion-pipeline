"""NEWS ingestion pipeline (Phase II signal ingestion).

For each enabled NEWS source: fetch the feed/API, parse items, derive a
PUBLICATION date deterministically (feed field -> article JSON-LD/meta/URL ->
relative text), decide freshness with the pure-Python engine, crawl full text,
validate a :class:`News` record, and store it with the freshness columns.

Hard rules honored here:
  * The LLM NEVER decides freshness and NEVER supplies a date. Dates come only
    from ``src.common.dates`` / ``src.crawler.article_dates``; freshness from
    ``src.common.freshness``. The optional summary step (off by default) is the
    only LLM use, and it only summarizes already-scraped text.
  * Snapshots (the "new since last run" high-water mark) are saved only after a
    fully successful fetch+parse; a failed/partial fetch never overwrites them.

Run: python -m src.pipeline.news [--limit N] [--window-hours 24] [--dry-run]
     [--db PATH] [--summarize --max-llm-calls M]
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup
from pydantic import ValidationError

from ..common.config import SourceConfig, load_sources
from ..common.dates import check_clock_skew, parse_date, utc_now
from ..common.freshness import (Snapshot, content_hash, decide,
                                snapshot_continuity, url_hash)
from ..common.logging import get_logger
from ..crawler.article_dates import extract_publication_date
from ..crawler.fetcher import AsyncFetcher
from ..schemas import News
from ..storage.db import Database

log = get_logger("pipeline.news")

CSV_PATH = "data/news.csv"
JSONL_PATH = "data/news.jsonl"

# Extra, non-schema columns stored in SQLite and appended to the exports.
EXTRA_COLS = ("freshness_status", "date_source")

_NS = {"atom": "http://www.w3.org/2005/Atom",
       "dc": "http://purl.org/dc/elements/1.1/",
       "content": "http://purl.org/rss/1.0/modules/content/"}

# AI relevance: whole-word / phrase match (so "email" never matches "ai").
_AI_TERMS = [
    "artificial intelligence", "machine learning", "deep learning",
    "neural network", "large language model", "generative ai", "chatbot",
    "ai", "llm", "llms", "gpt", "genai", "transformer", "openai", "anthropic",
    "gemini", "deepmind", "hugging face", "nvidia", "inference", "agentic",
    "ai agent", "ai agents", "copilot", "diffusion model",
]
_AI_RE = re.compile(r"\b(" + "|".join(re.escape(t) for t in _AI_TERMS) + r")\b",
                    re.IGNORECASE)


def is_ai_relevant(text: str) -> bool:
    return bool(_AI_RE.search(text or ""))


# ---------------------------------------------------------------------------
# Feed / API parsing (no feedparser dependency; stdlib + bs4)
# ---------------------------------------------------------------------------
@dataclass
class FeedItem:
    title: str
    link: str
    published_raw: Optional[str]   # publication field only
    updated_raw: Optional[str]     # modified field (used only if updated_is_published)
    author: Optional[str]
    body_html: str                 # feed-provided body (may be empty/excerpt)


def strip_html(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for bad in soup(["script", "style", "noscript"]):
        bad.decompose()
    return soup.get_text(" ", strip=True)


def parse_rss_atom(text: str) -> List[FeedItem]:
    root = ET.fromstring(text)
    items: List[FeedItem] = []
    rss_items = root.findall(".//item")
    if rss_items:  # RSS 2.0
        for it in rss_items:
            link = (it.findtext("link") or "").strip()
            body = (it.findtext("content:encoded", namespaces=_NS)
                    or it.findtext("description") or "")
            items.append(FeedItem(
                title=(it.findtext("title") or "").strip(),
                link=link,
                published_raw=(it.findtext("pubDate")
                               or it.findtext("dc:date", namespaces=_NS)),
                updated_raw=None,
                author=(it.findtext("dc:creator", namespaces=_NS)
                        or it.findtext("author") or None),
                body_html=body))
    else:  # Atom
        for en in root.findall(".//atom:entry", _NS):
            link_el = en.find("atom:link", _NS)
            link = (link_el.get("href") if link_el is not None else "") or ""
            body = (en.findtext("atom:content", namespaces=_NS)
                    or en.findtext("atom:summary", namespaces=_NS) or "")
            items.append(FeedItem(
                title=(en.findtext("atom:title", namespaces=_NS) or "").strip(),
                link=link.strip(),
                published_raw=en.findtext("atom:published", namespaces=_NS),
                updated_raw=en.findtext("atom:updated", namespaces=_NS),
                author=en.findtext("atom:author/atom:name", namespaces=_NS) or None,
                body_html=body))
    return items


def parse_hn_algolia(text: str) -> List[FeedItem]:
    data = json.loads(text)
    items: List[FeedItem] = []
    for h in data.get("hits", []):
        link = h.get("url")  # external article; None for text posts
        if not link:
            continue
        items.append(FeedItem(
            title=(h.get("title") or "").strip(),
            link=link.strip(),
            published_raw=h.get("created_at"),
            updated_raw=None,
            author=None,  # HN submitter is not the article author
            body_html=""))
    return items


def parse_feed(source: SourceConfig, text: str) -> List[FeedItem]:
    if source.type == "api":
        return parse_hn_algolia(text)
    return parse_rss_atom(text)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
@dataclass
class Stats:
    by_source: Counter = field(default_factory=Counter)
    decisions: Counter = field(default_factory=Counter)
    date_sources: Counter = field(default_factory=Counter)
    ai_filtered: int = 0
    stored: int = 0
    invalid: int = 0
    fetch_errors: int = 0


def _news_dict(item: FeedItem, decision, full_text: str, now_iso: str,
               summary: Optional[str]) -> dict:
    return {
        "schemaVersion": "1.0",
        "recordType": "NEWS",
        "source": {"name": item._source_name, "url": item.link},
        "collectedAt": now_iso,
        "content": {
            "title": item.title,
            "published_date": decision.published_date.isoformat(),
            "full_text": full_text,
            "date_confidence": decision.date_confidence.value
            if hasattr(decision.date_confidence, "value") else decision.date_confidence,
            "author": item.author,
            "summary": summary,
        },
    }


async def process_source(db: Database, fetcher: AsyncFetcher, source: SourceConfig,
                         stats: Stats, now: datetime, window_hours: int,
                         clock_checked: dict, dry_run: bool = False) -> None:
    fetch_started = utc_now()  # snapshot time == when the fetch started
    res = await fetcher.fetch_text(str(source.base_url), respect_robots=True)

    # Clock-skew check against the first successful HTTP response of the run.
    if res.ok and not clock_checked["done"]:
        date_hdr = {k.lower(): v for k, v in (res.headers or {}).items()}.get("date")
        check_clock_skew(now, date_hdr)
        clock_checked["done"] = True

    if not res.ok or res.blocked or not res.text:
        stats.fetch_errors += 1
        log.warning("feed fetch failed; snapshot NOT updated", extra={"extra": {
            "source": source.name, "status": res.status, "blocked": res.blocked,
            "error": res.error}})
        return
    try:
        items = parse_feed(source, res.text)
    except (ET.ParseError, ValueError) as exc:
        stats.fetch_errors += 1
        log.warning("feed parse failed; snapshot NOT updated", extra={"extra": {
            "source": source.name, "reason": str(exc)}})
        return

    for it in items:  # stash source name for the record builder
        it._source_name = source.name  # type: ignore[attr-defined]

    current_hashes = {url_hash(it.link) for it in items if it.link}
    prev_raw = db.get_source_state(source.name)
    prev = Snapshot(
        last_snapshot_at=parse_date(prev_raw["last_snapshot_at"], now=now).dt
        if prev_raw and prev_raw.get("last_snapshot_at") else None,
        url_hashes=set(prev_raw["url_hashes"]) if prev_raw else set())
    continuity_ok = snapshot_continuity(prev, current_hashes)

    for it in items:
        if not it.link or not it.title:
            continue
        stats.by_source[source.name] += 1
        uh = url_hash(it.link)

        # 1) publication date from the feed (publication field only, unless the
        #    source explicitly allows the modified date to stand in).
        pub_raw = it.published_raw
        if not pub_raw and source.updated_is_published:
            pub_raw = it.updated_raw
        candidate = parse_date(pub_raw, now=now, source_tz=source.source_tz)
        if candidate is not None and candidate.source is None:
            candidate.source = "feed_published"

        # 2) full text (feed body first) + article crawl for text/date backfill.
        full_text = strip_html(it.body_html) if source.full_text_in_feed else ""
        need_date = not (candidate is not None and candidate.ok and candidate.verifiable)
        if source.article_crawl and (need_date or not full_text):
            art = await fetcher.fetch_text(it.link, respect_robots=True)
            if art.ok and not art.blocked and art.text:
                if not full_text:
                    full_text = _article_text(art.text)
                if need_date:
                    hit = extract_publication_date(art.text, it.link, now=now)
                    if hit is not None:
                        candidate, candidate.source = hit[0], hit[1]
            else:
                log.info("article crawl skipped/blocked", extra={"extra": {
                    "source": source.name, "url": it.link, "status": art.status,
                    "blocked": art.blocked}})
        if not full_text:
            full_text = strip_html(it.body_html) or it.title

        # 3) AI relevance filter (general-tech / discovery sources).
        if source.ai_filter and not is_ai_relevant(f"{it.title} {full_text}"):
            stats.ai_filtered += 1
            continue

        # 4) freshness decision (pure Python; LLM never involved).
        chash = content_hash(full_text)
        existing_first_seen = db.get_first_seen(uh)
        first_seen_at = (parse_date(existing_first_seen, now=now).dt
                         if existing_first_seen else now)
        # "Seen before" includes the previous COMPLETE snapshot's URLs: an
        # undated item carried over from the baseline listing is not new, even
        # though the baseline emitted nothing (so it's not in the seen ledger).
        seen_url_before = db.is_seen_url(uh) or (uh in prev.url_hashes)
        decision = decide(
            candidate=candidate, url_hash_=uh, content_hash_=chash,
            seen_url_before=seen_url_before,
            seen_content_before=db.is_seen_content(chash),
            first_seen_at=first_seen_at, prev_snapshot=prev,
            continuity_ok=continuity_ok, now=now, window_hours=window_hours)
        stats.decisions[decision.status] += 1

        if not decision.accepted:
            continue
        stats.date_sources[decision.date_source] += 1
        if db.news_exists(uh):
            continue

        now_iso = now.isoformat()
        # summary stays None: a free paraphrase can't be verbatim-grounded, and
        # news dates/text are fully deterministic, so the LLM is not used here
        # (deterministic-first hard rule). The field remains schema-optional.
        summary = None
        record = _news_dict(it, decision, full_text, now_iso, summary)
        try:
            News.model_validate(record)
        except ValidationError as exc:
            stats.invalid += 1
            log.warning("invalid NEWS record skipped", extra={"extra": {
                "source": source.name, "url": it.link, "reason": str(exc)[:200]}})
            continue

        if dry_run:
            stats.stored += 1
            continue
        row = {
            "url_hash": uh, "url": it.link, "title": it.title, "author": it.author,
            "published_date": decision.published_date.isoformat(),
            "date_confidence": record["content"]["date_confidence"],
            "freshness_status": decision.freshness_status,
            "date_source": decision.date_source, "full_text": full_text,
            "summary": summary, "content_hash": chash,
            "source_name": source.name, "source_url": it.link,
            "collected_at": now_iso, "first_seen_at": first_seen_at.isoformat()}
        if db.insert_news(row):
            db.mark_seen(uh, chash, source.name, first_seen_at.isoformat())
            stats.stored += 1

    # 5) snapshot saved ONLY after a fully successful fetch+parse.
    if not dry_run:
        db.save_source_snapshot(source.name, fetch_started.isoformat(),
                                sorted(current_hashes))
    log.info("source done", extra={"extra": {
        "source": source.name, "items": stats.by_source[source.name],
        "continuity_ok": continuity_ok, "had_prev_snapshot": prev.exists}})


def _article_text(html: str) -> str:
    """Visible text of an article page, preferring the <article> element."""
    soup = BeautifulSoup(html, "lxml")
    for bad in soup(["script", "style", "noscript", "nav", "header", "footer"]):
        bad.decompose()
    node = soup.find("article") or soup.body or soup
    return node.get_text(" ", strip=True)


# ---------------------------------------------------------------------------
# Export (News record fields + the two freshness columns)
# ---------------------------------------------------------------------------
def export(db: Database) -> int:
    rows = list(db.iter_news())
    Path("data").mkdir(parents=True, exist_ok=True)
    jsonl_lines: List[str] = []
    flats: List[dict] = []
    for r in rows:
        record = {
            "schemaVersion": "1.0", "recordType": "NEWS",
            "source": {"name": r["source_name"], "url": r["source_url"]},
            "collectedAt": r["collected_at"],
            "content": {
                "title": r["title"], "published_date": r["published_date"],
                "full_text": r["full_text"], "date_confidence": r["date_confidence"],
                "author": r["author"], "summary": r["summary"]},
        }
        model = News.model_validate(record)
        obj = json.loads(model.to_json())
        obj["freshness_status"] = r["freshness_status"]
        obj["date_source"] = r["date_source"]
        jsonl_lines.append(json.dumps(obj))
        flat = model.to_flat_dict()
        flat["freshness_status"] = r["freshness_status"]
        flat["date_source"] = r["date_source"]
        flats.append(flat)

    with open(JSONL_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(jsonl_lines) + ("\n" if jsonl_lines else ""))
    fields_order: List[str] = []
    for f in flats:
        for k in f:
            if k not in fields_order:
                fields_order.append(k)
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields_order)
        w.writeheader()
        w.writerows(flats)
    log.info("exported news", extra={"extra": {
        "rows": len(rows), "csv": CSV_PATH, "jsonl": JSONL_PATH}})
    return len(rows)


async def run(args: argparse.Namespace) -> None:
    now = utc_now()
    log.info("RUN now (UTC)", extra={"extra": {"now": now.isoformat(),
             "window_hours": args.window_hours}})
    db = Database(args.db)
    sources = [s for s in load_sources().enabled_sources()
               if "NEWS" in s.record_types]
    if args.limit:
        sources = sources[: args.limit]

    stats = Stats()
    clock_checked = {"done": False}
    fetcher = AsyncFetcher(global_concurrency=4, domain_concurrency=1,
                           rate_limit_per_sec=1.0, respect_robots=True)
    async with fetcher:
        for src in sources:
            try:
                await process_source(db, fetcher, src, stats, now,
                                     args.window_hours, clock_checked,
                                     dry_run=args.dry_run)
            except Exception as exc:  # noqa: BLE001 - one source must not kill the run
                stats.fetch_errors += 1
                log.warning("source crashed", extra={"extra": {
                    "source": src.name, "reason": repr(exc)}})

    if not args.dry_run:
        export(db)

    log.info("NEWS RUN SUMMARY", extra={"extra": {
        "now": now.isoformat(),
        "sources": [s.name for s in sources],
        "items_by_source": dict(stats.by_source),
        "decisions": dict(stats.decisions),
        "accepted_date_sources": dict(stats.date_sources),
        "ai_filtered_out": stats.ai_filtered,
        "stored": stats.stored,
        "invalid": stats.invalid,
        "fetch_errors": stats.fetch_errors,
        "llm_calls": 0,  # news is fully deterministic; no LLM in this pipeline
        "total_news_in_db": db.count_news()}})
    db.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="NEWS ingestion pipeline (Phase II)")
    p.add_argument("--limit", type=int, default=None, help="max sources to process")
    p.add_argument("--window-hours", type=int, default=24, help="freshness window")
    p.add_argument("--db", default="data/state.db", help="SQLite path")
    p.add_argument("--dry-run", action="store_true", help="do not persist/export")
    return p


def main() -> None:
    asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
