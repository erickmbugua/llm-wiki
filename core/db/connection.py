from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec  # pyright: ignore[reportMissingModuleSource]

from ..config import VAULT_DB_FILE, VAULT_INTERNAL_DIR

__all__ = ["get_db", "db_connection"]

log = logging.getLogger(__name__)


def get_db(vault_path: Path) -> sqlite3.Connection:
    """Open (or create) the vault's SQLite database and ensure the schema exists.

    Enables WAL journal mode and foreign-key enforcement on every connection.
    The database file lives at ``<vault>/.llm-wiki/wiki.db``.

    Args:
        vault_path: Root directory of the vault.

    Returns:
        An open ``sqlite3.Connection`` with ``row_factory`` set to ``sqlite3.Row``.
    """
    db_path = vault_path / VAULT_INTERNAL_DIR / VAULT_DB_FILE
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)  # pyright: ignore[reportAttributeAccessIssue]
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


@contextmanager
def db_connection(vault_path: Path) -> Iterator[sqlite3.Connection]:
    """Context manager that opens a vault DB connection and ensures it is closed on exit.

    Prefer this over ``get_db`` + ``try/finally`` to eliminate connection leaks.

    Args:
        vault_path: Root directory of the vault.

    Yields:
        An open ``sqlite3.Connection`` with WAL mode, foreign keys, and sqlite-vec loaded.
    """
    conn = get_db(vault_path)
    try:
        yield conn
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create all tables, FTS5 virtual table, triggers, and indexes if they don't exist.

    Args:
        conn: Open database connection.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pages (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT UNIQUE NOT NULL,
            title    TEXT NOT NULL,
            category TEXT,
            content  TEXT,
            tags     TEXT DEFAULT '[]',
            mtime    REAL,
            summary  TEXT,
            backlinks TEXT DEFAULT '[]'
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
            title,
            content,
            content=pages,
            content_rowid=id,
            tokenize='porter ascii'
        );

        CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
            INSERT INTO pages_fts(rowid, title, content)
            VALUES (new.id, new.title, new.content);
        END;

        CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, title, content)
            VALUES ('delete', old.id, old.title, old.content);
        END;

        CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, title, content)
            VALUES ('delete', old.id, old.title, old.content);
            INSERT INTO pages_fts(rowid, title, content)
            VALUES (new.id, new.title, new.content);
        END;

        CREATE TABLE IF NOT EXISTS ingest_queue (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path   TEXT UNIQUE NOT NULL,
            status      TEXT DEFAULT 'pending',
            added_at    REAL,
            processed_at REAL,
            error       TEXT
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS page_vectors USING vec0(
            embedding float[768]
        );
        CREATE TABLE IF NOT EXISTS ingest_jobs (
            id           TEXT PRIMARY KEY,
            vault        TEXT NOT NULL,
            source       TEXT NOT NULL,
            status       TEXT DEFAULT 'pending',
            created_at   REAL NOT NULL,
            started_at   REAL,
            finished_at  REAL,
            pages_written TEXT DEFAULT '[]',
            error        TEXT
        );

        CREATE TABLE IF NOT EXISTS links (
            source_path TEXT NOT NULL,
            target_stem TEXT NOT NULL,
            PRIMARY KEY (source_path, target_stem)
        );

        CREATE INDEX IF NOT EXISTS links_target_idx ON links(target_stem);
    """)
    conn.commit()
