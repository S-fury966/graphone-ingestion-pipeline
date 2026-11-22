# Signal Source Reconnaissance — AI News & AI Jobs (Phase II)

Phase II monitors **5 AI news sources** and **5 AI job boards** for items
**published in the last 24 hours**, with full text and normalized dates. This is
**recon only** — no pipeline built, nothing under `src/`, `config/`, or `data/`
touched. Every number below comes from **real requests via the project
`AsyncFetcher`**, run politely (2 conns/domain, ~1 req/s, single pass, no block
bypass). `robots.txt` was fetched and parsed for every domain.

**Measurement window (exact, from the HTTP responses):**
**2026-11-21 ~21:10–21:14 UTC.** The 24-hour cutoff is therefore
**≥ 2026-11-20 ~21:13 UTC**. (The machine clock trails the session calendar date
of 2026-11-22 by a few hours; all "fresh in 24h" counts use the real response
timestamps shown here.)

Probe code: [scratch/probe_jobs_news.py](../scratch/probe_jobs_news.py).

---

## TL;DR selection

**NEWS (use):** TechCrunch AI · The Verge AI · Ars Technica · MIT Tech Review AI ·
Hacker News (Algolia, AI query). **Backups:** The Decoder (article pages only) ·
Hugging Face blog.

**JOBS (use):** Arbeitnow API · RemoteOK API · Ashby ATS (curated AI companies) ·
Greenhouse ATS (curated AI companies) · HN "Who is hiring" (Algolia). **Backups:**
We Work Remotely RSS · Lever ATS (same pattern, slug discovery needed).

**Dropped on politeness/robots grounds:** Remotive API (`/api` disallowed in
robots), Google News RSS (`/rss/search` disallowed), The Decoder **feed**
(`/feed/` disallowed — its article pages are allowed), ai-jobs.net (HTTP 403),
VentureBeat (stale AI feed + HTTP 429 on main feed).

---

## Summary table — NEWS

| Source | Access | robots (our paths) | Date field (raw sample) | Fresh ≤24h @21:13Z | Full text in feed? | AI filter? | Verdict |
|---|---|---|---|---|---|---|---|
| **TechCrunch AI** | RSS | ✅ allowed | RFC822 `Sat, 21 Nov 2026 21:07:42 +0000` | **3** | No (≈150-char excerpt) → crawl article (static) | Pre-filtered (AI category) | **USE** |
| **The Verge AI** | Atom | ✅ allowed | ISO `2026-11-21T15:03:37-05:00` | **3** | Yes (`content`, ~1.5–45 KB) | Pre-filtered (AI section) | **USE** |
| **Ars Technica** | RSS (`feeds.arstechnica.com`) | ✅ (no robots on feed host; article host 405s) | RFC822 `Sat, 21 Nov 2026 12:01:13 +0000` | **5** | Partial (`content:encoded` ~1–2 KB) | **Needs AI keyword filter** (general tech) | **USE** (feed only — article crawl blocked, 405) |
| **MIT Tech Review AI** | RSS | ✅ allowed | RFC822 `Fri, 20 Nov 2026 18:15:15 +0000` | **0** now | **Yes** (full `content:encoded`, ~10 KB) | Pre-filtered (AI topic) | **USE** (high quality, low volume) |
| **Hacker News (AI)** | Algolia JSON API | ✅ (no robots; documented API) | ISO `2026-11-21T18:29:29Z` | **6** | No — links out → crawl target | Query = `AI`, `points>20`; still needs relevance check | **USE** (discovery + messy-crawl demo) |
| The Decoder | RSS | ⚠️ `/feed/` **disallowed**; articles allowed | RFC822 `Sat, 21 Nov 2026 16:23:45 +0000` | 7 | No (~900-char excerpt) → crawl article (static; page shows "hours ago") | AI-pure | **BACKUP** (feed robots-blocked; reach articles via sitemap/HN) |
| Hugging Face blog | RSS | ✅ allowed | RFC822 `Fri, 20 Nov 2026 00:00:00 GMT` | 0 | No (empty body) → crawl page | AI/ML-pure | **BACKUP** (infrequent) |
| VentureBeat AI | RSS | — | AI feed stale (Aug 2026); main feed **HTTP 429** | 0 | No | Pre-filtered | **SKIP** (stale + rate-limited) |
| Google News "AI when:1d" | RSS | ⚠️ `/rss/search` **disallowed** | RFC822, `when:1d` → all fresh | 100 | No (snippet; Google redirect links) | Query-controlled | **SKIP** (robots) — strong if permitted |

