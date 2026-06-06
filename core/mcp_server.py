"""
MCP server exposing llm-wiki vault operations as tools.

Start with:
    python -m core.mcp_server [--vault VAULT_NAME]

Or register in Claude Code's MCP settings:
    {
      "mcpServers": {
        "llm-wiki": {
          "command": "/path/to/llm-wiki/.venv/bin/python",
          "args": ["-m", "core.mcp_server"],
          "env": {"ANTHROPIC_API_KEY": "..."}
        }
      }
    }
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure project root is on the path when run as __main__
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp.server.fastmcp import FastMCP

from core.config import GlobalConfig
from core.db import db_connection, list_pages, search

mcp = FastMCP("llm-wiki")

# Set at startup via --vault; used as fallback when the caller omits vault=
_default_vault: str | None = None


def _resolve(vault_name: str | None) -> tuple[str, Path]:
    """Resolve a vault name (or None) to (name, path).

    Args:
        vault_name: Explicit vault name from tool argument, or None to use the default.

    Returns:
        Tuple of (vault_name, vault_path).
    """
    config = GlobalConfig.load()
    return config.resolve_vault(vault_name or _default_vault)


# ------------------------------------------------------------------
# Tools
# ------------------------------------------------------------------


@mcp.tool()
def search_wiki(query: str, vault: str | None = None, limit: int = 8) -> str:
    """Full-text search across all wiki pages in a vault.

    Returns ranked results with titles, summaries, and file paths.

    Args:
        query: Search query string.
        vault: Vault name (uses default vault if omitted).
        limit: Maximum number of results to return.

    Returns:
        JSON array of matching pages with title, file_path, category, and summary.
    """
    _, vpath = _resolve(vault)
    with db_connection(vpath) as conn:
        results = search(conn, query, limit=limit)
    output = [
        {
            "title": r["title"],
            "file_path": r["file_path"],
            "category": r["category"],
            "summary": r["summary"],
        }
        for r in results
    ]
    return json.dumps(output, indent=2)


@mcp.tool()
def view_page(file_path: str, vault: str | None = None) -> str:
    """Read the full content of a specific wiki page by its relative file path.

    Args:
        file_path: Relative path within wiki/, e.g. Concepts/Transformers.md
        vault: Vault name (uses default vault if omitted).

    Returns:
        Markdown content of the page, or an error message if not found.
    """
    _, vpath = _resolve(vault)
    page_path = vpath / "wiki" / str(file_path)
    if not page_path.exists():
        return f"Page not found: {file_path}"
    return str(page_path.read_text())


@mcp.tool()
def list_wiki_pages(vault: str | None = None, category: str | None = None) -> str:
    """List all pages in a vault, optionally filtered by category.

    Args:
        vault: Vault name (uses default vault if omitted).
        category: Filter by category: Sources, Concepts, or Entities.

    Returns:
        JSON array of pages with title, file_path, category, and summary.
    """
    _, vpath = _resolve(vault)
    with db_connection(vpath) as conn:
        pages = list_pages(conn, category=category)
    output = [
        {
            "title": p["title"],
            "file_path": p["file_path"],
            "category": p["category"],
            "summary": p["summary"],
        }
        for p in pages
    ]
    return json.dumps(output, indent=2)


@mcp.tool()
def ingest(source: str, vault: str | None = None, dry_run: bool = False) -> str:
    """Ingest a file path or URL into the wiki, generating wiki pages from the source.

    Args:
        source: File path or URL to ingest.
        vault: Vault name (uses default vault if omitted).
        dry_run: If True, return proposed changes without writing to disk.

    Returns:
        JSON object with source_page path, pages_written list, and updates list.
    """
    from core.config import VaultConfig
    from core.ingest import ingest_source

    vname, vpath = _resolve(vault)
    vcfg = VaultConfig.load(vpath)
    result = ingest_source(vpath, source, vcfg.name or vname, dry_run=dry_run)
    return json.dumps(
        {
            "source_page": result.get("source_page", {}).get("file_path"),
            "pages_written": result.get("pages_written", []),
            "updates": [u.get("file_path") for u in result.get("page_updates", [])],
        },
        indent=2,
    )


@mcp.tool()
def lint(vault: str | None = None) -> str:
    """Run a lint pass on the vault: find orphaned pages, broken links, and LLM-detected contradictions.

    Args:
        vault: Vault name (uses default vault if omitted).

    Returns:
        Structural summary and the full LLM contradiction report.
    """
    from core.lint import lint_vault

    _, vpath = _resolve(vault)
    result = lint_vault(vpath)
    s = result["structural"]
    return (
        f"Orphans: {len(s['orphans'])}\n"
        f"Broken links: {len(s['broken_links'])}\n"
        f"Missing summaries: {len(s['missing_summaries'])}\n\n"
        f"Report saved to: {result['saved_to']}\n\n"
        f"--- LLM Report ---\n{result['llm_report']}"
    )


@mcp.tool()
def query(question: str, vault: str | None = None, save_as: str | None = None) -> str:
    """Ask a question and get an answer grounded in wiki content.

    Args:
        question: The question to answer.
        vault: Vault name (uses default vault if omitted).
        save_as: Save the answer as a new wiki page at this relative path.

    Returns:
        The answer with source page references, and saved path if save_as was given.
    """
    from core.query import query_wiki

    _, vpath = _resolve(vault)
    result = query_wiki(vpath, question, save_as=save_as)
    out: str = str(result["answer"])
    if result["sources"]:
        out += f"\n\nSources: {', '.join(result['sources'])}"
    if result["saved_to"]:
        out += f"\nSaved to: {result['saved_to']}"
    return out


@mcp.tool()
def list_vaults() -> str:
    """List all registered llm-wiki vaults.

    Returns:
        JSON object with vaults dict and default vault name.
    """
    cfg = GlobalConfig.load()
    return json.dumps({"vaults": cfg.vaults, "default": cfg.default_vault}, indent=2)


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------


def main(vault: str | None = None) -> None:
    """Start the MCP stdio server.

    Args:
        vault: Optional default vault name to use when tools omit the vault argument.
    """
    global _default_vault
    _default_vault = vault
    mcp.run(transport="stdio")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="llm-wiki MCP server")
    parser.add_argument("--vault", default=None, help="Default vault name")
    parsed = parser.parse_args()
    main(vault=parsed.vault)
