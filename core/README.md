# core/

The shared Python library used by the CLI (`main.py`), the web server (`core/server.py`), and the MCP server (`core/mcp_server.py`). All business logic lives here.

---

## Module Map

```
core/
├── config.py       Global + per-vault configuration
├── embeddings.py   compute_embedding() — provider-agnostic litellm embedding call
├── extraction.py   Text extraction from files and URLs (_extract_text, _fetch_url, _extract_pdf, _extract_docx)
├── chunking.py     Overlapping window splitting and map-reduce summarization (_chunk_text, _summarize_chunks)
├── prompts.py      Ingest prompt assembly and LLM JSON parsing (_build_ingest_prompt, _parse_llm_json)
├── db/             SQLite persistence layer (split by concern)
│   ├── __init__.py     Re-exports all public symbols
│   ├── connection.py   get_db(), schema DDL
│   ├── pages.py        Page CRUD + category/summary helpers
│   ├── search.py       FTS5, vector KNN, hybrid RRF search
│   ├── reconcile.py    Filesystem ↔ DB sync, backlink graph
│   ├── queue.py        ingest_queue CRUD
│   └── jobs.py         ingest_jobs CRUD
├── vault.py        Vault init and stats
├── watcher.py      Watchdog file monitor on raw/
├── ingest.py       Orchestration only: extract → related search → LLM call → parse → write → reconcile → log
├── query.py        FTS5 context + LLM-powered Q&A
├── lint.py         Structural checks + LLM contradiction detection
├── server.py       FastAPI app: REST API + static file serving
└── mcp_server.py   MCP server exposing wiki operations as tools
```

---

## config.py

Manages two configuration scopes:

**`GlobalConfig`** — persisted at `~/.llm-wiki/config.json`
- `vaults: dict[str, str]` — name → absolute path registry
- `default_vault: str | None` — used when `--vault` is omitted
- `model: str` — default LiteLLM model string
- `server_port: int` — dashboard port (default 8000)
- `context_chars: int` — default source text limit sent to LLM (default 24,000)
- `chunk_size: int` — max chars per chunk for map-reduce ingest (default 20,000)
- `chunk_overlap: int` — overlap chars between adjacent chunks (default 500)
- `embedding_model: str` — LiteLLM embedding model string (default `"ollama/nomic-embed-text"`)
- `embedding_dim: int` — vector dimension matching the embedding model (default 768)

**`VaultConfig`** — persisted at `<vault>/.llm-wiki/config.json`
- `name: str` — display name
- `model: str | None` — overrides global when set
- `context_chars: int | None` — overrides global when set
- `chunk_size: int | None` — overrides global when set
- `chunk_overlap: int | None` — overrides global when set
- `embedding_model: str | None` — overrides global when set
- `embedding_dim: int | None` — overrides global when set

**`resolve_model(vault_path)`** — returns the effective model for a vault (vault override → global → hardcoded default). Call this in any module that needs to invoke the LLM.

**`resolve_context_chars(vault_path)`** — returns the effective character limit for source text (vault override → global → 24,000). Use this instead of any hardcoded constant.

**`resolve_chunk_config(vault_path)`** — returns `(chunk_size, chunk_overlap)` using the same priority chain.

**`resolve_embedding_config(vault_path)`** — returns `(embedding_model, embedding_dim)` using the same priority chain.

Constants:
- `VAULT_INTERNAL_DIR = ".llm-wiki"` — internal dir name inside each vault
- `VAULT_DB_FILE = "wiki.db"` — SQLite DB filename

---

## vault.py

Handles vault initialization, stats, and index maintenance. No LLM calls here.

**`init_vault(vault_path, name)`**
Creates the full vault skeleton:
- `raw/` — source drop folder
- `wiki/Sources/`, `wiki/Concepts/`, `wiki/Entities/`
- `wiki/index.md`, `wiki/log.md`, `wiki/schema.md` (from templates, never overwrite if existing)
- `.llm-wiki/` with `.gitignore` and per-vault `config.json`

**`vault_stats(vault_path) → dict`**
Returns `{total_pages, raw_queued, categories: {Sources, Concepts, Entities}}` by scanning the filesystem. Used by the CLI `status` command and the dashboard sidebar.

**`rebuild_index(vault_path)`**
Reads all non-root pages from the database, groups them by category, sorts alphabetically by title, and fully rewrites `wiki/index.md` with a markdown table per category. Called automatically by `ingest_source` after every successful ingest. Also available via `llm-wiki index` CLI and `POST /api/vaults/{name}/index/rebuild`. Summaries are truncated to 120 characters and pipe characters escaped to prevent broken table rendering.

