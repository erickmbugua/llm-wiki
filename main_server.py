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
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import uvicorn

from core.config import GlobalConfig, VaultConfig
from core.db import create_job, get_db
from core.server import register_vault_executor, run_ingest_job
from core.watcher import VaultWatcher

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


def _make_ingest_callback(
    vpath: Path, vname: str, executor: ThreadPoolExecutor
) -> Callable[[str], None]:
    """Return an on_file callback that creates an ingest job and submits it to the executor.

    The callback is called from the watchdog thread whenever a file lands in raw/.
    Each detected file gets its own job record so the dashboard can display its progress.
    Using max_workers=1 ensures LLM calls are serialised per vault.

    Args:
        vpath: Root directory of the vault.
        vname: Human-readable vault name forwarded to the ingest worker.
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
        job_id = str(uuid.uuid4())
        conn = get_db(vpath)
        try:
            create_job(conn, job_id=job_id, vault=vname, source=file_path)
        finally:
            conn.close()
        executor.submit(run_ingest_job, vpath, vname, file_path, job_id, False)

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
        register_vault_executor(vname, executor)

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
