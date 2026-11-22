"""LLM extraction orchestrator: tier fallback, backoff, grounding, caching."""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

from pydantic import ValidationError

from ..common.env import get_secret
from ..common.logging import get_logger
from ..crawler.fetcher import AsyncFetcher, _TokenBucket
from .chunker import estimate_tokens, fit
from .config import LLMConfig, TierConfig
from .grounding import (PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt,
                        schema_hint_from_model, verify_grounding)
from .providers import (AuthError, BadOutput, BaseProvider, MockProvider,
                        PayloadTooLarge, ProviderDown, RateLimited,
                        build_provider, reset_seconds_from_headers,
                        tpm_limit_from_headers)

log = get_logger("llm.orchestrator")


class _TokenWindow:
    """Rolling 60-second tokens-per-minute limiter.

    Before a call, acquire() the call's estimated tokens; it waits until the
    trailing 60s window has room under `tpm`. A single oversized call is let
    through on an empty window to avoid deadlock. `tpm=None` => unlimited.
    """

    WINDOW_S = 60.0

    def __init__(self, tpm: Optional[float], now=time.monotonic, sleep=asyncio.sleep):
        self.tpm = tpm
        self._now = now
        self._sleep = sleep
        self._events: Deque[Tuple[float, float]] = deque()

    def set_limit(self, tpm: Optional[float]) -> None:
        if tpm and tpm > 0:
            self.tpm = tpm

    def _evict(self, t: float) -> None:
        while self._events and self._events[0][0] <= t - self.WINDOW_S:
            self._events.popleft()

    async def acquire(self, tokens: float) -> None:
        if not self.tpm:
            return
        while True:
            t = self._now()
            self._evict(t)
            used = sum(tok for _, tok in self._events)
            if used + tokens <= self.tpm or not self._events:
                self._events.append((t, tokens))
                return
            # Wait until the oldest event ages out of the window.
            wait = self.WINDOW_S - (t - self._events[0][0])
            await self._sleep(max(wait, 0.01))


