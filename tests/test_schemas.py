"""Schema validation tests: valid records pass; each rejection rule fails."""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.schemas import (
    Job,
    News,
    Product,
    ResearchPaper,
    Startup,
)
from src.schemas.enums import DateConfidence, PricingModel

UTC_NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
NAIVE = datetime(2026, 11, 21, 12, 0, 0)
SRC = {"name": "arxiv", "url": "https://arxiv.org/abs/2401.00001"}


# ---------- valid records pass ----------

def test_valid_startup():
    s = Startup(source=SRC, collectedAt=UTC_NOW,
                content={"entityName": "OpenAI", "data": {"employeeCount": 500}})
    assert s.recordType == "STARTUP"
    assert s.content.data.employeeCount == 500


def test_valid_product():
    p = Product(source=SRC, collectedAt=UTC_NOW,
                content={"startupName": "OpenAI", "name": "ChatGPT",
                         "pricingModel": PricingModel.FREEMIUM})
    assert p.content.pricingModel == PricingModel.FREEMIUM.value


def test_product_pricing_model_optional_null():
    # No pricing evidence -> pricingModel is null (never guessed).
    p = Product(source=SRC, collectedAt=UTC_NOW,
                content={"startupName": "OpenAI", "name": "ChatGPT"})
    assert p.content.pricingModel is None


def test_valid_research_paper_with_github():
    r = ResearchPaper(source=SRC, collectedAt=UTC_NOW, content={
        "title": "Attention Is All You Need",
        "authors": ["Vaswani", "Shazeer"],
        "paper_url": "https://arxiv.org/abs/1706.03762",
        "published_date": UTC_NOW,
        "arxiv_id": "1706.03762",
        "github_url": "https://github.com/tensorflow/tensor2tensor",
        "github_stars": 15000,
    })
    assert r.content.github_stars == 15000


def test_valid_paper_without_github():
    r = ResearchPaper(source=SRC, collectedAt=UTC_NOW, content={
        "title": "Some Paper", "authors": ["A"],
        "paper_url": "https://arxiv.org/abs/2401.00002", "published_date": UTC_NOW,
    })
    assert r.content.github_url is None


def test_valid_job():
    j = Job(source=SRC, collectedAt=UTC_NOW, content={
        "company": "OpenAI", "title": "Research Engineer", "date": UTC_NOW,
        "is_remote": True, "role_family": "Engineering"})
    assert j.content.is_remote is True
    assert j.content.title == "Research Engineer"


def test_reject_job_missing_title():
    with pytest.raises(ValidationError):
        Job(source=SRC, collectedAt=UTC_NOW, content={
            "company": "OpenAI", "date": UTC_NOW, "is_remote": True,
            "role_family": "Engineering"})


def test_valid_news():
    n = News(source=SRC, collectedAt=UTC_NOW, content={
        "title": "Big AI news", "published_date": UTC_NOW,
        "full_text": "Lots of text.", "date_confidence": DateConfidence.EXPLICIT})
    assert n.content.date_confidence == DateConfidence.EXPLICIT.value


# ---------- rejection rules ----------

def test_reject_missing_source_url():
    with pytest.raises(ValidationError):
        Startup(source={"name": "x"}, collectedAt=UTC_NOW,
                content={"entityName": "OpenAI"})


def test_reject_invalid_source_url():
    with pytest.raises(ValidationError):
        Startup(source={"name": "x", "url": "not-a-url"}, collectedAt=UTC_NOW,
                content={"entityName": "OpenAI"})


def test_reject_invalid_paper_url():
    with pytest.raises(ValidationError):
        ResearchPaper(source=SRC, collectedAt=UTC_NOW, content={
            "title": "T", "authors": ["A"], "paper_url": "ftp://bad",
            "published_date": UTC_NOW})


def test_reject_empty_entity_name():
    with pytest.raises(ValidationError):
        Startup(source=SRC, collectedAt=UTC_NOW, content={"entityName": "   "})


def test_reject_negative_employee_count():
    with pytest.raises(ValidationError):
        Startup(source=SRC, collectedAt=UTC_NOW,
                content={"entityName": "OpenAI", "data": {"employeeCount": -5}})


def test_reject_stars_without_github_url():
    with pytest.raises(ValidationError):
        ResearchPaper(source=SRC, collectedAt=UTC_NOW, content={
            "title": "T", "authors": ["A"],
            "paper_url": "https://arxiv.org/abs/2401.1", "published_date": UTC_NOW,
            "github_stars": 10})


def test_reject_naive_collected_at():
    with pytest.raises(ValidationError):
        Startup(source=SRC, collectedAt=NAIVE, content={"entityName": "OpenAI"})


def test_reject_bad_pricing_enum():
    with pytest.raises(ValidationError):
        Product(source=SRC, collectedAt=UTC_NOW, content={
            "startupName": "OpenAI", "name": "ChatGPT", "pricingModel": "CHEAP"})


# ---------- helpers ----------

def test_flat_dict_and_json():
    s = Startup(source=SRC, collectedAt=UTC_NOW,
                content={"entityName": "OpenAI", "data": {"employeeCount": 500}})
    flat = s.to_flat_dict()
    assert flat["content.entityName"] == "OpenAI"
    assert flat["content.data.employeeCount"] == 500
    assert flat["recordType"] == "STARTUP"
    assert "source.url" in flat
    assert '"recordType":"STARTUP"' in s.to_json()


def test_collected_at_normalized_to_utc():
    offset = datetime(2026, 11, 21, 17, 0, 0,
                      tzinfo=timezone(__import__("datetime").timedelta(hours=5)))
    s = Startup(source=SRC, collectedAt=offset, content={"entityName": "OpenAI"})
    assert s.collectedAt.tzinfo == timezone.utc
    assert s.collectedAt.hour == 12
