"""Validate a submission bundle. Writes submission/VALIDATION.md and exits
non-zero when any check fails.

    python -m src.export.validate [--out submission]

Operates on the WRITTEN bundle (the JSONL files carry full records; the CSVs are
checked for cell length + UTF-8), so it validates the actual artifacts that get
imported.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pydantic import ValidationError

from ..common.dates import parse_date, utc_now
from ..schemas import Job, News, Product, ResearchPaper, Startup

MINIMUMS = {"Startups": 1000, "Products": 1000, "Research Papers": 1000}
WINDOW_HOURS = 24
CELL_CAP = 45_000

_MODEL = {"STARTUP": Startup, "PRODUCT": Product, "RESEARCH_PAPER": ResearchPaper,
          "JOB": Job, "NEWS": News}
_EXTRA_KEYS = {
    "STARTUP": ("raw_name",),
    "PRODUCT": ("startup_raw", "evidence_name", "evidence_pricing"),
    "RESEARCH_PAPER": (),
    "JOB": ("company_raw", "freshness_status", "date_source", "remote_basis", "role_basis"),
    "NEWS": ("freshness_status", "date_source"),
}


def _load_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _strip_extras(obj: dict) -> dict:
    core = dict(obj)
    for k in _EXTRA_KEYS.get(obj.get("recordType"), ()):
        core.pop(k, None)
    return core


class _Report:
    def __init__(self) -> None:
        self.lines: List[str] = []
        self.ok = True

    def check(self, passed: bool, label: str, detail: str = "") -> None:
        mark = "PASS" if passed else "FAIL"
        if not passed:
            self.ok = False
        self.lines.append(f"- [{mark}] {label}" + (f" — {detail}" if detail else ""))

    def info(self, text: str) -> None:
        self.lines.append(text)


def validate(out: str = "submission", db_path: str = "data/state.db",
             now: Optional[datetime] = None,
             truncated: Optional[Dict[str, int]] = None) -> int:
    now = now or utc_now()
    out_dir = Path(out)
    rep = _Report()
    rep.info(f"# Validation report\n\nbundle now (UTC): {now.isoformat()}\n")

    data = {tab: _load_jsonl(out_dir / f"{tab}.jsonl") for tab in
            ("Startups", "Products", "Research Papers", "Jobs", "News",
             "Entity Mapping Log")}

    # 1. Row-count minimums + signal counts ---------------------------------
    rep.info("\n## Row counts")
    for tab, minimum in MINIMUMS.items():
        n = len(data[tab])
        rep.check(n >= minimum, f"{tab} >= {minimum}", f"{n} rows")
    rep.info(f"- Jobs (fresh): {len(data['Jobs'])}")
    rep.info(f"- News (fresh): {len(data['News'])}")
    rep.info(f"- Entity Mapping Log: {len(data['Entity Mapping Log'])}")

    # 2. Duplicate keys ------------------------------------------------------
    rep.info("\n## Duplicate keys")
    _dupe(rep, "startup canonical name",
          [r["content"]["entityName"] for r in data["Startups"]])
    _dupe(rep, "(startup, product) pair",
          [(r["content"]["startupName"], r["content"]["name"]) for r in data["Products"]])
    _dupe(rep, "arXiv id",
          [r["content"].get("arxiv_id") for r in data["Research Papers"]
           if r["content"].get("arxiv_id")])
    _dupe(rep, "job URL", [r["source"]["url"] for r in data["Jobs"]])
    _dupe(rep, "news URL", [r["source"]["url"] for r in data["News"]])

    # 3. Source URL present + 4. Pydantic re-validation ----------------------
    rep.info("\n## Source URL + Pydantic re-validation")
    for tab in ("Startups", "Products", "Research Papers", "Jobs", "News"):
        bad_url = 0
        bad_model = 0
        for obj in data[tab]:
            url = (obj.get("content", {}).get("paper_url")
                   if obj.get("recordType") == "RESEARCH_PAPER"
                   else obj.get("source", {}).get("url"))
            if not url or not str(url).strip():
                bad_url += 1
            try:
                _MODEL[obj["recordType"]].model_validate(_strip_extras(obj))
            except (ValidationError, KeyError):
                bad_model += 1
        rep.check(bad_url == 0, f"{tab}: every row has a source URL",
                  f"{bad_url} missing" if bad_url else "")
        rep.check(bad_model == 0, f"{tab}: every row re-validates",
                  f"{bad_model} invalid" if bad_model else "")

    # 5. Referential integrity ----------------------------------------------
    rep.info("\n## Referential integrity")
    startup_names = {r["content"]["entityName"] for r in data["Startups"]}
    orphans = sorted({r["content"]["startupName"] for r in data["Products"]
                      if r["content"]["startupName"] not in startup_names})
    rep.check(not orphans, "every Products.startupName exists in Startups.entityName",
              f"{len(orphans)} orphans: {orphans[:10]}" if orphans else "")

    # 6. Papers github + products evidence + pricing distribution ------------
    rep.info("\n## Field completeness")
    no_gh = sum(1 for r in data["Research Papers"]
                if not r["content"].get("github_url") or r["content"].get("github_stars") is None)
    rep.check(no_gh == 0, "every paper has github_url and github_stars",
              f"{no_gh} missing" if no_gh else "")
    missing_ev = sum(1 for r in data["Products"]
                     if r["content"].get("pricingModel")
                     and not (r.get("evidence_pricing") or "").strip())
    rep.check(missing_ev == 0,
              "every product with a pricing_model has an evidence quote",
              f"{missing_ev} missing" if missing_ev else "")
    dist = Counter((r["content"].get("pricingModel") or "null") for r in data["Products"])
    rep.info(f"- pricing_model distribution: {dict(dist)}")

    # 7. Freshness of Jobs/News ---------------------------------------------
    rep.info("\n## Freshness (jobs/news, window 24h at bundle time)")
    for tab, datekey in (("Jobs", "date"), ("News", "published_date")):
        rows = data[tab]
        oldest_age_h = None
        too_old = 0
        for obj in rows:
            parsed = parse_date(obj["content"][datekey], now=now)
            if parsed is None:
                too_old += 1
                continue
            age_h = (now - parsed.dt).total_seconds() / 3600.0
            oldest_age_h = age_h if oldest_age_h is None else max(oldest_age_h, age_h)
            if age_h > WINDOW_HOURS:
                too_old += 1
        fs = Counter(o.get("freshness_status") for o in rows)
        rep.check(too_old == 0, f"{tab}: no item older than {WINDOW_HOURS}h",
                  f"{too_old} stale" if too_old else "")
        age_str = f"{oldest_age_h:.1f}h" if oldest_age_h is not None else "n/a (0 items)"
        rep.info(f"- {tab}: oldest item age = {age_str}; freshness_status = {dict(fs)}")

    # 8. Entity log: real mappings only, counts per source_table -------------
    rep.info("\n## Entity Mapping Log")
    elog = data["Entity Mapping Log"]
    src_counts = Counter(r.get("source_table") for r in elog)
    synthetic = [r for r in elog if "eval_corpus" in (r.get("source_table") or "")]
    rep.check(not synthetic, "no synthetic eval_corpus rows in the live entity log",
              f"{len(synthetic)} synthetic rows present" if synthetic else "")
    nontrivial = sum(1 for r in elog if r.get("method") != "EXACT_SELF")
    rep.info(f"- real non-trivial mappings (method != EXACT_SELF): {nontrivial}")
    rep.info(f"- rows per source_table: {dict(src_counts)}")

    # 9. Max cell length per CSV + UTF-8 ------------------------------------
    rep.info("\n## CSV cell length + UTF-8")
    for tab in ("Startups", "Products", "Research Papers", "Jobs", "News",
                "Entity Mapping Log"):
        p = out_dir / f"{tab}.csv"
        ok_utf8, max_len, max_col = _csv_stats(p)
        rep.check(ok_utf8, f"{tab}.csv is valid UTF-8")
        over = max_len > CELL_CAP + len(" [truncated]")
        rep.check(not over, f"{tab}.csv max cell <= {CELL_CAP} (+marker)",
                  f"max {max_len} in {max_col}")
        rep.info(f"- {tab}.csv: max cell length {max_len} (col {max_col})")

    if truncated:
        rep.info("\n## Truncated cells (this bundle)")
        for col, n in truncated.items():
            rep.info(f"- {col}: {n}")

    rep.info("\n---\n")
    rep.info("**RESULT: " + ("PASS" if rep.ok else "FAIL") + "**")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "VALIDATION.md").write_text("\n".join(rep.lines) + "\n", encoding="utf-8")
    print(f"validation: {'PASS' if rep.ok else 'FAIL'} -> {out_dir/'VALIDATION.md'}")
    return 0 if rep.ok else 1


def _dupe(rep: _Report, label: str, keys: List) -> None:
    c = Counter(keys)
    dupes = {k: n for k, n in c.items() if n > 1}
    rep.check(not dupes, f"no duplicate {label}",
              f"{len(dupes)} dup keys e.g. {list(dupes)[:3]}" if dupes else "")


def _csv_stats(path: Path) -> Tuple[bool, int, str]:
    if not path.exists():
        return False, 0, ""
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, [])
            max_len, max_col = 0, ""
            for row in reader:
                for i, cell in enumerate(row):
                    if len(cell) > max_len:
                        max_len = len(cell)
                        max_col = header[i] if i < len(header) else f"col{i}"
        return True, max_len, max_col
    except UnicodeDecodeError:
        return False, 0, ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate a submission bundle.")
    ap.add_argument("--out", default="submission")
    ap.add_argument("--db", default="data/state.db")
    args = ap.parse_args()
    return validate(args.out, db_path=args.db)


if __name__ == "__main__":
    raise SystemExit(main())
