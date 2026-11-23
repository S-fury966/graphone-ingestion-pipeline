# GraphOne / FrontierAtlas — Ingestion Pipeline Architecture

Async scraping + grounded LLM extraction → startups, products, papers,
freshness-gated news & jobs → entity resolution → six-tab export. All state in
one SQLite file; every stage resumable. Figures from [`facts.md`](facts.md).
The polished, page-limited version is [`../architecture.pdf`](../architecture.pdf)
(2 pages, A4), rendered from this content with Playwright.

## 1 · System overview

```
 Acquisition  →  Freshness gate  →  LLM extract      →  Entity resolution  →  Export
 arXiv·GitHub     24h window,        tier chain +        EXACT→…→NEW_ENTITY     6 CSV/JSONL
 YC·ATS·RSS       deterministic      grounding guard     (deterministic)        tabs
 ─────────────────────────────────────────────────────────────────────────────────────
 SQLite data/state.db — resumable seen-URL / snapshot / products / tier-state ledger
 Resilience band: retry + jittered backoff · Retry-After · per-domain limit ·
                  circuit breaker · per-call deadline · watchdog · parked tiers
```

<svg width="700" height="150" viewBox="0 0 700 150" xmlns="http://www.w3.org/2000/svg" font-family="Helvetica,Arial,sans-serif">
  <defs><marker id="a" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto"><path d="M0,0 L6,3 L0,6 Z" fill="#48566e"/></marker></defs>
  <g font-size="10">
    <rect x="4" y="40" width="118" height="46" rx="5" fill="#e8eefc" stroke="#2f4a86"/><text x="63" y="59" text-anchor="middle" font-weight="bold" fill="#0d1b3e">Acquisition</text><text x="63" y="73" text-anchor="middle" font-size="8" fill="#48566e">arXiv·GitHub·YC·ATS·RSS</text>
    <rect x="140" y="40" width="112" height="46" rx="5" fill="#e8f7ee" stroke="#2f865a"/><text x="196" y="56" text-anchor="middle" font-weight="bold" fill="#0d3e26">Freshness</text><text x="196" y="70" text-anchor="middle" font-size="8" fill="#48566e">gate (24h)</text>
    <rect x="270" y="40" width="118" height="46" rx="5" fill="#fdf0e6" stroke="#b5651d"/><text x="329" y="56" text-anchor="middle" font-weight="bold" fill="#7a4b00">LLM extract</text><text x="329" y="70" text-anchor="middle" font-size="8" fill="#48566e">+ grounding guard</text>
    <rect x="406" y="40" width="118" height="46" rx="5" fill="#f0e8fc" stroke="#5a2f86"/><text x="465" y="56" text-anchor="middle" font-weight="bold" fill="#2e0d3e">Entity</text><text x="465" y="70" text-anchor="middle" font-size="8" fill="#48566e">resolution</text>
    <rect x="542" y="40" width="112" height="46" rx="5" fill="#e8eefc" stroke="#2f4a86"/><text x="598" y="59" text-anchor="middle" font-weight="bold" fill="#0d1b3e">Export</text><text x="598" y="73" text-anchor="middle" font-size="8" fill="#48566e">6 CSV/JSONL tabs</text>
  </g>
  <g stroke="#48566e" stroke-width="1.3" marker-end="url(#a)"><line x1="122" y1="63" x2="138" y2="63"/><line x1="252" y1="63" x2="268" y2="63"/><line x1="388" y1="63" x2="404" y2="63"/><line x1="524" y1="63" x2="540" y2="63"/></g>
  <rect x="4" y="100" width="650" height="18" rx="4" fill="#f4f6fb" stroke="#9aa7c2"/><text x="329" y="112" text-anchor="middle" font-size="8.5" fill="#0d1b3e">SQLite data/state.db — resumable seen-URL / snapshot / products / tier-state ledger</text>
  <rect x="4" y="122" width="650" height="18" rx="4" fill="#fbf4f4" stroke="#c29a9a"/><text x="329" y="134" text-anchor="middle" font-size="8.5" fill="#0d1b3e">Resilience: retry+jittered backoff · Retry-After · per-domain limit · circuit breaker · per-call deadline · watchdog · parked tiers</text>
</svg>

## 2 · Fault tolerance
- **Retries + backoff**: full-jitter exponential backoff on 429/5xx/timeout (`src/crawler/fetcher.py`, `src/llm/orchestrator.py`).
- **Retry-After honoured**: a 429's reset time is used verbatim.
- **Per-domain token bucket** + bounded per-domain concurrency.
- **Circuit breaker**: 5 consecutive failures park a tier for a cooldown.
- **Per-call deadline** (`call_deadline_s`, default 90 s): every `provider.complete` is wrapped in `asyncio.wait_for`, so a hung call can never wedge a worker.
- **Progress watchdog** (`--watchdog-timeout-s`, 300 s): if no startup finishes in the window, pending items are deferred and the run exits cleanly.
- **Resumable SQLite state**: seen-URL/content ledger, per-source snapshot high-water mark, products, and **parked-tier cooldowns that persist** (`tier_state`) across runs.

