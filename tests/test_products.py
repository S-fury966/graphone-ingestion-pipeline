"""Products vertical tests: crawl classification + grounded extraction (no internet)."""
from pathlib import Path

import pytest

from src.crawler.fetcher import FetchResult
from src.crawler.product_pages import (BLOCKED, HTTP_ERROR, JS_ONLY, NO_WEBSITE,
                                       OK, PARKED, ROBOTS, CrawlResult,
                                       crawl_startup, find_pricing_links,
                                       is_parked)
from src.llm.config import LLMConfig, TierConfig
from src.llm.orchestrator import Orchestrator
from src.llm.providers import MockProvider
from src.pipeline.products import (LLMProductList, Stats, audit_pricing,
                                   collapse_tier_name, load_plan_tiers,
                                   process_startup, refine_free_pricing,
                                   reject_reason)
from src.storage.db import Database

FIX = Path(__file__).parent / "fixtures"
HOME = (FIX / "product_home.html").read_text()
PRICING = (FIX / "pricing_page.html").read_text()
JS = (FIX / "js_only.html").read_text()


def _res(url, text, status=200, blocked=False, error=None):
    return FetchResult(url=url, final_url=url, status=status, text=text,
                       content_type="text/html", blocked=blocked,
                       error=error if error else (None if status < 400 else f"HTTP {status}"))


class FakeFetcher:
    def __init__(self, mapping):
        self.mapping = mapping

    async def fetch_text(self, url, **opts):
        return self.mapping.get(url, _res(url, "", status=404))


# ---------------- pricing link discovery ----------------
def test_find_pricing_links_same_domain_only():
    links = find_pricing_links(HOME, "https://acme.com/")
    assert "https://acme.com/pricing" in links
    assert all("external-partner.com" not in l for l in links)  # cross-domain dropped


# ---------------- crawl classification ----------------
async def test_crawl_ok_with_pricing():
    f = FakeFetcher({"https://acme.com": _res("https://acme.com", HOME),
                     "https://acme.com/pricing": _res("https://acme.com/pricing", PRICING)})
    r = await crawl_startup(f, "acme", "https://acme.com")
    assert r.status == OK
    assert len(r.pages) == 2
    assert "WidgetPro" in r.combined_text and "per user per month" in r.combined_text


async def test_crawl_js_only():
    f = FakeFetcher({"https://spa.com": _res("https://spa.com", JS)})
    r = await crawl_startup(f, "spa", "https://spa.com")
    assert r.status == JS_ONLY


async def test_crawl_blocked():
    f = FakeFetcher({"https://cf.com": _res("https://cf.com", "blocked", status=403, blocked=True)})
    r = await crawl_startup(f, "cf", "https://cf.com")
    assert r.status == BLOCKED


async def test_crawl_robots_disallowed():
    f = FakeFetcher({"https://no.com": _res("https://no.com", None, status=0,
                                            error="disallowed by robots.txt")})
    r = await crawl_startup(f, "no", "https://no.com")
    assert r.status == ROBOTS


async def test_crawl_http_error():
    f = FakeFetcher({"https://err.com": _res("https://err.com", "oops", status=500)})
    r = await crawl_startup(f, "err", "https://err.com")
    assert r.status == HTTP_ERROR


async def test_crawl_no_website():
    r = await crawl_startup(FakeFetcher({}), "x", None)
    assert r.status == NO_WEBSITE


# ---------------- parked / domain-for-sale detection ----------------
def test_is_parked_by_host():
    assert is_parked("https://domains.atom.com/lpd/name/Foo.com", "hello",
                     frozenset({"domains.atom.com"}), ())
    assert is_parked("https://www.sedo.com/search", "x",
                     frozenset({"sedo.com"}), ())            # www + subdomain host


def test_is_parked_by_phrase():
    assert is_parked("https://foo.com", "Great news — this domain is for sale!",
                     frozenset(), ("this domain is for sale",))


def test_not_parked_for_real_site():
    assert not is_parked("https://foo.com", "We build AI agents for finance.",
                         frozenset({"sedo.com"}), ("buy this domain",))


async def test_crawl_marks_parked_page():
    html = "<html><body><p>This domain is for sale. Make an offer today.</p></body></html>"
    f = FakeFetcher({"https://sold.ai": _res("https://sold.ai", html)})
    r = await crawl_startup(f, "sold", "https://sold.ai")
    assert r.status == PARKED            # phrase from config matched -> no LLM call


