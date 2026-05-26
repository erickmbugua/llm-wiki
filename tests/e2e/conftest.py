"""Shared fixtures for e2e tests.

E2E tests exercise the full system through real subprocess boundaries:
- CLI tests spawn ``llm-wiki`` as a subprocess
- HTTP tests start a real uvicorn server and issue requests with httpx

LLM calls are intercepted by a real TCP mock server (pytest-httpserver) that speaks
the OpenAI API protocol. All subprocesses receive ``OPENAI_API_BASE`` in their environment
so litellm routes to the mock instead of a real provider. The vault config uses
``model: "openai/stub"`` which bypasses the Ollama preflight check.

Fixture dependency graph:
  vault_env ──┬──> mock_llm_server ──> live_server
              └─── (used directly by CLI and smoke tests that need no LLM)
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Generator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
import pytest
from pytest_httpserver import HTTPServer

from core.config import GlobalConfig, VaultConfig
from core.vault import init_vault

PROJECT_ROOT = Path(__file__).parent.parent.parent
VENV_BIN = PROJECT_ROOT / ".venv" / "bin"
VAULT_NAME = "E2ETestVault"

# Canned LLM ingest response — one Sources page and one Concepts page with a wikilink.
# Used by the mock server for all completions calls; lint and query both accept any string.
CANNED_INGEST_JSON = json.dumps(
    {
        "source_page": {
            "file_path": "Sources/E2E_Source.md",
            "content": (
                "---\ntitle: E2E Source\ntags: [e2e]\n---\n\n"
                "An e2e test source. See also [[E2E_Concept]].\n"
            ),
        },
        "page_updates": [
            {
                "file_path": "Concepts/E2E_Concept.md",
                "action": "create",
                "content": (
                    "---\ntitle: E2E Concept\ntags: [e2e]\n---\n\n"
                    "An e2e test concept extracted during end-to-end testing.\n"
                ),
            }
        ],
    }
)


def _free_port() -> int:
    """Bind to port 0 and return the OS-assigned port number.

    Returns:
        An available TCP port on 127.0.0.1.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_server(base_url: str, timeout: float = 15.0) -> None:
    """Poll GET /api/vaults until the server responds or the timeout expires.

    Args:
        base_url: Base URL of the server (e.g. ``http://127.0.0.1:8765``).
        timeout: Maximum seconds to wait before raising.

    Raises:
        RuntimeError: The server did not become ready within ``timeout`` seconds.
    """
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        last_exc = None
        try:
            httpx.get(f"{base_url}/api/vaults", timeout=1.0)
            return
        except Exception as exc:
            last_exc = exc
        time.sleep(0.25)
    raise RuntimeError(f"Server at {base_url} did not become ready within {timeout}s") from last_exc


@pytest.fixture
def vault_env(tmp_path: Path) -> dict[str, str]:
    """Initialise a vault and write a temp GlobalConfig. Return the subprocess environment.

    The returned dict inherits ``os.environ`` so subprocesses receive PATH, HOME, etc.
    Key additions:
    - ``LLM_WIKI_HOME``: redirects GlobalConfig away from ``~/.llm-wiki`` (test isolation).
    - ``LLM_WIKI_VAULT_DIR``: convenience key for test code to locate the vault on disk.
    - ``OPENAI_API_KEY``: satisfies litellm's auth check without a real key.
    - ``PATH``: prepended with the project venv bin so ``llm-wiki`` resolves correctly.

    The per-vault config sets ``model: "openai/stub"`` so litellm uses the OpenAI provider
    (routed to the mock server via OPENAI_API_BASE) and skips the Ollama preflight check.
    """
    llm_home = tmp_path / ".llm-wiki"
    llm_home.mkdir()
    vault_dir = tmp_path / "e2e-vault"
    init_vault(vault_dir, VAULT_NAME)
    VaultConfig(name=VAULT_NAME, model="openai/stub").save(vault_dir)

    cfg = GlobalConfig(vaults={VAULT_NAME: str(vault_dir)}, default_vault=VAULT_NAME)
    (llm_home / "config.json").write_text(json.dumps(asdict(cfg), indent=2))

    env = dict(os.environ)
    env.update(
        {
            "LLM_WIKI_HOME": str(llm_home),
            "LLM_WIKI_VAULT_DIR": str(vault_dir),
            "OPENAI_API_KEY": "test",
            "PATH": f"{VENV_BIN}:{env.get('PATH', '')}",
        }
    )
    return env


@pytest.fixture
def mock_llm_server(httpserver: HTTPServer, vault_env: dict[str, str]) -> str:
    """Configure httpserver as an OpenAI-compatible LLM mock and inject OPENAI_API_BASE.

    All ``POST /v1/chat/completions`` requests return the canned ingest JSON wrapped in
    an OpenAI chat-completion envelope. This satisfies:
    - ``ingest_source``: parses the JSON to create wiki pages.
    - ``lint_vault``: uses the raw string as a markdown quality report.
    - ``query_wiki``: uses the raw string as the answer text.

    Also mutates ``vault_env`` so that ``live_server`` and CLI subprocesses both receive
    ``OPENAI_API_BASE`` pointing at the mock.

    Returns:
        The mock server base URL (e.g. ``http://127.0.0.1:12345``).
    """
    openai_response: dict[str, Any] = {
        "id": "chatcmpl-e2e",
        "object": "chat.completion",
        "model": "stub",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": CANNED_INGEST_JSON},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 50, "total_tokens": 60},
    }
    # litellm (via the OpenAI SDK) constructs: {api_base}/chat/completions (no /v1 prefix
    # when api_base already ends with a slash). The werkzeug server receives the request
    # at /chat/completions.
    httpserver.expect_request("/chat/completions", method="POST").respond_with_json(openai_response)
    base_url = httpserver.url_for("")
    vault_env["OPENAI_API_BASE"] = base_url
    return base_url


@pytest.fixture
def live_server(vault_env: dict[str, str], mock_llm_server: str) -> Generator[str, None, None]:
    """Start a real uvicorn subprocess and yield its base URL.

    Depends on ``mock_llm_server`` to ensure ``OPENAI_API_BASE`` is set in ``vault_env``
    before the subprocess launches. The server is terminated and waited on teardown so the
    temporary vault directory can be cleaned up by pytest.

    Yields:
        The server base URL (e.g. ``http://127.0.0.1:54321``).
    """
    port = _free_port()
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "core.server:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    proc = subprocess.Popen(
        cmd,
        env=vault_env,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_server(base_url)
        yield base_url
    finally:
        proc.terminate()
        proc.wait(timeout=5)
