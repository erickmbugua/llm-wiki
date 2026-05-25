# Maintainability Evaluation — llm-wiki

**Date:** 2026-05-25
**Evaluator:** Principal Engineer review
**Scope:** `core/`, `main.py`, `main_server.py` — 3,956 total lines across 12 files

---

## Executive Summary

The codebase is well above average for a solo/small-team project: it has good docstrings,
a working test suite (247 tests), and real thought put into edge cases (JSON repair, path
traversal guards, FTS5 sanitisation). The primary maintainability risk is **module cohesion**.
The two largest files, `database.py` and `ingest.py`, each stitch together three to four
distinct responsibilities. A future engineer making a focused change — say, swapping the
embedding model provider or adding a new file format — would need to trace through 800-line
files and mentally partition concerns that could instead be expressed as distinct modules.
The global-state pattern is a secondary risk: mutable process-level caches in three separate
files create hidden coupling and make test isolation fragile.

---

## Finding 1 — `database.py` does too much (SRP violation, high severity)

`database.py` (792 lines) currently owns:

| Concern | Functions |
|---|---|
| Connection & schema | `get_db`, `_ensure_schema` |
| Page CRUD | `upsert_page`, `delete_page`, `get_page`, `list_pages` |
| Lexical search | `search` |
| Semantic search | `vector_search`, `hybrid_search` |
| **ML inference** | `compute_embedding` |
| Reconciliation | `reconcile`, `partial_reconcile` |
| Backlink graph | `_rebuild_backlinks_full`, `_rebuild_backlinks_incremental` |
| Ingest queue | `queue_raw_file`, `get_pending_queue`, `mark_queue_item` |
| Job tracking | `create_job`, `update_job_status`, `get_job`, `list_jobs` |

`compute_embedding` is the sharpest violation. It calls `litellm` — an LLM provider — from
inside the database layer. An engineer reading `database.py` to understand how records are
stored would not expect to find model inference logic sitting between `vector_search` and the
ingest queue. When the embedding provider changes (e.g. moving from Ollama to OpenAI's
text-embedding model), the change happens in the database file, which is the wrong mental model.

**Suggested split:**

```
core/
  db/
    connection.py     # get_db, _ensure_schema
    pages.py          # upsert_page, delete_page, get_page, list_pages, _infer_category, _extract_summary
    search.py         # search, vector_search, hybrid_search
    reconcile.py      # reconcile, partial_reconcile, _rebuild_backlinks_*
    queue.py          # queue_raw_file, get_pending_queue, mark_queue_item
    jobs.py           # create_job, update_job_status, get_job, list_jobs
  embeddings.py       # compute_embedding (standalone, imported by search and ingest)
```

This split means each file has a single reason to change. A breaking change in the SQLite
schema affects `connection.py` and `pages.py`. A new search algorithm affects `search.py`.
A new embedding provider touches only `embeddings.py`.

---

## Finding 2 — `ingest.py` is a pipeline masquerading as a module (SRP, high severity)

`ingest.py` (804 lines) owns the entire ingest pipeline end-to-end:

| Stage | Functions |
|---|---|
| Text extraction | `_extract_text`, `_fetch_url`, `_extract_pdf`, `_extract_docx` |
| Chunking | `_chunk_text`, `_summarize_chunks` |
| Ollama preflight | `_check_ollama` |
| Prompt building | `_build_ingest_prompt`, `_build_ingest_prompt_strict` |
| JSON parsing | `_parse_llm_json` |
| Page writing | `_write_pages`, `_safe_wiki_path` |
| Embedding storage | `_store_embeddings` |
| Activity log | `_append_log` |
| Queue runner | `ingest_queued` |

The text extraction stage is particularly worth isolating: adding a new format (e.g. `.epub`,
`.html` download, `.csv`) today means editing `ingest.py` near LLM prompt logic. These have
nothing in common. The natural split:

```
core/
  extraction.py   # _extract_text, _fetch_url, _extract_pdf, _extract_docx, _BINARY_SUFFIXES
  chunking.py     # _chunk_text, _summarize_chunks
  prompts.py      # all _build_*_prompt functions + _parse_llm_json (prompt contract lives here)
  ingest.py       # ingest_source, ingest_queued, _store_embeddings, _write_pages, _append_log
```

Adding an `.epub` extractor becomes: open `extraction.py`, add one handler, done. No risk of
accidentally breaking JSON parsing or prompt logic.

---

