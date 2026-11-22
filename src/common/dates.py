"""Deterministic date/time engine for freshness.

Everything is parsed into **timezone-aware UTC**. Two jobs:

  * ``parse_date(raw, now, source_tz)`` — turn a raw date string/number from a
    feed or an article into a :class:`ParsedDate` (UTC datetime + a
    :class:`DateConfidence` of EXPLICIT or RELATIVE + an uncertainty margin for
    relative strings). Dates more than 1 hour in the future are **rejected**
    (``status == "FUTURE_DATE"``). Unparseable input returns ``None``.
  * ``is_fresh(parsed, now, window_hours)`` — exact, tested window test. For a
    relative date the **whole uncertainty range** must fit inside the window.

The LLM is never involved here: freshness is decided only by this module and
``src.common.freshness``. ``dateparser`` is used only as a wrapped, deterministic
fallback for odd absolute strings (fixed base time, forced UTC output).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..schemas.enums import DateConfidence
from .logging import get_logger

log = get_logger("common.dates")

# A clock a little ahead of a publisher's is normal; more than this in the
# future means a bad/garbage timestamp, so we reject it.
FUTURE_SLACK = timedelta(hours=1)

STATUS_OK = "OK"
STATUS_FUTURE = "FUTURE_DATE"


@dataclass
class ParsedDate:
    """A normalized timestamp plus how sure we are of it.

    ``dt`` is always timezone-aware UTC. ``margin`` is the +/- uncertainty
    (zero for explicit timestamps). ``verifiable`` is False for coarse relative
    strings ("yesterday", "last week") that are too imprecise to ever prove
    freshness — the freshness engine must route those through the snapshot
    heuristic instead of trusting them.
    """

    dt: datetime
    confidence: DateConfidence
    status: str = STATUS_OK
    margin: timedelta = timedelta(0)
    tz_assumed: bool = False
    verifiable: bool = True
    raw: str = ""
    # Where the date came from (filled in by article_dates / the pipeline):
    # feed_published | jsonld | meta | url_pattern | relative_text | snapshot
    source: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


def utc_now() -> datetime:
    """The one true 'now'. Never hardcode a value; always call this."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Relative English strings
# ---------------------------------------------------------------------------
_UNIT_MARGIN = {
    "second": timedelta(minutes=1),   # sub-minute precision floored to +-1 min
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=31),
    "year": timedelta(days=366),
}
_UNIT_DELTA = {
    "second": timedelta(seconds=1),
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
    "year": timedelta(days=365),
}
# Units coarser than a day can never be verified against a 24h window.
_VERIFIABLE_UNITS = {"second", "minute", "hour", "day"}

_REL_NUM = re.compile(
    r"\b(?:(\d+)|an?|a\s+few)\s+"
    r"(second|minute|hour|day|week|month|year)s?\s+ago\b",
    re.IGNORECASE,
)


def _parse_relative(s: str, now: datetime) -> Optional[tuple[datetime, timedelta, bool]]:
    low = s.strip().lower()
    if low in ("just now", "moments ago", "a moment ago", "now"):
        return now, timedelta(minutes=1), True
    if low in ("yesterday",):
        return now - timedelta(days=1), timedelta(days=1), False
    if low in ("last week", "a week ago"):
        return now - timedelta(weeks=1), timedelta(days=7), False
    if low in ("last month", "a month ago"):
        return now - timedelta(days=30), timedelta(days=31), False
    if low in ("last year", "a year ago"):
        return now - timedelta(days=365), timedelta(days=366), False

    m = _REL_NUM.search(low)
    if not m:
        return None
    num_raw, unit = m.group(1), m.group(2).lower()
    n = int(num_raw) if num_raw else 1  # "an hour ago" / "a few ... ago" -> 1
    delta = _UNIT_DELTA[unit] * n
    margin = _UNIT_MARGIN[unit]
    verifiable = unit in _VERIFIABLE_UNITS
    return now - delta, margin, verifiable


# ---------------------------------------------------------------------------
# Absolute timestamps
# ---------------------------------------------------------------------------
_EPOCH_S = re.compile(r"^\d{10}$")
_EPOCH_MS = re.compile(r"^\d{13}$")


def _parse_epoch(s: str) -> Optional[datetime]:
    s = s.strip()
    if _EPOCH_S.match(s):
        return datetime.fromtimestamp(int(s), timezone.utc)
    if _EPOCH_MS.match(s):
        return datetime.fromtimestamp(int(s) / 1000.0, timezone.utc)
    return None


def _apply_tz(dt: datetime, source_tz: Optional[str]) -> tuple[datetime, bool]:
    """Make a datetime aware. Returns (aware_dt, tz_was_assumed)."""
    if dt.tzinfo is not None:
        return dt, False
    if source_tz:
        try:
            return dt.replace(tzinfo=ZoneInfo(source_tz)), False
        except (ZoneInfoNotFoundError, ValueError):
            log.warning("unknown source_tz; assuming UTC",
                        extra={"extra": {"source_tz": source_tz}})
    return dt.replace(tzinfo=timezone.utc), True


def _parse_absolute(s: str, source_tz: Optional[str]) -> Optional[tuple[datetime, bool]]:
    s = s.strip()
    # ISO-8601 (with offset, Z, or naive)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return _apply_tz(dt, source_tz)
    except ValueError:
        pass
    # RFC-822 / RFC-1123 (GMT and +0000 forms)
    try:
        dt = parsedate_to_datetime(s)
        if dt is not None:
            return _apply_tz(dt, source_tz)
    except (TypeError, ValueError):
        pass
    return None


