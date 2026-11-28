"""The one logging setup of the collector (logger "faf").

Hooks log to .faf/local/collector.log only: hook output can reach the agent's
context. The installer and manual commands also log to the console.

Allowed in a line: time, collector version, hook event name, counts, HTTP status,
duration, endpoint host, upload on/off, files the installer changed. Never: the
key, repo, branch, task, session, model, tokens, instruction paths or content,
server response bodies, exception messages.
"""
import logging
import sys
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER = logging.getLogger("faf")
LOG_MAX_BYTES = 1_000_000


class _Formatter(logging.Formatter):
    def __init__(self, tag):
        super().__init__()
        self.tag = tag

    def format(self, record):
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        return f"{stamp} {self.tag}{record.getMessage()}"


class _LocalFileHandler(RotatingFileHandler):
    """Writes only while .faf/local exists; never creates it."""

    def __init__(self, path):
        super().__init__(path, maxBytes=LOG_MAX_BYTES, backupCount=1, encoding="utf-8", delay=True)

    def emit(self, record):
        if Path(self.baseFilename).parent.is_dir():
            super().emit(record)

    def handleError(self, record):
        pass  # logging must never break a hook


def setup(root, console=False, event=None):
    """Route the "faf" logger to <root>/.faf/local/collector.log, and the console if asked."""
    close()
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    if root is not None:
        handler = _LocalFileHandler(str(Path(root) / ".faf" / "local" / "collector.log"))
        handler.setFormatter(_Formatter(f"[{event}] " if event else ""))
        LOGGER.addHandler(handler)
    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(logging.Formatter("[faf] %(message)s"))
        LOGGER.addHandler(stream)
    return LOGGER


def close():
    """Close the handlers, so the log file is not held open (Windows)."""
    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        handler.close()


def crash_line(exc):
    """`error <Type> at <file>:<line>` of the innermost frame; no message, no path."""
    frames = traceback.extract_tb(exc.__traceback__)
    where = f" at {Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else ""
    return f"error {type(exc).__name__}{where}"
