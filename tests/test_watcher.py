"""Tests for core/watcher.py — VaultWatcher and _RawFolderHandler."""

from pathlib import Path

from core.db import get_db, get_pending_queue
from core.watcher import IGNORED_SUFFIXES, VaultWatcher


class TestRawFolderHandlerQueuesPaths:
    def test_stores_vault_relative_path(self, tmp_vault: Path) -> None:
        """Detected file is stored in the queue as a vault-relative path, not absolute."""
        raw_file = tmp_vault / "raw" / "paper.pdf"
        raw_file.parent.mkdir(parents=True, exist_ok=True)
        raw_file.touch()

        watcher = VaultWatcher(tmp_vault)
        watcher._handler._handle(str(raw_file))

        conn = get_db(tmp_vault)
        pending = get_pending_queue(conn)
        conn.close()

        assert len(pending) == 1
        stored = pending[0]["file_path"]
        assert stored == "raw/paper.pdf"
        assert not Path(stored).is_absolute()

    def test_on_file_callback_receives_absolute_path(self, tmp_vault: Path) -> None:
        """The on_file callback still receives the absolute path for immediate use."""
        raw_file = tmp_vault / "raw" / "doc.txt"
        raw_file.parent.mkdir(parents=True, exist_ok=True)
        raw_file.touch()

        received: list[str] = []
        watcher = VaultWatcher(tmp_vault, on_file=received.append)
        watcher._handler._handle(str(raw_file))

        assert received == [str(raw_file)]

    def test_ignores_dotfiles(self, tmp_vault: Path) -> None:
        """Hidden files are silently ignored."""
        raw_file = tmp_vault / "raw" / ".DS_Store"

        watcher = VaultWatcher(tmp_vault)
        watcher._handler._handle(str(raw_file))

        conn = get_db(tmp_vault)
        pending = get_pending_queue(conn)
        conn.close()
        assert pending == []

    def test_ignores_known_suffixes(self, tmp_vault: Path) -> None:
        """Files with ignored suffixes (e.g. .tmp) are not queued."""
        for suffix in list(IGNORED_SUFFIXES)[:2]:
            raw_file = tmp_vault / "raw" / f"file{suffix}"
            watcher = VaultWatcher(tmp_vault)
            watcher._handler._handle(str(raw_file))

        conn = get_db(tmp_vault)
        pending = get_pending_queue(conn)
        conn.close()
        assert pending == []


class TestVaultWatcherLifecycle:
    def test_is_alive_after_start_false_before(self, tmp_vault: Path) -> None:
        """Watcher is not alive before start() is called."""
        watcher = VaultWatcher(tmp_vault)
        assert not watcher.is_alive()

    def test_start_creates_raw_directory(self, tmp_vault: Path) -> None:
        """start() creates raw/ if it does not exist."""
        raw = tmp_vault / "raw"
        if raw.exists():
            raw.rmdir()
        watcher = VaultWatcher(tmp_vault)
        watcher.start()
        watcher.stop()
        assert raw.exists()
