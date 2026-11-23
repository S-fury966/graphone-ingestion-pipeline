# Validation report

bundle now (UTC): 2026-11-23T04:53:01.621358+00:00


## Row counts
- [PASS] Startups >= 1000 — 1150 rows
- [PASS] Products >= 1000 — 1143 rows
- [PASS] Research Papers >= 1000 — 1044 rows
- Jobs (fresh): 25
- News (fresh): 9
- Entity Mapping Log: 2309

## Duplicate keys
- [PASS] no duplicate startup canonical name
- [PASS] no duplicate (startup, product) pair
- [PASS] no duplicate arXiv id
- [PASS] no duplicate job URL
- [PASS] no duplicate news URL

## Source URL + Pydantic re-validation
- [PASS] Startups: every row has a source URL
- [PASS] Startups: every row re-validates
- [PASS] Products: every row has a source URL
- [PASS] Products: every row re-validates
- [PASS] Research Papers: every row has a source URL
- [PASS] Research Papers: every row re-validates
- [PASS] Jobs: every row has a source URL
- [PASS] Jobs: every row re-validates
- [PASS] News: every row has a source URL
- [PASS] News: every row re-validates

## Referential integrity
- [PASS] every Products.startupName exists in Startups.entityName

## Field completeness
- [PASS] every paper has github_url and github_stars
- [PASS] every product with a pricing_model has an evidence quote
- pricing_model distribution: {'null': 831, 'FREEMIUM': 182, 'ENTERPRISE': 14, 'PAID': 109, 'FREE': 7}

## Freshness (jobs/news, window 24h at bundle time)
- [PASS] Jobs: no item older than 24h
- Jobs: oldest item age = 20.3h; freshness_status = {'VERIFIED_FRESH': 25}
- [PASS] News: no item older than 24h
- News: oldest item age = 22.4h; freshness_status = {'VERIFIED_FRESH': 9}

## Entity Mapping Log
- [PASS] no synthetic eval_corpus rows in the live entity log
- real non-trivial mappings (method != EXACT_SELF): 20
- rows per source_table: {'jobs': 20, 'products,startups': 851, 'products': 1139, 'startups': 299}

## CSV cell length + UTF-8
- [PASS] Startups.csv is valid UTF-8
- [PASS] Startups.csv max cell <= 45000 (+marker) — max 84 in source.url
- Startups.csv: max cell length 84 (col source.url)
- [PASS] Products.csv is valid UTF-8
- [PASS] Products.csv max cell <= 45000 (+marker) — max 236 in evidence_pricing
- Products.csv: max cell length 236 (col evidence_pricing)
- [PASS] Research Papers.csv is valid UTF-8
- [PASS] Research Papers.csv max cell <= 45000 (+marker) — max 5779 in content.authors
- Research Papers.csv: max cell length 5779 (col content.authors)
- [PASS] Jobs.csv is valid UTF-8
- [PASS] Jobs.csv max cell <= 45000 (+marker) — max 224 in source.url
- Jobs.csv: max cell length 224 (col source.url)
- [PASS] News.csv is valid UTF-8
- [PASS] News.csv max cell <= 45000 (+marker) — max 22144 in content.full_text
- News.csv: max cell length 22144 (col content.full_text)
- [PASS] Entity Mapping Log.csv is valid UTF-8
- [PASS] Entity Mapping Log.csv max cell <= 45000 (+marker) — max 95 in raw_name
- Entity Mapping Log.csv: max cell length 95 (col raw_name)

---

**RESULT: PASS**
