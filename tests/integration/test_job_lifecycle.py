"""Integration tests for the HTTP 202 ingest job lifecycle.

These tests go through the FastAPI layer with a real ThreadPoolExecutor registered
for the vault, verifying the full path:
  POST /ingest → 202 → background job runs → poll GET /jobs/{id} → terminal state
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.db import db_connection, list_pages

from .conftest import VAULT_NAME

_POLL_INTERVAL = 0.2  # seconds between status polls
_POLL_TIMEOUT = 10.0  # maximum seconds to wait for a terminal job state
_TERMINAL_STATUSES = {"done", "failed"}


def _wait_for_terminal_status(client: TestClient, job_id: str) -> dict[str, Any]:
    """Poll GET /jobs/{job_id} until status is 'done' or 'failed', then return the job dict.

    Args:
        client: The FastAPI TestClient.
        job_id: UUID of the job to poll.

    Returns:
        The final job dict at the point the terminal state was reached.

    Raises:
        AssertionError: The job did not reach a terminal state within _POLL_TIMEOUT seconds.
    """
    deadline = time.monotonic() + _POLL_TIMEOUT
    while time.monotonic() < deadline:
        response = client.get(f"/api/vaults/{VAULT_NAME}/jobs/{job_id}")
        assert response.status_code == 200
        job: dict[str, Any] = response.json()
        if job["status"] in _TERMINAL_STATUSES:
            return job
        time.sleep(_POLL_INTERVAL)
    raise AssertionError(f"Job {job_id} did not reach a terminal state within {_POLL_TIMEOUT}s")


@pytest.mark.integration
class TestJobLifecycle:
    def test_post_ingest_returns_202(
        self, api_client: TestClient, vault_path: Path, llm_stub: MagicMock
    ) -> None:
        """POST /ingest immediately returns 202 with a job_id and 'pending' status."""
        source_file = vault_path / "raw" / "job_test.txt"
        source_file.write_text("Some content.")

        response = api_client.post(
            f"/api/vaults/{VAULT_NAME}/ingest",
            json={"source": str(source_file)},
        )

        assert response.status_code == 202
        body = response.json()
        assert "job_id" in body
        assert body["status"] == "pending"

    def test_job_reaches_done_state(
        self, api_client: TestClient, vault_path: Path, llm_stub: MagicMock
    ) -> None:
        """A submitted ingest job transitions from 'pending' to 'done' via the background executor."""
        source_file = vault_path / "raw" / "job_test.txt"
        source_file.write_text("Content about neural networks.")

        post_response = api_client.post(
            f"/api/vaults/{VAULT_NAME}/ingest",
            json={"source": str(source_file)},
        )
        job_id = post_response.json()["job_id"]

        job = _wait_for_terminal_status(api_client, job_id)

        assert job["status"] == "done"

    def test_done_job_pages_appear_in_db(
        self, api_client: TestClient, vault_path: Path, llm_stub: MagicMock
    ) -> None:
        """After a job reaches 'done', the pages it wrote are queryable from the vault DB."""
        source_file = vault_path / "raw" / "job_test.txt"
        source_file.write_text("Content about neural networks.")

        post_response = api_client.post(
            f"/api/vaults/{VAULT_NAME}/ingest",
            json={"source": str(source_file)},
        )
        job_id = post_response.json()["job_id"]
        _wait_for_terminal_status(api_client, job_id)

        with db_connection(vault_path) as conn:
            pages = list_pages(conn)
        titles = [p["title"] for p in pages]
        assert "Test Source" in titles
        assert "Test Concept" in titles

    def test_failed_job_records_error_message(
        self, api_client: TestClient, vault_path: Path, llm_stub: MagicMock
    ) -> None:
        """When ingest_source raises, the job transitions to 'failed' with a non-empty error field."""
        source_file = vault_path / "raw" / "job_test.txt"
        source_file.write_text("Some content.")

        with patch("core.server.ingest_source", side_effect=RuntimeError("stub ingest failure")):
            post_response = api_client.post(
                f"/api/vaults/{VAULT_NAME}/ingest",
                json={"source": str(source_file)},
            )
            job_id = post_response.json()["job_id"]
            job = _wait_for_terminal_status(api_client, job_id)

        assert job["status"] == "failed"
        assert job["error"]
        assert "stub ingest failure" in str(job["error"])
