"""Startups pipeline: collect YC companies, dedup, validate, store + export.

Run:  python -m src.pipeline.startups --target 1000 [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List

from pydantic import ValidationError

from ..common.logging import get_logger
from ..crawler.fetcher import AsyncFetcher
from ..crawler.yc_startups import StartupRecord, YCCollector
from ..schemas import Startup
from ..storage.db import Database

log = get_logger("pipeline.startups")

CSV_PATH = "data/startups.csv"
JSONL_PATH = "data/startups.jsonl"


@dataclass
class Stats:
    fetched: int = 0
    duplicates_removed: int = 0
    invalid: int = 0
    with_employee_count: int = 0
    inserted: int = 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_model_dict(rec: StartupRecord, collected_at: str) -> dict[str, Any]:
    return {
        "schemaVersion": "1.0",
        "recordType": "STARTUP",
        "source": {"name": rec.source_name, "url": rec.source_url},
        "collectedAt": collected_at,
        "content": {
            "entityName": rec.entity_name,
            "data": {"employeeCount": rec.employee_count},
        },
    }


def _to_db_record(rec: StartupRecord, collected_at: str) -> dict[str, Any]:
    return {
        "slug": rec.slug,
        "normalized_name": rec.normalized_name or rec.slug,
        "entity_name": rec.entity_name,
        "employee_count": rec.employee_count,
        "website": rec.website,
        "domain": rec.domain,
        "one_liner": rec.one_liner,
        "batch": rec.batch,
        "tags": rec.tags,
        "source_name": rec.source_name,
        "source_url": rec.source_url,
        "collected_at": collected_at,
    }


def persist(db: Database, records: List[StartupRecord], stats: Stats,
            dry_run: bool = False) -> None:
    for rec in records:
        collected_at = _now_iso()
        try:
            Startup.model_validate(_to_model_dict(rec, collected_at))
        except ValidationError as exc:
            stats.invalid += 1
            log.warning("invalid startup skipped", extra={"extra": {
                "slug": rec.slug, "name": rec.entity_name, "reason": str(exc)}})
            continue
        if rec.employee_count is not None:
            stats.with_employee_count += 1
        if dry_run:
            stats.inserted += 1
        elif db.insert_startup(_to_db_record(rec, collected_at)):
            stats.inserted += 1


def _export(db: Database) -> int:
    models: List[Startup] = []
    for row in db.iter_startups():
        models.append(Startup.model_validate({
            "schemaVersion": "1.0",
            "recordType": "STARTUP",
            "source": {"name": row["source_name"], "url": row["source_url"]},
            "collectedAt": row["collected_at"],
            "content": {"entityName": row["entity_name"],
                        "data": {"employeeCount": row["employee_count"]}},
        }))

    Path("data").mkdir(parents=True, exist_ok=True)
    with open(JSONL_PATH, "w", encoding="utf-8") as fh:
        for m in models:
            fh.write(m.to_json() + "\n")

    flats = [m.to_flat_dict() for m in models]
    fieldnames: List[str] = []
    for f in flats:
        for k in f:
            if k not in fieldnames:
                fieldnames.append(k)
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(flats)
    log.info("exported", extra={"extra": {"rows": len(models),
             "csv": CSV_PATH, "jsonl": JSONL_PATH}})
    return len(models)


async def run(args: argparse.Namespace) -> None:
    db = Database(args.db)
    # Open-data JSON mirror; all.json is ~11 MB so raise the size cap above 5 MB.
    fetcher = AsyncFetcher(rate_limit_per_sec=5, domain_concurrency=2,
                           respect_robots=False, max_bytes=40 * 1024 * 1024)
    collector = YCCollector()
    stats = Stats()

    async with fetcher:
        records, cstats = await collector.collect(fetcher, args.target, args.margin)
    stats.fetched = cstats["fetched"]
    stats.duplicates_removed = cstats["duplicates_removed"]

    persist(db, records, stats, dry_run=args.dry_run)

    total = stats.inserted if args.dry_run else db.count_startups()
    if not args.dry_run:
        _export(db)

    log.info("RUN SUMMARY", extra={"extra": {
        "fetched": stats.fetched,
        "duplicates_removed": stats.duplicates_removed,
        "invalid": stats.invalid,
        "with_employee_count": stats.with_employee_count,
        "final_total": total,
    }})
    db.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Startups ingestion pipeline (YC)")
    p.add_argument("--target", type=int, default=1000, help="minimum startups to reach")
    p.add_argument("--margin", type=int, default=100, help="comfortable margin over target")
    p.add_argument("--dry-run", action="store_true", help="collect+validate but do not persist")
    p.add_argument("--db", default="data/state.db", help="SQLite path")
    return p


def main() -> None:
    asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
