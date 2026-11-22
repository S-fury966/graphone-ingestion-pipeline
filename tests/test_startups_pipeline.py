"""Startups pipeline tests: idempotency + validation (no real internet)."""
import json
from pathlib import Path

from src.crawler.yc_startups import dedup, map_company
from src.pipeline.startups import Stats, persist
from src.storage.db import Database

FIXTURE = Path(__file__).parent / "fixtures" / "yc_sample.json"


def _records():
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    kept, _ = dedup([map_company(r) for r in raw])
    return kept


def test_persist_and_idempotent_rerun(tmp_path):
    db = Database(tmp_path / "state.db")
    records = _records()

    s1 = Stats()
    persist(db, records, s1)
    first_total = db.count_startups()
    assert first_total == len(records) == 7
    assert s1.inserted == 7

    # Rerun: nothing new is added (idempotent).
    s2 = Stats()
    persist(db, records, s2)
    assert s2.inserted == 0
    assert db.count_startups() == first_total
    db.close()


def test_with_employee_count_counted(tmp_path):
    db = Database(tmp_path / "state.db")
    stats = Stats()
    persist(db, _records(), stats)
    # openai-x(500), dupdomain-a(10), badweb(7) have positive team_size.
    assert stats.with_employee_count == 3
    db.close()


def test_dry_run_does_not_persist(tmp_path):
    db = Database(tmp_path / "state.db")
    stats = Stats()
    persist(db, _records(), stats, dry_run=True)
    assert stats.inserted == 7
    assert db.count_startups() == 0
    db.close()


def test_all_records_have_valid_source_url(tmp_path):
    db = Database(tmp_path / "state.db")
    persist(db, _records(), Stats())
    for row in db.iter_startups():
        assert row["source_url"].startswith("https://")
    db.close()