# ---------------- per-page chunk budget ----------------
def test_build_llm_input_trims_per_page_keeps_title():
    from src.llm.chunker import estimate_tokens
    from src.pipeline.products import build_llm_input
    home = ("<html><head><title>Acme Home</title></head><body><h1>Acme</h1>"
            "<p>" + ("word " * 5000) + "</p></body></html>")
    pricing = "<html><body><h2>Pricing</h2><p>Pro $10 per month</p></body></html>"
    cr = CrawlResult(slug="x", status=OK, pages=[("u1", "t1"), ("u2", "t2")],
                     html_pages=[("https://acme.com", home),
                                 ("https://acme.com/pricing", pricing)])
    out = build_llm_input(cr, 200, 100)
    assert "[PAGE: https://acme.com]" in out
    assert "[PAGE: https://acme.com/pricing]" in out
    assert "Acme Home" in out                       # title always kept
    assert estimate_tokens(out) < 400               # far smaller than the raw page


# ---------------- extraction + grounding ----------------
COMBINED = ("[PAGE: https://acme.com]\nAcme Corp builds WidgetPro and DataSync for teams.\n\n"
            "[PAGE: https://acme.com/pricing]\nWidgetPro Free plan: $0/month. "
            "Pro: $15 per user per month.")


def _crawl_ok(slug="acme"):
    return CrawlResult(slug=slug, status=OK,
                       pages=[("https://acme.com", "Acme Corp builds WidgetPro and DataSync for teams."),
                              ("https://acme.com/pricing", "WidgetPro Free plan: $0/month. Pro: $15 per user per month.")],
                       pages_fetched=2)


def make_orch(db, products, max_calls=None):
    tier = TierConfig(name="m", provider="mock", model="m", api_key_env="",
                      enabled=True, rpm_limit=100000)
    cfg = LLMConfig(prompt_version="1", tiers=[tier])
    script = [{"products": products}]
    return Orchestrator(cfg, db=db, providers={"m": MockProvider(script)}, max_calls=max_calls)


async def test_grounded_product_kept_and_startupname_from_db(tmp_path):
    db = Database(tmp_path / "s.db")
    orch = make_orch(db, [
        {"name": "WidgetPro", "pricing_model": "FREEMIUM",
         "name_evidence": "builds WidgetPro", "pricing_evidence": "Free plan: $0/month"},
    ])
    added = await process_startup(db, orch, _crawl_ok(), "Acme Corp", None, Stats())
    assert added == 1
    row = list(db.iter_products())[0]
    assert row["product_name"] == "WidgetPro"
    assert row["pricing_model"] == "FREEMIUM"
    assert row["startup_name"] == "Acme Corp"      # canonical from DB, not LLM
    db.close()


async def test_hallucinated_product_dropped(tmp_path):
    db = Database(tmp_path / "s.db")
    orch = make_orch(db, [
        {"name": "GhostApp", "pricing_model": "PAID",
         "name_evidence": "GhostApp is amazing", "pricing_evidence": "costs $99"},
    ])
    stats = Stats()
    added = await process_startup(db, orch, _crawl_ok(), "Acme Corp", None, stats)
    assert added == 0
    assert stats.dropped_hallucinated == 1
    db.close()


async def test_pricing_nulled_when_evidence_fails(tmp_path):
    db = Database(tmp_path / "s.db")
    orch = make_orch(db, [
        {"name": "WidgetPro", "pricing_model": "PAID",
         "name_evidence": "builds WidgetPro", "pricing_evidence": "costs nine hundred dollars"},
    ])
    stats = Stats()
    added = await process_startup(db, orch, _crawl_ok(), "Acme Corp", None, stats)
    assert added == 1
    assert stats.pricing_nulled == 1
    assert list(db.iter_products())[0]["pricing_model"] is None
    db.close()


async def test_dedup_same_normalized_name(tmp_path):
    db = Database(tmp_path / "s.db")
    orch = make_orch(db, [
        {"name": "WidgetPro", "pricing_model": "FREE", "name_evidence": "builds WidgetPro",
         "pricing_evidence": "Free plan"},
        {"name": "WIDGETPRO", "pricing_model": "PAID", "name_evidence": "builds WidgetPro",
         "pricing_evidence": "Pro: $15"},
    ])
    added = await process_startup(db, orch, _crawl_ok(), "Acme Corp", None, Stats())
    assert added == 1  # both normalize to "widgetpro"
    db.close()


async def test_idempotent_rerun(tmp_path):
    db = Database(tmp_path / "s.db")
    prods = [{"name": "WidgetPro", "pricing_model": "FREEMIUM",
              "name_evidence": "builds WidgetPro", "pricing_evidence": "Free plan: $0/month"}]
    o1 = make_orch(db, prods)
    assert await process_startup(db, o1, _crawl_ok(), "Acme Corp", None, Stats()) == 1
    o2 = make_orch(db, prods)
    assert await process_startup(db, o2, _crawl_ok(), "Acme Corp", None, Stats()) == 0
    assert db.count_products() == 1
    db.close()


