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
from .sampling import PRESETS
from .slices import Slice, SliceError

MAX_WRITES_LIMIT = 100_000


class LaunchError(ValueError):
    """The options were refused; the message says why and is safe to show."""


def _command(apply: bool, max_writes: int, describe: bool, preset: str | None, chosen: Slice, reanalyse: bool) -> list[str]:
    command = [sys.executable, "-I", "-m", "videotag", "run"]
    if apply:
        command += ["--apply", "--max-writes", str(max_writes)]
    command.append("--describe" if describe else "--no-describe")  # always said: a flag beats the saved settings
    if preset is not None:
        command += ["--preset", preset]
    if chosen.limit is not None:
        command += ["--limit", str(chosen.limit)]
    if chosen.new_only:
        command.append("--new-only")
    if chosen.album_id is not None:
        command += ["--album", str(chosen.album_id)]
    if reanalyse:
        command.append("--reanalyse")
    return command


def _detached() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def start_run(src: Sources, now: datetime, *, apply: bool, max_writes: int, describe: bool, preset: str | None = None,
              limit: int | None = None, new_only: bool = False, album: int | None = None, reanalyse: bool = False,
              popen=subprocess.Popen) -> None:
    if not isinstance(max_writes, int) or isinstance(max_writes, bool) or not 1 <= max_writes <= MAX_WRITES_LIMIT:
        raise LaunchError(f"max_writes must be a whole number from 1 to {MAX_WRITES_LIMIT}")
    if preset is not None and (not isinstance(preset, str) or preset not in PRESETS):
        raise LaunchError(f"preset must be one of: {', '.join(PRESETS)}")
    try:
        chosen = Slice(limit, new_only, album)
    except SliceError as exc:
        raise LaunchError(str(exc)) from None
    if apply and chosen.narrows:
        raise LaunchError("a limit, 'not analysed yet' or an album is for a dry run: a real run always covers the whole library")
    if run_status(src, now)["state"] == "running":
        raise AlreadyRunning("a run is already in progress")
    popen(_command(apply, max_writes, describe, preset, chosen, reanalyse), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
          stderr=subprocess.DEVNULL, **_detached())
