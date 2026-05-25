from __future__ import annotations

import logging
import textwrap
from pathlib import Path
from typing import Any

import litellm

from .config import resolve_embedding_config, resolve_model
from .db import db_connection, hybrid_search, partial_reconcile
from .embeddings import compute_embedding
from .prompts import _build_query_prompt

__all__ = ["query_wiki"]

log = logging.getLogger(__name__)

CONTEXT_PAGES = 6
CONTEXT_CHARS_PER_PAGE = 2000


def query_wiki(
    vault_path: Path,
    question: str,
    save_as: str | None = None,
) -> dict[str, Any]:
    """Answer a question grounded in wiki content, optionally saving the answer as a new page.

    Retrieves the top-ranked pages via FTS5, builds a context block, and sends a single
    prompt to the LLM. The reconcile step after saving keeps the DB in sync.

    Args:
        vault_path: Root directory of the vault.
        question: Natural-language question to answer.
        save_as: If provided, the answer is saved as a wiki page at this path (or in
            ``Concepts/`` if no directory separator is present). The ``.md`` extension
            is added automatically if missing.

    Returns:
        A dict with keys:
        - ``answer``: LLM-generated answer string.
        - ``sources``: List of page file paths used as context.
        - ``saved_to``: Relative path of the saved page, or ``None``.
    """
    wiki_root = vault_path / "wiki"
    context, sources = _build_context(vault_path, wiki_root, question)
    model = resolve_model(vault_path)

    prompt = _build_query_prompt(question, context)
    log.info("Calling %s for query", model)
    response = litellm.completion(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    answer = (response.choices[0].message.content or "").strip()  # pyright: ignore[reportAttributeAccessIssue]

    saved_to = None
    if save_as:
        saved_to = _save_answer(wiki_root, save_as, question, answer, sources)
        with db_connection(vault_path) as conn:
            partial_reconcile(conn, wiki_root, [wiki_root / saved_to])

    return {"answer": answer, "sources": sources, "saved_to": saved_to}


def _build_context(vault_path: Path, wiki_root: Path, question: str) -> tuple[str, list[str]]:
    """Search the wiki for pages relevant to the question and format them as an LLM context block.

    Uses hybrid retrieval (FTS5 + vector search with RRF) when an embedding model is
    configured. Falls back to lexical-only search if the embedding call fails.

    Args:
        vault_path: Root directory of the vault (used to open the DB).
        wiki_root: Root of the wiki directory (used to read page files).
        question: The user's question, used as the search query.

    Returns:
        A tuple of (context_string, source_paths). ``context_string`` is a markdown-formatted
        block of page snippets separated by ``---``. ``source_paths`` lists each page's
        relative path. Returns a fallback message and empty list when no pages are found.
    """
    emb_model, _ = resolve_embedding_config(vault_path)
    query_embedding: list[float] | None = None
    try:
        query_embedding = compute_embedding(question, model=emb_model)
    except Exception:
        log.warning("Embedding failed for query; falling back to lexical-only search")

    with db_connection(vault_path) as conn:
        results = hybrid_search(conn, question, query_embedding, limit=CONTEXT_PAGES)

    if not results:
        return "(No relevant pages found in wiki.)", []

    parts: list[str] = []
    sources: list[str] = []
    for r in results:
        page_path = wiki_root / r["file_path"]
        if page_path.exists():
            content = page_path.read_text()[:CONTEXT_CHARS_PER_PAGE]
            parts.append(f"### {r['title']}\n**Path:** {r['file_path']}\n\n{content}")
            sources.append(r["file_path"])

    return "\n\n---\n\n".join(parts), sources


def _save_answer(
    wiki_root: Path, save_as: str, question: str, answer: str, sources: list[str]
) -> str:
    """Write the LLM answer as a structured wiki page with YAML frontmatter.

    Pages with no directory component are placed in ``Concepts/`` by default.

    Args:
        wiki_root: Root of the wiki directory.
        save_as: Desired file path (relative to wiki_root). Directory and ``.md`` extension
            are inferred when absent.
        question: The original question embedded in the page body.
        answer: LLM-generated answer text.
        sources: List of source page paths rendered as wikilinks.

    Returns:
        The relative path (from wiki_root) where the page was written.
    """
    from datetime import datetime

    slug = save_as if save_as.endswith(".md") else f"{save_as}.md"
    if "/" not in slug:
        slug = f"Concepts/{slug}"
    p = wiki_root / slug
    p.parent.mkdir(parents=True, exist_ok=True)
    title = slug.rsplit("/", 1)[-1].replace(".md", "").replace("-", " ").replace("_", " ")
    source_links = ", ".join(f"[[{s.replace('.md', '')}]]" for s in sources)
    content = textwrap.dedent(f"""
        ---
        title: {title}
        type: query-answer
        created: {datetime.now().strftime("%Y-%m-%d")}
        tags: [query-answer]
        ---

        # {title}

        **Question:** {question}

        {answer}

        ## Sources
        {source_links}
    """).strip()
    p.write_text(content)
    return slug
