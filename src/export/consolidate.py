"""Consolidate the signal DB (state_news.db) into the single state.db.

Copies the news, jobs, seen_items and source_state tables from the source DB
into the main DB with INSERT OR IGNORE (never overwriting or deleting anything),
after verifying the two schemas match. Prints/returns before/after row counts.

Read side is opened read-only; only the main DB is written.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Dict, List, Tuple

from ..common.logging import get_logger
from ..storage.db import Database

log = get_logger("export.consolidate")

# Tables that live in the signal DB and must move into the consolidated DB.
CONSOLIDATED_TABLES: Tuple[str, ...] = ("news", "jobs", "seen_items", "source_state")


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def check_schemas(main_conn: sqlite3.Connection, src_conn: sqlite3.Connection,
                  tables: Tuple[str, ...] = CONSOLIDATED_TABLES) -> None:
    """Raise ValueError if any table's columns differ between the two DBs."""
    for t in tables:
        main_cols = _columns(main_conn, t)
        src_cols = _columns(src_conn, t)
        if not src_cols:
            raise ValueError(f"source DB has no table {t!r}")
        if not main_cols:
            raise ValueError(f"main DB has no table {t!r}")
        if main_cols != src_cols:
            raise ValueError(
                f"schema mismatch for {t!r}: main={main_cols} source={src_cols}")


def consolidate(main_path: str, source_path: str,
                tables: Tuple[str, ...] = CONSOLIDATED_TABLES) -> Dict[str, dict]:
    """Copy `tables` from source_path into main_path (INSERT OR IGNORE).

    Returns {table: {"main_before", "source", "inserted", "main_after",
    "expected_after"}}. Idempotent: a second run inserts 0 rows.
    """
    # Ensure the main DB exists with the full schema (creates empty tables if new).
    Database(main_path).close()

    main = sqlite3.connect(main_path)
    main.row_factory = sqlite3.Row
    src = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    report: Dict[str, dict] = {}
    try:
        check_schemas(main, src, tables)
        for t in tables:
            cols = _columns(main, t)
            before = _count(main, t)
            src_rows = list(src.execute(f"SELECT {', '.join(cols)} FROM {t}"))
            source_n = len(src_rows)
            placeholders = ", ".join("?" for _ in cols)
            sql = (f"INSERT OR IGNORE INTO {t} ({', '.join(cols)}) "
                   f"VALUES ({placeholders})")
            cur = main.executemany(sql, [tuple(r[c] for c in cols) for r in src_rows])
            inserted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
            main.commit()
            after = _count(main, t)
            report[t] = {
                "main_before": before, "source": source_n,
                "inserted": inserted, "main_after": after,
                "expected_after": before + inserted,
            }
            log.info("consolidated table", extra={"extra": {"table": t, **report[t]}})
    finally:
        src.close()
        main.close()
    return report


def _print_report(report: Dict[str, dict]) -> bool:
    ok = True
    print(f"{'table':14} {'main_before':>11} {'source':>7} {'inserted':>9} "
          f"{'main_after':>10} {'expected':>9} {'ok':>4}")
    for t, r in report.items():
        good = r["main_after"] == r["expected_after"]
        ok = ok and good
        print(f"{t:14} {r['main_before']:>11} {r['source']:>7} {r['inserted']:>9} "
              f"{r['main_after']:>10} {r['expected_after']:>9} {'YES' if good else 'NO':>4}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Consolidate signal DB into state.db")
    ap.add_argument("--main-db", default="data/state.db")
    ap.add_argument("--source-db", default="data/state_news.db")
    args = ap.parse_args()
    report = consolidate(args.main_db, args.source_db)
    ok = _print_report(report)
    print("consolidation OK" if ok else "consolidation MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
