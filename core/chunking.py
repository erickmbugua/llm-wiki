"""Text chunking and map-reduce summarization for large documents.

Public surface:
- _chunk_text()       — split text into overlapping windows
- _summarize_chunks() — call LLM once per chunk and concatenate summaries
"""

from __future__ import annotations

import logging

import litellm

__all__ = ["_chunk_text", "_summarize_chunks"]

log = logging.getLogger(__name__)


def _chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Split text into overlapping chunks of at most chunk_size characters.

    Args:
        text: Full source text to split.
        chunk_size: Maximum characters per chunk.
        overlap: Characters of context shared between consecutive chunks.

    Returns:
        List of text chunks. Returns ``[text]`` unchanged when ``len(text) <= chunk_size``.
    """
    if len(text) <= chunk_size:
        return [text]

    step = max(1, chunk_size - overlap)
    starts = list(range(0, len(text), step))
    chunks: list[str] = []

    for i, start in enumerate(starts):
        end = min(start + chunk_size, len(text))
        chunk = text[start:end]

        # For non-final chunks, break at the nearest newline in the last 200 chars
        # to avoid splitting mid-sentence.
        if i < len(starts) - 1 and end < len(text):
            newline_pos = chunk.rfind("\n", max(0, chunk_size - 200))
            if newline_pos > 0:
                chunk = chunk[: newline_pos + 1]

        chunks.append(chunk)

    return chunks


def _summarize_chunks(
    chunks: list[str],
    model: str,
    vault_name: str,
    filename: str,
    context_chars: int = 24_000,
) -> str:
    """Call the LLM once per chunk to extract key points, then concatenate.

    Args:
        chunks: List of text chunks from ``_chunk_text``.
        model: litellm model string to use for summarization.
        vault_name: Passed to the prompt for context.
        filename: Display name of the source document.
        context_chars: If the concatenated summaries exceed this many characters,
            truncate with a trailing note.

    Returns:
        A single string of bullet-point summaries from all chunks. Truncated to
        ``context_chars`` characters when the combined output is too large.
    """
    n = len(chunks)
    summaries: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        prompt = (
            f'You are summarizing part {i}/{n} of a document called "{filename}" '
            f'for a personal wiki called "{vault_name}".\n\n'
            "Extract the 5-10 most important facts, claims, or ideas from this section as "
            "concise bullet points. Focus on substance; skip navigation text, footers, "
            "and boilerplate.\n\n"
            f"--- SECTION ---\n{chunk}"
        )
        log.info("Summarizing chunk %d/%d for '%s'", i, n, filename)
        response = litellm.completion(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        bullet = response.choices[0].message.content  # pyright: ignore[reportAttributeAccessIssue]
        if not bullet:
            continue
        bullet = str(bullet)
        summaries.append(f"### Part {i}/{n}\n{bullet.strip()}")

    summaries_text = "\n\n".join(summaries)
    if len(summaries_text) > context_chars:
        note = (
            "\n\n[Note: document was too large to fully summarize; "
            "above covers the first sections only]"
        )
        summaries_text = summaries_text[: context_chars - len(note)] + note
    return summaries_text
