# Source Reconnaissance — Startups & Products

Phase I needs **1,000+ unique startups** and **1,000+ unique products**, each
with a real source URL. This is recon only (no scrapers built). All probes used
the project `AsyncFetcher`: `respect_robots=True` for HTML pages, treated the
official/open APIs as sanctioned endpoints, and never attempted to bypass any
block. Counts below are from **actual small requests on 2026-11-21**, not guesses.

> TL;DR: **Startups** are fully covered by the open **YC (yc-oss) JSON mirror**
> (1,054 AI companies / 6,406 total, with `team_size` → employeeCount).
> **Products** are fully covered by **Hugging Face Spaces** (cursor-paginated,
> thousands), with **HN Show HN** and **GitHub search** as diversity sources.
> `pricingModel` is the one hard part — it is not in any API and must be derived
> from each product's own pricing page, else left null/UNKNOWN.

---

## 1. YC companies — open JSON mirror (yc-oss.github.io)
- **Access type:** Static JSON files on GitHub Pages (API-like, no rendering).
- **Auth:** None. Free.
- **robots/terms:** Open-source community mirror of public YC directory data;
  GitHub Pages serves it freely. **Allowed.**
- **Fields:** `name`, `website`, `one_liner` (description), `long_description`,
  **`team_size`** (→ employeeCount), `batch` (→ founded year/season),
  `industry`/`subindustry`, `all_locations`, `status`, `tags`, `url` (YC profile).
  No product name or pricing.
- **Rate limits / blocks:** None observed (plain CDN file). `200`, no challenge.
- **Real yield (proof):** `tags/artificial-intelligence.json` → **1,054** AI
  companies; `companies/all.json` → **6,406** companies total (10.9 MB).
- **Pagination / cheapest path to 1k:** None needed — one request to the AI tag
  file already returns 1,054 ≥ 1,000. Per-batch and per-tag files exist for slices.
- **Deterministic?** Yes, fully. Every field is structured JSON.
- **Verdict:** **USE** — single open file clears the entire startups target with
  real URLs, team size, and founding batch.

## 2. Hacker News — Algolia Search API (hn.algolia.com)
- **Access type:** Official public JSON API.
- **Auth:** None. Free.
- **robots/terms:** Documented public API intended for programmatic use. **Allowed.**
- **Fields:** `title` (→ product name, after stripping "Show HN:"), `url`
  (→ website; **sometimes null** for text posts), `author`, `created_at`,
  `points`, `num_comments`, `objectID` (→ `news.ycombinator.com/item?id=` URL).
  No pricing, no team size.
- **Rate limits / blocks:** No auth limit hit; `200`. Algolia caps a single query
  window at ~1,000 hits (`nbPages: 20 × 50`).
- **Real yield (proof):** `tags=show_hn` → **nbHits 504,546**. Thousands of
  product launches; realistically thousands of usable "Show HN" products.
- **Pagination / cheapest path to 1k:** `hitsPerPage` up to 1000; to exceed the
  1,000-window cap, page by time with `search_by_date` + `numericFilters=
  created_at_i<...>`. ~2–3 requests gets 1,000+.
- **Deterministic?** Name/url/date yes. Must drop hits with `url: null`.
- **Verdict:** **USE (secondary)** — great for product diversity and real launch
  URLs; no pricing.

## 3. Hugging Face — public APIs (huggingface.co/api)
- **Access type:** Official public JSON API (spaces, models, organizations).
- **Auth:** None for public listings. Free (token only raises limits).
- **robots/terms:** Public documented API. **Allowed.**
- **Fields (spaces):** `id` (`author/name`), `author` (→ org/startup),
  `cardData`, `sdk`, `tags`, `likes`, `createdAt`. Space = a runnable AI app
  (a product). Models similar + `downloads`, `pipeline_tag`. No pricing.
