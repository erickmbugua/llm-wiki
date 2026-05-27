---
description: Runs the project's mandatory 5-step QA sequence (ruff → mypy → pyright → pytest) in order and interprets failures against known gotchas. Use when the user says a task is done, asks to verify a change, or wants to check for errors.
allowed-tools: Bash(.venv/bin/ruff *) Bash(.venv/bin/mypy) Bash(.venv/bin/mypy *) Bash(.venv/bin/pyright) Bash(.venv/bin/pytest *)
---

Run each command in order. Stop immediately if any exits non-zero — do not run subsequent steps.

## Sequence

1. `.venv/bin/ruff check --fix .`
2. `.venv/bin/ruff format .`
3. `.venv/bin/mypy`
4. `.venv/bin/pyright`
5. `.venv/bin/pytest tests/ -q`

## Interpreting failures

Before reporting an error as a bug to fix, check if it matches a known project gotcha:

**pyright — litellm response type**
`litellm.completion()` returns `ModelResponse | CustomStreamWrapper`. Accessing `.choices` on the union is expected to fail pyright. Suppress with `# pyright: ignore[reportAttributeAccessIssue]`. Similarly, `.content` can be `None` — always use `or ""` before `.strip()`.

**pyright — Unknown not narrowed by truthiness guards**
After `if not raw: raise ...`, pyright still sees `str | Unknown | None` in the union. Fix with an explicit cast: `raw = str(raw)`. This is not a logic bug — it is a pyright limitation with litellm's opaque return type.

**pyright / mypy — bare `dict` in dataclass `field()`**
`field(default_factory=dict)` causes pyright to infer `dict[Unknown, Unknown]`, ignoring the annotation. Use `field(default_factory=lambda: {})` — pyright then defers to the annotation.

**mypy — missing type annotations**
All function signatures in every file (including tests) must carry full type annotations. `check_untyped_defs`, `disallow_untyped_defs`, and `disallow_incomplete_defs` are all active.

**Tests — patch target mismatch**
Patches must target the name at the point of *use*, not the point of *definition*. For example, patch `core.server.ingest_source`, not `core.ingest.ingest_source`. Private names (`_name`) must never be patched across module boundaries.

**Tests — nullable return not guarded**
`get_page()` and similar functions return `T | None`. Always assert `is not None` before subscripting. mypy will catch this as `error: Item "None" of "X | None" has no attribute "Y"`.

**Tests — wrong test tier running**
- Unit tests only: `pytest -m "not integration and not e2e" -q`
- Integration only (LLM stubbed, no Ollama): `pytest -m integration -q`
- E2E only (real subprocess, TCP mock LLM): `pytest -m e2e -q`
- Full suite: `pytest tests/ -q`

If e2e tests fail with connection errors, check that `pytest-httpserver` is installed and that the mock server fixture is being used.

**Tests — lint always calls the LLM (e2e)**
Even on a freshly initialised vault, `lint_vault` indexes root wiki files created by `init_vault`. The "no pages" early-return is never triggered. E2E lint tests always need `mock_llm_server`.

**pyright — third-party stubs**
Before suppressing an import error, check if stubs exist: `uv pip index versions types-<packagename>`. If stubs exist, install them with `uv add --dev types-<name>` rather than suppressing. Only use `# pyright: ignore[reportMissingImports]` for libraries with no stubs (litellm, mcp, frontmatter).

## Reporting

If all 5 steps pass: report "QA passed — all 5 steps clean." with the pytest summary line.

If a step fails: report which step number failed, paste the relevant error lines, identify the matching known gotcha if one applies, and state the fix. Do not report a known gotcha as an "issue to investigate" — it has a known fix; apply it.
