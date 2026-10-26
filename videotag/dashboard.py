"""Everything the GUI shows, read from the data home: the reports, the state file, the description cache and the log.

Nothing here talks to the NAS or writes a file, so the dashboard shows the same picture of a run whether it was
started from a terminal, by the scheduled task or from the GUI. Every function returns plain JSON-ready data.
"""
import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from . import facecache
from .accuracy import ACCURACY_NAME
from .config import EVIDENCE_DIR, LOG_FILE, REPORTS_DIR
from .descache import DESCRIBE_DIR
from .describe import Description
from .estimates import preset_estimates, reanalysis
from .evidence import crops
from .live import LIVE_NAME, Live
from .live import read as read_live
from .lock import LOCK_FILE, STALE_AFTER_SECONDS, taken_at
from .runlog import RunLog, last_run, parse_lines, read_lines, read_tail
from .sampling import PRESETS
from .settings import Settings
from .spotcheck import sample
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
    live_file: Path = REPORTS_DIR / LIVE_NAME
    faces_dir: Path = facecache.FACES_DIR
    evidence_dir: Path = EVIDENCE_DIR
    accuracy_file: Path = REPORTS_DIR / ACCURACY_NAME


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
    evidence_id = row.get("twin_of") or video_id  # a copy of a file shows the crops of the video that was analysed
    shown = crops(src.evidence_dir, evidence_id) if row.get("matches") else {}
    people = []
    for match in row.get("matches", []):
        status = _person_status(video_id, match["person_id"], match["decision"], state, levels)
        if status != "none" or match["decision"] != "none":
            people.append({"id": match["person_id"], "name": match["name"], "best": match["best"],
                           "hits": match["hit_frames"], "decision": match["decision"], "status": status,
                           "crops": shown.get(match["person_id"], [])})
    description = _description(src, video_id)
    return {"id": video_id, "filename": row["filename"], "frames": row.get("frames"), "faces": row.get("faces"),
            "twin_of": row.get("twin_of"), "folder_id": row.get("folder_id"), "evidence_id": evidence_id,
            "error": row.get("error"), "decision": _video_decision(row), "people": people, "description": description,
            "description_status": _description_status(video_id, state, description)}


def _twins(rows: list[dict]) -> list[dict]:
    """The files that sit in the library under more than one id: the file name and every id, the first one first."""
    later: dict[int, list[int]] = {}
    for row in rows:
        if row.get("twin_of") is not None:
            later.setdefault(row["twin_of"], []).append(row["video_id"])
    names = {row["video_id"]: row["filename"] for row in rows}
    return [{"filename": names.get(first, ""), "ids": [first, *ids]} for first, ids in later.items()]


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
            "rejected": len(state.rejected), "albums": int(state.review_album is not None), "face_links": len(state.faces),
            "descriptions": len(state.descriptions), "descriptions_rejected": len(state.description_rejected)}


def _live_run(src: Sources, now: datetime) -> Live | None:
    """The rows the running run has made so far. Nothing when no run holds the lock, when the file is from a run that
    began earlier (one that died), or when the run has not made a row yet."""
    lock, taken = _lock(src, now)
    if not lock["present"] or lock["stale"]:
        return None
    found = read_live(src.live_file)
    return found if found and found.rows and found.started >= taken - RUN_MARKER_GRACE else None


def library_view(src: Sources, now: datetime | None = None) -> dict:
    """The results joined with the state and the description cache: one entry per video. The results are the rows of the
    run in progress while there are some, else the newest report."""
    state, problem = _load_state(src)
    live = _live_run(src, now or datetime.now())
    latest = None if live else _latest_report(src)
    path, rows = latest if latest else (None, live.rows if live else [])
    report = ({"name": "current run", "time": live.started.strftime(TIME_FORMAT), "live": True} if live
              else {"name": path.name, "time": _report_time(path)} if path else None)
    levels = {(tag.item_id, tag.person_id): tag.level for tag in state.written}
    videos = [_video(src, row, state, levels) for row in rows]
    return {"report": report, "problem": problem,
            "totals": {**_counts(rows), "described": sum(video["description"] is not None for video in videos)},
            "state": _state_counts(state), "people": _people(rows, videos), "twins": _twins(rows), "videos": videos}


def _stamp(path: Path) -> str:
    """Changes whenever the file does, also when a run appends to it. A missing file has its own stamp."""
    try:
        status = path.stat()
    except FileNotFoundError:
        return "0"
    return f"{status.st_mtime_ns}.{status.st_size}"


def data_version(src: Sources) -> str:
    """Changes whenever a new report or a new state is saved, so the page knows when to reload the library."""
    latest = next(iter(_report_files(src)), None)
    return f"{latest.name if latest else '-'}|{_stamp(src.state_file)}|{_stamp(src.live_file)}|{_stamp(src.accuracy_file)}"


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


def _references(run: RunLog) -> dict | None:
    if run.references is None:
        return None
    seconds = run.references_remaining_seconds
    return {"done": run.references[0], "total": run.references[1],
            "remaining_seconds": None if seconds is None else round(seconds)}


def _run(run: RunLog, now: datetime, live: bool) -> dict:
    elapsed = (run.ended or now) - run.started if run.finished or live else None
    return {"mode": run.mode, "started": run.started.strftime(TIME_FORMAT),
            "ended": run.ended.strftime(TIME_FORMAT) if run.ended else None, "phase": run.phase, "total": run.total,
            "done": run.done, "current": run.current, "errors": run.errors, "planned_tags": run.planned_tags,
            "written": run.written, "finished": run.finished, "slice_of": run.slice_of,
            "references": _references(run),
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


def _pace_seconds(src: Sources, now: datetime) -> int | None:
    """Seconds per video of the last finished run, which is a rough upper bound for redoing a video's frames and faces."""
    run = run_status(src, now)["run"]
    if run and run["finished"] and run["done"] and run["elapsed_seconds"]:
        return round(run["elapsed_seconds"] / run["done"])
    return None


def settings_view(src: Sources, saved: Settings, now: datetime) -> dict:
    """The saved settings with what the user needs to choose a preset: what each does and what it would save on this
    library, worked out from the results already saved, and what re-analysing under the chosen preset would touch."""
    summaries = facecache.summaries(src.faces_dir)
    return {"preset": saved.preset, "describe": saved.describe,
            "presets": [{"name": name, "fps": rule.fps, "head_seconds": rule.head_seconds, "tail_fps": rule.tail_fps}
                        for name, rule in PRESETS.items()],
            "estimate": preset_estimates(summaries),
            "reanalyse": {**reanalysis(summaries, PRESETS[saved.preset]), "pace_seconds": _pace_seconds(src, now),
                          "touched": {name: reanalysis(summaries, rule)["videos"] for name, rule in PRESETS.items()}}}


def accuracy_view(src: Sources) -> dict | None:
    """The accuracy report the latest run wrote, or None when there is none or it cannot be read."""
    try:
        report = json.loads(src.accuracy_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return report if isinstance(report, dict) else None


def spot_check(src: Sources, count: int, rng: random.Random | None = None) -> list[dict]:
    """A random sample of each person's matches in the newest report, to look at in Photos."""
    state, _ = _load_state(src)
    latest = _latest_report(src)
    return sample(latest[1] if latest else [], state, count, rng or random.Random())


def log_tail(src: Sources, count: int = 80) -> list[dict]:
    lines = parse_lines(read_tail(src.log_file))[-count:]
    return [{"time": line.time.strftime(TIME_FORMAT), "level": line.level, "text": line.text} for line in lines]
