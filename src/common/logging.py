"""Structured logging for pipeline runs.

Console output on Windows defaults to cp1252, so logging a review containing an
emoji or Devanagari raises ``UnicodeEncodeError`` and kills a run that may have
been going for days (edge-case 0.1). Both handlers are therefore forced to UTF-8
with replacement, and the file handler is the one to trust for exact text.

Hardening covers the whole process rather than just the log handler, because a
stage's own ``print`` output shares the same stream: ``build_corpus`` completed
every stage and still exited 1 on a single ``⚠`` in its funnel report. Since every
entry point already calls ``setup_logging`` first, that is the reliable place to
do it. See :mod:`src.common.encoding` for the input-side half of the same problem.

Callers log ``doc_id`` rather than document text; a helper is provided for the
rare case where a snippet genuinely helps debugging.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.common.encoding import harden_stdio

_CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s %(name)-24s %(message)s"
_FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(filename)s:%(lineno)d %(message)s"
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

_configured = False


def new_run_id(stage: str) -> str:
    """Sortable, unique-enough run id: ``20260818T231500Z-collect``."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{stage}"


def setup_logging(
    run_id: str,
    logs_dir: Path,
    level: int = logging.INFO,
    console_level: int | None = None,
) -> logging.Logger:
    """Attach console + file handlers to the root logger. Idempotent per process."""
    global _configured

    # Ahead of the idempotence guard: cheap, and a caller re-entering after
    # something replaced sys.stdout still gets a UTF-8 stream.
    harden_stdio()

    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)

    if _configured:
        return root

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(console_level if console_level is not None else level)
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT, _TIME_FORMAT))
    root.addHandler(console)

    file_handler = logging.FileHandler(
        logs_dir / f"{run_id}.log", encoding="utf-8", errors="replace"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT, _TIME_FORMAT))
    root.addHandler(file_handler)

    # praw and googleapiclient are chatty at INFO and drown the pipeline's own output.
    for noisy in ("praw", "prawcore", "urllib3", "googleapiclient", "google_auth_httplib2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    root.debug("Logging initialised: run_id=%s logs_dir=%s", run_id, logs_dir)
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def safe_snippet(text: str, limit: int = 120) -> str:
    """Single-line, length-capped text for log messages."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "\u2026"
