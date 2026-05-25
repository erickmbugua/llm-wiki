# P1 — Incremental Backlink Updates

## Problem Statement

`_rebuild_backlinks` in `core/database.py:307` fetches every page from the database,
regex-searches each page's content for `[[wikilinks]]`, rebuilds a complete backlink map,
then issues one `UPDATE` per page.

This function is called from both `reconcile` (full vault scan — acceptable) and
`partial_reconcile` (called after every single ingest). For a vault with 500 pages, an ingest
that writes 5 new pages triggers a full scan and regex parse of all 500 pages. For 5,000
pages this takes multiple seconds per ingest.

The data needed to build backlinks already exists per-page at upsert time — the set of
`[[wikilinks]]` found in the page's content. There is no reason to re-derive them for
unchanged pages during `partial_reconcile`.

---

## Implementation Plan

### Strategy: explicit links table with incremental upsert

Add a `links` table that stores one row per directed wikilink edge. On every page upsert,
delete the outgoing link rows for that page and re-insert them. The `backlinks` JSON column
becomes a derived view over this table, rebuilt only for pages whose link neighbourhood
changed.

---

### Step 1 — Add `links` table to schema

**File:** `core/database.py:_ensure_schema`

```sql
CREATE TABLE IF NOT EXISTS links (
    source_path TEXT NOT NULL,
    target_stem TEXT NOT NULL,    -- the [[PageName]] stem, unresolved
    PRIMARY KEY (source_path, target_stem)
);

CREATE INDEX IF NOT EXISTS links_target_idx ON links(target_stem);
```

`target_stem` stores the raw wikilink text (e.g. `"Transformers"`) rather than the resolved
path. Resolution happens at query time using the same stem → path lookup already in
`_rebuild_backlinks`. This avoids cascading updates when pages are renamed.

---

### Step 2 — Update `upsert_page` to write links incrementally

**File:** `core/database.py:upsert_page`

After the page INSERT/UPDATE, extract outgoing wikilinks from `content` and sync the `links`
table for this page only:

```python
# Extract outgoing wikilinks from this page
outgoing = set(
    m.strip()
    for m in re.findall(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]", content or "")
)

# Replace all outgoing links for this page in a single transaction step
conn.execute("DELETE FROM links WHERE source_path = ?", (rel_path,))
if outgoing:
    conn.executemany(
        "INSERT OR IGNORE INTO links(source_path, target_stem) VALUES (?, ?)",
        [(rel_path, stem) for stem in outgoing],
    )
# conn.commit() already called at the end of upsert_page
```

---

### Step 3 — Replace `_rebuild_backlinks` with two variants

**File:** `core/database.py`

#### 3a — Full rebuild (used by `reconcile`)

Keep the existing full-rebuild logic but read from the `links` table instead of scanning
page content. This is now a pure SQL operation:

```python
def _rebuild_backlinks_full(conn: sqlite3.Connection) -> None:
    """Rewrite the backlinks JSON column for every page from the links table.

    Used by reconcile() after a full vault scan. O(pages) reads but no regex scanning
    since outgoing links are already stored in the links table.
    """
    # Resolve stems to paths using the same collision-aware logic as before
    rows = conn.execute("SELECT id, file_path FROM pages").fetchall()
    title_to_path: dict[str, str] = {}
    for r in sorted(rows, key=lambda r: r["file_path"]):
        stem = r["file_path"].rsplit("/", 1)[-1].replace(".md", "")
        if stem not in title_to_path:
            title_to_path[stem] = r["file_path"]

    # Build backlink map from links table — no page content reads needed
    backlink_map: dict[str, list[str]] = {r["file_path"]: [] for r in rows}
    link_rows = conn.execute("SELECT source_path, target_stem FROM links").fetchall()
    for lr in link_rows:
        target = title_to_path.get(lr["target_stem"])
        if target and target != lr["source_path"] and lr["source_path"] not in backlink_map.get(target, []):
            backlink_map.setdefault(target, []).append(lr["source_path"])

    for path, backlinks in backlink_map.items():
        conn.execute(
            "UPDATE pages SET backlinks=? WHERE file_path=?",
            (json.dumps(backlinks), path),
        )
    conn.commit()
```

