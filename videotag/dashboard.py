"""Everything the GUI shows, read from the data home: the reports, the state file, the description cache and the log.

Nothing here talks to the NAS or writes a file, so the dashboard shows the same picture of a run whether it was
started from a terminal, by the scheduled task or from the GUI. Every function returns plain JSON-ready data.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from .config import LOG_FILE, REPORTS_DIR
from .descache import DESCRIBE_DIR
from .describe import Description
from .lock import LOCK_FILE, STALE_AFTER_SECONDS, taken_at
from .runlog import RunLog, last_run, parse_lines, read_lines, read_tail
from .state import STATE_FILE, State, StateError

DECISIONS = ("auto", "review", "none")  # best first
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
RUN_MARKER_GRACE = timedelta(seconds=1)  # the log keeps milliseconds, the lock file a finer clock


@dataclass(frozen=True)
class Sources:
    """Where the data lives. Tests point this at a temporary folder."""
    reports_dir: Path = REPORTS_DIR
    state_file: Path = STATE_FILE
    describe_dir: Path = DESCRIBE_DIR
    log_file: Path = LOG_FILE
    lock_file: Path = LOCK_FILE


# -- reports -----------------------------------------------------------------------------------

def _report_files(src: Sources) -> list[Path]:
    return sorted(src.reports_dir.glob("run-*.json"), reverse=True)


def _read_report(path: Path) -> list[dict] | None:
    """The rows of a report, or None while it is unreadable (for example half written)."""
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rows if isinstance(rows, list) else None


def _report_time(path: Path) -> str:
    try:
        return datetime.strptime(path.stem, "run-%Y%m%d-%H%M%S").strftime(TIME_FORMAT)
    except ValueError:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime(TIME_FORMAT)


def _latest_report(src: Sources) -> tuple[Path, list[dict]] | None:
    for path in _report_files(src):
        rows = _read_report(path)
        if rows is not None:
            return path, rows
    return None


def _video_decision(row: dict) -> str:
    if "error" in row:
        return "error"
    found = {match["decision"] for match in row.get("matches", [])}
    return next((decision for decision in DECISIONS if decision in found), "none")


def _counts(rows: list[dict]) -> dict:
    decisions = [_video_decision(row) for row in rows]
    return {"videos": len(rows), "auto": decisions.count("auto"), "review": decisions.count("review"),
            "none": decisions.count("none"), "errors": decisions.count("error")}


@lru_cache(maxsize=64)
def _summary(path: str, mtime_ns: int) -> dict | None:
    """Counts of one report; cached so the history does not re-read every report on each refresh."""
    rows = _read_report(Path(path))
    return None if rows is None else _counts(rows)


def history(src: Sources, limit: int = 30) -> list[dict]:
    entries = []
    for path in _report_files(src):
        counts = _summary(str(path), path.stat().st_mtime_ns)
        if counts is not None:
            entries.append({"name": path.name, "time": _report_time(path), **counts})
        if len(entries) == limit:
            break
    return entries


# -- the library: what was decided, what was written -------------------------------------------

def _load_state(src: Sources) -> tuple[State, str | None]:
    try:
        return State.load(src.state_file), None
    except StateError as exc:
        return State(src.state_file), str(exc)


def _description(src: Sources, video_id: int) -> dict | None:
    try:
        entry = json.loads((src.describe_dir / f"{video_id}.json").read_text(encoding="utf-8"))["description"]
        found = Description.from_dict(entry) if entry else None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if found is None:
        return None
    return {"en": found.summary_en, "de": found.summary_de,
            "topics": {"en": list(found.topics_en), "de": list(found.topics_de)},
            "objects": {"en": list(found.objects_en), "de": list(found.objects_de)},
            "place": found.place, "evidence": found.place_evidence}


def _person_status(video_id: int, person_id: int, decision: str, state: State, levels: dict) -> str:
    key = (video_id, person_id)
    if key in state.confirmed:
        return "confirmed"
    if key in state.rejected:
        return "rejected"
    if key in levels:
        return "tagged" if levels[key] == "auto" else "suggested"
    return "planned" if decision != "none" else "none"


def _description_status(video_id: int, state: State, description: dict | None) -> str:
    if video_id in state.descriptions:
        return "written"
    if video_id in state.description_rejected:
        return "rejected"
    return "pending" if description else "none"


def _video(src: Sources, row: dict, state: State, levels: dict) -> dict:
    video_id = row["video_id"]
    people = []
    for match in row.get("matches", []):
        status = _person_status(video_id, match["person_id"], match["decision"], state, levels)
        if status != "none" or match["decision"] != "none":
            people.append({"id": match["person_id"], "name": match["name"], "best": match["best"],
                           "hits": match["hit_frames"], "decision": match["decision"], "status": status})
    description = _description(src, video_id)
    return {"id": video_id, "filename": row["filename"], "frames": row.get("frames"), "faces": row.get("faces"),
            "error": row.get("error"), "decision": _video_decision(row), "people": people, "description": description,
            "description_status": _description_status(video_id, state, description)}


def _people(rows: list[dict], videos: list[dict]) -> list[dict]:
    names = {match["person_id"]: match["name"] for row in rows for match in row.get("matches", [])}
    summary = {person_id: {"id": person_id, "name": name, "tagged": 0, "suggested": 0, "planned": 0, "confirmed": 0,
                           "rejected": 0} for person_id, name in names.items()}
    for video in videos:
        for person in video["people"]:
            summary[person["id"]][person["status"]] += 1
    return list(summary.values())


def _state_counts(state: State) -> dict:
    levels = [tag.level for tag in state.written]
    return {"tags_auto": levels.count("auto"), "tags_review": levels.count("review"), "confirmed": len(state.confirmed),
            "rejected": len(state.rejected), "albums": len(state.albums), "face_links": len(state.faces),
            "descriptions": len(state.descriptions), "descriptions_rejected": len(state.description_rejected)}


def library_view(src: Sources) -> dict:
    """The newest report joined with the state and the description cache: one entry per video."""
    state, problem = _load_state(src)
    latest = _latest_report(src)
    path, rows = latest if latest else (None, [])
    levels = {(tag.item_id, tag.person_id): tag.level for tag in state.written}
    videos = [_video(src, row, state, levels) for row in rows]
    return {"report": {"name": path.name, "time": _report_time(path)} if path else None, "problem": problem,
            "totals": {**_counts(rows), "described": sum(video["description"] is not None for video in videos)},
            "state": _state_counts(state), "people": _people(rows, videos), "videos": videos}


def data_version(src: Sources) -> str:
    """Changes whenever a new report or a new state is saved, so the page knows when to reload the library."""
    latest = next(iter(_report_files(src)), None)
    try:
        stamp = src.state_file.stat().st_mtime_ns
    except FileNotFoundError:
        stamp = 0
    return f"{latest.name if latest else '-'}|{stamp}"


# -- the run that is going on, or the last one --------------------------------------------------

def _lock(src: Sources, now: datetime) -> tuple[dict, datetime | None]:
    """The lock as the page shows it (its age counts from the run's last refresh), and when its run took it."""
    try:
        modified = src.lock_file.stat().st_mtime
    except FileNotFoundError:
        return {"present": False, "age_seconds": None, "stale": False}, None
    age = now.timestamp() - modified
    return ({"present": True, "age_seconds": round(age), "stale": age >= STALE_AFTER_SECONDS},
            datetime.fromtimestamp(taken_at(src.lock_file) or modified))


def _run(run: RunLog, now: datetime, live: bool) -> dict:
    elapsed = (run.ended or now) - run.started if run.finished or live else None
    return {"mode": run.mode, "started": run.started.strftime(TIME_FORMAT),
            "ended": run.ended.strftime(TIME_FORMAT) if run.ended else None, "phase": run.phase, "total": run.total,
            "done": run.done, "current": run.current, "errors": run.errors, "planned_tags": run.planned_tags,
            "written": run.written, "finished": run.finished,
            "elapsed_seconds": round(elapsed.total_seconds()) if elapsed else None,
            "remaining_seconds": round(run.remaining_seconds) if run.remaining_seconds is not None else None}


def run_status(src: Sources, now: datetime) -> dict:
    """`running` while a fresh lock is held, else `idle` (last run finished), `interrupted` or `never`."""
    lock, taken = _lock(src, now)
    running = lock["present"] and not lock["stale"]
    run = last_run(read_lines(src.log_file), now)
    if running:
        state = "running"
        if run is not None and run.started < taken - RUN_MARKER_GRACE:
            run = None  # the run in the log began before this lock was taken: it is the previous one
    else:
        state = "never" if run is None else "idle" if run.finished else "interrupted"
    return {"state": state, "lock": lock, "run": _run(run, now, running) if run else None}


def log_tail(src: Sources, count: int = 80) -> list[dict]:
    lines = parse_lines(read_tail(src.log_file))[-count:]
    return [{"time": line.time.strftime(TIME_FORMAT), "level": line.level, "text": line.text} for line in lines]
