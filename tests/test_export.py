"""Step 15 export bundle: consolidation, resolver wiring, collisions,
truncation, CSV round-trip, freshness-at-export, validation failures."""
import csv
import json
from datetime import timedelta

import pytest

from src.common.dates import utc_now
from src.export import bundle, consolidate, validate
from src.export.resolve_map import build_resolver, startup_collisions
from src.resolver.mapping_log import _ro
from src.storage.db import Database


# --------------------------------------------------------------------------- #
# seeding helpers
# --------------------------------------------------------------------------- #
def _iso(dt):
    return dt.isoformat()


def _seed_startup(db, slug, name, url=None):
    db.insert_startup({
        "slug": slug, "normalized_name": slug, "entity_name": name,
        "employee_count": 10, "website": f"https://{slug}.example",
        "domain": f"{slug}.example", "one_liner": "x", "batch": "W24",
        "tags": [], "source_name": "YC", "source_url": url or f"https://ycombinator.com/{slug}",
        "collected_at": _iso(utc_now())})


def _seed_product(db, slug, startup_name, product, pricing=None, ev="costs $9/month"):
    db.insert_product({
        "startup_slug": slug, "normalized_name": product.lower(),
        "startup_name": startup_name, "product_name": product,
        "pricing_model": pricing, "evidence_name": f"{product} is great",
        "evidence_pricing": ev if pricing else None,
        "page_url": f"https://{slug}.example/product", "source_name": "Company website",
        "llm_tier": "mock", "extracted_at": _iso(utc_now()), "collected_at": _iso(utc_now())})


def _seed_job(db, uh, company, when, fresh="VERIFIED_FRESH"):
    db.insert_job({
        "url_hash": uh, "url": f"https://jobs.example/{uh}", "source_name": "Greenhouse",
        "company_raw": company, "title": "Engineer", "is_remote": 1,
        "remote_basis": "api_flag", "role_family": "Engineering", "role_basis": "title_match",
        "published_date": _iso(when), "date_confidence": "EXPLICIT",
        "freshness_status": fresh, "date_source": "first_published", "content_hash": uh,
        "llm_tier": None, "collected_at": _iso(utc_now()), "first_seen_at": _iso(utc_now())})


def _seed_news(db, uh, when, fresh="VERIFIED_FRESH"):
    db.insert_news({
        "url_hash": uh, "url": f"https://news.example/{uh}", "title": "Headline",
        "author": "A", "published_date": _iso(when), "date_confidence": "EXPLICIT",
        "freshness_status": fresh, "date_source": "feed_published", "full_text": "body "*10,
        "summary": None, "content_hash": uh, "source_name": "TechCrunch",
        "source_url": f"https://news.example/{uh}", "collected_at": _iso(utc_now()),
        "first_seen_at": _iso(utc_now())})


# --------------------------------------------------------------------------- #
# 1. consolidation
# --------------------------------------------------------------------------- #
def test_consolidation_counts_and_idempotent(tmp_path):
    main = str(tmp_path / "main.db")
    src = str(tmp_path / "news.db")
    md, sd = Database(main), Database(src)
    now = utc_now()
    _seed_news(sd, "n1", now); _seed_news(sd, "n2", now)
    _seed_job(sd, "j1", "Acme", now)
    sd.mark_seen("u1", "c1", "s", _iso(now))
    sd.save_source_snapshot("TechCrunch", _iso(now), ["u1"])
    md.close(); sd.close()

    rep = consolidate.consolidate(main, src)
    assert rep["news"]["inserted"] == 2 and rep["news"]["main_after"] == 2
    assert rep["jobs"]["inserted"] == 1
    assert rep["seen_items"]["inserted"] == 1
    assert rep["source_state"]["inserted"] == 1
    for r in rep.values():
        assert r["main_after"] == r["expected_after"]

    rep2 = consolidate.consolidate(main, src)   # idempotent
    for t, r in rep2.items():
        assert r["inserted"] == 0, t
        assert r["main_after"] == rep[t]["main_after"]


def test_consolidation_schema_mismatch_raises(tmp_path):
    main = str(tmp_path / "m.db"); src = str(tmp_path / "s.db")
    Database(main).close()
    import sqlite3
    c = sqlite3.connect(src)
    c.execute("CREATE TABLE news (url_hash TEXT, extra TEXT)")  # wrong columns
    c.execute("CREATE TABLE jobs (x TEXT)")
    c.execute("CREATE TABLE seen_items (x TEXT)")
    c.execute("CREATE TABLE source_state (x TEXT)")
    c.commit(); c.close()
    with pytest.raises(ValueError, match="schema mismatch"):
        consolidate.consolidate(main, src)


