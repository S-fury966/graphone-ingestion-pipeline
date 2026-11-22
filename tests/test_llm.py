"""LLM engine tests: chunker, grounding, orchestrator (MockProvider, no internet)."""
from typing import Optional

import pytest
from pydantic import BaseModel, ConfigDict

from src.llm.chunker import estimate_tokens, fit, html_to_text
from src.llm.config import LLMConfig, TierConfig
from src.llm.grounding import verify_grounding
from src.llm.orchestrator import Orchestrator, _TokenWindow
from src.llm.providers import (AuthError, MockProvider, PayloadTooLarge,
                               ProviderDown, RateLimited, _parse_duration,
                               reset_seconds_from_headers, tpm_limit_from_headers)
from src.schemas.enums import PricingModel


# ----- tiny extraction schema -----
class TinyProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_name: Optional[str] = None
    pricing_model: Optional[PricingModel] = None


SRC = "Acme makes WidgetPro. WidgetPro is free to use for everyone."
VALID = {"product_name": "WidgetPro", "pricing_model": "FREE",
         "_evidence": {"product_name": "WidgetPro", "pricing_model": "free to use"}}
INSTR = "Extract the product name and pricing model."


def mk_config(names, **over):
    tiers = [TierConfig(name=n, provider="mock", model="m", api_key_env="",
                        enabled=True, max_input_tokens=4000, max_output_tokens=512,
                        rpm_limit=100000, timeout_s=5) for n in names]
    params = dict(prompt_version="1", global_concurrency=8, max_attempts_per_tier=3,
                  circuit_threshold=4, circuit_cooldown_s=1, backoff_base_s=0.5,
                  backoff_max_s=2, tiers=tiers)
    params.update(over)
    return LLMConfig(**params)


def make_orch(scripts: dict, waits: Optional[list] = None, **over):
    """scripts: {tier_name: [script items]}; waits records backoff sleeps."""
    config = mk_config(list(scripts.keys()), **over)
    providers = {name: MockProvider(script) for name, script in scripts.items()}

    async def rec(s):
        if waits is not None:
            waits.append(s)

    return Orchestrator(config, providers=providers, sleep=rec), providers


async def _run(orch):
    return await orch.extract("prod", TinyProduct, SRC, INSTR)


# ---------------- orchestrator ----------------
async def test_normal_success():
    orch, _ = make_orch({"t1": [VALID]})
    r = await _run(orch)
    assert r.success and r.tier_used == "t1"
    assert r.data["product_name"] == "WidgetPro"
    assert r.data["pricing_model"] == "FREE"
    assert r.evidence["product_name"] == "WidgetPro"


async def test_429_then_success_backoff_honors_retry_after():
    waits = []
    orch, _ = make_orch({"t1": [RateLimited("429", retry_after=1), VALID]}, waits=waits)
    r = await _run(orch)
    assert r.success
    assert 1.0 in waits  # retry_after honored


async def test_429_jitter_within_bounds():
    waits = []
    orch, _ = make_orch({"t1": [RateLimited("429", None), VALID]}, waits=waits)
    r = await _run(orch)
    assert r.success
    assert all(0 <= w <= 2 for w in waits)  # full jitter capped at backoff_max


async def test_429_exhausts_tier_then_fallback():
    orch, _ = make_orch({"t1": [RateLimited(), RateLimited(), RateLimited()],
                         "t2": [VALID]})
    r = await _run(orch)
    assert r.success and r.tier_used == "t2"
    assert orch.metrics["t1"].rate_limited == 3
    assert orch.metrics["t1"].fallbacks == 1


async def test_413_then_smaller_then_success():
    orch, _ = make_orch({"t1": [PayloadTooLarge(), VALID]})
    r = await _run(orch)
    assert r.success and r.tier_used == "t1"
    assert orch.metrics["t1"].too_large == 1


async def test_413_every_size_then_fallback():
    orch, _ = make_orch({"t1": [PayloadTooLarge(), PayloadTooLarge(),
                                PayloadTooLarge(), PayloadTooLarge()],
                         "t2": [VALID]})
    r = await _run(orch)
    assert r.success and r.tier_used == "t2"
    assert orch.metrics["t1"].too_large == 4


