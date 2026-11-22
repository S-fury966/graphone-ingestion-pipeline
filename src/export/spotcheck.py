"""Print N random rows per tab (source URL + key fields) for manual verification.

    python -m src.export.spotcheck --n 10 [--out submission]
"""
from __future__ import annotations

import argparse
import json
import random
import textwrap
from pathlib import Path
from typing import List


def _load(path: Path) -> List[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _short(v, n=160) -> str:
    s = "" if v is None else str(v)
    return textwrap.shorten(s, width=n, placeholder=" …")


def _sample(rows: List[dict], n: int) -> List[dict]:
    if len(rows) <= n:
        return rows
    return random.sample(rows, n)


def spotcheck(out: str = "submission", n: int = 10, seed: int = 0) -> None:
    random.seed(seed)
    out_dir = Path(out)

    def head(tab):
        print(f"\n{'='*70}\n{tab}  (random {n})\n{'='*70}")

    head("Startups")
    for r in _sample(_load(out_dir / "Startups.jsonl"), n):
        c = r["content"]
        print(f"- {c['entityName']}  (raw: {r.get('raw_name')})  "
              f"employees={c['data'].get('employeeCount')}")
        print(f"    url: {r['source']['url']}")

    head("Products")
    for r in _sample(_load(out_dir / "Products.jsonl"), n):
        c = r["content"]
        print(f"- {c['startupName']} / {c['name']}  pricing={c.get('pricingModel')}")
        print(f"    url: {r['source']['url']}")
        print(f"    name_evidence:    {_short(r.get('evidence_name'))}")
        print(f"    pricing_evidence: {_short(r.get('evidence_pricing'))}")

    head("Research Papers")
    for r in _sample(_load(out_dir / "Research Papers.jsonl"), n):
        c = r["content"]
        print(f"- {_short(c['title'], 90)}  [{c.get('arxiv_id')}]  stars={c.get('github_stars')}")
        print(f"    paper_url:  {c['paper_url']}")
        print(f"    github_url: {c.get('github_url')}")

    head("Jobs")
    for r in _sample(_load(out_dir / "Jobs.jsonl"), n):
        c = r["content"]
        print(f"- {c['company']} — {_short(c['title'], 80)}  remote={c['is_remote']}  {c['date']}")
        print(f"    url: {r['source']['url']}")

    head("News")
    for r in _sample(_load(out_dir / "News.jsonl"), n):
        c = r["content"]
        print(f"- {_short(c['title'], 90)}  {c['published_date']}")
        print(f"    url: {r['source']['url']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Random spot-check of the bundle.")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--out", default="submission")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    spotcheck(args.out, args.n, args.seed)


if __name__ == "__main__":
    main()