**Expected fresh news / 24h across the 5:** ≈ **17 at the measured instant**
(3+3+5+0+6), realistically **~30–60/day** — TechCrunch, Verge, Ars and HN all
publish steadily through the day; MIT TR adds a few. Ars and HN need an AI
keyword filter; the other three are pre-filtered by section.

### News field availability
All five feeds give **title** and **date** deterministically. **Author:** present
in TechCrunch (`dc:creator`), Verge (`author/name`), Ars (`dc:creator`, with
whitespace to trim), MIT TR (`dc:creator`); **absent** in HN (it's a link
aggregator). **Full text:** in-feed for Verge and MIT TR; Ars gives partial;
TechCrunch/HN/Decoder require crawling the article page — which is **static HTML
with full text** for TechCrunch, Verge and Decoder (confirmed: 1.3–3.3 k words,
`<article>` + JSON-LD `datePublished` + `<meta article:published_time>` all
present). Ars article pages are **blocked (HTTP 405)**, so Ars relies on feed
text only.

---

## Summary table — JOBS

| Source | Access | robots (our paths) | Date field (raw sample) | Fresh ≤24h @21:13Z | Full text? | AI filter? | Verdict |
|---|---|---|---|---|---|---|---|
| **Arbeitnow** | Public JSON API | ✅ `/api` allowed | **unix epoch** `1795287328` (created_at) | **244** / 300 | Yes (`description`) | **Needs filter** (~13% AI: 40/300) | **USE** (volume; epoch = no TZ trap) |
| **RemoteOK** | Public JSON API | ✅ `/api` allowed | ISO `2026-11-20T16:00:18+00:00` (+ `epoch`) | **0** now | Yes (`description`) | Needs filter (tag/keyword) | **USE** (daily remote-eng flow) |
| **Ashby ATS** (AI cos) | Public posting API | ✅ (401 on robots → allow; documented) | ISO `2026-11-09T21:26:28.671+00:00` (publishedAt) | 0–2 per board | Yes (`descriptionPlain`) | By company (curated AI list) | **USE** (best structured fields) |
| **Greenhouse ATS** (AI cos) | Public board API | ✅ allowed | ISO `2026-11-18T19:40:55-05:00` (updated_at) | small/day | Yes (`?content=true`) | By company (curated AI list) | **USE** (deep boards: Anthropic 635, Scale 169) |
| **HN "Who is hiring"** | Algolia JSON API | ✅ (documented) | ISO `2026-11-21T13:35:59Z` (comment time) | **1** | Yes (comment = whole post) | Monthly thread; needs AI filter | **USE** (relative/missing-date + LLM demo) |
| We Work Remotely | RSS | ✅ allowed | RFC822 `Tue, 03 Nov 2026 17:00:19 +0000` | 0 | Yes (`description` 3–12 KB) | Needs filter | **BACKUP** (pins stale "featured" items at top) |
| Lever ATS | Public postings API | ✅ pattern | epoch `createdAt` (ms) | — | Yes | By company | **BACKUP** (no AI-co slug confirmed; discovery needed) |
| Remotive | Public JSON API | ❌ `/api` **disallowed** | ISO **no TZ** `2026-11-19T14:58:42` | 0 | Yes | Has `category` | **SKIP** (robots disallows `/api`) |
| ai-jobs.net | RSS | — | — | — | — | AI-pure | **SKIP** (HTTP 403 / Cloudflare) |

**Expected fresh jobs / 24h across the 5:** Dominated by **Arbeitnow (~244
remote-tech, ~30 after an AI filter)**; **RemoteOK ~5–20/day**; **ATS boards
0–several/day** combined across ~15–20 curated AI companies (high quality,
deterministic fields); **HN hiring ~1–5/day**. Net: **~40–80 AI-relevant
jobs/day**, or a few hundred/day if we keep all remote-software roles.

