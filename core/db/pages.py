from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

import frontmatter
import sqlite_vec  # pyright: ignore[reportMissingModuleSource]
import yaml

from ..constants import WIKI_CATEGORIES

__all__ = ["upsert_page", "delete_page", "get_page", "list_pages"]

log = logging.getLogger(__name__)


def upsert_page(
    conn: sqlite3.Connection,
    wiki_root: Path,
    md_path: Path,
    embedding: list[float] | None = None,
) -> None:
    """Insert or update a page record from a markdown file on disk.

    Reads YAML frontmatter (title, tags) and derives the category from the file path.
    The first non-heading, non-table line is stored as a short summary.
    When ``embedding`` is provided, upserts the vector into ``page_vectors``.

    Args:
        conn: Open database connection.
        wiki_root: Root of the wiki directory (used to derive the relative path).
        md_path: Absolute path to the ``.md`` file to index.
        embedding: Optional dense embedding vector to store for semantic search.
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
    if embedding is not None:
        row = conn.execute("SELECT id FROM pages WHERE file_path=?", (rel_path,)).fetchone()
        if row is not None:
            conn.execute(
                "INSERT OR REPLACE INTO page_vectors(rowid, embedding) VALUES (?, ?)",
                (row["id"], sqlite_vec.serialize_float32(embedding)),  # pyright: ignore[reportAttributeAccessIssue]
            )

    # Sync outgoing wikilinks for this page into the links table
    outgoing = {m.strip() for m in re.findall(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]", content or "")}
    conn.execute("DELETE FROM links WHERE source_path = ?", (rel_path,))
    if outgoing:
        conn.executemany(
            "INSERT OR IGNORE INTO links(source_path, target_stem) VALUES (?, ?)",
            [(rel_path, stem) for stem in outgoing],
        )
    conn.commit()


def delete_page(conn: sqlite3.Connection, rel_path: str) -> None:
    """Remove a page record and its outgoing links from the database.

    Args:
        conn: Open database connection.
        rel_path: Page path relative to ``wiki_root`` (e.g. ``"Concepts/Attention.md"``).
    """
    conn.execute("DELETE FROM links WHERE source_path = ?", (rel_path,))
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


def _infer_category(rel_path: str) -> str:
    """Derive a page's category from its path relative to wiki_root.

    Args:
        rel_path: Path relative to the wiki root (e.g. ``"Concepts/Attention.md"``).

    Returns:
        The top-level directory name if it is one of Sources/Concepts/Entities,
        otherwise ``"root"``.
    """
    parts = rel_path.split("/")
    if len(parts) > 1 and parts[0] in WIKI_CATEGORIES:
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