## 3 · LLM orchestration
- **Tier chain** (first success wins): gemini-flash-lite → nvidia → groq-oss-120b → groq-oss; missing-key tiers skipped.
- **Rate limiters**: per-tier RPM bucket + rolling 60 s token window; real limits learned from response headers.
- **429**: short reset → sleep+retry; long reset (> `max_wait_s` 120 s) → park tier, fall through. **413**: re-fit a smaller chunk, retry.
- **Caching**: extraction results keyed by content hash (`llm_cache`).
- **Grounding guard**: a field survives only with a verbatim quote from the scraped text; otherwise nulled.
- **Free-tier lessons (real)**: a hidden Groq **daily token cap** (~200k/day) causes long parks; Nemotron emits **empty content** when its reasoning fills the budget (now treated as BadOutput → repair/fallback); an undetected **provider hang** wedged a run — fixed by the per-call deadline above.

## 4 · Data quality & freshness
- **Deterministic first**: APIs/RSS/JSON parsed directly; the LLM is used only for messy product pages and HN free-text jobs.
- **No hallucinated URLs**: `source.url` is always the fetched URL.
- **Freshness is pure code, never the LLM** (`src/common/freshness.py`): `VERIFIED_FRESH` = trusted timestamp < 24 h; `INFERRED_FRESH` = undated but new since the last complete snapshot (feed continuity required); otherwise stale/unprovable and dropped.
- **Pydantic v2** models validate every row at ingest and re-validate at export.

## 5 · Entity resolution
- Layers, first hit wins: **EXACT → DOMAIN → TABLE → FUZZY → NEW_ENTITY** (`src/resolver/resolver.py`).
- **Precision first**: fuzzy fires only when score ≥ 0.93, compact len ≥ 5, same first char, and a 0.02 margin over the 2nd-best — a wrong merge is worse than a missed one.
- Applied **at export time** with one shared mapping across tabs; raw values stay in `*_raw` columns; colliding canonicals stop the bundle.
- **Honest caveat**: the 133-case eval (precision/recall 1.0) is a developer-set corpus including tuning cases — not blind. Only ~20 real non-trivial mappings exist (mostly job-company names → seed canonicals) because the seed list is a ~50-company mock.

## 6 · Anti-bot ladder
- **L0** polite `AsyncFetcher` GET; robots respected, and one fixed self-identifying UA `Mozilla/5.0 (compatible; GraphOneBot/1.0)` (no rotating disguise).
- **L1** real headless Chromium render — only if L0 is js_only (< 300 chars) and policy allows; **no stealth, no captcha solving**.
- **L2** challenge detected → stop (`blocked_by_challenge`), no bypass; the answer is an alternative source. One site is skipped for terms.

## 7 · Proposed design (not implemented) — 100× and beyond
- **Job queue + worker fleet**: URLs/tasks on a durable queue; stateless async workers scale on queue depth (render workers separate from HTTP).
- **Postgres instead of SQLite** for concurrent writers; same ledger schema.
- **Raw page store in object storage** (S3) so re-extraction never re-crawls — the biggest current gap (today pages aren't persisted).
- **Per-domain scheduler** with a shared rate/health store (Redis); auto-demote domains that keep challenging.
- **Batch LLM APIs + paid-tier fallback** behind the existing tier chain; the current chain + parked-tier logic already point this way.
- **Cache, observability, alerts**: structured logs exist; add metrics on defer/429/latency and alerting.
- **Incremental crawling** by sitemap + ETag/Last-Modified (conditional requests are *not* yet implemented); the seen-URL + content-hash ledger it would build on already exists.

_Already pointing that way in code_: config-driven YAML sources, per-domain limiters, persisted tier-state, seen/snapshot ledger, a TTL disk cache, and an L0/L1 split that isolates expensive rendering.

## 8 · Known limitations
- Pricing null for 72.7 % of products (no public price; never guessed).
- Fresh news/jobs counts depend on run time (25 jobs, 9 news at the 2026-11-23T06:10Z snapshot; 24 h gate at export; hn-hiring's monthly thread had no comment inside 24 h, so 3 of 5 job boards contributed).
- Bot-hostile publisher article pages (403/405) are skipped, not bypassed — this lowers the share of HN items with a verifiable article date.
- Free-tier LLM quotas throttle big runs; raw pages aren't stored, so re-extraction re-crawls.
- Resolver eval is not blind; only ~20 real non-trivial mappings (mock seed).