def _dateparser_absolute(s: str, source_tz: Optional[str], now: datetime
                         ) -> Optional[tuple[datetime, bool]]:
    """Wrapped dateparser fallback, pinned deterministic.

    Fixed relative base (``now``), input/output timezones forced, so the same
    string always yields the same UTC instant regardless of the host clock/TZ.
    """
    try:
        import dateparser
    except ImportError:  # pragma: no cover - dateparser is a declared dep
        return None
    settings = {
        "RETURN_AS_TIMEZONE_AWARE": True,
        "TIMEZONE": source_tz or "UTC",
        "TO_TIMEZONE": "UTC",
        "RELATIVE_BASE": now.replace(tzinfo=None),
        "PREFER_DATES_FROM": "past",
    }
    try:
        dt = dateparser.parse(s, settings=settings)
    except Exception:  # noqa: BLE001 - never let the fallback raise
        return None
    if dt is None:
        return None
    assumed = source_tz is None
    return dt.astimezone(timezone.utc), assumed


def _finalize(dt: datetime, confidence: DateConfidence, now: datetime, *,
              margin: timedelta = timedelta(0), tz_assumed: bool = False,
              verifiable: bool = True, raw: str = "") -> ParsedDate:
    dt = dt.astimezone(timezone.utc)
    if tz_assumed:
        log.info("timezone assumed UTC (naive timestamp)",
                 extra={"extra": {"raw": raw}})
    status = STATUS_OK
    if dt > now + FUTURE_SLACK:
        status = STATUS_FUTURE
        log.warning("rejected future date",
                    extra={"extra": {"raw": raw, "parsed": dt.isoformat()}})
    return ParsedDate(dt=dt, confidence=confidence, status=status, margin=margin,
                      tz_assumed=tz_assumed, verifiable=verifiable, raw=raw)


def parse_date(raw: Optional[Union[str, int, float]], now: Optional[datetime] = None,
               source_tz: Optional[str] = None) -> Optional[ParsedDate]:
    """Parse one raw date value into a UTC :class:`ParsedDate`, or ``None``.

    Handles ISO-8601 (offset/Z/naive), RFC-822 (GMT & +0000), epoch seconds and
    milliseconds, and English relative strings. Naive timestamps use
    ``source_tz`` when given, else UTC (logged as assumed). Future dates (> 1h
    ahead of ``now``) come back with ``status == "FUTURE_DATE"``.
    """
    now = now or utc_now()
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None

    rel = _parse_relative(s, now)
    if rel is not None:
        dt, margin, verifiable = rel
        return _finalize(dt, DateConfidence.RELATIVE, now, margin=margin,
                         verifiable=verifiable, raw=s)

    epoch = _parse_epoch(s)
    if epoch is not None:
        return _finalize(epoch, DateConfidence.EXPLICIT, now, raw=s)

    absolute = _parse_absolute(s, source_tz)
    if absolute is not None:
        dt, assumed = absolute
        return _finalize(dt, DateConfidence.EXPLICIT, now, tz_assumed=assumed, raw=s)

    fallback = _dateparser_absolute(s, source_tz, now)
    if fallback is not None:
        dt, assumed = fallback
        return _finalize(dt, DateConfidence.EXPLICIT, now, tz_assumed=assumed, raw=s)

    return None


def is_fresh(parsed: Union[ParsedDate, datetime, None], now: Optional[datetime] = None,
             window_hours: int = 24) -> bool:
    """True if the timestamp falls inside the last ``window_hours``.

    Boundary is inclusive at the cutoff (``now - window``). For a relative date
    the entire uncertainty range ``[dt-margin, dt+margin]`` must fit within
    ``[cutoff, now + FUTURE_SLACK]`` — a date whose range spills past the window
    edge is not provably fresh.
    """
    now = now or utc_now()
    if parsed is None:
        return False
    if isinstance(parsed, datetime):
        dt, margin = parsed, timedelta(0)
    else:
        if not parsed.ok:
            return False
        dt, margin = parsed.dt, parsed.margin
    cutoff = now - timedelta(hours=window_hours)
    earliest = dt - margin
    latest = dt + margin
    return earliest >= cutoff and latest <= now + FUTURE_SLACK


def parse_http_date(value: Optional[str]) -> Optional[datetime]:
    """Parse an HTTP ``Date`` header into aware UTC, or None."""
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def check_clock_skew(now: datetime, server_date_header: Optional[str],
                     max_skew: timedelta = timedelta(minutes=5)) -> Optional[float]:
    """Compare the run's ``now`` with a server ``Date`` header.

    Logs a loud warning when they differ by more than ``max_skew``. Returns the
    absolute skew in seconds, or None if the header was unusable.
    """
    server = parse_http_date(server_date_header)
    if server is None:
        return None
    skew = abs((now - server).total_seconds())
    if skew > max_skew.total_seconds():
        log.warning(
            "CLOCK SKEW: local now differs from server Date by %.0fs (> %ds) — "
            "freshness windows may be wrong" % (skew, int(max_skew.total_seconds())),
            extra={"extra": {"local_now": now.isoformat(),
                             "server_date": server.isoformat(),
                             "skew_seconds": round(skew, 1)}})
    return skew