async def test_max_llm_calls_budget_stop(tmp_path):
    db = Database(tmp_path / "s.db")
    orch = make_orch(db, [{"name": "WidgetPro", "name_evidence": "builds WidgetPro"}], max_calls=0)
    added = await process_startup(db, orch, _crawl_ok(), "Acme Corp", None, Stats())
    assert added == 0                      # budget exhausted -> no extraction
    assert db.count_products() == 0
    assert db.get_crawl_status("acme") is None  # left unmarked for retry
    db.close()


# ---------------- deterministic post-filters ----------------
TIERS = load_plan_tiers()


def test_reject_too_long():
    assert reject_reason("One Two Three Four Five Six Seven", "Acme", None, TIERS) == "too_long"
    assert reject_reason("X" * 61, "Acme", None, TIERS) == "too_long"


def test_reject_sentence_like():
    assert reject_reason("A great tool.", "Acme", None, TIERS) == "sentence_like"      # period
    assert reject_reason("WidgetPro, our flagship", "Acme", None, TIERS) == "sentence_like"  # comma
    assert reject_reason("Build better software", "Acme", None, TIERS) == "sentence_like"     # verb start


def test_reject_plan_tier_and_exception():
    assert reject_reason("Pro", "Acme", None, TIERS) == "plan_tier"
    assert reject_reason("Max", "Acme", None, TIERS) == "plan_tier"
    # Exception: name also contains the startup's own name -> allowed.
    assert reject_reason("Acme Max", "Acme", None, TIERS) is None


def test_reject_equals_tagline():
    assert reject_reason("WidgetPro", "Acme", "WidgetPro", TIERS) == "equals_tagline"


def test_reject_generic_no_proper_name():
    assert reject_reason("data tools for teams", "Acme", None, TIERS) == "generic_no_proper_name"


def test_single_product_fallback_uses_startup_name():
    # The company's own name is always a valid product (bypasses all rules).
    assert reject_reason("Acme", "Acme", None, TIERS) is None


def test_valid_product_name_passes():
    assert reject_reason("WidgetPro", "Acme", None, TIERS) is None


# ---------------- compound plan-tier collapse ----------------
def test_collapse_tier_suffix_names():
    assert collapse_tier_name("Fabriq Developer") == "Fabriq"
    assert collapse_tier_name("Fabriq Enterprise") == "Fabriq"
    assert collapse_tier_name("Acme Pro") == "Acme"
    assert collapse_tier_name("Acme Teams") == "Acme"


def test_collapse_leaves_real_products_alone():
    assert collapse_tier_name("WidgetPro") is None          # single word
    assert collapse_tier_name("Office Agent Dataset") is None  # last word not a tier


async def test_tier_variants_collapse_to_single_base_product(tmp_path):
    # Mirrors the real "Agentic Fabriq" case: two tier variants -> one "Fabriq".
    db = Database(tmp_path / "s.db")
    crawl = CrawlResult(
        slug="fabriq", status=OK,
        pages=[("https://fabriq.com", "Fabriq is our platform."),
               ("https://fabriq.com/pricing",
                "Fabriq Developer Free forever. Fabriq Enterprise Contact sales.")],
        pages_fetched=2)
    orch = make_orch(db, [
        {"name": "Fabriq Developer", "pricing_model": "FREE",
         "name_evidence": "Fabriq Developer Free forever",
         "pricing_evidence": "Fabriq Developer Free forever"},
        {"name": "Fabriq Enterprise", "pricing_model": "ENTERPRISE",
         "name_evidence": "Fabriq Enterprise Contact sales",
         "pricing_evidence": "Fabriq Enterprise Contact sales"},
    ])
    stats = Stats()
    added = await process_startup(db, orch, crawl, "Agentic Fabriq", None, stats)
    assert added == 1
    rows = list(db.iter_products())
    assert len(rows) == 1
    assert rows[0]["product_name"] == "Fabriq"
    assert rows[0]["pricing_model"] == "FREEMIUM"  # free tier + paid tier
    assert stats.rejections["compound_tier"] == 2
    db.close()


async def test_pricing_nulled_when_quote_lacks_price_wording(tmp_path):
    db = Database(tmp_path / "s.db")
    orch = make_orch(db, [
        {"name": "WidgetPro", "pricing_model": "PAID",
         "name_evidence": "builds WidgetPro",
         "pricing_evidence": "builds WidgetPro and DataSync"},  # grounded but no price words
    ])
    stats = Stats()
    added = await process_startup(db, orch, _crawl_ok(), "Acme Corp", None, stats)
    assert added == 1
    assert stats.pricing_nulled == 1
    assert list(db.iter_products())[0]["pricing_model"] is None
    db.close()