async def test_bad_json_then_repair_then_success():
    orch, _ = make_orch({"t1": ["this is not json", VALID]})
    r = await _run(orch)
    assert r.success and r.tier_used == "t1"


async def test_bad_json_twice_then_fallback():
    orch, _ = make_orch({"t1": ["nope", "still bad"], "t2": [VALID]})
    r = await _run(orch)
    assert r.success and r.tier_used == "t2"


async def test_auth_error_disables_tier():
    orch, _ = make_orch({"t1": [AuthError("401")], "t2": [VALID]})
    r = await _run(orch)
    assert r.success and r.tier_used == "t2"
    assert "t1" in orch.disabled_tiers


async def test_all_tiers_fail_returns_failed():
    orch, _ = make_orch({"t1": [ProviderDown(), ProviderDown(), ProviderDown()]})
    r = await _run(orch)
    assert r.success is False
    assert r.data is None
    assert r.error


async def test_cache_hit_makes_no_provider_call():
    class FakeDB:
        def get_llm_cache(self, key):
            return {"data": {"product_name": "Cached"}, "evidence": {}, "tier_used": "t1"}

        def set_llm_cache(self, *a):  # pragma: no cover
            raise AssertionError("should not write on cache hit")

    config = mk_config(["t1"])
    prov = MockProvider([VALID])
    orch = Orchestrator(config, db=FakeDB(), providers={"t1": prov})
    r = await _run(orch)
    assert r.cached is True
    assert r.data["product_name"] == "Cached"
    assert prov.calls == 0


async def test_circuit_trips_on_repeated_provider_down():
    orch, provs = make_orch({"t1": [ProviderDown(), ProviderDown()], "t2": [VALID, VALID]},
                            circuit_threshold=2, max_attempts_per_tier=3,
                            circuit_cooldown_s=100)
    r = await _run(orch)
    assert r.success and r.tier_used == "t2"
    assert orch._circuit_open("t1")           # breaker opened on 5xx/timeout, not just 429
    t1_calls = provs["t1"].calls
    r2 = await orch.extract("prod2", TinyProduct, SRC, INSTR)
    assert r2.success and r2.tier_used == "t2"
    assert provs["t1"].calls == t1_calls       # t1 skipped while on cooldown


async def test_max_llm_calls_budget_stops_before_next_tier():
    orch, provs = make_orch({"t1": [ProviderDown()], "t2": [VALID]}, max_attempts_per_tier=1)
    orch.max_calls = 1
    r = await _run(orch)
    assert r.success is False
    assert "budget" in (r.error or "")
    assert provs["t2"].calls == 0              # budget stopped before the fallback call


async def test_grounding_skips_list_fields():
    # A products list with inline evidence must survive the generic guard.
    data = {"products": [{"name": "X", "pricing_model": "FREE"}], "_evidence": {}}
    g = verify_grounding(data, "irrelevant")
    assert g.data["products"] == [{"name": "X", "pricing_model": "FREE"}]
    assert g.nulled_count == 0


# ---------------- chunker ----------------
def test_chunker_always_fits_budget():
    text = "Some paragraph. " * 500
    out = fit(text, budget=50)
    assert estimate_tokens(out) <= 50


def test_chunker_keeps_title_and_keyword_block():
    html = """<html><head><title>Acme Pricing</title>
    <meta name="description" content="Plans and pricing"></head><body>
    <nav>home about contact</nav>
    <p>Filler one lorem ipsum dolor sit amet nothing useful here at all.</p>
    <p>Our Pro plan costs $20 per month, billed annually.</p>
    <p>More filler text about nothing in particular here today.</p>
    <footer>copyright 2026</footer></body></html>"""
    out = fit(html, budget=28, focus_keywords=["pricing"])
    assert "Acme Pricing" in out              # title kept
    assert "$20 per month" in out             # keyword/digit-dense block kept
    assert "Filler one" not in out            # boilerplate dropped
    assert estimate_tokens(out) <= 28


def test_html_to_text_strips_boilerplate():
    header, blocks = html_to_text(
        "<html><head><title>T</title></head><body><script>x=1</script>"
        "<nav>menu</nav><p>Real content here.</p><footer>foot</footer></body></html>")
    texts = [b.text for b in blocks]
    assert "Real content here." in texts
    assert "menu" not in texts and "x=1" not in texts


