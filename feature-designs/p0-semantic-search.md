# P0 — Hybrid Semantic Search (FTS5 + Vector Embeddings)

## Problem Statement

The current retrieval layer (`core/database.py:search`) uses SQLite FTS5 with a porter ASCII
tokenizer and BM25 ranking. This is purely lexical: a query for `"neural networks"` will not
match a page titled `"Deep Learning"` that never uses those exact words. Two pages discussing
the same concept with different terminology — common in a personal knowledge base that ingests
sources from diverse authors — will never be cross-linked in search results or query context.

The consequence is that `query_wiki` (`core/query.py`) retrieves wrong or empty context,
and the LLM answers questions with no grounding or misleading grounding. This directly defeats
the core value proposition of the tool.

On MacBook M1, `nomic-embed-text` via Ollama runs at ~30ms per embedding using the neural
engine and downloads at ~274MB. The cost to add semantic search on the target hardware is
near zero.

---

## Implementation Plan

### Strategy: hybrid retrieval with Reciprocal Rank Fusion

Keep FTS5 for lexical recall (fast, handles exact terms, model names, dates). Add a vector
index via `sqlite-vec` for semantic recall. At query time, retrieve the top-K candidates from
each, merge with Reciprocal Rank Fusion (RRF), and return the fused top-N to the LLM.

`sqlite-vec` stores vectors directly in the SQLite file. No new process, no extra server, no
Chroma or Pinecone. The vault stays self-contained.

---

### Step 1 — Add dependencies

**File:** `pyproject.toml`

```toml
dependencies = [
    ...
    "sqlite-vec>=0.1",   # vector index inside SQLite
]
```

`sqlite-vec` ships as a Python wheel that bundles the SQLite extension. No system-level
install required.

---

### Step 2 — Add embedding config

**File:** `core/config.py`

Add to `GlobalConfig` and `VaultConfig`:

```python
embedding_model: str = "ollama/nomic-embed-text"   # litellm embedding model string
embedding_dim: int = 768                            # nomic-embed-text output dimension
```

Add `resolve_embedding_config(vault_path) -> tuple[str, int]` following the same three-level
priority chain as `resolve_model`.

Add a CLI setter:
```
llm-wiki set-embedding-model <model> [--vault VAULT]
```

Also add an Ollama preflight check for the embedding model (same pattern as `_check_ollama`
in `ingest.py`) since it is a separate model that must be pulled independently.

---

### Step 3 — Extend the database schema

**File:** `core/database.py:_ensure_schema`

Add a `vec_items` virtual table and a migration guard:

```sql
-- load the sqlite-vec extension once per connection (in get_db)
-- conn.enable_load_extension(True)
-- sqlite_vec.load(conn)

CREATE VIRTUAL TABLE IF NOT EXISTS page_vectors USING vec0(
    page_id INTEGER PRIMARY KEY,
    embedding FLOAT[768]   -- dimension matches embedding_model
);
```

Update `get_db` to load the `sqlite-vec` extension on every new connection:

```python
import sqlite_vec

def get_db(vault_path: Path) -> sqlite3.Connection:
    ...
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    ...
```

Add schema version tracking (see `p2-schema-migrations.md`) to handle the new table on
existing vaults.

---

### Step 4 — Implement the embedding function

**File:** `core/database.py` — new module-level function

```python
def compute_embedding(text: str, model: str) -> list[float]:
    """Compute a dense embedding vector for text using the given litellm model.

    Args:
        text: Text to embed. Will be truncated to 8192 chars if necessary.
        model: litellm embedding model string (e.g. "ollama/nomic-embed-text").

    Returns:
        A list of floats representing the embedding vector.

    Raises:
        RuntimeError: The embedding model is unavailable or returns an unexpected shape.
    """
```

Implementation: call `litellm.embedding(model=model, input=[text[:8192]])` and return
`response.data[0].embedding`. Wrap in a try/except and raise a `RuntimeError` with a clear
message if it fails — embedding failures should not silently drop pages.

---

### Step 5 — Store embeddings on page write

**File:** `core/database.py:upsert_page`

Add an optional `embedding: list[float] | None = None` parameter.

When `embedding` is not None, upsert into `page_vectors`:

```python
if embedding is not None:
    conn.execute(
        "INSERT OR REPLACE INTO page_vectors(page_id, embedding) VALUES (?, ?)",
        (page_id, sqlite_vec.serialize_float32(embedding)),
    )
```

**File:** `core/ingest.py:ingest_source`

After `partial_reconcile`, compute embeddings for newly written pages:

```python
emb_model, emb_dim = resolve_embedding_config(vault_path)
if emb_model.startswith("ollama/"):
    _check_ollama(emb_model)
conn = get_db(vault_path)
try:
    for rel_path in written:
        page_path = wiki_root / rel_path
        text_for_embed = page_path.read_text()[:8192]
        embedding = compute_embedding(text_for_embed, model=emb_model)
        page = get_page(conn, rel_path)
        if page:
            upsert_page(conn, wiki_root, page_path, embedding=embedding)
finally:
    conn.close()
```

Note: embedding is computed after the LLM write, so it does not block the ingest prompt.
Embedding calls to Ollama are fast (~30ms each), so 5–10 pages per ingest adds ~150–300ms.

---

### Step 6 — Implement vector search

**File:** `core/database.py` — new function

