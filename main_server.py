#!/usr/bin/env python3
"""
Startup entry point: launches FastAPI server + watchdog watchers for all vaults.

Each vault gets a dedicated single-threaded executor so that files dropped into
raw/ are automatically ingested serially — one LLM call at a time per vault.
This keeps memory pressure manageable on machines running a local 7B model.

Usage: python main_server.py [--port 8000]
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import uvicorn

from core.config import GlobalConfig, VaultConfig
from core.ingest import ingest_queued
from core.watcher import VaultWatcher

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


def _run_ingest(vpath: Path, vname: str) -> None:
    """Drain the ingest queue for a vault, logging a summary when done.

    Intended to run inside a ThreadPoolExecutor worker — never on the event loop.

    Args:
        vpath: Root directory of the vault.
        vname: Human-readable vault name used in log output.
    """
    try:
        results = ingest_queued(vpath, vname)
        done = sum(1 for r in results if r["status"] == "done")
        failed = sum(1 for r in results if r["status"] == "failed")
        log.info("Auto-ingest '%s': %d done, %d failed", vname, done, failed)
    except Exception:
        log.exception("Auto-ingest failed unexpectedly for vault '%s'", vname)


def _make_ingest_callback(
    vpath: Path, vname: str, executor: ThreadPoolExecutor
) -> Callable[[str], None]:
    """Return an on_file callback that submits a queue-drain task to the executor.

    The callback is called from the watchdog thread whenever a file lands in raw/.
    It schedules _run_ingest on the executor without blocking the watchdog thread.
    Using max_workers=1 ensures LLM calls are serialised per vault.

    Args:
        vpath: Root directory of the vault.
        vname: Human-readable vault name forwarded to _run_ingest.
        executor: Single-worker ThreadPoolExecutor dedicated to this vault.

    Returns:
        A callable that accepts the new file's absolute path string.
    """

    def _callback(file_path: str) -> None:
        log.info(
            "Detected '%s' in raw/ — scheduling auto-ingest for vault '%s'",
            Path(file_path).name,
            vname,
        )
        executor.submit(_run_ingest, vpath, vname)

    return _callback


def main() -> None:
    """Start the llm-wiki FastAPI server and one watchdog watcher per registered vault."""
    parser = argparse.ArgumentParser(description="llm-wiki server")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    config = GlobalConfig.load()
    port = args.port or config.server_port

    if not config.vaults:
        log.warning("No vaults registered. Run `llm-wiki init <path>` first.")

    # Start one watcher + one single-worker executor per vault.
    watchers: list[VaultWatcher] = []
    executors: list[ThreadPoolExecutor] = []

    for vname, vpath_str in config.vaults.items():
        vpath = Path(vpath_str)
        if not vpath.exists():
            log.warning("Vault '%s' path does not exist, skipping: %s", vname, vpath_str)
            continue

        vcfg = VaultConfig.load(vpath)
        effective_name = vcfg.name or vname

        # max_workers=1 serialises LLM calls so a local 7B model isn't overloaded.
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"ingest-{vname}")
        executors.append(executor)

        callback = _make_ingest_callback(vpath, effective_name, executor)
        w = VaultWatcher(vpath, on_file=callback)
        w.start()
        watchers.append(w)
        log.info("Watching vault '%s' at %s (auto-ingest enabled)", effective_name, vpath)

    def shutdown(*_: object) -> None:
        log.info("Shutting down watchers and ingest executors…")
        for w in watchers:
            w.stop()
        for ex in executors:
            # wait=False: don't block shutdown if an LLM call is in progress.
            ex.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info("Starting llm-wiki server at http://%s:%d", args.host, port)
    from core.server import app

    uvicorn.run(app, host=args.host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