# --------------------------------------------------------------------------- #
# 2. resolver applied consistently across tabs (referential integrity)
# --------------------------------------------------------------------------- #
def test_resolver_consistent_referential_integrity(tmp_path):
    db = Database(str(tmp_path / "s.db"))
    _seed_startup(db, "openai", "OpenAI, Inc.")       # seed alias -> OpenAI
    _seed_startup(db, "acme", "Acme Labs")
    _seed_product(db, "openai", "OpenAI, Inc.", "ChatGPT", "PAID")
    _seed_product(db, "acme", "Acme Labs", "Widget", "FREEMIUM")
    db.close()

    conn = _ro(str(tmp_path / "s.db"))
    resolver = build_resolver(conn)
    s_flats, _ = bundle.build_startups(conn, resolver)
    p_flats, _ = bundle.build_products(conn, resolver)
    conn.close()

    startup_names = {r["content.entityName"] for r in s_flats}
    # OpenAI, Inc. must canonicalize to the same value in both tabs.
    assert "OpenAI" in startup_names
    for p in p_flats:
        assert p["content.startupName"] in startup_names, p["content.startupName"]


# --------------------------------------------------------------------------- #
# 3. collision detection stops the bundle
# --------------------------------------------------------------------------- #
def test_collision_detection_stops_bundle(tmp_path):
    dbp = str(tmp_path / "s.db")
    db = Database(dbp)
    # Two DIFFERENT startups that clean to the same canonical.
    _seed_startup(db, "acme1", "Acme")
    _seed_startup(db, "acme2", "Acme, Inc.")
    db.close()

    conn = _ro(dbp)
    resolver = build_resolver(conn)
    coll = startup_collisions(conn, resolver)
    conn.close()
    assert len(coll) == 1 and coll[0]["canonical"] == "Acme"
    assert set(coll[0]["raw_names"]) == {"Acme", "Acme, Inc."}

    out = tmp_path / "sub"
    code = bundle.run_bundle(out=str(out), db_path=dbp)
    assert code == 2                                  # stopped
    assert "canonical collision" in (out / "VALIDATION.md").read_text()
    assert not (out / "Startups.csv").exists()        # nothing written


# --------------------------------------------------------------------------- #
# 4. truncation + 5. CSV round trip
# --------------------------------------------------------------------------- #
def test_truncation_marks_and_counts():
    trunc = {}
    big = "x" * 50_000
    out = bundle._fmt_cell(big, "content.full_text", trunc)
    assert out.endswith(" [truncated]")
    assert len(out) == bundle.CELL_CAP + len(" [truncated]")
    assert trunc["content.full_text"] == 1
    assert bundle._fmt_cell(True, "b", {}) == "TRUE"
    assert bundle._fmt_cell(False, "b", {}) == "FALSE"
    assert bundle._fmt_cell(None, "x", {}) == ""


