# P2 — Lint Coverage: Beyond 8 Pages

## Problem Statement

`CONTRADICTION_SAMPLE = 8` in `core/lint.py:18` caps the LLM quality review at 8 pages
per lint run. The sampling strategy picks the 8 Sources/Concepts pages with the longest
summaries — prioritizing dense, well-developed pages.

For a vault with 200 pages this means the lint LLM call covers 4% of the vault. Contradictions
between pages outside the top-8 will never be found. The sampling bias toward long-summary
pages means newly ingested, shorter pages are systematically excluded — exactly the pages
most likely to have errors or incomplete links.

Additionally, running one LLM call over 8 pages at once is not an effective use of the
model's attention. A 7B model given 8 pages of 1200 chars each (9,600 chars of context)
will spread its attention thin. Smaller, focused batches produce higher-quality reports.

The root cause is that lint was designed as a single LLM call covering a static sample.
A multi-pass strategy with different sampling criteria would give much broader coverage
within the same token budget.

---

## Implementation Plan

### Strategy: rotating multi-pass lint with coverage tracking

Run lint in multiple small focused passes, each using a different slice of the vault.
Track which pages have been linted and when. Prioritize unlinted and stale-linted pages in
future runs.

---

### Step 1 — Add `last_linted` column to `pages` table

This requires the schema migration system from `p2-schema-migrations.md` to be in place.

**Migration:** Add `last_linted REAL` (nullable Unix timestamp) to `pages`.

```python
def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE pages ADD COLUMN last_linted REAL")
```

Update `SCHEMA_VERSION = 2` and add `(1, 2, _migrate_v1_to_v2)` to `_MIGRATIONS`.

---

### Step 2 — Change sampling to prioritize unlinted and diverse pages

**File:** `core/lint.py:_llm_lint`

Replace the current top-8 longest-summary sample with a coverage-aware selector:

```python
def _select_lint_sample(
    pages: list[dict[str, Any]],
    batch_size: int = 6,
) -> list[dict[str, Any]]:
    """Select a batch of pages to lint, prioritizing coverage over recency.

    Ordering:
    1. Pages never linted (last_linted IS NULL) — Sources and Concepts first
    2. Pages linted longest ago (oldest last_linted)
    3. Within each tier, shorter summaries first (less-developed pages get attention)

    Args:
        pages: All pages from the vault database.
        batch_size: Number of pages to include in this lint pass.

    Returns:
        Selected pages, up to batch_size.
    """
    import math

    def sort_key(p: dict[str, Any]) -> tuple[int, float, int]:
        # tier 0 = never linted, tier 1 = linted before
        tier = 0 if p.get("last_linted") is None else 1
        # use negative mtime as a secondary sort (oldest = smallest timestamp = first)
        age = p.get("last_linted") or 0.0
        # prefer shorter summaries (less developed)
        summary_len = len(p.get("summary") or "")
        return (tier, age, summary_len)

    # Filter to Sources and Concepts; fall back to all pages if insufficient
    candidates = [p for p in pages if p.get("category") in ("Sources", "Concepts")]
    if len(candidates) < batch_size:
        candidates = list(pages)

    return sorted(candidates, key=sort_key)[:batch_size]
```

---

### Step 3 — Update `last_linted` after each lint pass

**File:** `core/lint.py:_llm_lint`

After the LLM call completes, update `last_linted` for the sampled pages:

```python
from datetime import datetime, timezone

def _llm_lint(vault_path: Path, wiki_root: Path, pages: list[dict]) -> str:
    ...
    sample = _select_lint_sample(pages)
    ...
    # [existing prompt build and LLM call]
    report = ...

    # Mark the sampled pages as linted
    now = datetime.now(timezone.utc).timestamp()
    conn = get_db(vault_path)
    try:
        conn.executemany(
            "UPDATE pages SET last_linted = ? WHERE file_path = ?",
            [(now, p["file_path"]) for p in sample],
        )
        conn.commit()
    finally:
        conn.close()

    return report
```

---

### Step 4 — Add coverage statistics to the lint report

**File:** `core/lint.py:_save_lint_report`

Add a coverage section at the top of the report showing how much of the vault has been
linted and when it was last fully covered:

