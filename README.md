# llm-wiki

A multi-vault, LLM-powered personal wiki manager built around the [Karpathy llm-wiki paradigm](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).

The core idea: instead of discarding LLM answers into chat history, build a **persistent, compounding knowledge artifact** — a wiki that grows richer with every ingested source and query. The LLM handles all maintenance (summarizing, cross-linking, contradiction detection) that humans find tedious.

---

## Architecture

```
Human / Agent
     │
     ├── CLI (bin/llm-wiki → main.py)
     ├── Web Dashboard (FastAPI → app/)
     └── MCP Server (core/mcp_server.py)
              │
         Core Engine (core/)
              │
     ┌────────┴────────┐
  SQLite FTS5       Obsidian Vault
  (wiki.db)         (wiki/ + raw/)
```

### Three-layer vault structure

Every initialized vault has:

```
<vault-root>/
├── raw/              ← drop source files here (watched by VaultWatcher)
├── wiki/
│   ├── Sources/      ← one page per ingested source
│   ├── Concepts/     ← abstract ideas, technologies, themes
│   ├── Entities/     ← people, orgs, projects, products
│   ├── index.md      ← auto-updated page catalog
│   ├── log.md        ← append-only activity log
│   └── schema.md     ← agent instructions for this vault
└── .llm-wiki/
    ├── wiki.db       ← SQLite FTS5 index (gitignored)
    └── config.json   ← per-vault name, model, and context_chars overrides
```

### Global config

`~/.llm-wiki/config.json` tracks all registered vaults:

```json
{
  "vaults": { "AI-Agents": "/path/to/vault" },
  "default_vault": "AI-Agents",
  "model": "claude-sonnet-4-6",
  "server_port": 8000
}
```

---

## Tech Stack

| Layer | Technology | Why |
|-------|-----------|-----|
| CLI | Python + Click + Rich | Zero install friction, pretty output |
| LLM | LiteLLM | Swap any provider (Claude, GPT, Ollama) via one env var |
| Search | SQLite FTS5 (BM25) | No vector DB needed; fast, local, zero infra |
| File watching | watchdog | Cross-platform `raw/` monitor |
| API | FastAPI | Async, auto-docs at `/docs`, minimal boilerplate |
| Frontend | Vanilla JS + Canvas | No Node/npm; ES Modules only |
| Agent integration | MCP (python SDK) | Exposes wiki as tools for Claude Code, Cursor, etc. |

---

## Quickstart

```bash
# 1. Install dependencies (creates .venv automatically)
uv sync --extra dev

# 2. Activate the venv
source .venv/bin/activate

# 3. Initialize a vault
llm-wiki init ~/Obsidian-Vaults/AI-Agents --name AI-Agents

# 4. Set the LLM model (litellm model string)
llm-wiki set-model claude-sonnet-4-6

# 5. Add your API key — copy .env.example to .env and fill it in
cp .env.example .env
# then edit .env and set e.g. ANTHROPIC_API_KEY=sk-...

# 6. Ingest a source
llm-wiki ingest https://example.com/article

# 7. Query
llm-wiki query "What are the key ideas about X?"

# 8. Start the dashboard
llm-wiki serve   # → http://127.0.0.1:8000
```

---

## CLI Commands

Run any command with `--help` for full option details.

### Vault management

| Command | Description |
|---------|-------------|
| `llm-wiki init [PATH] [-n NAME]` | Initialize llm-wiki structure in PATH (default: `.`). Registers the vault globally. |
| `llm-wiki list` | List all registered vaults with their path, default flag, and effective model. |
| `llm-wiki status [-v VAULT]` | Show page counts, raw queue depth, and per-category stats for a vault. |
| `llm-wiki use VAULT_NAME` | Set the default vault (used when `-v` is omitted). |
| `llm-wiki unregister VAULT_NAME` | Remove a vault from the registry. Files on disk are left untouched. |

### Configuration

All config commands accept `-v VAULT` to apply the setting to a single vault instead of globally.
The three-level priority chain is: vault config > global config > built-in default.

| Command | Description |
|---------|-------------|
| `llm-wiki set-model MODEL [-v VAULT]` | Set the LiteLLM model string (e.g. `claude-sonnet-4-6`, `gpt-4o`, `ollama/llama3`). |
| `llm-wiki set-context CHARS [-v VAULT]` | Max source characters fed to the LLM per ingest. Defaults: 6 000 (3B–4B), **24 000** (7B), 48 000 (70B+/cloud). |
| `llm-wiki set-chunk-size CHARS [-v VAULT]` | Characters per chunk for large-document map-reduce summarization. Default: 20 000. |
| `llm-wiki set-chunk-overlap CHARS [-v VAULT]` | Overlap between adjacent chunks (preserves context at boundaries). Default: 500. |
| `llm-wiki set-embedding-model MODEL [-v VAULT]` | Embedding model for semantic (vector) search (e.g. `ollama/nomic-embed-text`). |

