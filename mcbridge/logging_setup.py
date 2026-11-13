"""Logging setup: console mirror + persistent, daily-rotated log file.

Durability model: every emit flushes (StreamHandler/FileHandler flush per
record by default) and ``logging.shutdown`` — registered via ``atexit`` by
the logging module — flushes and closes all handlers on interpreter exit.
We deliberately do NOT ``os.fsync`` per record: that costs latency and SD-card
wear on the target Pi, and kernel writeback is the right durability point for
chat logs. On a clean shutdown nothing is lost; on a power cut the last few
buffered lines may be, which is acceptable for this use case.

The file handler is a ``TimedRotatingFileHandler`` rotating at midnight
(rotation happens lazily on the first emit after the boundary — fine for an
always-chatty bridge). Rotated files are gzipped in place via a
``namer``/``rotator`` pair; retention pruning accounts for the ``.gz``
suffix from Python 3.11 on, which is our floor. ``delay=True`` keeps the
file closed until the first emit; ``backupCount`` prunes rotated files
beyond the retention window.

The effective level resolves as: explicit ``log_level`` (CLI or TOML)
wins, else the ``--debug`` flag (DEBUG), else INFO — see
:func:`effective_level`.

Logging must never take the bridge down: any OSError while creating the file
handler falls back to console-only with a visible WARNING naming the path.
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import IO, Optional

from .config import Config

CONSOLE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
CONSOLE_DATEFMT = "%H:%M:%S"
FILE_DATEFMT = "%Y-%m-%d %H:%M:%S"

logger = logging.getLogger("mcbridge")


def effective_level(cfg: Config) -> int:
    """Explicit ``log_level`` (CLI/TOML) wins; else ``--debug``; else INFO."""
    if cfg.log_level is not None:
        return cfg.log_level
    return logging.DEBUG if cfg.debug else logging.INFO


def _gzip_namer(name: str) -> str:
    """Rotated-file name: append ``.gz`` (content is written by the rotator)."""
    return name + ".gz"


def _gzip_rotator(source: str, dest: str) -> None:
    """Replace a rotated log with a gzip of itself, then drop the original."""
    with open(source, "rb") as sf, open(dest, "wb") as df:
        with gzip.GzipFile(filename=dest, mode="wb", fileobj=df) as zf:
            shutil.copyfileobj(sf, zf)
    os.remove(source)


def default_log_path(cfg: Config) -> Path:
    return Path(cfg.state_dir) / "logs" / "bridge.log"


def _make_file_handler(
    path: Path, when: str, backup_count: int
) -> TimedRotatingFileHandler:
    """Create the file handler, raising OSError if the path is unusable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(
        path, when=when, backupCount=backup_count, delay=True, encoding="utf-8"
    )
    # gzip rotated files: 14 days of history at a fraction of the disk cost
    # (pruning understands the .gz suffix on Python 3.11+, our floor)
    handler.namer = _gzip_namer
    handler.rotator = _gzip_rotator
    # delay=True defers the actual open() to the first emit; probe now so a
    # bad path fails here (where we can fall back) instead of at emit time.
    with open(path, "a", encoding="utf-8"):
        pass
    handler.setFormatter(
        logging.Formatter(CONSOLE_FORMAT, datefmt=FILE_DATEFMT)
    )
    return handler


def setup_logging(
    cfg: Config,
    *,
    rotate_when: str = "midnight",
    backup_count: Optional[int] = None,
    stream: Optional[IO[str]] = None,
) -> Optional[Path]:
    """Attach console + rotating-file handlers to the ``mcbridge`` logger.

    Returns the resolved log file path, or None if file logging fell back
    to console-only. ``rotate_when``/``backup_count`` exist for tests.
    """
    if backup_count is None:
        backup_count = cfg.log_keep_days
    requested = int(backup_count)
    # Defensive clamp; load_config() already warns on nonsense values.
    backup_count = max(1, requested)

    log_path = Path(cfg.log_file).expanduser() if cfg.log_file else default_log_path(cfg)

    root = logging.getLogger("mcbridge")
    # Replace any previous handlers so repeated setup is idempotent.
    for old in list(root.handlers):
        root.removeHandler(old)
        old.close()

    level = effective_level(cfg)
    root.setLevel(level)

    console = logging.StreamHandler(stream if stream is not None else sys.stdout)
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT, datefmt=CONSOLE_DATEFMT))
    root.addHandler(console)

    if backup_count != requested:
        logger.warning(
            "log_keep_days must be >= 1 (got %d); clamping to 1", requested
        )

    try:
        file_handler = _make_file_handler(log_path, rotate_when, backup_count)
    except OSError as exc:
        logger.warning(
            "Cannot write log file %s (%s); logging to console only",
            log_path,
            exc,
        )
        return None

    root.addHandler(file_handler)
    return log_path
