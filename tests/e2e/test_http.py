"""E2E HTTP tests — real uvicorn server exercised via httpx.

Tests issue real TCP requests to a live uvicorn process started in a subprocess.
LLM calls are intercepted by the TCP mock server (pytest-httpserver). Each test
gets a fresh server and empty vault (function-scoped fixtures).

Tests that require pages in the database call ``_ingest_and_wait`` as part of
their setup so they are fully self-contained and order-independent.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from .conftest import VAULT_NAME

pytestmark = pytest.mark.e2e

_POLL_INTERVAL = 0.25
_POLL_TIMEOUT = 15.0
_TERMINAL = {"done", "failed"}


def _ingest_and_wait(
    base_url: str,
    source_path: Path,
    timeout: float = _POLL_TIMEOUT,
) -> dict[str, Any]:
    """POST an ingest request and poll until the job reaches a terminal state.

    Args:
        base_url: Base URL of the live server.
        source_path: Absolute path of the source file to ingest.
        timeout: Maximum seconds to wait for a terminal job status.

    Returns:
        The final job dict (with ``status``, ``job_id``, and optional ``error`` keys).

    Raises:
        AssertionError: POST did not return 202, or job did not reach terminal state.
    """
    r = httpx.post(
        f"{base_url}/api/vaults/{VAULT_NAME}/ingest",
        json={"source": str(source_path)},
        timeout=10.0,
    )
    assert r.status_code == 202, f"Expected 202, got {r.status_code}: {r.text}"
    job_id: str = r.json()["job_id"]

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        poll = httpx.get(f"{base_url}/api/vaults/{VAULT_NAME}/jobs/{job_id}", timeout=5.0)
        job: dict[str, Any] = poll.json()
        if job["status"] in _TERMINAL:
            return job
        time.sleep(_POLL_INTERVAL)

    raise AssertionError(f"Job {job_id} did not reach terminal state within {timeout}s")


class TestStatelessEndpoints:
    """Tests that require no prior data in the vault."""

    def test_get_vaults_lists_registered_vault(self, live_server: str) -> None:
        """GET /api/vaults returns a JSON body containing the registered vault."""
        r = httpx.get(f"{live_server}/api/vaults")
        assert r.status_code == 200
        assert VAULT_NAME in r.json()["vaults"]

    def test_vault_status_returns_200(self, live_server: str) -> None:
        """GET /api/vaults/{vault}/status returns 200 with the vault name."""
        r = httpx.get(f"{live_server}/api/vaults/{VAULT_NAME}/status")
        assert r.status_code == 200
        body = r.json()
        assert "name" in body
        assert body["name"] == VAULT_NAME

    def test_ingest_returns_202_with_job_id(
        self, live_server: str, vault_env: dict[str, str]
    ) -> None:
        """POST /api/vaults/{vault}/ingest immediately returns 202 with a pending job."""
        source = Path(vault_env["LLM_WIKI_VAULT_DIR"]) / "raw" / "http_test.txt"
        source.write_text("HTTP test article.")
        r = httpx.post(
            f"{live_server}/api/vaults/{VAULT_NAME}/ingest",
            json={"source": str(source)},
            timeout=10.0,
        )
        assert r.status_code == 202
        body = r.json()
        assert "job_id" in body
        assert body["status"] == "pending"

    def test_failed_ingest_of_missing_source_records_error(self, live_server: str) -> None:
        """Ingesting a non-existent path produces a job with status 'failed' and an error."""
        job = _ingest_and_wait(live_server, Path("/nonexistent/missing_file.txt"))
        assert job["status"] == "failed"
        assert job.get("error")


class TestEndpointsWithData:
    """Tests that ingest one source as part of setup, then assert on the resulting data."""

    def test_job_reaches_done_state(self, live_server: str, vault_env: dict[str, str]) -> None:
        """An ingest job transitions from 'pending' to 'done' via the background executor."""
        source = Path(vault_env["LLM_WIKI_VAULT_DIR"]) / "raw" / "done_test.txt"
        source.write_text("Content about neural networks and backpropagation.")
        job = _ingest_and_wait(live_server, source)
        assert job["status"] == "done"

    def test_pages_visible_after_ingest(self, live_server: str, vault_env: dict[str, str]) -> None:
        """GET /pages lists the pages written by the background ingest job."""
        source = Path(vault_env["LLM_WIKI_VAULT_DIR"]) / "raw" / "pages_test.txt"
        source.write_text("Article about transformers and attention mechanisms.")
        _ingest_and_wait(live_server, source)

        r = httpx.get(f"{live_server}/api/vaults/{VAULT_NAME}/pages", timeout=5.0)
        assert r.status_code == 200
        titles = [p["title"] for p in r.json()["pages"]]
        assert "E2E Source" in titles
        assert "E2E Concept" in titles

    def test_search_returns_results_after_ingest(
        self, live_server: str, vault_env: dict[str, str]
    ) -> None:
        """GET /search returns non-empty results once pages are in the FTS5 index."""
        source = Path(vault_env["LLM_WIKI_VAULT_DIR"]) / "raw" / "search_test.txt"
        source.write_text("Article about deep learning and gradient descent.")
        _ingest_and_wait(live_server, source)

        r = httpx.get(
            f"{live_server}/api/vaults/{VAULT_NAME}/search",
            params={"q": "concept"},
            timeout=5.0,
        )
        assert r.status_code == 200
        assert len(r.json()["results"]) > 0

    def test_query_endpoint_returns_200(self, live_server: str, vault_env: dict[str, str]) -> None:
        """POST /query returns 200 with an answer field after pages exist in the vault."""
        source = Path(vault_env["LLM_WIKI_VAULT_DIR"]) / "raw" / "query_test.txt"
        source.write_text("Article about attention mechanisms in transformers.")
        _ingest_and_wait(live_server, source)

        r = httpx.post(
            f"{live_server}/api/vaults/{VAULT_NAME}/query",
            json={"question": "What is an e2e concept?"},
            timeout=15.0,
        )
        assert r.status_code == 200
        assert "answer" in r.json()

    def test_graph_endpoint_returns_nodes_and_edges(
        self, live_server: str, vault_env: dict[str, str]
    ) -> None:
        """GET /graph returns a payload with 'nodes' and 'edges' after ingest."""
        source = Path(vault_env["LLM_WIKI_VAULT_DIR"]) / "raw" / "graph_test.txt"
        source.write_text("Article about knowledge graphs and linked concepts.")
        _ingest_and_wait(live_server, source)

        r = httpx.get(f"{live_server}/api/vaults/{VAULT_NAME}/graph", timeout=5.0)
        assert r.status_code == 200
        body = r.json()
        assert "nodes" in body
        assert "edges" in body
        assert len(body["nodes"]) > 0