### LLM operations

| Command | Description |
|---------|-------------|
| `llm-wiki ingest SOURCE [-v VAULT] [--dry-run]` | Ingest a file path or URL. Extracts text → LLM generates wiki pages → writes to `wiki/` → updates index. `--dry-run` shows what would be written without touching disk. |
| `llm-wiki query QUESTION [-v VAULT] [--save-as PATH]` | Answer a question from wiki content via FTS5 context retrieval + LLM. `--save-as` persists the answer as a new wiki page. |
| `llm-wiki lint [-v VAULT]` | Run a full lint pass: orphan detection, broken wikilinks, missing summaries, and LLM contradiction review. Saves a report to the vault root. |
| `llm-wiki index [-v VAULT]` | Rebuild `wiki/index.md` from the current database state without a full reconcile. |
| `llm-wiki reconcile [-v VAULT]` | Re-sync the FTS5 search index with all wiki files on disk (full scan). |
| `llm-wiki serve [--host HOST] [-p PORT]` | Start the web dashboard and per-vault file watchers. Default: `http://127.0.0.1:8000`. Ctrl-C cleanly stops everything. |

---

## Directory Map

| Path | Purpose |
|------|---------|
| `main.py` | Click CLI group — all user-facing commands |
| `main_server.py` | Server startup: FastAPI + per-vault watchdog watchers |
| `pyproject.toml` | Declares the `llm-wiki` entry point (installed to `.venv/bin/llm-wiki` by `uv sync`) |
| `core/` | Shared Python library used by CLI, server, and MCP |
| `app/` | Web dashboard: HTML template, CSS, and JS |

---

## LLM Provider Configuration

LiteLLM routes based on the model string. Set the corresponding API key in `.env` (copy `.env.example` to get started):

| Model string | Env var needed |
|---|---|
| `claude-sonnet-4-6` | `ANTHROPIC_API_KEY` |
| `gpt-4o` | `OPENAI_API_KEY` |
| `openrouter/anthropic/claude-3-5-sonnet` | `OPENROUTER_API_KEY` |
| `gemini/gemini-pro` | `GEMINI_API_KEY` |
| `ollama/llama3` | (none — needs Ollama running locally) |

Keys are loaded automatically from `.env` at startup — no `export` needed.

Override globally: `llm-wiki set-model <model>`  
Override per-vault: `llm-wiki set-model <model> --vault MyVault`

---

## MCP Integration (Claude Code / Cursor)

Add to your MCP settings:

```json
{
  "mcpServers": {
    "llm-wiki": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "core.mcp_server"],
      "cwd": "/path/to/llm-wiki"
    }
  }
}
```

API keys are loaded from `.env` in the project root. If you need to pass them explicitly (e.g. the MCP host doesn't inherit your shell env), add an `"env"` block: `{ "ANTHROPIC_API_KEY": "sk-..." }`.

Available MCP tools: `search_wiki`, `view_page`, `list_pages`, `ingest`, `query`, `lint`, `list_vaults`.

---

## Running the Tests

```bash
# Install dev dependencies first
uv sync --extra dev

# Full suite (unit + integration + e2e, 288 tests)
.venv/bin/pytest tests/ -q

# Unit tests only (fast, no external processes)
.venv/bin/pytest -m "not integration" -q

# Integration tests only (exercises real pipelines, LLM is stubbed — no Ollama needed)
.venv/bin/pytest -m integration -q

# All QA tools in the required order
.venv/bin/ruff check --fix . && .venv/bin/ruff format . && .venv/bin/mypy && .venv/bin/pyright && .venv/bin/pytest tests/ -q
```

Integration tests cover four areas: the full ingest pipeline (extraction → DB → backlinks → log → index), the HTTP 202 job lifecycle (POST → poll → terminal state), the file-watcher pipeline (watchdog → queue → ingest), and the three-level config resolution chain. All use a stubbed LLM — no running model is required.

---

## Key Data Flows

**Ingest:** `raw/` file detected → `VaultWatcher` queues it → `ingest_source()` extracts text → LiteLLM generates wiki pages → pages written to `wiki/` → `partial_reconcile()` updates FTS5 index for changed files only → `log.md` appended.

**Query:** User question → `search()` FTS5 BM25 → top pages assembled as context → LiteLLM answers → optionally saved as new Concepts/ page.

**Lint:** `reconcile()` → structural checks (orphans, broken links) → LiteLLM contradiction review on sampled pages → lint report saved to vault root.

**Graph:** FastAPI `/api/vaults/{name}/graph` returns nodes + edges from backlinks column → Canvas force-directed simulation.
