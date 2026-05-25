from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter
import litellm
import sqlite_vec  # pyright: ignore[reportMissingModuleSource]
import yaml

from .config import VAULT_DB_FILE, VAULT_INTERNAL_DIR

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


# ---------------------------------------------------------------------------
# Page CRUD
# ---------------------------------------------------------------------------


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
# Semantic search
# ---------------------------------------------------------------------------


def compute_embedding(text: str, model: str) -> list[float]:
    """Compute a dense embedding vector for text using the given litellm model.

    Args:
        text: Text to embed. Truncated to 8192 chars to stay within model context limits.
        model: litellm embedding model string (e.g. ``"ollama/nomic-embed-text"``).

    Returns:
        A list of floats representing the embedding vector.

    Raises:
        RuntimeError: The embedding model is unavailable or returns an unexpected shape.
    """
    try:
        response = litellm.embedding(  # pyright: ignore[reportAttributeAccessIssue]
            model=model, input=[text[:8192]]
        )
        result = response.data[0].embedding  # pyright: ignore[reportAttributeAccessIssue]
        return [float(v) for v in result]
    except Exception as exc:
        raise RuntimeError(
            f"Embedding failed for model '{model}': {exc}. "
            "Ensure the embedding model is running and pulled."
        ) from exc


def vector_search(
    conn: sqlite3.Connection,
    query_embedding: list[float],
    limit: int = 10,
) -> list[dict[str, Any]]:
    """KNN search over page_vectors using cosine distance.

    Args:
        conn: Open database connection with sqlite-vec loaded.
        query_embedding: Dense vector for the query.
        limit: Maximum number of results.

    Returns:
        List of page dicts (file_path, title, category, summary, tags, backlinks, rank)
        ordered by vector similarity, or an empty list if no embeddings exist yet.
    """
    try:
        rows = conn.execute(
            """
            SELECT p.file_path, p.title, p.category, p.summary, p.tags, p.backlinks,
                   v.distance AS rank
            FROM page_vectors v
            JOIN pages p ON v.rowid = p.id
            WHERE v.embedding MATCH ?
              AND k = ?
            ORDER BY v.distance
            """,
            (sqlite_vec.serialize_float32(query_embedding), limit),  # pyright: ignore[reportAttributeAccessIssue]
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def hybrid_search(
    conn: sqlite3.Connection,
    query: str,
    query_embedding: list[float] | None,
    limit: int = 10,
    rrf_k: int = 60,
) -> list[dict[str, Any]]:
    """Merge FTS5 and vector search results with Reciprocal Rank Fusion (RRF).

    Falls back to FTS5-only when query_embedding is None (e.g. embedding model not
    configured or embedding call failed).

    Args:
        conn: Open database connection.
        query: Raw text query for FTS5.
        query_embedding: Dense vector for semantic search, or None for lexical-only.
        limit: Final result count to return.
        rrf_k: RRF smoothing constant (60 is the standard default).

    Returns:
        List of page dicts ordered by fused relevance score.
    """
    fts_results = search(conn, query, limit=limit)
    if query_embedding is None:
        return fts_results

    vec_results = vector_search(conn, query_embedding, limit=limit)

    # Assign RRF scores: score(doc) = sum(1 / (rrf_k + rank_i)) for each list it appears in
    scores: dict[str, float] = {}
    pages: dict[str, dict[str, Any]] = {}

    for rank, r in enumerate(fts_results, start=1):
        fp = r["file_path"]
        scores[fp] = scores.get(fp, 0.0) + 1.0 / (rrf_k + rank)
        pages[fp] = r

    for rank, r in enumerate(vec_results, start=1):
        fp = r["file_path"]
        scores[fp] = scores.get(fp, 0.0) + 1.0 / (rrf_k + rank)
        pages.setdefault(fp, r)

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [pages[fp] for fp, _ in ranked[:limit]]


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

    _rebuild_backlinks_full(conn)
    return {"added": added, "updated": updated, "removed": removed}


def partial_reconcile(
    conn: sqlite3.Connection, wiki_root: Path, changed_paths: list[Path]
) -> dict[str, int]:
    """Re-index only the given paths and update backlinks for the affected neighbourhood.

    Use this after ingest when the exact set of changed files is known.
    For a full sync from disk, use ``reconcile()`` instead.

    Args:
        conn: Open database connection.
        wiki_root: Root of the wiki directory.
        changed_paths: Absolute paths to ``.md`` files that were just written.

    Returns:
        A dict with integer counts: ``{"added": int, "updated": int, "removed": int}``.
    """
    existing = {
        row["file_path"]: row["mtime"] for row in conn.execute("SELECT file_path, mtime FROM pages")
    }
    added = updated = 0
    for md_path in changed_paths:
        if not md_path.exists():
            continue
        rel = str(md_path.relative_to(wiki_root))
        mtime = md_path.stat().st_mtime
        if rel not in existing:
            upsert_page(conn, wiki_root, md_path)
            added += 1
        elif abs(mtime - existing[rel]) > 0.01:
            upsert_page(conn, wiki_root, md_path)
            updated += 1
    _rebuild_backlinks_incremental(
        conn, [str(p.relative_to(wiki_root)) for p in changed_paths if p.exists()]
    )
    return {"added": added, "updated": updated, "removed": 0}


def _rebuild_backlinks_full(conn: sqlite3.Connection) -> None:
    """Rewrite the ``backlinks`` JSON column for every page from the ``links`` table.

    Reads outgoing-link edges from the ``links`` table (populated by ``upsert_page``)
    instead of regex-scanning page content. O(pages) SQL reads with no content parsing.

    When two pages share the same stem (e.g. ``Concepts/Python.md`` and
    ``Entities/Python.md``), the alphabetically first path wins and a WARNING is
    logged so the user knows to rename one of the files.

    Args:
        conn: Open database connection. All pages are rewritten in a single transaction.
    """
    rows = conn.execute("SELECT id, file_path FROM pages").fetchall()
    title_to_path: dict[str, str] = {}
    for r in sorted(rows, key=lambda r: r["file_path"]):
        stem = r["file_path"].rsplit("/", 1)[-1].replace(".md", "")
        if stem in title_to_path:
            log.warning(
                "Wikilink collision: [[%s]] matches both '%s' and '%s'; "
                "using '%s'. Rename one page to disambiguate.",
                stem,
                title_to_path[stem],
                r["file_path"],
                title_to_path[stem],
            )
        else:
            title_to_path[stem] = r["file_path"]

    backlink_map: dict[str, list[str]] = {r["file_path"]: [] for r in rows}
    link_rows = conn.execute("SELECT source_path, target_stem FROM links").fetchall()
    for lr in link_rows:
        target = title_to_path.get(lr["target_stem"])
        if (
            target
            and target != lr["source_path"]
            and lr["source_path"] not in backlink_map.get(target, [])
        ):
            backlink_map.setdefault(target, []).append(lr["source_path"])

    for path, backlinks in backlink_map.items():
        conn.execute(
            "UPDATE pages SET backlinks=? WHERE file_path=?",
            (json.dumps(backlinks), path),
        )
    conn.commit()


def _rebuild_backlinks_incremental(conn: sqlite3.Connection, changed_paths: list[str]) -> None:
    """Recompute backlinks only for pages whose link neighbourhood changed.

    A page's backlinks can change when one of the changed pages now links to it
    (new backlink) or used to link to it (removed backlink). Both cases are covered
    by recomputing backlinks for all targets of the changed pages' outgoing links,
    plus the changed pages themselves.

    Args:
        conn: Open database connection.
        changed_paths: Vault-relative file paths of pages that were just written
            (e.g. ``["Concepts/Attention.md"]``).
    """
    if not changed_paths:
        return

    # Stems of changed pages — other pages may link to them by stem
    changed_stems = {p.rsplit("/", 1)[-1].replace(".md", "") for p in changed_paths}

    # Outgoing stems from changed pages — their targets' backlinks may have changed
    placeholders = ",".join("?" * len(changed_paths))
    outgoing_rows = conn.execute(
        f"SELECT target_stem FROM links WHERE source_path IN ({placeholders})",
        changed_paths,
    ).fetchall()
    affected_stems = changed_stems | {r["target_stem"] for r in outgoing_rows}

    # Resolve stems → paths (first alphabetically wins on collision, same as full rebuild)
    all_pages = conn.execute("SELECT id, file_path FROM pages").fetchall()
    title_to_path: dict[str, str] = {}
    for r in sorted(all_pages, key=lambda r: r["file_path"]):
        stem = r["file_path"].rsplit("/", 1)[-1].replace(".md", "")
        if stem not in title_to_path:
            title_to_path[stem] = r["file_path"]

    affected_paths = {title_to_path[s] for s in affected_stems if s in title_to_path}
    if not affected_paths:
        return

    # Recompute backlinks only for affected pages
    link_rows = conn.execute("SELECT source_path, target_stem FROM links").fetchall()
    for target_path in affected_paths:
        target_stem = target_path.rsplit("/", 1)[-1].replace(".md", "")
        backlinks = [
            lr["source_path"]
            for lr in link_rows
            if lr["target_stem"] == target_stem and lr["source_path"] != target_path
        ]
        conn.execute(
            "UPDATE pages SET backlinks=? WHERE file_path=?",
            (json.dumps(backlinks), target_path),
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
        file_path: Vault-relative path to the raw file (e.g. ``"raw/paper.pdf"``).
            Always use a path relative to the vault root, never an absolute path.
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
# Ingest jobs
# ---------------------------------------------------------------------------


def create_job(conn: sqlite3.Connection, job_id: str, vault: str, source: str) -> str:
    """Insert a new ingest job record with status ``"pending"`` and return its ID.

    Args:
        conn: Open database connection.
        job_id: UUID string to use as the primary key.
        vault: Vault name this job belongs to.
        source: File path or URL being ingested.

    Returns:
        The job_id that was inserted.
    """
    conn.execute(
        """
        INSERT INTO ingest_jobs (id, vault, source, status, created_at)
        VALUES (?, ?, ?, 'pending', ?)
        """,
        (job_id, vault, source, datetime.now(timezone.utc).timestamp()),
    )
    conn.commit()
    return job_id


def update_job_status(
    conn: sqlite3.Connection,
    job_id: str,
    status: str,
    pages_written: list[str] | None = None,
    error: str | None = None,
) -> None:
    """Update an ingest job's status and optional result fields.

    Sets ``started_at`` when transitioning to ``"running"``, and ``finished_at``
    when transitioning to ``"done"`` or ``"failed"``.

    Args:
        conn: Open database connection.
        job_id: UUID of the job to update.
        status: New status string: ``"pending"``, ``"running"``, ``"done"``, or ``"failed"``.
        pages_written: List of relative page paths written (stored as JSON). Only used on done.
        error: Error message to store when status is ``"failed"``.
    """
    now = datetime.now(timezone.utc).timestamp()
    started_at = now if status == "running" else None
    finished_at = now if status in ("done", "failed") else None
    pw_json = json.dumps(pages_written or [])
    conn.execute(
        """
        UPDATE ingest_jobs
        SET status=?,
            started_at=COALESCE(started_at, ?),
            finished_at=COALESCE(?, finished_at),
            pages_written=?,
            error=COALESCE(?, error)
        WHERE id=?
        """,
        (status, started_at, finished_at, pw_json, error, job_id),
    )
    conn.commit()


def get_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    """Fetch a single ingest job record by its UUID.

    Args:
        conn: Open database connection.
        job_id: UUID of the job to look up.

    Returns:
        A dict of all job columns (id, vault, source, status, created_at, started_at,
        finished_at, pages_written, error), or ``None`` if not found.
    """
    row = conn.execute("SELECT * FROM ingest_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["pages_written"] = json.loads(d.get("pages_written") or "[]")
    return d


def list_jobs(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent ingest jobs, newest first.

    Args:
        conn: Open database connection.
        limit: Maximum number of jobs to return (default 20).

    Returns:
        List of job dicts ordered by ``created_at`` descending.
    """
    rows = conn.execute(
        "SELECT * FROM ingest_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["pages_written"] = json.loads(d.get("pages_written") or "[]")
        result.append(d)
    return result


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
