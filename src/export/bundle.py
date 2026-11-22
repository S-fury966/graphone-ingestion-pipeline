"""Submission bundle: six CSV tabs (+ matching JSONL) for import into one Google
Sheet, with the entity resolver applied at export time.

    python -m src.export.bundle [--out submission]

Design notes:
- The resolver is applied at export time; stored raw values are untouched and
  kept in `*_raw` columns.
- Jobs and News apply the 24h freshness window at the moment the bundle runs.
- CSV text cells are capped at 45,000 chars (Google Sheets limit) with a
  " [truncated]" marker; the JSONL files keep the full text.
- If two different startups resolve to one canonical name, the bundle STOPS and
  lists the collisions (nothing is silently merged/dropped).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..common.dates import is_fresh, parse_date, utc_now
from ..common.logging import get_logger
from ..resolver.mapping_log import CSV_COLUMNS as ENTITY_COLS
from ..resolver.mapping_log import _ro
from ..storage.db import Database
from .resolve_map import (build_resolver, canonical_startup, live_entity_rows,
                          ordered_mapping_rows, startup_collisions)

log = get_logger("export.bundle")

CELL_CAP = 45_000
TRUNC = " [truncated]"
WINDOW_HOURS = 24

TAB_FILES = {
    "startups": "Startups",
    "products": "Products",
    "papers": "Research Papers",
    "jobs": "Jobs",
    "news": "News",
    "entity": "Entity Mapping Log",
}

STARTUP_COLS = ["schemaVersion", "recordType", "source.name", "source.url",
                "collectedAt", "content.entityName",
                "content.data.employeeCount", "raw_name"]
PRODUCT_COLS = ["schemaVersion", "recordType", "source.name", "source.url",
                "collectedAt", "content.startupName", "content.name",
                "content.pricingModel", "startup_raw", "evidence_name",
                "evidence_pricing"]
PAPER_COLS = ["schemaVersion", "recordType", "source.name", "source.url",
              "collectedAt", "content.title", "content.authors",
              "content.paper_url", "content.published_date", "content.arxiv_id",
              "content.github_url", "content.github_stars"]
JOB_COLS = ["schemaVersion", "recordType", "source.name", "source.url",
            "collectedAt", "content.company", "content.title", "content.date",
            "content.is_remote", "content.role_family", "company_raw",
            "freshness_status", "date_source", "remote_basis", "role_basis"]
NEWS_COLS = ["schemaVersion", "recordType", "source.name", "source.url",
             "collectedAt", "content.title", "content.published_date",
             "content.full_text", "content.date_confidence", "content.author",
             "content.summary", "freshness_status", "date_source"]


# --------------------------------------------------------------------------- #
# cell formatting + file writing
# --------------------------------------------------------------------------- #
def _fmt_cell(v, col: str, trunc: Dict[str, int]) -> str:
    """Render one CSV cell: None->"", bool->TRUE/FALSE, cap long text."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    s = v if isinstance(v, str) else str(v)
    if len(s) > CELL_CAP:
        s = s[:CELL_CAP] + TRUNC
        trunc[col] = trunc.get(col, 0) + 1
    return s


