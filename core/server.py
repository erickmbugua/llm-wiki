from __future__ import annotations

import asyncio
import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import GlobalConfig, VaultConfig
from .db import (
    create_job,
    get_db,
    get_job,
    list_jobs,
    list_pages,
    reconcile,
    search,
    update_job_status,
)
from .ingest import ingest_source
from .lint import lint_vault
from .query import query_wiki
from .vault import rebuild_index, vault_stats

log = logging.getLogger(__name__)

HERE = Path(__file__).parent.parent
app = FastAPI(title="llm-wiki", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(HERE / "app" / "static")), name="static")

# Per-vault executors registered by main_server.py at startup.
_vault_executors: dict[str, ThreadPoolExecutor] = {}


def register_vault_executor(vault_name: str, executor: ThreadPoolExecutor) -> None:
    """Register the background executor for a vault.

    Called by main_server.py at startup so that API ingest jobs can be submitted
    to the same single-worker executor used by the file watcher.

    Args:
        vault_name: Registered vault name (key in GlobalConfig.vaults).
        executor: Single-worker ThreadPoolExecutor dedicated to this vault.
    """
    _vault_executors[vault_name] = executor


def _get_executor(vault_name: str) -> ThreadPoolExecutor | None:
    """Return the registered executor for a vault, or None if not yet registered.

    Args:
        vault_name: Registered vault name.

    Returns:
        The executor for this vault, or ``None`` when called outside main_server.py
        (e.g. in tests or direct uvicorn invocations).
    """
    return _vault_executors.get(vault_name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_vault(vault_name: str | None = None):
    """Resolve a vault name to (name, path), raising HTTP 404 if not found.

    Config is cached per-process in ``GlobalConfig.load()``; call
    ``_clear_global_config_cache()`` after any external disk mutation.

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
    """Return the raw markdown content of a single wiki page by its relative file path.

    Raises HTTP 400 if the resolved path escapes the wiki root (path traversal guard).
    """
    _, vpath = _get_vault(vault_name)
    wiki_root = (vpath / "wiki").resolve()
    page_path = (wiki_root / file_path).resolve()
    if not page_path.is_relative_to(wiki_root):
        raise HTTPException(status_code=400, detail="Invalid file path")
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


def _run_ingest_job(vpath: Path, vname: str, source: str, job_id: str, dry_run: bool) -> None:
    """Execute an ingest job and update its DB record with the result.

    Intended to run inside a ThreadPoolExecutor worker — never on the event loop.
    Sets status to ``"running"`` before the LLM call, then ``"done"`` or ``"failed"``.

    Args:
        vpath: Root directory of the vault.
        vname: Human-readable vault name forwarded to ``ingest_source``.
        source: File path or URL being ingested.
        job_id: UUID of the job record to update.
        dry_run: When True, no pages are written to disk.
    """
    conn = get_db(vpath)
    try:
        update_job_status(conn, job_id, "running")
        result = ingest_source(vpath, source, vname, dry_run=dry_run)
        update_job_status(
            conn,
            job_id,
            "done",
            pages_written=result.get("pages_written", []),
        )
    except Exception as e:
        update_job_status(conn, job_id, "failed", error=str(e))
        log.error("Ingest job %s failed: %s", job_id, e)
    finally:
        conn.close()


@app.post("/api/vaults/{vault_name}/ingest", status_code=202)
def api_ingest(vault_name: str, req: IngestRequest) -> JSONResponse:
    """Enqueue an ingest job and return its ID immediately (HTTP 202).

    The job runs in the vault's background executor. Poll
    ``GET /api/vaults/{vault}/jobs/{job_id}`` or stream
    ``GET /api/vaults/{vault}/jobs/{job_id}/stream`` for status.
    """
    vname, vpath = _get_vault(vault_name)
    vcfg = VaultConfig.load(vpath)
    effective_name = vcfg.name or vname

    job_id = str(uuid.uuid4())
    conn = get_db(vpath)
    try:
        create_job(conn, job_id=job_id, vault=vname, source=req.source)
    finally:
        conn.close()

    executor = _get_executor(vname) or ThreadPoolExecutor(max_workers=1)
    executor.submit(_run_ingest_job, vpath, effective_name, req.source, job_id, req.dry_run)

    return JSONResponse({"job_id": job_id, "status": "pending"}, status_code=202)


# ---------------------------------------------------------------------------
# Job status API
# ---------------------------------------------------------------------------


@app.get("/api/vaults/{vault_name}/jobs/{job_id}")
async def api_get_job(vault_name: str, job_id: str) -> dict[str, Any]:
    """Return the current status of an ingest job."""
    _, vpath = _get_vault(vault_name)
    conn = get_db(vpath)
    try:
        job = get_job(conn, job_id)
    finally:
        conn.close()
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job


@app.get("/api/vaults/{vault_name}/jobs/{job_id}/stream")
async def api_stream_job(vault_name: str, job_id: str) -> StreamingResponse:
    """SSE stream that emits the job record every second until the job reaches a terminal state.

    The client should open an ``EventSource`` on this URL. Each event is the full job JSON.
    The stream closes automatically when status is ``"done"`` or ``"failed"``.
    """
    _, vpath = _get_vault(vault_name)

    async def _generator():
        while True:
            conn = get_db(vpath)
            try:
                job = get_job(conn, job_id)
            finally:
                conn.close()
            if job is None:
                yield "event: error\ndata: job not found\n\n"
                return
            yield f"data: {json.dumps(job)}\n\n"
            if job["status"] in ("done", "failed"):
                return
            await asyncio.sleep(1)

    return StreamingResponse(_generator(), media_type="text/event-stream")


@app.get("/api/vaults/{vault_name}/jobs")
async def api_list_jobs(vault_name: str) -> dict[str, Any]:
    """Return the 20 most recent ingest jobs for a vault, newest first."""
    _, vpath = _get_vault(vault_name)
    conn = get_db(vpath)
    try:
        jobs = list_jobs(conn)
    finally:
        conn.close()
    return {"jobs": jobs}


class QueryRequest(BaseModel):
    question: str
    save_as: str | None = None


@app.post("/api/vaults/{vault_name}/query")
def api_query(vault_name: str, req: QueryRequest):
    """Answer a natural-language question grounded in vault content, optionally saving the result."""
    _, vpath = _get_vault(vault_name)
    try:
        result = query_wiki(vpath, req.question, save_as=req.save_as)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return result


@app.post("/api/vaults/{vault_name}/lint")
def api_lint(vault_name: str):
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


# ---------------------------------------------------------------------------
# Index API
# ---------------------------------------------------------------------------


@app.post("/api/vaults/{vault_name}/index/rebuild")
async def api_rebuild_index(vault_name: str) -> dict[str, str]:
    """Rebuild wiki/index.md from the current database state.

    Reads all non-root pages, groups them by category, and rewrites index.md
    with a sorted markdown table. Useful after manual page edits or a reconcile
    without a full ingest.
    """
    _, vpath = _get_vault(vault_name)
    rebuild_index(vpath)
    return {"status": "ok"}