### Job field availability (company / title / date / remote / location / desc)
| Source | company | title | date | is_remote | location | description |
|---|---|---|---|---|---|---|
| Arbeitnow | ✅ `company_name` | ✅ | ✅ epoch | ✅ `remote` (bool) | ✅ | ✅ |
| RemoteOK | ✅ `company` | ✅ `position` | ✅ ISO+epoch | ⚠️ infer from tags/"Remote" | ✅ | ✅ |
| Ashby | ✅ (= board slug) | ✅ | ✅ `publishedAt` | ✅ **`isRemote` bool** + `workplaceType` | ✅ | ✅ `descriptionPlain` |
| Greenhouse | ✅ (= board slug) | ✅ | ⚠️ `updated_at` only (not true post date) | ⚠️ infer from `location.name`/offices | ✅ string | ✅ (`content=true`) |
| HN hiring | ❌ free text | ❌ free text | ⚠️ comment timestamp only | ❌ free text | ❌ free text | ✅ (whole comment) |

---

## Answers to the four questions

### 1. Best 5+5 selection and expected fresh volume
**News:** TechCrunch AI, The Verge AI, Ars Technica, MIT Tech Review AI, Hacker
News (Algolia AI query). This mixes **pre-filtered AI sections** (TC, Verge, MIT
TR — high precision, dates always explicit, full text in-feed or via static
crawl) with **high-recall discovery** (HN, Ars) that needs an AI keyword filter
and exercises the messy per-article crawl. Backups: The Decoder (article pages)
and Hugging Face blog. **Expected ≈17 fresh at the measured instant, ~30–60/day.**

**Jobs:** Arbeitnow (volume + clean epoch dates), RemoteOK (daily remote-eng
flow), Ashby and Greenhouse ATS over a **curated AI-company list** (highest-quality
structured fields; `sources.yaml` can carry the slug list so scaling = config
only), and HN "Who is hiring". Backups: We Work Remotely RSS and Lever ATS.
**Expected ~40–80 AI-relevant jobs/day** (more if the AI filter is loosened).
Quality note: ATS boards are the most trustworthy (company is the slug, `isRemote`
is a real boolean), while Arbeitnow supplies the bulk of the raw volume.

### 2. A source with relative / missing dates (for the normalization + "new since last run" demo)
- **Job source — yes, naturally:** **HN "Who is hiring"**. Each job is a
  free-text comment with **no structured posting date** (only the comment
  timestamp) and no structured company/title/remote — the canonical case for
  deriving a date and applying the **"new since last run"** high-water-mark
  heuristic. **Greenhouse** is a second case: its board API exposes only
  `updated_at`, not a true "posted" date, so freshness also leans on the
  seen-URL / high-water-mark heuristic.
- **News source — not at the feed level:** every *good* news feed we kept
  (TechCrunch, Verge, Ars, MIT TR, HN) carries an **explicit ISO/RFC822 date**,
  so **none of the chosen news feeds has relative or missing dates** — stating
  this as asked. The relative/missing path still gets exercised one layer down,
  during **article crawl**: **The Decoder** article pages render a relative
  **"X hours ago"** string, and **HN-linked third-party pages** frequently expose
  the date only in **JSON-LD / `<meta>`** or as a relative string (and sometimes
  not at all). If a purely relative/missing *feed* is required for the demo, the
  **Hugging Face blog** fits in spirit (feed carries a date but **no body**, so
  full text + any on-page date must be recovered by crawling the post).

### 3. Deterministic vs LLM fields
- **Deterministic (no LLM):**
  - **company** — Arbeitnow (`company_name`), RemoteOK (`company`), ATS (the
    board slug *is* the canonical company). News author via feed fields.
  - **is_remote** — Ashby (`isRemote` bool + `workplaceType`), Arbeitnow
    (`remote` bool). RemoteOK is near-deterministic (tags / "Remote" location).
  - **role_family** — ATS `department`/`team` map cleanly to a family
    (Engineering, Research, Sales…); for others a **title→family keyword table**
    is deterministic for the common cases.
