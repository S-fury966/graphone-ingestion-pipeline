"""JOBS ingestion pipeline (Phase II signal ingestion, Step 12).

Reuses the date engine, freshness engine, snapshot method, seen tables and the
ingest/export pattern from the news pipeline. Deterministic first: dates,
freshness, is_remote and role_family are pure Python. The ONLY LLM use is
extracting fields from Hacker News "Who is hiring" free-text comments, and even
there the LLM never sets the date or the freshness status.

Run: python -m src.pipeline.jobs [--window-hours 24] [--sources a,b]
     [--max-items-per-source N] [--skip-llm-sources] [--max-llm-calls N]
     [--db PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, ValidationError

from ..common.ai_keywords import is_ai_relevant
from ..common.config import SourceConfig, load_sources
from ..common.dates import (ParsedDate, check_clock_skew, is_fresh, parse_date,
                            utc_now)
from ..common.freshness import (INFERRED_FRESH, STALE, UNDATED_BASELINE,
                                VERIFIED_FRESH, Snapshot, content_hash, decide,
                                snapshot_continuity, url_hash)
from ..common.logging import get_logger
from ..crawler.fetcher import AsyncFetcher
from ..crawler.jobs import (JobItem, classify_role, decide_remote,
                            load_board_slugs, load_role_table)
from ..crawler.jobs import arbeitnow, ashby, greenhouse, hn_hiring, remoteok
from ..schemas import Job
from ..storage.db import Database

log = get_logger("pipeline.jobs")

CSV_PATH = "data/jobs.csv"
JSONL_PATH = "data/jobs.jsonl"
REJECTED_PATH = "data/jobs_rejected.csv"

# Stable reason codes (shared with news) + UNGROUNDED for the LLM path.
R_STALE, R_UNPROVABLE, R_BASELINE = STALE, "FRESHNESS_UNPROVABLE", UNDATED_BASELINE
R_NON_AI, R_NO_TEXT, R_DUP = "NON_AI", "NO_TEXT", "DUPLICATE"
R_BAD_DATE, R_FUTURE, R_ROBOTS, R_HTTP = "BAD_DATE", "FUTURE_DATE", "ROBOTS", "HTTP_ERROR"
R_UNGROUNDED = "UNGROUNDED"


@dataclass
class Stats:
    seen: Counter = field(default_factory=Counter)
    verified: Counter = field(default_factory=Counter)
    inferred: Counter = field(default_factory=Counter)
    rejected: Counter = field(default_factory=Counter)
    stored: int = 0


@dataclass
class Reject:
    url: str
    source: str
    reason: str
    date_source: str = ""
    raw_date: str = ""
    detail: str = ""


# ---------------------------------------------------------------------------
# LLM schema for HN comment extraction (grounded; never sets date/freshness)
# ---------------------------------------------------------------------------
class HNJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: Optional[str] = None
    title: Optional[str] = None
    is_remote: Optional[bool] = None
    role_family: Optional[str] = None


HN_INSTRUCTIONS = (
    "The TEXT is a single 'Who is hiring?' comment. Extract the hiring company, "
    "the FIRST job title/role mentioned, whether the role is remote, and a role "
    "family. Use ONLY the TEXT. For every non-null field add a verbatim quote to "
    "_evidence. If the company or the role is not clearly stated, set it to null."
)
_FAMILIES = {"Engineering", "Research", "Data", "Product", "Design", "Sales",
             "Marketing", "Operations", "Customer Support",
             "People and Recruiting", "Finance and Legal", "Other"}


@dataclass
class Fields:
    company: str
    title: str
    is_remote: bool
    remote_basis: str
    role_family: str
    role_basis: str
    llm_tier: Optional[str] = None
    reject: Optional[str] = None
    detail: str = ""


def _deterministic_fields(item: JobItem, role_table) -> Fields:
    is_remote, rbasis = decide_remote(item.remote_flag, item.location, item.title,
                                      item.tags)
    role_family, role_basis = classify_role(item.department, item.title, role_table)
    return Fields(company=item.company_raw, title=item.title, is_remote=is_remote,
                  remote_basis=rbasis, role_family=role_family, role_basis=role_basis)


async def _hn_fields(item: JobItem, orch, role_table) -> Fields:
    """LLM-extract company/title/is_remote/role_family from a HN comment."""
    if not item.comment_text:
        return Fields("", "", False, "default_false", "Other", "default_other",
                      reject=R_NO_TEXT)
    res = await orch.extract(f"hn-job:{url_hash(item.url)}", HNJob,
                             item.comment_text, HN_INSTRUCTIONS)
    if not res.success or not res.data:
        return Fields("", "", False, "default_false", "Other", "default_other",
                      reject=R_NO_TEXT, detail=res.error or "extract failed")
    data = res.data
    company = (data.get("company") or "").strip()
    title = (data.get("title") or "").strip()
    if not company or not title:
        # company/title evidence failed grounding (nulled) or absent.
        return Fields(company, title, False, "default_false", "Other",
                      "default_other", reject=R_UNGROUNDED,
                      detail="company/title not grounded")
    # is_remote: use the grounded LLM bool if present, else read the comment text.
    if data.get("is_remote") is True:
        is_remote, rbasis = True, "text_match"
    else:
        is_remote, rbasis = decide_remote(None, "", title, [item.comment_text])
    # role_family: trust a valid grounded family, else classify from the title.
    fam = (data.get("role_family") or "").strip()
    if fam in _FAMILIES and fam != "Other":
        role_family, role_basis = fam, "title_match"
    else:
        role_family, role_basis = classify_role(None, title, role_table)
    return Fields(company, title, is_remote, rbasis, role_family, role_basis,
                  llm_tier=res.tier_used)


def _job_record_dict(source: SourceConfig, item: JobItem, f: Fields,
                     decision, now_iso: str) -> dict:
    return {
        "schemaVersion": "1.0", "recordType": "JOB",
        "source": {"name": source.name, "url": item.url},
        "collectedAt": now_iso,
        "content": {"company": f.company, "title": f.title,
                    "date": decision.published_date.isoformat(),
                    "is_remote": f.is_remote, "role_family": f.role_family},
    }


async def _process_item(db: Database, source: SourceConfig, item: JobItem,
                        snapshot_key: str, prev: Snapshot, continuity_ok: bool,
                        now: datetime, window: int, stats: Stats,
                        rejects: List[Reject], orch, role_table,
                        dry_run: bool) -> None:
    stats.seen[source.name] += 1
    uh = url_hash(item.url)

    # Processed once, ever.
    if db.job_exists(uh):
        stats.rejected[R_DUP] += 1
        rejects.append(Reject(item.url, source.name, R_DUP, item.date_field or "",
                              item.raw_date or ""))
        return

    # AI relevance filter (Arbeitnow / RemoteOK only).
    if source.ai_filter:
        hay = f"{item.title} {' '.join(item.tags)}"
        if not is_ai_relevant(hay):
            stats.rejected[R_NON_AI] += 1
            rejects.append(Reject(item.url, source.name, R_NON_AI,
                                  item.date_field or "", item.raw_date or ""))
            return

    # Field extraction (deterministic, or LLM for HN).
    if source.uses_llm:
        f = await _hn_fields(item, orch, role_table)
        if f.reject:
            stats.rejected[f.reject] += 1
            rejects.append(Reject(item.url, source.name, f.reject,
                                  item.date_field or "", item.raw_date or "", f.detail))
            return
    else:
        f = _deterministic_fields(item, role_table)

    # Publication date (deterministic; LLM is never consulted here).
    candidate: Optional[ParsedDate] = None
    if item.raw_date:
        candidate = parse_date(item.raw_date, now=now, source_tz=source.source_tz)
        if candidate is None:
            stats.rejected[R_BAD_DATE] += 1
            rejects.append(Reject(item.url, source.name, R_BAD_DATE,
                                  item.date_field or "", item.raw_date, "unparseable"))
            return
        if candidate.status == "FUTURE_DATE":
            stats.rejected[R_FUTURE] += 1
            rejects.append(Reject(item.url, source.name, R_FUTURE,
                                  item.date_field or "", item.raw_date,
                                  candidate.dt.isoformat()))
            return
        candidate.source = item.date_field

    chash = content_hash(f"{item.url}|{f.title}|{f.company}")
    existing_first = db.get_first_seen(uh)
    first_seen_at = (parse_date(existing_first, now=now).dt if existing_first else now)
    seen_url_before = db.is_seen_url(uh) or (uh in prev.url_hashes)

    decision = decide(candidate=candidate, url_hash_=uh, content_hash_=chash,
                      seen_url_before=seen_url_before,
                      seen_content_before=db.is_seen_content(chash),
                      first_seen_at=first_seen_at, prev_snapshot=prev,
                      continuity_ok=continuity_ok, now=now, window_hours=window)

    if not decision.accepted:
        stats.rejected[decision.status] += 1
        rejects.append(Reject(item.url, source.name, decision.status,
                              item.date_field or "snapshot", item.raw_date or "",
                              decision.evidence or ""))
        return

    if decision.freshness_status == VERIFIED_FRESH:
        stats.verified[source.name] += 1
    else:
        stats.inferred[source.name] += 1

    now_iso = now.isoformat()
    record = _job_record_dict(source, item, f, decision, now_iso)
    try:
        Job.model_validate(record)
    except ValidationError as exc:
        stats.rejected["INVALID"] += 1
        rejects.append(Reject(item.url, source.name, "INVALID", item.date_field or "",
                              item.raw_date or "", str(exc)[:160]))
        return

    if dry_run:
        stats.stored += 1
        return
    row = {
        "url_hash": uh, "url": item.url, "source_name": source.name,
        "company_raw": item.company_raw or f.company, "title": f.title,
        "is_remote": 1 if f.is_remote else 0, "remote_basis": f.remote_basis,
        "role_family": f.role_family, "role_basis": f.role_basis,
        "published_date": decision.published_date.isoformat(),
        "date_confidence": decision.date_confidence.value,
        "freshness_status": decision.freshness_status,
        "date_source": decision.date_source, "content_hash": chash,
        "llm_tier": f.llm_tier, "collected_at": now_iso,
        "first_seen_at": first_seen_at.isoformat()}
    if db.insert_job(row):
        db.mark_seen(uh, chash, source.name, first_seen_at.isoformat())
        stats.stored += 1


async def _process_listing(db: Database, source: SourceConfig, items: List[JobItem],
                           snapshot_key: str, now: datetime, window: int,
                           stats: Stats, rejects: List[Reject], orch, role_table,
                           max_items: Optional[int], dry_run: bool,
                           fetch_started: datetime) -> None:
    """Freshness continuity, per-item processing, then save the snapshot."""
    current_hashes = {url_hash(it.url) for it in items if it.url}
    prev_raw = db.get_source_state(snapshot_key)
    prev = Snapshot(
        last_snapshot_at=parse_date(prev_raw["last_snapshot_at"], now=now).dt
        if prev_raw and prev_raw.get("last_snapshot_at") else None,
        url_hashes=set(prev_raw["url_hashes"]) if prev_raw else set())

    # Complete listings (ATS boards): a full successful fetch satisfies
    # continuity. Chronological feeds need the URL-overlap check.
    if source.listing_type == "complete":
        continuity_ok = True
    else:
        continuity_ok = snapshot_continuity(prev, current_hashes)

    to_process = items[:max_items] if max_items else items
    for it in to_process:
        if not it.url:
            continue
        await _process_item(db, source, it, snapshot_key, prev, continuity_ok,
                            now, window, stats, rejects, orch, role_table, dry_run)

    # Snapshot saved ONLY after a fully successful fetch+parse (we are here only
    # on success). Records the FULL listing, not just the capped slice.
    if not dry_run:
        db.save_source_snapshot(snapshot_key, fetch_started.isoformat(),
                                sorted(current_hashes))


async def _fetch_json(fetcher: AsyncFetcher, url: str):
    started = utc_now()
    res = await fetcher.fetch_text(url, respect_robots=True)
    return started, res


def _source_error(res, source_name, url, rejects, stats) -> None:
    reason = R_ROBOTS if (res.error and "robots" in res.error.lower()) else R_HTTP
    if res.blocked:
        reason = R_ROBOTS
    stats.rejected[reason] += 1
    rejects.append(Reject(url, source_name, reason, "", "",
                          f"status={res.status} error={res.error}"))
    log.warning("source fetch failed; snapshot not updated", extra={"extra": {
        "source": source_name, "url": url, "status": res.status,
        "blocked": res.blocked, "error": res.error}})


async def ingest_source(db: Database, fetcher: AsyncFetcher, source: SourceConfig,
                        now: datetime, window: int, stats: Stats,
                        rejects: List[Reject], orch, role_table,
                        max_items: Optional[int], clock: dict, dry_run: bool) -> None:
    name = source.name

    def _clock(res):
        if res.ok and not clock["done"]:
            hdr = {k.lower(): v for k, v in (res.headers or {}).items()}.get("date")
            clock["skew"] = check_clock_skew(now, hdr)
            clock["done"] = True

    if name in ("arbeitnow", "remoteok"):
        started, res = await _fetch_json(fetcher, str(source.base_url))
        _clock(res)
        if not res.ok or not res.text:
            return _source_error(res, name, str(source.base_url), rejects, stats)
        mod = arbeitnow if name == "arbeitnow" else remoteok
        items = mod.parse(res.text)
        await _process_listing(db, source, items, name, now, window, stats,
                               rejects, orch, role_table, max_items, dry_run, started)

    elif name in ("greenhouse", "ashby"):
        boards = load_board_slugs()[name]
        for b in boards:
            slug, company = b["slug"], b["company"]
            if name == "greenhouse":
                url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
            else:
                url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
            started, res = await _fetch_json(fetcher, url)
            _clock(res)
            if not res.ok or not res.text:
                _source_error(res, f"{name}:{slug}", url, rejects, stats)
                continue
            mod = greenhouse if name == "greenhouse" else ashby
            try:
                items = mod.parse(res.text, company)
            except (ValueError, KeyError) as exc:
                _source_error(res, f"{name}:{slug}", url, rejects, stats)
                continue
            await _process_listing(db, source, items, f"{name}:{slug}", now, window,
                                   stats, rejects, orch, role_table, max_items,
                                   dry_run, started)

    elif name == "hn-hiring":
        started, res = await _fetch_json(fetcher, hn_hiring.THREAD_SEARCH)
        _clock(res)
        if not res.ok or not res.text:
            return _source_error(res, name, hn_hiring.THREAD_SEARCH, rejects, stats)
        story_id = hn_hiring.parse_thread(res.text)
        if not story_id:
            return _source_error(res, name, hn_hiring.THREAD_SEARCH, rejects, stats)
        hits = max_items or 100
        curl = hn_hiring.comments_url(story_id, hits=hits)
        started2, res2 = await _fetch_json(fetcher, curl)
        if not res2.ok or not res2.text:
            return _source_error(res2, name, curl, rejects, stats)
        items = hn_hiring.parse_comments(res2.text, story_id)
        await _process_listing(db, source, items, name, now, window, stats,
                               rejects, orch, role_table, max_items, dry_run, started)
    else:
        log.warning("unknown job source; skipped", extra={"extra": {"source": name}})


# ---------------------------------------------------------------------------
# Export (freshness RE-CHECKED at the current time)
# ---------------------------------------------------------------------------
def export(db: Database, window_hours: int, now: Optional[datetime] = None) -> int:
    now = now or utc_now()
    Path("data").mkdir(parents=True, exist_ok=True)
    jsonl_lines: List[str] = []
    flats: List[dict] = []
    for r in db.iter_jobs():
        pub = parse_date(r["published_date"], now=now)
        if pub is None or not is_fresh(pub.dt, now, window_hours):
            continue  # freshness no longer holds at export time
        # content.company is the display/company name; company_raw is also
        # exported (the entity-resolution step maps it to canonical later).
        record = {
            "schemaVersion": "1.0", "recordType": "JOB",
            "source": {"name": r["source_name"], "url": r["url"]},
            "collectedAt": r["collected_at"],
            "content": {"company": r["company_raw"], "title": r["title"],
                        "date": r["published_date"],
                        "is_remote": bool(r["is_remote"]),
                        "role_family": r["role_family"]},
        }
        model = Job.model_validate(record)
        obj = json.loads(model.to_json())
        extra = {"freshness_status": r["freshness_status"], "date_source": r["date_source"],
                 "remote_basis": r["remote_basis"], "role_basis": r["role_basis"],
                 "company_raw": r["company_raw"]}
        obj.update(extra)
        jsonl_lines.append(json.dumps(obj))
        flat = model.to_flat_dict()
        flat.update(extra)
        flats.append(flat)

    with open(JSONL_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(jsonl_lines) + ("\n" if jsonl_lines else ""))
    order: List[str] = []
    for f in flats:
        for k in f:
            if k not in order:
                order.append(k)
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=order)
        w.writeheader()
        w.writerows(flats)
    log.info("exported jobs", extra={"extra": {"rows": len(flats), "csv": CSV_PATH}})
    return len(flats)


def write_rejected(rejects: List[Reject]) -> None:
    Path("data").mkdir(parents=True, exist_ok=True)
    with open(REJECTED_PATH, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["url", "source", "reason", "date_source", "raw_date", "detail"])
        for r in rejects:
            w.writerow([r.url, r.source, r.reason, r.date_source, r.raw_date, r.detail])


async def run(args: argparse.Namespace) -> None:
    now = utc_now()
    log.info("RUN now (UTC)", extra={"extra": {
        "now": now.isoformat(), "window_hours": args.window_hours,
        "skip_llm_sources": args.skip_llm_sources}})
    db = Database(args.db)
    role_table = load_role_table()

    sources = [s for s in load_sources().enabled_sources() if "JOB" in s.record_types]
    if args.sources:
        wanted = {x.strip() for x in args.sources.split(",")}
        sources = [s for s in sources if s.name in wanted]
    if args.skip_llm_sources:
        sources = [s for s in sources if not s.uses_llm]

    orch = None
    if any(s.uses_llm for s in sources):
        from ..llm.config import load_llm_config
        from ..llm.orchestrator import Orchestrator
        orch = Orchestrator(load_llm_config(), fetcher=None, db=db,
                            max_calls=args.max_llm_calls)

    stats = Stats()
    rejects: List[Reject] = []
    clock = {"done": False, "skew": None}
    fetcher = AsyncFetcher(global_concurrency=max(4, args.concurrency),
                           domain_concurrency=4, rate_limit_per_sec=3.0,
                           respect_robots=True)
    async with fetcher:
        for src in sources:
            try:
                await ingest_source(db, fetcher, src, now, args.window_hours, stats,
                                    rejects, orch, role_table,
                                    args.max_items_per_source, clock, args.dry_run)
            except Exception as exc:  # noqa: BLE001 - one source must not kill the run
                stats.rejected[R_HTTP] += 1
                log.warning("source crashed", extra={"extra": {
                    "source": src.name, "reason": repr(exc)}})

    exported = 0
    if not args.dry_run:
        exported = export(db, args.window_hours)
        write_rejected(rejects)

    log.info("JOBS RUN SUMMARY", extra={"extra": {
        "now": now.isoformat(),
        "clock_skew_seconds": clock["skew"],
        "sources": [s.name for s in sources],
        "seen_by_source": dict(stats.seen),
        "verified_fresh": dict(stats.verified),
        "inferred_fresh": dict(stats.inferred),
        "rejected_by_code": dict(stats.rejected),
        "stored_this_run": stats.stored,
        "exported_fresh_now": exported,
        "llm_calls": orch.calls_made if orch else 0,
        "total_jobs_in_db": db.count_jobs()}})
    db.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="JOBS ingestion pipeline (Phase II)")
    p.add_argument("--window-hours", type=int, default=24)
    p.add_argument("--sources", type=str, default=None,
                   help="comma-separated source names to include")
    p.add_argument("--max-items-per-source", type=int, default=None,
                   help="cap items processed per source/board listing")
    p.add_argument("--skip-llm-sources", action="store_true",
                   help="exclude sources that need the LLM (HN 'Who is hiring')")
    p.add_argument("--max-llm-calls", type=int, default=30)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--db", default="data/state_news.db",
                   help="SQLite path (never data/state.db while another run uses it)")
    p.add_argument("--dry-run", action="store_true")
    return p


def main() -> None:
    asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