## Finding 3 — Three separate caches, one conceptual concern (global state, medium severity)

The process-level config cache is implemented in three places:

- `config.py` — `_global_cfg_cache: GlobalConfig | None`, `_vault_cfg_cache: dict[str, VaultConfig]`, two `_clear_*` helpers
- `server.py` — `_config_cache: GlobalConfig | None`, a second `_get_config()` / `_reset_config_cache()` pair
- `ingest.py` — `_ollama_verified: set[str]`

The server's `_config_cache` and `config.py`'s `_global_cfg_cache` both cache `GlobalConfig`.
They're separate objects; a mutation through one path doesn't reliably invalidate the other
unless the caller happens to call `_reset_config_cache()`. This is a latent correctness bug.

**Recommendation:** Consolidate to a single cache in `config.py`. `server.py` should call
`GlobalConfig.load()` directly — the caching is already there. Delete `_config_cache` and
the private helpers in `server.py`. The `_ollama_verified` set in `ingest.py` is fine as a
process-lifetime optimisation, but should be a module-level constant with a clear comment,
not a bare set literal.

---

## Finding 4 — `compute_embedding` lives in the wrong module (type leakage, medium severity)

The function typed as `conn: object` in `_store_embeddings` (`ingest.py:309`) exists
specifically because `compute_embedding` is imported from `database.py`, which creates a
circular-dependency risk if `ingest.py` were ever imported by `database.py`. The
`isinstance(conn, _sqlite3.Connection)` guard inside `_store_embeddings` is a code smell
that signals the caller and callee are fighting over where this code belongs. Moving
`compute_embedding` to a standalone `embeddings.py` (as suggested in Finding 1) resolves
this: `_store_embeddings` takes a properly-typed `sqlite3.Connection` and no guard is needed.

---

## Finding 5 — Hard-coded category list duplicated across three modules (DRY, low-medium severity)

The canonical category list `["Sources", "Concepts", "Entities"]` appears independently in:

- `vault.py:10` — `WIKI_SUBDIRS = ["Sources", "Concepts", "Entities"]`
- `database.py:767` — `if len(parts) > 1 and parts[0] in ("Sources", "Concepts", "Entities")`
- `lint.py:136` — `[p for p in pages if p["category"] in ("Sources", "Concepts")]`

Adding a new top-level category (e.g. `Projects/`) requires changes in all three files.
Declare it once:

```python
# core/constants.py
WIKI_CATEGORIES: frozenset[str] = frozenset({"Sources", "Concepts", "Entities"})
```

All three sites import from there. The change surface for a new category becomes one line.

---

## Finding 6 — Connection management is boilerplate-heavy without a context manager (ergonomics, low-medium severity)

Every endpoint in `server.py` and every DB-touching helper uses the same pattern:

```python
conn = get_db(vpath)
try:
    result = do_something(conn)
finally:
    conn.close()
```

This appears 10+ times. If a developer forgets the `try/finally`, connections leak. A simple
context manager eliminates the risk and the noise:

```python
# core/db/connection.py
from contextlib import contextmanager

@contextmanager
def db_connection(vault_path: Path):
    conn = get_db(vault_path)
    try:
        yield conn
    finally:
        conn.close()
```

Usage becomes:

```python
with db_connection(vpath) as conn:
    result = do_something(conn)
```

This also makes the SSE endpoint cleaner — `api_stream_job` currently calls `get_db` inside
its `while True` loop on every 1-second tick, opening and closing a connection per poll cycle.

---

## Finding 7 — `api_ingest` creates an untracked executor (resource leak, low severity)

In `server.py:333`:

```python
executor = _get_executor(vname) or ThreadPoolExecutor(max_workers=1)
```

When `_get_executor` returns `None` (tests, direct uvicorn, any startup path that skips
`main_server.py`), a brand-new `ThreadPoolExecutor` is created and submitted to — but never
stored and never shut down. The executor object is GC'd, but the worker thread running the
submitted job stays alive until the job finishes. In tests with fast teardown this can produce
unexpected background activity.

**Fix:** Either register a fallback executor per vault in `_get_executor` on first miss, or
raise a clear `RuntimeError` when called outside the expected startup path. Making the
behaviour explicit is better than silent resource creation.

---

## Finding 8 — Prompts are embedded in business logic (changeability, low severity)

LLM prompts in `ingest.py`, `query.py`, and `lint.py` are inline `textwrap.dedent(f"""...""")`
strings interleaved with the calling code. When the product team wants to tune a prompt, they
must navigate a pipeline file, locate the right string, and be careful not to touch adjacent
logic. This friction grows as the project adds more LLM operations.

