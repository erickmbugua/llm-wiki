"""Ingest orchestration: extract → related search → LLM prompt → parse → write → reconcile → log.

This module is the entry point for all ingest operations. Text extraction, chunking,
and prompt assembly live in the focused sub-modules imported below.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import litellm
import requests

from .chunking import _chunk_text, _summarize_chunks
from .config import (
    resolve_chunk_config,
    resolve_context_chars,
    resolve_embedding_config,
    resolve_model,
)
from .db import (
    db_connection,
    get_page,
    get_pending_queue,
    hybrid_search,
    mark_queue_item,
    partial_reconcile,
    upsert_page,
)
from .embeddings import compute_embedding
from .extraction import _extract_text
from .prompts import _build_ingest_prompt, _build_ingest_prompt_strict, _parse_llm_json
from .vault import rebuild_index

log = logging.getLogger(__name__)

# Models whose Ollama availability has already been verified this process lifetime.
# Avoids a redundant GET /api/tags on every item in a batched ingest queue.
_ollama_verified: set[str] = set()

RELATED_PAGES_LIMIT = 5


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def ingest_source(
    vault_path: Path,
    source: str,
    vault_name: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Extract text from a source, call the LLM to generate wiki pages, and write them to disk.

    After writing, only the newly written paths are re-indexed via ``partial_reconcile``
    rather than a full vault scan, keeping large vaults fast.

    Args:
        vault_path: Root directory of the vault.
        source: A file path or HTTP/HTTPS URL to ingest.
        vault_name: Human-readable vault name passed to the LLM prompt for context.
        dry_run: When True, parses and returns the LLM output but writes nothing to disk.

    Returns:
        A dict with keys:
        - ``source_page``: the generated source page dict (file_path, content).
        - ``page_updates``: list of concept/entity page dicts from the LLM.
        - ``pages_written``: list of relative paths actually written (empty on dry run).

    Raises:
        ValueError: Text extraction returned empty content.
    """
    char_limit = resolve_context_chars(vault_path)
    text, display_name = _extract_text(source, char_limit=char_limit)
    if not text:
        raise ValueError(f"Could not extract text from: {source}")

    wiki_root = vault_path / "wiki"
    schema = _load_schema(vault_path)
    related = _fetch_related(vault_path, wiki_root, text)

    model = resolve_model(vault_path)
    if model.startswith("ollama/"):
        _check_ollama(model)

    chunk_size, chunk_overlap = resolve_chunk_config(vault_path)
    chunks = _chunk_text(text, chunk_size=chunk_size, overlap=chunk_overlap)
    if len(chunks) > 1:
        log.info(
            "Source '%s' split into %d chunks — running summarization pass",
            display_name,
            len(chunks),
        )
        text = _summarize_chunks(
            chunks,
            model=model,
            vault_name=vault_name,
            filename=display_name,
            context_chars=char_limit,
        )
        text = (
            f"[This document was split into {len(chunks)} sections and pre-summarized.]\n\n" + text
        )

    prompt = _build_ingest_prompt(vault_name, schema, related, display_name, text)

    log.info("Calling %s for ingest of '%s'", model, display_name)
    response = litellm.completion(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
    )
    raw = response.choices[0].message.content  # pyright: ignore[reportAttributeAccessIssue]
    if not raw:
        raise ValueError("LLM returned an empty response for ingest")
    raw = str(raw)  # narrow Unknown | str | None → str after the guard above

    try:
        result = _parse_llm_json(raw)
    except ValueError:
        log.warning("JSON parse failed for '%s'; retrying with constrained prompt", display_name)
        prompt_retry = _build_ingest_prompt_strict(vault_name, schema, related, display_name, text)
        response_retry = litellm.completion(
            model=model,
            messages=[{"role": "user", "content": prompt_retry}],
            temperature=0.0,
        )
        raw_retry = str(response_retry.choices[0].message.content or "")  # pyright: ignore[reportAttributeAccessIssue]
        result = _parse_llm_json(raw_retry)  # let ValueError propagate on second failure
    if not dry_run:
        written = _write_pages(wiki_root, result)
        with db_connection(vault_path) as conn:
            written_paths = [wiki_root / fp for fp in written]
            partial_reconcile(conn, wiki_root, written_paths)
            _store_embeddings(conn, wiki_root, written_paths, vault_path)
        _append_log(vault_path, display_name, written)
        rebuild_index(vault_path)
        result["pages_written"] = written
    else:
        result["pages_written"] = []

    return result


