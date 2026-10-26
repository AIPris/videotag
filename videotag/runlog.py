"""What the run log says about the latest run: where it is, how far it got, what it wrote.

The log is the one interface between a run and anyone watching it, so a headless run, a scheduled one and one
started from the GUI look the same. The line formats are the ones `pipeline.py` and `cli.py` write; a test runs
a real pipeline pass and feeds its log back through this module, so the two cannot drift apart unnoticed.
"""
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import LOG_BACKUPS

TAIL_BYTES = 1_500_000  # the log rotates at 1 MB, so this reads all of one log file

_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3}) (\w+) (.*)$")
_STARTED = re.compile(r"^run started: (dry run|apply)$")
_LIBRARY = re.compile(r"^library: \d+ photos, (\d+) videos$")
_VIDEO = re.compile(r"^(\d+ .*?)( frames=\d+ faces=\d+ \|.*|: .*)$")
_PLANNED = re.compile(r"^(\d+) tag change\(s\) planned")
_WRITTEN = {"tags": re.compile(r"^(\d+) tag change\(s\) written$"), "faces": re.compile(r"^(\d+) face change\(s\) written$"),
            "descriptions": re.compile(r"^(\d+) description\(s\) written$"),
            "albums": re.compile(r"^(\d+) album item change\(s\) written$")}


def started_message(apply: bool) -> str:
    """The first line of every run; `last_run` looks for exactly this."""
    return f"run started: {'apply' if apply else 'dry run'}"


@dataclass(frozen=True)
class LogLine:
    time: datetime
    level: str
    text: str


@dataclass(frozen=True)
class RunLog:
    mode: str                          # "dry run" or "apply"
    started: datetime
    ended: datetime | None             # when the report was saved; None while the run is unfinished
    phase: str
    total: int | None                  # videos in the library, once it has been read
    done: int
    current: str | None                # "<video id> <file name>" of the video handled last
    errors: int
    planned_tags: int | None
    written: dict[str, int] | None     # what an apply run wrote, by kind
    finished: bool
    remaining_seconds: float | None


def parse_lines(text: str) -> list[LogLine]:
    """Log lines with their time and level. Continuation lines (tracebacks) are skipped."""
    lines = []
    for raw in text.splitlines():
        match = _LINE.match(raw)
        if match:
            when = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S").replace(microsecond=int(match[2]) * 1000)
            lines.append(LogLine(when, match[3], match[4]))
    return lines


def read_tail(path: Path, limit: int = TAIL_BYTES) -> str:
    """The end of a log file as text, starting on a whole line. A missing file reads as empty."""
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - limit))
            data = handle.read()
            cut = size > limit
    except FileNotFoundError:
        return ""
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")  # the handler writes CRLF on Windows
    return text.split("\n", 1)[1] if cut and "\n" in text else text


def read_lines(path: Path, backups: int = LOG_BACKUPS) -> list[LogLine]:
    """The log's lines, oldest first. A long run outgrows one file, so its marker may sit in a rotated one:
    the backups are read, newest first, only until a run marker turns up."""
    lines = parse_lines(read_tail(path))
    for number in range(1, backups + 1):
        if any(_STARTED.match(line.text) for line in lines):
            break
        lines = parse_lines(read_tail(path.with_name(f"{path.name}.{number}"))) + lines
    return lines


def _phase(mode: str, total: int | None, planned: bool, finished: bool) -> str:
    if finished:
        return "Finished"
    if planned:
        return "Writing to Photos" if mode == "apply" else "Planning changes"
    return "Reading the library and learning the faces" if total is None else "Analysing videos"


def last_run(lines: list[LogLine], now: datetime) -> RunLog | None:
    """The latest run found in the log, or None when the log holds no run marker."""
    starts = [index for index, line in enumerate(lines) if _STARTED.match(line.text)]
    if not starts:
        return None
    run = lines[starts[-1]:]
    mode = _STARTED.match(run[0].text)[1]
    total, library_time, last_video_time, current = None, None, None, None
    done = errors = 0
    planned_tags, written, ended = None, {}, None
    for line in run[1:]:
        library = _LIBRARY.match(line.text)
        video = _VIDEO.match(line.text)
        if library:
            total, library_time = int(library[1]), line.time
        elif video and line.level in ("INFO", "ERROR"):
            done, current, last_video_time = done + 1, video[1], line.time
        elif _PLANNED.match(line.text):
            planned_tags = int(_PLANNED.match(line.text)[1])
        elif line.text.startswith("report: "):
            ended = line.time
        for kind, pattern in _WRITTEN.items():
            if pattern.match(line.text):
                written[kind] = int(pattern.match(line.text)[1])
        errors += line.level == "ERROR"
    remaining = None
    if ended is None and total is not None and done:
        remaining = (last_video_time - library_time).total_seconds() / done * max(total - done, 0)
    return RunLog(mode, run[0].time, ended, _phase(mode, total, planned_tags is not None, ended is not None), total, done,
                  current, errors, planned_tags, written or None, ended is not None, remaining)
