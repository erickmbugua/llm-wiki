"""Integration tests for the file-watcher pipeline.

These tests start a real VaultWatcher (watchdog observer thread) and drop files into
the raw/ directory, verifying that:
  - The on_file callback fires for new files
  - Ignored files (dotfiles, known temp suffixes) do not fire the callback
  - A dropped file is queued in the DB
  - The full watcher → ingest pipeline produces pages in the DB (with LLM stubbed)
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.db import db_connection, get_pending_queue, list_pages
from core.ingest import ingest_source
from core.watcher import VaultWatcher

from .conftest import VAULT_NAME

_WATCH_TIMEOUT = 5.0  # maximum seconds to wait for a watchdog callback
_WATCH_INTERVAL = 0.1  # seconds between polls


@pytest.mark.integration
class TestWatcherPipeline:
    def test_file_drop_fires_callback(self, vault_path: Path) -> None:
        """Dropping a file into raw/ calls the on_file callback with its absolute path."""
        received: list[str] = []
        watcher = VaultWatcher(vault_path, on_file=received.append)
        watcher.start()
        try:
            (vault_path / "raw" / "test_article.txt").write_text("Hello from the watcher test.")

            deadline = time.monotonic() + _WATCH_TIMEOUT
            while time.monotonic() < deadline and not received:
                time.sleep(_WATCH_INTERVAL)

            assert received, "on_file callback was never called"
            assert "test_article.txt" in received[0]
        finally:
            watcher.stop()

    def test_file_drop_queues_db_entry(self, vault_path: Path) -> None:
        """A file dropped into raw/ is recorded in the DB queue with status 'pending'."""
        watcher = VaultWatcher(vault_path)
        watcher.start()
        try:
            (vault_path / "raw" / "queued_doc.txt").write_text("Doc to be queued.")

            deadline = time.monotonic() + _WATCH_TIMEOUT
            while time.monotonic() < deadline:
                with db_connection(vault_path) as conn:
                    pending = get_pending_queue(conn)
                if pending:
                    break
                time.sleep(_WATCH_INTERVAL)

            with db_connection(vault_path) as conn:
                pending = get_pending_queue(conn)
            assert pending, "No pending queue entries found after file drop"
            paths = [item["file_path"] for item in pending]
            assert any("queued_doc.txt" in p for p in paths)
        finally:
            watcher.stop()

    def test_watcher_ignores_dotfiles(self, vault_path: Path) -> None:
        """Hidden files (dotfiles) must not trigger the on_file callback."""
        received: list[str] = []
        watcher = VaultWatcher(vault_path, on_file=received.append)
        watcher.start()
        try:
            (vault_path / "raw" / ".hidden_file").write_text("This should be ignored.")
            time.sleep(_WATCH_TIMEOUT / 2)

            assert not received, f"Callback fired for a dotfile: {received}"
        finally:
            watcher.stop()

    def test_full_watcher_ingest_pipeline(self, vault_path: Path, llm_stub: MagicMock) -> None:
        """Watcher detects a dropped file, ingest_source runs, and pages appear in the DB.

        The on_file callback calls ingest_source directly, mirroring how main_server.py
        wires the watcher to the executor (simplified here to run synchronously).
        """

        def _on_file(abs_path: str) -> None:
            ingest_source(vault_path, abs_path, VAULT_NAME)

        watcher = VaultWatcher(vault_path, on_file=_on_file)
        watcher.start()
        try:
            (vault_path / "raw" / "wired_article.txt").write_text(
                "An article about transformers and attention mechanisms."
            )

            deadline = time.monotonic() + _WATCH_TIMEOUT
            pages_found = False
            while time.monotonic() < deadline:
                with db_connection(vault_path) as conn:
                    pages = list_pages(conn)
                titles = [p["title"] for p in pages]
                if "Test Source" in titles and "Test Concept" in titles:
                    pages_found = True
                    break
                time.sleep(_WATCH_INTERVAL)

            assert pages_found, "Pages did not appear in the DB after watcher-triggered ingest"
        finally:
            watcher.stop()