async def test_plan_tier_never_emitted_as_product(tmp_path):
    db = Database(tmp_path / "s.db")
    orch = make_orch(db, [
        {"name": "Pro", "pricing_model": "PAID", "name_evidence": "Pro: $15 per user",
         "pricing_evidence": "Pro: $15 per user"},
    ])
    stats = Stats()
    added = await process_startup(db, orch, _crawl_ok(), "Acme Corp", None, stats)
    assert added == 0
    assert stats.rejections["plan_tier"] == 1
    assert db.count_products() == 0
    db.close()


# ---------------- FREE-label discipline ----------------
def test_refine_free_strong_keeps_free():
    assert refine_free_pricing("FREE", "Open source and free forever.") == "FREE"
    assert refine_free_pricing("FREE", "100% free for everyone.") == "FREE"


def test_refine_free_weak_only_becomes_null():
    assert refine_free_pricing("FREE", "Get started free.") is None
    assert refine_free_pricing("FREE", "Start your free trial today.") is None
    assert refine_free_pricing("FREE", "Includes $0... nothing free here") is None


def test_refine_free_entry_plus_paid_becomes_freemium():
    assert refine_free_pricing("FREE", "Free to start.", "Pro plan $20 per month") == "FREEMIUM"
    # strong free wording but a paid plan is also present -> freemium, not free
    assert refine_free_pricing("FREE", "Completely free to use.",
                               "Enterprise: contact sales") == "FREEMIUM"


def test_refine_non_free_passes_through():
    assert refine_free_pricing("PAID", "anything") == "PAID"
    assert refine_free_pricing("FREEMIUM", "x") == "FREEMIUM"
    assert refine_free_pricing(None, "free forever") is None


async def test_free_downgraded_to_freemium_live(tmp_path):
    db = Database(tmp_path / "s.db")
    orch = make_orch(db, [
        {"name": "WidgetPro", "pricing_model": "FREE",
         "name_evidence": "builds WidgetPro", "pricing_evidence": "Free plan: $0/month"},
    ])
    stats = Stats()
    added = await process_startup(db, orch, _crawl_ok(), "Acme Corp", None, stats)
    assert added == 1
    # crawl text also shows "Pro: $15 per user per month" -> paid signal -> FREEMIUM
    assert list(db.iter_products())[0]["pricing_model"] == "FREEMIUM"
    assert stats.rejections["free_downgraded"] == 1
    db.close()


def test_audit_pricing_downgrades_stored_free(tmp_path, monkeypatch):
    import src.pipeline.products as P
    monkeypatch.setattr(P, "_export", lambda db: 0)  # don't clobber real exports
    db = Database(tmp_path / "s.db")

    def ins(norm, pm, ev):
        db.insert_product({
            "startup_slug": "s", "normalized_name": norm, "startup_name": "S",
            "product_name": norm, "pricing_model": pm, "evidence_name": "x",
            "evidence_pricing": ev, "page_url": "https://x.com",
            "source_name": "Company website", "llm_tier": "m",
            "extracted_at": "t", "collected_at": "t"})

    ins("a", "FREE", "Free forever, no paid plan anywhere.")  # strong, no paid -> FREE
    ins("b", "FREE", "Get started free.")                     # weak only       -> null
    ins("c", "FREE", "Free to start. Pro plan $20 per month.")  # free+paid     -> FREEMIUM
    ins("d", "PAID", "$10 per month")                         # non-FREE untouched

    changes = P.audit_pricing(db, apply=True)
    by = {c["product"]: c["new"] for c in changes}
    assert by == {"b": None, "c": "FREEMIUM"}                 # only b, c changed
    rows = {r["normalized_name"]: r["pricing_model"] for r in db.iter_products()}
    assert rows["a"] == "FREE" and rows["b"] is None
    assert rows["c"] == "FREEMIUM" and rows["d"] == "PAID"
    db.close()


# ---------------- paid-signal rule: $ needs billing wording; credits != paid ----
def test_has_paid_signal_requires_billing_context():
    from src.pipeline.products import _has_paid_signal
    assert _has_paid_signal("Pro plan $15 per month") is True
    assert _has_paid_signal("Starting at $99/mo") is True
    assert _has_paid_signal("Contact sales for pricing") is True
    assert _has_paid_signal("billed annually") is True
    # a bare dollar amount, or one tied only to credits, is NOT a paid plan
    assert _has_paid_signal("Includes $20 in usage credits. No card required.") is False
    assert _has_paid_signal("$20") is False
    assert _has_paid_signal("free credits to get started") is False


def test_refine_free_credits_only_becomes_null():
    # Archal's exact evidence: $ amount is credits, not a billed plan -> null
    assert refine_free_pricing("FREE", "Includes $20 in usage credits. No card required.") is None


def test_refine_free_dollar_with_plan_becomes_freemium():
    assert refine_free_pricing("FREE", "Get started free.", "Team plan $20 per seat") == "FREEMIUM"