**Template functions** (`_index_template`, `_log_template`, `_schema_template`)
Generate the initial content for the three special wiki files. `_index_template` now uses a "No pages yet" placeholder rather than an empty table — `rebuild_index` overwrites it on first ingest. `schema.md` is the most important — it tells the LLM how to behave when ingesting into this vault.

---

## embeddings.py

**`compute_embedding(text, model) → list[float]`**
Calls `litellm.embedding` and returns a dense float vector. Raises `RuntimeError` if the
model is unavailable. Truncates input to 8192 chars. Patch target in tests:
`"core.embeddings.litellm.embedding"`.

---

## db/

The persistence layer. All reads/writes to `wiki.db` go through here. Import public symbols
from `core.db`; import private helpers directly from their sub-module
(e.g. `from core.db.pages import _infer_category`).

### Schema

```sql
pages (
    id, file_path UNIQUE, title, category, content,
    tags (JSON array), mtime, summary, backlinks (JSON array)
)
pages_fts     -- FTS5 virtual table, content=pages (triggers keep in sync)
page_vectors  -- vec0 virtual table; embedding float[768]; rowid = pages.id
ingest_queue (id, file_path, status, added_at, processed_at, error)
ingest_jobs (id TEXT PK, vault, source, status, created_at, started_at, finished_at,
             pages_written JSON array, error)
links (source_path TEXT, target_stem TEXT, PRIMARY KEY (source_path, target_stem))
```

`pages_fts` uses **porter ASCII tokenizer** and is kept in sync with `pages` via three triggers (INSERT, UPDATE, DELETE). Do not manually insert into `pages_fts`.

`page_vectors` is a `vec0` virtual table provided by the **sqlite-vec** extension. `get_db` loads the extension on every connection (`sqlite_vec.load(conn)`) before calling `_ensure_schema`. The rowid of `page_vectors` is the same as `pages.id`, joined as `JOIN pages p ON v.rowid = p.id`.

`links` stores one row per directed wikilink edge (`source_path → target_stem`). `upsert_page` syncs it atomically — deletes all outgoing rows for the page then re-inserts current links. `delete_page` purges link rows before removing the page. This table is the source of truth for backlink computation; `_rebuild_backlinks_full` and `_rebuild_backlinks_incremental` read from it instead of scanning page content.

### Key functions

| Function | What it does |
|---|---|
| `get_db(vault_path)` | Opens (and creates if needed) the SQLite connection with WAL mode enabled; loads sqlite-vec |
| `upsert_page(conn, wiki_root, md_path, embedding)` | Parses YAML frontmatter, infers category, extracts summary, upserts `pages`; stores embedding in `page_vectors` when provided |
| `reconcile(conn, wiki_root)` | Diffs filesystem vs DB by comparing `mtime`; adds/updates/removes pages; calls `_rebuild_backlinks_full` |
| `_rebuild_backlinks_full(conn)` | Reads the `links` table (no content scanning) and rewrites the `backlinks` JSON column for every page; logs a WARNING on title stem collisions (alphabetically first path wins) |
| `_rebuild_backlinks_incremental(conn, changed_paths)` | Recomputes backlinks only for pages in the changed set and their direct link neighbours — O(neighbourhood) instead of O(all pages) |
| `search(conn, query, limit)` | FTS5 `MATCH` with BM25 ranking (`ORDER BY rank`) |
| `compute_embedding(text, model)` | Calls `litellm.embedding` and returns a `list[float]`; raises `RuntimeError` if the model is unavailable |
| `vector_search(conn, query_embedding, limit)` | KNN search over `page_vectors` using vec0 cosine distance; returns empty list when no embeddings exist |
| `hybrid_search(conn, query, query_embedding, limit, rrf_k)` | Merges FTS5 + vector results with Reciprocal Rank Fusion; falls back to FTS5-only when `query_embedding` is `None` |
| `queue_raw_file(conn, file_path)` | Adds a file to `ingest_queue` with status `pending`; re-queues failed items by resetting their status |
| `get_pending_queue(conn)` | Returns all pending queue items in insertion order |
| `mark_queue_item(conn, path, status, error)` | Updates queue item status to `processing`, `done`, or `failed` |
| `create_job(conn, job_id, vault, source)` | Inserts a new `ingest_jobs` record with status `pending` |
| `update_job_status(conn, job_id, status, pages_written, error)` | Updates a job's status; sets `started_at` on running, `finished_at` on terminal states |
| `get_job(conn, job_id)` | Returns a job dict by UUID, or `None` if not found |
| `list_jobs(conn, limit)` | Returns up to `limit` jobs ordered newest-first |

### Category inference
`_infer_category(rel_path)` checks the first path segment: `Sources` → `Sources`, `Concepts` → `Concepts`, `Entities` → `Entities`, anything else → `root`.

### Wikilink regex
`\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]` — handles `[[Target]]` and `[[Target|Alias]]`, ignores section anchors (`#`).

