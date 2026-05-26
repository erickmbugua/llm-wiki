"""E2E CLI tests — subprocess invocations of the llm-wiki command.

Tests exercise the full Click CLI surface by spawning real subprocesses.
LLM calls are intercepted by the TCP mock server (via OPENAI_API_BASE in vault_env).
Tests that call the LLM (ingest, query) request mock_llm_server so OPENAI_API_BASE
is set before the subprocess is spawned. Tests that never reach the LLM (init, list,
status, lint on an empty vault) request only vault_env.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from .conftest import VAULT_NAME

pytestmark = pytest.mark.e2e

_TIMEOUT = 30  # seconds; generous for the LLM mock round-trip in CI


def _cli(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the llm-wiki CLI as a subprocess and return the completed process.

    Args:
        *args: CLI arguments passed after ``llm-wiki``.
        env: Subprocess environment dict (should include LLM_WIKI_HOME and PATH).

    Returns:
        CompletedProcess with stdout, stderr, and returncode populated.
    """
    return subprocess.run(
        ["llm-wiki", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=_TIMEOUT,
    )


class TestCLIInit:
    def test_init_creates_expected_directory_structure(
        self, vault_env: dict[str, str], tmp_path: Path
    ) -> None:
        """``llm-wiki init`` writes raw/, wiki/Sources/, wiki/Concepts/, and config."""
        new_vault = tmp_path / "cli-vault"
        result = _cli("init", str(new_vault), "--name", "CLIVault", env=vault_env)
        assert result.returncode == 0, result.stderr
        assert (new_vault / "raw").is_dir()
        assert (new_vault / "wiki" / "Sources").is_dir()
        assert (new_vault / "wiki" / "Concepts").is_dir()
        assert (new_vault / ".llm-wiki" / "config.json").exists()
        assert (new_vault / "wiki" / "schema.md").exists()


class TestCLIIngest:
    def test_ingest_text_file_exits_zero(
        self, vault_env: dict[str, str], mock_llm_server: str
    ) -> None:
        """``llm-wiki ingest <file>`` exits 0 when the LLM mock responds correctly."""
        source = Path(vault_env["LLM_WIKI_VAULT_DIR"]) / "raw" / "article.txt"
        source.write_text("Transformers are a type of neural network architecture.")
        result = _cli("ingest", str(source), env=vault_env)
        assert result.returncode == 0, result.stderr

    def test_ingest_creates_sources_wiki_file(
        self, vault_env: dict[str, str], mock_llm_server: str
    ) -> None:
        """After ingest, the Sources page written by the LLM exists on disk."""
        source = Path(vault_env["LLM_WIKI_VAULT_DIR"]) / "raw" / "article.txt"
        source.write_text("Article about neural networks.")
        _cli("ingest", str(source), env=vault_env)
        wiki_root = Path(vault_env["LLM_WIKI_VAULT_DIR"]) / "wiki"
        assert (wiki_root / "Sources" / "E2E_Source.md").exists()

    def test_ingest_creates_concepts_wiki_file(
        self, vault_env: dict[str, str], mock_llm_server: str
    ) -> None:
        """After ingest, the Concepts page extracted by the LLM exists on disk."""
        source = Path(vault_env["LLM_WIKI_VAULT_DIR"]) / "raw" / "article.txt"
        source.write_text("Article about machine learning concepts.")
        _cli("ingest", str(source), env=vault_env)
        wiki_root = Path(vault_env["LLM_WIKI_VAULT_DIR"]) / "wiki"
        assert (wiki_root / "Concepts" / "E2E_Concept.md").exists()

    def test_ingest_appends_entry_to_log(
        self, vault_env: dict[str, str], mock_llm_server: str
    ) -> None:
        """``llm-wiki ingest`` appends a timestamped entry to wiki/log.md."""
        source = Path(vault_env["LLM_WIKI_VAULT_DIR"]) / "raw" / "article.txt"
        source.write_text("Some content to ingest.")
        log_path = Path(vault_env["LLM_WIKI_VAULT_DIR"]) / "wiki" / "log.md"
        before = log_path.read_text() if log_path.exists() else ""
        _cli("ingest", str(source), env=vault_env)
        after = log_path.read_text()
        assert len(after) > len(before)
        assert "article.txt" in after


class TestCLIQueryAndLint:
    def test_query_exits_zero_with_output(
        self, vault_env: dict[str, str], mock_llm_server: str
    ) -> None:
        """``llm-wiki query`` exits 0 and prints a non-empty answer."""
        result = _cli("query", "What is a test concept?", env=vault_env)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip()

    def test_lint_exits_zero(self, vault_env: dict[str, str], mock_llm_server: str) -> None:
        """``llm-wiki lint`` exits 0; reconcile indexes root wiki files so the LLM is called."""
        result = _cli("lint", env=vault_env)
        assert result.returncode == 0, result.stderr


class TestCLIStatus:
    def test_status_shows_vault_name(self, vault_env: dict[str, str]) -> None:
        """``llm-wiki status`` exits 0 and prints the registered vault name."""
        result = _cli("status", env=vault_env)
        assert result.returncode == 0, result.stderr
        assert VAULT_NAME in result.stdout
