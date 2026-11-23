# GraphOne / FrontierAtlas — AI-ecosystem ingestion pipeline

An asynchronous, fault-tolerant ingestion pipeline that builds an "intelligence
graph" of the AI/venture ecosystem by mixing **deterministic scraping of official
APIs/feeds** with **LLM extraction of messy page text**. It collects startups,
products, research papers, freshness-gated news and jobs, resolves entity names
to canonical form, and exports a six-tab submission bundle.

Numbers in this README come from `docs/facts.md` (sourced from
`submission/VALIDATION.md`, the database and the config files).

## Results (data snapshot taken at 2026-11-23T04:46:03Z)
| Tab | Rows | Required minimum |
|---|---|---|
| Startups | 1150 | ≥ 1000 ✅ |
| Products | 1143 | ≥ 1000 ✅ |
| Research Papers | 1044 | ≥ 1000 ✅ |
| Jobs (fresh at bundle) | 25 | — (24 h window) |
| News (fresh at bundle) | 9 | — (24 h window) |
| Entity Mapping Log | 2309 | — |

Jobs/News are freshness-gated to the last 24 h **at bundle time**, so those two
counts reflect the snapshot moment above. Papers: all 1044 carry a verified
`github_url` + star count (911 with > 0 stars, 150 with ≥ 100). Products pricing:
null 831 / FREEMIUM 182 / PAID 109 / ENTERPRISE 14 / FREE 7. Tests: **322**.

## Quick start
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.lock          # pinned; use requirements.txt for loose pins
cp .env.example .env                       # then fill in the keys you have
```
`.env` variable **names** (values never committed; a tier whose key is missing is
skipped): `GEMINI_API_KEY`, `GROQ_API_KEY`, `NVIDIA_API_KEY`, `DEEPSEEK_API_KEY`,
`CEREBRAS_API_KEY`, `MISTRAL_API_KEY`, `GITHUB_TOKEN`, `GOOGLE_SERVICE_ACCOUNT_JSON`,
`SHEET_ID`.

Playwright (only needed for the anti-bot browser rung and rendering the
architecture PDF) is pinned in `requirements.lock`; download the browser once:
```bash
playwright install chromium
```

All state lives in a single SQLite file, `data/state.db`; every stage is
resumable and can be re-run.

## Running each stage
```bash
# 1. Research papers (arXiv + GitHub stars)
python -m src.pipeline.papers --target 1000 --page-size 100 --max-pages 80 [--hf-fallback]

# 2. Startups (YC open dataset)
python -m src.pipeline.startups --target 1000 --margin 100

# 3. Products (crawl each startup site; LLM extracts pricing from scraped text)
python -m src.pipeline.products --limit 950 --concurrency 2 --max-llm-calls 900 \
    [--use-browser] [--watchdog-timeout-s 300] [--audit-pricing]

# 4. News (5 AI feeds; deterministic 24 h freshness)
python -m src.pipeline.news --window-hours 24 --max-items-per-source 40

# 5. Jobs (API/ATS boards; --skip-llm-sources drops the HN free-text source)
python -m src.pipeline.jobs --window-hours 24 [--skip-llm-sources] [--max-llm-calls 30]

# 6. Submission bundle (applies the resolver, enforces freshness at export)
python -m src.export.bundle --out submission        # add --run-signals to re-ingest
                                                    # news+jobs first; --with-llm-sources
                                                    # adds the HN jobs source

# 7. Validation report + spot check
python -m src.export.validate --out submission
python -m src.export.spotcheck --n 10 --out submission
```
Common flags: `--db <path>` (default `data/state.db`), `--dry-run`
(collect/validate but do not persist/export).

## Repo layout
```
src/
  crawler/     async HTTP fetcher, arXiv, GitHub, YC, job adapters,
               product-page crawler, anti-bot browser + escalation ladder
  llm/         orchestrator (tier fallback, limits, grounding), providers, chunker
  pipeline/    papers, startups, products, news, jobs — one ingest+export each
  resolver/    deterministic entity resolution + mapping log
  export/      consolidate, resolve_map, bundle, validate, spotcheck, eval_corpus
  schemas/     Pydantic v2 record models (one per record type)
  storage/     SQLite state (single data/state.db)
