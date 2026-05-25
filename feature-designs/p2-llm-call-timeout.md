# P2 — LLM Call Timeout

## Problem Statement

All three `litellm.completion()` calls in the codebase have no `timeout` argument:

- `core/ingest.py:102` — ingest prompt
- `core/ingest.py` (retry path, once added per p1-json-parsing-resilience)
- `core/query.py:52` — query prompt
- `core/lint.py:151` — lint quality review

If the Ollama server becomes unresponsive (process OOM-killed, GPU memory exhausted, model
loading stalled), `litellm.completion()` will block indefinitely. The call runs inside a
`ThreadPoolExecutor(max_workers=1)` per vault. A hung call permanently blocks that executor
— all subsequent auto-ingest events pile up in the queue in `"pending"` state and are never
processed, with no error surfaced to the user.

The only recovery is a server restart. On a MacBook M1 with 16GB unified memory, Ollama
OOM events are not rare when a 7B model is loaded alongside a browser, IDE, and other
applications.

---

## Implementation Plan

### Step 1 — Add timeout config field

**File:** `core/config.py`

Add `llm_timeout_seconds: int = 180` to `GlobalConfig` (and optionally `VaultConfig` for
per-vault override):

```python
@dataclass
class GlobalConfig:
    ...
    llm_timeout_seconds: int = 180   # max seconds to wait for a single LLM completion call
```

180 seconds (3 minutes) is conservative for a local 7B model. Cloud models (Claude, GPT-4)
should complete well under 30 seconds.

Extend `resolve_vault_config` (from `p1-config-loading.md`) to include this field:

```python
@dataclass
class ResolvedConfig:
    model: str
    context_chars: int
    llm_timeout_seconds: int
```

Add a CLI setter:

```
llm-wiki set-timeout <seconds> [--vault VAULT]
```

---

### Step 2 — Add the timeout to all `litellm.completion` calls

**File:** `core/ingest.py:ingest_source`

```python
response = litellm.completion(
    model=cfg.model,
    messages=[{"role": "user", "content": prompt}],
    temperature=0.0,
    timeout=cfg.llm_timeout_seconds,
)
```

**File:** `core/query.py:query_wiki`

```python
cfg = resolve_vault_config(vault_path)
response = litellm.completion(
    model=cfg.model,
    messages=[{"role": "user", "content": prompt}],
    temperature=0.3,
    timeout=cfg.llm_timeout_seconds,
)
```

**File:** `core/lint.py:_llm_lint`

```python
cfg = resolve_vault_config(vault_path)
response = litellm.completion(
    model=cfg.model,
    messages=[{"role": "user", "content": prompt}],
    temperature=0.2,
    timeout=cfg.llm_timeout_seconds,
)
```

---

### Step 3 — Handle timeout errors explicitly in the queue processor

**File:** `core/ingest.py:ingest_queued`

The generic `except Exception` in the ingest loop already catches timeout errors and marks
items `"failed"`. But the error message from litellm's timeout exception is not always
user-friendly. Catch it specifically to improve the stored error message:

```python
except TimeoutError as e:
    mark_queue_item(conn, fp, "failed", f"LLM timed out after {cfg.llm_timeout_seconds}s — "
                                         "check that Ollama is running and the model is loaded.")
    log.error("LLM timeout for %s after %ds", fp, cfg.llm_timeout_seconds)
    results.append({"file": fp, "status": "failed", "error": str(e)})
```

litellm raises `litellm.exceptions.Timeout` (a subclass of `TimeoutError`) on timeout.

---

### Step 4 — Add timeout to the Ollama preflight check

**File:** `core/ingest.py:_check_ollama`

The `requests.get(f"{base_url}/api/tags", timeout=3)` already has a 3-second timeout —
this is correct. No change needed here.

---

### Step 5 — Write tests

**File:** `tests/test_ingest.py`

- `test_ingest_source_passes_timeout_to_litellm`: mock `litellm.completion` → verify
  `timeout` kwarg matches `cfg.llm_timeout_seconds`
- `test_ingest_queued_marks_failed_on_timeout`: mock `litellm.completion` to raise
  `TimeoutError` → item is marked `"failed"` with a readable error message

**File:** `tests/test_query.py`

- `test_query_wiki_passes_timeout`: verify timeout kwarg is forwarded

**File:** `tests/test_lint.py`

- `test_lint_vault_passes_timeout`: verify timeout kwarg is forwarded

---

### Step 6 — Documentation

- `CLAUDE.md` Known Gotchas — add a note that all completion calls use
  `cfg.llm_timeout_seconds` (default 180s) and that the timeout can be adjusted via
  `llm-wiki set-timeout` for slower machines or faster cloud models
- `CLAUDE.md` Ollama section — add a note that Ollama OOM events will now surface as
  queue `"failed"` items rather than hung executors

---

### Estimated scope

| Area | Files | Changes |
|---|---|---|
| Config | `core/config.py`, `main.py` | `llm_timeout_seconds` field, 1 CLI command |
| Ingest | `core/ingest.py` | timeout kwarg + explicit TimeoutError handler |
| Query | `core/query.py` | timeout kwarg |
| Lint | `core/lint.py` | timeout kwarg |
| Tests | 3 test files | ~5 new test cases |
| Docs | `CLAUDE.md` | Known Gotchas, Ollama section |

No new dependencies. litellm already maps the `timeout` parameter to the underlying HTTP
client for both Ollama and cloud providers.
