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
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Ensure project root is on the path when run as __main__
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolResult,
    ListToolsResult,
    TextContent,
    Tool,
)

from core.config import GlobalConfig
from core.db import db_connection, list_pages, search


def build_server(default_vault: str | None = None) -> Server:
    server = Server("llm-wiki")
    config = GlobalConfig.load()

    def _resolve(vault_name: str | None) -> tuple[str, Path]:
        vname, vpath = config.resolve_vault(vault_name or default_vault)
        return vname, vpath

    # ------------------------------------------------------------------
    # Tool definitions
    # ------------------------------------------------------------------

    @server.list_tools()  # pyright: ignore[reportArgumentType]
    async def list_tools() -> ListToolsResult:
        return ListToolsResult(
            tools=[
                Tool(
                    name="search_wiki",
                    description="Full-text search across all wiki pages in a vault. Returns ranked results with titles, summaries, and file paths.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Search query"},
                            "vault": {
                                "type": "string",
                                "description": "Vault name (uses default if omitted)",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Max results (default 8)",
                                "default": 8,
                            },
                        },
                        "required": ["query"],
                    },
                ),
                Tool(
                    name="view_page",
                    description="Read the full content of a specific wiki page by its relative file path.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": "Relative path within wiki/ e.g. Concepts/Transformers.md",
                            },
                            "vault": {
                                "type": "string",
                                "description": "Vault name (uses default if omitted)",
                            },
                        },
                        "required": ["file_path"],
                    },
                ),
                Tool(
                    name="list_pages",
                    description="List all pages in a vault, optionally filtered by category (Sources, Concepts, Entities).",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "vault": {
                                "type": "string",
                                "description": "Vault name (uses default if omitted)",
                            },
                            "category": {
                                "type": "string",
                                "description": "Filter by category: Sources, Concepts, Entities",
                            },
                        },
                    },
                ),
                Tool(
                    name="ingest",
                    description="Ingest a file path or URL into the wiki, generating wiki pages from the source.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "string",
                                "description": "File path or URL to ingest",
                            },
                            "vault": {
                                "type": "string",
                                "description": "Vault name (uses default if omitted)",
                            },
                            "dry_run": {
                                "type": "boolean",
                                "description": "If true, return proposed changes without writing",
                                "default": False,
                            },
                        },
                        "required": ["source"],
                    },
                ),
                Tool(
                    name="lint",
                    description="Run a lint pass on the vault: find orphaned pages, broken links, and LLM-detected contradictions.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "vault": {
                                "type": "string",
                                "description": "Vault name (uses default if omitted)",
                            },
                        },
                    },
                ),
                Tool(
                    name="query",
                    description="Ask a question and get an answer grounded in wiki content.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "question": {"type": "string", "description": "The question to answer"},
                            "vault": {
                                "type": "string",
                                "description": "Vault name (uses default if omitted)",
                            },
                            "save_as": {
                                "type": "string",
                                "description": "Save the answer as a new wiki page at this path",
                            },
                        },
                        "required": ["question"],
                    },
                ),
                Tool(
                    name="list_vaults",
                    description="List all registered llm-wiki vaults.",
                    inputSchema={"type": "object", "properties": {}},
                ),
            ]
        )

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        try:
            result = await _dispatch(name, arguments, _resolve)
            return CallToolResult(content=[TextContent(type="text", text=result)])
        except Exception as e:
            return CallToolResult(
                content=[TextContent(type="text", text=f"Error: {e}")],
                isError=True,
            )

    return server


async def _dispatch(
    name: str, args: dict[str, Any], resolve: Callable[[str | None], tuple[str, Path]]
) -> str:
    if name == "list_vaults":
        cfg = GlobalConfig.load()
        return json.dumps({"vaults": cfg.vaults, "default": cfg.default_vault}, indent=2)

    if name == "search_wiki":
        vname, vpath = resolve(args.get("vault"))
        with db_connection(vpath) as conn:
            results = search(conn, args["query"], limit=args.get("limit", 8))
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

    if name == "view_page":
        vname, vpath = resolve(args.get("vault"))
        page_path = vpath / "wiki" / str(args["file_path"])
        if not page_path.exists():
            return f"Page not found: {args['file_path']}"
        return str(page_path.read_text())

    if name == "list_pages":
        vname, vpath = resolve(args.get("vault"))
        with db_connection(vpath) as conn:
            pages = list_pages(conn, category=args.get("category"))
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

    if name == "ingest":
        from core.config import VaultConfig
        from core.ingest import ingest_source

        vname, vpath = resolve(args.get("vault"))
        vcfg = VaultConfig.load(vpath)
        result = ingest_source(
            vpath, args["source"], vcfg.name or vname, dry_run=args.get("dry_run", False)
        )
        return json.dumps(
            {
                "source_page": result.get("source_page", {}).get("file_path"),
                "pages_written": result.get("pages_written", []),
                "updates": [u.get("file_path") for u in result.get("page_updates", [])],
            },
            indent=2,
        )

    if name == "lint":
        from core.lint import lint_vault

        vname, vpath = resolve(args.get("vault"))
        result = lint_vault(vpath)
        s = result["structural"]
        return (
            f"Orphans: {len(s['orphans'])}\n"
            f"Broken links: {len(s['broken_links'])}\n"
            f"Missing summaries: {len(s['missing_summaries'])}\n\n"
            f"Report saved to: {result['saved_to']}\n\n"
            f"--- LLM Report ---\n{result['llm_report']}"
        )

    if name == "query":
        from core.query import query_wiki

        vname, vpath = resolve(args.get("vault"))
        result = query_wiki(vpath, args["question"], save_as=args.get("save_as"))
        out: str = str(result["answer"])
        if result["sources"]:
            out += f"\n\nSources: {', '.join(result['sources'])}"
        if result["saved_to"]:
            out += f"\nSaved to: {result['saved_to']}"
        return out

    return f"Unknown tool: {name}"


async def _run(vault: str | None) -> None:
    server = build_server(default_vault=vault)
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    parser = argparse.ArgumentParser(description="llm-wiki MCP server")
    parser.add_argument("--vault", default=None, help="Default vault name")
    parsed = parser.parse_args()

    asyncio.run(_run(parsed.vault))
