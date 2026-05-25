"""Prompt assembly and LLM JSON parsing for the wiki pipeline.

Public surface:
- _build_ingest_prompt()        — assemble the primary ingest prompt
- _build_ingest_prompt_strict() — retry variant with explicit JSON-only constraint
- _parse_llm_json()             — parse (and repair) the LLM's JSON response
- _build_query_prompt()         — assemble the Q&A prompt for query_wiki
- _build_lint_prompt()          — assemble the quality-review prompt for lint_vault
"""

from __future__ import annotations

import json
import logging
import re
import textwrap
from typing import Any

log = logging.getLogger(__name__)


def _build_ingest_prompt(
    vault_name: str, schema: str, related: str, filename: str, text: str
) -> str:
    """Assemble the LLM prompt that instructs the model to produce wiki page JSON.

    Args:
        vault_name: Name of the vault, embedded in the system context.
        schema: Content of wiki/schema.md describing vault conventions.
        related: Pre-formatted snippets of existing related pages (may be empty).
        filename: Display name of the source (URL title or filename).
        text: Extracted source text to ingest.

    Returns:
        A single prompt string ready to be sent as a user message to the LLM.
    """
    related_section = (
        f"## Existing Related Pages\n{related}"
        if related
        else "## Existing Related Pages\n(none yet)"
    )
    return textwrap.dedent(f"""
        You are a wiki editor for a personal knowledge base called "{vault_name}".

        ## Vault Schema
        {schema}

        {related_section}

        ## Source to Ingest
        Filename/Title: {filename}

        {text}

        ---

        Produce wiki updates as **valid JSON** (no markdown fences, no prose before/after):

        {{
          "source_page": {{
            "file_path": "Sources/<SlugTitle>.md",
            "content": "<full markdown with YAML frontmatter>"
          }},
          "page_updates": [
            {{
              "file_path": "Concepts/<PageName>.md",
              "action": "create",
              "content": "<full markdown with YAML frontmatter>"
            }}
          ]
        }}

        Rules:
        - source_page goes in Sources/; write a clear summary with [[wikilinks]] to concepts
        - Create or update pages in Concepts/ and Entities/ as appropriate
        - "action": "create" — write this page (replaces existing content if the page already exists)
        - "action": "update" — alias for create; always provide the complete updated page content
        - YAML frontmatter must include title and tags fields
        - Always quote YAML string values that contain colons: title: "Foo: Bar" not title: Foo: Bar
        - Use Obsidian [[Page Name]] syntax for all internal links
        - If a source contradicts an existing page, add a ## Contradictions section
        - page_updates may be an empty array if no concept/entity pages need changes
    """).strip()


def _build_ingest_prompt_strict(
    vault_name: str, schema: str, related: str, filename: str, text: str
) -> str:
    """Assemble a stricter ingest prompt for retry when the initial JSON parse fails.

    Identical to ``_build_ingest_prompt`` but prepends an explicit constraint
    requiring the response to be a bare JSON object with no surrounding prose or fences.

    Args:
        vault_name: Name of the vault.
        schema: Content of wiki/schema.md.
        related: Pre-formatted related page snippets.
        filename: Display name of the source.
        text: Extracted source text.

    Returns:
        A single prompt string with a leading JSON-only constraint.
    """
    preamble = textwrap.dedent("""\
        IMPORTANT: Your entire response must be a single valid JSON object.
        Do not write any text before or after the JSON.
        Do not use markdown code fences.
        Start your response with { and end with }.

    """)
    return preamble + _build_ingest_prompt(vault_name, schema, related, filename, text)


def _parse_llm_json(raw: str) -> dict[str, Any]:
    """Parse the LLM's JSON response, attempting repair before failing.

    Strips markdown fences, tries json.loads, then json_repair.repair_json as a fallback.
    Logs a warning when repair is needed so the user can see which models are unreliable.

    Args:
        raw: Raw string returned by the LLM.

    Returns:
        Parsed dict containing at least ``source_page`` and ``page_updates`` keys.

    Raises:
        ValueError: The string could not be parsed or repaired into valid JSON,
            or the repaired result is missing the ``source_page`` key.
    """
    from json_repair import repair_json

    # Strip markdown fences and leading/trailing prose
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip(), flags=re.MULTILINE)

    # Find the outermost JSON object — handles prose before/after the JSON block
    obj_match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if obj_match:
        cleaned = obj_match.group(0)

    # First attempt: standard parse (fast path, works for well-formed output)
    try:
        data: dict[str, Any] = json.loads(cleaned)
    except json.JSONDecodeError:
        log.warning(
            "LLM output was not valid JSON; attempting repair. "
            "Consider using a larger model or structured output."
        )
        try:
            repaired = repair_json(cleaned, return_objects=False)
            data = json.loads(repaired)
        except (json.JSONDecodeError, ValueError) as e:
            raise ValueError(
                f"LLM returned JSON that could not be repaired: {e}\n\nRaw output:\n{raw[:500]}"
            ) from e

    if "source_page" not in data:
        raise ValueError("LLM response missing 'source_page' key")
    data.setdefault("page_updates", [])
    return data


def _build_query_prompt(question: str, context: str) -> str:
    """Assemble the LLM prompt for answering a question from wiki context.

    Args:
        question: The user's question.
        context: Pre-formatted wiki page snippets to ground the answer.

    Returns:
        A single prompt string ready to be sent as a user message to the LLM.
    """
    return textwrap.dedent(f"""
        You are answering a question using content from a personal wiki knowledge base.
        Answer based strictly on the wiki content provided. If information is missing or
        uncertain, say so clearly. Be concise and cite which pages support your answer.

        ## Wiki Context
        {context}

        ## Question
        {question}

        Provide a direct answer followed by a brief **Sources** section listing the wiki
        pages you used (by title and path).
    """).strip()


def _build_lint_prompt(pages_context: str) -> str:
    """Assemble the LLM prompt for the wiki quality-review pass.

    Args:
        pages_context: Pre-formatted block of page snippets (title, path, content preview).

    Returns:
        A single prompt string ready to be sent as a user message to the LLM.
    """
    return textwrap.dedent(f"""
        You are auditing a personal wiki knowledge base for quality and consistency.

        ## Wiki Pages (Sample)
        {pages_context}

        ## Your Task
        Review the pages above and produce a concise markdown lint report covering:

        1. **Contradictions** — factual conflicts between pages (quote the conflicting claims)
        2. **Incomplete Pages** — pages that seem underdeveloped or missing key information
        3. **Missing Links** — concepts mentioned but not yet linked or given their own page
        4. **Suggestions** — 2-3 concrete improvements for this vault

        Format your response as a markdown document with these four sections.
        Be specific: reference pages by name and quote relevant text when flagging issues.
    """).strip()