---

## watcher.py

Thin watchdog wrapper. Monitors `<vault>/raw/` for new files and queues them.

**`VaultWatcher(vault_path, on_file=None)`**
- `start()` — schedules `_RawFolderHandler` on `raw/` (non-recursive)
- `stop()` — graceful observer shutdown
- `is_alive()` — check if observer thread is running

**`_RawFolderHandler`**
Handles `on_created` and `on_moved` events (covers both direct saves and downloads-then-moves). Ignores files with suffixes in `IGNORED_SUFFIXES` (`.db`, `.tmp`, `.part`, `.crdownload`) and dotfiles.

On detection: calls `queue_raw_file()` → then calls `on_file` callback if provided. In `main_server.py` the callback can trigger an immediate ingest; currently it just queues.

**Extension point:** pass an `on_file` callback to `VaultWatcher` to trigger auto-ingest without polling the queue. The callback receives the absolute file path string.

---

## extraction.py

Text extraction from local files and remote URLs. No LLM calls here.

**`_extract_text(source, char_limit) → tuple[str, str]`**
Dispatches to `_fetch_url`, `_extract_pdf`, `_extract_docx`, or plain `Path.read_text` based on the source string. Returns `(text, display_name)`. Raises `ValueError` for known binary formats (`.xlsx`, images, archives, etc.).

**`_fetch_url(url, char_limit) → tuple[str, str]`**
`requests.get` + `BeautifulSoup` — strips `script/style/nav/footer/aside`, returns `(plain_text, page_title)`. Patch target: `core.extraction.requests.get`.

**`_extract_pdf(path, char_limit) → str`**
`pypdf` (optional; logs a warning and returns `""` if not installed).

**`_extract_docx(path, char_limit) → str`**
`python-docx` (optional; logs a warning and returns `""` if not installed).

**`SOURCE_CHAR_LIMIT = 24_000`** — default character cap; all extractors default to this.

Extension point: add new format support in `_extract_text()` by matching on suffix or URL pattern.

---

## chunking.py

Chunking and map-reduce summarization for large documents. No direct DB or file I/O.

**`_chunk_text(text, chunk_size, overlap) → list[str]`**
Splits text into overlapping windows. Returns `[text]` unchanged when `len(text) <= chunk_size`. Tries to break at newlines in the last 200 characters of each window.

**`_summarize_chunks(chunks, model, vault_name, filename, context_chars) → str`**
Calls `litellm.completion` once per chunk (temperature 0.1) to extract bullet-point summaries, then concatenates. Truncates to `context_chars` when combined output is too large.
Patch target for tests: `core.chunking.litellm.completion`.

---

## prompts.py

Ingest prompt assembly and LLM JSON parsing. No LLM calls (parsing only); `_summarize_chunks` in `chunking.py` is the only caller that makes LLM calls from prompt-adjacent code.

**`_build_ingest_prompt(vault_name, schema, related, filename, text) → str`**
Assembles the primary ingest prompt. Extension point: edit this to change the wiki page format the LLM generates.

**`_build_ingest_prompt_strict(vault_name, schema, related, filename, text) → str`**
Retry variant — prepends an explicit "bare JSON object only" constraint before the standard prompt. Called by `ingest_source` when the first parse fails.

**`_parse_llm_json(raw) → dict[str, Any]`**
Strips markdown fences, extracts the outermost `{...}` block, tries `json.loads`, then `json_repair.repair_json` as fallback. Raises `ValueError` on unrecoverable input or when `source_page` is missing.

---

## ingest.py

Orchestration only. Delegates text extraction to `extraction.py`, chunking to `chunking.py`, and prompt/parsing to `prompts.py`.

### `ingest_source(vault_path, source, vault_name, dry_run=False)`

1. **Extract** text via `_extract_text()` (from `extraction.py`)
2. **Search** existing wiki for related pages (first 500 chars → FTS5 seed query)
3. **Load schema** from `wiki/schema.md`
4. **Chunk** large documents via `_chunk_text` + `_summarize_chunks` (from `chunking.py`)
5. **Prompt LLM** with source content + related pages + schema (prompt from `prompts.py`)
6. **Parse** JSON response via `_parse_llm_json()` (from `prompts.py`); retries with strict prompt on failure
7. **Write** pages to `wiki/` via `_write_pages()`
8. **Reconcile** DB via `partial_reconcile()`
9. **Store embeddings** via `_store_embeddings()`
10. **Append** to `log.md` via `_append_log()`
11. **Rebuild** `wiki/index.md` via `rebuild_index()` (skipped when `dry_run=True`)

### `ingest_queued(vault_path, vault_name)`
Processes all `pending` queue items from `ingest_queue`. Sets status to `processing` before calling `ingest_source`, then `done` or `failed`. Used by the watcher callback path.