# ---------------- grounding ----------------
def test_grounding_nulls_hallucination_keeps_grounded():
    data = {"product_name": "WidgetPro", "pricing_model": "FREE",
            "_evidence": {"product_name": "WidgetPro", "pricing_model": "nowhere text"}}
    source = "WidgetPro is a great product that is free."
    g = verify_grounding(data, source)
    assert g.data["product_name"] == "WidgetPro"   # grounded -> kept
    assert g.data["pricing_model"] is None         # evidence absent -> nulled
    assert g.nulled_count == 1
    assert "pricing_model" in g.nulled_fields


def test_grounding_nulls_field_without_evidence():
    data = {"product_name": "Ghost", "_evidence": {}}
    g = verify_grounding(data, "unrelated source text")
    assert g.data["product_name"] is None
    assert g.nulled_count == 1


# ---------------- token-aware rate limiting (tpm) ----------------
class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t


async def test_token_window_waits_until_room():
    clk = _FakeClock()
    slept = []

    async def sleep(s):
        slept.append(s)
        clk.t += s

    w = _TokenWindow(tpm=100, now=clk.now, sleep=sleep)
    await w.acquire(60)          # window: 60
    await w.acquire(30)          # window: 90 (fits)
    await w.acquire(30)          # 90+30 > 100 -> must wait out the 60s window
    assert slept == [60.0]       # waited exactly one window for the oldest to age out


async def test_token_window_unlimited_never_sleeps():
    slept = []

    async def sleep(s):
        slept.append(s)

    w = _TokenWindow(tpm=None, now=_FakeClock().now, sleep=sleep)
    for _ in range(5):
        await w.acquire(10_000)
    assert slept == []


async def test_token_window_oversized_call_passes_on_empty():
    w = _TokenWindow(tpm=100, now=_FakeClock().now, sleep=lambda s: None)
    await w.acquire(5000)        # bigger than tpm but window empty -> no deadlock
    assert len(w._events) == 1


# ---------------- rate-limit header parsing ----------------
def test_parse_duration_formats():
    assert _parse_duration("3") == 3.0
    assert abs(_parse_duration("7.66s") - 7.66) < 1e-6
    assert abs(_parse_duration("2m59.5s") - 179.5) < 1e-6
    assert abs(_parse_duration("88ms") - 0.088) < 1e-6
    assert _parse_duration(None) is None
    assert _parse_duration("") is None


def test_reset_and_tpm_from_headers():
    h = {"Retry-After": "5", "x-ratelimit-limit-tokens": "6000",
         "x-ratelimit-reset-tokens": "7.66s"}
    assert reset_seconds_from_headers(h) == 5.0          # Retry-After wins
    assert tpm_limit_from_headers(h) == 6000.0
    assert reset_seconds_from_headers({"x-ratelimit-reset-tokens": "2m"}) == 120.0
    assert reset_seconds_from_headers({}) is None


# ---------------- orchestrator learns + honors header limits ----------------
async def test_learn_limits_overrides_window_tpm():
    orch, _ = make_orch({"t1": [VALID]})
    assert orch._token_windows["t1"].tpm is None
    orch._learn_limits("t1", {"x-ratelimit-limit-tokens": "5000"})
    assert orch._token_windows["t1"].tpm == 5000.0


async def test_429_waits_header_reset_and_learns_tpm():
    waits = []
    rl = RateLimited("429", retry_after=2, headers={"x-ratelimit-limit-tokens": "4321"})
    orch, _ = make_orch({"t1": [rl, VALID]}, waits=waits)
    r = await _run(orch)
    assert r.success
    assert 2.0 in waits                               # waited the header reset, not backoff
    assert orch._token_windows["t1"].tpm == 4321.0    # learned from the 429 headers


async def test_429_without_headers_falls_back_to_backoff():
    waits = []
    orch, _ = make_orch({"t1": [RateLimited("429", retry_after=None), VALID]}, waits=waits)
    r = await _run(orch)
    assert r.success
    assert all(0 <= w <= 2 for w in waits)            # jittered backoff within cap


