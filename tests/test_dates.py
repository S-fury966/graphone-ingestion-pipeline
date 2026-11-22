"""Date engine tests — deterministic, offline, fixed `now`."""
from datetime import datetime, timedelta, timezone

from src.common.dates import (FUTURE_SLACK, check_clock_skew, is_fresh,
                              parse_date, utc_now)
from src.schemas.enums import DateConfidence

NOW = datetime(2026, 11, 21, 21, 0, 0, tzinfo=timezone.utc)


# ---- absolute formats ----
def test_iso_with_offset():
    p = parse_date("2026-11-21T15:03:37-05:00", now=NOW)
    assert p.ok and p.confidence == DateConfidence.EXPLICIT
    assert p.dt == datetime(2026, 11, 21, 20, 3, 37, tzinfo=timezone.utc)
    assert p.tz_assumed is False


def test_iso_z():
    p = parse_date("2026-11-21T18:29:29Z", now=NOW)
    assert p.dt.hour == 18 and p.dt.tzinfo == timezone.utc


def test_iso_naive_assumes_utc_and_flags():
    p = parse_date("2026-11-21T14:58:42", now=NOW)
    assert p.ok and p.tz_assumed is True
    assert p.dt == datetime(2026, 11, 21, 14, 58, 42, tzinfo=timezone.utc)


def test_iso_naive_uses_source_tz():
    # 14:58 US/Eastern (-05:00) -> 19:58 UTC
    p = parse_date("2026-11-21T14:58:42", now=NOW, source_tz="America/New_York")
    assert p.dt == datetime(2026, 11, 21, 19, 58, 42, tzinfo=timezone.utc)
    assert p.tz_assumed is False


def test_rfc822_plus0000_and_gmt_agree():
    a = parse_date("Sat, 21 Nov 2026 12:01:13 +0000", now=NOW)
    b = parse_date("Sat, 21 Nov 2026 12:01:13 GMT", now=NOW)
    assert a.dt == b.dt == datetime(2026, 11, 21, 12, 1, 13, tzinfo=timezone.utc)


def test_epoch_seconds_and_millis():
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
    secs = int(base.timestamp())
    assert parse_date(str(secs), now=NOW).dt == base
    assert parse_date(str(secs * 1000), now=NOW).dt == base


# ---- relative formats ----
def test_relative_minutes():
    p = parse_date("5 minutes ago", now=NOW)
    assert p.confidence == DateConfidence.RELATIVE
    assert p.dt == NOW - timedelta(minutes=5)
    assert p.margin == timedelta(minutes=1) and p.verifiable is True


def test_relative_hours():
    p = parse_date("2 hours ago", now=NOW)
    assert p.dt == NOW - timedelta(hours=2)
    assert p.margin == timedelta(hours=1)


def test_relative_an_hour():
    p = parse_date("an hour ago", now=NOW)
    assert p.dt == NOW - timedelta(hours=1)


def test_just_now():
    p = parse_date("just now", now=NOW)
    assert p.dt == NOW and p.confidence == DateConfidence.RELATIVE


def test_yesterday_is_coarse_unverifiable():
    p = parse_date("yesterday", now=NOW)
    assert p.confidence == DateConfidence.RELATIVE
    assert p.verifiable is False


def test_days_ago_verifiable():
    p = parse_date("3 days ago", now=NOW)
    assert p.dt == NOW - timedelta(days=3) and p.verifiable is True


def test_week_ago_unverifiable():
    assert parse_date("a week ago", now=NOW).verifiable is False


# ---- future rejection ----
def test_future_date_rejected():
    p = parse_date("2026-11-25T00:00:00Z", now=NOW)
    assert p.status == "FUTURE_DATE" and not p.ok


def test_small_future_within_slack_ok():
    soon = (NOW + timedelta(minutes=30)).isoformat()
    assert parse_date(soon, now=NOW).ok


# ---- unparseable ----
def test_unparseable_returns_none():
    assert parse_date("not a date at all", now=NOW) is None
    assert parse_date("", now=NOW) is None
    assert parse_date(None, now=NOW) is None


# ---- is_fresh boundary ----
def test_is_fresh_within_window():
    assert is_fresh(parse_date("2 hours ago", now=NOW), NOW, 24) is True


def test_is_fresh_exact_cutoff_inclusive():
    exactly = (NOW - timedelta(hours=24)).isoformat()
    assert is_fresh(parse_date(exactly, now=NOW), NOW, 24) is True


def test_is_fresh_just_outside():
    just_out = (NOW - timedelta(hours=24, seconds=1)).isoformat()
    assert is_fresh(parse_date(just_out, now=NOW), NOW, 24) is False


def test_relative_range_must_fit_window():
    # 23h30m ago +-1h -> range reaches 24h30m back -> does NOT fit 24h window.
    p = parse_date("1410 minutes ago", now=NOW)  # 23.5h
    # margin is +-1 minute for minutes-grain, so this one DOES fit:
    assert is_fresh(p, NOW, 24) is True
    # "23 hours ago" +-1h reaches exactly 24h back == cutoff -> still fits.
    assert is_fresh(parse_date("23 hours ago", now=NOW), NOW, 24) is True
    # "24 hours ago" +-1h reaches 25h back -> overflows the window -> not fresh.
    assert is_fresh(parse_date("24 hours ago", now=NOW), NOW, 24) is False


def test_is_fresh_stale():
    assert is_fresh(parse_date("3 days ago", now=NOW), NOW, 24) is False


def test_is_fresh_none_and_future():
    assert is_fresh(None, NOW, 24) is False
    assert is_fresh(parse_date("2026-11-30T00:00:00Z", now=NOW), NOW, 24) is False


# ---- clock skew ----
def test_clock_skew_within_tolerance():
    hdr = "Sat, 21 Nov 2026 21:02:00 +0000"   # 2 min off
    skew = check_clock_skew(NOW, hdr)
    assert skew is not None and skew <= 300


def test_clock_skew_large(caplog):
    hdr = "Sat, 21 Nov 2026 20:00:00 +0000"   # 1h off
    skew = check_clock_skew(NOW, hdr)
    assert skew == 3600


def test_clock_skew_bad_header():
    assert check_clock_skew(NOW, "garbage") is None
    assert check_clock_skew(NOW, None) is None


def test_utc_now_is_aware():
    assert utc_now().tzinfo == timezone.utc
