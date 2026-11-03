"""Logging: one line format for our modules and uvicorn, written to stdout.

Under systemd stdout lands in journald (`journalctl -u localktv`), so there
is no log file to rotate.  KTV_LOG_LEVEL picks the level (default INFO).

Never log passwords, cookies, session/invite tokens, KTV_ACCESS_KEY or the
YouTube cookies file — names, track ids, IPs and timings only.
"""

from __future__ import annotations

import logging
import os
import sys

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def level_from_env() -> str:
    level = os.environ.get("KTV_LOG_LEVEL", "INFO").strip().upper()
    return level if level in LEVELS else "INFO"


def setup() -> None:
    """Configure the root logger; uvicorn's loggers propagate into it."""
    stream = sys.stdout
    try:
        # a cp950 Windows console must not turn a song title into a logging error
        stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(FORMAT))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level_from_env())
    # uvicorn installs its own handlers only when given a log_config; we pass
    # none, so make sure nothing left over prints a second copy.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
