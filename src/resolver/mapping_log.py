"""Entity Mapping Log (Step 13).

Resolves every distinct company/startup/product name found in our own tables
(startups.entityName, products.startupName + product name, jobs.company_raw) to
a canonical name, writes one row per distinct raw string with an occurrence
count, and exports data/entity_mapping_log.csv (non-trivial mappings first).

The source tables are read ONLY; raw values are never modified. The real run
operates on a consistent COPY of the live state.db plus state_news.db (read
only), so a concurrently-writing pipeline is never touched.
"""
from __future__ import annotations

import argparse
import csv
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

from ..storage.db import Database
from .resolver import (EXACT, NEW_ENTITY, PRODUCT, STARTUP, TABLE, Resolver,
                       TableRow, compact_key)
from rapidfuzz import fuzz

EXACT_SELF = "EXACT_SELF"
CSV_COLUMNS = ["entity_type", "raw_name", "canonical", "method", "score",
               "matched_alias", "source_table", "occurrences"]


@dataclass(frozen=True)
class NameOccurrence:
    entity_type: str
    raw_name: str
    source_table: str
    startup: Optional[str] = None     # context for PRODUCT rows


# ---------------- collection ----------------
def _ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def collect_from_startups(conn: sqlite3.Connection) -> Iterator[NameOccurrence]:
    for row in conn.execute("SELECT entity_name FROM startups"):
        name = (row["entity_name"] or "").strip()
        if name:
            yield NameOccurrence(STARTUP, name, "startups")


def collect_from_products(conn: sqlite3.Connection) -> Iterator[NameOccurrence]:
    for row in conn.execute("SELECT startup_name, product_name FROM products"):
        sname = (row["startup_name"] or "").strip()
        pname = (row["product_name"] or "").strip()
        if sname:
            yield NameOccurrence(STARTUP, sname, "products")
        if pname:
            yield NameOccurrence(PRODUCT, pname, "products", startup=sname or None)


def collect_from_jobs(conn: sqlite3.Connection) -> Iterator[NameOccurrence]:
    for row in conn.execute("SELECT company_raw FROM jobs"):
        name = (row["company_raw"] or "").strip()
        if name:
            yield NameOccurrence(STARTUP, name, "jobs")


# ---------------- build ----------------
def build_rows(resolver: Resolver, occurrences: Iterable[NameOccurrence],
               now: Optional[str] = None) -> list[dict]:
    now = now or datetime.now(timezone.utc).isoformat()
    agg: dict[tuple[str, str], dict] = {}
    for occ in occurrences:
        key = (occ.entity_type, occ.raw_name)
        e = agg.get(key)
        if e is None:
            e = {"count": 0, "tables": set(), "startup": occ.startup}
            agg[key] = e
        e["count"] += 1
        e["tables"].add(occ.source_table)
        if e["startup"] is None and occ.startup:
            e["startup"] = occ.startup

    rows: list[dict] = []
    for (etype, raw), e in agg.items():
        res = resolver.resolve(raw, etype, startup=e["startup"])
        method = res.method
        # A trivial identity mapping (the raw string already IS its canonical,
        # matched exactly against the seed or our own table) is EXACT_SELF so it
        # sorts after real mappings. NEW_ENTITY stays distinct — an unresolved
        # name is interesting even though its canonical equals the cleaned raw.
        if res.canonical == raw and method in (EXACT, TABLE):
            method = EXACT_SELF
        rows.append({
            "entity_type": etype,
            "raw_name": raw,
            "canonical": res.canonical,
            "method": method,
            "_raw_method": res.method,          # pre-relabel; not written to CSV
            "score": res.score,
            "matched_alias": res.matched_alias,
            "source_table": ",".join(sorted(e["tables"])),
            "occurrences": e["count"],
            "first_seen_at": now,
        })
    return rows


def sort_rows(rows: list[dict]) -> list[dict]:
    # Non-trivial (method != EXACT_SELF) first, then most frequent, then stable.
    return sorted(rows, key=lambda r: (r["method"] == EXACT_SELF,
                                       -r["occurrences"],
                                       r["entity_type"], r["raw_name"]))


