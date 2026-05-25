# P1 — Config Loading: Double-Read and Stale Cache

## Problem Statement

There are two distinct but related config loading problems.

### Problem A: Double-read on every LLM operation

`resolve_model(vault_path)` in `core/config.py:125` calls `GlobalConfig.load()`, which reads
`~/.llm-wiki/config.json` from disk. `resolve_context_chars(vault_path)` in `core/config.py:144`
also calls `GlobalConfig.load()` independently.

Both are called at the top of `ingest_source` before any LLM work begins:

```python
# core/ingest.py:87-96
char_limit = resolve_context_chars(vault_path)   # → GlobalConfig.load() + VaultConfig.load()
...
model = resolve_model(vault_path)                 # → GlobalConfig.load() + VaultConfig.load() again
```

This means four JSON file reads (two global, two vault config) plus four `json.loads` parses
happen before the first LLM call. On a fast SSD this is sub-millisecond, but it is redundant
work that accumulates across every ingest, query, and lint operation.

### Problem B: Server config cache is never invalidated

`_get_config()` in `core/server.py:28` caches `GlobalConfig` in a module-level global
(`_config_cache`) that is never automatically refreshed. If a user runs `llm-wiki set-model
claude-sonnet-4-6` while the server is running, the server continues using the old model
string for all subsequent ingest and query calls until it is restarted.

There is a `_reset_config_cache()` function but it is never called from any endpoint — it
exists only as a test helper.

---

## Implementation Plan

### Fix A — Combine resolution into a single function

**File:** `core/config.py`

Add a new unified resolver that loads each config file exactly once:

```python
from dataclasses import dataclass

@dataclass
class ResolvedConfig:
    """Effective config for a single vault operation — all fields resolved."""
    model: str
    context_chars: int
    # Extend here as new per-operation config fields are added

def resolve_vault_config(vault_path: Path | None = None) -> ResolvedConfig:
    """Return the effective model and context_chars for a vault in a single load.

    Loads GlobalConfig and VaultConfig exactly once each, applying the vault-level
    override when present.

    Args:
        vault_path: Root directory of the vault. When None, only global config is used.

    Returns:
        A ResolvedConfig with all fields resolved through the three-level priority chain.
    """
    global_cfg = GlobalConfig.load()
    model = global_cfg.model
    context_chars = global_cfg.context_chars

    if vault_path is not None:
        vault_cfg = VaultConfig.load(vault_path)
        if vault_cfg.model:
            model = vault_cfg.model
        if vault_cfg.context_chars is not None:
            context_chars = vault_cfg.context_chars

    return ResolvedConfig(model=model, context_chars=context_chars)
```

Keep `resolve_model` and `resolve_context_chars` as thin wrappers around
`resolve_vault_config` so call sites in `lint.py` and `query.py` continue to work unchanged:

```python
def resolve_model(vault_path: Path | None = None) -> str:
    return resolve_vault_config(vault_path).model

def resolve_context_chars(vault_path: Path | None = None) -> int:
    return resolve_vault_config(vault_path).context_chars
```

**File:** `core/ingest.py:ingest_source`

Replace the two separate calls with one:

```python
cfg = resolve_vault_config(vault_path)
char_limit = cfg.context_chars
...
model = cfg.model
```

---

### Fix B — File-mtime cache invalidation for the server config

**File:** `core/server.py`

Replace the simple `_config_cache: GlobalConfig | None = None` global with a mtime-aware
cache:

```python
import os

_config_cache: GlobalConfig | None = None
_config_mtime: float = 0.0


def _get_config() -> GlobalConfig:
    """Return GlobalConfig, reloading if the file has been modified since last load.

    Uses the config file's mtime as a cheap invalidation signal. The stat call adds
    ~0.05ms per request but is far cheaper than re-parsing JSON on every request.
    """
    global _config_cache, _config_mtime

    config_path = GLOBAL_CONFIG_FILE   # import from core.config
    try:
        current_mtime = os.stat(config_path).st_mtime
    except FileNotFoundError:
        current_mtime = 0.0

    if _config_cache is None or current_mtime != _config_mtime:
        _config_cache = GlobalConfig.load()
        _config_mtime = current_mtime

    return _config_cache
```

Import `GLOBAL_CONFIG_FILE` from `core.config`.

The `_reset_config_cache()` function can remain for tests but is no longer needed for
correctness — the mtime check will catch any mutation within one request cycle.

---

### Step 3 — Write tests

**File:** `tests/test_config.py`

- `test_resolve_vault_config_uses_vault_override`: vault config sets model → `ResolvedConfig`
  uses vault model
- `test_resolve_vault_config_falls_back_to_global`: no vault override → global model used
- `test_resolve_vault_config_single_load`: mock `GlobalConfig.load` and `VaultConfig.load`
  → each called exactly once
- `test_resolve_model_delegates_to_resolve_vault_config`: verify `resolve_model` returns
  `resolve_vault_config(...).model`

**File:** `tests/test_server.py`

- `test_get_config_reloads_on_mtime_change`: write config, call `_get_config()`, update file,
  call again → second call returns updated config
- `test_get_config_returns_cached_when_unchanged`: two calls without file change → `load()`
  called only once

**File:** `tests/test_ingest.py`

- `test_ingest_source_calls_resolve_vault_config_once`: mock `resolve_vault_config` →
  verify called exactly once (not once per field)

---

### Step 4 — Documentation

- `CLAUDE.md` Known Gotchas — remove the entry about `_reset_config_cache()` (no longer
  needed by callers); add a note about `resolve_vault_config` as the preferred single-call
  resolver
- Docstrings on `resolve_vault_config` and the new `_get_config`

---

### Estimated scope

| Area | Files | Changes |
|---|---|---|
| Config | `core/config.py` | `ResolvedConfig` dataclass, `resolve_vault_config` |
| Ingest | `core/ingest.py` | 2 calls → 1 call to `resolve_vault_config` |
| Server | `core/server.py` | mtime-aware `_get_config` |
| Tests | `tests/test_config.py`, `tests/test_server.py`, `tests/test_ingest.py` | ~7 tests |
| Docs | `CLAUDE.md` | Known Gotchas update |

No new dependencies. No schema changes. Fully backward-compatible.
