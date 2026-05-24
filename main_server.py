#!/usr/bin/env python3
"""
Startup entry point: launches FastAPI server + watchdog watchers for all vaults.
Usage: python main_server.py [--port 8000]
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

import uvicorn

from core.config import GlobalConfig
from core.watcher import VaultWatcher

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="llm-wiki server")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    config = GlobalConfig.load()
    port = args.port or config.server_port

    if not config.vaults:
        log.warning("No vaults registered. Run `llm-wiki init <path>` first.")

    # Start one watchdog watcher per vault
    watchers: list[VaultWatcher] = []
    for vname, vpath_str in config.vaults.items():
        vpath = Path(vpath_str)
        if vpath.exists():
            log.info("Starting watcher for vault '%s'", vname)
            w = VaultWatcher(vpath)
            w.start()
            watchers.append(w)

    def shutdown(*_):
        log.info("Shutting down watchers…")
        for w in watchers:
            w.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info("Starting llm-wiki server at http://%s:%d", args.host, port)
    from core.server import app

    uvicorn.run(app, host=args.host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
