# Claims cross-check

Every feature named in `README.md` and `docs/architecture.md` mapped to the code
that implements it. Claims that could not be pointed to were removed or softened
(bottom section). Verified 2026-11-22.

## Acquisition
| Claim | File |
|---|---|
| Async HTTP fetcher (retries, per-domain limits) | `src/crawler/fetcher.py` (`AsyncFetcher`, `_TokenBucket`, `_domain_sem`) |
| arXiv API (papers) | `src/crawler/arxiv.py` |
| GitHub repo existence + star counts | `src/crawler/github_api.py` |
| YC open dataset (startups) | `src/crawler/yc_startups.py` |
| Job adapters (arbeitnow, remoteok, greenhouse, ashby, hn_hiring) | `src/crawler/jobs/*.py` |
| Product-page crawler + pricing-link discovery | `src/crawler/product_pages.py` |
| 24 verified ATS boards (6 Greenhouse + 18 Ashby) | `config/job_boards.yaml` |
| 5 news + 5 job sources, YAML-configured | `config/sources.yaml` |

## Fault tolerance
| Claim | File |
|---|---|
| Full-jitter exponential backoff | `src/crawler/fetcher.py` (`_backoff`), `src/llm/orchestrator.py` (`_backoff`) |
| Retry-After honoured | `src/crawler/fetcher.py` (`_parse_retry_after`), `src/llm/orchestrator.py` (429 path) |
| Per-domain token bucket + bounded concurrency | `src/crawler/fetcher.py` (`_TokenBucket`, `_domain_sem`) |
| Circuit breaker (5 failures) | `src/llm/orchestrator.py` (`_circuit`, `_record_failure`) |
| Per-call deadline (`call_deadline_s`, default 90 s) | `src/llm/orchestrator.py:373` (`asyncio.wait_for`), `src/llm/config.py` |
| Progress watchdog (`--watchdog-timeout-s`, 300 s) | `src/pipeline/products.py:712` (`watchdog`) |
| Resumable state ledger | `src/storage/db.py` (`seen_items`, `source_state`, `products`) |
| Parked tiers persist across runs | `src/storage/db.py` (`tier_state`), `src/llm/orchestrator.py` (`_mark_exhausted`) |

## LLM orchestration
| Claim | File |
|---|---|
| Tier chain, first success wins | `config/llm.yaml`, `src/llm/orchestrator.py` (`extract`) |
| Per-tier RPM bucket + rolling 60 s token window | `src/llm/orchestrator.py` (`_buckets`, `_TokenWindow`) |
| Limits learned from response headers | `src/llm/orchestrator.py` (`_learn_limits`) |
| 429 short-sleep vs long-park (`max_wait_s`) | `src/llm/orchestrator.py` (429 branch) |
| 413 → re-fit smaller chunk | `src/llm/orchestrator.py` (`PayloadTooLarge`), `src/llm/chunker.py` (`fit`) |
| Extraction cache by content hash | `src/storage/db.py` (`llm_cache`), `src/llm/orchestrator.py` (`_cache_key`) |
| Grounding guard (verbatim evidence) | `src/llm/grounding.py` (`verify_grounding`), `src/pipeline/products.py` (`quote_grounded`) |
| Empty content → BadOutput; hang fixed | `src/llm/orchestrator.py` (empty-content check + `wait_for`) |

## Data quality & freshness
| Claim | File |
|---|---|
| Deterministic VERIFIED_FRESH / INFERRED_FRESH, LLM never decides | `src/common/freshness.py` (`decide`) |
| Dates from feeds / article parsing only | `src/common/dates.py`, `src/crawler/article_dates.py` |
| Pydantic v2 validation at ingest + re-validation at export | `src/schemas/*.py`, `src/export/validate.py` |
| No hallucinated URLs (source.url = fetched URL) | `src/pipeline/*.py` (record builders), `src/export/bundle.py` |
| Pricing null over guess; trial-vs-free-tier audit | `src/pipeline/products.py` (`audit_pricing`, `trial_freemium_verdict`) |

## Entity resolution & export
| Claim | File |
|---|---|
| Layers EXACT→DOMAIN→TABLE→FUZZY→NEW_ENTITY | `src/resolver/resolver.py` |
| Precision-first fuzzy guards | `config/resolver.yaml`, `src/resolver/resolver.py` |
| One shared resolver across tabs; `*_raw` columns | `src/export/resolve_map.py`, `src/export/bundle.py` |
| Canonical collision stops the bundle | `src/export/bundle.py` (`startup_collisions`) |
| DB consolidation (single state.db) | `src/export/consolidate.py` |
| Six-tab bundle + validate + spotcheck | `src/export/{bundle,validate,spotcheck}.py` |
| Synthetic eval corpus (precision/recall) | `src/export/eval_corpus.py`, `docs/entity_resolution_eval.*` |

## Anti-bot ladder
| Claim | File |
|---|---|
| L0 polite GET, robots respected | `src/crawler/fetcher.py` (`_robots_allowed`) |
| Single fixed identifying UA (no rotation), same identity for robots | `src/crawler/fetcher.py` (`USER_AGENT`, `_headers`, `ROBOTS_AGENT`) |
| L1 real Chromium render, no stealth/captcha | `src/crawler/browser.py` |
| L2 stop on challenge, no bypass | `src/crawler/ladder.py`, `src/crawler/fetcher.py` (`detect_block`) |
| TTL disk cache (conditional-request precursor) | `src/crawler/fetcher.py` (`_cache_read`, `_cache_write`) |
| Site skipped for terms (recon, demo replay) | `docs/anti_bot_strategy.md`, `scripts/demo_antibot.py`, `src/crawler/ladder.py` (`render: never`) |
| Structured logging | `src/common/logging.py` |

## Claims removed or softened (could not be fully supported)
1. **"Consistent `GraphOneBot` User-Agent / not a rotating disguise" (L0).**
   RESOLVED in Step 18a. The L0 fetcher previously rotated browser UAs; it now
   sends one fixed, self-identifying UA
   `Mozilla/5.0 (compatible; GraphOneBot/1.0)` (`src/crawler/fetcher.py`
   `USER_AGENT` + `_headers`), matched against the same `GraphOneBot` robots
   identity. A regression test
   (`tests/test_fetcher.py::test_fetcher_sends_only_the_fixed_identifying_ua`)
   asserts no other UA is ever sent. README, architecture and
   `docs/anti_bot_strategy.md` now state the single identifying UA.
2. **"Conditional-request support (ETag/If-Modified-Since) already exists."**
   REMOVED. No such headers are sent (`src/crawler/fetcher.py` has none). The
   architecture now lists conditional requests as *not implemented*; the
   seen-URL/content-hash ledger and a TTL disk cache (which do exist) are cited
   instead.
