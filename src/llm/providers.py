"""Async LLM provider clients with a common interface + typed errors.

All HTTP goes through the project AsyncFetcher (UA rotation, timeouts), never
vendor SDKs. API keys and full prompts are never logged.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from ..common.logging import get_logger
from ..crawler.fetcher import AsyncFetcher

log = get_logger("llm.providers")


def _parse_duration(value: Optional[str]) -> Optional[float]:
    """Parse a rate-limit duration into seconds.

    Accepts plain seconds ("3", "7.66") and Groq-style compound strings
    ("2m59.56s", "88ms", "1m"). Returns None when nothing parses.
    """
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    total = 0.0
    found = False
    m = re.search(r"(\d+(?:\.\d+)?)\s*ms", s)
    if m:
        total += float(m.group(1)) / 1000.0
        found = True
    m = re.search(r"(\d+(?:\.\d+)?)\s*m(?!s)", s)
    if m:
        total += float(m.group(1)) * 60.0
        found = True
    m = re.search(r"(\d+(?:\.\d+)?)\s*s", s)
    if m:
        total += float(m.group(1))
        found = True
    return total if found else None


def _lower_headers(headers: Optional[Mapping[str, str]]) -> dict:
    try:
        return {str(k).lower(): v for k, v in (headers or {}).items()}
    except Exception:  # noqa: BLE001
        return {}


def reset_seconds_from_headers(headers: Optional[Mapping[str, str]]) -> Optional[float]:
    """Seconds to wait from Retry-After or x-ratelimit-reset-* headers."""
    low = _lower_headers(headers)
    v = _parse_duration(low.get("retry-after"))
    if v is not None:
        return v
    for key in ("x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        v = _parse_duration(low.get(key))
        if v is not None:
            return v
    return None


def tpm_limit_from_headers(headers: Optional[Mapping[str, str]]) -> Optional[float]:
    """Provider's advertised tokens-per-minute limit, if present."""
    low = _lower_headers(headers)
    raw = low.get("x-ratelimit-limit-tokens")
    try:
        return float(raw) if raw else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------
class LLMError(Exception):
    """Base for provider errors."""


class RateLimited(LLMError):
    def __init__(self, msg: str = "rate limited", retry_after: Optional[float] = None,
                 headers: Optional[Mapping[str, str]] = None,
                 body: Optional[str] = None):
        super().__init__(msg)
        self.retry_after = retry_after
        self.headers = dict(headers or {})
        # Raw 429 response body (often names the exact limit, used, and reset).
        self.body = (body or "")[:1000]


class PayloadTooLarge(LLMError):
    """413 or any context-length / too-large error."""


class ProviderDown(LLMError):
    """5xx, timeout, or connection failure."""


class BadOutput(LLMError):
    """Malformed response envelope or invalid JSON content."""


class AuthError(LLMError):
    """401 / 403 — bad or missing credentials."""


@dataclass
class ProviderResponse:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    headers: dict = field(default_factory=dict)


_TOO_LARGE_MARKERS = ("context length", "too large", "maximum context",
                      "reduce the length", "token count", "exceeds", "too long",
                      "input is too long", "request entity too large")


def _looks_too_large(status: int, body: str) -> bool:
    if status == 413:
        return True
    low = (body or "").lower()
    return any(m in low for m in _TOO_LARGE_MARKERS)


class BaseProvider:
    name = "base"

    def __init__(self, fetcher: AsyncFetcher, model: str, api_key: Optional[str],
                 timeout_s: float = 30.0) -> None:
        self.fetcher = fetcher
        self.model = model
        self.api_key = api_key
        self.timeout_s = timeout_s

    async def complete(self, system: str, user: str,
                       json_schema: Optional[dict] = None) -> ProviderResponse:
        raise NotImplementedError

    def _map_http_error(self, status: int, body: str, retry_after: Optional[float],
                        headers: Optional[Mapping[str, str]] = None) -> LLMError:
        if status in (401, 403):
            return AuthError(f"auth error {status}")
        if status == 429:
            return RateLimited("429", retry_after, headers, body)
        if _looks_too_large(status, body):
            return PayloadTooLarge(f"payload too large ({status})")
        if status >= 500 or status == 0:
            return ProviderDown(f"provider down ({status})")
        # Other 4xx: treat an over-long 400 as PayloadTooLarge (handled above),
        # else a bad request we cannot retry.
        return BadOutput(f"unexpected status {status}")


# ---------------------------------------------------------------------------
# Gemini (generateContent REST, JSON output mode)
# ---------------------------------------------------------------------------
class GeminiProvider(BaseProvider):
    name = "gemini"
    BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    async def complete(self, system, user, json_schema=None):
        url = f"{self.BASE}/{self.model}:generateContent"
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "maxOutputTokens": 2048,
                # Disable Gemini 3.x "thinking" so output tokens go to the JSON
                # answer (a small budget otherwise yields empty content).
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        if json_schema:
            body["generationConfig"]["responseSchema"] = json_schema
        started = time.monotonic()
        res = await self.fetcher.fetch_text(
            url, method="POST", json_body=body,
            headers={"x-goog-api-key": self.api_key or "", "Content-Type": "application/json"},
            respect_robots=False, max_attempts=1, total_timeout=self.timeout_s)
        latency = (time.monotonic() - started) * 1000

        if res.status != 200:
            retry_after = reset_seconds_from_headers(res.headers)
            raise self._map_http_error(res.status, res.text or "", retry_after, res.headers)
        try:
            payload = _json_loads(res.text)
            cand = payload["candidates"][0]
            text = "".join(p.get("text", "") for p in cand["content"]["parts"])
            usage = payload.get("usageMetadata", {})
        except Exception as exc:  # noqa: BLE001
            raise BadOutput(f"gemini envelope: {exc}")
        return ProviderResponse(text=text,
                                tokens_in=usage.get("promptTokenCount", 0),
                                tokens_out=usage.get("candidatesTokenCount", 0),
                                latency_ms=latency, headers=dict(res.headers or {}))


