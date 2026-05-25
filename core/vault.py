from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .config import VAULT_INTERNAL_DIR, VaultConfig
from .constants import WIKI_CATEGORIES
from .db import db_connection, list_pages


def init_vault(vault_path: Path, name: str) -> None:
    """Create the full llm-wiki directory skeleton inside an (optionally new) vault directory.

    Creates ``raw/``, ``wiki/{Sources,Concepts,Entities}/``, the three root wiki pages
    (index, log, schema), the ``.llm-wiki/`` internal directory, and a per-vault config.
    Existing files are never overwritten.

    Args:
        vault_path: Root directory for the vault (created if it does not exist).
        name: Human-readable vault name written into schema.md and config.json.
    """
    vault_path = vault_path.resolve()
    vault_path.mkdir(parents=True, exist_ok=True)

    (vault_path / "raw").mkdir(exist_ok=True)

    wiki = vault_path / "wiki"
    wiki.mkdir(exist_ok=True)
    for subdir in sorted(WIKI_CATEGORIES):
        (wiki / subdir).mkdir(exist_ok=True)

    _write_if_missing(wiki / "index.md", _index_template())
    _write_if_missing(wiki / "log.md", _log_template())
    _write_if_missing(wiki / "schema.md", _schema_template(name))

    internal = vault_path / VAULT_INTERNAL_DIR
    internal.mkdir(exist_ok=True)
    # keep the SQLite DB out of Obsidian and git
    _write_if_missing(internal / ".gitignore", "wiki.db\n")

    cfg = VaultConfig(name=name)
    cfg.save(vault_path)


def vault_stats(vault_path: Path) -> dict[str, Any]:
    """Return a snapshot of page and file counts for a vault.

    Args:
        vault_path: Root directory of the vault.

    Returns:
        A dict with keys ``total_pages`` (int), ``raw_queued`` (int), and
        ``categories`` (dict mapping each wiki subdir name to its page count).
    """
    wiki = vault_path / "wiki"
    pages = list(wiki.rglob("*.md")) if wiki.exists() else []
    raw_files = list((vault_path / "raw").iterdir()) if (vault_path / "raw").exists() else []
    categories: dict[str, int] = {}
    for subdir in sorted(WIKI_CATEGORIES):
        categories[subdir] = (
            len(list((wiki / subdir).glob("*.md"))) if (wiki / subdir).exists() else 0
        )
    return {
        "total_pages": len(pages),
        "raw_queued": len(raw_files),
        "categories": categories,
    }


def rebuild_index(vault_path: Path) -> None:
    """Regenerate wiki/index.md from the current database, grouped by category.

    Reads all pages via list_pages, sorts by category then title, and writes a
    markdown table with page link and summary columns. The file is always fully
    rewritten — no incremental append. Root-category pages (index, log, schema)
    are excluded.

    Args:
        vault_path: Root directory of the vault.
    """
    with db_connection(vault_path) as conn:
        pages = list_pages(conn)

    wiki = vault_path / "wiki"
    index_path = wiki / "index.md"
    now = datetime.now().strftime("%Y-%m-%d")

    lines: list[str] = [
        "---",
        "title: Index",
        "type: index",
        f"updated: {now}",
        "---",
        "",
        "# Wiki Index",
        "",
    ]

    categories: dict[str, list[dict[str, Any]]] = {}
    for page in pages:
        cat = page.get("category") or "root"
        if cat == "root":
            continue
        categories.setdefault(cat, []).append(page)

    if not categories:
        lines.append("*No pages yet. Ingest a source to populate this index.*")
        lines.append("")
    else:
        for cat in sorted(categories):
            lines.append(f"## {cat}")
            lines.append("")
            lines.append("| Page | Summary |")
            lines.append("|------|---------|")
            for page in sorted(categories[cat], key=lambda p: p.get("title", "")):
                fp = page.get("file_path", "")
                stem = (
                    fp.replace(".md", "").rsplit("/", 1)[-1]
                    if fp
                    else page.get("title", "Untitled")
                )
                summary = (page.get("summary") or "").replace("|", "—")[:120]
                lines.append(f"| [[{stem}]] | {summary} |")
            lines.append("")
        total = sum(len(v) for v in categories.values())
        lines.append(f"*{total} pages · updated {now}*")
        lines.append("")

    index_path.write_text("\n".join(lines))


def _write_if_missing(path: Path, content: str) -> None:
    """Write content to path only when the file does not already exist.

    Args:
        path: Destination file path.
        content: Text to write on first creation.
    """
    if not path.exists():
        path.write_text(content)


def _index_template() -> str:
    """Return the initial content for wiki/index.md with YAML frontmatter and a placeholder."""
    return f"""\
---
title: Index
type: index
updated: {datetime.now().strftime("%Y-%m-%d")}
---

# Wiki Index

*No pages yet. Ingest a source to populate this index.*
"""


def _log_template() -> str:
    """Return the initial content for wiki/log.md with YAML frontmatter and an empty activity log."""
    return """\
---
title: Log
type: log
---

# Activity Log

"""


def _schema_template(name: str) -> str:
    """Return the initial content for wiki/schema.md describing vault purpose and conventions.

    Args:
        name: Vault name embedded in the schema heading.
    """
    return f"""\
---
title: Schema
type: schema
---

# {name} — Wiki Schema

## Purpose
Describe the purpose of this vault.

## Categories
- **Sources/** — Summarized articles, papers, books, web clips
- **Concepts/** — Abstract ideas, technologies, themes
- **Entities/** — People, organizations, projects, products

## Ingestion Rules
- Every source gets a page in Sources/ with a one-paragraph summary
- Extract key concepts and create double-bracket wikilinks to them
- Flag contradictions with existing pages in a Contradictions section
- One source typically touches 5–15 wiki pages

## Link Conventions
- Use Obsidian double-bracket syntax for internal links, e.g. the page title in double square brackets
- Use YAML frontmatter tags for broad categorization
- Cross-reference related concepts liberally

## Lint Rules
- Flag orphaned pages (no backlinks, no outgoing links)
- Flag stale pages (not updated in 90+ days relative to recent ingests)
- Flag contradictions between pages on the same topic
"""