- **Rate limits / blocks:** None observed; `200`.
- **Real yield (proof):** `spaces?limit=50` → 50 returned **with a cursor `Link`
  header** for the next page; the catalog is hundreds of thousands. `models`
  likewise.
- **Pagination / cheapest path to 1k:** Cursor via `Link` header (or
  `?limit=1000`). ~1–2 requests → 1,000+ products.
- **Deterministic?** Yes. (Product name = space/model id; website =
  `huggingface.co/spaces/{id}`.)
- **Verdict:** **USE (primary for products)** — unbounded, free, deterministic.

## 4. GitHub — Search API (api.github.com/search)
- **Access type:** Official REST search API.
- **Auth:** Free token (we already have `GITHUB_TOKEN`); raises search to 30/min.
- **robots/terms:** Official API. **Allowed.**
- **Fields:** `full_name`, **`homepage`** (→ product website), `description`,
  `stargazers_count`, `owner` (→ org profile = startup candidate). No pricing.
- **Rate limits / blocks:** Search = **30 req/min**; observed
  `X-RateLimit-Remaining: 29` after one call. Each query caps at **1,000 results**.
- **Real yield (proof):** `q=topic:artificial-intelligence stars:>500` →
  `total_count` **711**, 50 per page. Varying topics (llm, machine-learning,
  agents, …) multiplies this well past 1,000.
- **Pagination / cheapest path to 1k:** `per_page=100` × pages, then rotate
  topics/star-bands to dodge the 1,000-per-query cap.
- **Deterministic?** Yes for name/homepage/stars. Only repos with a non-empty
  `homepage` are real products.
- **Verdict:** **MAYBE/USE (secondary)** — good AI-tool products + org profiles;
  mind the 30/min and 1,000/query caps.

## 5. Wikidata — SPARQL (query.wikidata.org/sparql)
- **Access type:** Official SPARQL endpoint, JSON results.
- **Auth:** None. Free. Data is CC0.
- **robots/terms:** Open data, public endpoint. **Allowed.**
- **Fields:** company label, `P856` official website, `P571` inception (founded),
  **`P1128` employees** (employeeCount for some), `P452` industry. Sparse coverage.
- **Rate limits / blocks:** Public endpoint with a **~60 s query budget**; our
  count query **timed out at the fetcher's 30 s total timeout** (`TimeoutError`).
  A narrower query + longer timeout returns fine.
- **Real yield (proof):** Not proven here (query timed out). AI-company coverage
  is modest and uneven versus YC.
- **Pagination:** `LIMIT`/`OFFSET`.
- **Deterministic?** Yes, but entity quality varies.
- **Verdict:** **MAYBE** — useful as an *enrichment* source (employeeCount,
  founded year) for names we already have, not as a primary list; needs a tuned
  query and a raised timeout.

## 6. Product Hunt (producthunt.com)
- **Access type:** Official **GraphQL API** (needs token); website otherwise.
- **Auth:** API token from a free developer account (OAuth). Website: none.
- **robots/terms:** `robots.txt` allows product pages but disallows `/search`,
  `/auth/*`, `/@*/*` profiles, etc. **API ToS requires a token + attribution.**
- **Fields (API):** product name, tagline, website, topics, votes, and a
  pricing/`productLinks` hint. Website HTML: heavy JS.
- **Rate limits / blocks:** **Homepage returned `403` with a challenge page
  (`blocked=True`, 5.6 KB)** to our default client — PH actively blocks
  unauthenticated automated access.
- **Real yield:** High *if* a token is obtained; website path is blocked.
- **Deterministic?** API yes; website no (JS).
- **Verdict:** **MAYBE** — only via the official token API; **skip website scraping**
  (it 403s). Not needed since HF+HN+GitHub already cover products.

## 7. AI tool directories (anti-bot observation only)
### theresanaiforthat.com
- **Access type:** JS-heavy HTML behind **Cloudflare** (`Server: cloudflare`,
  `cf-ray` present).
