---
description: Implement a new feature following the project's mandatory 4-gate workflow (requirements → options → plan → TDD). Use when the user asks to add, build, or implement something new. Does not write any implementation code until Gate 4 is explicitly unlocked.
argument-hint: <feature description>
allowed-tools: WebSearch WebFetch
---

Feature request: $ARGUMENTS

---

## Gate 1 — Requirements gathering

Ask targeted questions to fully understand the requirement. If the feature touches external APIs, libraries, or architectural patterns not already in the codebase, run WebSearch / WebFetch research first and bring findings back before asking questions.

Questions to answer before moving on:
- What exact behaviour does the user want?
- Which existing modules does this touch? (`core/ingest.py`, `core/server.py`, `core/db/`, etc.)
- Does it require a schema change (new table, new column, migration)?
- Does it affect the MCP server tool list?
- Does it affect CLI commands in `main.py`?
- Does it need a new config field (GlobalConfig / VaultConfig / resolve_* function)?
- What are the failure modes — what should happen when the LLM is unavailable, the source is malformed, the vault doesn't exist?

**Do not proceed to Gate 2 until all relevant questions are answered.**

---

## Gate 2 — Options with trade-offs

Present 2–4 concrete implementation approaches. For each, state:
- What it does (one paragraph)
- Key trade-off: complexity, performance, testability, or maintainability
- Your recommendation and the reason

**Explicitly ask the user to choose an approach. Do not proceed to Gate 3 until they select one.**

---

## Gate 3 — Implementation plan

Once an approach is chosen, produce a step-by-step plan that covers:

**Code**
- Files to create or modify (exact paths)
- New functions / classes with their full signatures and docstring contract
- Schema changes and whether a migration is needed
- Any new config fields and the resolver function changes

**Tests (TDD — tests are written before implementation)**
- Test file(s) and fixture additions to `tests/conftest.py` or tier-specific conftest
- At minimum: one happy-path test and one edge/error test per new function
- Integration test if the feature crosses module boundaries (mark `@pytest.mark.integration`)
- E2E test if it adds a CLI command or REST route (mark `@pytest.mark.e2e`)

**Documentation (same commit as the code — no follow-ups)**
- `CLAUDE.md` sections to update: Known Gotchas, Project Structure, Toolchain, test counts
- Folder `README.md` files: module map, API table, CLI table, config field list
- Docstrings: every new public function needs description + Args + Returns + Raises

**Explicitly ask: "Does this plan look right? Should I proceed?" Do not write any code until the user says yes.**

---

## Gate 4 — TDD implementation

Only after the user approves the plan:

1. Write the failing tests first — run them to confirm they fail for the right reason
2. Write the minimum implementation to make the tests pass
3. Refactor if needed, keeping tests green
4. Update all documentation in the same pass (not as a follow-up)
5. Run `/qa` to verify all 5 QA steps pass before declaring the feature complete

### Code quality rules to enforce
- All new function signatures must have full type annotations
- Never use bare `dict` or `list[dict]` — always parameterise (`dict[str, Any]`, `list[dict[str, Any]]`)
- Private helpers (`_name`) must not be imported across modules
- `def` (not `async def`) for any FastAPI endpoint that calls the LLM — FastAPI dispatches `def` endpoints to anyio's thread pool, keeping the event loop free
- Patch at the point of use in tests, not the point of definition
- Every new public function must have a docstring
