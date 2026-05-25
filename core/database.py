from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter
import yaml

from .config import VAULT_DB_FILE, VAULT_INTERNAL_DIR


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
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the ``pages``, FTS5 virtual table, triggers, and ``ingest_queue`` if they don't exist.

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
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Page CRUD
# ---------------------------------------------------------------------------


def upsert_page(conn: sqlite3.Connection, wiki_root: Path, md_path: Path) -> None:
    """Insert or update a page record from a markdown file on disk.

    Reads YAML frontmatter (title, tags) and derives the category from the file path.
    The first non-heading, non-table line is stored as a short summary.

    Args:
        conn: Open database connection.
        wiki_root: Root of the wiki directory (used to derive the relative path).
        md_path: Absolute path to the ``.md`` file to index.
    """
    try:
        post = frontmatter.load(str(md_path))
        content = post.content
        title = str(post.get("title") or md_path.stem)
        tags = json.dumps(list(post.get("tags") or []))  # type: ignore[call-overload]
    except yaml.YAMLError:
        content = md_path.read_text()
        title = md_path.stem
        tags = json.dumps([])
    mtime = md_path.stat().st_mtime

    rel_path = str(md_path.relative_to(wiki_root))
    category = _infer_category(rel_path)
    summary = _extract_summary(content)

    conn.execute(
        """
        INSERT INTO pages (file_path, title, category, content, tags, mtime, summary, backlinks)
        VALUES (?, ?, ?, ?, ?, ?, ?, '[]')
        ON CONFLICT(file_path) DO UPDATE SET
            title    = excluded.title,
            category = excluded.category,
            content  = excluded.content,
            tags     = excluded.tags,
            mtime    = excluded.mtime,
            summary  = excluded.summary
    """,
        (rel_path, title, category, content, tags, mtime, summary),
    )
    conn.commit()


def delete_page(conn: sqlite3.Connection, rel_path: str) -> None:
    """Remove a page record from the database.

    Args:
        conn: Open database connection.
        rel_path: Page path relative to ``wiki_root`` (e.g. ``"Concepts/Attention.md"``).
    """
    conn.execute("DELETE FROM pages WHERE file_path=?", (rel_path,))
    conn.commit()


def get_page(conn: sqlite3.Connection, file_path: str) -> dict[str, Any] | None:
    """Fetch a single page record by its relative file path.

    Args:
        conn: Open database connection.
        file_path: Path relative to ``wiki_root`` (e.g. ``"Concepts/Attention.md"``).

    Returns:
        A dict of all page columns, or ``None`` if the page is not found.
    """
    row = conn.execute("SELECT * FROM pages WHERE file_path=?", (file_path,)).fetchone()
    return dict(row) if row else None


