# Facts (single source of truth for the docs)

Every number below comes from `submission/VALIDATION.md`, the live
`data/state.db`, or the `config/*.yaml` files — not from memory. **Data snapshot
(final bundle): 2026-11-23T06:10:43Z.**

## Row counts per tab (vs required minimum)
| Tab | Rows | Minimum | Source |
|---|---|---|---|
| Startups | 1150 | 1000 | VALIDATION.md |
| Products | 1143 | 1000 | VALIDATION.md |
| Research Papers | 1044 | 1000 | VALIDATION.md |
| Jobs (fresh at bundle) | 25 | — | VALIDATION.md |
| News (fresh at bundle) | 9 | — | VALIDATION.md |
| Entity Mapping Log | 2309 | — | VALIDATION.md / entity_map table |

## Pricing distribution (Products, n=1143)
From VALIDATION.md: **null 831, FREEMIUM 182, PAID 109, ENTERPRISE 14, FREE 7**.
null = 831/1143 = **72.7 %**.

## Research papers / GitHub
From `data/state.db` (`papers`): 1044 papers, **all 1044 have `github_url` and
`github_stars`**. **911** have stars > 0; **150** have ≥ 100 stars; max = **36405**.

## Freshness (jobs/news) at the final bundle
VALIDATION.md: **Jobs 25 fresh — all VERIFIED_FRESH, 0 INFERRED_FRESH, oldest
21.6 h.** **News 9 fresh — all VERIFIED_FRESH, 0 INFERRED_FRESH, oldest 23.7 h.**
Window = 24 h, enforced at bundle time. (DB holds 18 news + 35 jobs total; the
rest were stale at bundle time.) Fresh jobs came from **3 of 5 boards**:
arbeitnow 20, greenhouse 3, ashby 2; remoteok 0 (no fresh AI remote job),
hn-hiring 0 (all 45 thread comments ≥ 24 h old → gated before the LLM, 0 calls).

## Sources (config/sources.yaml, config/job_boards.yaml)
- **Papers:** `arxiv` (API) primary; `github` (API) for repo existence + stars.
- **News — 5 enabled:** techcrunch-ai, verge-ai, ars-technica,
  mit-tech-review-ai (RSS), hn-algolia-ai (API, `date_authority: article`).
- **Job boards — 5 enabled:** arbeitnow, remoteok (APIs), greenhouse, ashby
  (ATS APIs), hn-hiring (API, LLM-parsed).
- **24 verified ATS company boards** (`config/job_boards.yaml`): **6 Greenhouse
  + 18 Ashby = 24**.

## LLM tiers (config/llm.yaml) — fallback chain, first that succeeds wins
Enabled: **gemini-flash-lite** (`gemini-3.1-flash-lite`) → **nvidia**
(`nvidia/nemotron-3.5-lightning-30b-a3b`, `max_output_tokens` 4096) →
**groq-oss-120b** (`openai/gpt-oss-120b`, `reasoning_effort: low`) →
**groq-oss** (`openai/gpt-oss-20b`, `reasoning_effort: low`). Disabled: mistral,
cerebras, deepseek, mock. Resilience: `call_deadline_s` default **90 s**,
`max_wait_s` **120 s**, `circuit_threshold` **5**.

## Entity resolution (src/resolver/, config/resolver.yaml)
- Layers, first hit wins: **EXACT → DOMAIN → TABLE → FUZZY → NEW_ENTITY**.
- Fuzzy guards: score ≥ 0.93, compact len ≥ 5, same first char, min margin 0.02.
- Live entity log: **2309 rows**, of which **20 are real non-trivial mappings**
  (method ≠ EXACT_SELF; up from 9 as more job-company names resolve to seed
  canonicals); the rest are trivial self-matches.
- Synthetic eval corpus (`docs/entity_resolution_eval.{csv,md}`): 133 cases
  (96 positive, 37 negative) → **precision 1.000, recall 1.000, 0 false merges**.

## Anti-bot ladder (src/crawler/)
Three rungs: **L0** `AsyncFetcher` GET with a single fixed identifying UA
`Mozilla/5.0 (compatible; GraphOneBot/1.0)` → **L1** real headless Chromium
render (js_only < 300 chars, policy allowing) → **L2** stop on challenge
(`blocked_by_challenge`, no retry, no bypass).

## Job ingest reason codes (src/pipeline/jobs.py `JOB_REASON_CODES`)
`NON_AI`, `DUPLICATE`, `BAD_DATE`, `FUTURE_DATE`, `STALE`, `FRESHNESS_UNPROVABLE`,
`UNDATED_BASELINE`, `ROBOTS`, `HTTP_ERROR`, `UNGROUNDED`, `NO_TEXT`,
`LLM_BUDGET_EXHAUSTED`, `EXTRACTION_FAILED`. The HN LLM source applies the
deterministic freshness gate (STALE/FUTURE_DATE/BAD_DATE) **before** any LLM
call; those are counted as `skipped_pre_llm`.

## Tests
`pytest --co`: **327 tests collected**.
