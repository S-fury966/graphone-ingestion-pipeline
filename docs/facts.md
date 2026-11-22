# Facts (single source of truth for the docs)

Every number below comes from `submission/VALIDATION.md`, the live
`data/state.db`, or the `config/*.yaml` files — not from memory. Last bundle:
**2026-11-22T20:35:33Z**.

## Row counts per tab (vs required minimum)
| Tab | Rows | Minimum | Source |
|---|---|---|---|
| Startups | 1150 | 1000 | VALIDATION.md |
| Products | 1143 | 1000 | VALIDATION.md |
| Research Papers | 1044 | 1000 | VALIDATION.md |
| Jobs (fresh at bundle) | 4 | — | VALIDATION.md |
| News (fresh at bundle) | 3 | — | VALIDATION.md |
| Entity Mapping Log | 2296 | — | VALIDATION.md / entity_map table |

## Pricing distribution (Products, n=1143)
From VALIDATION.md: **null 831, FREEMIUM 182, PAID 109, ENTERPRISE 14, FREE 7**.
null = 831/1143 = **72.7 %**.

## Research papers / GitHub
From `data/state.db` (`papers`): 1044 papers, **all 1044 have `github_url` and
`github_stars`** (schema requires both). **911** have stars > 0; **150** have
≥ 100 stars; max stars = **36405**.

## Freshness (jobs/news) at the last bundle
VALIDATION.md: Jobs oldest 19.9 h, 4 fresh, all `VERIFIED_FRESH`; News oldest
23.5 h, 3 fresh, all `VERIFIED_FRESH`. Window = 24 h, enforced at bundle time.
(DB holds 10 jobs and 9 news total; the rest were stale at bundle time.)

## Sources (config/sources.yaml, config/job_boards.yaml)
- **Papers:** `arxiv` (API) primary; `github` (API) for repo existence + star
  counts.
- **News — 5 enabled:** techcrunch-ai, verge-ai, ars-technica,
  mit-tech-review-ai (RSS), hn-algolia-ai (API, `date_authority: article`).
  2 disabled (the-decoder, huggingface-blog).
- **Job boards — 5 enabled:** arbeitnow, remoteok (APIs), greenhouse, ashby
  (ATS APIs), hn-hiring (API, LLM-parsed). 1 disabled (weworkremotely).
- **24 verified ATS company boards** (`config/job_boards.yaml`, "Slugs VERIFIED
  with one live request"): **6 Greenhouse + 18 Ashby = 24**.

## LLM tiers (config/llm.yaml) — fallback chain, first that succeeds wins
Enabled chain, in order:
1. **gemini-flash-lite** — Gemini `gemini-3.1-flash-lite`; separate provider
   budget, tried first to spare the shared Groq daily pool.
2. **nvidia** — generic OpenAI-compatible, `nvidia/nemotron-3.5-lightning-30b-a3b`
   via `integrate.api.nvidia.com`; `max_output_tokens` 4096 (verbose reasoning).
3. **groq-oss-120b** — Groq `openai/gpt-oss-120b`, `reasoning_effort: low`.
4. **groq-oss** — Groq `openai/gpt-oss-20b`, `reasoning_effort: low`.

Disabled (configured, skipped): mistral (`mistral-small-latest`), cerebras
(`llama-3.3-70b`), deepseek (`deepseek-chat`), mock (`mock-1`, tests only).
Resilience params: `call_deadline_s` default **90 s** (per-call hard deadline),
`max_wait_s` **120 s** (park-vs-sleep on long 429s), `circuit_threshold` **5**.

## Entity resolution (src/resolver/, config/resolver.yaml)
- Layers, first hit wins: **EXACT → DOMAIN → TABLE → FUZZY → NEW_ENTITY**.
- Fuzzy guards: score ≥ 0.93, compact len ≥ 5, same first char, min margin 0.02.
- Live entity log: **2296 rows**, of which **9 are real non-trivial mappings**
  (method ≠ EXACT_SELF); the rest are trivial self-matches.
- Synthetic eval corpus (`docs/entity_resolution_eval.{csv,md}`): 133 cases
  (96 positive, 37 negative) → **precision 1.000, recall 1.000, 0 false merges**.

## Anti-bot ladder (src/crawler/ladder.py)
Three rungs: **L0** plain `AsyncFetcher` GET → **L1** real headless Chromium
render (only if L0 is js_only < 300 chars and policy allows) → **L2** stop on
challenge (`blocked_by_challenge`, no retry, no bypass).

## Tests
`pytest --co`: **321 tests collected**.
