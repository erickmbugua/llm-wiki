# llm-wiki — Claude Working Guide

## Project Overview

Implementation of Andrej Karpathy's llm-wiki paradigm: a persistent, compounding personal
knowledge base where an LLM maintains wiki pages, cross-references, and contradiction flags.

**Stack:** Python 3.10+, FastAPI, SQLite FTS5, litellm (provider-agnostic LLM), MCP server,
Vanilla JS dashboard, watchdog file watcher.

**Key concepts:**
- Multi-vault support — each vault is a self-contained directory with `raw/`, `wiki/`, `.llm-wiki/`
- Global config at `~/.llm-wiki/config.json`; per-vault config at `<vault>/.llm-wiki/config.json`
- SQLite FTS5 with porter ASCII tokenizer, BM25 ranking, content-table triggers
- MCP server exposes wiki operations as tools for Claude Code / Cursor
- **Auto-ingest**: dropping a file into `raw/` triggers automatic ingest via watchdog → per-vault
  `ThreadPoolExecutor(max_workers=1)`. Serial execution prevents memory oversubscription on local
  7B models. The executor is started in `main_server.py`; the watcher in `core/watcher.py` is
  responsible only for detecting files and queuing them.

---

## Workflow for New Features

**Follow this sequence strictly. Do not skip steps or proceed without explicit approval.**

### 1. Requirements gathering
Before writing any code, ask the user targeted questions to fully understand the requirement.
If the feature touches external APIs, libraries, or architectural patterns — do research first
(`WebSearch`, `WebFetch`) and bring findings back before asking questions.

### 2. Present options with trade-offs
Lay out 2–4 concrete implementation approaches. For each, state:
- What it does
- Key trade-off (complexity, performance, maintainability)
- Your recommendation and why

**Explicitly wait for the user to select an approach before proceeding.**

### 3. Write an implementation plan
Once an approach is chosen, produce a step-by-step plan covering:
- Files to create or modify
- New functions/classes and their signatures
- Test cases to write (TDD: tests first)
- Any migrations or schema changes

**Present the plan and explicitly wait for approval before writing any code.**

### 4. Code — in TDD order
1. Write failing tests that specify the behaviour
2. Write the implementation to make them pass
3. Refactor if needed, keeping tests green

---

## Code Quality Standards

### Documentation
Every code change must update all affected documentation in the same commit — not as a follow-up.
This includes:
- **`CLAUDE.md`** — update Known Gotchas, Project Structure, Toolchain, or Key Concepts when
  behaviour, architecture, or test counts change
- **Folder `README.md` files** — update module maps, API tables, CLI command tables, config field
  lists, and data-flow descriptions to match the new code
- **Docstrings** — every public function and class must have a docstring with:
  - One-line description of what it does
  - `Args:` section for non-obvious parameters
  - `Returns:` section describing the return value and shape
  - `Raises:` section for exceptions callers should handle

Private helpers need at minimum a one-line docstring if their purpose is not immediately obvious
from the name and signature.

### Testing
- Tests live in `tests/` and mirror the module structure (`test_database.py` → `core/database.py`)
- Use `pytest` fixtures; shared fixtures live in `tests/conftest.py`
- Every new function needs at least one happy-path test and one edge/error test
- `get_page()` and similar nullable-return functions: always assert `is not None` before subscripting
- Mock at the boundary (`core.server.ingest_source`, not `core.ingest.ingest_source`) so patches
  match where the name is actually used

### Type annotations
- All function signatures must have full type annotations
- Never use bare `dict` or `list[dict]` — always parameterise: `dict[str, Any]`, `list[dict[str, Any]]`
- Add `from typing import Any` to any module that needs it
- Run both `mypy` and `pyright` — they catch different things
- Use `# pyright: ignore[<rule>]` for pyright-only suppressions (mypy ignores these comments)
- Use `# type: ignore[<code>]` for mypy-only suppressions
- Never use a bare `# type: ignore` without a specific error code
- After a truthiness guard (`if not x: raise ...`), pyright may still see `Unknown` in the union —
  use `x = str(x)` (or the appropriate cast) to explicitly narrow the type

### Third-party library types
When adding a new dependency, determine which category it falls into and apply the fix:

| Situation | Fix |
|---|---|
| Popular library (requests, boto3, etc.) | `pip install types-<name>` — add to `dev` deps |
| Ships its own types (pypdf 3+, pydantic, fastapi) | Just install it — pyright picks up inline types |
| Optional dep never installed in dev | Add to `[project.optional-dependencies] dev` anyway |
| No stubs at all (litellm, mcp, frontmatter) | `# pyright: ignore[reportAttributeAccessIssue]` at call sites |

Check whether stubs exist before writing any suppressions:
```bash
pip index versions types-<packagename>
```

