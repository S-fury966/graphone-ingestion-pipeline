"""JOB adapter parsing + deterministic field classifiers (offline)."""
import json
from pathlib import Path

from src.common.ai_keywords import is_ai_relevant
from src.crawler.jobs import (classify_role, decide_remote, load_board_slugs,
                              load_role_table)
from src.crawler.jobs import arbeitnow, ashby, greenhouse, hn_hiring, remoteok

FIX = Path(__file__).parent / "fixtures" / "jobs"


def _load(name):
    return (FIX / name).read_text(encoding="utf-8")


# ---- adapters ----
def test_arbeitnow_parse():
    items = arbeitnow.parse(_load("arbeitnow.json"))
    assert len(items) == 3
    j = items[0]
    assert j.company_raw == "NeuralCo" and j.date_field == "created_at"
    assert j.remote_flag is True and "ml" in j.tags


def test_remoteok_skips_legal_notice():
    items = remoteok.parse(_load("remoteok.json"))
    assert len(items) == 2  # legal notice dropped
    assert items[0].company_raw == "DataCorp" and items[0].remote_flag is True
    assert items[0].date_field == "date"


def test_ashby_parse_fields():
    items = ashby.parse(_load("ashby.json"), "Cohere")
    assert items[0].company_raw == "Cohere"
    assert items[0].date_field == "publishedAt" and items[0].remote_flag is True
    assert items[0].department == "Research" and items[0].listing_type == "complete"


def test_greenhouse_uses_first_published_not_updated():
    items = greenhouse.parse(_load("greenhouse_firstpub.json"), "Anthropic")
    assert items[0].date_field == "first_published"
    assert items[0].raw_date == "2026-11-22T06:00:00+00:00"
    assert items[0].raw_modified == "2026-11-22T06:00:00+00:00"  # updated_at kept for logs
    assert items[0].department == "Engineering"


def test_greenhouse_updated_only_is_undated():
    items = greenhouse.parse(_load("greenhouse_updated_only.json"), "Scale AI")
    assert items[0].raw_date is None and items[0].date_field is None
    assert items[0].raw_modified == "2026-11-22T06:00:00+00:00"


def test_hn_thread_and_comments():
    sid = hn_hiring.parse_thread(_load("hn_thread.json"))
    assert sid == "999"
    comments = hn_hiring.parse_comments(_load("hn_comments.json"), sid)
    # Both top-level comments kept (parent_id == story); the reply is dropped.
    # The non-job comment is filtered later by the grounded LLM step.
    assert len(comments) == 2
    c = comments[0]
    # provenance: comment's own created_at + the news.ycombinator permalink
    assert c.date_field == "comment_created_at"
    assert c.raw_date == "2026-11-22T06:00:00Z"
    assert c.url == "https://news.ycombinator.com/item?id=50518905"
    # HTML -> plain text: tags dropped, entities unescaped, line breaks kept
    body = c.comment_text
    assert "<p>" not in body and "<a" not in body and "&amp;" not in body
    assert "you'll ship" in body.lower()              # &#x27; -> '
    assert "features across the whole product & take" in body   # &amp; -> &
    assert "https://acme.ai/jobs/ml" in body          # anchor text kept
    assert "\n" in body                                # paragraph breaks preserved
    first_line = body.splitlines()[0]
    assert first_line.startswith("Acme AI | Senior ML Engineer")


# ---- classifiers ----
def test_decide_remote_all_bases():
    assert decide_remote(True, "NYC", "Engineer", []) == (True, "api_flag")
    assert decide_remote(None, "", "Remote Engineer", []) == (True, "text_match")
    assert decide_remote(None, "", "x", ["remote"]) == (True, "text_match")
    assert decide_remote(None, "San Francisco", "Engineer", []) == (False, "location_only")
    assert decide_remote(None, "", "Hybrid role", []) == (False, "location_only")
    assert decide_remote(None, "", "Engineer", []) == (False, "default_false")


def test_role_family_by_department_and_title():
    rt = load_role_table()
    assert classify_role("Engineering", "Account Exec", rt) == ("Engineering", "department")
    assert classify_role(None, "Senior Data Scientist", rt) == ("Data", "title_match")
    assert classify_role(None, "Research Scientist", rt) == ("Research", "title_match")
    assert classify_role("Sales", "Engineer", rt) == ("Sales", "department")
    assert classify_role(None, "Mysterious Wizard", rt) == ("Other", "default_other")


def test_ai_filter():
    assert is_ai_relevant("Machine Learning Engineer") is True
    assert is_ai_relevant("LLM infrastructure") is True
    assert is_ai_relevant("Senior Plumber for email team") is False


def test_board_slugs_loaded():
    b = load_board_slugs()
    assert b["greenhouse"] and b["ashby"]
    assert all("slug" in x and "company" in x for x in b["greenhouse"])
