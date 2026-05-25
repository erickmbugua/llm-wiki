"""Tests for core/config.py — GlobalConfig, VaultConfig, resolve_model, resolve_context_chars."""

import json

import pytest

from core.config import GlobalConfig, VaultConfig, resolve_context_chars, resolve_model

# ── GlobalConfig ──────────────────────────────────────────────────────────────


class TestGlobalConfigLoad:
    def test_returns_defaults_when_no_file(self, patched_global_config):
        cfg = GlobalConfig.load()
        assert cfg.vaults == {}
        assert cfg.default_vault is None
        assert cfg.model == "ollama/qwen2.5-coder:7b"
        assert cfg.server_port == 8000

    def test_loads_existing_file(self, patched_global_config, tmp_path):
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

    def test_ignores_unknown_keys(self, patched_global_config):
        data = {"vaults": {}, "unknown_key": "should-be-ignored"}
        (patched_global_config / "config.json").write_text(json.dumps(data))
        cfg = GlobalConfig.load()  # must not raise
        assert cfg.vaults == {}


class TestGlobalConfigSave:
    def test_creates_file_on_save(self, patched_global_config):
        cfg = GlobalConfig()
        cfg.save()
        saved = json.loads((patched_global_config / "config.json").read_text())
        assert saved["vaults"] == {}
        assert saved["model"] == "ollama/qwen2.5-coder:7b"

    def test_save_roundtrip(self, patched_global_config, tmp_path):
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
    def test_adds_vault_and_sets_default_when_first(self, patched_global_config, tmp_path):
        cfg = GlobalConfig()
        cfg.register_vault("Cars", tmp_path / "cars")
        assert "Cars" in cfg.vaults
        assert cfg.default_vault == "Cars"

    def test_does_not_change_default_when_already_set(self, patched_global_config, tmp_path):
        cfg = GlobalConfig()
        cfg.register_vault("First", tmp_path / "first")
        cfg.register_vault("Second", tmp_path / "second")
        assert cfg.default_vault == "First"

    def test_persists_vault_to_disk(self, patched_global_config, tmp_path):
        vault_path = tmp_path / "saved"
        vault_path.mkdir()
        cfg = GlobalConfig()
        cfg.register_vault("Saved", vault_path)
        reloaded = GlobalConfig.load()
        assert "Saved" in reloaded.vaults


class TestGlobalConfigResolveVault:
    def test_resolves_named_vault(self, patched_global_config, tmp_path):
        cfg = GlobalConfig()
        cfg.vaults = {"X": str(tmp_path)}
        name, path = cfg.resolve_vault("X")
        assert name == "X"
        assert path == tmp_path

    def test_resolves_default_when_name_is_none(self, patched_global_config, tmp_path):
        cfg = GlobalConfig()
        cfg.vaults = {"Y": str(tmp_path)}
        cfg.default_vault = "Y"
        name, path = cfg.resolve_vault(None)
        assert name == "Y"

    def test_raises_when_no_default_and_no_name(self, patched_global_config):
        cfg = GlobalConfig()
        with pytest.raises(ValueError, match="No vault specified"):
            cfg.resolve_vault(None)

    def test_raises_for_unknown_vault(self, patched_global_config):
        cfg = GlobalConfig()
        with pytest.raises(KeyError, match="not registered"):
            cfg.resolve_vault("nonexistent")


# ── VaultConfig ───────────────────────────────────────────────────────────────


class TestVaultConfig:
    def test_defaults_when_no_file(self, tmp_vault):
        cfg = VaultConfig.load(tmp_vault)
        assert cfg.name == "TestVault"  # init_vault writes this

    def test_save_and_load_roundtrip(self, tmp_vault):
        cfg = VaultConfig(name="Test", model="gpt-4o")
        cfg.save(tmp_vault)
        loaded = VaultConfig.load(tmp_vault)
        assert loaded.name == "Test"
        assert loaded.model == "gpt-4o"

    def test_model_none_by_default(self, tmp_path):
        vault = tmp_path / "empty"
        vault.mkdir()
        cfg = VaultConfig.load(vault)
        assert cfg.model is None


# ── resolve_model ─────────────────────────────────────────────────────────────


class TestResolveModel:
    def test_returns_vault_override_when_set(self, tmp_vault, patched_global_config):
        vcfg = VaultConfig(name="T", model="ollama/llama3")
        vcfg.save(tmp_vault)
        assert resolve_model(tmp_vault) == "ollama/llama3"

    def test_falls_back_to_global_model(self, tmp_vault, patched_global_config):
        cfg = GlobalConfig()
        cfg.model = "gpt-4o"
        cfg.save()
        assert resolve_model(tmp_vault) == "gpt-4o"

    def test_returns_global_when_no_vault_path(self, patched_global_config):
        cfg = GlobalConfig()
        cfg.model = "gpt-4o"
        cfg.save()
        assert resolve_model(None) == "gpt-4o"


# ── GlobalConfig.reconcile_vaults ─────────────────────────────────────────────


class TestReconcileVaults:
    def test_drops_missing_vault(self, tmp_path):
        cfg = GlobalConfig()
        cfg.vaults = {"Gone": str(tmp_path / "nonexistent")}
        dropped = cfg.reconcile_vaults()
        assert "Gone" in dropped
        assert "Gone" not in cfg.vaults

    def test_keeps_existing_vault(self, tmp_path):
        existing = tmp_path / "vault"
        existing.mkdir()
        cfg = GlobalConfig()
        cfg.vaults = {"Present": str(existing)}
        dropped = cfg.reconcile_vaults()
        assert dropped == []
        assert "Present" in cfg.vaults

    def test_clears_default_when_only_vault_dropped(self, tmp_path):
        cfg = GlobalConfig()
        cfg.vaults = {"Gone": str(tmp_path / "nonexistent")}
        cfg.default_vault = "Gone"
        cfg.reconcile_vaults()
        assert cfg.default_vault is None

    def test_sets_default_to_remaining_vault(self, tmp_path):
        existing = tmp_path / "vault"
        existing.mkdir()
        cfg = GlobalConfig()
        cfg.vaults = {"Gone": str(tmp_path / "nonexistent"), "Present": str(existing)}
        cfg.default_vault = "Gone"
        cfg.reconcile_vaults()
        assert cfg.default_vault == "Present"

    def test_load_auto_reconciles_and_saves(self, patched_global_config):
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
    def test_resolve_context_chars_default(self, patched_global_config):
        assert resolve_context_chars() == 24_000

    def test_resolve_context_chars_falls_back_to_global(self, patched_global_config):
        data = {
            "vaults": {},
            "default_vault": None,
            "model": "ollama/qwen2.5-coder:7b",
            "server_port": 8000,
            "context_chars": 48_000,
        }
        (patched_global_config / "config.json").write_text(json.dumps(data))
        assert resolve_context_chars() == 48_000

    def test_resolve_context_chars_uses_vault_override(self, patched_global_config, tmp_path):
        vault = tmp_path / "v"
        vault.mkdir()
        (vault / ".llm-wiki").mkdir()
        (vault / ".llm-wiki" / "config.json").write_text(
            json.dumps({"name": "v", "model": None, "context_chars": 8_000})
        )
        assert resolve_context_chars(vault) == 8_000
