from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

__all__ = ["create_job", "update_job_status", "get_job", "list_jobs"]

log = logging.getLogger(__name__)


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
