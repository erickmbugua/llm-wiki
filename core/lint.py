from __future__ import annotations

import json
import logging
import re
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any

import litellm

from .config import resolve_model
from .constants import WIKI_CATEGORIES
from .db import db_connection, list_pages, reconcile
from .prompts import _build_lint_prompt

__all__ = ["lint_vault"]

log = logging.getLogger(__name__)

CONTRADICTION_SAMPLE = 8  # pages sent to LLM for contradiction check
CONTRADICTION_CHARS = 1200
MAX_LINT_REPORTS = 10


def lint_vault(vault_path: Path) -> dict[str, Any]:
    """Run a full lint pass combining structural checks and an LLM quality review.

    Reconciles the database, runs structural checks (orphans, broken links, missing
    summaries), requests an LLM contradiction/quality review, and saves the report.

    Args:
        vault_path: Root directory of the vault.

    Returns:
        A dict with keys:
        - ``structural``: dict with ``orphans``, ``broken_links``, and ``missing_summaries``.
        - ``llm_report``: Markdown string from the LLM quality review.
        - ``saved_to``: Relative path of the saved lint report file.
    """
    wiki_root = vault_path / "wiki"
    with db_connection(vault_path) as conn:
        reconcile(conn, wiki_root)
        pages = list_pages(conn)

    structural = _structural_checks(wiki_root, pages)
    llm_report = _llm_lint(vault_path, wiki_root, pages)
    saved_to = _save_lint_report(vault_path, wiki_root, structural, llm_report)

    return {
        "structural": structural,
        "llm_report": llm_report,
        "saved_to": saved_to,
    }


# ---------------------------------------------------------------------------
# Structural checks (no LLM needed)
# ---------------------------------------------------------------------------


def _structural_checks(wiki_root: Path, pages: list[dict[str, Any]]) -> dict[str, Any]:
    """Scan all pages for structural issues without calling the LLM.

    Checks for:
    - **Orphaned pages**: no inbound backlinks and no outgoing wikilinks (excluding root pages).
    - **Broken wikilinks**: ``[[targets]]`` that do not resolve to a known page title.
    - **Missing summaries**: pages whose ``summary`` field is empty in the database.

    Args:
        wiki_root: Root of the wiki directory (used to read page content).
        pages: List of page record dicts as returned by ``list_pages``.

    Returns:
        A dict with keys ``orphans`` (list), ``broken_links`` (dict mapping path → list),
        and ``missing_summaries`` (list).
    """
    all_titles = {p["file_path"].rsplit("/", 1)[-1].replace(".md", "") for p in pages}

    orphans: list[str] = []
    broken_links: dict[str, list[str]] = {}
    missing_summaries: list[str] = []

    for page in pages:
        backlinks = json.loads(page["backlinks"] or "[]")
        content = (
            (wiki_root / page["file_path"]).read_text()
            if (wiki_root / page["file_path"]).exists()
            else ""
        )
        outgoing = re.findall(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]", content)

        if not backlinks and not outgoing and page["category"] not in ("root",):
            orphans.append(page["file_path"])

        broken = [link for link in outgoing if link.strip() not in all_titles]
        if broken:
            broken_links[page["file_path"]] = broken

        if not page["summary"]:
            missing_summaries.append(page["file_path"])

    return {
        "orphans": orphans,
        "broken_links": broken_links,
        "missing_summaries": missing_summaries,
    }


# ---------------------------------------------------------------------------
# LLM contradiction & quality check
# ---------------------------------------------------------------------------