@dataclass
class ExtractionResult:
    success: bool
    data: Optional[Dict[str, Any]]
    tier_used: Optional[str]
    attempts: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cached: bool = False
    evidence: Dict[str, str] = field(default_factory=dict)
    nulled_fields: List[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class _TierMetrics:
    calls: int = 0
    rate_limited: int = 0
    too_large: int = 0
    fallbacks: int = 0
    cache_hits: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


class Orchestrator:
    def __init__(self, config: LLMConfig, fetcher: Optional[AsyncFetcher] = None,
                 db: Any = None, providers: Optional[Dict[str, BaseProvider]] = None,
                 sleep=asyncio.sleep, max_calls: Optional[int] = None) -> None:
        self.config = config
        self.fetcher = fetcher
        self.db = db
        self.sleep = sleep
        self.max_calls = max_calls
        self.calls_made = 0
        self._budget_logged = False
        self.global_sem = asyncio.Semaphore(config.global_concurrency)
        self.metrics: Dict[str, _TierMetrics] = {t.name: _TierMetrics() for t in config.tiers}
        self.disabled_tiers: set[str] = set()
        # Circuit breaker trips on repeated failures (429s AND timeouts/5xx).
        self._circuit: Dict[str, Dict[str, float]] = {
            t.name: {"failures": 0, "cooldown_until": 0.0} for t in config.tiers}

        # Per-provider shared RPM limiter (uses real sleep; independent of the
        # injectable backoff sleep so tests can isolate backoff timing).
        self._buckets: Dict[str, _TokenBucket] = {}
        for t in config.tiers:
            self._buckets[t.name] = _TokenBucket(rate=t.rpm_limit / 60.0, capacity=1.0,
                                                 sleep=asyncio.sleep)

        # Per-tier token-per-minute window (seeded from config, overridden by
        # the provider's rate-limit headers at runtime).
        self._token_windows: Dict[str, _TokenWindow] = {
            t.name: _TokenWindow(t.tpm_limit) for t in config.tiers}
        self._limits_logged: set[str] = set()
        self._429_body_logged: set[str] = set()

        # Per-tier cooldown (epoch seconds): a 429 whose reset exceeds max_wait_s
        # parks the tier instead of sleeping. Seeded from SQLite so reruns respect
        # a daily-capped tier.
        self.max_wait_s = config.max_wait_s
        self._exhausted_until: Dict[str, float] = {t.name: 0.0 for t in config.tiers}
        if db is not None and hasattr(db, "all_tier_exhausted_until"):
            for name, until in db.all_tier_exhausted_until().items():
                if name in self._exhausted_until:
                    self._exhausted_until[name] = until

        # Build providers (override wins; else construct from config + env key).
        self.providers: Dict[str, Optional[BaseProvider]] = {}
        for t in config.tiers:
            if providers and t.name in providers:
                self.providers[t.name] = providers[t.name]
                continue
            if t.provider == "mock":
                self.providers[t.name] = None  # tests inject mocks explicitly
                continue
            key = get_secret(t.api_key_env) if t.api_key_env else None
            if not key:
                self.providers[t.name] = None
                log.warning("tier skipped: missing API key", extra={"extra": {
                    "tier": t.name, "key_env": t.api_key_env}})
                continue
            if fetcher is None:
                self.providers[t.name] = None
                continue
            self.providers[t.name] = build_provider(t.provider, fetcher, t.model,
                                                    key, t.timeout_s,
                                                    getattr(t, "base_url", None),
                                                    getattr(t, "reasoning_effort", None),
                                                    getattr(t, "max_output_tokens", None))

    # ---- circuit breaker ----
    def _circuit_open(self, name: str) -> bool:
        return time.monotonic() < self._circuit[name]["cooldown_until"]

    def _record_failure(self, name: str, kind: str) -> None:
        """Count a hard failure (429, 5xx, or timeout) toward the breaker."""
        c = self._circuit[name]
        c["failures"] += 1
        if c["failures"] >= self.config.circuit_threshold:
            c["cooldown_until"] = time.monotonic() + self.config.circuit_cooldown_s
            log.warning("circuit opened (repeated failures)", extra={"extra": {
                "tier": name, "kind": kind, "failures": int(c["failures"]),
                "cooldown_s": self.config.circuit_cooldown_s}})

    def _reset_circuit(self, name: str) -> None:
        self._circuit[name] = {"failures": 0, "cooldown_until": 0.0}

    # ---- per-tier exhaustion (daily-cap cooldown, persisted) ----
    def _is_exhausted(self, name: str) -> bool:
        return time.time() < self._exhausted_until.get(name, 0.0)

    def _mark_exhausted(self, name: str, reset_s: float, reason: str) -> None:
        until = time.time() + reset_s
        self._exhausted_until[name] = until
        if self.db is not None and hasattr(self.db, "set_tier_exhausted"):
            self.db.set_tier_exhausted(name, until, reason)
        log.warning("tier exhausted; parking and falling through", extra={"extra": {
            "tier": name, "reset_s": round(reset_s, 1),
            "until_epoch": round(until, 1), "reason": reason[:200]}})

    def _tier_usable(self, tier: TierConfig) -> bool:
        """A tier we could call right now (ignores budget, which is global)."""
        return (tier.enabled and tier.name not in self.disabled_tiers
                and self.providers.get(tier.name) is not None
                and not self._circuit_open(tier.name)
                and not self._is_exhausted(tier.name))

    def any_tier_usable(self) -> bool:
        return any(self._tier_usable(t) for t in self.config.tiers)

    def soonest_tier_reset(self) -> Optional[float]:
        """Earliest epoch-seconds at which some currently-parked tier frees up,
        or None if a tier is already usable or none can ever recover."""
        if self.any_tier_usable():
            return None
        candidates = []
        for t in self.config.tiers:
            if (t.enabled and t.name not in self.disabled_tiers
                    and self.providers.get(t.name) is not None):
                until = self._exhausted_until.get(t.name, 0.0)
                if until > time.time():
                    candidates.append(until)
        return min(candidates) if candidates else None

    # ---- learn real rate limits from provider headers ----
    def _learn_limits(self, name: str, headers: Optional[dict]) -> None:
        if not headers:
            return
        tpm = tpm_limit_from_headers(headers)
        if name not in self._limits_logged:
            low = {str(k).lower(): v for k, v in headers.items()}
            log.info("provider rate limits (from headers)", extra={"extra": {
                "tier": name,
                "limit_tokens": low.get("x-ratelimit-limit-tokens"),
                "remaining_tokens": low.get("x-ratelimit-remaining-tokens"),
                "reset_tokens": low.get("x-ratelimit-reset-tokens"),
                "limit_requests": low.get("x-ratelimit-limit-requests"),
                "retry_after": low.get("retry-after")}})
            self._limits_logged.add(name)
        if tpm:
            self._token_windows[name].set_limit(tpm)

    def _rl_snapshot(self, name: str, headers: Optional[dict], kind: str) -> None:
        """Log x-ratelimit-remaining-tokens on every call, so a 429 spike can be
        diagnosed from the last N snapshots. Observability only; no behavior change."""
        if not headers:
            return
        low = {str(k).lower(): v for k, v in headers.items()}
        log.info("ratelimit snapshot", extra={"extra": {
            "tier": name, "kind": kind,
            "remaining_tokens": low.get("x-ratelimit-remaining-tokens"),
            "reset_tokens": low.get("x-ratelimit-reset-tokens"),
            "remaining_requests": low.get("x-ratelimit-remaining-requests")}})

    # ---- call budget ----
    def budget_reached(self) -> bool:
        if self.max_calls is not None and self.calls_made >= self.max_calls:
            if not self._budget_logged:
                log.warning("LLM call budget reached", extra={"extra": {
                    "max_calls": self.max_calls, "calls_made": self.calls_made}})
                self._budget_logged = True
            return True
        return False

    # ---- backoff ----
    def _backoff(self, attempt: int, retry_after: Optional[float]) -> float:
        capped = min(self.config.backoff_max_s,
                     self.config.backoff_base_s * (2 ** (attempt - 1)))
        wait = random.uniform(0, capped)  # full jitter
        if retry_after is not None:
            wait = max(wait, retry_after)
        return wait

    # ---- cache key ----
    def _cache_key(self, task_name: str, schema_model: Any, instructions: str,
                   source_text: str) -> str:
        h = hashlib.sha256()
        h.update("|".join([task_name, self.config.prompt_version, PROMPT_VERSION,
                           schema_model.__name__, instructions, source_text]).encode("utf-8"))
        return h.hexdigest()

    # ---- main entry ----
    async def extract(self, task_name: str, schema_model: Any, source_text: str,
                      instructions: str, focus_keywords: Optional[List[str]] = None
                      ) -> ExtractionResult:
        cache_key = self._cache_key(task_name, schema_model, instructions, source_text)
        if self.db is not None:
            hit = self.db.get_llm_cache(cache_key)
            if hit is not None:
                tier = hit.get("tier_used")
                if tier in self.metrics:
                    self.metrics[tier].cache_hits += 1
                return ExtractionResult(success=True, data=hit["data"],
                                        tier_used=hit.get("tier_used"), cached=True,
                                        evidence=hit.get("evidence", {}))

        if self.budget_reached():
            return ExtractionResult(success=False, data=None, tier_used=None,
                                    attempts=0, error="llm call budget reached")

        schema_hint = schema_hint_from_model(schema_model)
        total_attempts = 0
        last_error: Optional[str] = None

        for tier in self.config.tiers:
            if not tier.enabled or tier.name in self.disabled_tiers:
                continue
            provider = self.providers.get(tier.name)
            if provider is None:
                continue
            if self._circuit_open(tier.name):
                log.warning("tier on cooldown; skipping", extra={"extra": {"tier": tier.name}})
                continue
            if self._is_exhausted(tier.name):
                log.warning("tier exhausted (daily cap); skipping", extra={"extra": {
                    "tier": tier.name,
                    "reset_in_s": round(self._exhausted_until[tier.name] - time.time(), 1)}})
                continue

            reserve = (estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(instructions)
                       + estimate_tokens(schema_hint) + tier.max_output_tokens + 200)
            budget = max(300, tier.max_input_tokens - reserve)

            outcome = await self._try_tier(tier, provider, schema_model, schema_hint,
                                           source_text, instructions, focus_keywords, budget)
            total_attempts += outcome["attempts"]
            self.metrics[tier.name].tokens_in += outcome["tokens_in"]
            self.metrics[tier.name].tokens_out += outcome["tokens_out"]

            if outcome["success"]:
                result = ExtractionResult(
                    success=True, data=outcome["data"], tier_used=tier.name,
                    attempts=total_attempts, tokens_in=outcome["tokens_in"],
                    tokens_out=outcome["tokens_out"], cached=False,
                    evidence=outcome["evidence"], nulled_fields=outcome["nulled"])
                if self.db is not None:
                    self.db.set_llm_cache(cache_key, result.data, result.evidence, tier.name)
                return result

            last_error = outcome["error"]
            self.metrics[tier.name].fallbacks += 1
            log.warning("tier failed; falling back", extra={"extra": {
                "tier": tier.name, "reason": last_error}})

        return ExtractionResult(success=False, data=None, tier_used=None,
                                attempts=total_attempts, error=last_error or "all tiers failed")

    async def _try_tier(self, tier: TierConfig, provider: BaseProvider, schema_model: Any,
                        schema_hint: str, source_text: str, instructions: str,
                        focus_keywords: Optional[List[str]], budget: int) -> dict:
        metrics = self.metrics[tier.name]
        hard_attempts = 0       # 429 / 5xx
        too_large_retries = 0
        repaired = False
        attempts = 0
        budget_cur = budget
        tokens_in = tokens_out = 0
        repair_error: Optional[str] = None

        while True:
            attempts += 1
            if attempts > 12:  # safety stop
                return self._fail("too many attempts", attempts, tokens_in, tokens_out)

            if self.budget_reached():
                return self._fail("llm call budget reached", attempts, tokens_in, tokens_out)

            fitted = fit(source_text, budget_cur, focus_keywords)
            user = build_user_prompt(instructions, schema_hint, fitted, repair_error)
            out_reserve = tier.tpm_output_reserve or tier.max_output_tokens
            est_tokens = (estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(user)
                          + out_reserve)
            try:
                async with self.global_sem:
                    await self._buckets[tier.name].acquire()
                    await self._token_windows[tier.name].acquire(est_tokens)
                    self.calls_made += 1
                    # Hard backstop: a provider call that hangs past the fetcher's
                    # own HTTP timeout (e.g. a wedged per-domain slot or a stuck
                    # connection) must NEVER park a worker forever. wait_for
                    # cancels the hung call so the semaphore/slot are released and
                    # it falls through as an ordinary ProviderDown.
                    try:
                        resp = await asyncio.wait_for(
                            provider.complete(SYSTEM_PROMPT, user),
                            timeout=self.config.call_deadline_s)
                    except asyncio.TimeoutError:
                        raise ProviderDown(
                            f"provider call exceeded {self.config.call_deadline_s}s deadline")
                metrics.calls += 1
                self._learn_limits(tier.name, getattr(resp, "headers", None))
                self._rl_snapshot(tier.name, getattr(resp, "headers", None), "200")
                tokens_in += resp.tokens_in
                tokens_out += resp.tokens_out

                # An empty or null content field is a bad output (NVIDIA Nemotron
                # does this when its reasoning channel fills the token budget):
                # treat it as BadOutput so the repair retry / fallback handle it
                # and it can never raise out of extract().
                text = resp.text
                if text is None or not str(text).strip():
                    raise BadOutput("empty content")
                try:
                    raw = json.loads(text)
                except (ValueError, TypeError) as exc:
                    raise BadOutput(f"invalid JSON: {exc}")
                if not isinstance(raw, dict):
                    raise BadOutput("JSON is not an object")

                core = {k: v for k, v in raw.items() if k != "_evidence"}
                try:
                    schema_model.model_validate(core)
                except ValidationError as ve:
                    raise BadOutput(str(ve))

                grounded = verify_grounding(raw, fitted)
                self._reset_circuit(tier.name)
                return {"success": True, "data": grounded.data,
                        "evidence": grounded.evidence, "nulled": grounded.nulled_fields,
                        "attempts": attempts, "tokens_in": tokens_in, "tokens_out": tokens_out}

            except RateLimited as exc:
                metrics.rate_limited += 1
                self._learn_limits(tier.name, getattr(exc, "headers", None))
                self._rl_snapshot(tier.name, getattr(exc, "headers", None), "429")
                # Log the full 429 body once per tier per run (names the exact
                # limit / used / reset, which the x-ratelimit headers omit).
                if tier.name not in self._429_body_logged:
                    log.warning("429 response body", extra={"extra": {
                        "tier": tier.name, "model": tier.model,
                        "body": getattr(exc, "body", "") or "<empty>"}})
                    self._429_body_logged.add(tier.name)
                self._record_failure(tier.name, "429")
                hard_attempts += 1
                # A reset longer than max_wait_s means a daily/long cap: do NOT
                # sleep through it — park the tier and fall to the next one.
                if exc.retry_after is not None and exc.retry_after > self.max_wait_s:
                    self._mark_exhausted(tier.name, exc.retry_after,
                                         f"429 reset {exc.retry_after:.0f}s > max_wait_s")
                    return self._fail("429 daily cap (tier parked)", attempts,
                                      tokens_in, tokens_out)
                if hard_attempts < self.config.max_attempts_per_tier and not self._circuit_open(tier.name):
                    # Prefer the provider's own reset time; fall back to jittered backoff.
                    if exc.retry_after is not None:
                        wait = exc.retry_after      # honor the header exactly
                        src = "header"
                    else:
                        wait = self._backoff(hard_attempts, None)
                        src = "backoff"
                    log.warning("429; waiting", extra={"extra": {
                        "tier": tier.name, "attempt": hard_attempts,
                        "wait_s": round(wait, 3), "source": src}})
                    await self.sleep(wait)
                    continue
                return self._fail("429 exhausted", attempts, tokens_in, tokens_out)

            except PayloadTooLarge:
                metrics.too_large += 1
                if too_large_retries < 3:
                    too_large_retries += 1
                    budget_cur = max(200, budget_cur // 2)
                    log.warning("413; re-fitting smaller", extra={"extra": {
                        "tier": tier.name, "new_budget": budget_cur}})
                    continue
                return self._fail("413 payload too large", attempts, tokens_in, tokens_out)

            except ProviderDown as exc:
                self._record_failure(tier.name, "provider_down")
                hard_attempts += 1
                if hard_attempts < self.config.max_attempts_per_tier and not self._circuit_open(tier.name):
                    wait = self._backoff(hard_attempts, None)
                    await self.sleep(wait)
                    continue
                return self._fail(f"provider down: {exc}", attempts, tokens_in, tokens_out)

            except BadOutput as exc:
                if not repaired:
                    repaired = True
                    repair_error = str(exc)[:300]
                    log.warning("bad output; one repair retry", extra={"extra": {"tier": tier.name}})
                    continue
                return self._fail(f"bad output: {exc}", attempts, tokens_in, tokens_out)

            except AuthError as exc:
                self.disabled_tiers.add(tier.name)
                log.warning("auth error; tier disabled for run", extra={"extra": {"tier": tier.name}})
                return self._fail(f"auth: {exc}", attempts, tokens_in, tokens_out)

    @staticmethod
    def _fail(reason: str, attempts: int, tin: int, tout: int) -> dict:
        return {"success": False, "data": None, "evidence": {}, "nulled": [],
                "attempts": attempts, "tokens_in": tin, "tokens_out": tout, "error": reason}

    def log_metrics(self) -> None:
        for name, m in self.metrics.items():
            log.info("LLM tier metrics", extra={"extra": {
                "tier": name, "calls": m.calls, "rate_limited": m.rate_limited,
                "too_large": m.too_large, "fallbacks": m.fallbacks,
                "cache_hits": m.cache_hits, "tokens_in": m.tokens_in,
                "tokens_out": m.tokens_out}})
