from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any

import sqlite_vec  # pyright: ignore[reportMissingModuleSource]

__all__ = ["search", "vector_search", "hybrid_search"]

log = logging.getLogger(__name__)


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
