# P0 — Document Chunking for Large Sources

## Problem Statement

`ingest_source` hard-truncates every source document at `context_chars` (default 24,000) before
sending it to the LLM. A 200-page PDF, a long Substack essay, or a dense research paper will all
be silently truncated to roughly the first 15 pages. The user sees a "done" queue status and
receives a wiki page that covers only a fraction of the document — with no indication that data
was dropped.

Concretely: `_extract_text` in `core/ingest.py:215` returns `text[:char_limit]` before returning
to `ingest_source`. That slice is the only text the LLM ever sees. There is no chunking, no
multi-pass summarization, and no warning in the written page that content was cut.

For a knowledge-base tool whose purpose is to surface everything in a document, this is a
correctness failure, not a UX polish gap.

---

## Implementation Plan

### Strategy: map-reduce summarization

For sources under `context_chars`, behaviour is unchanged (single-pass, as today).
For sources over `context_chars`, split into overlapping chunks, summarize each chunk with
a lightweight "extract key points" prompt, then run the normal ingest prompt over the
concatenated summaries.

This keeps the final ingest prompt within the model's context window regardless of document
length, requires no new dependencies, and works identically for local and cloud models.

---

### Step 1 — Add chunking config fields

**File:** `core/config.py`

Add two new optional fields to `VaultConfig` and `GlobalConfig`:

```python
chunk_size: int = 20_000        # chars per chunk (slightly under context_chars)
chunk_overlap: int = 500        # chars of overlap between adjacent chunks
```

Expose them via `resolve_chunk_config(vault_path) -> tuple[int, int]` following the same
three-level priority chain as `resolve_model` and `resolve_context_chars`.

Add CLI setters to `main.py`:
```
llm-wiki set-chunk-size <chars> [--vault VAULT]
llm-wiki set-chunk-overlap <chars> [--vault VAULT]
```

---

### Step 2 — Implement the chunker

**File:** `core/ingest.py` — new private function

```python
def _chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Split text into overlapping chunks of at most chunk_size characters.

    Args:
        text: Full source text to split.
        chunk_size: Maximum characters per chunk.
        overlap: Characters of context shared between consecutive chunks.

    Returns:
        List of text chunks. Returns [text] unchanged when len(text) <= chunk_size.
    """
```

Implementation: slide a window of `chunk_size` chars, advancing by `chunk_size - overlap`
each step. Try to break at the nearest newline within the last 200 chars of each window to
avoid splitting mid-sentence. Return `[text]` when `len(text) <= chunk_size` so callers need
no special-case logic.

---

### Step 3 — Implement per-chunk summarization

**File:** `core/ingest.py` — new private function

```python
def _summarize_chunks(
    chunks: list[str],
    model: str,
    vault_name: str,
    filename: str,
) -> str:
    """Call the LLM once per chunk to extract key points, then concatenate.

    Args:
        chunks: List of text chunks from _chunk_text.
        model: litellm model string to use for summarization.
        vault_name: Passed to the prompt for context.
        filename: Display name of the source document.

    Returns:
        A single string of extracted bullet-point summaries from all chunks,
        sized to fit within context_chars.
    """
```

Per-chunk prompt template (simple and low-token):

```
You are summarizing part {i}/{n} of a document called "{filename}" for a personal wiki.

Extract the 5–10 most important facts, claims, or ideas from this section as concise
bullet points. Focus on substance; skip navigation text, footers, and boilerplate.

--- SECTION ---
{chunk_text}
```

Collect the bullet-point responses and concatenate them into a single `summaries_text` string.
If `summaries_text` still exceeds `context_chars`, truncate with a trailing note:
`"\n\n[Note: document was too large to fully summarize; above covers first N sections]"`.

---

### Step 4 — Wire into `ingest_source`

**File:** `core/ingest.py:ingest_source`

After extracting text and before building the ingest prompt:

```python
chunk_size, chunk_overlap = resolve_chunk_config(vault_path)
chunks = _chunk_text(text, chunk_size=chunk_size, overlap=chunk_overlap)

if len(chunks) > 1:
    log.info(
        "Source '%s' split into %d chunks — running summarization pass",
        display_name, len(chunks)
    )
    text = _summarize_chunks(chunks, model=model, vault_name=vault_name, filename=display_name)
    # Annotate that this was a multi-chunk ingest so the LLM knows
    text = f"[This document was split into {len(chunks)} sections and pre-summarized.]\n\n" + text
```

The rest of `ingest_source` (related-pages fetch, main prompt, JSON parse, write) is unchanged.

---

### Step 5 — Write tests

**File:** `tests/test_ingest.py`

New test cases:
- `test_chunk_text_single_chunk`: text under chunk_size → returns `[text]` unchanged
- `test_chunk_text_splits_correctly`: text at 2× chunk_size → two chunks with correct overlap
- `test_chunk_text_breaks_at_newline`: last chunk boundary aligns to nearest newline
- `test_summarize_chunks_calls_model_per_chunk`: verify litellm is called N times for N chunks
- `test_ingest_source_large_doc_uses_chunking`: mock a 60K-char source → verify
  `_summarize_chunks` is called and `ingest_source` returns pages_written
- `test_ingest_source_small_doc_no_chunking`: source under chunk_size → single-pass (no change
  to existing behaviour)

---

### Step 6 — Update documentation

- `CLAUDE.md` — add `chunk_size` and `chunk_overlap` to the Known Gotchas / config section and
  the Vault Structure config fields table
- `core/README.md` — update the `ingest.py` module description to mention multi-chunk flow
- Docstrings on `_chunk_text` and `_summarize_chunks`

---

### Estimated scope

| Area | Files | New functions |
|---|---|---|
| Config | `core/config.py`, `main.py` | `resolve_chunk_config`, 2 CLI commands |
| Chunking | `core/ingest.py` | `_chunk_text`, `_summarize_chunks` |
| Tests | `tests/test_ingest.py` | 6 new test cases |
| Docs | `CLAUDE.md`, `core/README.md` | — |

No new dependencies. No schema changes. Backward-compatible: vaults without chunk config use
defaults that match current single-pass behaviour for small documents.
