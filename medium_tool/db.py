from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS stories (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        url TEXT NOT NULL UNIQUE,
        publication_date TEXT,
        tags_json TEXT NOT NULL DEFAULT '[]',
        subtitle TEXT,
        current_publication TEXT,
        is_self_published INTEGER NOT NULL DEFAULT 1,
        is_eligible INTEGER NOT NULL DEFAULT 0,
        eligibility_reason TEXT,
        processing_status TEXT NOT NULL DEFAULT 'discovered',
        source_html TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS publications (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        url TEXT NOT NULL UNIQUE,
        description TEXT,
        follower_count INTEGER,
        accepted_topics_json TEXT NOT NULL DEFAULT '[]',
        excluded_topics_json TEXT NOT NULL DEFAULT '[]',
        guideline_url TEXT,
        onboarding_method TEXT,
        onboarding_destination TEXT,
        is_open_to_all INTEGER,
        requires_application INTEGER,
        accepts_published INTEGER,
        requires_unpublished INTEGER,
        accepts_submissions INTEGER,
        last_publication_date TEXT,
        posts_last_30_days INTEGER,
        latest_verification_date TEXT,
        status TEXT NOT NULL DEFAULT 'uncertain',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS guidelines (
        id INTEGER PRIMARY KEY,
        publication_id INTEGER NOT NULL REFERENCES publications(id),
        url TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        extracted_text TEXT NOT NULL,
        rules_json TEXT NOT NULL DEFAULT '{}',
        verified_at TEXT NOT NULL,
        html_snapshot_path TEXT,
        UNIQUE(publication_id, content_hash)
    );
    CREATE TABLE IF NOT EXISTS matches (
        id INTEGER PRIMARY KEY,
        story_id INTEGER NOT NULL REFERENCES stories(id),
        publication_id INTEGER NOT NULL REFERENCES publications(id),
        score REAL NOT NULL,
        eligible INTEGER NOT NULL,
        explanation_json TEXT NOT NULL,
        rejection_reasons_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(story_id, publication_id)
    );
    CREATE TABLE IF NOT EXISTS writer_applications (
        id INTEGER PRIMARY KEY,
        publication_id INTEGER NOT NULL REFERENCES publications(id),
        destination TEXT,
        method TEXT NOT NULL,
        message TEXT,
        referenced_story_ids_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'application-required',
        approved_at TEXT,
        sent_at TEXT,
        result TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS one_open_application
        ON writer_applications(publication_id)
        WHERE status IN ('application-required','approved','application-sent');
    CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY,
        match_id INTEGER NOT NULL REFERENCES matches(id),
        story_id INTEGER NOT NULL REFERENCES stories(id),
        publication_id INTEGER NOT NULL REFERENCES publications(id),
        status TEXT NOT NULL,
        submitted_at TEXT,
        result TEXT,
        guideline_hash TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS one_pending_story_submission
        ON submissions(story_id)
        WHERE status IN ('approved','submitted','application-required','application-sent');
    CREATE TABLE IF NOT EXISTS approvals (
        id INTEGER PRIMARY KEY,
        match_id INTEGER NOT NULL REFERENCES matches(id),
        action TEXT NOT NULL CHECK(action IN ('approve','reject')),
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS browser_actions (
        id INTEGER PRIMARY KEY,
        action TEXT NOT NULL,
        target_type TEXT,
        target_id INTEGER,
        destination TEXT,
        dry_run INTEGER NOT NULL,
        result TEXT NOT NULL,
        details_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS errors (
        id INTEGER PRIMARY KEY,
        operation TEXT NOT NULL,
        message TEXT NOT NULL,
        traceback TEXT,
        screenshot_path TEXT,
        html_snapshot_path TEXT,
        created_at TEXT NOT NULL
    );
    """,
]


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def migrate(self) -> None:
        with self.connect() as conn:
            conn.executescript(MIGRATIONS[0])
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)",
                (utcnow(),),
            )

    def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute(sql, params))

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.connect() as conn:
            cursor = conn.execute(sql, params)
            return int(cursor.lastrowid or 0)

    def upsert_story(self, story: dict[str, Any]) -> int:
        now = utcnow()
        values = (
            story["title"], story["url"], story.get("publication_date"),
            json.dumps(story.get("tags", [])), story.get("subtitle"),
            story.get("current_publication"), int(story.get("is_self_published", True)),
            int(story.get("is_eligible", False)), story.get("eligibility_reason"),
            story.get("processing_status", "discovered"), story.get("source_html"),
            now, now,
        )
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO stories(
                    title,url,publication_date,tags_json,subtitle,current_publication,
                    is_self_published,is_eligible,eligibility_reason,processing_status,
                    source_html,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(url) DO UPDATE SET
                    title=excluded.title, publication_date=excluded.publication_date,
                    tags_json=excluded.tags_json, subtitle=excluded.subtitle,
                    current_publication=excluded.current_publication,
                    is_self_published=excluded.is_self_published,
                    is_eligible=excluded.is_eligible,
                    eligibility_reason=excluded.eligibility_reason,
                    source_html=excluded.source_html, updated_at=excluded.updated_at""",
                values,
            )
            return int(conn.execute("SELECT id FROM stories WHERE url=?", (story["url"],)).fetchone()[0])

    def upsert_publication(self, publication: dict[str, Any]) -> int:
        now = utcnow()
        columns = [
            "name", "url", "description", "follower_count", "accepted_topics_json",
            "excluded_topics_json", "guideline_url", "onboarding_method",
            "onboarding_destination", "is_open_to_all", "requires_application",
            "accepts_published", "requires_unpublished", "accepts_submissions",
            "last_publication_date", "posts_last_30_days",
            "latest_verification_date", "status",
        ]
        values = [
            publication.get("name"), publication.get("url"), publication.get("description"),
            publication.get("follower_count"), json.dumps(publication.get("accepted_topics", [])),
            json.dumps(publication.get("excluded_topics", [])), publication.get("guideline_url"),
            publication.get("onboarding_method"), publication.get("onboarding_destination"),
            _tri(publication.get("is_open_to_all")), _tri(publication.get("requires_application")),
            _tri(publication.get("accepts_published")), _tri(publication.get("requires_unpublished")),
            _tri(publication.get("accepts_submissions")), publication.get("last_publication_date"),
            publication.get("posts_last_30_days"), publication.get("latest_verification_date"),
            publication.get("status", "uncertain"),
        ]
        with self.connect() as conn:
            placeholders = ",".join("?" for _ in columns)
            updates = ",".join(f"{c}=excluded.{c}" for c in columns if c not in {"url"})
            conn.execute(
                f"""INSERT INTO publications({','.join(columns)},created_at,updated_at)
                VALUES({placeholders},?,?)
                ON CONFLICT(url) DO UPDATE SET {updates},updated_at=excluded.updated_at""",
                (*values, now, now),
            )
            return int(conn.execute("SELECT id FROM publications WHERE url=?", (publication["url"],)).fetchone()[0])


def _tri(value: Any) -> int | None:
    return None if value is None else int(bool(value))

