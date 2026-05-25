# P0 — Ingest Progress Feedback

## Problem Statement

`POST /api/vaults/{vault_name}/ingest` is a blocking endpoint. FastAPI runs it in its
thread pool (correctly, since it is declared `def` not `async def`), but the HTTP response
is withheld until the LLM call completes — up to 120 seconds on a local 7B model.

A user who clicks "Ingest" in the dashboard sees a frozen spinner with no feedback for up to
two minutes. They have no way to know whether the model is running, the server is stuck, or
the request was lost. The dashboard's only status signal is "request finished / request
errored". There is no progress during the call.

The watcher-based auto-ingest (files dropped into `raw/`) already runs in a background
`ThreadPoolExecutor` and finishes asynchronously. The explicit API ingest path has no
equivalent mechanism — it bypasses the queue entirely and blocks the caller.

This needs a job model: POST to ingest returns immediately with a job ID; a streaming
endpoint delivers progress events as the job runs.

---

## Implementation Plan

### Strategy: job table + Server-Sent Events

Add an `ingest_jobs` table to the vault DB. The API ingest endpoint creates a job record,
submits work to the per-vault executor (reusing the existing one), and returns the job ID
immediately (HTTP 202). A new SSE endpoint streams status updates for a given job ID.

The auto-ingest watcher path will also create job records so the dashboard can display their
progress in the same UI surface.

---

### Step 1 — Add `ingest_jobs` table to schema

**File:** `core/database.py:_ensure_schema`

```sql
CREATE TABLE IF NOT EXISTS ingest_jobs (
    id          TEXT PRIMARY KEY,    -- UUID
    vault       TEXT NOT NULL,
    source      TEXT NOT NULL,
    status      TEXT DEFAULT 'pending',   -- pending | running | done | failed
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL,
    pages_written TEXT DEFAULT '[]',      -- JSON array of relative paths
    error       TEXT
);
```

Add CRUD functions to `core/database.py`:

```python
def create_job(conn: sqlite3.Connection, vault: str, source: str) -> str:
    """Insert a new ingest job and return its UUID."""

def update_job_status(
    conn: sqlite3.Connection,
    job_id: str,
    status: str,
    pages_written: list[str] | None = None,
    error: str | None = None,
) -> None:
    """Update job status and optional result fields."""

def get_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    """Fetch a single job record by ID."""

def list_jobs(
    conn: sqlite3.Connection,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return the most recent ingest jobs, newest first."""
```

---

### Step 2 — Expose the per-vault executor to the server

Currently, `ThreadPoolExecutor` instances are created in `main_server.py` and are not
accessible to `core/server.py`. The server needs a way to submit jobs to the same executor.

**File:** `core/server.py`

Add a module-level registry:

```python
_vault_executors: dict[str, ThreadPoolExecutor] = {}

def register_vault_executor(vault_name: str, executor: ThreadPoolExecutor) -> None:
    """Register the executor for a vault. Called by main_server.py at startup."""
    _vault_executors[vault_name] = executor

def _get_executor(vault_name: str) -> ThreadPoolExecutor | None:
    """Return the executor for vault_name, or None if not registered."""
    return _vault_executors.get(vault_name)
```

**File:** `main_server.py`

After creating each executor, call:
```python
from core.server import register_vault_executor
register_vault_executor(vname, executor)
```

For the case where the server is started without `main_server.py` (e.g. direct `uvicorn`
call in tests), `_get_executor` returns `None` and the endpoint falls back to creating a
one-shot thread directly.

---

### Step 3 — Rewrite `api_ingest` as a non-blocking endpoint

**File:** `core/server.py`

```python
import uuid
from fastapi.responses import JSONResponse

@app.post("/api/vaults/{vault_name}/ingest", status_code=202)
def api_ingest(vault_name: str, req: IngestRequest) -> JSONResponse:
    """Enqueue an ingest job and return its ID immediately (HTTP 202).

    The job runs in the vault's background executor. Poll
    GET /api/vaults/{vault_name}/jobs/{job_id} or stream
    GET /api/vaults/{vault_name}/jobs/{job_id}/stream for status.
    """
    vname, vpath = _get_vault(vault_name)
    vcfg = VaultConfig.load(vpath)

    job_id = str(uuid.uuid4())
    conn = get_db(vpath)
    try:
        create_job(conn, job_id=job_id, vault=vname, source=req.source)
    finally:
        conn.close()

    executor = _get_executor(vname) or ThreadPoolExecutor(max_workers=1)
    executor.submit(_run_ingest_job, vpath, vcfg.name or vname, req.source, job_id, req.dry_run)

    return JSONResponse({"job_id": job_id, "status": "pending"}, status_code=202)
```

