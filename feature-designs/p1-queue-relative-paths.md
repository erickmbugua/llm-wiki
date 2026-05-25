# P1 — Ingest Queue: Store Relative Paths

## Problem Statement

`queue_raw_file` in `core/database.py:357` stores the raw file path exactly as supplied by
the caller. The caller is `core/watcher.py:_RawFolderHandler._handle`, which receives
`event.src_path` directly from watchdog — an absolute OS path such as
`/Users/alice/Documents/my-vault/raw/paper.pdf`.

This means the `ingest_queue` table holds absolute paths. If any of the following happen,
every queued item becomes a dead pointer with no way to recover:

1. The vault directory is moved or renamed (e.g., `my-vault` → `AI-notes`)
2. The vault is synced to another machine via iCloud/Dropbox/git and opened there
3. The user's home directory path changes (e.g., a macOS username change)

When `ingest_queued` tries to process a stale absolute path, `_extract_text` returns
`("", source)` because `Path(source).exists()` is False, which causes `ingest_source` to
raise `ValueError("Could not extract text from: ...")`. The item is marked `"failed"`.
The user has no actionable feedback — the error message shows a path that looks valid but
simply no longer exists at that location.

The fix is to store paths relative to the vault root, so the queue is vault-portable.

---

## Implementation Plan

### Step 1 — Change `queue_raw_file` to accept and store relative paths

**File:** `core/database.py:queue_raw_file`

The function signature does not change (still accepts a string), but update the docstring
to require vault-relative paths:

```python
def queue_raw_file(conn: sqlite3.Connection, file_path: str) -> None:
    """Add a raw file path to the ingest queue with status ``"pending"``.

    Args:
        file_path: Path to the raw file, relative to the vault root
            (e.g. ``"raw/paper.pdf"``). Must not be an absolute path.
            Absolute paths will raise ValueError.

    Raises:
        ValueError: file_path is absolute.
    """
    if Path(file_path).is_absolute():
        raise ValueError(
            f"queue_raw_file requires a vault-relative path, got absolute: {file_path}"
        )
    conn.execute(...)
```

Adding the guard prevents future callers from accidentally re-introducing absolute paths.

---

### Step 2 — Update the watcher to compute relative paths

**File:** `core/watcher.py:_RawFolderHandler._handle`

The handler has access to `self.vault_path`. Compute the relative path before queuing:

```python
def _handle(self, path: str) -> None:
    p = Path(path)
    if p.suffix.lower() in IGNORED_SUFFIXES or p.name.startswith("."):
        return
    try:
        rel_path = str(p.relative_to(self.vault_path))
    except ValueError:
        # Path is outside the vault — this should not happen in normal use
        log.warning("Detected file outside vault root, skipping: %s", path)
        return
    log.info("Raw file detected: %s", p.name)
    conn = get_db(self.vault_path)
    try:
        queue_raw_file(conn, rel_path)
    finally:
        conn.close()
    if self.on_file:
        self.on_file(str(p))  # callback still receives absolute path for logging
```

---

### Step 3 — Update `ingest_queued` to reconstruct the absolute path

**File:** `core/ingest.py:ingest_queued`

The `item["file_path"]` values are now relative. Reconstruct the absolute path before
passing to `ingest_source`, which still expects a resolvable source string:

```python
for item in pending:
    rel_fp = item["file_path"]
    abs_fp = str(vault_path / rel_fp)   # reconstruct absolute path for extraction
    mark_queue_item(conn, rel_fp, "processing")
    try:
        r = ingest_source(vault_path, abs_fp, vault_name)
        mark_queue_item(conn, rel_fp, "done")
        results.append({"file": rel_fp, "status": "done", **r})
    except Exception as e:
        mark_queue_item(conn, rel_fp, "failed", str(e))
        log.error("Failed to ingest %s: %s", rel_fp, e)
        results.append({"file": rel_fp, "status": "failed", "error": str(e)})
```

---

### Step 4 — Add a one-time migration for existing queues

**File:** `core/database.py:_ensure_schema`

For existing vaults where the queue may already have absolute paths, add a migration guard.
The migration converts any absolute path in the queue to a relative path by stripping the
vault path prefix (the vault path is not available inside `_ensure_schema`, so this migration
is best run lazily on first use).

Add a `migrate_queue_to_relative_paths` function called from `get_db`:

```python
def migrate_queue_to_relative_paths(
    conn: sqlite3.Connection, vault_path: Path
) -> int:
    """Convert any absolute paths in ingest_queue to vault-relative paths.

    Safe to call repeatedly — items already relative are not changed.

    Args:
        conn: Open database connection.
        vault_path: Vault root used to compute relative paths.

    Returns:
        Number of rows migrated.
    """
    vault_str = str(vault_path.resolve())
    rows = conn.execute(
        "SELECT file_path FROM ingest_queue WHERE file_path LIKE ?",
        (f"{vault_str}/%",),
    ).fetchall()
    migrated = 0
    for row in rows:
        abs_path = row["file_path"]
        rel_path = abs_path[len(vault_str) + 1:]   # strip vault prefix + separator
        conn.execute(
            "UPDATE ingest_queue SET file_path = ? WHERE file_path = ?",
            (rel_path, abs_path),
        )
        migrated += 1
    if migrated:
        conn.commit()
        log.info("Migrated %d ingest_queue rows from absolute to relative paths", migrated)
    return migrated
```

**File:** `core/database.py:get_db`

Call the migration after `_ensure_schema` (vault_path is available in `get_db`):

```python
def get_db(vault_path: Path) -> sqlite3.Connection:
    ...
    _ensure_schema(conn)
    migrate_queue_to_relative_paths(conn, vault_path)
    return conn
```

---

### Step 5 — Write tests

**File:** `tests/test_database.py`

- `test_queue_raw_file_rejects_absolute_path`: absolute path → raises ValueError
- `test_queue_raw_file_accepts_relative_path`: relative path → stored correctly
- `test_migrate_queue_to_relative_paths`: pre-insert absolute paths → verify converted to
  relative; verify idempotent on second call

**File:** `tests/test_watcher.py` (or add to existing watcher tests)

- `test_watcher_queues_relative_path`: drop a file, verify the DB row contains a relative
  path (not absolute)

**File:** `tests/test_ingest.py`

- `test_ingest_queued_resolves_relative_to_absolute`: queue item with relative path →
  verify `ingest_source` is called with the absolute path

---

### Step 6 — Documentation

- `CLAUDE.md` — update the Known Gotchas section: remove the note about absolute paths and
  add a note that queue paths are relative to vault root (important for anyone writing
  external tools that interact with `ingest_queue`)

---

### Estimated scope

| Area | Files | Changes |
|---|---|---|
| DB | `core/database.py` | `queue_raw_file` guard, `migrate_queue_to_relative_paths` |
| Watcher | `core/watcher.py` | compute relative path before queuing |
| Ingest | `core/ingest.py` | reconstruct absolute path in `ingest_queued` |
| Tests | 3 test files | ~5 new test cases |
| Docs | `CLAUDE.md` | Known Gotchas update |

No new dependencies. Migration is safe to run on existing vaults at next server start.
