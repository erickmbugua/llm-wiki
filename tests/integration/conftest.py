"""Shared fixtures for integration tests.

Integration tests exercise real cross-module pipelines with the LLM stubbed out.
Nothing here requires Ollama or a real network connection.

Fixture hierarchy:
  vault_path         — a real initialized vault in a pytest tmp_path
  ingest_json        — canned LLM response JSON (source_page + one Concepts/ page with a wikilink)
  llm_stub           — patches litellm.completion/embedding so no network calls are made
  patched_config     — patches GlobalConfig.load and vault config caches for test isolation
  api_client         — TestClient with a real ThreadPoolExecutor registered for the vault
"""

from __future__ import annotations

import json
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from core import server as server_mod
from core.config import (
    VaultConfig,
    _clear_global_config_cache,
    _clear_vault_config_cache,
)
from core.server import app, register_vault_executor
from core.vault import init_vault

VAULT_NAME = "IntegrationVault"


@pytest.fixture
def vault_path(tmp_path: Path) -> Path:
    """A fully initialized vault in a temporary directory, configured to use a stub model.

    The per-vault config is written with ``model: "stub/model"`` so ingest never triggers
    the Ollama preflight check (which fires only for ``ollama/`` prefixed model strings).
    """
    path = tmp_path / "integration-vault"
    init_vault(path, VAULT_NAME)
    vcfg = VaultConfig(name=VAULT_NAME, model="stub/model")
    vcfg.save(path)
    return path


@pytest.fixture
def ingest_json() -> str:
    """Canned LLM ingest response — one Sources page and one Concepts page with a wikilink.

    The wikilink from Sources/Test_Source.md to [[Test_Concept]] exercises the
    backlink reconciliation path in partial_reconcile.
    """
    return json.dumps(
        {
            "source_page": {
                "file_path": "Sources/Test_Source.md",
                "content": (
                    "---\ntitle: Test Source\ntags: [test]\n---\n\n"
                    "A test source. See also [[Test_Concept]].\n"
                ),
            },
            "page_updates": [
                {
                    "file_path": "Concepts/Test_Concept.md",
                    "action": "create",
                    "content": (
                        "---\ntitle: Test Concept\ntags: [test]\n---\n\n"
                        "A test concept extracted during integration testing.\n"
                    ),
                }
            ],
        }
    )


@pytest.fixture
def llm_stub(monkeypatch: pytest.MonkeyPatch, ingest_json: str) -> MagicMock:
    """Patch litellm in core.ingest and core.embeddings so no network calls are made.

    Completion returns the canned ingest JSON. Embedding raises RuntimeError so the
    code falls through to FTS5-only search (the graceful fallback path).
    """
    completion_response = MagicMock()
    completion_response.choices[0].message.content = ingest_json

    monkeypatch.setattr("core.ingest.litellm.completion", lambda **kw: completion_response)
    monkeypatch.setattr(
        "core.embeddings.litellm.embedding",
        MagicMock(side_effect=RuntimeError("embedding stubbed out")),
    )
    return completion_response


@pytest.fixture
def patched_config(
    vault_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    """Patch GlobalConfig.load to return a config pointing at vault_path, and clear caches.

    Clearing both caches before and after ensures that config values written during
    one test do not bleed into the next test via the process-level cache.
    """
    from core.config import GlobalConfig

    _clear_global_config_cache()
    _clear_vault_config_cache()

    cfg = GlobalConfig()
    cfg.vaults = {VAULT_NAME: str(vault_path)}
    cfg.default_vault = VAULT_NAME

    monkeypatch.setattr("core.server.GlobalConfig.load", lambda: cfg)

    yield

    _clear_global_config_cache()
    _clear_vault_config_cache()


@pytest.fixture
def api_client(
    vault_path: Path,
    llm_stub: MagicMock,
    patched_config: None,
) -> Generator[TestClient, None, None]:
    """TestClient with a real single-worker ThreadPoolExecutor registered for the vault.

    Using a real executor (instead of the fallback one-shot executor) exercises the
    job-submission and status-polling path that runs in production.  The executor is
    shut down after each test so all background ingest jobs complete before the
    temporary vault directory is cleaned up.
    """
    executor = ThreadPoolExecutor(max_workers=1)
    register_vault_executor(VAULT_NAME, executor)

    yield TestClient(app)

    executor.shutdown(wait=True)
    server_mod._vault_executors.pop(VAULT_NAME, None)
