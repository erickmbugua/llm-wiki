"""Tests for main.py CLI commands."""

import json

import pytest
from click.testing import CliRunner

from main import cli


@pytest.fixture
def runner():
    """Click test runner."""
    return CliRunner()


@pytest.fixture
def two_vault_config(patched_global_config, tmp_path):
    """Config file with two registered vaults, 'Alpha' as default."""
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    beta = tmp_path / "beta"
    beta.mkdir()
    data = {
        "vaults": {"Alpha": str(alpha), "Beta": str(beta)},
        "default_vault": "Alpha",
        "model": "ollama/qwen2.5-coder:7b",
        "server_port": 8000,
    }
    (patched_global_config / "config.json").write_text(json.dumps(data))
    return patched_global_config


# ── unregister ────────────────────────────────────────────────────────────────


class TestUnregisterCommand:
    def test_removes_vault_from_config(self, runner, two_vault_config):
        result = runner.invoke(cli, ["unregister", "Beta"])
        assert result.exit_code == 0
        saved = json.loads((two_vault_config / "config.json").read_text())
        assert "Beta" not in saved["vaults"]
        assert "Alpha" in saved["vaults"]

    def test_clears_default_when_no_vaults_remain(self, runner, patched_global_config, tmp_path):
        vault = tmp_path / "only"
        vault.mkdir()
        data = {
            "vaults": {"Only": str(vault)},
            "default_vault": "Only",
            "model": "ollama/qwen2.5-coder:7b",
            "server_port": 8000,
        }
        (patched_global_config / "config.json").write_text(json.dumps(data))
        result = runner.invoke(cli, ["unregister", "Only"])
        assert result.exit_code == 0
        saved = json.loads((patched_global_config / "config.json").read_text())
        assert saved["default_vault"] is None

    def test_sets_new_default_when_others_remain(self, runner, two_vault_config):
        result = runner.invoke(cli, ["unregister", "Alpha"])
        assert result.exit_code == 0
        saved = json.loads((two_vault_config / "config.json").read_text())
        assert saved["default_vault"] == "Beta"

    def test_errors_for_unknown_vault(self, runner, two_vault_config):
        result = runner.invoke(cli, ["unregister", "Nonexistent"])
        assert result.exit_code != 0


# ── set-model validation ───────────────────────────────────────────────────────


class TestSetModelValidation:
    def test_unknown_prefix_prints_warning(self, runner, patched_global_config):
        result = runner.invoke(cli, ["set-model", "gpt5"])
        assert result.exit_code == 0
        assert "Warning" in result.output

    def test_known_prefix_no_warning(self, runner, patched_global_config):
        result = runner.invoke(cli, ["set-model", "ollama/llama3"])
        assert result.exit_code == 0
        assert "Warning" not in result.output
