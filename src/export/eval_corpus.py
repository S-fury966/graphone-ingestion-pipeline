"""Synthetic entity-resolver evaluation corpus report (Step 16).

This is NOT live data: it is a labelled test corpus for the deterministic entity
resolver. It writes docs/entity_resolution_eval.csv (expected vs actual + a
pass/fail column) and docs/entity_resolution_eval.md (accuracy summary at the
top: precision, recall, false merges).

Precision/recall use the same definitions as tests/test_resolver.py:
  recall    = correct positive merges / all positive cases
  precision = 1 - (negatives that wrongly merged) / all negative cases
A seed-only resolver is used, so the numbers measure the resolver's intrinsic
quality and are independent of whatever happens to be in the live tables.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Dict, List, Tuple

import yaml

from ..resolver.resolver import NEW_ENTITY, Resolver

FIXTURE = "tests/fixtures/entity_eval.yaml"
CSV_OUT = "docs/entity_resolution_eval.csv"
MD_OUT = "docs/entity_resolution_eval.md"

CSV_TITLE = ("# SYNTHETIC ENTITY-RESOLVER EVAL CORPUS — labelled test cases, "
             "expected vs actual (NOT live data)")
CSV_COLUMNS = ["entity_type", "raw_name", "startup_context", "kind", "expected",
               "actual_canonical", "actual_method", "actual_score", "pass"]


def evaluate(fixture: str = FIXTURE,
             seed: str = "config/seed_entities.yaml",
             config: str = "config/resolver.yaml") -> Tuple[List[dict], Dict]:
    """Resolve every case; return (rows, summary)."""
    resolver = Resolver(seed_path=seed, config_path=config)
    cases = (yaml.safe_load(Path(fixture).read_text(encoding="utf-8")) or {}).get("cases", [])
    rows: List[dict] = []
    pos_total = pos_correct = 0
    neg_total = wrong_merges = 0
    for c in cases:
        kind = c.get("kind")
        expect = c.get("expect")
        res = resolver.resolve(c["raw"], c.get("type", "STARTUP"),
                               startup=c.get("startup"))
        if kind == "positive":
            pos_total += 1
            passed = res.canonical == expect
            pos_correct += int(passed)
        else:  # negative: must NOT merge
            neg_total += 1
            if expect == NEW_ENTITY:
                merged = res.method != NEW_ENTITY
            else:
                merged = res.canonical != expect
            passed = not merged
            wrong_merges += int(merged)
        rows.append({
            "entity_type": c.get("type", "STARTUP"), "raw_name": c["raw"],
            "startup_context": c.get("startup") or "", "kind": kind,
            "expected": expect, "actual_canonical": res.canonical,
            "actual_method": res.method,
            "actual_score": "" if res.score is None else f"{res.score:.4f}",
            "pass": "PASS" if passed else "FAIL"})
    recall = pos_correct / pos_total if pos_total else 0.0
    precision = 1.0 - wrong_merges / neg_total if neg_total else 1.0
    summary = {
        "cases": len(cases), "positives": pos_total, "positive_correct": pos_correct,
        "negatives": neg_total, "false_merges": wrong_merges,
        "recall": recall, "precision": precision,
        "failures": [r for r in rows if r["pass"] == "FAIL"],
    }
    return rows, summary


def write_docs(rows: List[dict], summary: Dict,
               csv_out: str = CSV_OUT, md_out: str = MD_OUT) -> None:
    cp = Path(csv_out)
    cp.parent.mkdir(parents=True, exist_ok=True)
    with cp.open("w", newline="", encoding="utf-8") as fh:
        fh.write(CSV_TITLE + "\n")
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    md = [
        "# Entity Resolution — Synthetic Evaluation Corpus",
        "",
        "> **Synthetic test corpus — NOT live data.** Labelled cases for the "
        "deterministic entity resolver (`src/resolver/`). Full per-case results "
        "are in [`entity_resolution_eval.csv`](entity_resolution_eval.csv).",
        "",
        "## Accuracy summary",
        "",
        f"- **Precision: {summary['precision']:.3f}** "
        f"(false merges = {summary['false_merges']} / {summary['negatives']} negatives)",
        f"- **Recall: {summary['recall']:.3f}** "
        f"({summary['positive_correct']} / {summary['positives']} positive merges correct)",
        f"- **False merges: {summary['false_merges']}** "
        "(negatives that were wrongly merged into an existing entity)",
        f"- Total cases: {summary['cases']} "
        f"({summary['positives']} positive, {summary['negatives']} negative)",
        "",
        "Definitions match `tests/test_resolver.py`: recall over positive cases, "
        "precision = 1 − wrong_merges / negatives.",
        "",
    ]
    if summary["failures"]:
        md.append("## Failures")
        md.append("")
        md.append("| raw | kind | expected | actual | method |")
        md.append("|---|---|---|---|---|")
        for r in summary["failures"]:
            md.append(f"| {r['raw_name']} | {r['kind']} | {r['expected']} | "
                      f"{r['actual_canonical']} | {r['actual_method']} |")
    else:
        md.append("**All cases pass.**")
    md.append("")
    Path(md_out).parent.mkdir(parents=True, exist_ok=True)
    Path(md_out).write_text("\n".join(md) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Write the synthetic eval corpus docs.")
    ap.add_argument("--fixture", default=FIXTURE)
    ap.add_argument("--csv", default=CSV_OUT)
    ap.add_argument("--md", default=MD_OUT)
    args = ap.parse_args()
    rows, summary = evaluate(args.fixture)
    write_docs(rows, summary, args.csv, args.md)
    print(f"precision={summary['precision']:.3f} recall={summary['recall']:.3f} "
          f"false_merges={summary['false_merges']} "
          f"(cases={summary['cases']}, pos={summary['positives']}, neg={summary['negatives']})")
    print(f"wrote {args.csv} and {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
