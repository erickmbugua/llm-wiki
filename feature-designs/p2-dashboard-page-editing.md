# P2 — Dashboard Page Editing

## Problem Statement

The dashboard (`app/static/`, `app/templates/index.html`) is entirely read-only. Users
can browse pages, search the vault, view the knowledge graph, and trigger ingest — but they
cannot edit a page's content from the browser.

To edit a wiki page today, a user must:
1. Find the page in the dashboard to get its file path
2. Navigate to that file on disk in Finder/terminal
3. Open it in a text editor
4. Save it
5. Wait for the next `reconcile` call (or run `llm-wiki reconcile`) to update the DB

This creates unnecessary friction for small corrections — fixing a typo in a concept page,
adding a wikilink, or annotating a source page with a personal note. These are exactly the
kinds of edits that a personal knowledge base tool should make frictionless.

The Obsidian viewer provides full editing, but the web dashboard is the only way to interact
with the vault without installing a third-party app.

---

## Implementation Plan

### Step 1 — Add a PUT endpoint for page content

**File:** `core/server.py`

```python
class UpdatePageRequest(BaseModel):
    content: str


@app.put("/api/vaults/{vault_name}/pages/content")
async def api_update_page(
    vault_name: str,
    file_path: str = Query(...),
    req: UpdatePageRequest = ...,
) -> dict[str, str]:
    """Write new content to a wiki page and re-index it.

    The page must already exist. Creates the file if absent only within the
    vault's wiki/ directory. Path traversal is rejected (HTTP 400).

    Raises:
        HTTPException: 400 if the file path escapes wiki root.
        HTTPException: 404 if the vault is not registered.
    """
    _, vpath = _get_vault(vault_name)
    wiki_root = (vpath / "wiki").resolve()
    page_path = (wiki_root / file_path).resolve()

    if not page_path.is_relative_to(wiki_root):
        raise HTTPException(status_code=400, detail="Invalid file path")

    # Only allow writing .md files
    if page_path.suffix.lower() != ".md":
        raise HTTPException(status_code=400, detail="Only .md files can be edited")

    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text(req.content)

    # Re-index immediately so search reflects the edit
    conn = get_db(vpath)
    try:
        from .database import partial_reconcile
        partial_reconcile(conn, wiki_root, [page_path])
    finally:
        conn.close()

    return {"status": "ok", "file_path": file_path}
```

---

### Step 2 — Add a simple in-browser editor

**File:** `app/static/` (JavaScript and CSS)

The editor does not need a full rich-text experience — the vault content is markdown, and
the target users are technical. A `<textarea>` with monospace font and keyboard shortcuts
is sufficient for v1.

UI flow:
1. User clicks a page in the page list → page content renders in the right panel
2. An "Edit" button appears in the page header
3. Clicking "Edit" replaces the rendered content with a full-height `<textarea>` pre-filled
   with the raw markdown
4. Two buttons appear: "Save" and "Cancel"
5. "Save" sends `PUT /api/vaults/{vault}/pages/content?file_path=...` with the textarea
   value as the body
6. On success, re-fetch and re-render the page content, show a brief "Saved" toast
7. "Cancel" reverts to the rendered view without saving

Keyboard shortcuts:
- `Ctrl+S` / `Cmd+S` while editing → Save
- `Escape` while editing → Cancel (with a confirmation if content was changed)

---

### Step 3 — Add dirty-state guard

**File:** `app/static/` (JavaScript)

Track whether the textarea content has changed from the original. If the user tries to
navigate away from an unsaved edit (clicks another page, or presses Escape), show a
`confirm()` dialog: "You have unsaved changes. Discard them?".

Also set `window.onbeforeunload` while editing to catch browser tab close / refresh:

```javascript
window.onbeforeunload = (e) => {
    if (isDirty) {
        e.preventDefault();
        e.returnValue = '';
    }
};
// Clear on save or cancel
```

---

### Step 4 — Add a "New Page" flow

Once editing is implemented, creating a new page is a small extension:

Add a "New Page" button in the sidebar that opens a modal asking for:
- Category (Sources / Concepts / Entities — dropdown)
- Page name (text input, used as filename slug)

On confirm, the dashboard generates a minimal YAML frontmatter template:

```markdown
---
title: <name>
tags: []
---

# <name>

```

And immediately enters edit mode for the new page using the same editor component.
Saving calls the PUT endpoint, which will create the file since `page_path.parent.mkdir`
and `page_path.write_text` handle new files.

---

### Step 5 — Write tests

**File:** `tests/test_server.py`

- `test_api_update_page_writes_content`: PUT with new content → file on disk updated,
  response is 200 with `{"status": "ok"}`
- `test_api_update_page_path_traversal_rejected`: `file_path=../../etc/passwd` → 400
- `test_api_update_page_non_md_rejected`: `file_path=Sources/foo.txt` → 400
- `test_api_update_page_reindexes`: after update, GET pages list shows new content/summary
- `test_api_update_page_creates_if_missing`: new file path → file created

---

### Step 6 — Documentation

- `CLAUDE.md` Project Structure — add PUT endpoint to the server.py route table
- `core/README.md` — update `server.py` API table with the new endpoint

---

### Estimated scope

| Area | Files | Changes |
|---|---|---|
| Server | `core/server.py` | 1 new endpoint (`PUT /pages/content`) |
| Frontend | `app/static/` | Edit button, textarea editor, save/cancel flow, dirty guard |
| Tests | `tests/test_server.py` | 5 new test cases |
| Docs | `CLAUDE.md`, `core/README.md` | Route table update |

No new dependencies. The PUT endpoint reuses the existing `partial_reconcile` so the DB
stays in sync without a full reconcile pass.
