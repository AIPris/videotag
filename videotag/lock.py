"""Keeps two unattended runs from overlapping."""
import os
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path

from .config import STATE_DIR

LOCK_FILE = STATE_DIR / "run.lock"
STALE_AFTER_SECONDS = 6 * 3600  # a lock nobody has refreshed for this long is assumed to be left over from a crashed run
HEARTBEAT_SECONDS = 60          # a live run refreshes its lock this often, so a run of any length keeps it


class AlreadyRunning(RuntimeError):
    pass


def taken_at(path: Path) -> float | None:
    """When the run that holds the lock took it, or None without a lock. The refreshing moves the file's own time,
    so the run writes its start into the file; a lock without that record counts from when it was last written."""
    try:
        try:
            return float(path.read_text(encoding="utf-8"))
        except ValueError:
            return path.stat().st_mtime
    except FileNotFoundError:
        return None


def _keep_fresh(path: Path, stop: threading.Event, interval: float) -> None:
    while not stop.wait(interval):
        with suppress(OSError):  # the lock is gone or out of reach for a moment: the next beat tries again
            os.utime(path)


@contextmanager
def single_run(path: Path = LOCK_FILE, stale_after: float = STALE_AFTER_SECONDS, heartbeat: float = HEARTBEAT_SECONDS):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if time.time() - path.stat().st_mtime < stale_after:
            raise AlreadyRunning(f"another run holds {path.name}") from None
        path.unlink()
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    stop = threading.Event()
    beat = threading.Thread(target=_keep_fresh, args=(path, stop, heartbeat), name="videotag-lock-heartbeat", daemon=True)
    try:
        os.write(descriptor, str(time.time()).encode())
        beat.start()
        yield
    finally:
        stop.set()
        if beat.is_alive():
            beat.join()
        os.close(descriptor)
        path.unlink(missing_ok=True)
