---
description: Add a new configuration field to GlobalConfig and VaultConfig, including the resolve_* function and CLI setter command that complete the three-level priority chain. Use when the user asks to add a config option, setting, or tunable parameter.
argument-hint: <field name>
---

Field to add: $ARGUMENTS

---

## The pattern

Every user-configurable value in this project follows a strict four-step structure. Apply all four steps — omitting any one leaves the config partially wired and breaks the resolver tests.

---

## Step 1 — GlobalConfig (system-wide default)

In `core/config.py`, add the field with a sensible hardcoded default:

```python
@dataclass
class GlobalConfig:
    ...
    new_field: <type> = <default>
```

For mutable defaults use `field(default_factory=lambda: ...)`, never `field(default_factory=dict)` — pyright infers `dict[Unknown, Unknown]` from the bare form and ignores the annotation.

---

## Step 2 — VaultConfig (per-vault override)

Add a nullable field so vaults can opt in to a different value:

```python
@dataclass
class VaultConfig:
    ...
    new_field: <type> | None = None
```

---

## Step 3 — Resolver function

Add `resolve_new_field(vault_path: Path) -> <type>` in `core/config.py`. The function must implement the three-level chain: vault config → global config → hardcoded default.

```python
def resolve_new_field(vault_path: Path) -> <type>:
    """Return the effective <field> for vault_path (vault > global > hardcoded default).

    Args:
        vault_path: Absolute path to the vault root.

    Returns:
        The resolved <field> value.
    """
    vault_cfg = VaultConfig.load(vault_path)
    if vault_cfg.new_field is not None:
        return vault_cfg.new_field
    global_cfg = GlobalConfig.load()
    if global_cfg.new_field != <sentinel_or_default>:
        return global_cfg.new_field
    return <hardcoded_default>
```

Export it from `core/config.py`'s `__all__`. Import in consuming modules via `from core.config import resolve_new_field`.

---

## Step 4 — CLI setter command

Add `llm-wiki set-new-field` in `main.py`. Use local imports (startup speed — all CLI commands do this):

```python
@cli.command("set-new-field")
@click.argument("value", type=<click_type>)
@click.option("--vault", default=None, help="Vault name (omit for global default)")
def cmd_set_new_field(value: <type>, vault: str | None) -> None:
    """Set <one-line description>."""
    from core.config import GlobalConfig, VaultConfig
    if vault:
        ...  # load VaultConfig, set field, save
    else:
        ...  # load GlobalConfig, set field, save
```

---

## Documentation

- Add the new field to the `GlobalConfig` and `VaultConfig` tables in `core/README.md`
- Add the new CLI command to the CLI command table in `core/README.md` or `README.md`
- If the field has non-obvious sizing guidance (like `context_chars`), add a note to `CLAUDE.md` Known Gotchas

---

## Tests

Write unit tests for `resolve_new_field`:

1. Vault override wins when `VaultConfig.new_field` is set
2. Global value wins when vault field is `None` but global is non-default
3. Hardcoded default returned when both are unset

Follow the pattern in `tests/integration/test_config_resolution.py` for multi-vault resolution — mark those tests `@pytest.mark.integration`.

---

## Run QA

Run `/qa` before declaring the task complete.
