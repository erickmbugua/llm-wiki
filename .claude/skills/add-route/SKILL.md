---
description: Add a new FastAPI endpoint to core/server.py. Enforces def-vs-async-def threading rules, 200-vs-202 status codes, vault path resolution pattern, and e2e test requirements. Use when the user asks to add an endpoint, route, or API method.
argument-hint: <route description>
---

Route to add: $ARGUMENTS

---

## Step 1 — Classify the endpoint

Answer both questions before writing any code.

**Does this endpoint call the LLM (`ingest_source`, `query_wiki`, `lint_vault`)?**
- **Yes → `def`, not `async def`**. FastAPI dispatches plain `def` endpoints to anyio's thread pool, keeping the event loop free during 30–120 s LLM calls. Using `async def` silently freezes the server for every concurrent caller during that window.
- **No (fast I/O only) → `async def`** is correct.

**Does this endpoint kick off long-running background work?**
- **Yes → HTTP 202** with `{"job_id": "<uuid>", "status": "pending"}`. Create a record via `create_job()`, submit work to the vault's executor via `_get_vault_executor(vault_name)`, and ensure a companion `GET /api/vaults/{name}/jobs/{job_id}` poll route exists.
- **No → HTTP 200** with the result directly.

---

## Step 2 — Resolve the vault path

All vault-scoped endpoints must resolve the vault path from the `{name}` path parameter using the standard pattern:

```python
config = GlobalConfig.load()
if name not in config.vaults:
    raise HTTPException(status_code=404, detail=f"Vault '{name}' not found")
vault_path = Path(config.vaults[name])
```

Never accept a raw filesystem path from the caller.

---

## Step 3 — Implement the endpoint

- Full type annotations on all parameters and the return type
- Docstring: one-line description + `Args:` + `Returns:` + `Raises:` for every `HTTPException`
- Keep the endpoint thin: delegate to a `core/` function; no business logic in `server.py`

---

## Step 4 — Update the API surface table

Add the new route to the API surface table in `core/README.md` (Method | Path | Purpose).

---

## Step 5 — Write tests

**E2E test** (required for every new route) in `tests/e2e/test_http.py`:
- Marker: `@pytest.mark.e2e`
- Fixtures: `live_server` + `vault_env` from `tests/e2e/conftest.py`; add `mock_llm_server` if the endpoint calls the LLM
- Cover: happy path, vault-not-found (404), at least one malformed-input case

**Unit test** in `tests/test_server.py` (uses `TestClient` — no real uvicorn needed):
- Covers request validation and error paths that are awkward to hit via a live subprocess

---

## Step 6 — Run QA

Run `/qa` before declaring the task complete.