```python
def vector_search(
    conn: sqlite3.Connection,
    query_embedding: list[float],
    limit: int = 10,
) -> list[dict[str, Any]]:
    """KNN search over page_vectors using cosine distance.

    Args:
        conn: Open database connection (must have sqlite-vec loaded).
        query_embedding: Dense vector for the query.
        limit: Maximum number of results.

    Returns:
        List of page dicts (same shape as search()) ordered by vector similarity,
        or an empty list if no embeddings exist yet.
    """
    rows = conn.execute(
        """
        SELECT p.file_path, p.title, p.category, p.summary, p.tags, p.backlinks,
               v.distance AS rank
        FROM page_vectors v
        JOIN pages p ON v.page_id = p.id
        WHERE v.embedding MATCH ?
          AND k = ?
        ORDER BY v.distance
        """,
        (sqlite_vec.serialize_float32(query_embedding), limit),
    ).fetchall()
    return [dict(r) for r in rows]
```

---

### Step 7 — Implement hybrid retrieval with RRF

**File:** `core/database.py` — new function

```python
def hybrid_search(
    conn: sqlite3.Connection,
    query: str,
    query_embedding: list[float] | None,
    limit: int = 10,
    rrf_k: int = 60,
) -> list[dict[str, Any]]:
    """Merge FTS5 and vector search results with Reciprocal Rank Fusion.

    Falls back to FTS5-only when query_embedding is None (e.g. embedding model not
    configured or embedding call failed).

    Args:
        conn: Open database connection.
        query: Raw text query for FTS5.
        query_embedding: Dense vector for semantic search, or None for lexical-only.
        limit: Final result count to return.
        rrf_k: RRF smoothing constant (60 is the standard default).

    Returns:
        List of page dicts ordered by fused relevance score.
    """
```

RRF score for each candidate: `sum(1 / (rrf_k + rank_i))` across all result lists it appears
in. Merge by `file_path`, sort descending by RRF score, return top `limit`.

---

### Step 8 — Wire hybrid retrieval into query and lint

**File:** `core/query.py:_build_context`

Replace the `search(conn, question, limit=CONTEXT_PAGES)` call:

```python
emb_model, _ = resolve_embedding_config(vault_path)
query_embedding: list[float] | None = None
try:
    query_embedding = compute_embedding(question, model=emb_model)
except Exception:
    log.warning("Embedding failed; falling back to lexical-only search")

results = hybrid_search(conn, question, query_embedding, limit=CONTEXT_PAGES)
```

**File:** `core/ingest.py:_fetch_related`

Same substitution for the related-pages lookup. This is particularly high value because the
related-pages context directly affects what the LLM produces during ingest.

---

### Step 9 — Add reconcile for existing vaults

**File:** `core/database.py:reconcile`

After the page upsert loop, add an embedding backfill pass for any page that has no entry in
`page_vectors`:

```python
# Backfill embeddings for pages added before vector search was enabled
unembedded = conn.execute(
    "SELECT p.id, p.file_path FROM pages p "
    "LEFT JOIN page_vectors v ON v.page_id = p.id WHERE v.page_id IS NULL"
).fetchall()
```

Pass the backfill list back to the caller as `{"added": ..., "updated": ..., "removed": ...,
"embeddings_backfilled": len(unembedded)}`. The actual embedding calls happen in the CLI
`reconcile` command, not in the DB layer, to keep the DB layer free of model dependencies.

Add a `llm-wiki reconcile --embed` flag that runs the backfill pass.

---

### Step 10 — Write tests

**File:** `tests/test_database.py`

- `test_compute_embedding_returns_correct_dim`: mock litellm.embedding → verify list length
- `test_upsert_page_stores_embedding`: write a page with embedding → verify page_vectors row
- `test_vector_search_returns_results`: insert two pages with embeddings → query nearest
- `test_hybrid_search_fuses_results`: verify RRF merging when FTS and vector sets differ
- `test_hybrid_search_fallback_lexical_only`: embedding=None → returns FTS results unchanged

**File:** `tests/test_query.py`

- `test_query_wiki_uses_hybrid_search`: mock compute_embedding and hybrid_search, verify called
- `test_query_wiki_falls_back_on_embedding_error`: embedding raises → still returns an answer

---

### Step 11 — Documentation

- `CLAUDE.md` — add `embedding_model` and `embedding_dim` to config fields; note that
  `nomic-embed-text` must be pulled separately; document RRF fallback behaviour
- `core/README.md` — update database.py and query.py module descriptions
- `CLAUDE.md` Known Gotchas — document that `enable_load_extension` must be called before
  `sqlite-vec` functions are available; note that this requires SQLite built with extension
  loading enabled (true by default on macOS)

---

### Estimated scope

| Area | Files | New functions |
|---|---|---|
| Config | `core/config.py`, `main.py` | `resolve_embedding_config`, 1 CLI command |
| DB schema | `core/database.py` | `compute_embedding`, `vector_search`, `hybrid_search` |
| Ingest | `core/ingest.py` | embedding calls in `ingest_source` and `_fetch_related` |
| Query | `core/query.py` | hybrid call in `_build_context` |
| Tests | `tests/test_database.py`, `tests/test_query.py` | ~7 new test cases |
| Docs | `CLAUDE.md`, `core/README.md` | — |

One new dependency: `sqlite-vec`. No new processes or servers.
