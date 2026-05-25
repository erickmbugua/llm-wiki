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
# 1. Create and activate a virtualenv
python3 -m venv .venv && source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Initialize a vault
bin/llm-wiki init ~/Obsidian-Vaults/AI-Agents --name AI-Agents

# 4. Set the LLM model (litellm model string)
bin/llm-wiki set-model claude-sonnet-4-6
export ANTHROPIC_API_KEY=sk-...

# 5. Ingest a source
bin/llm-wiki ingest https://example.com/article

# 6. Query
bin/llm-wiki query "What are the key ideas about X?"

# 7. Start the dashboard
bin/llm-wiki serve   # → http://127.0.0.1:8000
```

---

## Directory Map

| Path | Purpose |
|------|---------|
| `main.py` | Click CLI group — all user-facing commands |
| `main_server.py` | Server startup: FastAPI + per-vault watchdog watchers |
| `bin/llm-wiki` | Executable wrapper that puts the project on `sys.path` |
| `requirements.txt` | All Python dependencies |
| `core/` | Shared Python library used by CLI, server, and MCP |
| `app/` | Web dashboard: HTML template, CSS, and JS |

---

## LLM Provider Configuration

LiteLLM routes based on the model string. Set the corresponding API key:

| Model string | Env var needed |
|---|---|
| `claude-sonnet-4-6` | `ANTHROPIC_API_KEY` |
| `gpt-4o` | `OPENAI_API_KEY` |
| `ollama/llama3` | (none — needs Ollama running) |
| `gemini/gemini-pro` | `GEMINI_API_KEY` |

Override globally: `bin/llm-wiki set-model <model>`  
Override per-vault: `bin/llm-wiki set-model <model> --vault MyVault`

---

## MCP Integration (Claude Code / Cursor)

Add to your MCP settings:

```json
{
  "mcpServers": {
    "llm-wiki": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "core.mcp_server"],
      "cwd": "/path/to/llm-wiki",
      "env": { "ANTHROPIC_API_KEY": "sk-..." }
    }
  }
}
```

Available MCP tools: `search_wiki`, `view_page`, `list_pages`, `ingest`, `query`, `lint`, `list_vaults`.

---

## Key Data Flows

**Ingest:** `raw/` file detected → `VaultWatcher` queues it → `ingest_source()` extracts text → LiteLLM generates wiki pages → pages written to `wiki/` → `partial_reconcile()` updates FTS5 index for changed files only → `log.md` appended.

**Query:** User question → `search()` FTS5 BM25 → top pages assembled as context → LiteLLM answers → optionally saved as new Concepts/ page.

**Lint:** `reconcile()` → structural checks (orphans, broken links) → LiteLLM contradiction review on sampled pages → lint report saved to vault root.

**Graph:** FastAPI `/api/vaults/{name}/graph` returns nodes + edges from backlinks column → Canvas force-directed simulation.
