# P2 — SQLite Schema Migrations

## Problem Statement

`_ensure_schema` in `core/database.py:41` creates all tables with `CREATE TABLE IF NOT
EXISTS` and triggers with `CREATE TRIGGER IF NOT EXISTS`. This means that for an existing
database, no schema changes take effect after the initial creation.

Adding a new column (e.g., `embedding BLOB` for vector search, or a `dry_run` flag on the
queue) requires either:
- Manually running `ALTER TABLE` on each vault's `wiki.db`
- Deleting `wiki.db` and running `llm-wiki reconcile` to rebuild from markdown files

Neither is acceptable for a tool that is supposed to be frictionless. The user should
never need to interact with the SQLite file directly.

There is no `schema_version` table, no migration log, and no migration runner. Any future
schema evolution without this infrastructure will corrupt existing vaults silently (the
new column is missing; queries referencing it fail with `OperationalError`) or require a
"blow away the DB" workaround.

---

## Implementation Plan

### Strategy: linear migration list with a version table

Store a `schema_version` integer in the database. On every `get_db` call, compare the
stored version against the current expected version. If behind, run migrations in order
until current. Each migration is a Python function that receives the open connection.

This is the SQLite equivalent of Alembic or Django migrations, implemented in ~50 lines.

---

### Step 1 — Add `schema_version` table and initial seed

**File:** `core/database.py`

Add a constant at module level:

```python
SCHEMA_VERSION = 1   # bump this when adding a migration
```

Update `_ensure_schema` to create the version table and set the initial version only on
fresh databases:

```python
def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the initial schema if this is a new database."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pages (
            ...   -- unchanged
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts ...;
        CREATE TRIGGER IF NOT EXISTS pages_ai ...;
        CREATE TRIGGER IF NOT EXISTS pages_ad ...;
        CREATE TRIGGER IF NOT EXISTS pages_au ...;

        CREATE TABLE IF NOT EXISTS ingest_queue (
            ...   -- unchanged
        );
    """)

    # Seed the version row only on first creation
    existing = conn.execute("SELECT version FROM schema_version").fetchone()
    if existing is None:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()
```

---

### Step 2 — Define the migration runner

**File:** `core/database.py`

```python
# Each entry is (from_version, to_version, migration_function).
# Migrations are applied in order until db version == SCHEMA_VERSION.
_MIGRATIONS: list[tuple[int, int, Callable[[sqlite3.Connection], None]]] = [
    # Example entry when the first migration is added:
    # (1, 2, _migrate_v1_to_v2),
]


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Apply any pending schema migrations in version order.

    Reads the current version from schema_version, runs each applicable
    migration function in sequence, and updates the stored version after each.

    Args:
        conn: Open database connection.
    """
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        return   # new DB; _ensure_schema already set the current version
    current = row["version"]
    if current == SCHEMA_VERSION:
        return

    for from_v, to_v, migrate_fn in _MIGRATIONS:
        if current == from_v:
            log.info("Running DB migration v%d → v%d", from_v, to_v)
            migrate_fn(conn)
            conn.execute("UPDATE schema_version SET version = ?", (to_v,))
            conn.commit()
            current = to_v
            if current == SCHEMA_VERSION:
                break

    if current != SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema migration incomplete: at v{current}, expected v{SCHEMA_VERSION}. "
            f"Delete the wiki.db file and run `llm-wiki reconcile` to rebuild."
        )
```

---

### Step 3 — Wire into `get_db`

**File:** `core/database.py:get_db`

```python
def get_db(vault_path: Path) -> sqlite3.Connection:
    ...
    _ensure_schema(conn)
    _run_migrations(conn)
    return conn
```

The migration runner is idempotent — it is a no-op if the DB is already at `SCHEMA_VERSION`.
The cost of the version check is one `SELECT` on a single-row table, sub-microsecond.

---

### Step 4 — Document how to add a future migration

**File:** `core/database.py` — module docstring block

```python
# HOW TO ADD A SCHEMA MIGRATION
# 1. Increment SCHEMA_VERSION by 1.
# 2. Write a migration function:
#
#    def _migrate_vN_to_vN1(conn: sqlite3.Connection) -> None:
#        conn.execute("ALTER TABLE pages ADD COLUMN embedding BLOB")
#
# 3. Append to _MIGRATIONS:
#
#    (N, N+1, _migrate_vN_to_vN1),
#
# The migration runs exactly once on the first get_db() call after upgrade.
# It is wrapped in a commit by _run_migrations, so it is atomic.
```

---

### Step 5 — Write tests

**File:** `tests/test_database.py`

- `test_fresh_db_has_current_schema_version`: create new DB → version == SCHEMA_VERSION
- `test_no_migrations_on_current_version`: `_run_migrations` on a current DB → no-op
  (mock `_MIGRATIONS` to verify migration functions not called)
- `test_migration_applied_in_order`: set DB to version 1, add mock migrations 1→2 and 2→3,
  set SCHEMA_VERSION=3 → both run in order, final version is 3
- `test_migration_updates_version_after_each_step`: verify version row is updated after
  each migration step (not just at the end)
- `test_migration_error_raises_runtime_error`: incomplete migration chain → RuntimeError
  with a clear message

---

### Step 6 — Documentation

- `CLAUDE.md` — add to the Known Gotchas section: how to add a migration, the fact that
  `_ensure_schema` is for initial creation only, and that `ALTER TABLE ADD COLUMN` is the
  common migration operation
- `core/README.md` — update `database.py` description to mention the migration system

---

### Estimated scope

| Area | Files | Changes |
|---|---|---|
| DB | `core/database.py` | `schema_version` table, `SCHEMA_VERSION` constant, `_MIGRATIONS` list, `_run_migrations` |
| Startup | `core/database.py:get_db` | call `_run_migrations` |
| Tests | `tests/test_database.py` | 5 new test cases |
| Docs | `CLAUDE.md`, `core/README.md` | developer guidance on migrations |

No new dependencies. No data loss risk — migrations only add; they never drop columns
(SQLite does not support column drops without a full table rebuild, which is out of scope).
