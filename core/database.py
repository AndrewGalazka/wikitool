"""
database.py
-----------
SQLite database setup and access for the Protiviti Operational Audit Assistant.
All data is stored in a single SQLite file at data/audit_assistant.db.
"""

import sqlite3
import os
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "audit_assistant.db"


def get_db() -> sqlite3.Connection:
    """Return a new SQLite connection with row_factory set to Row."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create all tables if they do not exist."""
    conn = get_db()
    cur = conn.cursor()

    cur.executescript("""
    -- ── Audits ────────────────────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS audits (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        client      TEXT,
        status      TEXT NOT NULL DEFAULT 'active',   -- active | closed
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );

    -- ── Sources (uploaded files) ──────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS sources (
        id              TEXT PRIMARY KEY,
        audit_id        TEXT NOT NULL REFERENCES audits(id) ON DELETE CASCADE,
        filename        TEXT NOT NULL,
        file_type       TEXT NOT NULL,   -- evidence | guidance
        original_path   TEXT NOT NULL,   -- path to raw file
        markdown_path   TEXT,            -- path to converted markdown
        content_hash    TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'pending',  -- pending | converting | ready | failed
        error_message   TEXT,
        provenance_meta TEXT,            -- JSON: page anchors, sheet refs, etc.
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    );

    -- ── Evidence Wiki Pages ───────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS wiki_pages (
        id          TEXT PRIMARY KEY,
        audit_id    TEXT NOT NULL REFERENCES audits(id) ON DELETE CASCADE,
        page_type   TEXT NOT NULL,   -- source | person | process | control | system | evidence_area | finding
        title       TEXT NOT NULL,
        content     TEXT NOT NULL,   -- markdown body
        metadata    TEXT,            -- JSON: related_entities, links, sources
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );

    -- ── Wiki Lint Issues ──────────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS lint_issues (
        id          TEXT PRIMARY KEY,
        audit_id    TEXT NOT NULL REFERENCES audits(id) ON DELETE CASCADE,
        page_id     TEXT REFERENCES wiki_pages(id) ON DELETE CASCADE,
        issue_type  TEXT NOT NULL,   -- contradiction | gap | unresolved_reference
        description TEXT NOT NULL,
        source_pages TEXT,           -- JSON array of related page IDs
        resolved    INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL
    );

    -- ── Work Program ─────────────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS work_programs (
        id              TEXT PRIMARY KEY,
        audit_id        TEXT NOT NULL REFERENCES audits(id) ON DELETE CASCADE,
        filename        TEXT NOT NULL,
        column_mapping  TEXT,        -- JSON: {test_id_col, description_col, objective_col, ...}
        created_at      TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS work_program_rows (
        id                  TEXT PRIMARY KEY,
        work_program_id     TEXT NOT NULL REFERENCES work_programs(id) ON DELETE CASCADE,
        audit_id            TEXT NOT NULL REFERENCES audits(id) ON DELETE CASCADE,
        test_id             TEXT NOT NULL,
        description         TEXT NOT NULL,
        objective           TEXT,
        extra_columns       TEXT,    -- JSON: any additional columns from upload
        status              TEXT NOT NULL DEFAULT 'pending',  -- pending | running | completed | pending_evidence | open_questions | error
        conclusion          TEXT,
        evidence_references TEXT,    -- JSON array of {source_file, location, quote}
        open_questions      TEXT,    -- JSON array of strings
        requested_evidence  TEXT,    -- JSON array of strings
        human_notes         TEXT,
        verified            INTEGER NOT NULL DEFAULT 0,
        last_run_at         TEXT,
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL
    );

    -- ── Chat History ─────────────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS chat_messages (
        id          TEXT PRIMARY KEY,
        audit_id    TEXT NOT NULL REFERENCES audits(id) ON DELETE CASCADE,
        scope       TEXT NOT NULL,   -- evidence | guidance
        role        TEXT NOT NULL,   -- user | assistant
        content     TEXT NOT NULL,
        citations   TEXT,            -- JSON array of citation objects
        created_at  TEXT NOT NULL
    );

    -- ── Token Usage ──────────────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS token_usage (
        id              TEXT PRIMARY KEY,
        audit_id        TEXT NOT NULL REFERENCES audits(id) ON DELETE CASCADE,
        operation       TEXT NOT NULL,  -- ingestion | lint | work_program | chat
        prompt_tokens   INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        total_tokens    INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL
    );

    -- ── Wiki Index & Log (Karpathy LLM-Wiki pattern) ────────────────────────
    CREATE TABLE IF NOT EXISTS wiki_index (
        id          TEXT PRIMARY KEY,
        audit_id    TEXT NOT NULL REFERENCES audits(id) ON DELETE CASCADE,
        content     TEXT NOT NULL,   -- full index.md markdown
        updated_at  TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS wiki_log (
        id          TEXT PRIMARY KEY,
        audit_id    TEXT NOT NULL REFERENCES audits(id) ON DELETE CASCADE,
        entry       TEXT NOT NULL,   -- single log line
        created_at  TEXT NOT NULL
    );
    -- ── Findings ─────────────────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS findings (
        id              TEXT PRIMARY KEY,
        audit_id        TEXT NOT NULL REFERENCES audits(id) ON DELETE CASCADE,
        title           TEXT NOT NULL,
        condition       TEXT,
        criteria        TEXT,
        cause           TEXT,
        consequence     TEXT,
        corrective_action TEXT,
        sub_issues      TEXT,        -- JSON array of strings
        source_row_ids  TEXT,        -- JSON array of work_program_row IDs
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    );
    """)

    conn.commit()
    conn.close()
