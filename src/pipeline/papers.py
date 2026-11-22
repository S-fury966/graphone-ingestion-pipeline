"""Research-papers pipeline: page arXiv, verify GitHub repos, store + export.

Run:  python -m src.pipeline.papers --target 1000
Opts: --target --page-size --max-pages --refresh-stars --dry-run --hf-fallback
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from pydantic import ValidationError

from ..common.config import load_sources
from ..common.env import get_secret
from ..common.logging import get_logger
from ..crawler.arxiv import ArxivCollector, ArxivPaper
from ..crawler.fetcher import AsyncFetcher, fetcher_from_source
from ..crawler.github_api import GitHubClient, RepoInfo
from ..crawler.github_links import extract_repo_urls, from_huggingface, split_repo
from ..schemas import ResearchPaper
from ..storage.db import Database

log = get_logger("pipeline.papers")

CSV_PATH = "data/research_papers.csv"
JSONL_PATH = "data/research_papers.jsonl"


@dataclass
class Stats:
    pages: int = 0
    candidates_seen: int = 0
    with_link: int = 0
    verified: int = 0
    dropped_404: int = 0
    invalid: int = 0
    no_link: int = 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_record(paper: ArxivPaper, info: RepoInfo) -> dict[str, Any]:
    now = _now_iso()
    return {
        "arxiv_id": paper.arxiv_id,
        "title": paper.title,
        "authors": paper.authors,
        "published": paper.published.isoformat(),
        "abs_url": paper.abs_url,
        "pdf_url": paper.pdf_url,
        "abstract": paper.abstract,
        "comment": paper.comment,
        "journal_ref": paper.journal_ref,
        "github_url": info.html_url,
        "github_stars": info.stars,
        "stars_fetched_at": now,
        "source_name": "arXiv",
        "source_url": paper.abs_url,
        "collected_at": now,
    }


def _record_to_model_dict(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": "1.0",
        "recordType": "RESEARCH_PAPER",
        "source": {"name": r["source_name"], "url": r["source_url"]},
        "collectedAt": r["collected_at"],
        "content": {
            "title": r["title"],
            "authors": r["authors"],
            "paper_url": r["abs_url"],
            "published_date": r["published"],
            "arxiv_id": r["arxiv_id"],
            "github_url": r["github_url"],
            "github_stars": r["github_stars"],
        },
    }


async def _resolve_repo(paper: ArxivPaper, gh_client: GitHubClient,
                        hf_fetcher: Optional[AsyncFetcher], stats: Stats) -> Optional[RepoInfo]:
    """Discovery order: abstract/comment first, then HF fallback. First verified wins."""
    urls = extract_repo_urls(paper.search_text)
    if not urls and hf_fetcher is not None:
        urls = await from_huggingface(hf_fetcher, paper.arxiv_id)
    if not urls:
        stats.no_link += 1
        return None
    stats.with_link += 1
    for url in urls:
        owner, repo = split_repo(url)
        info = await gh_client.get_repo(owner, repo)
        if info.exists:
            return info
    stats.dropped_404 += 1
    return None


async def process_page(db: Database, papers: List[ArxivPaper], gh_client: GitHubClient,
                       hf_fetcher: Optional[AsyncFetcher], stats: Stats,
                       dry_run: bool = False, concurrency: int = 5) -> int:
    """Verify a page of papers concurrently; persist verified ones. Returns added count."""
    stats.candidates_seen += len(papers)
    new = [p for p in papers if not db.paper_exists(p.arxiv_id)]
    sem = asyncio.Semaphore(concurrency)

    async def worker(p: ArxivPaper):
        async with sem:
            return p, await _resolve_repo(p, gh_client, hf_fetcher, stats)

    results = await asyncio.gather(*(worker(p) for p in new))
    added = 0
    for paper, info in results:
        if info is None:
            continue
        record = _build_record(paper, info)
        try:
            ResearchPaper.model_validate(_record_to_model_dict(record))
        except ValidationError as exc:
            stats.invalid += 1
            log.warning("invalid paper record skipped", extra={"extra": {
                "arxiv_id": paper.arxiv_id, "reason": str(exc)}})
            continue
        if dry_run:
            stats.verified += 1
            added += 1
        elif db.insert_paper(record):
            stats.verified += 1
            added += 1
    return added


def _export(db: Database) -> int:
    rows = [dict(r) for r in db.iter_papers()]
    models: List[ResearchPaper] = []
    for r in rows:
        r = dict(r)
        r["authors"] = json.loads(r["authors"])
        models.append(ResearchPaper.model_validate(_record_to_model_dict(r)))

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


async def _refresh_stars(db: Database, gh_client: GitHubClient) -> int:
    updated = 0
    for row in list(db.iter_papers()):
        owner, repo = split_repo(row["github_url"])
        info = await gh_client.get_repo(owner, repo, force=True)
        if info.exists and info.stars is not None:
            db.update_stars(row["arxiv_id"], info.stars, _now_iso())
            updated += 1
    log.info("stars refreshed", extra={"extra": {"updated": updated}})
    return updated


def _summary(stats: Stats, total: int) -> None:
    log.info("RUN SUMMARY", extra={"extra": {
        "pages_fetched": stats.pages,
        "candidates_seen": stats.candidates_seen,
        "with_repo_link": stats.with_link,
        "verified": stats.verified,
        "dropped_404_or_unverified": stats.dropped_404,
        "invalid": stats.invalid,
        "no_link": stats.no_link,
        "final_total_in_db": total,
    }})


async def run(args: argparse.Namespace) -> None:
    db = Database(args.db)
    token = get_secret("GITHUB_TOKEN")
    if not token:
        log.warning("no GITHUB_TOKEN set; GitHub API limited to 60 req/hr")

    try:
        sources = {s.name: s for s in load_sources().sources}
    except Exception:  # noqa: BLE001
        sources = {}

    gh_cfg = sources.get("github")
    gh_fetcher = (fetcher_from_source(gh_cfg, respect_robots=False) if gh_cfg
                  else AsyncFetcher(rate_limit_per_sec=8, domain_concurrency=5,
                                    respect_robots=False))
    gh_client = GitHubClient(gh_fetcher, token, db)

    if args.refresh_stars:
        async with gh_fetcher:
            await _refresh_stars(db, gh_client)
        _export(db)
        db.close()
        return

    arxiv_cfg = sources.get("arxiv")
    arxiv_fetcher = (fetcher_from_source(arxiv_cfg, respect_robots=False) if arxiv_cfg
                     else AsyncFetcher(rate_limit_per_sec=0.33, domain_concurrency=1,
                                       respect_robots=False))
    collector = ArxivCollector()
    stats = Stats()
    total = db.count_papers()
    conc = gh_cfg.max_concurrency if gh_cfg else 5

    async with arxiv_fetcher, gh_fetcher:
        start = 0
        while total < args.target and stats.pages < args.max_pages:
            papers = await collector.fetch_page(arxiv_fetcher, start, args.page_size)
            stats.pages += 1
            if not papers:
                log.info("arxiv returned no entries; stopping")
                break
            start += args.page_size
            added = await process_page(
                db, papers, gh_client,
                gh_fetcher if args.hf_fallback else None,
                stats, dry_run=args.dry_run, concurrency=conc)
            total += added
            log.info("page complete", extra={"extra": {
                "page": stats.pages, "added": added, "verified_total": total}})

    if not args.dry_run:
        _export(db)
    _summary(stats, total)
    db.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Research-papers ingestion pipeline")
    p.add_argument("--target", type=int, default=1000, help="verified papers to reach")
    p.add_argument("--page-size", type=int, default=100, help="arXiv page size")
    p.add_argument("--max-pages", type=int, default=80, help="max arXiv pages to fetch")
    p.add_argument("--refresh-stars", action="store_true", help="refresh stars for stored rows")
    p.add_argument("--dry-run", action="store_true", help="verify but do not persist/export")
    p.add_argument("--hf-fallback", action="store_true",
                   help="use Hugging Face page as a best-effort GitHub-link fallback")
    p.add_argument("--db", default="data/state.db", help="SQLite path")
    return p


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
