from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

__all__ = ["get_pending_queue", "mark_queue_item", "queue_raw_file"]

log = logging.getLogger(__name__)


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
