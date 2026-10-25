"""Keeps two unattended runs from overlapping."""
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .config import STATE_DIR

LOCK_FILE = STATE_DIR / "run.lock"
STALE_AFTER_SECONDS = 6 * 3600  # a lock older than this is assumed to be left over from a crashed run


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def single_run(path: Path = LOCK_FILE, stale_after: float = STALE_AFTER_SECONDS):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if time.time() - path.stat().st_mtime < stale_after:
            raise AlreadyRunning(f"another run holds {path.name}") from None
        path.unlink()
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        yield
    finally:
        os.close(descriptor)
        path.unlink(missing_ok=True)
