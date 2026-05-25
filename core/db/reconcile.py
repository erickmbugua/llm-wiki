from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from .pages import delete_page, upsert_page

__all__ = ["partial_reconcile", "reconcile"]

log = logging.getLogger(__name__)


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
