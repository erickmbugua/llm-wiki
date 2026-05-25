# P1 — LLM JSON Parsing Resilience

## Problem Statement

`_parse_llm_json` in `core/ingest.py:458` does two things: strip markdown fences and call
`json.loads`. Any output from the LLM that does not survive `json.loads` causes a
`ValueError`, which propagates out of `ingest_source`, gets caught in `ingest_queued`, and
marks the queue item `"failed"`. The user must manually re-drop the file to retry.

Small and quantized local models (3B–7B) routinely produce near-valid JSON that fails
`json.loads` for one of these reasons:
- Trailing comma in the last array or object element
- Single-quoted string values instead of double-quoted
- Unescaped apostrophes inside string values (`it's` instead of `it\'s`)
- A missing closing `}` when the model runs out of context
- Prose sentences before or after the JSON block (the fence-strip regex catches markdown
  fences but not all prose wrapping)

The failure rate on a 7B quantized model is roughly 10–20% of ingests, meaning one in five
or ten documents fails silently into the queue without a page being written.

Additionally, the ingest prompt uses `temperature=0.2` for all models, including small local
ones. For structured JSON output, any temperature above 0 increases the probability of
structural deviation with no benefit — the "creativity" is unwanted here.

---

## Implementation Plan

### Step 1 — Add `json-repair` dependency

**File:** `pyproject.toml`

```toml
dependencies = [
    ...
    "json-repair>=0.28",
]
```

`json-repair` is a 5KB pure-Python library with no transitive dependencies that handles all
the common LLM JSON failure modes listed above. It makes a best-effort repair pass before
falling through to standard parsing.

---

### Step 2 — Rewrite `_parse_llm_json` with a repair fallback

**File:** `core/ingest.py:_parse_llm_json`

```python
def _parse_llm_json(raw: str) -> dict[str, Any]:
    """Parse the LLM's JSON response, attempting repair before failing.

    Strips markdown fences, tries json.loads, then json_repair.repair as a fallback.
    Logs a warning when repair is needed so the user can see which models are unreliable.

    Args:
        raw: Raw string returned by the LLM.

    Returns:
        Parsed dict containing at least ``source_page`` and ``page_updates`` keys.

    Raises:
        ValueError: The string could not be parsed or repaired into valid JSON,
            or the repaired result is missing the ``source_page`` key.
    """
    from json_repair import repair_json

    # Strip markdown fences and leading/trailing prose
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip(), flags=re.MULTILINE)

    # Find the outermost JSON object — handles prose before/after the JSON block
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        cleaned = match.group(0)

    # First attempt: standard parse (fast path, works for well-formed output)
    try:
        data: dict[str, Any] = json.loads(cleaned)
    except json.JSONDecodeError:
        # Second attempt: repair then parse
        log.warning(
            "LLM output was not valid JSON; attempting repair. "
            "Consider using a larger model or structured output."
        )
        try:
            repaired = repair_json(cleaned, return_objects=False)
            data = json.loads(repaired)
        except (json.JSONDecodeError, ValueError) as e:
            raise ValueError(
                f"LLM returned JSON that could not be repaired: {e}\n\nRaw output:\n{raw[:500]}"
            ) from e

    if "source_page" not in data:
        raise ValueError("LLM response missing 'source_page' key")
    data.setdefault("page_updates", [])
    return data
```

---

### Step 3 — Set ingest temperature to 0.0

**File:** `core/ingest.py:ingest_source`

Change:
```python
response = litellm.completion(
    model=model,
    messages=[{"role": "user", "content": prompt}],
    temperature=0.2,
)
```

To:
```python
response = litellm.completion(
    model=model,
    messages=[{"role": "user", "content": prompt}],
    temperature=0.0,
)
```

Rationale: the ingest prompt requests deterministic structured JSON. Temperature above 0
adds randomness without benefit and increases structural deviation probability.

Keep `temperature=0.3` for `query_wiki` (answers benefit from some variation) and
`temperature=0.2` for `lint_vault` (quality review needs some flexibility).

---

### Step 4 — Add one retry on repair failure

**File:** `core/ingest.py:ingest_source`

After the `_parse_llm_json` call, if a `ValueError` is raised, make one retry with a
simplified prompt that constrains the output more tightly:

```python
try:
    result = _parse_llm_json(raw)
except ValueError:
    log.warning("JSON parse failed for '%s'; retrying with constrained prompt", display_name)
    prompt_retry = _build_ingest_prompt_strict(vault_name, schema, related, display_name, text)
    response_retry = litellm.completion(
        model=model,
        messages=[{"role": "user", "content": prompt_retry}],
        temperature=0.0,
    )
    raw_retry = str(response_retry.choices[0].message.content or "")  # pyright: ignore[reportAttributeAccessIssue]
    result = _parse_llm_json(raw_retry)   # let ValueError propagate on second failure
```

The strict prompt variant (`_build_ingest_prompt_strict`) is identical to the normal prompt
but with an added explicit constraint at the top:

```
IMPORTANT: Your entire response must be a single valid JSON object.
Do not write any text before or after the JSON.
Do not use markdown code fences.
Start your response with { and end with }.
```

---

### Step 5 — Write tests

**File:** `tests/test_ingest.py`

- `test_parse_llm_json_valid`: clean JSON → parsed correctly (existing test, keep)
- `test_parse_llm_json_markdown_fences`: JSON wrapped in ```json ... ``` → parsed correctly
- `test_parse_llm_json_trailing_comma`: `{"source_page": {...},}` → repaired and parsed
- `test_parse_llm_json_single_quotes`: single-quoted JSON → repaired and parsed
- `test_parse_llm_json_prose_before_json`: "Here is the JSON: {...}" → JSON extracted
- `test_parse_llm_json_unrepairable`: completely non-JSON string → raises ValueError
- `test_parse_llm_json_missing_source_page_key`: valid JSON but missing key → raises ValueError

---

### Step 6 — Documentation

- `CLAUDE.md` Known Gotchas — update the litellm section to note that ingest now uses
  `temperature=0.0` and add a note about `json-repair` handling near-valid output
- `pyproject.toml` — `json-repair` is a runtime (not dev) dependency; add to `[project]
  dependencies`

---

### Estimated scope

| Area | Files | Changes |
|---|---|---|
| Parsing | `core/ingest.py` | `_parse_llm_json` rewritten, temperature changed, 1 retry path |
| Deps | `pyproject.toml` | +1 dependency (`json-repair`) |
| Tests | `tests/test_ingest.py` | 6 new test cases (1 existing updated) |
| Docs | `CLAUDE.md` | Known Gotchas update |

No schema changes. No new CLI commands. Fully backward-compatible.