def ingest_queued(vault_path: Path, vault_name: str) -> list[dict[str, Any]]:
    """Process every pending item in the ingest queue, updating queue status as it goes.

    Opens a single DB connection for the entire loop — status updates (pending →
    processing → done/failed) all reuse it. ``ingest_source`` still manages its own
    internal connection for the reconcile step.

    Each item is marked ``"processing"`` before the ingest attempt, then ``"done"``
    or ``"failed"`` afterwards. Failures are logged but do not abort remaining items.

    Args:
        vault_path: Root directory of the vault.
        vault_name: Human-readable vault name forwarded to ``ingest_source``.

    Returns:
        A list of result dicts, one per queued file, each containing at minimum
        ``{"file": str, "status": "done"|"failed"}`` plus ingest output or an ``"error"`` key.
    """
    with db_connection(vault_path) as conn:
        pending = get_pending_queue(conn)
        results = []
        for item in pending:
            fp = item["file_path"]  # vault-relative, e.g. "raw/paper.pdf"
            abs_fp = str(vault_path / fp)
            mark_queue_item(conn, fp, "processing")
            try:
                r = ingest_source(vault_path, abs_fp, vault_name)
                mark_queue_item(conn, fp, "done")
                results.append({"file": fp, "status": "done", **r})
            except Exception as e:
                mark_queue_item(conn, fp, "failed", str(e))
                log.error("Failed to ingest %s: %s", fp, e)
                results.append({"file": fp, "status": "failed", "error": str(e)})
    return results


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------


def _store_embeddings(
    conn: sqlite3.Connection,
    wiki_root: Path,
    written_paths: list[Path],
    vault_path: Path,
) -> None:
    """Compute and store embeddings for newly written wiki pages.

    Silently skips individual pages whose embedding computation fails so that a
    missing or slow embedding model never aborts an otherwise successful ingest.

    Args:
        conn: Open database connection.
        wiki_root: Root of the wiki directory.
        written_paths: Absolute paths to pages just written by this ingest.
        vault_path: Root of the vault (used to resolve the embedding model).
    """
    emb_model, _ = resolve_embedding_config(vault_path)
    for page_path in written_paths:
        if not page_path.exists():
            continue
        rel = str(page_path.relative_to(wiki_root))
        try:
            text_for_embed = page_path.read_text()[:8192]
            embedding = compute_embedding(text_for_embed, model=emb_model)
            page = get_page(conn, rel)
            if page is not None:
                upsert_page(conn, wiki_root, page_path, embedding=embedding)
        except Exception as exc:
            log.warning("Could not compute embedding for '%s': %s", rel, exc)


# ---------------------------------------------------------------------------
# Ollama preflight
# ---------------------------------------------------------------------------


def _check_ollama(model: str) -> None:
    """Verify the Ollama server is reachable and the requested model is pulled.

    Results are cached in ``_ollama_verified`` for the lifetime of the process,
    so repeated calls for the same model string skip the network round-trip.

    Args:
        model: litellm-format model string, e.g. ``"ollama/qwen2.5-coder:7b"``.

    Raises:
        RuntimeError: Ollama server is not running or unreachable.
        RuntimeError: The specific model has not been pulled locally.
    """
    if model in _ollama_verified:
        return
    base_url = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434").rstrip("/")
    model_name = model[len("ollama/") :]

    try:
        resp = requests.get(f"{base_url}/api/tags", timeout=3)
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(
            f"Ollama is not running or unreachable at {base_url}.\n"
            f"Start it with: ollama serve\n"
            f"Then pull the model: ollama pull {model_name}"
        ) from exc

    available = [m["name"] for m in resp.json().get("models", [])]
    if model_name not in available:
        listed = ", ".join(available) if available else "(none)"
        raise RuntimeError(
            f"Model '{model_name}' is not pulled. Run: ollama pull {model_name}\n"
            f"Available models: {listed}"
        )
    _ollama_verified.add(model)


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------


def _load_schema(vault_path: Path) -> str:
    """Read wiki/schema.md and return its content, or an empty string if it does not exist.

    Args:
        vault_path: Root directory of the vault.

    Returns:
        Raw schema markdown text, or ``""`` when the file is absent.
    """
    schema_path = vault_path / "wiki" / "schema.md"
    return schema_path.read_text() if schema_path.exists() else ""


