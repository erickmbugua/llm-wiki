"""Integration tests for the three-level config resolution chain.

Tests cover:
  - Vault-level override wins over global config
  - Global config is used when no per-vault override is set
  - The full priority chain: vault > global > hardcoded default
  - Two vaults with different overrides resolve independently (no cache bleed)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.config import (
    VaultConfig,
    _clear_global_config_cache,
    _clear_vault_config_cache,
    resolve_context_chars,
    resolve_model,
)
from core.vault import init_vault


def _make_vault(parent: Path, name: str) -> Path:
    """Initialise a vault and return its path.

    Args:
        parent: Directory that will contain the vault.
        name: Vault name passed to init_vault.

    Returns:
        The path to the newly created vault.
    """
    vault = parent / name.lower().replace(" ", "-")
    init_vault(vault, name)
    return vault


@pytest.mark.integration
class TestConfigResolution:
    def setup_method(self) -> None:
        """Clear all config caches before each test to prevent cross-test bleed."""
        _clear_global_config_cache()
        _clear_vault_config_cache()

    def teardown_method(self) -> None:
        """Clear caches after each test so next test starts clean."""
        _clear_global_config_cache()
        _clear_vault_config_cache()

    def test_vault_model_overrides_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A model set in per-vault config wins over the global config model."""
        monkeypatch.setattr("core.config.GLOBAL_CONFIG_DIR", tmp_path / "global")
        monkeypatch.setattr("core.config.GLOBAL_CONFIG_FILE", tmp_path / "global" / "config.json")

        vault = _make_vault(tmp_path, "VaultA")
        VaultConfig(name="VaultA", model="vault-specific/llama3").save(vault)

        assert resolve_model(vault) == "vault-specific/llama3"

    def test_global_model_used_when_no_vault_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When a vault has no model in its config, the global config model is returned."""
        cfg_dir = tmp_path / "global"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "config.json"
        cfg_file.write_text(json.dumps({"model": "global/qwen3"}))

        monkeypatch.setattr("core.config.GLOBAL_CONFIG_DIR", cfg_dir)
        monkeypatch.setattr("core.config.GLOBAL_CONFIG_FILE", cfg_file)

        vault = _make_vault(tmp_path, "VaultB")
        # No per-vault model override written

        assert resolve_model(vault) == "global/qwen3"

    def test_context_chars_full_priority_chain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """context_chars resolution exercises all three tiers: vault > global > hardcoded default."""
        cfg_dir = tmp_path / "global"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "config.json"
        cfg_file.write_text(json.dumps({"context_chars": 48_000}))

        monkeypatch.setattr("core.config.GLOBAL_CONFIG_DIR", cfg_dir)
        monkeypatch.setattr("core.config.GLOBAL_CONFIG_FILE", cfg_file)

        vault_with_override = _make_vault(tmp_path, "VaultOverride")
        VaultConfig(name="VaultOverride", context_chars=6_000).save(vault_with_override)

        vault_global_only = _make_vault(tmp_path, "VaultGlobalOnly")
        vault_defaults_only = _make_vault(tmp_path, "VaultDefaultsOnly")

        # Tier 1: vault-level override wins
        assert resolve_context_chars(vault_with_override) == 6_000

        # Tier 2: global config used when no vault-level override is set
        assert resolve_context_chars(vault_global_only) == 48_000

        # Tier 3: hardcoded default (24_000) when neither vault nor global config exists
        monkeypatch.setattr("core.config.GLOBAL_CONFIG_FILE", tmp_path / "nonexistent.json")
        _clear_global_config_cache()
        assert resolve_context_chars(vault_defaults_only) == 24_000

    def test_two_vaults_resolve_independently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two vaults with different model overrides each resolve to their own model string.

        This verifies that the per-vault cache does not let one vault's config bleed
        into another vault's resolution.
        """
        monkeypatch.setattr("core.config.GLOBAL_CONFIG_DIR", tmp_path / "global")
        monkeypatch.setattr("core.config.GLOBAL_CONFIG_FILE", tmp_path / "global" / "config.json")

        vault_a = _make_vault(tmp_path, "VaultAlpha")
        vault_b = _make_vault(tmp_path, "VaultBeta")

        VaultConfig(name="VaultAlpha", model="alpha/model-7b").save(vault_a)
        VaultConfig(name="VaultBeta", model="beta/model-70b").save(vault_b)

        assert resolve_model(vault_a) == "alpha/model-7b"
        assert resolve_model(vault_b) == "beta/model-70b"
        # Re-resolve vault_a to confirm cache returns the correct value, not vault_b's
        assert resolve_model(vault_a) == "alpha/model-7b"
