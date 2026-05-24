"""Shared fixtures for all test modules."""

from unittest.mock import MagicMock

import pytest

from core.database import get_db, reconcile
from core.vault import init_vault

# ── Vault fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_vault(tmp_path):
    """A fully initialized vault in a temp directory."""
    vault_path = tmp_path / "test-vault"
    init_vault(vault_path, "TestVault")
    return vault_path


@pytest.fixture
def db_conn(tmp_vault):
    """Open SQLite connection to a temp vault; closes after the test."""
    conn = get_db(tmp_vault)
    yield conn
    conn.close()


@pytest.fixture
def populated_vault(tmp_vault):
    """
    Vault with three pre-indexed wiki pages:
      Concepts/Transformers.md  — links to [[Attention]]
      Concepts/Attention.md     — linked from Transformers
      Sources/Paper.md          — standalone (no links)
    """
    wiki = tmp_vault / "wiki"
    (wiki / "Concepts" / "Transformers.md").write_text(
        "---\ntitle: Transformers\ntags: [deep-learning]\n---\n"
        "Transformers use self-attention mechanisms. See also [[Attention]].\n"
    )
    (wiki / "Concepts" / "Attention.md").write_text(
        "---\ntitle: Attention\ntags: [deep-learning]\n---\n"
        "The attention mechanism lets models focus on relevant tokens.\n"
    )
    (wiki / "Sources" / "Paper.md").write_text(
        "---\ntitle: Attention Is All You Need\ntags: [paper]\n---\n"
        "Seminal paper introducing the Transformer architecture.\n"
    )
    conn = get_db(tmp_vault)
    reconcile(conn, wiki)
    conn.close()
    return tmp_vault


# ── Config fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def patched_global_config(tmp_path, monkeypatch):
    """Redirect GlobalConfig I/O to a temp dir so tests never touch ~/.llm-wiki."""
    cfg_dir = tmp_path / "global-cfg"
    cfg_dir.mkdir()
    monkeypatch.setattr("core.config.GLOBAL_CONFIG_DIR", cfg_dir)
    monkeypatch.setattr("core.config.GLOBAL_CONFIG_FILE", cfg_dir / "config.json")
    return cfg_dir


# ── LLM mock helpers ─────────────────────────────────────────────────────────


@pytest.fixture
def fake_llm_response():
    """
    Factory that builds a mock litellm completion response.
    Usage: fake_llm_response("some text")
    """

    def _make(content: str):
        mock = MagicMock()
        mock.choices[0].message.content = content
        return mock

    return _make