#### 3b — Incremental update (used by `partial_reconcile`)

Only recompute backlinks for pages that are the target of any link in the changed set:

```python
def _rebuild_backlinks_incremental(
    conn: sqlite3.Connection, changed_paths: list[str]
) -> None:
    """Recompute backlinks only for pages that could be affected by the changed pages.

    A page's backlinks can change when:
    1. One of the changed pages now links to it (new backlink).
    2. One of the changed pages used to link to it (removed backlink).
    Both cases are covered by recomputing backlinks for all targets of the
    changed pages' outgoing links, plus the changed pages themselves.

    Args:
        conn: Open database connection.
        changed_paths: Relative file paths of pages that were just written.
    """
    if not changed_paths:
        return

    # Stems of changed pages — they may be targets of links in other pages
    changed_stems = {p.rsplit("/", 1)[-1].replace(".md", "") for p in changed_paths}

    # Outgoing stems from changed pages — their targets' backlinks may have changed
    placeholders = ",".join("?" * len(changed_paths))
    outgoing_rows = conn.execute(
        f"SELECT target_stem FROM links WHERE source_path IN ({placeholders})",
        changed_paths,
    ).fetchall()
    affected_stems = changed_stems | {r["target_stem"] for r in outgoing_rows}

    # Resolve stems to paths
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
            if lr["target_stem"] == target_stem
            and lr["source_path"] != target_path
        ]
        conn.execute(
            "UPDATE pages SET backlinks=? WHERE file_path=?",
            (json.dumps(backlinks), target_path),
        )
    conn.commit()
```

---

### Step 4 — Wire into reconcile and partial_reconcile

**File:** `core/database.py:reconcile`

Replace the `_rebuild_backlinks(conn)` call at line 268 with `_rebuild_backlinks_full(conn)`.

**File:** `core/database.py:partial_reconcile`

Replace `_rebuild_backlinks(conn)` with:

```python
_rebuild_backlinks_incremental(
    conn, [str(p.relative_to(wiki_root)) for p in changed_paths if p.exists()]
)
```

---

### Step 5 — Handle `delete_page` cleanup

**File:** `core/database.py:delete_page`

Add a `links` cleanup before the page delete:

```python
def delete_page(conn: sqlite3.Connection, rel_path: str) -> None:
    conn.execute("DELETE FROM links WHERE source_path = ?", (rel_path,))
    conn.execute("DELETE FROM pages WHERE file_path=?", (rel_path,))
    conn.commit()
```

---

### Step 6 — Write tests

**File:** `tests/test_database.py`

- `test_upsert_page_writes_links`: upsert a page with two wikilinks → verify links table rows
- `test_upsert_page_replaces_links_on_update`: update page removing one link → old link gone
- `test_rebuild_backlinks_full_correct`: two pages linking to a third → third has two backlinks
- `test_rebuild_backlinks_incremental_only_affects_neighbourhood`: 100 pages, change 2 →
  verify only the touched pages' backlinks are updated (mock the execute calls to count them)
- `test_delete_page_removes_links`: delete page → links rows for that source removed
- `test_backlinks_wikilink_collision_warning`: two pages with the same stem → log warning

---

### Step 7 — Documentation

- `CLAUDE.md` Known Gotchas — update the wikilink collision note to reference the `links`
  table instead of the in-memory dict in `_rebuild_backlinks`
- `core/README.md` — update `database.py` module description to mention the links table

---

### Estimated scope

| Area | Files | Changes |
|---|---|---|
| Schema | `core/database.py` | +1 table, +1 index |
| CRUD | `core/database.py` | `upsert_page` updated, `delete_page` updated |
| Backlinks | `core/database.py` | `_rebuild_backlinks` → 2 new functions |
| Tests | `tests/test_database.py` | 6 new / updated test cases |
| Docs | `CLAUDE.md`, `core/README.md` | — |

No new dependencies. Fully backward-compatible with existing vaults (the links table is
populated incrementally as pages are next upserted; full reconcile fills it completely).