def write_csv(rows: list[dict], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for r in sort_rows(rows):
            w.writerow({k: ("" if r[k] is None else r[k]) for k in CSV_COLUMNS})


def persist(db: Database, rows: list[dict]) -> None:
    db.clear_entity_map()
    for r in rows:
        db.upsert_entity_map(r)


# ---------------- table rows for the resolver ----------------
def load_startup_rows(conn: sqlite3.Connection) -> list[TableRow]:
    return [TableRow(canonical=r["entity_name"], domain=r["domain"])
            for r in conn.execute("SELECT entity_name, domain FROM startups")
            if (r["entity_name"] or "").strip()]


def load_product_rows(conn: sqlite3.Connection) -> list[TableRow]:
    return [TableRow(canonical=r["product_name"], startup=r["startup_name"])
            for r in conn.execute("SELECT product_name, startup_name FROM products")
            if (r["product_name"] or "").strip() and (r["startup_name"] or "").strip()]


# ---------------- real-data run ----------------
def _consistent_copy(src_path: str, dst_path: str) -> None:
    """Snapshot a possibly-live SQLite DB to dst using the backup API (never
    writes to or locks the source for writers)."""
    Path(dst_path).parent.mkdir(parents=True, exist_ok=True)
    if Path(dst_path).exists():
        Path(dst_path).unlink()
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Entity resolver dry run over copied DBs.")
    ap.add_argument("--state-db", default="data/state.db",
                    help="Live DB with startups/products (copied, read-only).")
    ap.add_argument("--state-copy", default="data/state_copy.db")
    ap.add_argument("--news-db", default="data/state_news.db",
                    help="DB with jobs (opened read-only).")
    ap.add_argument("--seed", default="config/seed_entities.yaml")
    ap.add_argument("--config", default="config/resolver.yaml")
    ap.add_argument("--out", default="data/entity_mapping_log.csv")
    ap.add_argument("--persist", action="store_true",
                    help="Also write rows into the entity_map table of the COPY db.")
    args = ap.parse_args()

    print(f"[copy] {args.state_db} -> {args.state_copy} (consistent snapshot)")
    _consistent_copy(args.state_db, args.state_copy)

    copy_conn = _ro(args.state_copy)
    jobs_conn = _ro(args.news_db) if Path(args.news_db).exists() else None

    resolver = Resolver(
        seed_path=args.seed, config_path=args.config,
        startup_rows=load_startup_rows(copy_conn),
        product_rows=load_product_rows(copy_conn),
    )

    occ: list[NameOccurrence] = []
    occ += list(collect_from_startups(copy_conn))
    occ += list(collect_from_products(copy_conn))
    if jobs_conn is not None:
        occ += list(collect_from_jobs(jobs_conn))

    now = datetime.now(timezone.utc).isoformat()
    rows = build_rows(resolver, occ, now=now)
    write_csv(rows, args.out)

    _report(rows, resolver, args.out)

    if args.persist:
        db = Database(args.state_copy)
        persist(db, rows)
        db.close()
        print(f"[persist] wrote {len(rows)} rows to entity_map in {args.state_copy}")

    copy_conn.close()
    if jobs_conn is not None:
        jobs_conn.close()


def _report(rows: list[dict], resolver: Resolver, out: str) -> None:
    from collections import Counter
    # 5-way breakdown uses the resolver's real method; trivial self-identity
    # (canonical == raw via EXACT/TABLE) is reported separately.
    by_method = Counter(r["_raw_method"] for r in rows)
    trivial = sum(1 for r in rows if r["method"] == EXACT_SELF)
    print("\n==================== ENTITY RESOLVER DRY RUN ====================")
    print(f"distinct raw names: {len(rows)}")
    print("\ncounts by method (resolver layer):")
    for m in (EXACT, "DOMAIN", TABLE, "FUZZY", NEW_ENTITY):
        print(f"  {m:<11}: {by_method.get(m,0)}")
    print(f"  (trivial self-identity mappings, canonical==raw: {trivial})")

    nontrivial = [r for r in rows if r["method"] != EXACT_SELF
                  and r["method"] != NEW_ENTITY]
    nontrivial = sorted(nontrivial, key=lambda r: (-r["occurrences"], r["raw_name"]))
    print("\n--- 15 most interesting non-trivial mappings (raw -> canonical, method, score) ---")
    for r in nontrivial[:15]:
        sc = f"{r['score']:.3f}" if r["score"] is not None else "-"
        print(f"  [{r['occurrences']:>4}x] {r['raw_name']!r} -> {r['canonical']!r}  "
              f"({r['method']}, score={sc}, src={r['source_table']})")

    fuzzy = [r for r in rows if r["method"] == "FUZZY"]
    print(f"\n--- every FUZZY mapping ({len(fuzzy)}) ---")
    for r in sorted(fuzzy, key=lambda r: -r["score"]):
        print(f"  {r['raw_name']!r} -> {r['canonical']!r}  (score={r['score']:.4f}, "
              f"alias={r['matched_alias']!r}, src={r['source_table']})")

    news = [r for r in rows if r["method"] == NEW_ENTITY]
    news = sorted(news, key=lambda r: (-r["occurrences"], r["raw_name"]))
    print(f"\n--- 10 most frequent NEW_ENTITY names (of {len(news)}) ---")
    for r in news[:10]:
        print(f"  [{r['occurrences']:>4}x] {r['raw_name']!r} -> {r['canonical']!r}  ({r['source_table']})")

    print("\n--- seed near-collisions (different seed companies that came close) ---")
    coll = resolver.audit_seed_collisions(threshold=0.88)
    if not coll:
        print("  none at ratio >= 0.88")
    for a, b, score, blocked in coll:
        print(f"  {a!r} ~ {b!r}  ratio={score:.4f}  -> SAFE ({blocked})")

    print(f"\nwrote {out}")
    print("================================================================\n")


if __name__ == "__main__":
    main()