### LLM JSON contract
The LLM is asked to return:
```json
{
  "source_page": { "file_path": "Sources/X.md", "content": "..." },
  "page_updates": [
    { "file_path": "Concepts/Y.md", "action": "create|update", "content": "..." }
  ]
}
```

---

## query.py

### `query_wiki(vault_path, question, save_as=None)`

1. FTS5 search with the raw question as query (top `CONTEXT_PAGES = 6` results)
2. Reads each result's full file content (truncated to `CONTEXT_CHARS_PER_PAGE = 2000`)
3. Sends assembled context + question to LiteLLM
4. Optionally saves the answer as a new page at `save_as` path (defaults to `Concepts/`)

The prompt instructs the LLM to cite which wiki pages it used and admit uncertainty rather than hallucinate.

**`save_as` format**: a relative path like `Concepts/My-Answer.md`. If no `/` is present, it's placed in `Concepts/` automatically.

---

## lint.py

### `lint_vault(vault_path) → dict`

Two-phase lint:

**Phase 1 — Structural (no LLM)**
- **Orphans**: pages with no backlinks AND no outgoing `[[wikilinks]]` (excludes `root` category pages like `index.md`)
- **Broken links**: `[[Target]]` references where `Target` doesn't match any page title
- **Missing summaries**: pages where `_extract_summary()` returned empty

**Phase 2 — LLM quality review**
Samples up to `CONTRADICTION_SAMPLE = 8` pages (weighted toward Sources + Concepts by summary length) and asks the LLM to report: contradictions, incomplete pages, missing links, suggestions.

Output is saved as `lint-YYYYMMDD-HHMM.md` in the **vault root** (not in `wiki/`) so Obsidian doesn't index it. A one-line summary is appended to `log.md`.

**Extension point**: increase `CONTRADICTION_SAMPLE` for larger vaults. Add a "stale page" check (compare `mtime` against recent ingest timestamps).

---

## server.py

FastAPI application. Imported by `main_server.py` and passed to uvicorn.

Static files served from `app/static/` at `/static/`.  
Dashboard HTML served from `app/templates/index.html` at `/`.

### API surface

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/vaults` | List all vaults + default |
| GET | `/api/vaults/{name}/status` | Stats + effective model |
| POST | `/api/vaults/{name}/reconcile` | Trigger DB sync |
| GET | `/api/vaults/{name}/pages` | List pages (optional `?category=`) |
| GET | `/api/vaults/{name}/pages/content` | Full page content (`?file_path=`) |
| GET | `/api/vaults/{name}/search` | FTS5 search (`?q=&limit=`) |
| GET | `/api/vaults/{name}/graph` | Nodes + edges for force-directed graph |
| POST | `/api/vaults/{name}/ingest` | `{source, dry_run}` → 202 `{job_id, status}` |
| GET | `/api/vaults/{name}/jobs` | List 20 most recent ingest jobs |
| GET | `/api/vaults/{name}/jobs/{id}` | Get single job status |
| GET | `/api/vaults/{name}/jobs/{id}/stream` | SSE stream of job status until terminal |
| POST | `/api/vaults/{name}/query` | `{question, save_as}` |
| POST | `/api/vaults/{name}/lint` | Run lint pass |
| GET | `/api/vaults/{name}/log` | Raw `log.md` content |
| POST | `/api/vaults/{name}/index/rebuild` | Rebuild `wiki/index.md` from DB |

All LLM endpoints (`ingest`, `query`, `lint`) are declared as plain `def` (not `async def`). FastAPI automatically dispatches `def` endpoints to anyio's thread pool, so the event loop stays free while the LLM blocks (30–120 s on a local 7B model). Do **not** change them to `async def`.

---

## mcp_server.py

An MCP (Model Context Protocol) server that exposes the wiki as a set of tools for AI agents.

**Exposed tools:**

| Tool | Description |
|------|-------------|
| `search_wiki` | BM25 search across all pages |
| `view_page` | Read a specific page by relative path |
| `list_pages` | List all pages, optionally by category |
| `ingest` | Ingest a file or URL |
| `query` | Q&A grounded in wiki content |
| `lint` | Run full lint pass |
| `list_vaults` | Show all registered vaults |

**Running:**
```bash
python -m core.mcp_server [--vault VAULT_NAME]
```

The `--vault` flag sets the default vault for tools that accept an optional `vault` argument. All tools also accept an explicit `vault` parameter to override.

**Implementation notes:**
- Uses `stdio_server` transport (stdin/stdout) — the standard for Claude Code MCP integration
- All tool handlers are in `_dispatch()` — add new tools by registering in `list_tools()` and adding a branch in `_dispatch()`
- Error handling wraps each dispatch in try/except and sets `isError=True` on the result