Current dev stubs installed: `types-requests`, `types-beautifulsoup4`, `pypdf` (inline types), `python-docx` (inline types — suppress attribute access with `# pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]`).

---

## Toolchain

All tools run from the project venv: `.venv/bin/<tool>`

| Tool | Purpose | Command |
|------|---------|---------|
| `ruff check --fix` | Lint + auto-fix | `.venv/bin/ruff check --fix .` |
| `ruff format` | Formatting | `.venv/bin/ruff format .` |
| `mypy` | Static type checking | `.venv/bin/mypy` |
| `pyright` | Pylance-compatible type checking | `.venv/bin/pyright` |
| `pytest` | Test suite (246 tests) | `.venv/bin/pytest tests/ -q` |

**Before declaring any task complete, all five commands must exit cleanly with zero errors.**
Run them in this order: `ruff check --fix` → `ruff format` → `mypy` → `pyright` → `pytest`.

Config lives in `pyproject.toml` (`[tool.ruff]`, `[tool.mypy]`) and `pyrightconfig.json`.

---

## Known Gotchas

### litellm response type
`litellm.completion()` returns `ModelResponse | CustomStreamWrapper`. Pyright flags `.choices`
access on the union. Since we never stream, suppress with:
```python
response.choices[0].message.content  # pyright: ignore[reportAttributeAccessIssue]
```
`.content` can also be `None` — always use `or ""` before calling `.strip()`.

### JSON parsing resilience in ingest
`_parse_llm_json` uses a two-step parse strategy: fast-path `json.loads`, then
`json_repair.repair_json` as a fallback for near-valid LLM output (trailing commas,
single quotes, missing closing braces, prose wrapping). If repair fails, a second LLM call
is made via `_build_ingest_prompt_strict`, which prepends a JSON-only constraint.
The ingest LLM call uses `temperature=0.0` — structured JSON output benefits from
determinism; `temperature=0.3` is kept for `query_wiki` and `temperature=0.2` for `lint_vault`.

### Ollama local model setup
The default model is `ollama/qwen2.5-coder:7b`. Before running any ingest, Ollama must be
running and the model must be pulled:
```bash
ollama serve          # start the server (keep this running)
ollama pull qwen2.5-coder:7b   # one-time pull (~4 GB)
```
`ingest_source()` runs a preflight check for any `ollama/*` model string — it hits
`GET /api/tags` and raises a clear `RuntimeError` if the server is unreachable or the model
is absent, rather than letting litellm's `ConnectionError` bubble up raw.

To use a non-default Ollama host/port, set the env var before starting the server:
```bash
export OLLAMA_API_BASE=http://192.168.1.10:11434
```
To switch to a cloud model for a specific vault, set `model` in
`<vault>/.llm-wiki/config.json`. To change the global default, set `model` in
`~/.llm-wiki/config.json`. Any litellm-compatible model string works (e.g.
`"claude-sonnet-4-6"`, `"openai/gpt-4o"`, `"ollama/llama3:8b"`).

### FTS5 input sanitisation
User queries can contain FTS5 special characters. Always sanitise before querying:
```python
clean = re.sub(r"[^\w\s]", " ", query).strip()
if not clean:
    return []
```

### frontmatter tags type
`python-frontmatter`'s `Post.get()` returns `object`. Use:
```python
list(post.get("tags") or [])  # type: ignore[call-overload]
```

### Local imports in CLI only
`main.py` CLI commands use local imports intentionally (startup speed). All other modules
(`core/`, `main_server.py`) must use module-level imports — required for testability and for
mock patches to work correctly.

### Vault path resolution in tests
The `client` fixture in `test_server.py` patches `core.server.GlobalConfig.load`, not
`core.config.GlobalConfig.load`. Patch at the point of use, not the point of definition.

### pyrightconfig.json
Must exist at the project root pointing at the venv, otherwise Pylance shows spurious
`reportMissingImports` errors for every third-party package:
```json
{
  "pythonVersion": "3.10",
  "venvPath": ".",
  "venv": ".venv",
  "reportMissingModuleSource": "none"
}
```
`reportMissingModuleSource = "none"` silences the secondary noise from libraries that ship
type stubs but not source (common with `types-*` packages).

### Bare `dict` in dataclass `field()`
`field(default_factory=dict)` makes pyright infer `dict[Unknown, Unknown]`, ignoring the
annotation. Use a lambda instead — pyright then defers to the annotation:
```python
vaults: dict[str, str] = field(default_factory=lambda: {})  # not default_factory=dict
```

### `Unknown` not narrowed by truthiness guards
After `if not raw: raise ...`, pyright still sees `str | Unknown | None`. Cast explicitly:
```python
raw = response.choices[0].message.content  # pyright: ignore[reportAttributeAccessIssue]
if not raw:
    raise ValueError("empty response")
raw = str(raw)  # narrows Unknown out of the union
```