def list_pages(conn: sqlite3.Connection, category: str | None = None) -> list[dict[str, Any]]:
    """Return all page records, optionally filtered to a single category.

    Args:
        conn: Open database connection.
        category: If provided, only pages with this ``category`` value are returned
            (e.g. ``"Concepts"``).

    Returns:
        List of page dicts ordered by category then title.
    """
    if category:
        rows = conn.execute(
            "SELECT * FROM pages WHERE category=? ORDER BY title", (category,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM pages ORDER BY category, title").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def search(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """BM25-ranked full-text search across page titles and content via FTS5.

    Special FTS5 characters are stripped from the query before execution to prevent
    syntax errors from arbitrary user input.

    Args:
        conn: Open database connection.
        query: Free-text search query.
        limit: Maximum number of results to return (default 10).

    Returns:
        List of result dicts (file_path, title, category, summary, tags, backlinks, rank),
        ordered by BM25 relevance. Returns an empty list for blank queries.
    """
    # Strip FTS5 special characters so arbitrary user input doesn't cause syntax errors
    clean = re.sub(r"[^\w\s]", " ", query).strip()
    if not clean:
        return []
    rows = conn.execute(
        """
        SELECT p.file_path, p.title, p.category, p.summary, p.tags, p.backlinks, rank
        FROM pages_fts
        JOIN pages p ON pages_fts.rowid = p.id
        WHERE pages_fts MATCH ?
        ORDER BY rank
        LIMIT ?
    """,
        (clean, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def reconcile(conn: sqlite3.Connection, wiki_root: Path) -> dict[str, int]:
    """Sync the database with the current state of all ``.md`` files under wiki_root.

    Compares on-disk mtimes against stored values. Inserts new files, updates changed ones,
    removes deleted ones, then rebuilds all backlink data.

    Args:
        conn: Open database connection.
        wiki_root: Root of the wiki directory to scan recursively.

    Returns:
        A dict with integer counts: ``{"added": int, "updated": int, "removed": int}``.
    """
    existing = {
        row["file_path"]: row["mtime"] for row in conn.execute("SELECT file_path, mtime FROM pages")
    }

    added = removed = updated = 0
    seen: set[str] = set()

    for md_path in wiki_root.rglob("*.md"):
        rel = str(md_path.relative_to(wiki_root))
        seen.add(rel)
        mtime = md_path.stat().st_mtime
        if rel not in existing:
            upsert_page(conn, wiki_root, md_path)
            added += 1
        elif abs(mtime - existing[rel]) > 0.01:
            upsert_page(conn, wiki_root, md_path)
            updated += 1

    for rel in set(existing) - seen:
        delete_page(conn, rel)
        removed += 1

    _rebuild_backlinks(conn)
    return {"added": added, "updated": updated, "removed": removed}


def _rebuild_backlinks(conn: sqlite3.Connection) -> None:
    """Rebuild the ``backlinks`` JSON column for every page by scanning all ``[[wikilink]]`` references.

    Args:
        conn: Open database connection. All pages are rewritten in a single transaction.
    """
    rows = conn.execute("SELECT id, file_path, content FROM pages").fetchall()
    title_to_path = {
        r["file_path"].rsplit("/", 1)[-1].replace(".md", ""): r["file_path"] for r in rows
    }
    backlink_map: dict[str, list[str]] = {r["file_path"]: [] for r in rows}

    for row in rows:
        for link in re.findall(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]", row["content"] or ""):
            target = title_to_path.get(link.strip())
            if (
                target
                and target != row["file_path"]
                and row["file_path"] not in backlink_map[target]
            ):
                backlink_map[target].append(row["file_path"])

    for path, backlinks in backlink_map.items():
        conn.execute(
            "UPDATE pages SET backlinks=? WHERE file_path=?",
            (json.dumps(backlinks), path),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Ingest queue
# ---------------------------------------------------------------------------


def queue_raw_file(conn: sqlite3.Connection, file_path: str) -> None:
    """Add a raw file path to the ingest queue with status ``"pending"``.

    If the file is already in the queue, re-queuing resets its status to
    ``"pending"``, clears the error field, and updates the timestamp.  This
    makes repeated calls a natural retry mechanism — re-dropping a file into
    ``raw/`` is enough to trigger a fresh ingest attempt after a previous
    failure, with no special-case logic required.

    Args:
        conn: Open database connection.
        file_path: Absolute or vault-relative path to the raw file.
    """
    conn.execute(
        """
        INSERT INTO ingest_queue (file_path, added_at)
        VALUES (?, ?)
        ON CONFLICT(file_path) DO UPDATE SET
            status       = 'pending',
            error        = NULL,
            added_at     = excluded.added_at,
            processed_at = NULL
    """,
        (file_path, datetime.now(timezone.utc).timestamp()),
    )
    conn.commit()


def get_pending_queue(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all ingest queue items with status ``"pending"``, ordered by insertion time.

    Args:
        conn: Open database connection.

    Returns:
        List of queue record dicts (id, file_path, status, added_at, processed_at, error).
    """
    rows = conn.execute(
        "SELECT * FROM ingest_queue WHERE status='pending' ORDER BY added_at"
    ).fetchall()
    return [dict(r) for r in rows]


def mark_queue_item(
    conn: sqlite3.Connection, file_path: str, status: str, error: str | None = None
) -> None:
    """Update the status of an ingest queue item, recording the current timestamp.

    Args:
        conn: Open database connection.
        file_path: Path identifying the queue item to update.
        status: New status string (e.g. ``"processing"``, ``"done"``, ``"failed"``).
        error: Optional error message stored when status is ``"failed"``.
    """
    conn.execute(
        """
        UPDATE ingest_queue SET status=?, processed_at=?, error=?
        WHERE file_path=?
    """,
        (status, datetime.now(timezone.utc).timestamp(), error, file_path),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _infer_category(rel_path: str) -> str:
    """Derive a page's category from its path relative to wiki_root.

    Args:
        rel_path: Path relative to the wiki root (e.g. ``"Concepts/Attention.md"``).

    Returns:
        The top-level directory name if it is one of Sources/Concepts/Entities,
        otherwise ``"root"``.
    """
    parts = rel_path.split("/")
    if len(parts) > 1 and parts[0] in ("Sources", "Concepts", "Entities"):
        return parts[0]
    return "root"


def _extract_summary(content: str) -> str:
    """Extract a short summary from page content as the first meaningful prose line.

    Skips headings (``#``), table rows (``|``), and YAML fence lines (``---``).

    Args:
        content: Raw markdown content of a wiki page.

    Returns:
        Up to 300 characters of the first non-structural line, or an empty string.
    """
    for line in content.splitlines():
        line = line.strip()
        if (
            line
            and not line.startswith("#")
            and not line.startswith("|")
            and not line.startswith("---")
        ):
            return line[:300]
    return ""