config/        YAML: sources, job_boards, llm tiers, resolver seed + thresholds
docs/          facts, architecture, recon notes, resolver eval, anti-bot strategy
submission/    the six CSV tabs + IMPORT.md + VALIDATION.md (JSONL kept local)
tests/         pytest suite (mock LLM; no network)
```

## Data model — six tabs
`Startups`, `Products`, `Research Papers`, `Jobs`, `News`, `Entity Mapping Log`.
Each record is a Pydantic model flattened to dotted CSV columns, with
`schemaVersion`, `source.name`, `source.url` and `collectedAt` on every row
(full schemas in `src/schemas/`). Audit extras travel alongside: `raw_name`
(startup), `startup_raw` + evidence quotes (product), `company_raw` +
freshness/basis columns (jobs/news), `github_url`/`github_stars`/`arxiv_id`
(papers). The Entity Mapping Log is raw → canonical with method, score,
source_table and occurrences.

## Data integrity rules
- **No LLM-supplied URLs.** Every `source.url` is the real page/API URL the
  crawler fetched; the LLM only structures already-scraped text.
- **Grounding guard.** Each extracted field must be backed by a **verbatim quote
  from the fetched text** (`src/llm/grounding.py`, `src/pipeline/products.py`);
  ungrounded fields are dropped, and a product with a `pricing_model` always has
  a stored evidence quote (checked in validation).
- **Freshness is deterministic and the LLM never decides it**
  (`src/common/freshness.py`): dates come only from feeds/`article_dates`, then a
  pure comparison to `now`. **VERIFIED_FRESH** = a trusted publication timestamp
  inside 24 h; **INFERRED_FRESH** = no usable timestamp but the "new since last
  complete snapshot" heuristic (with feed continuity) proves it is new.
- **Null over guess.** `pricingModel` is null when there is no evidence; it is
  never invented.

## Job ingest reason codes
Every rejected job is written to `data/jobs_rejected.csv` with one of these codes
(single source of truth: `JOB_REASON_CODES` in `src/pipeline/jobs.py`):
`NON_AI`, `DUPLICATE`, `BAD_DATE`, `FUTURE_DATE`, `STALE`, `FRESHNESS_UNPROVABLE`,
`UNDATED_BASELINE`, `ROBOTS`, `HTTP_ERROR`, `UNGROUNDED`, `NO_TEXT`,
`LLM_BUDGET_EXHAUSTED`, `EXTRACTION_FAILED`.
For the LLM source (HN "Who is hiring"), the **deterministic freshness gate runs
before any LLM call**: a comment that is `STALE`/`FUTURE_DATE`/`BAD_DATE` is
rejected and never reaches the orchestrator (counted as `skipped_pre_llm`), so
the LLM budget is spent only on possibly-fresh comments. `NO_TEXT` is reserved
for genuinely empty comment text; `LLM_BUDGET_EXHAUSTED` means `--max-llm-calls`
was reached; `EXTRACTION_FAILED` means the LLM call failed or returned nothing
usable.

## Ethics & anti-bot stance
Be a polite, honest client and prefer official data. Both rungs send **one
fixed, self-identifying User-Agent** — `Mozilla/5.0 (compatible; GraphOneBot/1.0)`
at L0 (`src/crawler/fetcher.py`) and `GraphOneBot/1.0` at L1 — never a rotating
browser disguise. `robots.txt` is respected for HTML pages and matched against
the same `GraphOneBot` identity, and per-domain rate limits are enforced. The L1
browser rung (`src/crawler/browser.py`) is a real headless Chromium with **no
stealth plugins, no fingerprint spoofing and no captcha solving** — on a
challenge it stops (`blocked_by_challenge`) and the answer is an alternative
source, never a bypass. One site documented in the recon notes is skipped
because its terms forbid AI collection (`docs/anti_bot_strategy.md`, replayed in
`scripts/demo_antibot.py` without a request). Bulk data comes from sanctioned
APIs/feeds; **arXiv is accessed through its public API** by design (the
documented access path), which is why it is not gated by HTML `robots`.

## Known limitations (plainly)
- **Pricing is null for 72.7 % of products (831/1143).** Most are early-stage YC
  sites with no public price (waitlists, "contact sales"); the pipeline refuses
  to guess, so "no evidence" becomes null rather than a fabricated label.
- **Fresh news/jobs counts are small and depend on run time** (4 jobs, 3 news at
  the last bundle). The 24 h window is enforced *at export*, so counts reflect
  how recently the bundle ran, not total ingested.
- **Free-tier LLM quotas** (Gemini daily request cap, a shared Groq ~200k
  tokens/day) throttle large product runs; the orchestrator parks capped tiers
  and falls through.
- **The resolver eval is a developer-set corpus, not blind.** The 133 cases in
  `docs/entity_resolution_eval.md` include cases used while tuning the resolver,
  so precision/recall 1.0 measures the tuned behaviour, not held-out
  generalization.
- **Only ~20 real non-trivial entity mappings** exist (20 at the latest snapshot,
  mostly job-company names resolving to seed canonicals) because the seed list is
  a mock of ~50 companies; most live names self-match (trivial) rather than
  collapsing variants.
- **Publisher article pages that return 403/405 are skipped — we do not bypass
  blocks.** The HN-news source verifies each item's publication date by fetching
  the article itself; bot-hostile/paywalled publishers (NYT, Economist, Ars,
  Techdirt, …) refuse our identifying `GraphOneBot` client, so those items lose
  their article date and are dropped as unprovable/NO_TEXT. This lowers the share
  of HN items with a verifiable article date — an honest cost of not bypassing.

## Importing into Google Sheets
The six `submission/*.csv` files become the six tabs of one sheet. Step-by-step
(File → Import, Insert new sheet, **uncheck** "Convert text to numbers, dates and
formulas", exact tab names, share as Anyone-with-link / Viewer) is in
[`submission/IMPORT.md`](submission/IMPORT.md).
