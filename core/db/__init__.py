"""SQLite persistence layer for llm-wiki.

Sub-modules:
  connection  — get_db, db_connection, schema creation
  pages       — page CRUD + category/summary helpers
  search      — FTS5, vector KNN, and hybrid RRF search
  reconcile   — full and incremental filesystem ↔ DB sync, backlink graph
  queue       — ingest_queue CRUD
  jobs        — ingest_jobs CRUD
"""

from .connection import db_connection, get_db
from .jobs import create_job, get_job, list_jobs, update_job_status
from .pages import delete_page, get_page, list_pages, upsert_page
from .queue import get_pending_queue, mark_queue_item, queue_raw_file
from .reconcile import partial_reconcile, reconcile
from .search import hybrid_search, search, vector_search

__all__ = [  # noqa: RUF022 — grouped by sub-module; alphabetical order would obscure the structure
    # connection
    "get_db",
    "db_connection",
    # pages
    "upsert_page",
    "delete_page",
    "get_page",
    "list_pages",
    # search
    "search",
    "vector_search",
    "hybrid_search",
    # reconcile
    "reconcile",
    "partial_reconcile",
    # queue
    "queue_raw_file",
    "get_pending_queue",
    "mark_queue_item",
    # jobs
    "create_job",
    "update_job_status",
    "get_job",
    "list_jobs",
]
