"""Regression tests for the all-tiers-parked deadlock (no network, mock tiers).

Scenario reproduced: Gemini on circuit cooldown, both Groq tiers parked on a
daily cap, and the only remaining tier (NVIDIA) issues a provider call that
never returns.  Before the fix the orchestrator had no hard deadline around
provider.complete(), so a worker blocked forever and -- with every other tier
parked -- the whole run wedged with no CPU, no sockets and no log output.

The fix adds a per-call asyncio.wait_for deadline so a hung provider call is
turned into a normal ProviderDown/fallback instead of an infinite wait.
"""
import asyncio
import time

import pytest
from pydantic import BaseModel, ConfigDict

from src.llm.config import LLMConfig, TierConfig
from src.llm.orchestrator import Orchestrator
from src.llm.providers import BaseProvider, MockProvider, ProviderResponse
from src.schemas.enums import PricingModel


class TinyProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_name: str | None = None
    pricing_model: PricingModel | None = None


SRC = "Acme makes WidgetPro. WidgetPro is free to use for everyone."
INSTR = "Extract the product name and pricing model."
VALID = {"product_name": "WidgetPro", "pricing_model": "FREE",
         "_evidence": {"product_name": "WidgetPro", "pricing_model": "free to use"}}


class HangingProvider(BaseProvider):
    """A provider whose complete() never returns -- models a stuck connection
    the HTTP timeout failed to break (or a wedged per-domain slot)."""
    name = "hang"

    def __init__(self):
        super().__init__(fetcher=None, model="hang", api_key="x")
        self.started = 0

    async def complete(self, system, user, json_schema=None):
        self.started += 1
        await asyncio.Event().wait()   # never set -> blocks forever
        raise AssertionError("unreachable")


class EmptyContentProvider(BaseProvider):
    """Returns an empty/None content field -- the real NVIDIA failure mode."""
    name = "empty"

    def __init__(self, text=None):
        super().__init__(fetcher=None, model="empty", api_key="x")
        self.text = text
        self.calls = 0

    async def complete(self, system, user, json_schema=None):
        self.calls += 1
        return ProviderResponse(text=self.text, tokens_in=10, tokens_out=0)


def _config():
    tiers = [
        TierConfig(name="gemini", provider="mock", model="m", api_key_env="",
                   enabled=True, max_input_tokens=4000, max_output_tokens=512,
                   rpm_limit=100000, timeout_s=5),
        TierConfig(name="groq-a", provider="mock", model="m", api_key_env="",
                   enabled=True, max_input_tokens=4000, max_output_tokens=512,
                   rpm_limit=100000, timeout_s=5),
        TierConfig(name="groq-b", provider="mock", model="m", api_key_env="",
                   enabled=True, max_input_tokens=4000, max_output_tokens=512,
                   rpm_limit=100000, timeout_s=5),
        TierConfig(name="nvidia", provider="mock", model="m", api_key_env="",
                   enabled=True, max_input_tokens=4000, max_output_tokens=512,
                   rpm_limit=100000, timeout_s=5),
    ]
    return LLMConfig(prompt_version="1", global_concurrency=8,
                     max_attempts_per_tier=2, circuit_threshold=4,
                     circuit_cooldown_s=1, backoff_base_s=0.01, backoff_max_s=0.02,
                     call_deadline_s=1, tiers=tiers)


def _parked_orch(nvidia_provider):
    """Gemini on cooldown, both Groq parked, nvidia = the given provider."""
    config = _config()
    providers = {
        "gemini": MockProvider([VALID]),
        "groq-a": MockProvider([VALID]),
        "groq-b": MockProvider([VALID]),
        "nvidia": nvidia_provider,
    }

    async def fast_sleep(_s):      # keep backoff instant
        return None

    orch = Orchestrator(config, providers=providers, sleep=fast_sleep)
    orch._circuit["gemini"]["cooldown_until"] = time.monotonic() + 60   # on cooldown
    orch._exhausted_until["groq-a"] = time.time() + 300                 # parked
    orch._exhausted_until["groq-b"] = time.time() + 300                 # parked
    return orch


async def test_hung_provider_on_last_tier_does_not_deadlock():
    """A never-returning call on the only live tier must not wedge the run.

    Several concurrent workers each call extract(); with the per-call deadline
    every call resolves (as a failure) and the gather completes.  Without the
    deadline this blocks forever and wait_for raises TimeoutError.
    """
    orch = _parked_orch(HangingProvider())

    async def worker():
        return await orch.extract("prod", TinyProduct, SRC, INSTR)

    results = await asyncio.wait_for(
        asyncio.gather(*(worker() for _ in range(4))), timeout=10)

    # Every worker returned a clean failure (no exception escaped extract()).
    assert all(r.success is False for r in results)
    assert all(r.error for r in results)


async def test_empty_content_is_bad_output_and_never_raises():
    """An empty/None content field must surface as a handled failure, not an
    exception out of extract()."""
    for text in (None, "", "   "):
        orch = _parked_orch(EmptyContentProvider(text=text))
        r = await asyncio.wait_for(
            orch.extract("prod", TinyProduct, SRC, INSTR), timeout=10)
        assert r.success is False
        assert r.error and "bad output" in r.error.lower()
