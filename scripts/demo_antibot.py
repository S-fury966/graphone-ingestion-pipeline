"""Phase V anti-bot LADDER demo (small, polite). Run once:

    .venv/bin/python scripts/demo_antibot.py

It:
  a) runs 8 js_only / http_error startups (from a read-only snapshot of
     state.db) through the ladder and reports before/after text + challenges;
  b) renders futuretools.io (JS site, robots allow /) — L0 vs rendered;
  c) shows challenge handling against a LOCAL Cloudflare-style server (detect,
     stop, no retries), and replays the saved recon finding for the
     Cloudflare-protected directory WITHOUT sending it any request;
  d) prints a timing + request-count summary.

No LLM calls. Honest: some startups will still fail — that is reported.
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from pathlib import Path

from aiohttp import web
from bs4 import BeautifulSoup

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.crawler.browser import BrowserFetcher
from src.crawler.fetcher import AsyncFetcher
from src.crawler.ladder import (BLOCKED_BY_CHALLENGE, LadderPolicy,
                                fetch_with_ladder, visible_text)
from src.resolver.mapping_log import _consistent_copy

STATE_DB = "data/state.db"
COPY_DB = "data/state_copy.db"
CF_HTML = ("<html><head><title>Just a moment...</title></head><body>"
           "<div class='cf-chl-bypass'>Checking your browser before accessing"
           "</div></body></html>")

REQS = {"l0": 0, "l1": 0}


def pick_startups(n=8):
    """js_only first, then http_error, from a fresh read-only snapshot."""
    _consistent_copy(STATE_DB, COPY_DB)  # backup API: never locks the live writer
    con = sqlite3.connect(f"file:{COPY_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT s.slug, s.website, c.status FROM crawl_status c "
        "JOIN startups s ON s.slug = c.slug "
        "WHERE c.status IN ('js_only','http_error') AND s.website IS NOT NULL "
        "ORDER BY CASE c.status WHEN 'js_only' THEN 0 ELSE 1 END, s.slug").fetchall()
    con.close()
    return rows[:n]


async def run_one(http, br, url):
    REQS["l0"] += 1
    r = await fetch_with_ladder(url, LadderPolicy(render="auto"), http, br)
    if "L1" in r.levels_tried:
        REQS["l1"] += 1
    return r


async def section_a(http, br):
    print("\n=== (a) 8 js_only / http_error startups through the ladder ===")
    rows = pick_startups(8)
    if not rows:
        print("  (no js_only/http_error startups in the snapshot)")
        return
    print(f"{'url':42} {'levels':9} {'final':5} {'L0len':>6} {'L1len':>6} chal")
    for row in rows:
        url = row["website"]
        if "://" not in url:
            url = "https://" + url
        try:
            r = await asyncio.wait_for(run_one(http, br, url), timeout=45)
            l1 = "" if r.text_len_l1 is None else r.text_len_l1
            print(f"{url[:42]:42} {'>'.join(r.levels_tried):9} {r.final_level:5} "
                  f"{r.text_len_l0:>6} {str(l1):>6} {'YES' if r.blocked else 'no'}"
                  f"  [{r.status}]")
        except Exception as exc:  # noqa: BLE001
            print(f"{url[:42]:42} ERROR  {type(exc).__name__}: {exc}")


async def section_b(http, br):
    print("\n=== (b) futuretools.io — JS site (robots allow /) ===")
    url = "https://www.futuretools.io/"
    try:
        r0 = await http.fetch_text(url, respect_robots=True)
        REQS["l0"] += 1
        l0_text = len(visible_text(r0.text)) if r0.text else 0
        l0_links = len(BeautifulSoup(r0.text or "", "lxml").find_all("a", href=True))
        rr = await br.fetch(url, respect_robots=True)
        REQS["l1"] += 1
        r1_text = len(visible_text(rr.text)) if rr.text else 0
        soup = BeautifulSoup(rr.text or "", "lxml")
        r1_links = len(soup.find_all("a", href=True))
        tool_links = len([a for a in soup.find_all("a", href=True)
                          if re.search(r"/tools?/", a["href"])])
        print(f"  L0 (plain)   : text={l0_text:>6} chars, anchors={l0_links}")
        print(f"  L1 (rendered): text={r1_text:>6} chars, anchors={r1_links}, "
              f"tool-item links={tool_links}, blocked={rr.blocked}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ERROR: {type(exc).__name__}: {exc}")


async def section_c(http, br):
    print("\n=== (c) challenge handling ===")
    # c1: LOCAL Cloudflare-style server
    app = web.Application()
    app.router.add_get("/robots.txt", lambda _: web.Response(text="User-agent: *\nAllow: /\n"))
    app.router.add_get("/", lambda _: web.Response(text=CF_HTML, status=503,
                                                   content_type="text/html"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = list(runner.addresses)[0][1]
    url = f"http://127.0.0.1:{port}/"
    r = await fetch_with_ladder(url, LadderPolicy(render="auto", respect_robots=False), http, br)
    REQS["l0"] += 1
    if "L1" in r.levels_tried:
        REQS["l1"] += 1
    print(f"  local challenge: status={r.status} final_level={r.final_level} "
          f"blocked={r.blocked} levels={r.levels_tried}")
    print(f"    reason: {r.reason}")
    assert r.status == BLOCKED_BY_CHALLENGE and r.blocked
    print("    -> detected, STOPPED, no retries, no bypass. OK")
    await runner.cleanup()

    # c2: replay the saved recon finding (NO new request to the live site)
    recon = Path("docs/source_recon.md").read_text(encoding="utf-8")
    block = [ln.strip() for ln in recon.splitlines()
             if "theresanaiforthat" in ln.lower() or "content signals" in ln.lower()
             or "cloudflare-fronted" in ln.lower()]
    print("  recon replay (theresanaiforthat.com — NO request sent; terms forbid AI):")
    for ln in block[:4]:
        print(f"    | {ln[:100]}")


async def main():
    t0 = time.monotonic()
    async with AsyncFetcher(respect_robots=True, rate_limit_per_sec=1.0) as http, \
            BrowserFetcher(global_concurrency=2, context_pool_size=2,
                           rate_limit_per_sec=0.7, respect_robots=True,
                           robots_fetcher=http) as br:
        await section_a(http, br)
        await section_b(http, br)
        await section_c(http, br)
    dt = time.monotonic() - t0
    print("\n=== (d) timing + request-count summary ===")
    print(f"  wall time     : {dt:.1f}s")
    print(f"  L0 fetches    : {REQS['l0']}")
    print(f"  L1 renders    : {REQS['l1']}")
    print(f"  total requests: {REQS['l0'] + REQS['l1']}  (no LLM calls)")


if __name__ == "__main__":
    asyncio.run(main())
