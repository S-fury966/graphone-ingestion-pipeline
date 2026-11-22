"""SQLite state: papers table + GitHub repo cache. Stored in data/state.db."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator, Optional

DEFAULT_DB_PATH = "data/state.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    arxiv_id         TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    authors          TEXT NOT NULL,         -- JSON array
    published        TEXT NOT NULL,         -- ISO-8601 UTC
    abs_url          TEXT NOT NULL,
    pdf_url          TEXT,
    abstract         TEXT,
    comment          TEXT,
    journal_ref      TEXT,
    github_url       TEXT NOT NULL,
    github_stars     INTEGER NOT NULL,
    stars_fetched_at TEXT NOT NULL,
    source_name      TEXT NOT NULL,
    source_url       TEXT NOT NULL,
    collected_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS startups (
    slug            TEXT PRIMARY KEY,
    normalized_name TEXT UNIQUE NOT NULL,
    entity_name     TEXT NOT NULL,
    employee_count  INTEGER,
    website         TEXT,
    domain          TEXT,
    one_liner       TEXT,
    batch           TEXT,
    tags            TEXT,                   -- JSON array
    source_name     TEXT NOT NULL,
    source_url      TEXT NOT NULL,
    collected_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS github_cache (
    repo_key   TEXT PRIMARY KEY,            -- owner/repo lowercased
    exists_    INTEGER NOT NULL,
    stars      INTEGER,
    html_url   TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crawl_status (
    slug          TEXT PRIMARY KEY,
    status        TEXT NOT NULL,            -- ok|blocked|js_only|robots_disallowed|http_error|no_website
    pages_fetched INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    checked_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    startup_slug     TEXT NOT NULL,
    normalized_name  TEXT NOT NULL,
    startup_name     TEXT NOT NULL,         -- canonical, from our startups table
    product_name     TEXT NOT NULL,
    pricing_model    TEXT,                  -- FREE|FREEMIUM|PAID|ENTERPRISE|NULL
    evidence_name    TEXT,
    evidence_pricing TEXT,
    page_url         TEXT NOT NULL,
    source_name      TEXT NOT NULL,
    llm_tier         TEXT,
    extracted_at     TEXT NOT NULL,
    collected_at     TEXT NOT NULL,
    PRIMARY KEY (startup_slug, normalized_name)
);

-- Per-tier cooldown persisted across runs: when a 429's reset exceeds max_wait_s
-- we park the tier until this epoch so reruns don't retry a daily-capped tier.
CREATE TABLE IF NOT EXISTS tier_state (
    tier_name       TEXT PRIMARY KEY,
    exhausted_until REAL NOT NULL DEFAULT 0,   -- unix epoch seconds
    reason          TEXT,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key  TEXT PRIMARY KEY,            -- hash(task, prompt_ver, source_text, ...)
    data       TEXT NOT NULL,               -- JSON extracted data
    evidence   TEXT NOT NULL,               -- JSON evidence map
    tier_used  TEXT,
    created_at TEXT NOT NULL
);

-- ---- Signal ingestion (Phase II): NEWS ----
-- freshness_status / date_source are stored here (and exported) but are NOT on
-- the Pydantic record models, which forbid extra fields.
CREATE TABLE IF NOT EXISTS news (
    url_hash         TEXT PRIMARY KEY,
    url              TEXT NOT NULL,
    title            TEXT NOT NULL,
    author           TEXT,
    published_date   TEXT NOT NULL,         -- ISO-8601 UTC
    date_confidence  TEXT NOT NULL,         -- EXPLICIT|RELATIVE|HEURISTIC
    freshness_status TEXT NOT NULL,         -- VERIFIED_FRESH|INFERRED_FRESH
    date_source      TEXT NOT NULL,         -- feed_published|jsonld|meta|url_pattern|relative_text|snapshot
    full_text        TEXT NOT NULL,
    summary          TEXT,
    content_hash     TEXT NOT NULL,
    source_name      TEXT NOT NULL,
    source_url       TEXT NOT NULL,
    collected_at     TEXT NOT NULL,
    first_seen_at    TEXT NOT NULL
);

-- Per-source high-water mark: the last COMPLETE listing we saw (for the
-- "new since last run" snapshot heuristic).
CREATE TABLE IF NOT EXISTS source_state (
    source_name      TEXT PRIMARY KEY,
    last_snapshot_at TEXT,                  -- fetch-start time of last full listing
    url_hashes       TEXT,                  -- JSON array of url_hashes in that listing
    updated_at       TEXT NOT NULL
);

-- Global seen-URL / seen-content ledger for dedup + first_seen_at high-water.
CREATE TABLE IF NOT EXISTS seen_items (
    url_hash      TEXT PRIMARY KEY,
    content_hash  TEXT,
    source_name   TEXT,
    first_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seen_content ON seen_items(content_hash);

-- ---- Signal ingestion (Phase II): JOBS ----
-- A job is stored once, ever (UNIQUE url_hash). freshness_status / date_source /
-- remote_basis / role_basis / company_raw are stored + exported but are NOT on
-- the Pydantic model. Export re-checks freshness at the current time.
CREATE TABLE IF NOT EXISTS jobs (
    url_hash         TEXT PRIMARY KEY,
    url              TEXT NOT NULL,
    source_name      TEXT NOT NULL,
    company_raw      TEXT NOT NULL,
    title            TEXT NOT NULL,
    is_remote        INTEGER NOT NULL,
    remote_basis     TEXT NOT NULL,
    role_family      TEXT NOT NULL,
    role_basis       TEXT NOT NULL,
    published_date   TEXT NOT NULL,         -- ISO-8601 UTC
    date_confidence  TEXT NOT NULL,         -- EXPLICIT|RELATIVE|HEURISTIC
    freshness_status TEXT NOT NULL,         -- VERIFIED_FRESH|INFERRED_FRESH
    date_source      TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    llm_tier         TEXT,
    collected_at     TEXT NOT NULL,
    first_seen_at    TEXT NOT NULL
);

-- ---- Entity resolution (Phase II / Step 13): raw -> canonical mapping log ----
-- One row per distinct raw name per entity_type. The raw values in the source
-- tables are NEVER modified; this is an additive audit log.
CREATE TABLE IF NOT EXISTS entity_map (
    entity_type   TEXT NOT NULL,              -- STARTUP|PRODUCT
    raw_name      TEXT NOT NULL,
    canonical     TEXT NOT NULL,
    method        TEXT NOT NULL,              -- EXACT|DOMAIN|TABLE|FUZZY|NEW_ENTITY|EXACT_SELF
    score         REAL,                       -- NULL for NEW_ENTITY
    matched_alias TEXT,
    source_table  TEXT NOT NULL,              -- comma-joined set: startups,products,jobs
    occurrences   INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (entity_type, raw_name)
);
"""