def test_csv_round_trip(tmp_path):
    cols = ["a", "b", "c"]
    flats = [{"a": "he,llo", "b": True, "c": None},
             {"a": "li\nne", "b": False, "c": "x"}]
    objs = [{"a": "he,llo"}, {"a": "li\nne"}]
    rows, _ = bundle.write_tab(tmp_path, "T", cols, flats, objs, {})
    assert rows == 2
    with (tmp_path / "T.csv").open(encoding="utf-8", newline="") as fh:
        got = list(csv.DictReader(fh))
    assert got[0]["a"] == "he,llo" and got[0]["b"] == "TRUE" and got[0]["c"] == ""
    assert got[1]["a"] == "li\nne" and got[1]["b"] == "FALSE" and got[1]["c"] == "x"
    # JSONL preserves full content
    lines = (tmp_path / "T.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["a"] == "he,llo"


# --------------------------------------------------------------------------- #
# 6. freshness applied at export time
# --------------------------------------------------------------------------- #
def test_freshness_applied_at_export(tmp_path):
    dbp = str(tmp_path / "s.db")
    db = Database(dbp)
    now = utc_now()
    _seed_job(db, "fresh", "Acme", now - timedelta(hours=2))
    _seed_job(db, "stale", "Acme", now - timedelta(hours=48))
    _seed_news(db, "freshn", now - timedelta(hours=1))
    _seed_news(db, "stalen", now - timedelta(hours=40))
    db.close()

    conn = _ro(dbp)
    resolver = build_resolver(conn)
    jobs, _ = bundle.build_jobs(conn, resolver, now)
    news, _ = bundle.build_news(conn, now)
    conn.close()
    assert {j["source.url"].rsplit("/", 1)[-1] for j in jobs} == {"fresh"}
    assert len(news) == 1


# --------------------------------------------------------------------------- #
# 7. validation fails on seeded bad rows
# --------------------------------------------------------------------------- #
def _write_jsonl(d, tab, objs):
    (d / f"{tab}.jsonl").write_text(
        "\n".join(json.dumps(o) for o in objs) + "\n", encoding="utf-8")
    # minimal matching CSV so the UTF-8/length checks have a file
    (d / f"{tab}.csv").write_text("col\n", encoding="utf-8")


def _startup_obj(name, url="https://ycombinator.com/x"):
    return {"schemaVersion": "1.0", "recordType": "STARTUP",
            "source": {"name": "YC", "url": url}, "collectedAt": _iso(utc_now()),
            "content": {"entityName": name, "data": {"employeeCount": 1}},
            "raw_name": name}


def _product_obj(startup, name):
    return {"schemaVersion": "1.0", "recordType": "PRODUCT",
            "source": {"name": "web", "url": "https://x.example/p"},
            "collectedAt": _iso(utc_now()),
            "content": {"startupName": startup, "name": name, "pricingModel": None},
            "startup_raw": startup, "evidence_name": "e", "evidence_pricing": None}


def test_validation_fails_on_duplicate_startup(tmp_path):
    d = tmp_path / "sub"; d.mkdir()
    for tab in ("Startups", "Products", "Research Papers", "Jobs", "News",
                "Entity Mapping Log"):
        _write_jsonl(d, tab, [])
    _write_jsonl(d, "Startups", [_startup_obj("Dup"), _startup_obj("Dup")])
    code = validate.validate(str(d))
    assert code == 1
    assert "no duplicate startup canonical name" in (d / "VALIDATION.md").read_text()


def test_validation_fails_on_missing_url(tmp_path):
    d = tmp_path / "sub"; d.mkdir()
    for tab in ("Startups", "Products", "Research Papers", "Jobs", "News",
                "Entity Mapping Log"):
        _write_jsonl(d, tab, [])
    bad = _startup_obj("NoUrl"); bad["source"]["url"] = ""
    _write_jsonl(d, "Startups", [bad])
    code = validate.validate(str(d))
    assert code == 1


def test_entity_log_real_only_and_ordered(tmp_path):
    dbp = str(tmp_path / "s.db")
    db = Database(dbp)
    _seed_startup(db, "openai", "OpenAI, Inc.")   # seed alias -> non-trivial
    _seed_startup(db, "acme", "Acme Labs")        # trivial self
    _seed_product(db, "acme", "Acme Labs", "Widget", "FREE")
    conn = _ro(dbp)
    resolver = build_resolver(conn)
    flats, _ = bundle.build_entity_log(conn, resolver, db)
    conn.close(); db.close()
    # No synthetic rows leak into the live entity log.
    assert all("eval_corpus" not in (r["source_table"] or "") for r in flats)
    # Order: every non-trivial precedes every trivial self-match.
    trivial_seen = False
    for r in flats:
        is_trivial = r["method"] == "EXACT_SELF"
        if is_trivial:
            trivial_seen = True
        else:
            assert not trivial_seen, "a non-trivial row appeared after a trivial one"


def test_eval_corpus_accuracy_and_passfail():
    from src.export import eval_corpus
    rows, summary = eval_corpus.evaluate()
    assert summary["cases"] == len(rows)
    assert summary["positives"] >= 40 and summary["negatives"] >= 25
    assert summary["precision"] == 1.0
    assert summary["recall"] >= 0.90
    assert summary["false_merges"] == 0
    # Every row carries expected vs actual and a PASS/FAIL verdict.
    for r in rows:
        assert r["pass"] in ("PASS", "FAIL")
        assert "expected" in r and "actual_canonical" in r
    assert sum(1 for r in rows if r["pass"] == "FAIL") == len(summary["failures"])


def test_validation_fails_on_orphan_product(tmp_path):
    d = tmp_path / "sub"; d.mkdir()
    for tab in ("Startups", "Products", "Research Papers", "Jobs", "News",
                "Entity Mapping Log"):
        _write_jsonl(d, tab, [])
    _write_jsonl(d, "Startups", [_startup_obj("RealCo")])
    _write_jsonl(d, "Products", [_product_obj("GhostCo", "Widget")])  # orphan
    code = validate.validate(str(d))
    assert code == 1
    assert "orphan" in (d / "VALIDATION.md").read_text().lower()
