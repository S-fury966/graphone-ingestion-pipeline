# PROJECT_CONTEXT.md

> Canonical context for the GraphOne / FrontierAtlas ingestion-pipeline assignment.
> Re-read this at the start of every step. Do only what each prompt asks.

## Company & Goal
- **Company:** GraphOne / FrontierAtlas — building a global **Intelligence Graph**
  for the AI and venture ecosystem.
- **My task:** Build a scalable, fault-tolerant **ingestion pipeline** that mixes
  web scraping with LLM-based extraction.
- **Format:** 3-day hiring assignment, delivered as a sequence of small prompts.

## Pipeline — 6 Phases

### 1. Bulk acquisition (concurrent async scraper)
- Minimum output: **1,000 unique startups**, **1,000 unique products**,
  **1,000 unique research papers**.
- Papers come from **arXiv** and **Papers with Code**; each paper linked to its
  **GitHub repo** with the **current star count**.
- Design must scale to **500k+ records with infrastructure changes only**, not
  code changes.

### 2. Signal ingestion (freshness-sensitive monitoring)
- Monitor **5 AI news sources** and **5 AI job boards**.
- Only items **published in the last 24 hours** are allowed.
- Crawl **full text**.
- Normalize dates, including **relative** ones ("2 hours ago") and pages with
  **missing date meta tags**.
- If a source has no date, use a **"new since last run"** heuristic.

### 3. LLM extraction engine (raw HTML/text -> canonical JSON)
- **Fallback chain:** Gemini Flash -> Groq Llama 3 -> DeepSeek.
- **Smart chunking + truncation** to avoid **413 Payload Too Large**, while
  keeping the most useful content.
- **429 handling:** exponential backoff **with jitter**.

### 4. Entity resolution (deterministic dedup)
- Map name variants ("OpenAI", "OpenAI, Inc.", "Open AI") to canonical "OpenAI".
- Use a **mocked seed list of ~50 known AI startups**.

### 5. Anti-bot
- Crawler must be **asynchronous** (asyncio + aiohttp, or Playwright async).
- Show/document a strategy for **Cloudflare / Datadome / JS-heavy** sites.

### 6. Architecture doc (max 3 pages)
- Scale strategy.
- 413 / 429 strategy.
- Freshness tracking so **no article or job is processed twice across
  distributed nodes**.
- Storage choices (**primary DB** plus **vector/graph store**).

## Schemas
All fields required unless marked optional.

### STARTUP
- `schemaVersion`
- `recordType` = "STARTUP"
- `source.name`
- `source.url`
- `content.entityName` (canonical)
- `content.data.employeeCount` (int, optional)
- `collectedAt` (ISO-8601)

### PRODUCT
- `schemaVersion`
- `recordType` = "PRODUCT"
- `source.name`
- `source.url`
- `content.startupName` (canonical)
- `content.pricingModel` (enum: FREE, FREEMIUM, PAID, ENTERPRISE)
- `collectedAt` (ISO-8601)

### RESEARCH_PAPER
- `schemaVersion`
- `recordType` = "RESEARCH_PAPER"
- `content.title`
- `content.authors` (array)
- `content.paper_url`
- `content.github_url` (optional)
- `content.github_stars` (int, optional)
- `content.published_date` (ISO-8601)

### JOB
- `schemaVersion`
- `recordType` = "JOB"
- `content.company` (canonical)
- `content.title` (string, required, non-empty — the job title as published)
- `content.date` (ISO-8601)
- `content.is_remote` (bool)
- `content.role_family` (string, e.g. "Engineering")
- `source.url` is the job posting URL.

### NEWS
- (schema not fully enumerated in brief — define during the News step.)

### Traceability (applies to PAPER, JOB, NEWS)
Also add `source.name`, `source.url` and `collectedAt` to every record so all
records are traceable.

## Deliverables
- **Google Sheet** with 6 tabs:
  Startups, Products, Research Papers, Jobs, News, Entity Mapping Log
  (raw name vs canonical name).
- **GitHub repo** with:
  - `README.md` (setup + architecture overview)
  - `src/` (crawler, LLM orchestrator, entity resolver)
  - `architecture.pdf`

## Grading Weights
| Area | Weight |
|---|---|
| LLM orchestration | 25% |
| Data quality (date parsing, 24h freshness, GitHub tracking) | 25% |
| Scale thinking | 20% |
| Engineering rigor (async, error handling, maintainability) | 20% |
| Entity resolution | 10% |

## HARD RULES (breaking = disqualification)
1. **No hallucinated data.** Every record must trace to a real, valid source URL.
2. **LLM structures only scraped text.** If a field is not in the source text, it
   must be `null`. Never let the LLM invent values.
3. **Prefer deterministic parsing** (selectors, JSON endpoints, RSS, APIs); use
   the LLM only when the page is messy.
