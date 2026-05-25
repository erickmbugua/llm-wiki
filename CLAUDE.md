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
Every public function and class must have a docstring with:
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
| `pytest` | Test suite (145 tests) | `.venv/bin/pytest tests/ -q` |

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

### FastAPI endpoint threading — `def` vs `async def`
`api_ingest`, `api_query`, and `api_lint` in `core/server.py` are declared as plain `def`,
**not** `async def`. This is intentional. FastAPI automatically runs `def` endpoints in
anyio's thread pool, keeping the event loop free while `litellm.completion` blocks (30–120 s
on a local 7B model). Changing them back to `async def` would freeze the entire server
during every LLM call. All other endpoints that only do fast I/O stay `async def`.

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
│   ├── config.py      # GlobalConfig, VaultConfig, resolve_model()
│   ├── database.py    # SQLite FTS5 engine, CRUD, reconcile, backlinks, queue
│   ├── ingest.py      # Text extraction, LLM page generation, queue processing
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
│   ├── index.md       # Auto-maintained table of contents
│   ├── log.md         # Append-only activity log
│   └── schema.md      # Vault purpose and ingestion conventions
└── .llm-wiki/
    ├── wiki.db        # SQLite database (gitignored)
    └── config.json    # Per-vault name + model override
```