def _llm_lint(vault_path: Path, wiki_root: Path, pages: list[dict]) -> str:
    """Send a sample of wiki pages to the LLM and return a markdown quality-review report.

    Samples up to ``CONTRADICTION_SAMPLE`` pages, weighted toward Sources and Concepts
    with the longest summaries. Each page is truncated to ``CONTRADICTION_CHARS`` characters.

    Args:
        vault_path: Root directory of the vault (used to resolve the model).
        wiki_root: Root of the wiki directory (used to read page files).
        pages: Full list of page dicts from the database.

    Returns:
        A markdown string covering contradictions, incomplete pages, missing links,
        and improvement suggestions. Returns ``"No pages to lint."`` for an empty vault.
    """
    if not pages:
        return "No pages to lint."

    # Sample a set of pages weighted toward Concepts and Sources
    sample = sorted(
        [p for p in pages if p["category"] in WIKI_CATEGORIES - {"Entities"}],
        key=lambda p: -len(p.get("summary") or ""),
    )[:CONTRADICTION_SAMPLE]
    if not sample:
        sample = pages[:CONTRADICTION_SAMPLE]

    page_snippets: list[str] = []
    for p in sample:
        path = wiki_root / p["file_path"]
        if path.exists():
            content = path.read_text()[:CONTRADICTION_CHARS]
            page_snippets.append(f"### {p['title']} ({p['file_path']})\n{content}")

    prompt = _build_lint_prompt("\n\n".join(page_snippets))
    model = resolve_model(vault_path)
    log.info("Calling %s for lint pass", model)
    response = litellm.completion(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    return (response.choices[0].message.content or "").strip()  # pyright: ignore[reportAttributeAccessIssue]


# ---------------------------------------------------------------------------
# Save lint report
# ---------------------------------------------------------------------------


def _rotate_lint_reports(vault_path: Path, keep: int = MAX_LINT_REPORTS) -> None:
    """Delete the oldest lint-*.md files at vault_path, keeping at most ``keep``.

    Files are sorted lexicographically, which is chronological because the filename
    embeds a timestamp (lint-YYYY-MM-DD-HHMM.md).

    Args:
        vault_path: Root directory of the vault.
        keep: Maximum number of lint reports to retain.
    """
    reports = sorted(vault_path.glob("lint-*.md"))
    for old in reports[:-keep]:
        old.unlink()


def _save_lint_report(
    vault_path: Path, wiki_root: Path, structural: dict[str, Any], llm_report: str
) -> str:
    """Write a combined lint report to the vault root and append a summary entry to log.md.

    The report file is named ``lint-YYYY-MM-DD-HHMM.md`` and saved at the vault root
    (not inside wiki/) so it does not appear as a regular wiki page. After writing,
    old reports are rotated so at most ``MAX_LINT_REPORTS`` files are retained.

    Args:
        vault_path: Root directory of the vault.
        wiki_root: Root of the wiki directory (used to locate log.md).
        structural: Dict returned by ``_structural_checks``.
        llm_report: Markdown string returned by ``_llm_lint``.

    Returns:
        The relative filename of the saved report (e.g. ``"lint-2026-05-24-1430.md"``).
    """
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    rel_path = f"lint-{timestamp}.md"
    out_path = vault_path / rel_path

    orphan_list = "\n".join(f"- {p}" for p in structural["orphans"]) or "_(none)_"
    broken_list = (
        "\n".join(f"- {p}: {', '.join(links)}" for p, links in structural["broken_links"].items())
        or "_(none)_"
    )
    missing_list = "\n".join(f"- {p}" for p in structural["missing_summaries"]) or "_(none)_"

    report = textwrap.dedent(f"""
        ---
        title: Lint Report {timestamp}
        type: lint-report
        created: {datetime.now().strftime("%Y-%m-%d")}
        ---

        # Lint Report — {timestamp}

        ## Structural Issues

        ### Orphaned Pages
        {orphan_list}

        ### Broken Wikilinks
        {broken_list}

        ### Pages Missing Summary
        {missing_list}

        ---

        ## LLM Quality Review

        {llm_report}
    """).strip()

    out_path.write_text(report)

    # also append a note to log.md
    log_path = wiki_root / "log.md"
    with log_path.open("a") as f:
        f.write(f"\n## {datetime.now().strftime('%Y-%m-%d %H:%M')} — Lint pass\n")
        f.write(f"Report saved to: {rel_path}\n")
        f.write(
            f"Orphans: {len(structural['orphans'])}, Broken links: {len(structural['broken_links'])}\n"
        )

    _rotate_lint_reports(vault_path)
    return rel_path
