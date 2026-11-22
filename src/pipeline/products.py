"""Products pipeline: crawl each startup's site, LLM-extract products, store grounded.

Run: python -m src.pipeline.products --limit N --max-llm-calls M --concurrency C
     [--retry-failed] [--reset-products] [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from functools import lru_cache

from ..common.logging import get_logger
from ..crawler.fetcher import AsyncFetcher
from ..crawler.product_pages import OK, CrawlResult, crawl_startup
from ..llm.chunker import fit
from ..llm.config import load_llm_config
from ..llm.grounding import quote_grounded
from ..llm.orchestrator import Orchestrator
from ..resolver.normalize import normalize_name
from ..schemas import Product
from ..schemas.enums import PricingModel
from ..storage.db import Database

log = get_logger("pipeline.products")

CSV_PATH = "data/products.csv"
JSONL_PATH = "data/products.jsonl"
SOURCE_NAME = "Company website"
FILTERS_PATH = "config/product_filters.yaml"
FOCUS = ["pricing", "plan", "free", "per month", "enterprise", "freemium", "subscription"]

# A trailing word from this set turns a name into a plan-tier variant
# ("Fabriq Developer", "Fabriq Enterprise", "Acme Pro"): we drop the tier word
# and keep the single base product. Union'd with the config stoplist at runtime.
_TIER_SUFFIX_WORDS = {
    "developer", "enterprise", "team", "teams", "business", "personal",
    "individual", "free", "pro", "plus", "max", "premium", "starter",
    "growth", "scale", "standard",
}

# A pricing_model's evidence must mention real price/plan wording, else we null it.
_PRICING_TOKENS = (
    "$", "€", "£", "/mo", "/month", "per month", "per user", "per seat",
    "/yr", "per year", "price", "pricing", "plan", "free", "paid",
    "subscription", "trial", "contact sales", "contact us", "quote",
)

# Rank for merging pricing when tier variants collapse onto one base product.
_PRICING_RANK = {None: 0, "FREE": 1, "PAID": 2, "ENTERPRISE": 2, "FREEMIUM": 3}

# --- FREE-label discipline ---------------------------------------------------
# FREE means the product genuinely costs nothing. Only these phrases are strong
# enough to assert FREE on their own.
_STRONG_FREE_PHRASES = (
    "free forever", "forever free", "completely free", "100% free",
    "always free", "free for everyone", "free and open source",
    "free, open source", "open-source", "open source",
)
# Free-ENTRY phrases: a free way in, but NOT proof the product is free. On their
# own -> null; alongside a paid signal -> FREEMIUM.
_WEAK_FREE_PHRASES = (
    "free to start", "start free", "get started free", "get started for free",
    "try for free", "free trial", "start for free", "free to try",
    "free plan", "free tier", "free credits", "usage credits", "in usage credits",
    "no credit card", "free to start.", "free to get started",
)
# A paid plan / price exists only when a currency amount is TIED to plan/billing
# wording, or an explicit charge phrase appears. A bare "$" is not enough:
# "$20 in usage credits" is NOT a paid plan.
_BILLING_WORDS = (
    "per month", "per year", "/mo", "/month", "/yr", "per seat", "per user",
    "/seat", "/user", "plan", "tier", "starting at", "per request",
)
# Charge phrases that imply payment on their own (no currency amount needed).
_EXPLICIT_PAID_PHRASES = (
    "contact sales", "contact us for pricing", "subscription",
    "billed monthly", "billed annually", "billed yearly",
)
_CURRENCY_AMOUNT = re.compile(r"[$€£]\s?\d")


def _has_any(text: Optional[str], needles) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(n in t for n in needles)


def _has_paid_signal(text: Optional[str]) -> bool:
    """True only when the text shows a real paid plan/price.

    A currency amount counts as a price only if plan/billing wording is also
    present; 'usage credits' / 'free credits' alone never count as paid.
    """
    if not text:
        return False
    t = text.lower()
    if any(p in t for p in _EXPLICIT_PAID_PHRASES):
        return True
    if _CURRENCY_AMOUNT.search(t) and any(b in t for b in _BILLING_WORDS):
        return True
    return False


def refine_free_pricing(pm_value: Optional[str], evidence: Optional[str],
                        context_text: Optional[str] = None) -> Optional[str]:
    """Downgrade an over-eager FREE label by the strong/weak-evidence rules.

    Only acts on FREE; other labels pass through unchanged.
      - strong free evidence and no paid signal        -> FREE
      - a free entry point AND a paid signal both seen  -> FREEMIUM
      - only a weak free-entry phrase (or nothing)      -> null
    `context_text` widens the search (e.g. the product's page); defaults to the
    evidence quote alone (used by the no-LLM audit over stored evidence).
    """
    if pm_value != "FREE":
        return pm_value
    ctx = " ".join(x for x in (evidence, context_text) if x)
    strong = _has_any(ctx, _STRONG_FREE_PHRASES)
    weak = _has_any(ctx, _WEAK_FREE_PHRASES)
    paid = _has_paid_signal(ctx)
    if paid and (strong or weak):
        return "FREEMIUM"
    if strong and not paid:
        return "FREE"
    return None


def collapse_tier_name(name: str, extra_tiers: Optional[set[str]] = None) -> Optional[str]:
    """If `name` is "<base> <tier-word>", return the base; else None.

    e.g. "Fabriq Developer"/"Fabriq Enterprise" -> "Fabriq", "Acme Pro" -> "Acme".
    Single-word names are never collapsed.
    """
    words = name.split()
    if len(words) < 2:
        return None
    last = words[-1].lower().strip(".,:;()[]")
    tiers = _TIER_SUFFIX_WORDS | (extra_tiers or set())
    if last in tiers:
        base = " ".join(words[:-1]).strip(" -–—")
        return base or None
    return None


def _pricing_quote_mentions_price(quote: Optional[str]) -> bool:
    if not quote:
        return False
    q = quote.lower()
    return any(tok in q for tok in _PRICING_TOKENS)


def _merge_pricing(rec: dict, pm_value: Optional[str], pricing_ev: Optional[str]) -> None:
    """Fold a tier variant's pricing into the base product's record.

    free tier + paid tier => FREEMIUM; otherwise keep the higher-rank signal.
    """
    cur = rec["pricing_model"]
    vals = {cur, pm_value} - {None}
    if "FREE" in vals and ({"PAID", "ENTERPRISE", "FREEMIUM"} & vals):
        rec["pricing_model"] = "FREEMIUM"
        if pm_value in ("PAID", "ENTERPRISE", "FREEMIUM"):
            rec["evidence_pricing"] = pricing_ev or rec["evidence_pricing"]
        return
    if _PRICING_RANK.get(pm_value, 0) > _PRICING_RANK.get(cur, 0):
        rec["pricing_model"] = pm_value
        rec["evidence_pricing"] = pricing_ev

# Verbs that commonly begin a tagline/sentence rather than a product name.
_VERB_START = {
    "build", "builds", "create", "creates", "manage", "manages", "get", "make",
    "makes", "find", "finds", "discover", "automate", "automates", "generate",
    "generates", "turn", "turns", "transform", "transforms", "help", "helps",
    "enable", "enables", "power", "powers", "bring", "brings", "deliver",
    "delivers", "connect", "connects", "track", "tracks", "learn", "explore",
    "unlock", "boost", "grow", "streamline", "simplify", "accelerate", "analyze",
    "understand", "move", "moving", "let", "lets", "run", "runs", "ship", "ships",
}

INSTRUCTIONS = (
    "From the company's own web pages below, list the real PRODUCTS the company "
    "sells or ships, each referred to by a proper name. Return at most 3; fewer "
    "if unsure. Fewer, correct products beat many.\n\n"
    "A product is NOT any of these — never list them as products:\n"
    "- pricing plans or tiers (Free, Pro, Plus, Max, Basic, Starter, Team, "
    "Business, Premium, Standard, Extra, Perks, Individual, and similar)\n"
    "- taglines, slogans, or descriptions\n"
    "- feature names, integrations, customer names, job titles, or blog posts.\n\n"
    "If the company has a SINGLE product with no separate name on the page, "
    "return the company's own name as the product — but only if that name "
    "actually appears in the text.\n\n"
    "When plan tiers exist, use them to decide pricing_model (free tier + paid "
    "tier = FREEMIUM; paid only = PAID; contact-sales only = ENTERPRISE) and do "
    "NOT list the tiers as products.\n\n"
    "Use FREE only with strong evidence the product costs nothing: wording like "
    "'free forever', 'completely free', '100% free', or open source with no paid "
    "plan or price anywhere in the text. Phrases like 'free to start', 'get "
    "started free', 'try for free', 'free trial', or usage credits are NOT "
    "enough for FREE: if a paid plan or price also appears use FREEMIUM, and if "
    "only such a weak free-entry phrase appears use null.\n\n"
    "For each product give:\n"
    "- name: the product's proper name exactly as written\n"
    "- pricing_model: one of FREE, FREEMIUM, PAID, ENTERPRISE, or null\n"
    "- name_evidence: a short verbatim quote from the page containing the name\n"
    "- pricing_evidence: a short verbatim quote supporting pricing_model (null if none)\n"
    "Use null for pricing_model unless the page clearly supports it. Do not use "
    "outside knowledge.\n"
    'Return JSON exactly: {"products": [{"name": ..., "pricing_model": ..., '
    '"name_evidence": ..., "pricing_evidence": ...}]}. No other keys.'
)


class LLMProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = None
    pricing_model: Optional[PricingModel] = None
    name_evidence: Optional[str] = None
    pricing_evidence: Optional[str] = None


class LLMProductList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    products: List[LLMProduct] = Field(default_factory=list)


@lru_cache(maxsize=4)
def load_chunk_budget(path: str = FILTERS_PATH) -> tuple[int, int]:
    """(homepage_tokens, pricing_tokens) for the per-call product input."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        cb = data.get("chunk_budget", {}) or {}
        return int(cb.get("homepage_tokens", 2500)), int(cb.get("pricing_tokens", 1500))
    except Exception:  # noqa: BLE001
        return 2500, 1500


def build_llm_input(crawl: CrawlResult, homepage_tokens: int, pricing_tokens: int) -> str:
    """Per-page, budget-trimmed input: homepage then pricing page, each keeping
    title/meta/og, the hero and headings/pricing blocks, trimming the rest.

    Fits from raw HTML when available; falls back to the crawled visible text."""
    sources = crawl.html_pages if crawl.html_pages else crawl.pages
    segs: List[str] = []
    for i, (url, body) in enumerate(sources):
        budget = homepage_tokens if i == 0 else pricing_tokens
        lead = 2 if i == 0 else 1
        fitted = fit(body, budget, FOCUS, always_lead=lead)
        # Drop the "## " heading markers so evidence quotes stay verbatim against
        # the crawled visible text (used by the grounding/post-filter checks).
        fitted = re.sub(r"(?m)^##\s+", "", fitted)
        segs.append(f"[PAGE: {url}]\n{fitted}")
    return "\n\n".join(segs)


def load_plan_tiers(path: str = FILTERS_PATH) -> set[str]:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return {str(t).strip().lower() for t in data.get("plan_tiers", [])}
    except Exception:  # noqa: BLE001
        return set()


def reject_reason(name: str, startup_name: str, tagline: Optional[str],
                  plan_tiers: set[str]) -> Optional[str]:
    """Return a rejection reason for a product name, or None if acceptable.

    The single-product fallback (name == startup's own name) bypasses all rules.
    """
    n = name.strip()
    if not n:
        return "empty"
    norm = normalize_name(n)
    startup_norm = normalize_name(startup_name)

    # Single-product fallback: the company's own name is always a valid product.
    if norm and norm == startup_norm:
        return None

    words = n.split()
    nlow = n.lower()

    if len(words) > 6 or len(n) > 60:
        return "too_long"
    if n.endswith(".") or "," in n or (words and words[0].lower().strip(".") in _VERB_START):
        return "sentence_like"
    # Exact plan-tier match, unless the name also contains the startup's name.
    if nlow in plan_tiers and (not startup_norm or startup_norm not in norm):
        return "plan_tier"
    if tagline and normalize_name(tagline) and norm == normalize_name(tagline):
        return "equals_tagline"
    # Generic category phrase: no proper-name token (nothing capitalized).
    if not any(c.isupper() for c in n):
        return "generic_no_proper_name"
    return None


@dataclass
class Stats:
    attempted: int = 0
    crawl_status: Counter = field(default_factory=Counter)
    pricing_nulled: int = 0
    dropped_hallucinated: int = 0
    rejections: Counter = field(default_factory=Counter)
    invalid: int = 0
    products_added: int = 0
    pricing_dist: Counter = field(default_factory=Counter)
    # Startups whose extraction could not run because ALL enabled tiers failed or
    # were parked (a 429 that later succeeds on another tier is NOT counted).
    deferred: int = 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pick_page(evidence: Optional[str], pages, default_url: str, default_text: str):
    for url, text in pages:
        if quote_grounded(evidence, text):
            return url, text
    return default_url, default_text


def _pricing_page_ok(pricing_ev: Optional[str], product_page_text: str, pages) -> bool:
    """Price evidence must sit on the product's own page or a pricing/plans page."""
    if quote_grounded(pricing_ev, product_page_text):
        return True
    for url, text in pages:
        u = url.lower()
        if ("pricing" in u or "/plans" in u or "/plan" in u) and quote_grounded(pricing_ev, text):
            return True
    return False


def _to_product_model_dict(startup_name: str, product_name: str,
                           pricing: Optional[str], page_url: str, collected_at: str) -> dict:
    return {
        "schemaVersion": "1.0",
        "recordType": "PRODUCT",
        "source": {"name": SOURCE_NAME, "url": page_url},
        "collectedAt": collected_at,
        "content": {"startupName": startup_name, "name": product_name,
                    "pricingModel": pricing},
    }


async def process_startup(db: Database, orch: Orchestrator, crawl: CrawlResult,
                          startup_name: str, tagline: Optional[str], stats: Stats,
                          plan_tiers: Optional[set[str]] = None,
                          dry_run: bool = False) -> int:
    """Extract + store grounded, filtered products for one startup. Returns added."""
    plan_tiers = plan_tiers if plan_tiers is not None else load_plan_tiers()
    stats.crawl_status[crawl.status] += 1
    if crawl.status != OK:
        if not dry_run:
            db.set_crawl_status(crawl.slug, crawl.status, crawl.pages_fetched, crawl.error)
        return 0

    hp, pp = load_chunk_budget()
    llm_input = build_llm_input(crawl, hp, pp)
    result = await orch.extract(f"products:{crawl.slug}", LLMProductList,
                                llm_input, INSTRUCTIONS, focus_keywords=FOCUS)
    if not result.success:
        # A real deferral = no enabled tier could serve the call (all failed or
        # parked). The deliberate call-budget stop is NOT a deferral.
        if not (result.error and "budget" in result.error):
            stats.deferred += 1
        log.warning("extraction deferred (all tiers failed/parked)", extra={"extra": {
            "slug": crawl.slug, "reason": result.error}})
        return 0
    if not result.data:
        return 0

    default_url = crawl.pages[0][0]
    default_text = crawl.combined_text
    added = 0
    merged: dict[str, dict] = {}  # normalized_name -> record (deduped, tier-collapsed)
    for item in result.data.get("products", [])[:6]:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        name_ev = item.get("name_evidence")
        # 1) grounding: name evidence must be verbatim in the crawled text.
        if not quote_grounded(name_ev, crawl.combined_text):
            stats.dropped_hallucinated += 1
            continue
        # 2) collapse plan-tier variants ("Fabriq Enterprise" -> "Fabriq").
        base = collapse_tier_name(name, plan_tiers)
        if base is not None:
            if base.lower() not in crawl.combined_text.lower():
                stats.rejections["tier_base_absent"] += 1
                continue
            stats.rejections["compound_tier"] += 1
            name = base
        # 3) deterministic post-filters.
        reason = reject_reason(name, startup_name, tagline, plan_tiers)
        if reason:
            stats.rejections[reason] += 1
            log.info("product rejected by filter", extra={"extra": {
                "slug": crawl.slug, "name": name, "reason": reason}})
            continue

        norm = normalize_name(name)
        if not norm:
            continue

        # 4) pricing: must mention real price/plan wording AND be grounded on the
        #    product's own page (or a pricing/plans page); otherwise null.
        page_url, page_text = _pick_page(name_ev, crawl.pages, default_url, default_text)
        pricing = item.get("pricing_model")
        pm_value = pricing.value if isinstance(pricing, PricingModel) else pricing
        pricing_ev = item.get("pricing_evidence")
        if pm_value is not None and not (
                _pricing_quote_mentions_price(pricing_ev)
                and _pricing_page_ok(pricing_ev, page_text, crawl.pages)):
            pm_value = None
            pricing_ev = None
            stats.pricing_nulled += 1
        # FREE-label discipline: downgrade weakly-supported FREE to FREEMIUM/null,
        # checking the whole crawled text for a paid signal.
        if pm_value == "FREE":
            refined = refine_free_pricing(pm_value, pricing_ev, crawl.combined_text)
            if refined != pm_value:
                stats.rejections["free_downgraded"] += 1
                pm_value = refined
                if pm_value is None:
                    pricing_ev = None

        if norm in merged:
            _merge_pricing(merged[norm], pm_value, pricing_ev)
            continue
        if len(merged) >= 3:  # at most 3 distinct products
            continue
        merged[norm] = {
            "startup_slug": crawl.slug, "normalized_name": norm,
            "startup_name": startup_name, "product_name": name,
            "pricing_model": pm_value, "evidence_name": name_ev,
            "evidence_pricing": pricing_ev, "page_url": page_url,
            "source_name": SOURCE_NAME, "llm_tier": result.tier_used,
        }

    for rec in merged.values():
        collected_at = _now_iso()
        try:
            Product.model_validate(_to_product_model_dict(
                startup_name, rec["product_name"], rec["pricing_model"],
                rec["page_url"], collected_at))
        except ValidationError as exc:
            stats.invalid += 1
            log.warning("invalid product skipped", extra={"extra": {
                "slug": crawl.slug, "name": rec["product_name"], "reason": str(exc)}})
            continue
        rec["extracted_at"] = collected_at
        rec["collected_at"] = collected_at
        if dry_run:
            added += 1
            stats.pricing_dist[rec["pricing_model"] or "null"] += 1
        elif db.insert_product(rec):
            added += 1
            stats.pricing_dist[rec["pricing_model"] or "null"] += 1

    if not dry_run:
        db.set_crawl_status(crawl.slug, OK, crawl.pages_fetched)
    stats.products_added += added
    return added


def _startups_with_website(db: Database) -> List[Any]:
    return [row for row in db.iter_startups() if row["website"]]


def _select_startups(db: Database, limit: int, retry_failed: bool) -> List[Any]:
    out = []
    for row in _startups_with_website(db):
        st = db.get_crawl_status(row["slug"])
        if st is None or (retry_failed and st != OK):
            out.append(row)
        if len(out) >= limit:
            break
    return out


def _orch_429(orch: Orchestrator) -> int:
    """Total 429/rate-limited responses seen across all tiers this run."""
    return sum(m.rate_limited for m in orch.metrics.values())


def _export(db: Database) -> int:
    models: List[Product] = []
    for row in db.iter_products():
        models.append(Product.model_validate(_to_product_model_dict(
            row["startup_name"], row["product_name"], row["pricing_model"],
            row["page_url"], row["collected_at"])))
    Path("data").mkdir(parents=True, exist_ok=True)
    with open(JSONL_PATH, "w", encoding="utf-8") as fh:
        for m in models:
            fh.write(m.to_json() + "\n")
    flats = [m.to_flat_dict() for m in models]
    fields_order: List[str] = []
    for f in flats:
        for k in f:
            if k not in fields_order:
                fields_order.append(k)
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields_order)
        w.writeheader()
        w.writerows(flats)
    log.info("exported", extra={"extra": {"rows": len(models),
             "csv": CSV_PATH, "jsonl": JSONL_PATH}})
    return len(models)


