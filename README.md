# GraphOne / FrontierAtlas — Intelligence Graph Ingestion Pipeline

Scalable, fault-tolerant ingestion pipeline mixing async web scraping with
LLM-based extraction. See [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) for the full
brief, schemas, and decisions.

> Placeholder README — setup and architecture overview land in a later step.

## Project layout
```
src/
  crawler/    fetching, parsing
  llm/        orchestrator, chunker
  resolver/   entity resolution
  schemas/    Pydantic v2 models
  pipeline/   phase runners
  storage/    SQLite state, dedup
  export/     CSV / JSONL / Sheets
  common/     config loader, env, logging
config/
  sources.yaml         source registry (data, not code)
  seed_entities.yaml   canonical AI-startup seed list
tests/
data/         CSV/JSONL output + SQLite state + logs (gitignored)
```

## Quick start (dev)
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # or: pip install -r requirements.lock (pinned)
cp .env.example .env   # fill in keys
pytest
```

## robots.txt policy
We respect `robots.txt` by default (`respect_robots=True`) for all crawling, and
honor per-domain rate limits.

The **arXiv API fetcher is the one deliberate exception** (`respect_robots=False`).
`export.arxiv.org/robots.txt` returns `Disallow: /` for all user agents — that
directive governs web crawlers of the arXiv *website*, not the arXiv *API*, which
is arXiv's officially sanctioned, documented programmatic endpoint. We honor
arXiv's published guidance of roughly **1 request every 3 seconds** instead
(`rate_limit_per_sec: 0.33`, `max_concurrency: 1` in `config/sources.yaml`). The
GitHub REST API is treated the same way (it serves no `robots.txt`); both are
APIs intended for automated access. General HTML crawling continues to obey
`robots.txt`.