Add the worker function:

```python
def _run_ingest_job(
    vpath: Path, vname: str, source: str, job_id: str, dry_run: bool
) -> None:
    """Execute an ingest job and update its DB record with the result."""
    conn = get_db(vpath)
    try:
        update_job_status(conn, job_id, "running")
        result = ingest_source(vpath, source, vname, dry_run=dry_run)
        update_job_status(
            conn, job_id, "done",
            pages_written=result.get("pages_written", []),
        )
    except Exception as e:
        update_job_status(conn, job_id, "failed", error=str(e))
        log.error("Ingest job %s failed: %s", job_id, e)
    finally:
        conn.close()
```

---

### Step 4 — Add job status and streaming endpoints

**File:** `core/server.py`

```python
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
async def api_stream_job(vault_name: str, job_id: str):
    """SSE stream that emits the job record each second until done or failed."""
    from fastapi.responses import StreamingResponse
    import asyncio, json as _json

    _, vpath = _get_vault(vault_name)

    async def _generator():
        while True:
            conn = get_db(vpath)
            try:
                job = get_job(conn, job_id)
            finally:
                conn.close()
            if job is None:
                yield f"event: error\ndata: job not found\n\n"
                return
            yield f"data: {_json.dumps(job)}\n\n"
            if job["status"] in ("done", "failed"):
                return
            await asyncio.sleep(1)

    return StreamingResponse(_generator(), media_type="text/event-stream")


@app.get("/api/vaults/{vault_name}/jobs")
async def api_list_jobs(vault_name: str) -> dict[str, Any]:
    """Return the 20 most recent ingest jobs for a vault."""
    _, vpath = _get_vault(vault_name)
    conn = get_db(vpath)
    try:
        jobs = list_jobs(conn)
    finally:
        conn.close()
    return {"jobs": jobs}
```

---

### Step 5 — Update the watcher path to create job records

**File:** `main_server.py:_run_ingest`

Replace the current `ingest_queued` call with a per-file `_run_ingest_job` call so that
auto-ingest items also appear in the jobs list:

```python
def _run_ingest(vpath: Path, vname: str, source: str) -> None:
    job_id = str(uuid.uuid4())
    _run_ingest_job_via_server(vpath, vname, source, job_id, dry_run=False)
```

Import `_run_ingest_job` from `core.server` to share the implementation.

---

### Step 6 — Update the dashboard UI

**File:** `app/static/` (JavaScript)

- Change the ingest form submit handler to `POST` and read back `{ job_id }` from the 202.
- Open an `EventSource` on `/api/vaults/{vault}/jobs/{job_id}/stream`.
- Display a progress row in the UI: source name, status badge (pending → running → done/failed),
  elapsed time, and pages written on completion.
- Add a "Recent jobs" panel populated from `GET /api/vaults/{vault}/jobs`.

---

### Step 7 — Write tests

**File:** `tests/test_server.py`

- `test_api_ingest_returns_202_with_job_id`: verify response is 202, body has `job_id`
- `test_api_get_job_returns_pending`: immediately after POST, job is pending
- `test_api_get_job_returns_done`: after executor completes mock ingest, job is done
- `test_api_get_job_404`: unknown job_id → 404
- `test_api_list_jobs`: returns list of recent jobs

**File:** `tests/test_database.py`

- `test_create_job_and_get_job`: round-trip
- `test_update_job_status_sets_fields`: verify finished_at is set on done/failed
- `test_list_jobs_newest_first`: verify ordering

---

### Step 8 — Documentation

- `CLAUDE.md` — update the API route table in the Project Structure section; document that
  `/api/vaults/{vault}/ingest` now returns 202 (breaking change from 200)
- `core/README.md` — update `server.py` route table with the three new job endpoints

---

### Estimated scope

| Area | Files | New/changed |
|---|---|---|
| DB | `core/database.py` | 1 new table, 4 new functions |
| Server | `core/server.py` | `api_ingest` rewritten, 3 new endpoints, executor registry |
| Startup | `main_server.py` | executor registration, watcher path updated |
| Frontend | `app/static/` | ingest form + job status panel |
| Tests | `tests/test_server.py`, `tests/test_database.py` | ~9 new test cases |
| Docs | `CLAUDE.md`, `core/README.md` | — |
