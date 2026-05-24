from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import GlobalConfig, VaultConfig
from .database import get_db, list_pages, reconcile, search
from .ingest import ingest_source
from .lint import lint_vault
from .query import query_wiki
from .vault import vault_stats

log = logging.getLogger(__name__)

HERE = Path(__file__).parent.parent
app = FastAPI(title="llm-wiki", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(HERE / "app" / "static")), name="static")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_vault(vault_name: str | None = None):
    """Resolve a vault name to (name, path), raising HTTP 404 if not found.

    Args:
        vault_name: Registered vault name. Falls back to the configured default when None.

    Returns:
        A tuple of (vault_name, vault_path).

    Raises:
        HTTPException: 404 if the vault name is not registered or no default is set.
    """
    config = GlobalConfig.load()
    try:
        vname, vpath = config.resolve_vault(vault_name)
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return vname, vpath


# ---------------------------------------------------------------------------
# HTML entry point
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the single-page dashboard HTML."""
    return FileResponse(str(HERE / "app" / "templates" / "index.html"))


# ---------------------------------------------------------------------------
# Vault API
# ---------------------------------------------------------------------------


@app.get("/api/vaults")
async def api_vaults():
    """Return all registered vault names and the current default."""
    config = GlobalConfig.load()
    return {
        "vaults": list(config.vaults.keys()),
        "default": config.default_vault,
    }


@app.get("/api/vaults/{vault_name}/status")
async def api_status(vault_name: str):
    """Return page counts, raw queue size, and the active model for a vault."""
    vname, vpath = _get_vault(vault_name)
    config = GlobalConfig.load()
    vcfg = VaultConfig.load(vpath)
    stats = vault_stats(vpath)
    return {
        "name": vname,
        "path": str(vpath),
        "model": vcfg.model or config.model,
        **stats,
    }


@app.post("/api/vaults/{vault_name}/reconcile")
async def api_reconcile(vault_name: str):
    """Sync the vault database with the current state of wiki markdown files on disk."""
    _, vpath = _get_vault(vault_name)
    conn = get_db(vpath)
    try:
        result = reconcile(conn, vpath / "wiki")
    finally:
        conn.close()
    return result


# ---------------------------------------------------------------------------
# Pages API
# ---------------------------------------------------------------------------


@app.get("/api/vaults/{vault_name}/pages")
async def api_list_pages(vault_name: str, category: str | None = Query(None)):
    """List all pages in the vault, optionally filtered to a single category."""
    _, vpath = _get_vault(vault_name)
    conn = get_db(vpath)
    try:
        pages = list_pages(conn, category=category)
    finally:
        conn.close()
    return {
        "pages": [
            {
                "file_path": p["file_path"],
                "title": p["title"],
                "category": p["category"],
                "summary": p["summary"],
                "tags": json.loads(p["tags"] or "[]"),
                "backlinks": json.loads(p["backlinks"] or "[]"),
            }
            for p in pages
        ]
    }


@app.get("/api/vaults/{vault_name}/pages/content")
async def api_get_page(vault_name: str, file_path: str = Query(...)):
    """Return the raw markdown content of a single wiki page by its relative file path."""
    _, vpath = _get_vault(vault_name)
    page_path = vpath / "wiki" / file_path
    if not page_path.exists():
        raise HTTPException(status_code=404, detail=f"Page not found: {file_path}")
    return {"file_path": file_path, "content": page_path.read_text()}


# ---------------------------------------------------------------------------
# Search API
# ---------------------------------------------------------------------------


@app.get("/api/vaults/{vault_name}/search")
async def api_search(vault_name: str, q: str = Query(...), limit: int = Query(10)):
    """Run a BM25 full-text search across the vault and return ranked results."""
    _, vpath = _get_vault(vault_name)
    conn = get_db(vpath)
    try:
        results = search(conn, q, limit=limit)
    finally:
        conn.close()
    return {
        "results": [
            {
                "file_path": r["file_path"],
                "title": r["title"],
                "category": r["category"],
                "summary": r["summary"],
            }
            for r in results
        ]
    }


# ---------------------------------------------------------------------------
# Graph API — nodes + edges for force-directed graph
# ---------------------------------------------------------------------------


@app.get("/api/vaults/{vault_name}/graph")
async def api_graph(vault_name: str):
    """Return nodes and directed edges for the vault's wikilink graph."""
    _, vpath = _get_vault(vault_name)
    conn = get_db(vpath)
    try:
        pages = list_pages(conn)
    finally:
        conn.close()

    path_to_id = {p["file_path"]: i for i, p in enumerate(pages)}
    nodes = [
        {
            "id": i,
            "title": p["title"],
            "file_path": p["file_path"],
            "category": p["category"],
            "backlink_count": len(json.loads(p["backlinks"] or "[]")),
        }
        for i, p in enumerate(pages)
    ]
    edges = []
    for p in pages:
        src = path_to_id[p["file_path"]]
        for bl in json.loads(p["backlinks"] or "[]"):
            if bl in path_to_id:
                edges.append({"source": path_to_id[bl], "target": src})

    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# LLM operations API
# ---------------------------------------------------------------------------


class IngestRequest(BaseModel):
    source: str
    dry_run: bool = False


@app.post("/api/vaults/{vault_name}/ingest")
async def api_ingest(vault_name: str, req: IngestRequest):
    """Ingest a source URL or file path into the vault, with optional dry-run mode."""
    vname, vpath = _get_vault(vault_name)
    vcfg = VaultConfig.load(vpath)
    try:
        result = ingest_source(vpath, req.source, vcfg.name or vname, dry_run=req.dry_run)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return result


class QueryRequest(BaseModel):
    question: str
    save_as: str | None = None


@app.post("/api/vaults/{vault_name}/query")
async def api_query(vault_name: str, req: QueryRequest):
    """Answer a natural-language question grounded in vault content, optionally saving the result."""
    _, vpath = _get_vault(vault_name)
    try:
        result = query_wiki(vpath, req.question, save_as=req.save_as)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return result


@app.post("/api/vaults/{vault_name}/lint")
async def api_lint(vault_name: str):
    """Run a full structural and LLM quality lint pass on the vault."""
    _, vpath = _get_vault(vault_name)
    try:
        result = lint_vault(vpath)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return result


# ---------------------------------------------------------------------------
# Log API
# ---------------------------------------------------------------------------


@app.get("/api/vaults/{vault_name}/log")
async def api_log(vault_name: str):
    """Return the raw content of wiki/log.md, or an empty string if it does not exist."""
    _, vpath = _get_vault(vault_name)
    log_path = vpath / "wiki" / "log.md"
    return {"content": log_path.read_text() if log_path.exists() else ""}