def write_tab(out_dir: Path, tab: str, columns: List[str],
              flats: List[dict], jsonl_objs: List[dict],
              trunc: Dict[str, int]) -> Tuple[int, int]:
    """Write <tab>.csv (capped) and <tab>.jsonl (full). Returns (rows, bytes)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{tab}.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(columns)
        for row in flats:
            w.writerow([_fmt_cell(row.get(c), c, trunc) for c in columns])
    jsonl_path = out_dir / f"{tab}.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for obj in jsonl_objs:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    return len(flats), csv_path.stat().st_size


# --------------------------------------------------------------------------- #
# per-tab row builders (apply resolver + extras)
# --------------------------------------------------------------------------- #
def build_startups(conn, resolver) -> Tuple[List[dict], List[dict]]:
    from ..schemas import Startup
    flats, objs = [], []
    for r in conn.execute("SELECT * FROM startups ORDER BY entity_name"):
        raw = r["entity_name"]
        canon = canonical_startup(resolver, raw)
        model = Startup.model_validate({
            "schemaVersion": "1.0", "recordType": "STARTUP",
            "source": {"name": r["source_name"], "url": r["source_url"]},
            "collectedAt": r["collected_at"],
            "content": {"entityName": canon,
                        "data": {"employeeCount": r["employee_count"]}}})
        flat = model.to_flat_dict(); flat["raw_name"] = raw
        obj = json.loads(model.to_json()); obj["raw_name"] = raw
        flats.append(flat); objs.append(obj)
    return flats, objs


def build_products(conn, resolver) -> Tuple[List[dict], List[dict]]:
    from ..schemas import Product
    flats, objs = [], []
    for r in conn.execute("SELECT * FROM products ORDER BY startup_name, product_name"):
        raw_startup = r["startup_name"]
        canon = canonical_startup(resolver, raw_startup)
        model = Product.model_validate({
            "schemaVersion": "1.0", "recordType": "PRODUCT",
            "source": {"name": r["source_name"], "url": r["page_url"]},
            "collectedAt": r["collected_at"],
            "content": {"startupName": canon, "name": r["product_name"],
                        "pricingModel": r["pricing_model"]}})
        extra = {"startup_raw": raw_startup,
                 "evidence_name": r["evidence_name"],
                 "evidence_pricing": r["evidence_pricing"]}
        flat = model.to_flat_dict(); flat.update(extra)
        obj = json.loads(model.to_json()); obj.update(extra)
        flats.append(flat); objs.append(obj)
    return flats, objs


def build_papers(conn) -> Tuple[List[dict], List[dict]]:
    from ..schemas import ResearchPaper
    flats, objs = [], []
    for r in conn.execute("SELECT * FROM papers ORDER BY published DESC"):
        authors = json.loads(r["authors"])
        model = ResearchPaper.model_validate({
            "schemaVersion": "1.0", "recordType": "RESEARCH_PAPER",
            "source": {"name": r["source_name"], "url": r["source_url"]},
            "collectedAt": r["collected_at"],
            "content": {"title": r["title"], "authors": authors,
                        "paper_url": r["abs_url"], "published_date": r["published"],
                        "arxiv_id": r["arxiv_id"], "github_url": r["github_url"],
                        "github_stars": r["github_stars"]}})
        flat = model.to_flat_dict()
        flat["content.authors"] = "; ".join(authors)      # task: join with "; "
        objs.append(json.loads(model.to_json()))
        flats.append(flat)
    return flats, objs


def _fresh(pub_str: str, now: datetime) -> bool:
    parsed = parse_date(pub_str, now=now)
    return parsed is not None and is_fresh(parsed, now, WINDOW_HOURS)


def build_jobs(conn, resolver, now: datetime) -> Tuple[List[dict], List[dict]]:
    from ..schemas import Job
    flats, objs = [], []
    for r in conn.execute("SELECT * FROM jobs ORDER BY published_date DESC"):
        if not _fresh(r["published_date"], now):
            continue  # freshness window enforced at export time
        canon = canonical_startup(resolver, r["company_raw"])
        model = Job.model_validate({
            "schemaVersion": "1.0", "recordType": "JOB",
            "source": {"name": r["source_name"], "url": r["url"]},
            "collectedAt": r["collected_at"],
            "content": {"company": canon, "title": r["title"],
                        "date": r["published_date"], "is_remote": bool(r["is_remote"]),
                        "role_family": r["role_family"]}})
        extra = {"company_raw": r["company_raw"],
                 "freshness_status": r["freshness_status"],
                 "date_source": r["date_source"], "remote_basis": r["remote_basis"],
                 "role_basis": r["role_basis"]}
        flat = model.to_flat_dict(); flat.update(extra)
        obj = json.loads(model.to_json()); obj.update(extra)
        flats.append(flat); objs.append(obj)
    return flats, objs


def build_news(conn, now: datetime) -> Tuple[List[dict], List[dict]]:
    from ..schemas import News
    flats, objs = [], []
    for r in conn.execute("SELECT * FROM news ORDER BY published_date DESC"):
        if not _fresh(r["published_date"], now):
            continue
        model = News.model_validate({
            "schemaVersion": "1.0", "recordType": "NEWS",
            "source": {"name": r["source_name"], "url": r["source_url"]},
            "collectedAt": r["collected_at"],
            "content": {"title": r["title"], "published_date": r["published_date"],
                        "full_text": r["full_text"], "date_confidence": r["date_confidence"],
                        "author": r["author"], "summary": r["summary"]}})
        extra = {"freshness_status": r["freshness_status"], "date_source": r["date_source"]}
        flat = model.to_flat_dict(); flat.update(extra)
        obj = json.loads(model.to_json()); obj.update(extra)
        flats.append(flat); objs.append(obj)
    return flats, objs


def build_entity_log(conn, resolver, db: Database) -> Tuple[List[dict], List[dict]]:
    """Real mappings only (non-trivial then trivial self-matches); also persists
    the entity_map table. The synthetic eval corpus is NOT here — it lives in
    docs/entity_resolution_eval.{csv,md}."""
    now = datetime.now(timezone.utc).isoformat()
    live = live_entity_rows(conn, resolver, now=now)
    from ..resolver.mapping_log import persist
    persist(db, live)
    ordered = ordered_mapping_rows(live)
    flats = [{k: r.get(k) for k in ENTITY_COLS} for r in ordered]
    return flats, flats  # entity log JSONL == same flat rows


# --------------------------------------------------------------------------- #
# signal ingest (optional, for the final run)
# --------------------------------------------------------------------------- #
async def _run_signals(db_path: str, with_llm: bool, max_llm_calls: int) -> None:
    from ..pipeline import jobs as jobs_pipe
    from ..pipeline import news as news_pipe
    news_args = argparse.Namespace(limit=None, window_hours=WINDOW_HOURS,
                                   max_items_per_source=40, db=db_path, dry_run=False)
    await news_pipe.run(news_args)
    jobs_args = argparse.Namespace(window_hours=WINDOW_HOURS, sources=None,
                                   max_items_per_source=None,
                                   skip_llm_sources=not with_llm,
                                   max_llm_calls=max_llm_calls, concurrency=4,
                                   db=db_path, dry_run=False)
    await jobs_pipe.run(jobs_args)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def write_import_md(out_dir: Path) -> None:
    tabs = ["Startups", "Products", "Research Papers", "Jobs", "News",
            "Entity Mapping Log"]
    lines = [
        "# Importing the submission into Google Sheets",
        "",
        "Six CSV files in this folder become the six tabs of ONE Google Sheet.",
        "",
        "## Steps (repeat for each CSV)",
        "1. Create a new, empty Google Sheet (or open the one you are building).",
        "2. **File → Import**.",
        "3. **Upload** and choose the CSV file.",
        "4. Import location: **Insert new sheet(s)**.",
        '5. Separator type: **Detect automatically** (or Comma).',
        '6. **UNCHECK "Convert text to numbers, dates and formulas"** — this keeps',
        "   IDs, timestamps and leading zeros exactly as written.",
        "7. Click **Import data**.",
        "8. Rename the new tab to match the file name (see the exact names below).",
        "",
        "## Exact tab names (in this order)",
    ]
    for t in tabs:
        lines.append(f"- `{t}`  ← `{t}.csv`")
    lines += [
        "",
        "## Share the sheet",
        "1. Click **Share** (top right).",
        '2. Under **General access**, choose **Anyone with the link**.',
        '3. Set the role to **Viewer**.',
        "4. Click **Copy link** and submit that link.",
        "",
        "The matching `.jsonl` files hold the full, untruncated records (CSV text",
        "cells are capped at 45,000 characters for the Google Sheets cell limit).",
        "",
        "The **Entity Mapping Log** tab contains only real mappings derived from",
        "the live database (raw name → canonical). The resolver's synthetic test",
        "corpus (expected vs actual, with accuracy) is kept separately in",
        "`docs/entity_resolution_eval.csv` / `docs/entity_resolution_eval.md`.",
    ]
    (out_dir / "IMPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_bundle(out: str = "submission", db_path: str = "data/state.db",
               now: Optional[datetime] = None, run_signals: bool = False,
               with_llm_sources: bool = False, max_llm_calls: int = 30) -> int:
    now = now or utc_now()
    out_dir = Path(out)
    print(f"bundle now (UTC): {now.isoformat()}")

    if run_signals:
        print("--run-signals: ingesting news + jobs first ...")
        asyncio.run(_run_signals(db_path, with_llm_sources, max_llm_calls))

    db = Database(db_path)
    conn = _ro(db_path)
    resolver = build_resolver(conn)

    # Collision gate: never silently merge two startups into one canonical.
    collisions = startup_collisions(conn, resolver)
    if collisions:
        out_dir.mkdir(parents=True, exist_ok=True)
        _write_collision_report(out_dir, collisions, now)
        conn.close(); db.close()
        print(f"STOPPED: {len(collisions)} canonical collision(s); see "
              f"{out_dir/'VALIDATION.md'}. Nothing was merged or dropped.")
        return 2

    trunc: Dict[str, int] = {}
    s_flats, s_objs = build_startups(conn, resolver)
    p_flats, p_objs = build_products(conn, resolver)
    pa_flats, pa_objs = build_papers(conn)
    j_flats, j_objs = build_jobs(conn, resolver, now)
    n_flats, n_objs = build_news(conn, now)
    e_flats, e_objs = build_entity_log(conn, resolver, db)

    specs = [
        ("Startups", STARTUP_COLS, s_flats, s_objs),
        ("Products", PRODUCT_COLS, p_flats, p_objs),
        ("Research Papers", PAPER_COLS, pa_flats, pa_objs),
        ("Jobs", JOB_COLS, j_flats, j_objs),
        ("News", NEWS_COLS, n_flats, n_objs),
        ("Entity Mapping Log", ENTITY_COLS, e_flats, e_objs),
    ]
    file_info = []
    for tab, cols, flats, objs in specs:
        rows, size = write_tab(out_dir, tab, cols, flats, objs, trunc)
        file_info.append((tab, rows, size))

    # Also refresh the canonical entity_mapping_log.csv under data/ (real only).
    from .resolve_map import _write_ordered_csv
    _write_ordered_csv(ordered_mapping_rows(live_entity_rows(conn, resolver)),
                       "data/entity_mapping_log.csv")

    write_import_md(out_dir)
    conn.close(); db.close()

    print("\n=== files written ===")
    for tab, rows, size in file_info:
        print(f"  {tab+'.csv':26} rows={rows:<6} bytes={size}")
    if trunc:
        print("\n=== truncated cells (>45,000 chars) ===")
        for col, n in trunc.items():
            print(f"  {col}: {n}")
    else:
        print("\nno cells truncated")

    # Validation (also run standalone via python -m src.export.validate).
    from . import validate as validate_mod
    code = validate_mod.validate(out, db_path=db_path, now=now, truncated=trunc)
    return code


def _write_collision_report(out_dir: Path, collisions: List[dict],
                            now: datetime) -> None:
    lines = [f"# VALIDATION — STOPPED ({now.isoformat()})", "",
             f"**{len(collisions)} canonical collision(s): two or more different "
             "startups resolve to the same canonical name.** Nothing was merged "
             "or dropped; decide how to handle each before re-running.", ""]
    for c in collisions:
        lines.append(f"## canonical: `{c['canonical']}`")
        for raw, url in zip(c["raw_names"], c["source_urls"]):
            lines.append(f"- `{raw}`  ({url})")
        lines.append("")
    (out_dir / "VALIDATION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Write the submission CSV/JSONL bundle.")
    p.add_argument("--out", default="submission")
    p.add_argument("--db", default="data/state.db")
    p.add_argument("--run-signals", action="store_true",
                   help="ingest news + jobs first, then bundle (for the final run)")
    p.add_argument("--with-llm-sources", action="store_true",
                   help="with --run-signals, include the Hacker News jobs source (LLM)")
    p.add_argument("--max-llm-calls", type=int, default=30)
    return p


def main() -> int:
    args = build_parser().parse_args()
    return run_bundle(out=args.out, db_path=args.db, run_signals=args.run_signals,
                      with_llm_sources=args.with_llm_sources,
                      max_llm_calls=args.max_llm_calls)


if __name__ == "__main__":
    raise SystemExit(main())