- **robots/terms:** `robots.txt` opens with a **"content signals" license that
  explicitly restricts AI/data collection** → terms **disallow** our use.
- **Observed:** Homepage served `200` but a **6.8 MB** JS payload via Cloudflare;
  CDN fingerprint present. Aggressive bot defenses are known for this host.
- **Verdict:** **SKIP for collection** (license) — **best Phase V anti-bot demo
  target** (Cloudflare-fronted).

### futuretools.io
- **Access type:** JS-rendered (Vercel; `Server: Vercel`, no `cf-ray`).
- **robots/terms:** `robots.txt` = `Allow: /` (disallows `/go/`, `/api/`,
  `/admin`, `/auth/`). Terms unclear.
- **Observed:** `200`, 433 KB, **no Cloudflare challenge**.
- **Verdict:** **SKIP/low-priority** — less protected; needs JS rendering;
  not required given the open APIs above.

---

## Answers to the four questions

### 1. Least-risk combination for 1,000 startups + 1,000 products
- **Startups → YC yc-oss mirror alone** (1,054 AI companies, or 6,406 total):
  open JSON, no auth, deterministic, real URLs, includes team size + batch.
- **Products → Hugging Face Spaces (primary)** + **HN Show HN** + **GitHub
  search (secondary)**: all official/open, no paid auth, deterministic names and
  URLs; combining three avoids over-reliance on one and de-dups to 1,000+ easily.
- This stack is **all official APIs / open data** — the lowest-risk path. Product
  Hunt and the AI directories are explicitly *not* on the critical path.

### 2. `content.pricingModel` without guessing
Pricing is in **none** of the APIs. Evidence-based approach:
1. For a product that has a website, fetch its site and look for a pricing page
   (`/pricing`, `/plans`, nav links containing "pricing").
2. Classify the **actual pricing-page text** deterministically first — e.g.
   "free"/"$0" → FREE; free tier + paid tier / "freemium" → FREEMIUM; only paid
   plans / "$/month" → PAID; "contact sales"/"custom"/"enterprise" → ENTERPRISE.
   Use the LLM only when the text is messy, and only to classify text that was
   actually scraped (never to invent a tier).
3. **If there is no pricing page or no evidence, leave it null/UNKNOWN** — never
   guess. (Honors the no-hallucination hard rule.)

**Schema check:** `ProductContent.pricingModel` is currently a **required**
`PricingModel` enum (FREE/FREEMIUM/PAID/ENTERPRISE) — it does **not** allow null
and has no UNKNOWN. **Recommended change:** make it
`Optional[PricingModel] = None` (null = no pricing evidence found) **and add an
`UNKNOWN` enum member** (pricing page existed but was inconclusive). This keeps
the four real tiers meaningful while making "no evidence" representable without
guessing. *(Proposal only — not changing code this step.)*

### 3. `content.data.employeeCount`
- **Primary:** YC `team_size` maps directly to `employeeCount` (deterministic,
  already present for all 1,054 AI startups).
- **Enrichment:** Wikidata `P1128` for a minority of well-known companies.
- **When unavailable:** leave **null** — the field is optional in the schema, so
  no change needed and no guessing required.

### 4. Best Phase V "aggressively blocks bots" demo + what was observed
- **Primary: theresanaiforthat.com** — Cloudflare-fronted (`Server: cloudflare`,
  `cf-ray` header present) with a restrictive "content-signals" anti-AI license
  in robots.txt; the canonical JS/Cloudflare target the brief points at.
- **Also observed: Product Hunt** returned a hard **`403` challenge page**
  (`blocked=True`) to our unauthenticated client on the homepage — a concrete,
  reproducible block we can cite.
- The anti-bot section will *document* escalation (async Playwright, residential
  context, respecting the block/backing off) against theresanaiforthat.com and
  reference the PH 403, **without bypassing** either.
