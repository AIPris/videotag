"""The results of the run in progress, written as they are made so the dashboard can show them before the report exists.

One JSON line per video, in the same shape as a row of the report, after a first line that says when the run began. It
lives next to the reports but is not one (reports are `run-*.json`): a run that dies leaves it behind, and the dashboard
only shows it for a run that is still going and began at or after the file's own start. The finished report replaces it.
"""
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

LIVE_NAME = "current-run.jsonl"


@dataclass(frozen=True)
class Live:
    started: datetime
    rows: list[dict]


class LiveRun:
    def __init__(self, path: Path, clock: Callable[[], datetime] = datetime.now):
        self._path, self._clock = path, clock
        self._began = False

    def begin(self) -> None:
        """Start the file afresh. Rows of an earlier run are gone with it."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"live": {"started": self._clock().isoformat()}}) + "\n", encoding="utf-8")
        self._began = True

    def add(self, row: dict) -> None:
        if not self._began:
            raise RuntimeError("begin() the live run before adding rows")
        with self._path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(row) + "\n")  # one write per row, so a reader sees whole lines or a short last one

    def finish(self) -> None:
        self._path.unlink(missing_ok=True)


def read(path: Path) -> Live | None:
    """The live run's start and rows so far, or None when there is no readable live file."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        started = datetime.fromisoformat(json.loads(lines[0])["live"]["started"])
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None
    rows = []
    for line in lines[1:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue  # the row the run is writing right now
        if isinstance(row, dict):
            rows.append(row)
    return Live(started, rows)
