"""Tests for core/config.py — GlobalConfig, VaultConfig, resolve_model, resolve_context_chars."""

import json
from pathlib import Path

import pytest

import core.config as cfg_mod
from core.config import GlobalConfig, VaultConfig, resolve_context_chars, resolve_model

# ── GlobalConfig ──────────────────────────────────────────────────────────────


class TestGlobalConfigLoad:
    def test_returns_defaults_when_no_file(self, patched_global_config: Path) -> None:
        cfg = GlobalConfig.load()
        assert cfg.vaults == {}
        assert cfg.default_vault is None
        assert cfg.model == "ollama/qwen2.5-coder:7b"
        assert cfg.server_port == 8000

    def test_loads_existing_file(self, patched_global_config: Path, tmp_path: Path) -> None:
        vault_path = tmp_path / "a"
        vault_path.mkdir()
        data = {
            "vaults": {"A": str(vault_path)},
            "default_vault": "A",
            "model": "gpt-4o",
            "server_port": 9000,
        }
        (patched_global_config / "config.json").write_text(json.dumps(data))
        cfg = GlobalConfig.load()
        assert cfg.vaults == {"A": str(vault_path)}
        assert cfg.default_vault == "A"
        assert cfg.model == "gpt-4o"
        assert cfg.server_port == 9000

    def test_ignores_unknown_keys(self, patched_global_config: Path) -> None:
        data = {"vaults": {}, "unknown_key": "should-be-ignored"}
        (patched_global_config / "config.json").write_text(json.dumps(data))
        cfg = GlobalConfig.load()  # must not raise
        assert cfg.vaults == {}


class TestGlobalConfigSave:
    def test_creates_file_on_save(self, patched_global_config: Path) -> None:
        cfg = GlobalConfig()
        cfg.save()
        saved = json.loads((patched_global_config / "config.json").read_text())
        assert saved["vaults"] == {}
        assert saved["model"] == "ollama/qwen2.5-coder:7b"

    def test_save_roundtrip(self, patched_global_config: Path, tmp_path: Path) -> None:
        vault_path = tmp_path / "my"
        vault_path.mkdir()
        cfg = GlobalConfig()
        cfg.vaults = {"My": str(vault_path)}
        cfg.default_vault = "My"
        cfg.model = "ollama/llama3"
        cfg.save()
        loaded = GlobalConfig.load()
        assert loaded.vaults == {"My": str(vault_path)}
        assert loaded.default_vault == "My"
        assert loaded.model == "ollama/llama3"


class TestGlobalConfigRegisterVault:
    def test_adds_vault_and_sets_default_when_first(
        self, patched_global_config: Path, tmp_path: Path
    ) -> None:
        cfg = GlobalConfig()
        cfg.register_vault("Cars", tmp_path / "cars")
        assert "Cars" in cfg.vaults
        assert cfg.default_vault == "Cars"

    def test_does_not_change_default_when_already_set(
        self, patched_global_config: Path, tmp_path: Path
    ) -> None:
        cfg = GlobalConfig()
        cfg.register_vault("First", tmp_path / "first")
        cfg.register_vault("Second", tmp_path / "second")
        assert cfg.default_vault == "First"

    def test_persists_vault_to_disk(self, patched_global_config: Path, tmp_path: Path) -> None:
        vault_path = tmp_path / "saved"
        vault_path.mkdir()
        cfg = GlobalConfig()
        cfg.register_vault("Saved", vault_path)
        reloaded = GlobalConfig.load()
        assert "Saved" in reloaded.vaults


class TestGlobalConfigResolveVault:
    def test_resolves_named_vault(self, patched_global_config: Path, tmp_path: Path) -> None:
        cfg = GlobalConfig()
        cfg.vaults = {"X": str(tmp_path)}
        name, path = cfg.resolve_vault("X")
        assert name == "X"
        assert path == tmp_path

    def test_resolves_default_when_name_is_none(
        self, patched_global_config: Path, tmp_path: Path
    ) -> None:
        cfg = GlobalConfig()
        cfg.vaults = {"Y": str(tmp_path)}
        cfg.default_vault = "Y"
        name, _path = cfg.resolve_vault(None)
        assert name == "Y"

    def test_raises_when_no_default_and_no_name(self, patched_global_config: Path) -> None:
        cfg = GlobalConfig()
        with pytest.raises(ValueError, match="No vault specified"):
            cfg.resolve_vault(None)

    def test_raises_for_unknown_vault(self, patched_global_config: Path) -> None:
        cfg = GlobalConfig()
        with pytest.raises(KeyError, match="not registered"):
            cfg.resolve_vault("nonexistent")