Extracting prompts into `prompts.py` (or per-domain `ingest_prompts.py`) co-locates all the
text that changes together and makes A/B testing or prompt versioning trivially addable without
touching pipeline logic. The `_build_ingest_prompt_strict` / `_build_ingest_prompt` relationship
(one wraps the other with a preamble) is a good hint that these belong together as a tested unit.

---

## Finding 9 — `query.py` uses full `reconcile` where `partial_reconcile` applies (performance, low severity)

In `query_wiki` (`query.py:59-63`), after saving an answer page, a full
`reconcile(conn, wiki_root)` is called — this scans every `.md` file on disk and does mtime
comparisons for all of them. The `ingest.py` code correctly uses `partial_reconcile` for the
same scenario (one new file written). This is inconsistent and will slow down as the vault grows.

**Fix** (one line): replace `reconcile` with `partial_reconcile(conn, wiki_root, [wiki_root / saved_to])`.

---

## Finding 10 — No `__all__` declarations (API surface ambiguity, low severity)

None of the `core/` modules declare `__all__`. Every private helper (`_extract_summary`,
`_infer_category`, `_safe_wiki_path`, etc.) is therefore implicitly re-exportable. Adding
`__all__` to each module documents the intended public contract, which is especially valuable
when splitting a large module (Findings 1 & 2) because the `__all__` in each new file makes
the boundary explicit.

---

## Priority Order for Refactoring

| Priority | Finding | Effort | Payoff |
|---|---|---|---|
| 1 | Split `database.py` into `db/` sub-package | Medium | High — most code touches the DB |
| 2 | Split `ingest.py` — extract `extraction.py` + `prompts.py` | Medium | High — most new features are extraction or prompt changes |
| 3 | Merge config caches (eliminate `server.py`'s duplicate) | Low | Medium — latent correctness risk |
| 4 | Move `compute_embedding` to `embeddings.py` | Low | Medium — removes cross-layer dependency |
| 5 | Extract `WIKI_CATEGORIES` constant | Low | Low — pays compounding DRY dividend |
| 6 | Add `db_connection` context manager | Low | Low — reduces boilerplate per endpoint |
| 7 | Fix `partial_reconcile` in `query.py` | Very low | Immediate perf correctness |
| 8 | Fix executor leak in `api_ingest` | Low | Correctness in test environments |

---

## What Is Already Done Well

- **Docstrings are thorough and accurate.** Every public function has Args/Returns/Raises
  sections with real detail. This is rare and makes the code genuinely navigable for new
  contributors.

- **Type annotations are complete and enforced.** Running both mypy and pyright, and having a
  `pyrightconfig.json` committed to the repo, is a disciplined choice that catches an entire
  class of bugs before they reach production.

- **The backlink model is architecturally sound.** Storing wikilinks in the `links` table
  rather than re-scanning content at query time is correct. The two-tier rebuild strategy
  (`_rebuild_backlinks_full` vs `_rebuild_backlinks_incremental`) is a clear optimisation with
  a documented invariant.

- **Security is addressed at the right layer.** Path traversal guard in `_safe_wiki_path`,
  FTS5 sanitisation in `search`, the HTTP 202 for async ingest, and the Ollama preflight check
  are all correct choices that show the author thought about failure modes.

- **`CLAUDE.md` is a genuine maintenance asset.** The Known Gotchas section covers litellm
  type quirks, FTS5 pitfalls, mock patching rules, and dataclass factory subtleties. Most
  projects don't have anything like it, and it will save hours per new contributor.

- **The test suite provides a real safety net.** 247 tests with a clear structure mirroring the
  module layout means the refactors recommended above can be executed incrementally with
  confidence — each file move can be validated before the next one starts.

---

## Conclusion

The codebase has a solid foundation. The risks are not about correctness — the logic is
generally right. They are about the cost of future changes: where do you go to add a new file
format, tune a prompt, or swap an embedding model? Right now the answer is "a large file that
also does other things." The refactors recommended here are mechanical (move code, fix imports,
add a constant) rather than architectural rewrites, and the test suite provides the safety net
to do them incrementally. Starting with Finding 1 (the `db/` sub-package split) and Finding 2
(extracting `extraction.py`) would reduce average file length from ~650 to ~200 lines and
immediately clarify the codebase's structure for the next engineer who opens it.
