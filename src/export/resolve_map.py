"""Shared entity-resolution mapping for exports (Step 15).

ONE resolver instance is built from the live consolidated DB and used for every
tab, so the same raw name always resolves identically in Startups, Products and
Jobs. Stored raw values are never modified — the canonical replaces the field at
export time and the raw value is kept in a `*_raw` column.

Also regenerates the Entity Mapping Log (entity_map table + the CSV tab), in the
order: real non-trivial mappings, synthetic eval-corpus rows (clearly labelled),
trivial self-matches.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from ..resolver.mapping_log import (EXACT_SELF, build_rows, collect_from_jobs,
                                    collect_from_products,
                                    collect_from_startups, load_product_rows,
                                    load_startup_rows, persist, sort_rows)
from ..resolver.resolver import STARTUP, Resolver
from ..storage.db import Database

EVAL_SOURCE_TABLE = "eval_corpus (synthetic)"
EVAL_FIXTURE = "tests/fixtures/entity_eval.yaml"


def build_resolver(conn: sqlite3.Connection,
                   seed_path: str = "config/seed_entities.yaml",
                   config_path: str = "config/resolver.yaml") -> Resolver:
    """One resolver seeded from the live startups/products tables + seed file."""
    return Resolver(seed_path=seed_path, config_path=config_path,
                    startup_rows=load_startup_rows(conn),
                    product_rows=load_product_rows(conn))


def canonical_startup(resolver: Resolver, raw: str) -> str:
    """Canonical name for a startup / product-startup / job-company raw string."""
    return resolver.resolve(raw, STARTUP).canonical


def startup_collisions(conn: sqlite3.Connection,
                       resolver: Resolver) -> List[dict]:
    """Distinct startups that resolve to the SAME canonical name.

    Returns one dict per colliding canonical: {canonical, raw_names, source_urls}.
    An empty list means every startup has a unique canonical.
    """
    by_canon: Dict[str, Dict[str, str]] = {}   # canonical -> {raw_name: source_url}
    for row in conn.execute("SELECT entity_name, source_url FROM startups"):
        raw = (row["entity_name"] or "").strip()
        if not raw:
            continue
        canon = canonical_startup(resolver, raw)
        by_canon.setdefault(canon, {})[raw] = row["source_url"]
    out: List[dict] = []
    for canon, raws in by_canon.items():
        if len(raws) > 1:
            out.append({"canonical": canon,
                        "raw_names": sorted(raws.keys()),
                        "source_urls": [raws[r] for r in sorted(raws.keys())]})
    return sorted(out, key=lambda c: c["canonical"])


def eval_corpus_rows(resolver: Resolver,
                     fixture_path: str = EVAL_FIXTURE) -> List[dict]:
    """Synthetic eval rows: one per distinct (type, raw) case in the fixture,
    resolved through the SAME shared resolver and labelled clearly."""
    data = yaml.safe_load(Path(fixture_path).read_text(encoding="utf-8")) or {}
    rows: List[dict] = []
    seen = set()
    for case in data.get("cases", []):
        raw = (case.get("raw") or "").strip()
        etype = case.get("type", STARTUP)
        if not raw or (etype, raw) in seen:
            continue
        seen.add((etype, raw))
        res = resolver.resolve(raw, etype, startup=case.get("startup"))
        rows.append({
            "entity_type": etype, "raw_name": raw, "canonical": res.canonical,
            "method": res.method, "score": res.score,
            "matched_alias": res.matched_alias,
            "source_table": EVAL_SOURCE_TABLE, "occurrences": 1,
        })
    return rows


def live_entity_rows(conn: sqlite3.Connection, resolver: Resolver,
                     now: Optional[str] = None) -> List[dict]:
    """Real mapping rows from startups + products + jobs in the live DB."""
    occ = list(collect_from_startups(conn))
    occ += list(collect_from_products(conn))
    occ += list(collect_from_jobs(conn))
    return build_rows(resolver, occ, now=now)


def ordered_mapping_rows(live_rows: List[dict]) -> List[dict]:
    """Real mappings only: non-trivial first, then trivial self-matches.

    The synthetic eval corpus is NOT included here — it lives in
    docs/entity_resolution_eval.{csv,md} (see src/export/eval_corpus.py).
    """
    live_sorted = sort_rows(live_rows)
    nontrivial = [r for r in live_sorted if r["method"] != EXACT_SELF]
    trivial = [r for r in live_sorted if r["method"] == EXACT_SELF]
    return nontrivial + trivial


def regenerate_entity_map(db: Database, conn: sqlite3.Connection,
                          resolver: Resolver, out_csv: str,
                          fixture_path: str = EVAL_FIXTURE) -> Dict[str, int]:
    """Rebuild entity_map in the DB (live rows) and write the CSV tab (live +
    synthetic eval). Returns per-source_table row counts for the CSV."""
    now = datetime.now(timezone.utc).isoformat()
    live = live_entity_rows(conn, resolver, now=now)
    persist(db, live)                      # entity_map table = live rows only
    ordered = ordered_mapping_rows(live)   # real only: non-trivial then trivial
    _write_ordered_csv(ordered, out_csv)
    from collections import Counter
    return dict(Counter(r["source_table"] for r in ordered))


def _write_ordered_csv(rows: List[dict], path: str) -> None:
    import csv
    from ..resolver.mapping_log import CSV_COLUMNS
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r[k]) for k in CSV_COLUMNS})