4. **Sources configurable via YAML**, not hardcoded (supports "scale without
   code changes").
5. **Be polite:** respect `robots.txt` and per-domain rate limits.

## Tech Preferences
- Python 3.11+
- asyncio, aiohttp
- Pydantic v2
- Playwright (async) only where needed
- SQLite for local state
- pytest
- Structured logging
- Secrets in a `.env` file (never committed)

## Working Style
- Work arrives as a sequence of small prompts.
- Do only what each prompt asks.
- After each step: summarize what was done and which files changed.

## Decisions
Locked-in decisions that resolve the Step 1 ambiguities.

### NEWS schema
- `schemaVersion`
- `recordType` = "NEWS"
- `source.name`, `source.url`
- `content.title`
- `content.published_date` (ISO-8601)
- `content.author` (optional)
- `content.full_text`
- `content.summary` (optional)
- `content.date_confidence` (enum: EXPLICIT, RELATIVE, HEURISTIC)
- `collectedAt`

### Output
- Pipeline writes **CSV + JSONL to `data/`**.
- A separate export step pushes to **Google Sheets via service account** later.

### Keys
- Keys come from `.env`.
- A tier whose key is missing is **skipped**.
- Tests use a **mock LLM**.

### LLM fallback chain (updated — supersedes Phase 3 brief)
- **No DeepSeek, no paid APIs.**
- The fallback chain is **config-driven**: a YAML list of tiers.
- **Default chain: Gemini Flash -> Groq Llama.**
- A **DeepSeek tier may exist in config but is disabled by default** and is
  skipped when no key is present.
- `DEEPSEEK_API_KEY` is **optional** (removed from required env vars).
- Tiers with a missing key are **skipped**.
- Tests use a **mock LLM tier** that can simulate **429, 413, and invalid JSON**.

### Uniqueness
- Startup = canonical name.
- Product = canonical startup name + product name.
- Paper = arXiv ID.
- Every record needs a **unique source URL**.

### Scale
- Working **1k+ run** plus a documented design. **No real 500k run.**

### Papers
- **arXiv is the primary** paper source. **Papers with Code is best-effort.**
- Only keep `github_url` if the **GitHub API confirms the repo exists**.

### Freshness
- **SQLite** with a seen-URL / content-hash table and a per-source
  high-water mark.

### JOB schema (Step 12)
- **JOB** gains `content.title` (required, non-empty) — each row must say what
  the job is. `source.url` is the job posting URL.
- `company_raw` is stored in SQLite/exports; the entity-resolution step maps it
  to a canonical `content.company` later. Until then the pipeline writes the
  source/config display name as `content.company`.
- Freshness/field provenance columns live in SQLite + CSV/JSONL only (not on the
  Pydantic model): `freshness_status`, `date_source`, `remote_basis`
  (api_flag|text_match|location_only|default_false), `role_basis`
  (department|title_match|default_other), `company_raw`.
- Greenhouse public boards expose `first_published` (a real publication time)
  and `company_name`; `updated_at` is a modified date and is ignored for
  freshness. Ashby exposes `publishedAt`. A Greenhouse job lacking
  `first_published` falls back to the snapshot "new since last run" heuristic.
  Both ATS are `listing_type: complete` (the API returns all open jobs), so a
  full successful fetch satisfies snapshot continuity.

### Entity resolution (Step 13)
- **Deterministic only, no LLM.** `src/resolver/resolver.py` `resolve(raw, type,
  startup=None)` tries layers in order: EXACT (compact key = `normalize_name`
  minus spaces/hyphens, also drops a leading "the" and "and" joiners) →
  DOMAIN (seed domain) → TABLE (our startups/products) → FUZZY (rapidfuzz) →
  NEW_ENTITY (cleaned raw). First hit wins.
- **Fuzzy guards (all required; `config/resolver.yaml`):** score ≥ 0.93, compact
  len ≥ 5, same first char, pair not in `do_not_merge`, best beats 2nd-best
  *different* canonical by ≥ 0.02. Precision over recall — a wrong merge is worse
  than a missed one.
- **Seed** `config/seed_entities.yaml` is a hand-curated MOCK (52 companies, 22
  products, 14 `do_not_merge` pairs). Aliases are deliberately tight — never a
  bare generic word.
- **Mapping log:** `entity_map` table (UNIQUE entity_type+raw_name) + 
  `data/entity_mapping_log.csv` (non-trivial first; identity matches labelled
  EXACT_SELF). Built over a consistent COPY of state.db (`state_copy.db`, via the
  SQLite backup API) + `state_news.db` jobs, both read-only; live data untouched.
- **Exporter hooks** (`src/resolver/hooks.py`) canonicalize job company / startup
  entityName / product startupName with a `*_raw` extra column — defined but NOT
  wired into live exports yet; the DB keeps raw values.

### Deviations from original schema (needed to satisfy decisions)
- **PRODUCT** gains `content.name` (product name) — required to make
  "canonical startup + product name" uniqueness possible; the original schema
  listed no product name field.
- **PRODUCT** `content.pricingModel` is now **Optional (default null)** — it is
  **null when there is no pricing evidence, never guessed**. The enum is
  unchanged: exactly FREE, FREEMIUM, PAID, ENTERPRISE (no UNKNOWN member).
- **RESEARCH_PAPER** gains `content.arxiv_id` (optional) — required for
  arXiv-ID uniqueness; best-effort PwC-only papers may lack it.
