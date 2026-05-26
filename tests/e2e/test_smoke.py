"""E2E smoke tests — server startup and basic surface checks.

These tests verify that the server starts correctly, core routes respond with
the right status codes and content types, and the CLI init/list commands work
end-to-end. No ingest or LLM calls are made.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
import pytest

from .conftest import VAULT_NAME

pytestmark = pytest.mark.e2e


class TestServerSmoke:
    def test_root_returns_html(self, live_server: str) -> None:
        """GET / returns 200 with HTML content."""
        r = httpx.get(f"{live_server}/", follow_redirects=True)
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")

    def test_api_vaults_lists_registered_vault(self, live_server: str) -> None:
        """GET /api/vaults returns a JSON object with the test vault registered."""
        r = httpx.get(f"{live_server}/api/vaults")
        assert r.status_code == 200
        body = r.json()
        assert "vaults" in body
        assert VAULT_NAME in body["vaults"]

    def test_vault_status_returns_200(self, live_server: str) -> None:
        """GET /api/vaults/{vault}/status returns 200 with a stats payload."""
        r = httpx.get(f"{live_server}/api/vaults/{VAULT_NAME}/status")
        assert r.status_code == 200
        body = r.json()
        assert "name" in body
        assert body["name"] == VAULT_NAME

    def test_static_css_served(self, live_server: str) -> None:
        """Static files are mounted and served from /static/."""
        r = httpx.get(f"{live_server}/static/css/style.css")
        assert r.status_code == 200


class TestCLISmoke:
    def test_cli_init_creates_vault_structure(
        self, vault_env: dict[str, str], tmp_path: Path
    ) -> None:
        """``llm-wiki init`` creates raw/, wiki/, and .llm-wiki/ directories on disk."""
        new_vault = tmp_path / "smoke-vault"
        result = subprocess.run(
            ["llm-wiki", "init", str(new_vault), "--name", "SmokeVault"],
            capture_output=True,
            text=True,
            env=vault_env,
        )
        assert result.returncode == 0, result.stderr
        assert (new_vault / "raw").is_dir()
        assert (new_vault / "wiki").is_dir()
        assert (new_vault / ".llm-wiki").is_dir()

    def test_cli_list_exits_zero(self, vault_env: dict[str, str]) -> None:
        """``llm-wiki list`` exits 0 and prints the registered vault name."""
        result = subprocess.run(
            ["llm-wiki", "list"],
            capture_output=True,
            text=True,
            env=vault_env,
        )
        assert result.returncode == 0, result.stderr
        assert VAULT_NAME in result.stdout