def _fetch_related(vault_path: Path, wiki_root: Path, text: str) -> str:
    """Search the wiki for pages related to the source text and return their content snippets.

    Uses the first 500 characters of the source text as a seed query. Performs hybrid
    retrieval (FTS5 + vector search with RRF) when an embedding model is available;
    falls back to lexical-only search on embedding failure.

    Args:
        vault_path: Root directory of the vault (used to open the DB).
        wiki_root: Root of the wiki directory (used to read page files).
        text: Source text whose beginning is used to seed the search query.

    Returns:
        A formatted string of related page snippets (title, path, content preview),
        or an empty string when no matches are found.
    """
    seed = re.sub(r"[^\w\s]", " ", text[:500])
    words = [w for w in seed.split() if len(w) > 4][:10]
    if not words:
        return ""
    fts_query = " OR ".join(words)

    emb_model, _ = resolve_embedding_config(vault_path)
    query_embedding: list[float] | None = None
    try:
        query_embedding = compute_embedding(text[:500], model=emb_model)
    except Exception:
        log.debug("Embedding unavailable for related-pages search; using lexical only")

    with db_connection(vault_path) as conn:
        results = hybrid_search(conn, fts_query, query_embedding, limit=RELATED_PAGES_LIMIT)

    if not results:
        return ""

    parts: list[str] = []
    for r in results:
        page_path = wiki_root / r["file_path"]
        if page_path.exists():
            content = page_path.read_text()[:1500]
            parts.append(f"### {r['title']} ({r['file_path']})\n{content}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Write pages
# ---------------------------------------------------------------------------


def _safe_wiki_path(wiki_root: Path, rel_path: str) -> Path | None:
    """Resolve a LLM-supplied relative path and confirm it stays inside wiki_root.

    Args:
        wiki_root: Absolute root of the wiki directory.
        rel_path: Relative path string returned by the LLM (e.g. ``"Concepts/Foo.md"``).

    Returns:
        The resolved absolute path if it is contained within wiki_root, otherwise ``None``.
        A ``None`` return means the path is unsafe and the caller should skip the write.
    """
    resolved = (wiki_root / rel_path).resolve()
    if not resolved.is_relative_to(wiki_root.resolve()):
        log.warning("LLM returned unsafe path '%s'; skipping write", rel_path)
        return None
    return resolved


def _write_pages(wiki_root: Path, result: dict[str, Any]) -> list[str]:
    """Write the source page and all page updates from the parsed LLM result to disk.

    Both ``"create"`` and ``"update"`` actions always write the full LLM-produced content,
    replacing any existing file. The LLM receives current page content via the related-pages
    context, so it already produces a complete updated page; appending would double content.

    All LLM-supplied file paths are validated against wiki_root before writing;
    paths that escape the wiki directory are logged and silently skipped.

    Args:
        wiki_root: Root of the wiki directory.
        result: Parsed LLM output dict with ``source_page`` and ``page_updates`` keys.

    Returns:
        List of relative file paths (relative to wiki_root) that were written.
    """
    written: list[str] = []

    source_page = result.get("source_page", {})
    if source_page.get("file_path") and source_page.get("content"):
        p = _safe_wiki_path(wiki_root, source_page["file_path"])
        if p is not None:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(source_page["content"])
            written.append(source_page["file_path"])

    for update in result.get("page_updates", []):
        fp = update.get("file_path", "")
        content = update.get("content", "")
        if not fp or not content:
            continue
        p = _safe_wiki_path(wiki_root, fp)
        if p is None:
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        written.append(fp)

    return written


def _append_log(vault_path: Path, source_name: str, pages_written: list[str]) -> None:
    """Append an ingest activity entry to wiki/log.md.

    Args:
        vault_path: Root directory of the vault.
        source_name: Display name of the ingested source (URL title or filename).
        pages_written: List of relative paths that were written during this ingest.
    """
    log_path = vault_path / "wiki" / "log.md"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n## {timestamp} — Ingest: {source_name}\n"
    entry += (
        f"Pages written ({len(pages_written)}): "
        + ", ".join(f"[[{p.replace('.md', '')}]]" for p in pages_written)
        + "\n"
    )
    with log_path.open("a") as f:
        f.write(entry)
