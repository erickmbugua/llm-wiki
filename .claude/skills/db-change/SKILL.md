---
description: Add or alter a table or column in the SQLite schema under core/db/. Enforces sqlite-vec extension load order, FTS5 trigger rules, and relational atomicity patterns. Use when the user asks to add a table, column, or change the database schema.
argument-hint: <description of change>
---

Change to make: $ARGUMENTS

---

## Hard constraints — read before touching any schema code

**sqlite-vec must load before schema creation.**
`get_db()` calls `sqlite_vec.load(conn)` before `_ensure_schema()`. Any `CREATE VIRTUAL TABLE … USING vec0(…)` therefore works. Any connection that bypasses `get_db()` will fail with `"no such module: vec0"`. Never bypass `get_db()`.

**Never INSERT into `pages_fts` directly.**
`pages_fts` is a content table kept in sync with `pages` via three triggers (`pages_ai`, `pages_au`, `pages_ad`). Inserting directly corrupts the index. To backfill after a schema change: `INSERT INTO pages_fts(pages_fts) VALUES ('rebuild')`.

---

## Adding a new table

1. Add `CREATE TABLE IF NOT EXISTS` DDL to `_ensure_schema()` in `core/db/connection.py`
2. Add CRUD functions to the appropriate sub-module (`pages.py`, `queue.py`, `jobs.py`) or a new file
3. If the table stores relational edges (like `links`), follow the atomicity pattern:
   - In the parent `upsert_*` function: DELETE all outgoing rows for the parent, then INSERT current rows — in one transaction
   - In the parent `delete_*` function: DELETE all rows referencing the parent **before** removing the parent row
4. Export new public functions from `core/db/__init__.py` — add to `__all__`
5. If adding a new file, add it to:
   - The sub-module map in `core/db/__init__.py`'s docstring
   - The `core/db/` section in `core/README.md`

---

## Adding a column to an existing table

SQLite's `ALTER TABLE … ADD COLUMN` cannot add `NOT NULL` columns without a default. Choose one:

- **Nullable:** `ADD COLUMN col TYPE` — handle `None` in all callers
- **With default:** `ADD COLUMN col TYPE DEFAULT value`
- **Rename dance (last resort):** create new table → copy rows → drop old → rename — guard with `PRAGMA user_version` so it only runs once

The rename dance belongs in `_ensure_schema()` as a conditional block, not a separate migration file.

---

## Adding an FTS5-indexed column

If the new column should be full-text searchable:

1. Drop and recreate the `pages_fts` virtual table DDL in `_ensure_schema()` to include the new column
2. Recreate all three triggers (`pages_ai`, `pages_au`, `pages_ad`) to include the new column
3. Backfill existing rows: `INSERT INTO pages_fts(pages_fts) VALUES ('rebuild')`

---

## Tests

- Mirror the new sub-module in `tests/` (e.g. `core/db/widgets.py` → `tests/test_db_widgets.py`)
- Test schema creation: call `get_db()` on a temp path, then `PRAGMA table_info(<table>)` to assert columns exist
- Test CRUD: happy path, duplicate key, missing parent (for relational tables)
- Integration test if the table is part of a multi-module flow — mark `@pytest.mark.integration`

---

## Documentation

- Update the schema block in `core/README.md`
- Update the `core/db/__init__.py` sub-module map docstring if a new file was added
- Add a Known Gotchas entry in `CLAUDE.md` if there is a non-obvious constraint (ordering requirement, collision behaviour, etc.)

---

## Run QA

Run `/qa` before declaring the task complete.