### Incremental backlinks — `links` table
Backlink data is derived from the `links` table (one row per directed `[[wikilink]]` edge),
not by scanning `pages.content` at query time. `upsert_page` syncs the links table
atomically: it deletes all outgoing rows for the page then re-inserts current links.
`delete_page` purges link rows before removing the page.

`reconcile` calls `_rebuild_backlinks_full` (reads entire `links` table — O(pages) SQL, no
regex). `partial_reconcile` calls `_rebuild_backlinks_incremental` which recomputes backlinks
only for the changed pages and their direct link neighbours, keeping per-ingest work
proportional to the size of the change rather than the vault.

Wikilink collision (two pages with the same stem, e.g. `Concepts/Python.md` and
`Entities/Python.md`) is detected in `_rebuild_backlinks_full` when building the
`title_to_path` dict. The alphabetically first path wins; a WARNING is logged. The `links`
table stores raw `target_stem` values, so renaming one of the colliding pages automatically
resolves the collision on the next `reconcile` without any data migration.

### FastAPI endpoint threading — `def` vs `async def`
`api_ingest`, `api_query`, and `api_lint` in `core/server.py` are declared as plain `def`,
**not** `async def`. This is intentional. FastAPI automatically runs `def` endpoints in
anyio's thread pool, keeping the event loop free while `litellm.completion` blocks (30–120 s
on a local 7B model). Changing them back to `async def` would freeze the entire server
during every LLM call. All other endpoints that only do fast I/O stay `async def`.

### `context_chars` config — model-tier sizing
`context_chars` controls how many characters of source text are fed to the LLM per ingest.
The default is `24_000` (suitable for 7B models). Override per-vault with `llm-wiki set-context`:
```
3B-4B models  : 6_000
7B models     : 24_000  (default)
70B+ or cloud : 48_000
```
`resolve_context_chars(vault_path)` mirrors `resolve_model` with the same priority chain:
vault-level `VaultConfig.context_chars` > global `GlobalConfig.context_chars` > 24_000.

### `chunk_size` / `chunk_overlap` — large-document map-reduce
When a source's extracted text exceeds `chunk_size` characters, `ingest_source` runs a
map-reduce summarization pass before the main ingest prompt:
1. `_chunk_text` splits the text into overlapping windows of `chunk_size` chars (default `20_000`)
   with `chunk_overlap` chars (default `500`) shared between adjacent chunks.
2. `_summarize_chunks` calls the LLM once per chunk to extract key bullet points.
3. The concatenated summaries (capped at `context_chars`) feed the final ingest prompt.

Override per-vault with `llm-wiki set-chunk-size` / `llm-wiki set-chunk-overlap`.
`resolve_chunk_config(vault_path) -> tuple[int, int]` uses the same three-level priority chain.

### `POST /api/vaults/{vault}/ingest` — now returns HTTP 202 (breaking change from 200)
The ingest endpoint is **non-blocking**. It creates an `ingest_jobs` DB record, submits the
work to the vault's background executor, and immediately returns `{"job_id": "<uuid>", "status": "pending"}`.
Poll with `GET /api/vaults/{vault}/jobs/{job_id}` or subscribe to the SSE stream at
`GET /api/vaults/{vault}/jobs/{job_id}/stream` (emits the full job JSON every second until terminal).

The executor registry (`_vault_executors` in `core/server.py`) is populated by `main_server.py`
at startup via `register_vault_executor()`. When the server is started without `main_server.py`
(e.g. direct uvicorn or tests), a one-shot `ThreadPoolExecutor` is created on the fly.