# ---------------- max_wait_s: park daily-capped tier, fall through ----------------
async def test_429_long_reset_parks_tier_and_falls_through():
    waits = []
    rl = RateLimited("429", retry_after=3600)   # > default max_wait_s (120)
    orch, _ = make_orch({"t1": [rl], "t2": [VALID]}, waits=waits)
    r = await _run(orch)
    assert r.success and r.tier_used == "t2"      # fell straight to next tier
    assert orch._is_exhausted("t1")               # t1 parked
    assert 3600 not in waits                       # did NOT sleep through the cap
    assert orch.any_tier_usable()                  # t2 still usable


async def test_429_short_reset_still_sleeps():
    waits = []
    rl = RateLimited("429", retry_after=5)        # <= max_wait_s -> sleep & retry
    orch, _ = make_orch({"t1": [rl, VALID]}, waits=waits, max_wait_s=120)
    r = await _run(orch)
    assert r.success and r.tier_used == "t1"
    assert 5 in waits
    assert not orch._is_exhausted("t1")


async def test_all_tiers_parked_reports_soonest_reset():
    rl = RateLimited("429", retry_after=3600)
    orch, _ = make_orch({"t1": [rl]})             # single tier, long cap
    r = await _run(orch)
    assert r.success is False
    assert not orch.any_tier_usable()
    soonest = orch.soonest_tier_reset()
    assert soonest is not None and soonest > 0


async def test_exhaustion_persists_and_is_respected_on_rerun():
    import time as _t

    class StateDB:
        def __init__(self):
            self.state = {}
        def get_llm_cache(self, key):
            return None
        def set_llm_cache(self, *a):
            pass
        def all_tier_exhausted_until(self):
            return dict(self.state)
        def set_tier_exhausted(self, name, until, reason=None):
            self.state[name] = until

    db = StateDB()
    rl = RateLimited("429", retry_after=3600)
    config1 = mk_config(["t1", "t2"])
    o1 = Orchestrator(config1, db=db,
                      providers={"t1": MockProvider([rl]), "t2": MockProvider([VALID])},
                      sleep=lambda s: None)
    r1 = await o1.extract("prod", TinyProduct, SRC, INSTR)
    assert r1.tier_used == "t2"
    assert db.state.get("t1", 0) > _t.time()      # persisted into the DB

    # Fresh orchestrator (simulated rerun) seeds from the DB and skips t1.
    config2 = mk_config(["t1", "t2"])
    o2 = Orchestrator(config2, db=db,
                      providers={"t1": MockProvider([VALID]), "t2": MockProvider([VALID])},
                      sleep=lambda s: None)
    assert o2._is_exhausted("t1")
    r2 = await o2.extract("prod2", TinyProduct, SRC, INSTR)
    assert r2.tier_used == "t2"                     # still avoids the parked tier


async def test_429_body_logged_on_rate_limit():
    rl = RateLimited("429", retry_after=3600, body='{"error":{"message":"limit per day"}}')
    orch, _ = make_orch({"t1": [rl], "t2": [VALID]})
    await _run(orch)
    assert "t1" in orch._429_body_logged


# ---------------- generic OpenAI-compatible provider ----------------
def test_build_generic_openai_provider_uses_base_url():
    from src.llm.providers import build_provider, GenericOpenAIProvider
    p = build_provider("openai", fetcher=None, model="llama-3.3-70b",
                       api_key="k", timeout_s=20,
                       base_url="https://api.cerebras.ai/v1/chat/completions")
    assert isinstance(p, GenericOpenAIProvider)
    assert p.ENDPOINT == "https://api.cerebras.ai/v1/chat/completions"


def test_generic_openai_provider_requires_base_url():
    from src.llm.providers import GenericOpenAIProvider
    with pytest.raises(ValueError):
        GenericOpenAIProvider(fetcher=None, model="m", api_key="k")


# ---------------- chunker: hero + pass-through ----------------
def test_fit_passthrough_small_plain_text():
    txt = "a short pre-trimmed snippet well under budget"
    assert fit(txt, 1000) == txt


def test_fit_always_keeps_hero_lead_block():
    html = ("<html><head><title>T</title></head><body><h1>HeroName</h1>"
            "<p>" + ("filler word " * 3000) + "</p>"
            "<p>Pro plan $10 per month</p></body></html>")
    out = fit(html, 60, ["pricing"], always_lead=1)
    assert "HeroName" in out        # hero kept even though a denser block exists
    assert estimate_tokens(out) <= 60
