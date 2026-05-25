from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from watchdog.events import (
    DirCreatedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileMovedEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

from .db import db_connection, queue_raw_file

__all__ = ["VaultWatcher", "IGNORED_SUFFIXES"]

log = logging.getLogger(__name__)

IGNORED_SUFFIXES = frozenset({".db", ".tmp", ".part", ".crdownload"})


class _RawFolderHandler(FileSystemEventHandler):
    def __init__(self, vault_path: Path, on_file: Callable[[str], None] | None):
        self.vault_path = vault_path
        self.on_file = on_file

    def _handle(self, path: str) -> None:
        p = Path(path)
        if p.suffix.lower() in IGNORED_SUFFIXES or p.name.startswith("."):
            return
        log.info("Raw file detected: %s", p.name)
        with db_connection(self.vault_path) as conn:
            queue_raw_file(conn, str(p.relative_to(self.vault_path)))
        if self.on_file:
            self.on_file(str(p))

    def on_created(self, event: DirCreatedEvent | FileCreatedEvent) -> None:
        if not event.is_directory:
            self._handle(str(event.src_path))

    def on_moved(self, event: DirMovedEvent | FileMovedEvent) -> None:
        if not event.is_directory:
            self._handle(str(event.dest_path))


class VaultWatcher:
    """Watches a vault's raw/ directory and queues new files for ingest."""

    def __init__(self, vault_path: Path, on_file: Callable[[str], None] | None = None):
        self.vault_path = vault_path
        self.raw_path = vault_path / "raw"
        self._observer = Observer()
        self._handler = _RawFolderHandler(vault_path, on_file)

    def start(self) -> None:
        self.raw_path.mkdir(exist_ok=True)
        self._observer.schedule(self._handler, str(self.raw_path), recursive=False)
        self._observer.start()
        log.info("Watching %s", self.raw_path)

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join()

    def is_alive(self) -> bool:
        return self._observer.is_alive()