# ---------------------------------------------------------------------------
# OpenAI-compatible chat completions (Groq, DeepSeek)
# ---------------------------------------------------------------------------
class OpenAICompatProvider(BaseProvider):
    ENDPOINT = ""  # set by subclass

    async def complete(self, system, user, json_schema=None):
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "max_tokens": 2048,
        }
        effort = getattr(self, "reasoning_effort", None)
        if effort:
            body["reasoning_effort"] = effort
        started = time.monotonic()
        res = await self.fetcher.fetch_text(
            self.ENDPOINT, method="POST", json_body=body,
            headers={"Authorization": f"Bearer {self.api_key or ''}",
                     "Content-Type": "application/json"},
            respect_robots=False, max_attempts=1, total_timeout=self.timeout_s)
        latency = (time.monotonic() - started) * 1000

        if res.status != 200:
            retry_after = reset_seconds_from_headers(res.headers)
            raise self._map_http_error(res.status, res.text or "", retry_after, res.headers)
        try:
            payload = _json_loads(res.text)
            text = payload["choices"][0]["message"]["content"]
            usage = payload.get("usage", {})
        except Exception as exc:  # noqa: BLE001
            raise BadOutput(f"openai-compat envelope: {exc}")
        return ProviderResponse(text=text,
                                tokens_in=usage.get("prompt_tokens", 0),
                                tokens_out=usage.get("completion_tokens", 0),
                                latency_ms=latency, headers=dict(res.headers or {}))


class GroqProvider(OpenAICompatProvider):
    name = "groq"
    ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"


class DeepSeekProvider(OpenAICompatProvider):
    name = "deepseek"
    ENDPOINT = "https://api.deepseek.com/chat/completions"


class GenericOpenAIProvider(OpenAICompatProvider):
    """Any OpenAI-compatible chat-completions endpoint (Cerebras, Mistral, …).

    The endpoint comes from config `base_url`; everything else reuses the shared
    OpenAI-compatible request/parse/error path above."""
    name = "openai"

    def __init__(self, fetcher: AsyncFetcher, model: str, api_key: Optional[str],
                 timeout_s: float = 30.0, base_url: str = "") -> None:
        super().__init__(fetcher, model, api_key, timeout_s)
        if not base_url:
            raise ValueError("openai-compatible tier requires base_url")
        self.ENDPOINT = base_url  # instance attr overrides the class blank


# ---------------------------------------------------------------------------
# Mock provider (scriptable for tests)
# ---------------------------------------------------------------------------
class MockProvider(BaseProvider):
    name = "mock"

    def __init__(self, script: Optional[list] = None, default_text: str = "{}"):
        """`script` is a list of items applied per call, in order:
          - an Exception instance -> raised
          - a str -> returned as response text
          - a dict -> json-serialized and returned
        When the script is exhausted, `default_text` is returned.
        """
        super().__init__(fetcher=None, model="mock", api_key="mock")  # type: ignore[arg-type]
        self.script = list(script or [])
        self.default_text = default_text
        self.calls = 0

    async def complete(self, system, user, json_schema=None):
        self.calls += 1
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, dict):
                import json as _j
                return ProviderResponse(text=_j.dumps(item), tokens_in=10, tokens_out=5)
            return ProviderResponse(text=str(item), tokens_in=10, tokens_out=5)
        return ProviderResponse(text=self.default_text, tokens_in=10, tokens_out=5)


def _json_loads(text: Optional[str]) -> Any:
    import json as _j
    if text is None:
        raise BadOutput("empty response")
    return _j.loads(text)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
_PROVIDER_CLASSES = {
    "gemini": GeminiProvider,
    "groq": GroqProvider,
    "deepseek": DeepSeekProvider,
    "openai": GenericOpenAIProvider,
}


def build_provider(provider: str, fetcher: AsyncFetcher, model: str,
                   api_key: Optional[str], timeout_s: float,
                   base_url: Optional[str] = None,
                   reasoning_effort: Optional[str] = None) -> BaseProvider:
    cls = _PROVIDER_CLASSES.get(provider)
    if cls is None:
        raise ValueError(f"unknown provider: {provider}")
    if cls is GenericOpenAIProvider:
        prov: BaseProvider = cls(fetcher, model, api_key, timeout_s, base_url or "")
    else:
        prov = cls(fetcher, model, api_key, timeout_s)
    # OpenAI-compatible providers (Groq/DeepSeek/generic) honor reasoning_effort.
    if reasoning_effort and isinstance(prov, OpenAICompatProvider):
        prov.reasoning_effort = reasoning_effort  # type: ignore[attr-defined]
    return prov