```python
def _coverage_stats(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute lint coverage statistics across the vault.

    Returns:
        Dict with keys: total, linted, unlinted, coverage_pct, oldest_linted_days.
    """
    total = len(pages)
    linted = [p for p in pages if p.get("last_linted") is not None]
    now = datetime.now(timezone.utc).timestamp()
    oldest = min((p["last_linted"] for p in linted), default=None)
    oldest_days = int((now - oldest) / 86400) if oldest else None
    return {
        "total": total,
        "linted": len(linted),
        "unlinted": total - len(linted),
        "coverage_pct": round(100 * len(linted) / total) if total else 0,
        "oldest_linted_days": oldest_days,
    }
```

Include this in the report header:

```markdown
## Lint Coverage
- Pages covered this pass: 6 of 200 (3%)
- Total ever linted: 142 of 200 (71%)
- Unlinted pages: 58
- Oldest linted page: 14 days ago
```

---

### Step 5 — Add a `--full` flag for exhaustive lint

**File:** `main.py` and `core/server.py`

For cases where the user wants to lint the entire vault (e.g., before a review), add
`lint --full` which runs multiple batches until all pages are covered:

```python
@cli.command()
@click.option("--vault", "-v", default=None)
@click.option("--full", is_flag=True, help="Lint all pages in multiple passes (slow)")
def lint(vault: str | None, full: bool):
    ...
    result = lint_vault(vpath, full=full)
```

**File:** `core/lint.py:lint_vault`

```python
def lint_vault(vault_path: Path, full: bool = False) -> dict[str, Any]:
    ...
    llm_reports = _llm_lint(vault_path, wiki_root, pages, full=full)
    ...
```

**File:** `core/lint.py:_llm_lint`

When `full=True`, loop over the entire pages list in batches of `batch_size=6`, making one
LLM call per batch, concatenating the reports. Add a per-batch delay of 2 seconds to avoid
overwhelming a local model:

```python
import time

def _llm_lint(
    vault_path: Path, wiki_root: Path, pages: list[dict], full: bool = False
) -> str:
    if not pages:
        return "No pages to lint."

    if full:
        all_reports: list[str] = []
        for i in range(0, len(pages), CONTRADICTION_SAMPLE):
            batch = pages[i : i + CONTRADICTION_SAMPLE]
            all_reports.append(_lint_batch(vault_path, wiki_root, batch))
            if i + CONTRADICTION_SAMPLE < len(pages):
                time.sleep(2)   # breathing room for local models
        return "\n\n---\n\n".join(all_reports)
    else:
        sample = _select_lint_sample(pages)
        return _lint_batch(vault_path, wiki_root, sample)
```

Extract the per-batch LLM call into `_lint_batch(vault_path, wiki_root, sample) -> str`.

---

### Step 6 — Write tests

**File:** `tests/test_lint.py`

- `test_select_lint_sample_prioritizes_unlinted`: mix of linted/unlinted → unlinted first
- `test_select_lint_sample_falls_back_when_few_candidates`: only 2 Concepts pages →
  falls back to include Entities
- `test_select_lint_sample_respects_batch_size`: 100 pages → returns exactly 6
- `test_llm_lint_updates_last_linted`: after lint pass, sampled pages have `last_linted` set
- `test_coverage_stats_correct`: assert coverage_pct = 0 for fresh vault
- `test_lint_vault_full_flag_calls_llm_per_batch`: 13 pages, batch_size=6 → 3 LLM calls

---

### Step 7 — Documentation

- `CLAUDE.md` — update `CONTRADICTION_SAMPLE` mention to reflect new coverage-aware sampling
- `core/README.md` — update `lint.py` description

---

### Estimated scope

| Area | Files | Changes |
|---|---|---|
| DB | `core/database.py` | `last_linted` column via migration |
| Lint | `core/lint.py` | `_select_lint_sample`, `_coverage_stats`, `_lint_batch`, `--full` path |
| CLI | `main.py` | `--full` flag |
| Server | `core/server.py` | `full` parameter on `api_lint` |
| Tests | `tests/test_lint.py` | 6 new test cases |
| Docs | `CLAUDE.md`, `core/README.md` | — |

Depends on `p2-schema-migrations.md` for the `last_linted` column migration.