# ── VaultConfig ───────────────────────────────────────────────────────────────


class TestVaultConfig:
    def test_defaults_when_no_file(self, tmp_vault: Path) -> None:
        cfg = VaultConfig.load(tmp_vault)
        assert cfg.name == "TestVault"  # init_vault writes this

    def test_save_and_load_roundtrip(self, tmp_vault: Path) -> None:
        cfg = VaultConfig(name="Test", model="gpt-4o")
        cfg.save(tmp_vault)
        loaded = VaultConfig.load(tmp_vault)
        assert loaded.name == "Test"
        assert loaded.model == "gpt-4o"

    def test_model_none_by_default(self, tmp_path: Path) -> None:
        vault = tmp_path / "empty"
        vault.mkdir()
        cfg = VaultConfig.load(vault)
        assert cfg.model is None


# ── resolve_model ─────────────────────────────────────────────────────────────


class TestResolveModel:
    def test_returns_vault_override_when_set(
        self, tmp_vault: Path, patched_global_config: Path
    ) -> None:
        vcfg = VaultConfig(name="T", model="ollama/llama3")
        vcfg.save(tmp_vault)
        assert resolve_model(tmp_vault) == "ollama/llama3"

    def test_falls_back_to_global_model(self, tmp_vault: Path, patched_global_config: Path) -> None:
        cfg = GlobalConfig()
        cfg.model = "gpt-4o"
        cfg.save()
        assert resolve_model(tmp_vault) == "gpt-4o"

    def test_returns_global_when_no_vault_path(self, patched_global_config: Path) -> None:
        cfg = GlobalConfig()
        cfg.model = "gpt-4o"
        cfg.save()
        assert resolve_model(None) == "gpt-4o"


# ── GlobalConfig.reconcile_vaults ─────────────────────────────────────────────


class TestReconcileVaults:
    def test_drops_missing_vault(self, tmp_path: Path) -> None:
        cfg = GlobalConfig()
        cfg.vaults = {"Gone": str(tmp_path / "nonexistent")}
        dropped = cfg.reconcile_vaults()
        assert "Gone" in dropped
        assert "Gone" not in cfg.vaults

    def test_keeps_existing_vault(self, tmp_path: Path) -> None:
        existing = tmp_path / "vault"
        existing.mkdir()
        cfg = GlobalConfig()
        cfg.vaults = {"Present": str(existing)}
        dropped = cfg.reconcile_vaults()
        assert dropped == []
        assert "Present" in cfg.vaults

    def test_clears_default_when_only_vault_dropped(self, tmp_path: Path) -> None:
        cfg = GlobalConfig()
        cfg.vaults = {"Gone": str(tmp_path / "nonexistent")}
        cfg.default_vault = "Gone"
        cfg.reconcile_vaults()
        assert cfg.default_vault is None

    def test_sets_default_to_remaining_vault(self, tmp_path: Path) -> None:
        existing = tmp_path / "vault"
        existing.mkdir()
        cfg = GlobalConfig()
        cfg.vaults = {"Gone": str(tmp_path / "nonexistent"), "Present": str(existing)}
        cfg.default_vault = "Gone"
        cfg.reconcile_vaults()
        assert cfg.default_vault == "Present"

    def test_load_auto_reconciles_and_saves(self, patched_global_config: Path) -> None:
        data = {
            "vaults": {"Stale": "/nonexistent/path/to/vault"},
            "default_vault": "Stale",
            "model": "ollama/qwen2.5-coder:7b",
            "server_port": 8000,
        }
        (patched_global_config / "config.json").write_text(json.dumps(data))
        cfg = GlobalConfig.load()
        assert "Stale" not in cfg.vaults
        assert cfg.default_vault is None
        saved = json.loads((patched_global_config / "config.json").read_text())
        assert "Stale" not in saved["vaults"]


# ── resolve_context_chars ─────────────────────────────────────────────────────