class Database:
    def __init__(self, path: str | Path = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        # Be a good citizen when another pipeline process shares this DB file:
        # wait (up to 5s) for a lock instead of erroring immediately.
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- papers ----
    def paper_exists(self, arxiv_id: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM papers WHERE arxiv_id = ?", (arxiv_id,))
        return cur.fetchone() is not None

    def count_papers(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]

    def insert_paper(self, record: dict[str, Any]) -> bool:
        """Insert a verified paper. Returns False if arxiv_id already present."""
        record = dict(record)
        record["authors"] = json.dumps(record["authors"])
        cols = ("arxiv_id", "title", "authors", "published", "abs_url", "pdf_url",
                "abstract", "comment", "journal_ref", "github_url", "github_stars",
                "stars_fetched_at", "source_name", "source_url", "collected_at")
        placeholders = ", ".join("?" for _ in cols)
        try:
            self.conn.execute(
                f"INSERT INTO papers ({', '.join(cols)}) VALUES ({placeholders})",
                tuple(record.get(c) for c in cols),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def update_stars(self, arxiv_id: str, stars: int, fetched_at: str) -> None:
        self.conn.execute(
            "UPDATE papers SET github_stars = ?, stars_fetched_at = ? WHERE arxiv_id = ?",
            (stars, fetched_at, arxiv_id),
        )
        self.conn.commit()

    def iter_papers(self) -> Iterator[sqlite3.Row]:
        yield from self.conn.execute("SELECT * FROM papers ORDER BY published DESC")

    # ---- startups ----
    def startup_exists(self, slug: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM startups WHERE slug = ?", (slug,))
        return cur.fetchone() is not None

    def count_startups(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM startups").fetchone()[0]

    def insert_startup(self, record: dict[str, Any]) -> bool:
        """Insert a startup. Returns False if slug or normalized_name already present."""
        record = dict(record)
        record["tags"] = json.dumps(record.get("tags") or [])
        cols = ("slug", "normalized_name", "entity_name", "employee_count", "website",
                "domain", "one_liner", "batch", "tags", "source_name", "source_url",
                "collected_at")
        placeholders = ", ".join("?" for _ in cols)
        try:
            cur = self.conn.execute(
                f"INSERT OR IGNORE INTO startups ({', '.join(cols)}) VALUES ({placeholders})",
                tuple(record.get(c) for c in cols),
            )
            self.conn.commit()
            return cur.rowcount > 0
        except sqlite3.IntegrityError:
            return False

    def iter_startups(self) -> Iterator[sqlite3.Row]:
        yield from self.conn.execute("SELECT * FROM startups ORDER BY entity_name")

    # ---- crawl status ----
    def get_crawl_status(self, slug: str) -> Optional[str]:
        row = self.conn.execute("SELECT status FROM crawl_status WHERE slug = ?",
                                (slug,)).fetchone()
        return row["status"] if row else None

    def set_crawl_status(self, slug: str, status: str, pages_fetched: int = 0,
                         error: Optional[str] = None) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO crawl_status (slug, status, pages_fetched, error, checked_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(slug) DO UPDATE SET "
            "status=excluded.status, pages_fetched=excluded.pages_fetched, "
            "error=excluded.error, checked_at=excluded.checked_at",
            (slug, status, pages_fetched, error, now))
        self.conn.commit()

    # ---- products ----
    def product_exists(self, slug: str, normalized_name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM products WHERE startup_slug = ? AND normalized_name = ?",
            (slug, normalized_name)).fetchone()
        return row is not None

    def count_products(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]

    def insert_product(self, record: dict[str, Any]) -> bool:
        cols = ("startup_slug", "normalized_name", "startup_name", "product_name",
                "pricing_model", "evidence_name", "evidence_pricing", "page_url",
                "source_name", "llm_tier", "extracted_at", "collected_at")
        placeholders = ", ".join("?" for _ in cols)
        cur = self.conn.execute(
            f"INSERT OR IGNORE INTO products ({', '.join(cols)}) VALUES ({placeholders})",
            tuple(record.get(c) for c in cols))
        self.conn.commit()
        return cur.rowcount > 0

    def iter_products(self) -> Iterator[sqlite3.Row]:
        yield from self.conn.execute("SELECT * FROM products ORDER BY startup_name, product_name")

    def delete_products_for(self, slug: str) -> int:
        cur = self.conn.execute("DELETE FROM products WHERE startup_slug = ?", (slug,))
        self.conn.commit()
        return cur.rowcount

    def delete_crawl_status(self, slug: str) -> None:
        self.conn.execute("DELETE FROM crawl_status WHERE slug = ?", (slug,))
        self.conn.commit()

    # ---- github cache ----
    def get_repo_cache(self, key: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            'SELECT exists_ AS "exists", stars, html_url FROM github_cache WHERE repo_key = ?',
            (key,),
        ).fetchone()
        return dict(row) if row else None

    def set_repo_cache(self, key: str, exists: bool, stars: Optional[int],
                       html_url: Optional[str]) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO github_cache (repo_key, exists_, stars, html_url, fetched_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(repo_key) DO UPDATE SET "
            "exists_=excluded.exists_, stars=excluded.stars, "
            "html_url=excluded.html_url, fetched_at=excluded.fetched_at",
            (key, 1 if exists else 0, stars, html_url, now),
        )
        self.conn.commit()

    # ---- tier state (per-tier cooldown across runs) ----
    def get_tier_exhausted_until(self, tier_name: str) -> float:
        row = self.conn.execute(
            "SELECT exhausted_until FROM tier_state WHERE tier_name = ?",
            (tier_name,)).fetchone()
        return float(row["exhausted_until"]) if row else 0.0

    def all_tier_exhausted_until(self) -> dict[str, float]:
        return {r["tier_name"]: float(r["exhausted_until"])
                for r in self.conn.execute(
                    "SELECT tier_name, exhausted_until FROM tier_state")}

    def set_tier_exhausted(self, tier_name: str, exhausted_until: float,
                           reason: Optional[str] = None) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO tier_state (tier_name, exhausted_until, reason, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(tier_name) DO UPDATE SET "
            "exhausted_until=excluded.exhausted_until, reason=excluded.reason, "
            "updated_at=excluded.updated_at",
            (tier_name, float(exhausted_until), reason, now))
        self.conn.commit()

    # ---- products: pricing audit update ----
    def update_product_pricing(self, startup_slug: str, normalized_name: str,
                               pricing_model: Optional[str]) -> None:
        self.conn.execute(
            "UPDATE products SET pricing_model = ? "
            "WHERE startup_slug = ? AND normalized_name = ?",
            (pricing_model, startup_slug, normalized_name))
        self.conn.commit()

    # ---- llm cache ----
    def get_llm_cache(self, key: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT data, evidence, tier_used FROM llm_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
        if not row:
            return None
        return {"data": json.loads(row["data"]),
                "evidence": json.loads(row["evidence"]),
                "tier_used": row["tier_used"]}

    def set_llm_cache(self, key: str, data: Any, evidence: Any,
                      tier_used: Optional[str]) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT OR REPLACE INTO llm_cache (cache_key, data, evidence, tier_used, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, json.dumps(data), json.dumps(evidence), tier_used, now),
        )
        self.conn.commit()

    # ---- news ----
    def news_exists(self, url_hash: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM news WHERE url_hash = ?", (url_hash,)).fetchone() is not None

    def count_news(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]

    def insert_news(self, record: dict[str, Any]) -> bool:
        """Insert a NEWS row. Returns False if url_hash already present."""
        cols = ("url_hash", "url", "title", "author", "published_date",
                "date_confidence", "freshness_status", "date_source", "full_text",
                "summary", "content_hash", "source_name", "source_url",
                "collected_at", "first_seen_at")
        placeholders = ", ".join("?" for _ in cols)
        cur = self.conn.execute(
            f"INSERT OR IGNORE INTO news ({', '.join(cols)}) VALUES ({placeholders})",
            tuple(record.get(c) for c in cols))
        self.conn.commit()
        return cur.rowcount > 0

    def iter_news(self) -> Iterator[sqlite3.Row]:
        yield from self.conn.execute("SELECT * FROM news ORDER BY published_date DESC")

    # ---- source snapshot state (freshness high-water mark) ----
    def get_source_state(self, source_name: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT last_snapshot_at, url_hashes FROM source_state WHERE source_name = ?",
            (source_name,)).fetchone()
        if not row:
            return None
        return {"last_snapshot_at": row["last_snapshot_at"],
                "url_hashes": json.loads(row["url_hashes"] or "[]")}

    def save_source_snapshot(self, source_name: str, snapshot_at: str,
                             url_hashes: list[str]) -> None:
        """Replace the source's last COMPLETE snapshot. Call only after a fully
        successful fetch+parse; the snapshot time is when the fetch started."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO source_state (source_name, last_snapshot_at, url_hashes, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(source_name) DO UPDATE SET "
            "last_snapshot_at=excluded.last_snapshot_at, url_hashes=excluded.url_hashes, "
            "updated_at=excluded.updated_at",
            (source_name, snapshot_at, json.dumps(url_hashes), now))
        self.conn.commit()

    # ---- seen ledger (dedup + first_seen_at) ----
    def is_seen_url(self, url_hash: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM seen_items WHERE url_hash = ?", (url_hash,)).fetchone() is not None

    def is_seen_content(self, content_hash: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM seen_items WHERE content_hash = ?",
            (content_hash,)).fetchone() is not None

    def get_first_seen(self, url_hash: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT first_seen_at FROM seen_items WHERE url_hash = ?",
            (url_hash,)).fetchone()
        return row["first_seen_at"] if row else None

    def mark_seen(self, url_hash: str, content_hash: str, source_name: str,
                  first_seen_at: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO seen_items (url_hash, content_hash, source_name, first_seen_at) "
            "VALUES (?, ?, ?, ?)",
            (url_hash, content_hash, source_name, first_seen_at))
        self.conn.commit()

    # ---- jobs ----
    def job_exists(self, url_hash: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM jobs WHERE url_hash = ?", (url_hash,)).fetchone() is not None

    def count_jobs(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    def insert_job(self, record: dict[str, Any]) -> bool:
        cols = ("url_hash", "url", "source_name", "company_raw", "title",
                "is_remote", "remote_basis", "role_family", "role_basis",
                "published_date", "date_confidence", "freshness_status",
                "date_source", "content_hash", "llm_tier", "collected_at",
                "first_seen_at")
        placeholders = ", ".join("?" for _ in cols)
        cur = self.conn.execute(
            f"INSERT OR IGNORE INTO jobs ({', '.join(cols)}) VALUES ({placeholders})",
            tuple(record.get(c) for c in cols))
        self.conn.commit()
        return cur.rowcount > 0

    def iter_jobs(self) -> Iterator[sqlite3.Row]:
        yield from self.conn.execute("SELECT * FROM jobs ORDER BY published_date DESC")

    # ---- entity_map (Step 13) ----
    def count_entity_map(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM entity_map").fetchone()[0]

    def upsert_entity_map(self, record: dict[str, Any]) -> None:
        cols = ("entity_type", "raw_name", "canonical", "method", "score",
                "matched_alias", "source_table", "occurrences", "first_seen_at")
        placeholders = ", ".join("?" for _ in cols)
        self.conn.execute(
            f"INSERT INTO entity_map ({', '.join(cols)}) VALUES ({placeholders}) "
            "ON CONFLICT(entity_type, raw_name) DO UPDATE SET "
            "canonical=excluded.canonical, method=excluded.method, "
            "score=excluded.score, matched_alias=excluded.matched_alias, "
            "source_table=excluded.source_table, occurrences=excluded.occurrences",
            tuple(record.get(c) for c in cols))
        self.conn.commit()

    def iter_entity_map(self) -> Iterator[sqlite3.Row]:
        yield from self.conn.execute("SELECT * FROM entity_map")

    def clear_entity_map(self) -> None:
        self.conn.execute("DELETE FROM entity_map")
        self.conn.commit()