def audit_pricing(db: Database, apply: bool = True) -> List[dict]:
    """Re-check every product's pricing_model against its STORED pricing evidence
    (no LLM). Returns the rows whose label changes; applies + re-exports if asked.
    """
    changes: List[dict] = []
    for row in db.iter_products():
        old = row["pricing_model"]
        if old != "FREE":
            continue  # only FREE is over-eager; other labels are left untouched
        new = refine_free_pricing(old, row["evidence_pricing"])
        if new != old:
            changes.append({
                "startup": row["startup_name"], "product": row["product_name"],
                "old": old, "new": new,
                "evidence": (row["evidence_pricing"] or "")[:160],
                "slug": row["startup_slug"], "norm": row["normalized_name"]})
    if apply and changes:
        for c in changes:
            db.update_product_pricing(c["slug"], c["norm"], c["new"])
        _export(db)
    return changes


async def run(args: argparse.Namespace) -> None:
    db = Database(args.db)
    plan_tiers = load_plan_tiers()

    # --reset-products: wipe products + crawl_status for exactly the N startups
    # that would be processed (first N with a website, in order), nothing else.
    if args.reset_products:
        targets = _startups_with_website(db)[: args.limit]
        for row in targets:
            db.delete_products_for(row["slug"])
            db.delete_crawl_status(row["slug"])
        log.info("reset products", extra={"extra": {"startups": len(targets)}})

    startups = _select_startups(db, args.limit, args.retry_failed)
    stats = Stats()

    fetcher = AsyncFetcher(global_concurrency=max(4, args.concurrency),
                           domain_concurrency=1, rate_limit_per_sec=1.0,
                           respect_robots=True)
    config = load_llm_config()
    orch = Orchestrator(config, fetcher=fetcher, db=db, max_calls=args.max_llm_calls)

    # --- windowed progress + safety stops over rolling blocks of WINDOW startups ---
    WINDOW = 50
    BAD_STATUSES = {"http_error", "blocked", "js_only"}
    rows_iter = iter(startups)
    lock = asyncio.Lock()
    stop_event = asyncio.Event()
    t0 = datetime.now(timezone.utc)
    prog = {"processed": 0, "win_calls0": 0, "win_429_0": 0, "win_bad": 0,
            "win_deferred0": 0, "last_export": 0, "stop_reason": None}

    def _elapsed_min() -> float:
        return max((datetime.now(timezone.utc) - t0).total_seconds() / 60.0, 1e-6)

    def _per_tier() -> dict:
        return {name: {"calls": m.calls, "tokens_in": m.tokens_in,
                       "tokens_out": m.tokens_out, "rate_limited": m.rate_limited}
                for name, m in orch.metrics.items() if m.calls or m.rate_limited}

    async def after_one(status: str) -> None:
        async with lock:
            prog["processed"] += 1
            if status in BAD_STATUSES:
                prog["win_bad"] += 1
            n = prog["processed"]

            # Periodic export so an interruption never loses the CSV/JSONL.
            if not args.dry_run and n - prog["last_export"] >= 200:
                _export(db)
                prog["last_export"] = n

            if n % WINDOW == 0:
                win_calls = orch.calls_made - prog["win_calls0"]
                win_429 = _orch_429(orch) - prog["win_429_0"]
                win_deferred = stats.deferred - prog["win_deferred0"]
                deferred_frac = win_deferred / WINDOW
                bad_frac = prog["win_bad"] / WINDOW
                log.info("PROGRESS", extra={"extra": {
                    "processed": n,
                    "window": WINDOW,
                    "window_deferred": win_deferred,       # all-tier failures/parks
                    "window_deferred_frac": round(deferred_frac, 3),
                    "window_429": win_429,                 # informational only
                    "window_llm_calls": win_calls,
                    "window_bad_frac": round(bad_frac, 3),
                    "calls_per_min": round(orch.calls_made / _elapsed_min(), 2),
                    "products_added": stats.products_added,
                    "crawl_status": dict(stats.crawl_status),
                    "per_tier": _per_tier()}})
                # Stop ONLY on genuine all-tier deferrals or too many bad crawls.
                # A 429 that later succeeds on another tier is not a failure.
                if deferred_frac > 0.10:
                    prog["stop_reason"] = (
                        f"{deferred_frac:.1%} of last {WINDOW} startups deferred "
                        f"(all enabled tiers failed/parked: {win_deferred}/{WINDOW})")
                    stop_event.set()
                elif bad_frac > 0.40:
                    prog["stop_reason"] = (
                        f"{bad_frac:.1%} of last {WINDOW} startups ended "
                        f"http_error/blocked/js_only (> 40%)")
                    stop_event.set()
                # reset window accumulators
                prog["win_calls0"] = orch.calls_made
                prog["win_429_0"] = _orch_429(orch)
                prog["win_deferred0"] = stats.deferred
                prog["win_bad"] = 0

    async def worker() -> None:
        while not stop_event.is_set():
            # All LLM tiers parked (daily caps) -> stop cleanly, report earliest.
            if not orch.any_tier_usable():
                soonest = orch.soonest_tier_reset()
                async with lock:
                    if prog["stop_reason"] is None:
                        if soonest is not None:
                            when = datetime.fromtimestamp(soonest, timezone.utc).isoformat()
                            secs = max(0, round(soonest - datetime.now(timezone.utc).timestamp()))
                            prog["stop_reason"] = (
                                f"all LLM tiers parked (daily caps); earliest tier "
                                f"returns in ~{secs}s at {when}")
                        else:
                            prog["stop_reason"] = "no LLM tier available"
                stop_event.set()
                return
            row = next(rows_iter, None)
            if row is None or orch.budget_reached():
                return
            crawl = await crawl_startup(fetcher, row["slug"], row["website"])
            stats.attempted += 1
            await process_startup(db, orch, crawl, row["entity_name"], row["one_liner"],
                                  stats, plan_tiers=plan_tiers, dry_run=args.dry_run)
            await after_one(crawl.status)

    async with fetcher:
        await asyncio.gather(*(worker() for _ in range(max(1, args.concurrency))))

    if prog["stop_reason"]:
        log.warning("STOPPED EARLY", extra={"extra": {"reason": prog["stop_reason"],
                    "processed": prog["processed"]}})

    if not args.dry_run:
        _export(db)

    tier_calls = {name: {"calls": m.calls, "rate_limited": m.rate_limited,
                         "tokens_in": m.tokens_in, "tokens_out": m.tokens_out}
                  for name, m in orch.metrics.items() if m.calls or m.rate_limited}
    cache_hits = sum(m.cache_hits for m in orch.metrics.values())
    log.info("RUN SUMMARY", extra={"extra": {
        "startups_attempted": stats.attempted,
        "crawl_status": dict(stats.crawl_status),
        "llm_calls": orch.calls_made,
        "calls_by_tier": tier_calls,
        "cache_hits": cache_hits,
        "pricing_nulled_by_guard": stats.pricing_nulled,
        "dropped_hallucinated": stats.dropped_hallucinated,
        "rejected_by_filter": dict(stats.rejections),
        "invalid": stats.invalid,
        "products_added": stats.products_added,
        "pricing_distribution": dict(stats.pricing_dist),
        "rate_limited_429": _orch_429(orch),
        "deferred_all_tiers": stats.deferred,
        "stopped_early": prog["stop_reason"],
        "final_total_products": db.count_products(),
    }})
    db.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Products ingestion pipeline")
    p.add_argument("--limit", type=int, default=25, help="max startups to process")
    p.add_argument("--max-llm-calls", type=int, default=None, help="global LLM call budget")
    p.add_argument("--concurrency", type=int, default=5, help="concurrent startups")
    p.add_argument("--retry-failed", action="store_true", help="reprocess non-ok startups")
    p.add_argument("--reset-products", action="store_true",
                   help="delete products + crawl_status for the N startups being processed")
    p.add_argument("--dry-run", action="store_true", help="do not persist/export")
    p.add_argument("--audit-pricing", action="store_true",
                   help="re-check stored FREE labels against stored evidence (no LLM), "
                        "apply downgrades, re-export, and exit")
    p.add_argument("--db", default="data/state.db", help="SQLite path")
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.audit_pricing:
        db = Database(args.db)
        changes = audit_pricing(db, apply=not args.dry_run)
        log.info("pricing audit", extra={"extra": {
            "rows_changed": len(changes), "applied": not args.dry_run}})
        for c in changes:
            print(f"{c['startup']} :: {c['product']}: {c['old']} -> {c['new']}  "
                  f"| {c['evidence']!r}")
        print(f"\n{len(changes)} row(s) changed.")
        db.close()
        return
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