class TestResolveContextChars:
    def test_resolve_context_chars_default(self, patched_global_config: Path) -> None:
        assert resolve_context_chars() == 24_000

    def test_resolve_context_chars_falls_back_to_global(self, patched_global_config: Path) -> None:
        data: dict[str, object] = {
            "vaults": {},
            "default_vault": None,
            "model": "ollama/qwen2.5-coder:7b",
            "server_port": 8000,
            "context_chars": 48_000,
        }
        (patched_global_config / "config.json").write_text(json.dumps(data))
        assert resolve_context_chars() == 48_000

    def test_resolve_context_chars_uses_vault_override(
        self, patched_global_config: Path, tmp_path: Path
    ) -> None:
        vault = tmp_path / "v"
        vault.mkdir()
        (vault / ".llm-wiki").mkdir()
        (vault / ".llm-wiki" / "config.json").write_text(
            json.dumps({"name": "v", "model": None, "context_chars": 8_000})
        )
        assert resolve_context_chars(vault) == 8_000


# ── Config caching ────────────────────────────────────────────────────────────


class TestGlobalConfigCache:
    def test_load_returns_same_instance_on_repeated_calls(
        self, patched_global_config: Path
    ) -> None:
        """GlobalConfig.load() returns the cached instance without re-reading disk."""
        first = GlobalConfig.load()
        # Mutate the file on disk — cache should shield us
        (patched_global_config / "config.json").write_text(
            json.dumps(
                {
                    "vaults": {},
                    "model": "mutated",
                    "server_port": 8000,
                    "default_vault": None,
                    "context_chars": 24000,
                    "chunk_size": 20000,
                    "chunk_overlap": 500,
                    "embedding_model": "ollama/nomic-embed-text",
                    "embedding_dim": 768,
                }
            )
        )
        second = GlobalConfig.load()
        assert first is second
        assert second.model != "mutated"

    def test_save_updates_cache_to_new_instance(self, patched_global_config: Path) -> None:
        """After save(), the next load() reflects the saved values without re-reading disk."""
        cfg = GlobalConfig()
        cfg.model = "before"
        cfg.save()

        cfg2 = GlobalConfig()
        cfg2.model = "after"
        cfg2.save()

        loaded = GlobalConfig.load()
        assert loaded.model == "after"

    def test_clear_cache_forces_disk_read(self, patched_global_config: Path) -> None:
        """_clear_global_config_cache() causes the next load() to read from disk."""
        GlobalConfig.load()  # populate cache
        (patched_global_config / "config.json").write_text(
            json.dumps(
                {
                    "vaults": {},
                    "model": "fresh",
                    "server_port": 8000,
                    "default_vault": None,
                    "context_chars": 24000,
                    "chunk_size": 20000,
                    "chunk_overlap": 500,
                    "embedding_model": "ollama/nomic-embed-text",
                    "embedding_dim": 768,
                }
            )
        )
        cfg_mod._clear_global_config_cache()
        reloaded = GlobalConfig.load()
        assert reloaded.model == "fresh"


class TestVaultConfigCache:
    def test_load_returns_same_instance_on_repeated_calls(self, tmp_vault: Path) -> None:
        """VaultConfig.load() returns the cached instance without re-reading disk."""
        first = VaultConfig.load(tmp_vault)
        cfg_file = tmp_vault / ".llm-wiki" / "config.json"
        cfg_file.write_text(json.dumps({"name": "mutated", "model": "gpt-4o"}))
        second = VaultConfig.load(tmp_vault)
        assert first is second

    def test_save_updates_cache_to_new_instance(self, tmp_vault: Path) -> None:
        """After save(), load() returns the new values without re-reading disk."""
        VaultConfig.load(tmp_vault)  # populate cache

        cfg = VaultConfig(name="Updated", model="claude-sonnet-4-6")
        cfg.save(tmp_vault)

        loaded = VaultConfig.load(tmp_vault)
        assert loaded.model == "claude-sonnet-4-6"
        assert loaded is cfg

    def test_clear_cache_forces_disk_read(self, tmp_vault: Path) -> None:
        """_clear_vault_config_cache() causes the next load() to read from disk."""
        VaultConfig.load(tmp_vault)  # populate cache
        cfg_file = tmp_vault / ".llm-wiki" / "config.json"
        cfg_file.write_text(json.dumps({"name": "fresh", "model": "fresh-model"}))
        cfg_mod._clear_vault_config_cache(tmp_vault)
        reloaded = VaultConfig.load(tmp_vault)
        assert reloaded.model == "fresh-model"
