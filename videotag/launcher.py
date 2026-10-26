"""Starts a run for the GUI.

The run is the real `videotag run` command in its own process, not a call into the pipeline: it takes the run lock
itself (the GUI never holds it, so an open dashboard cannot make the scheduled task skip a day), writes the same
log and report as any other run, and keeps going if the GUI is closed.
"""
import subprocess
import sys
from datetime import datetime

from .dashboard import Sources, run_status
from .lock import AlreadyRunning

MAX_WRITES_LIMIT = 100_000


class LaunchError(ValueError):
    """The options were refused; the message says why and is safe to show."""


def _command(apply: bool, max_writes: int, describe: bool) -> list[str]:
    command = [sys.executable, "-I", "-m", "videotag", "run"]
    if apply:
        command += ["--apply", "--max-writes", str(max_writes)]
    if not describe:
        command.append("--no-describe")
    return command


def _detached() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def start_run(src: Sources, now: datetime, *, apply: bool, max_writes: int, describe: bool, popen=subprocess.Popen) -> None:
    if not isinstance(max_writes, int) or isinstance(max_writes, bool) or not 1 <= max_writes <= MAX_WRITES_LIMIT:
        raise LaunchError(f"max_writes must be a whole number from 1 to {MAX_WRITES_LIMIT}")
    if run_status(src, now)["state"] == "running":
        raise AlreadyRunning("a run is already in progress")
    popen(_command(apply, max_writes, describe), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
          stderr=subprocess.DEVNULL, **_detached())