### db/ sub-package — import paths
All database symbols are imported from `core.db` (or its sub-modules for private helpers):
```python
from core.db import get_db, search, reconcile   # public API
from core.db.pages import _infer_category       # private helpers — import from sub-module directly
from core.db.reconcile import _rebuild_backlinks_full
```
Embedding computation is in `core.embeddings`, not `core.db`:
```python
from core.embeddings import compute_embedding
```
When patching in tests, target the sub-module where the name is defined:
```python
patch("core.embeddings.litellm.embedding", ...)   # not core.db.litellm.embedding
```
When using `caplog` to capture warnings from `_rebuild_backlinks_full`, the logger is
`"core.db.reconcile"` (the module's `__name__`), not `"core.database"`.

### sqlite-vec extension loading
`get_db` loads the `sqlite-vec` extension on every connection:
```python
conn.enable_load_extension(True)
sqlite_vec.load(conn)
conn.enable_load_extension(False)
```
This must happen **before** `_ensure_schema` because `CREATE VIRTUAL TABLE … USING vec0(…)` is a
vec0 virtual table. Any connection that skips this step will fail with
`"no such module: vec0"`. Tests use `get_db` so they pick it up automatically.

### Embedding model setup
`compute_embedding` calls `litellm.embedding`. The default model is `ollama/nomic-embed-text`.
Pull it before ingest:
```bash
ollama pull nomic-embed-text
```
If the embedding call fails (model not running, dimension mismatch), `ingest_source` and
`_build_context` both catch the exception and continue without embeddings — FTS5-only search
acts as the graceful fallback. Override the embedding model per-vault with
`llm-wiki set-embedding-model`. `resolve_embedding_config(vault_path) -> tuple[str, int]` uses
the same three-level priority chain as `resolve_model`.

### `compute_embedding` return type coercion
`litellm.embedding()` returns an opaque type that mypy treats as `Any`. To satisfy the
`list[float]` return type annotation, the result is coerced explicitly:
```python
return [float(v) for v in result]
```
This also guards against models that return numpy arrays or other sequences.

### Optional dependency type gaps (pypdf)
Optional imports inside `try/except ImportError` suppress the module-not-found error but
leave member access as `Unknown`. Suppress usage lines individually:
```python
import pypdf  # pyright: ignore[reportMissingImports]
reader = pypdf.PdfReader(str(path))  # pyright: ignore[reportUnknownMemberType]
pages: list[str] = [p.extract_text() or "" for p in reader.pages]  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
```

---

## Project Structure

```
llm-wiki/
├── core/
│   ├── config.py      # GlobalConfig, VaultConfig, resolve_model(), resolve_context_chars(), resolve_chunk_config(), resolve_embedding_config()
│   ├── constants.py   # WIKI_CATEGORIES — single source of truth for top-level wiki directory names
│   ├── embeddings.py  # compute_embedding() — litellm embedding call, returns list[float]
│   ├── extraction.py  # _extract_text(), _fetch_url(), _extract_pdf(), _extract_docx(), SOURCE_CHAR_LIMIT
│   ├── chunking.py    # _chunk_text(), _summarize_chunks() — map-reduce for large docs
│   ├── prompts.py     # _build_ingest_prompt(), _build_ingest_prompt_strict(), _parse_llm_json()
│   ├── db/            # SQLite persistence layer (split by concern)
│   │   ├── __init__.py    # Re-exports all public symbols; sub-module map in docstring
│   │   ├── connection.py  # get_db(), _ensure_schema() — connection lifecycle + schema DDL
│   │   ├── pages.py       # upsert_page(), delete_page(), get_page(), list_pages(), _infer_category(), _extract_summary()
│   │   ├── search.py      # search() FTS5, vector_search() KNN, hybrid_search() RRF
│   │   ├── reconcile.py   # reconcile(), partial_reconcile(), _rebuild_backlinks_full/incremental()
│   │   ├── queue.py       # queue_raw_file(), get_pending_queue(), mark_queue_item()
│   │   └── jobs.py        # create_job(), update_job_status(), get_job(), list_jobs()
│   ├── ingest.py      # Orchestration only: extract → related search → LLM call → parse → write → reconcile → log
│   ├── lint.py        # Structural checks + LLM contradiction review
│   ├── mcp_server.py  # MCP stdio server (7 tools for Claude Code / Cursor)
│   ├── query.py       # FTS5 context retrieval + LLM Q&A
│   ├── server.py      # FastAPI app (17 REST routes)
│   ├── vault.py       # init_vault(), vault_stats(), skeleton templates
│   └── watcher.py     # watchdog observer for raw/ directory
├── app/
│   ├── static/        # Vanilla JS + CSS dashboard (no Node/npm)
│   └── templates/     # index.html entry point
├── tests/             # pytest suite — mirrors core/ structure
├── main.py            # Click CLI (llm-wiki init | ingest | query | lint | …)
├── main_server.py     # Startup: uvicorn + one VaultWatcher per vault
├── pyproject.toml     # Build, deps, ruff config, mypy config, pytest config
└── pyrightconfig.json # Points Pylance/pyright at the venv
```

---

## Vault Structure (per vault)

```
<vault>/
├── raw/               # Drop source files here; watchdog queues them
├── wiki/
│   ├── Sources/       # One page per ingested source
│   ├── Concepts/      # Abstract ideas, technologies, themes
│   ├── Entities/      # People, organisations, projects
│   ├── index.md       # Rebuilt after every ingest by rebuild_index() in core/vault.py
│   ├── log.md         # Append-only activity log
│   └── schema.md      # Vault purpose and ingestion conventions
└── .llm-wiki/
    ├── wiki.db        # SQLite database (gitignored)
    └── config.json    # Per-vault name, model, and context_chars overrides
```
