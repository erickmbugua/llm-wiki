# P1 — Auto-Maintain wiki/index.md

## Problem Statement

`init_vault` in `core/vault.py:13` creates `wiki/index.md` with a YAML frontmatter header
and an empty markdown table:

```markdown
| Page | Category | Summary |
|------|----------|---------|
```

Nothing ever updates this file. After ingesting 50 documents, `wiki/index.md` still shows
an empty table. A user who opens the vault in Obsidian — the intended viewer — sees a
useless placeholder that was never described as intentionally empty.

The file is indexed by the FTS5 database and shows up in search results, lint reports
(it has no outgoing links, making it appear as a potential orphan), and the dashboard page
list. It occupies visual space without providing value.

This contradicts the stated purpose in `schema.md` ("Auto-maintained table of contents")
and in the vault structure documentation in `CLAUDE.md`.

---

## Implementation Plan

### Strategy: regenerate index.md after every ingest

After each successful ingest, rebuild `index.md` from the current database contents.
This is a pure DB read + file write — no LLM call, no performance concern. The index
remains up-to-date automatically without requiring user intervention.

---

### Step 1 — Add `rebuild_index` function to `core/vault.py`

```python
def rebuild_index(vault_path: Path) -> None:
    """Regenerate wiki/index.md from the current database, grouped by category.

    Reads all pages via list_pages, sorts by category then title, and writes a
    markdown table with title, category, and summary columns. The file is always
    fully rewritten — no incremental append.

    Args:
        vault_path: Root directory of the vault.
    """
    from .database import get_db, list_pages

    conn = get_db(vault_path)
    try:
        pages = list_pages(conn)
    finally:
        conn.close()

    wiki = vault_path / "wiki"
    index_path = wiki / "index.md"

    now = datetime.now().strftime("%Y-%m-%d")

    lines: list[str] = [
        "---",
        "title: Index",
        "type: index",
        f"updated: {now}",
        "---",
        "",
        "# Wiki Index",
        "",
    ]

    # Group by category
    categories: dict[str, list[dict[str, Any]]] = {}
    for page in pages:
        cat = page.get("category") or "root"
        if cat == "root":
            continue   # skip log.md, schema.md, index.md itself
        categories.setdefault(cat, []).append(page)

    for cat in sorted(categories):
        lines.append(f"## {cat}")
        lines.append("")
        lines.append("| Page | Summary |")
        lines.append("|------|---------|")
        for page in sorted(categories[cat], key=lambda p: p.get("title", "")):
            title = page.get("title", "Untitled")
            fp = page.get("file_path", "")
            stem = fp.replace(".md", "").rsplit("/", 1)[-1] if fp else title
            summary = (page.get("summary") or "").replace("|", "—")[:120]
            lines.append(f"| [[{stem}]] | {summary} |")
        lines.append("")

    total = sum(len(v) for v in categories.values())
    lines.append(f"*{total} pages · updated {now}*")
    lines.append("")

    index_path.write_text("\n".join(lines))
```

---

### Step 2 — Call `rebuild_index` from `ingest_source`

**File:** `core/ingest.py:ingest_source`

At the end of the function, after `_append_log`, add:

```python
if not dry_run:
    from .vault import rebuild_index
    rebuild_index(vault_path)
```

The call happens after `partial_reconcile`, so the DB is already up-to-date. The write to
`index.md` will be picked up by the next `partial_reconcile` (called at the next ingest) —
no need to trigger a re-reconcile immediately.

---

### Step 3 — Add a CLI command for manual rebuild

**File:** `main.py`

```python
@cli.command("index")
@click.option("--vault", "-v", default=None, help="Vault name (uses default if unset)")
def rebuild_index_cmd(vault: str | None):
    """Rebuild wiki/index.md from the current database state."""
    from core.vault import rebuild_index

    config = GlobalConfig.load()
    try:
        vname, vpath = config.resolve_vault(vault)
    except (ValueError, KeyError) as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1) from None

    rebuild_index(vpath)
    console.print(f"[green]✓[/green] Rebuilt index for vault [bold]{vname}[/bold]")
```

This lets users rebuild after a manual edit, a vault migration, or after using `reconcile`
without ingesting.

---

### Step 4 — Expose via the REST API

**File:** `core/server.py`

Add a POST endpoint for explicit rebuilds (useful for the dashboard "Rebuild Index" button):

```python
@app.post("/api/vaults/{vault_name}/index/rebuild")
async def api_rebuild_index(vault_name: str) -> dict[str, str]:
    """Rebuild wiki/index.md from the current database state."""
    _, vpath = _get_vault(vault_name)
    from .vault import rebuild_index
    rebuild_index(vpath)
    return {"status": "ok"}
```

---

### Step 5 — Update `_index_template` to indicate it will be auto-maintained

**File:** `core/vault.py:_index_template`

The initial template is immediately replaced on first ingest. Update the placeholder text so
it is useful before the first ingest:

```python
def _index_template() -> str:
    return f"""\
---
title: Index
type: index
updated: {datetime.now().strftime("%Y-%m-%d")}
---

# Wiki Index

*No pages yet. Ingest a source to populate this index.*
"""
```

---

### Step 6 — Write tests

**File:** `tests/test_vault.py`

- `test_rebuild_index_empty_vault`: no non-root pages → index contains placeholder text
- `test_rebuild_index_groups_by_category`: pages in Concepts and Sources → two sections
- `test_rebuild_index_sorts_by_title`: pages in random order → sorted alphabetically in output
- `test_rebuild_index_truncates_summary`: long summary → capped at 120 chars in table
- `test_rebuild_index_escapes_pipe_in_summary`: summary with `|` → replaced with em dash

**File:** `tests/test_ingest.py`

- `test_ingest_source_calls_rebuild_index`: after successful ingest, verify `rebuild_index`
  was called (mock it to avoid file I/O)
- `test_ingest_source_dry_run_does_not_rebuild_index`: dry_run=True → rebuild not called

---

### Step 7 — Documentation

- `CLAUDE.md` Vault Structure section — update `index.md` description from "Auto-maintained
  table of contents" to clarify it is rebuilt after each ingest by `rebuild_index`
- `core/README.md` — add `rebuild_index` to the `vault.py` function table

---

### Estimated scope

| Area | Files | Changes |
|---|---|---|
| Vault | `core/vault.py` | `rebuild_index`, updated `_index_template` |
| Ingest | `core/ingest.py` | call `rebuild_index` after ingest |
| Server | `core/server.py` | 1 new endpoint |
| CLI | `main.py` | 1 new command |
| Tests | `tests/test_vault.py`, `tests/test_ingest.py` | ~7 new test cases |
| Docs | `CLAUDE.md`, `core/README.md` | — |

No new dependencies. No schema changes. The index file is regenerated atomically (full
overwrite) so a crashed ingest cannot leave it partially written.
