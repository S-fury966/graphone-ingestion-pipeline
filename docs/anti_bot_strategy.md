# Anti-Bot & JS-Rendering Strategy (Phase V)

This explains how the pipeline handles bot protection and JavaScript-heavy
sites. The guiding rule is simple: **be a polite, honest client, prefer official
data, and render with a real browser only when a page genuinely needs it.** We
never try to defeat a protection system.

## 1. Principle: don't trigger protection in the first place

Most anti-bot systems fire on *behaviour*, not identity. So the cheapest defence
is to behave well, which the shared `AsyncFetcher` already does:

- **One polite rate per domain** (token bucket) and bounded per-domain
  concurrency — we never flood a host.
- **Caching + conditional requests** and a hard response-size cap, so we re-ask
  for as little as possible.
- **A consistent, identifying User-Agent** (`GraphOneBot`), stable within a
  session — not a rotating disguise.
- **robots.txt respected** for every HTML page (official API/RSS endpoints are
  used as their documented access path).
- **Prefer official APIs, RSS and open datasets** over scraping. This project
  already does exactly that for its bulk data:
  - **Research papers →** arXiv API + GitHub API (stars).
  - **Startups →** the open YC (yc-oss) JSON mirror.
  - **Jobs →** Arbeitnow / RemoteOK / Greenhouse / Ashby JSON APIs.
  - **News →** publisher RSS/Atom feeds + the Hacker News Algolia API.
  None of these need a browser or any bypass — they are structured and sanctioned.

The only place real scraping is unavoidable is a **product's own website** (for
`pricingModel`, which exists in no API), and that is where the ladder below
applies.

## 2. The escalation ladder (`src/crawler/ladder.py`)

`fetch_with_ladder(url, policy)` tries levels in order and records which it used:

| Level | What it does | When |
|---|---|---|
| **L0** | Plain `AsyncFetcher` GET (polite headers, cache, conditional) | Always first |
| **L1** | `BrowserFetcher` (real headless Chromium) renders the page | Only if L0 is **js_only** (visible text < ~300 chars) **and** the source policy allows rendering |
| **L2** | **Stop.** Record `blocked_by_challenge` | A challenge (Cloudflare "Just a moment", `cf-chl`, Datadome, captcha, hard 403/503) is detected at **any** level |

Policy is per-source in `config/sources.yaml`: `render: never | auto | always`,
plus `respect_robots` and `max_rate`. `auto` is the default and only renders when
a page is actually starved of text. A source that disallows automated access or
whose terms forbid AI collection is set `render: never` and is never rendered.

**The browser is honest** (`src/crawler/browser.py`): one shared Chromium, a
small pool of reusable contexts, a global concurrency cap and a per-domain rate
limit; a standard viewport, a consistent desktop-Chromium User-Agent, and
locale/timezone from config. It **blocks images, fonts, media and known
trackers** to save bandwidth. There are **no stealth plugins, no fingerprint
spoofing, no captcha solvers**. On a challenge it closes the page and returns
`blocked` immediately — it never waits the challenge out or retries with tricks.
Transient errors (navigation timeouts) get a short backoff retry; a challenge
never does.

### Measured results (from `scripts/demo_antibot.py`, run once, polite)

- **8 js_only / http_error startups → ladder.** All escalated L0→L1 (rendered),
  and **all stayed `still_js_only`** — these are defunct/placeholder YC domains
  (e.g. `99dresses.com`, `222.place`), so rendering honestly recovers nothing.
  This is the expected, reported outcome: **rendering fixes JS, not dead sites.**
  No challenges were hit on these.
- **futuretools.io** (JS tool directory, robots `Allow: /`). L0 already returned
  **7,524 chars / 102 anchors**; rendering produced the same text and surfaced
  **40 tool-item links** — i.e. this site serves enough server HTML that L0
  suffices and `auto` would not even escalate.
- **Challenge handling.** A local Cloudflare-style "Just a moment" (HTTP 503)
  page was **detected at L0, the ladder stopped (`blocked_by_challenge`), made
  no retries and no bypass.** The reason returned tells the caller to use an
  alternative source.
- **Totals:** 19 requests, ~72 s, **0 LLM calls**.

## 3. What we do NOT do, and why

- **No captcha solving, no fingerprint/stealth spoofing, no residential-proxy
  evasion of a block, no ignoring robots.txt or a site's terms.** These are
  adversarial, brittle, and often against the site's terms — and they are not
  needed, because our bulk data comes from APIs.
- **When a site refuses, the answer is an alternative source**, not a harder
  attack: the official API (e.g. Product Hunt's token GraphQL instead of its
  403-ing website), an RSS feed, a partner feed, or a public archive. The recon
  notes record `theresanaiforthat.com` as Cloudflare-fronted **with a
  content-signals license that forbids AI collection** — so it is `render:
  never`/skip, replayed from notes in the demo **without sending it a request.**

## 4. Scaling to hundreds of thousands of pages

The design separates cheap L0 work from expensive L1 rendering so each scales
independently:

- **Browser rendering becomes its own autoscaled service** — a pool of Chromium
  workers behind a queue, scaled on queue depth, entirely separate from the
  async HTTP fetchers. 95%+ of URLs never reach it.
- **Per-domain budgets in a shared store** (Redis) so every worker/node honours
  one global rate per domain — the token bucket moves from in-process to shared.
- **Proxy pools for geography and rate distribution only where the site's terms
  allow it** — for legitimate rate spreading, never to evade a block.
- **Domain health scoring:** track success/challenge/latency per domain; **auto-
  demote domains that keep returning challenges** (stop rendering them, route to
  an alternative source or a manual review queue) and auto-promote recovered
  ones.
- **Idempotent, resumable work** (the seen-URL / content-hash ledger already in
  SQLite → a shared DB) so distributed nodes never double-process a page.

## 5. Which sources needed which level (this project)

| Source | Level | Note |
|---|---|---|
| arXiv, GitHub, YC JSON, ATS/job APIs, publisher RSS, HN Algolia | **L0** | Official/open — no rendering, no blocks |
| Product homepages (pricing) | **L0**, minority **L1** | Most return server HTML; js_only homepages escalate to the browser |
| `futuretools.io` | **L0** (L1 available) | Serves enough HTML; `auto` wouldn't escalate |
| Product Hunt website | **alternative** | Website 403s automated clients → use the token API |
| `theresanaiforthat.com` | **L2 / skip** | Cloudflare + content-signals license forbids AI collection → alternative source only |