- **Needs the LLM engine:**
  - **HN "Who is hiring"** — company, title, is_remote, location and role_family
    all live in free prose → LLM extraction (grounded in the scraped comment).
  - **Greenhouse is_remote** — only a location string / offices; a keyword rule
    covers most, LLM handles the ambiguous ("Hybrid – SF or Remote-US").
  - **role_family** for messy/novel titles that the keyword table misses.
  - **News**: the headline/author are deterministic from the feed, but any
    *extracted entities* (which company/product a story is about) need the LLM.
  Per the hard rules, the LLM only structures text that was actually scraped;
  missing fields stay `null`.

### 4. Timezone traps observed
- **Remotive `publication_date`** — ISO **with no offset** (`2026-11-19T14:58:42`).
  Naive; it is really UTC but unlabeled → must be forced to UTC before the 24h
  test. (Moot here since Remotive is dropped on robots grounds, but it's the
  clearest trap seen.)
- **Greenhouse `updated_at`** — **US-Eastern offset** `-05:00` / `-04:00`,
  **DST-dependent**; must convert to UTC, not assume UTC.
- **The Verge `published`** — Eastern offset `-05:00` (parseable, but **not UTC**;
  normalize or the 24h window is off by ~5 h).
- **RFC822 "GMT" vs "+0000"** — HF/VentureBeat/Google News emit `GMT`, others
  `+0000`; both are UTC and `email.utils.parsedate_to_datetime` handles them, but
  the parser must accept both spellings.
- **Safe (unambiguous):** Arbeitnow (**unix epoch** — the cleanest), RemoteOK
  (`+00:00` ISO **and** epoch), Ashby (`...+00:00` with milliseconds), TechCrunch
  / MIT TR / Ars (`+0000`).
- **Non-TZ staleness trap:** We Work Remotely pins **"featured" items dated 2024**
  at the top of the feed — a freshness trap (not a timezone one): don't treat
  feed order as recency; filter strictly by parsed date.

---

## Robots.txt & access notes (as measured)
- **Allowed for our paths:** TechCrunch, The Verge, MIT TR, Hugging Face blog
  (feed + articles), RemoteOK `/api`, Arbeitnow `/api`, We Work Remotely RSS,
  Greenhouse board API, Ashby posting API (no accessible robots → allow), HN
  Algolia (documented public API, no robots).
- **Disallowed → we respect and drop/limit:** **Remotive `/api`**,
  **Google News `/rss/search`**, **The Decoder `/feed/`** (its article pages are
  allowed, so Decoder survives as an article-only backup).
- **Hostile to automation:** **Ars Technica** article host returns **405** on
  both `robots.txt` and article GETs → use the `feeds.arstechnica.com` feed only
  (no robots there) and accept partial in-feed text. **ai-jobs.net** returns
  **403** (Cloudflare) → skip.
- Several publishers (TechCrunch, Verge, RemoteOK, Google News) name AI-training
  crawlers (GPTBot, CCBot, anthropic, claude, "content signal") in robots, but
  **our feed/API/article paths are not disallowed** for a generic agent — we read
  published feeds/APIs, not training corpora, and stay within what robots permits.

## Implementation pointers (for the next step — not built here)
- Put news feeds, ATS company slugs, and job APIs in `config/sources.yaml` as
  data (`type: rss|api|html`), honoring hard-rule #4 (scale via config).
- **Date confidence** maps directly to the NEWS schema's `date_confidence`:
  `EXPLICIT` for the five feeds, `RELATIVE` for Decoder/"hours ago" article
  pages, `HEURISTIC` for HN-linked pages with no date and for HN-hiring/Greenhouse
  jobs (drive it off the SQLite high-water mark).
- Normalize **every** parsed datetime to UTC immediately (handle naive→UTC,
  Eastern offsets, epoch, and GMT/`+0000`) before the 24h comparison.
- Keep an **AI keyword filter** for Ars, HN, Arbeitnow, RemoteOK; TechCrunch,
  Verge, MIT TR, Ashby/Greenhouse-by-AI-company are pre-scoped.
