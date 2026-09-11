"""
Centralized logging and in-memory trace buffer for Woeyyy Discord Bot.
Provides rotating file logs and an in-memory ring buffer for quick tracing.
"""

import collections
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import threading
from typing import List, Optional

LOG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "logs"))
DEFAULT_LOG_FILE = os.path.join(LOG_DIR, "bot.log")
DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
DEFAULT_BACKUP_COUNT = 3
MAX_RECENT_LOGS = 250


class RecentLogsHandler(logging.Handler):
    """Thread-safe circular in-memory buffer capturing recent formatted log lines."""

    def __init__(self, maxlen: int = MAX_RECENT_LOGS):
        super().__init__()
        self._buffer: collections.deque = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            with self._lock:
                self._buffer.append((record.levelno, msg))
        except Exception:
            self.handleError(record)

    def get_recent(self, limit: int = 25, min_level: int = logging.INFO) -> List[str]:
        """Return the most recent formatted log lines matching the minimum level."""
        with self._lock:
            filtered = [
                msg for level, msg in self._buffer
                if level >= min_level
            ]
            return list(filtered[-limit:])

    def clear(self):
        with self._lock:
            self._buffer.clear()


# Global singleton handler for in-memory recent traces
_recent_handler: Optional[RecentLogsHandler] = None
_configured: bool = False


def setup_logging(
    log_file: Optional[str] = None,
    log_level: int = logging.INFO,
    to_console: bool = True,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> logging.Logger:
    """
    Initialize centralized logging with rotating file handler, console handler,
    and in-memory ring buffer. Safe to call multiple times.
    """
    global _recent_handler, _configured

    root_logger = logging.getLogger("woeyyy")
    root_logger.setLevel(log_level)

    if _configured:
        return root_logger

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 1. Rotating File Handler
    if log_file is None:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            target_log_file = DEFAULT_LOG_FILE
        except Exception:
            target_log_file = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bot.log"))
    else:
        target_log_file = log_file

    try:
        os.makedirs(os.path.dirname(target_log_file), exist_ok=True)
        file_handler = RotatingFileHandler(
            target_log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(log_level)
        root_logger.addHandler(file_handler)
    except Exception as e:
        sys.stderr.write(f"[WARNING] Failed to initialize file logger at {target_log_file}: {e}\n")

    # 2. In-Memory Ring Buffer Handler for Fast Tracing
    _recent_handler = RecentLogsHandler(maxlen=MAX_RECENT_LOGS)
    _recent_handler.setFormatter(formatter)
    _recent_handler.setLevel(logging.DEBUG)
    root_logger.addHandler(_recent_handler)

    # 3. Console Stream Handler
    if to_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(log_level)
        root_logger.addHandler(console_handler)

    _configured = True
    return root_logger


def get_logger(name: str = "woeyyy") -> logging.Logger:
    """Return a child logger under the centralized woeyyy namespace."""
    if not _configured:
        setup_logging()
    if name in ("woeyyy", ""):
        return logging.getLogger("woeyyy")
    return logging.getLogger(f"woeyyy.{name}")


def get_recent_logs(limit: int = 25, min_level: int = logging.INFO) -> List[str]:
    """Retrieve the most recent log entries from the in-memory trace buffer."""
    if _recent_handler is None:
        setup_logging()
    if _recent_handler is not None:
        return _recent_handler.get_recent(limit=limit, min_level=min_level)
    return []


def clear_recent_logs():
    """Clear the in-memory trace buffer."""
    if _recent_handler is not None:
        _recent_handler.clear()
