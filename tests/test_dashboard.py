import json
import os
from datetime import datetime

from videotag import facecache
from videotag.dashboard import (
    Sources,
    accuracy_view,
    data_version,
    history,
    library_view,
    log_tail,
    run_status,
    settings_view,
    spot_check,
)
from videotag.describe import Description
from videotag.live import LiveRun
from videotag.lock import STALE_AFTER_SECONDS
from videotag.sampling import PRESETS
from videotag.settings import Settings
from videotag.state import State, WrittenTag

NOW = datetime(2026, 10, 25, 15, 19, 40)
CAT = Description("A cat on a sofa.", "Eine Katze auf einem Sofa.", ("pets",), ("Haustiere",), ("cat", "sofa"), ("Katze", "Sofa"),
                  "Vienna", "a street sign")


def sources(tmp_path) -> Sources:
    return Sources(reports_dir=tmp_path / "reports", state_file=tmp_path / "state" / "state.json",
                   describe_dir=tmp_path / "state" / "describe", log_file=tmp_path / "logs" / "videotag.log",
                   lock_file=tmp_path / "state" / "run.lock", live_file=tmp_path / "reports" / "current-run.jsonl",
                   faces_dir=tmp_path / "state" / "faces", evidence_dir=tmp_path / "state" / "evidence",
                   accuracy_file=tmp_path / "reports" / "accuracy.json")


def match(person_id, name, decision, best=0.5, hits=3):
    return {"person_id": person_id, "name": name, "best": best, "hit_frames": hits, "decision": decision}


def row(video_id, matches=(), **extra):
    return {"video_id": video_id, "filename": f"clip{video_id}.mp4", "frames": 10, "faces": 8, "description": None,
            "place": None, "matches": list(matches), **extra}


def save_report(src, name, rows):
    src.reports_dir.mkdir(parents=True, exist_ok=True)
    (src.reports_dir / name).write_text(json.dumps(rows), encoding="utf-8")


def save_state(src, **fields):
    State(src.state_file, **fields).save()


def tag(video_id, person_id, name, level):
    return WrittenTag(video_id, person_id, 40 + person_id, name, level, "2026-10-25T10:00:00")


def log(src, *lines):
    src.log_file.parent.mkdir(parents=True, exist_ok=True)
    src.log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def lock(src, age_seconds, taken_ago=None):
    """A lock last refreshed `age_seconds` ago; with `taken_ago` it also records when the run took it, as a live run does."""
    src.lock_file.parent.mkdir(parents=True, exist_ok=True)
    src.lock_file.write_text("" if taken_ago is None else str(NOW.timestamp() - taken_ago), encoding="utf-8")
    stamp = NOW.timestamp() - age_seconds
    os.utime(src.lock_file, (stamp, stamp))


START = "2026-10-25 15:19:19,000 INFO run started: dry run"
LIBRARY = "2026-10-25 15:19:20,000 INFO library: 9 photos, 4 videos"
FIRST = "2026-10-25 15:19:25,000 INFO 1 clip1.mp4 frames=10 faces=8 | Alice=0.5/3:auto"
REPORT = "2026-10-25 15:19:37,000 INFO report: x.json"


# -- library_view ------------------------------------------------------------------------------

def test_an_empty_data_home_gives_an_empty_library(tmp_path):
    view = library_view(sources(tmp_path))
    assert view["report"] is None and view["videos"] == [] and view["problem"] is None
    assert view["totals"] == {"videos": 0, "auto": 0, "review": 0, "none": 0, "errors": 0, "described": 0}


def test_every_person_status_comes_from_the_report_and_the_state(tmp_path):
    src = sources(tmp_path)
    save_report(src, "run-20261025-101500.json", [
        row(1, [match(7, "Alice", "auto")]),
        row(2, [match(7, "Alice", "review", hits=1)]),
        row(3, [match(7, "Alice", "auto")]),
        row(4, [match(7, "Alice", "none", best=None, hits=0)]),
        row(5, [match(7, "Alice", "none", best=None, hits=0)]),
        row(6, [match(7, "Alice", "none", best=None, hits=0)])])
    save_state(src, written=[tag(1, 7, "Alice", "auto"), tag(2, 7, "Alice?", "review")], confirmed={(4, 7)}, rejected={(5, 7)})
    people = {video["id"]: video["people"] for video in library_view(src)["videos"]}
    assert [person["status"] for person in people[1]] == ["tagged"]
    assert [person["status"] for person in people[2]] == ["suggested"]
    assert [person["status"] for person in people[3]] == ["planned"]
    assert [person["status"] for person in people[4]] == ["confirmed"]
    assert [person["status"] for person in people[5]] == ["rejected"]
    assert people[6] == []


def test_a_person_entry_carries_the_scores_the_decision_was_based_on(tmp_path):
    src = sources(tmp_path)
    save_report(src, "run-20261025-101500.json", [row(1, [match(7, "Alice", "auto", best=0.612, hits=12)])])
    person = library_view(src)["videos"][0]["people"][0]
    assert person == {"id": 7, "name": "Alice", "best": 0.612, "hits": 12, "decision": "auto", "status": "planned", "crops": []}


def test_the_video_decision_is_the_best_of_its_people_or_error(tmp_path):
    src = sources(tmp_path)
    save_report(src, "run-20261025-101500.json", [
        row(1, [match(7, "Alice", "review"), match(8, "Bob", "auto")]),
        row(2, [match(7, "Alice", "review")]),
        row(3, [match(7, "Alice", "none")]),
        {"video_id": 4, "filename": "clip4.mp4", "error": "download failed"}])
    view = library_view(src)
    assert [video["decision"] for video in view["videos"]] == ["auto", "review", "none", "error"]
    assert view["videos"][3]["error"] == "download failed" and view["videos"][3]["people"] == []
    assert view["totals"] == {"videos": 4, "auto": 1, "review": 1, "none": 1, "errors": 1, "described": 0}


def test_the_full_description_comes_from_the_cache_and_its_status_from_the_state(tmp_path):
    src = sources(tmp_path)
    save_report(src, "run-20261025-101500.json", [row(1, description="A cat on a sofa."), row(2, description="A cat on a sofa."),
                                                  row(3, description="A cat on a sofa."), row(4)])
    src.describe_dir.mkdir(parents=True)
    cached = json.dumps({"key": "k", "description": CAT.to_dict()})
    for video_id in (1, 2, 3):
        (src.describe_dir / f"{video_id}.json").write_text(cached, encoding="utf-8")
    (src.describe_dir / "4.json").write_text(json.dumps({"key": "k", "description": None}), encoding="utf-8")
    save_state(src, descriptions={1: "[vt]...[/vt]"}, description_rejected={2})
    videos = library_view(src)["videos"]
    assert videos[0]["description"] == {"en": "A cat on a sofa.", "de": "Eine Katze auf einem Sofa.",
                                        "topics": {"en": ["pets"], "de": ["Haustiere"]},
                                        "objects": {"en": ["cat", "sofa"], "de": ["Katze", "Sofa"]},
                                        "place": "Vienna", "evidence": "a street sign"}
    assert [video["description_status"] for video in videos] == ["written", "rejected", "pending", "none"]
    assert videos[3]["description"] is None
    assert library_view(src)["totals"]["described"] == 3


def test_a_missing_description_cache_means_no_description(tmp_path):
    src = sources(tmp_path)
    save_report(src, "run-20261025-101500.json", [row(1)])
    video = library_view(src)["videos"][0]
    assert video["description"] is None and video["description_status"] == "none"


def test_people_are_summarised_across_videos(tmp_path):
    src = sources(tmp_path)
    save_report(src, "run-20261025-101500.json", [
        row(1, [match(7, "Alice", "auto"), match(8, "Bob", "none")]),
        row(2, [match(7, "Alice", "review"), match(8, "Bob", "none")]),
        row(3, [match(7, "Alice", "none"), match(8, "Bob", "none")])])
    save_state(src, written=[tag(1, 7, "Alice", "auto")], rejected={(3, 8)})
    assert library_view(src)["people"] == [
        {"id": 7, "name": "Alice", "tagged": 1, "suggested": 0, "planned": 1, "confirmed": 0, "rejected": 0},
        {"id": 8, "name": "Bob", "tagged": 0, "suggested": 0, "planned": 0, "confirmed": 0, "rejected": 1}]


def test_the_state_is_counted(tmp_path):
    src = sources(tmp_path)
    save_state(src, written=[tag(1, 7, "Alice", "auto"), tag(2, 7, "Alice?", "review"), tag(3, 7, "Alice", "auto")],
               confirmed={(4, 7)}, rejected={(5, 7), (6, 7)}, review_album=900, faces={(1, 7): 301},
               descriptions={1: "b"}, description_rejected={2})
    assert library_view(src)["state"] == {"tags_auto": 2, "tags_review": 1, "confirmed": 1, "rejected": 2, "albums": 1,
                                          "face_links": 1, "descriptions": 1, "descriptions_rejected": 1}


def test_no_album_on_record_is_counted_as_none(tmp_path):
    src = sources(tmp_path)
    save_state(src)
    assert library_view(src)["state"]["albums"] == 0


def test_an_unreadable_state_is_reported_instead_of_raised(tmp_path):
    src = sources(tmp_path)
    save_report(src, "run-20261025-101500.json", [row(1, [match(7, "Alice", "auto")])])
    src.state_file.parent.mkdir(parents=True)
    src.state_file.write_text("{ half written", encoding="utf-8")
    view = library_view(src)
    assert "unreadable" in view["problem"] and view["videos"][0]["people"][0]["status"] == "planned"


def test_a_report_that_is_still_being_written_falls_back_to_the_previous_one(tmp_path):
    src = sources(tmp_path)
    save_report(src, "run-20261025-101500.json", [row(1)])
    (src.reports_dir / "run-20261025-111500.json").write_text('[{"video_id": 1, "file', encoding="utf-8")
    assert library_view(src)["report"] == {"name": "run-20261025-101500.json", "time": "2026-10-25T10:15:00"}


def test_the_report_time_comes_from_its_file_name(tmp_path):
    src = sources(tmp_path)
    save_report(src, "run-20261025-101500.json", [])
    save_report(src, "run-20261025-151919.json", [])
    assert library_view(src)["report"]["time"] == "2026-10-25T15:19:19"


# -- the live view of the run that is going on ----------------------------------------------------------

LIVE_START = datetime(2026, 10, 25, 15, 19, 19)


def live_rows(src, *rows, started=LIVE_START):
    live = LiveRun(src.live_file, clock=lambda: started)
    live.begin()
    for one in rows:
        live.add(one)
    return live


def test_while_a_run_is_going_the_library_shows_the_rows_made_so_far_instead_of_the_old_report(tmp_path):
    src = sources(tmp_path)
    save_report(src, "run-20261024-101500.json", [row(50, [match(7, "Alice", "auto")])])
    live_rows(src, row(1, [match(7, "Alice", "auto")]), row(2, [match(7, "Alice", "review")]),
              {"video_id": 3, "filename": "clip3.mp4", "error": "download failed"})
    lock(src, 25)
    view = library_view(src, NOW)
    assert view["report"] == {"name": "current run", "time": "2026-10-25T15:19:19", "live": True}
    assert [video["id"] for video in view["videos"]] == [1, 2, 3]
    assert view["totals"] == {"videos": 3, "auto": 1, "review": 1, "none": 0, "errors": 1, "described": 0}
    assert [person["name"] for person in view["people"]] == ["Alice"]


def test_a_finished_or_idle_dashboard_shows_the_report_even_if_a_live_file_is_still_there(tmp_path):
    src = sources(tmp_path)
    save_report(src, "run-20261024-101500.json", [row(50)])
    live_rows(src, row(1))
    assert [video["id"] for video in library_view(src, NOW)["videos"]] == [50]  # no lock: nothing is running
    assert "live" not in library_view(src, NOW)["report"]


def test_the_live_file_of_a_run_that_died_is_not_shown_for_the_next_run(tmp_path):
    src = sources(tmp_path)
    save_report(src, "run-20261024-101500.json", [row(50)])
    live_rows(src, row(1), started=datetime(2026, 10, 25, 9, 0, 0))   # an earlier run that never finished
    lock(src, 5, taken_ago=5)                                          # the new run took the lock at 15:19:35
    assert [video["id"] for video in library_view(src, NOW)["videos"]] == [50]


def test_a_run_that_has_made_no_row_yet_leaves_the_last_report_on_screen(tmp_path):
    src = sources(tmp_path)
    save_report(src, "run-20261024-101500.json", [row(50)])
    live_rows(src)
    lock(src, 25)
    assert [video["id"] for video in library_view(src, NOW)["videos"]] == [50]


def test_a_row_the_run_is_writing_right_now_does_not_break_the_view(tmp_path):
    src = sources(tmp_path)
    live_rows(src, row(1))
    with src.live_file.open("a", encoding="utf-8") as out:
        out.write('{"video_id": 2, "filen')
    lock(src, 25)
    assert [video["id"] for video in library_view(src, NOW)["videos"]] == [1]


def test_the_live_file_is_not_a_report_and_not_part_of_the_history(tmp_path):
    src = sources(tmp_path)
    save_report(src, "run-20261024-101500.json", [row(50)])
    live_rows(src, row(1))
    assert [entry["name"] for entry in history(src)] == ["run-20261024-101500.json"]
    assert library_view(src)["report"]["name"] == "run-20261024-101500.json"


def test_the_data_version_changes_when_a_run_writes_its_accuracy_report(tmp_path):
    src = sources(tmp_path)
    before = data_version(src)
    src.accuracy_file.parent.mkdir(parents=True, exist_ok=True)
    src.accuracy_file.write_text("{}", encoding="utf-8")
    assert data_version(src) != before


def test_the_data_version_changes_with_every_row_the_run_makes(tmp_path):
    src = sources(tmp_path)
    live = live_rows(src, row(1))
    before = data_version(src)
    live.add(row(2))
    after = data_version(src)
    live.finish()
    assert len({before, after, data_version(src)}) == 3


# -- twins ----------------------------------------------------------------------------------------------

def test_twins_are_listed_with_the_file_name_and_every_id_the_file_has(tmp_path):
    src = sources(tmp_path)
    save_report(src, "run-20261025-101500.json", [
        row(1, [match(7, "Alice", "auto")], filename="beach.mp4"), row(2),
        row(3, [match(7, "Alice", "auto")], twin_of=1, filename="beach.mp4"),
        row(4, twin_of=1, filename="beach.mp4"), row(5, twin_of=2, filename="clip2.mp4")])
    view = library_view(src)
    assert view["twins"] == [{"filename": "beach.mp4", "ids": [1, 3, 4]}, {"filename": "clip2.mp4", "ids": [2, 5]}]
    assert [video["twin_of"] for video in view["videos"]] == [None, None, 1, 1, 2]


def test_a_library_without_twins_lists_none(tmp_path):
    src = sources(tmp_path)
    save_report(src, "run-20261025-101500.json", [row(1), row(2)])
    assert library_view(src)["twins"] == []


# -- evidence, accuracy and the spot-check ---------------------------------------------------------------

def make_crops(src, video_id, *names):
    folder = src.evidence_dir / str(video_id)
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"jpeg")


def test_a_person_entry_names_the_crops_that_show_why_the_video_matched_them(tmp_path):
    src = sources(tmp_path)
    save_report(src, "run-20261025-101500.json", [row(1, [match(7, "Alice", "auto"), match(8, "Bob", "review")]),
                                                  row(2, [match(7, "Alice", "auto")])])
    make_crops(src, 1, "7-1.jpg", "7-2.jpg", "8-1.jpg")
    first, second = library_view(src)["videos"]
    assert [(p["name"], p["crops"]) for p in first["people"]] == [("Alice", ["7-1.jpg", "7-2.jpg"]), ("Bob", ["8-1.jpg"])]
    assert first["evidence_id"] == 1 and second["people"][0]["crops"] == []


def test_a_copy_of_a_file_shows_the_crops_of_the_video_that_was_analysed(tmp_path):
    src = sources(tmp_path)
    save_report(src, "run-20261025-101500.json", [row(1, [match(7, "Alice", "auto")]),
                                                  row(2, [match(7, "Alice", "auto")], twin_of=1)])
    make_crops(src, 1, "7-1.jpg")
    original, copy = library_view(src)["videos"]
    assert copy["evidence_id"] == 1 and copy["people"][0]["crops"] == ["7-1.jpg"] and original["evidence_id"] == 1


def test_the_accuracy_report_is_read_from_its_file_and_is_nothing_when_missing_or_unreadable(tmp_path):
    src = sources(tmp_path)
    assert accuracy_view(src) is None
    src.accuracy_file.parent.mkdir(parents=True)
    src.accuracy_file.write_text('{ half written', encoding="utf-8")
    assert accuracy_view(src) is None
    report = {"time": "2026-10-26T12:00:00", "people": [], "lookalikes": [], "mixed_up": []}
    src.accuracy_file.write_text(json.dumps(report), encoding="utf-8")
    assert accuracy_view(src)["time"] == "2026-10-26T12:00:00"


def test_the_accuracy_report_is_not_a_run_report(tmp_path):
    src = sources(tmp_path)
    src.accuracy_file.parent.mkdir(parents=True)
    src.accuracy_file.write_text("{}", encoding="utf-8")
    assert history(src) == [] and library_view(src)["report"] is None


def test_the_spot_check_samples_the_newest_report_and_leaves_out_what_the_user_settled(tmp_path):
    src = sources(tmp_path)
    save_report(src, "run-20261025-101500.json", [row(1, [match(7, "Alice", "auto")])])
    save_report(src, "run-20261025-151919.json", [row(n, [match(7, "Alice", "auto")]) for n in range(10, 16)])
    save_state(src, confirmed={(10, 7)}, rejected={(11, 7)})
    (alice,) = spot_check(src, 20)
    assert sorted(item["video_id"] for item in alice["confident"]["sample"]) == [12, 13, 14, 15]
    assert alice["confident"]["total"] == 4


def test_the_spot_check_of_an_empty_data_home_is_empty(tmp_path):
    assert spot_check(sources(tmp_path), 5) == []


# -- history -----------------------------------------------------------------------------------

def test_history_lists_reports_newest_first_with_their_counts(tmp_path):
    src = sources(tmp_path)
    save_report(src, "run-20261025-101500.json", [row(1, [match(7, "Alice", "auto")])])
    save_report(src, "run-20261025-151919.json", [row(1, [match(7, "Alice", "review")]), row(2, [match(7, "Alice", "none")]),
                                                  {"video_id": 3, "filename": "x.mp4", "error": "boom"}])
    (src.reports_dir / "run-20261025-120000.json").write_text("not json", encoding="utf-8")
    assert history(src) == [
        {"name": "run-20261025-151919.json", "time": "2026-10-25T15:19:19",
         "videos": 3, "auto": 0, "review": 1, "none": 1, "errors": 1},
        {"name": "run-20261025-101500.json", "time": "2026-10-25T10:15:00",
         "videos": 1, "auto": 1, "review": 0, "none": 0, "errors": 0}]


def test_history_is_limited(tmp_path):
    src = sources(tmp_path)
    for minute in range(5):
        save_report(src, f"run-20261025-10{minute:02d}00.json", [])
    assert [entry["name"] for entry in history(src, limit=2)] == ["run-20261025-100400.json", "run-20261025-100300.json"]


# -- run status --------------------------------------------------------------------------------

def test_with_no_log_and_no_lock_nothing_ever_ran(tmp_path):
    status = run_status(sources(tmp_path), NOW)
    assert status["state"] == "never" and status["run"] is None and status["lock"]["present"] is False


def test_a_fresh_lock_with_a_run_in_the_log_means_running(tmp_path):
    src = sources(tmp_path)
    log(src, START, LIBRARY, FIRST)
    lock(src, 25)  # the lock was taken at 15:19:15, four seconds before the run wrote its marker
    status = run_status(src, NOW)
    assert status["state"] == "running" and status["lock"] == {"present": True, "age_seconds": 25, "stale": False}
    assert status["run"]["phase"] == "Analysing videos" and (status["run"]["done"], status["run"]["total"]) == (1, 4)
    assert status["run"]["elapsed_seconds"] == 21 and status["run"]["started"] == "2026-10-25T15:19:19"


def test_a_fresh_lock_before_the_run_wrote_its_marker_means_starting(tmp_path):
    src = sources(tmp_path)
    log(src, START, LIBRARY, FIRST, REPORT)  # the previous run
    lock(src, 1)
    status = run_status(src, NOW)
    assert status["state"] == "running" and status["run"] is None


def test_a_run_that_saved_its_report_but_still_holds_the_lock_is_finishing_not_starting(tmp_path):
    src = sources(tmp_path)
    log(src, START, LIBRARY, FIRST, REPORT)
    lock(src, 25)
    status = run_status(src, NOW)
    assert status["state"] == "running" and status["run"]["finished"] is True


def test_a_finished_run_without_a_lock_is_idle(tmp_path):
    src = sources(tmp_path)
    log(src, START, LIBRARY, FIRST, REPORT)
    status = run_status(src, NOW)
    assert status["state"] == "idle" and status["run"]["finished"] is True
    assert status["run"]["ended"] == "2026-10-25T15:19:37" and status["run"]["elapsed_seconds"] == 18


def test_an_unfinished_run_without_a_lock_was_interrupted(tmp_path):
    src = sources(tmp_path)
    log(src, START, LIBRARY, FIRST)
    status = run_status(src, NOW)
    assert status["state"] == "interrupted" and status["run"]["elapsed_seconds"] is None


def test_a_stale_lock_does_not_count_as_running(tmp_path):
    src = sources(tmp_path)
    log(src, START, LIBRARY, FIRST)
    lock(src, STALE_AFTER_SECONDS + 60)
    status = run_status(src, NOW)
    assert status["state"] == "interrupted" and status["lock"]["stale"] is True


def test_a_run_that_has_gone_on_longer_than_the_stale_limit_still_shows_as_running(tmp_path):
    src = sources(tmp_path)
    log(src, START, LIBRARY, FIRST)
    lock(src, 5, taken_ago=STALE_AFTER_SECONDS + 600)  # refreshed 5 s ago by the run, taken more than 6 hours ago
    status = run_status(src, NOW)
    assert status["state"] == "running" and status["lock"]["stale"] is False
    assert status["run"]["started"] == "2026-10-25T15:19:19" and status["run"]["done"] == 1


def test_a_run_whose_marker_rotated_out_of_the_current_log_still_shows(tmp_path):
    src = sources(tmp_path)
    log(src, FIRST)
    src.log_file.with_name("videotag.log.1").write_text("\n".join([START, LIBRARY]) + "\n", encoding="utf-8")
    lock(src, 25)  # taken at 15:19:15, before the run wrote its marker
    status = run_status(src, NOW)
    assert status["state"] == "running" and (status["run"]["done"], status["run"]["total"]) == (1, 4)


# -- log tail and version -----------------------------------------------------------------------

def test_the_log_tail_returns_the_last_lines_with_their_level(tmp_path):
    src = sources(tmp_path)
    log(src, START, LIBRARY, "2026-10-25 15:19:21,000 ERROR something broke", FIRST)
    tail = log_tail(src, count=3)
    assert [(line["level"], line["text"]) for line in tail] == [("INFO", "library: 9 photos, 4 videos"),
                                                                ("ERROR", "something broke"),
                                                                ("INFO", "1 clip1.mp4 frames=10 faces=8 | Alice=0.5/3:auto")]
    assert tail[0]["time"] == "2026-10-25T15:19:20"


def test_the_data_version_changes_when_a_report_or_the_state_changes(tmp_path):
    src = sources(tmp_path)
    empty = data_version(src)
    save_report(src, "run-20261025-101500.json", [])
    with_report = data_version(src)
    save_state(src)
    with_state = data_version(src)
    os.utime(src.state_file, (1_000_000_000, 1_000_000_000))
    assert len({empty, with_report, with_state, data_version(src)}) == 4


# -- the phases before the videos, and the settings view -------------------------------------------------

def test_the_run_reports_the_reference_phase_and_how_big_the_library_slice_is(tmp_path):
    src = sources(tmp_path)
    log(src, START, "2026-10-25 15:19:20,000 INFO references: 0 of 100 photos",
        "2026-10-25 15:19:30,000 INFO references: 25 of 100 photos")
    lock(src, 25)
    run = run_status(src, NOW)["run"]
    assert run["phase"] == "Learning the faces of your people"
    assert run["references"] == {"done": 25, "total": 100, "remaining_seconds": 30}
    assert run["slice_of"] is None


def test_a_slice_shows_how_big_the_whole_library_is(tmp_path):
    src = sources(tmp_path)
    log(src, START, LIBRARY, "2026-10-25 15:19:20,100 INFO slice: 2 of 4 videos", FIRST)
    lock(src, 25)
    run = run_status(src, NOW)["run"]
    assert (run["total"], run["slice_of"], run["done"], run["references"]) == (2, 4, 1, None)


def save_result(src, video_id, frames, sampling):
    facecache.save(video_id, {"filesize": video_id, "time": 1}, [[]] * frames, sampling)


def test_the_settings_view_carries_the_choice_the_presets_and_what_they_would_save_on_this_library(tmp_path, monkeypatch):
    src = sources(tmp_path)
    monkeypatch.setattr(facecache, "FACES_DIR", src.faces_dir)
    save_result(src, 1, 32, PRESETS["thorough"])
    save_result(src, 2, 2460, PRESETS["thorough"])
    view = settings_view(src, Settings("balanced", False), NOW)
    assert (view["preset"], view["describe"]) == ("balanced", False)
    assert [(p["name"], p["fps"], p["head_seconds"], p["tail_fps"]) for p in view["presets"]] == [
        ("thorough", 2.0, None, None), ("balanced", 2.0, 30.0, 0.5), ("fast", 2.0, 30.0, 0.25)]
    assert view["estimate"]["saving"] == {"thorough": 0, "balanced": 72, "fast": 84}
    assert view["reanalyse"]["videos"] == 2 and view["reanalyse"]["of"] == 2  # both were cut under thorough, balanced is chosen
    assert view["reanalyse"]["touched"] == {"thorough": 0, "balanced": 2, "fast": 2}  # and what any other choice would touch


def test_the_time_a_re_analysis_might_take_comes_from_the_pace_of_the_last_finished_run(tmp_path, monkeypatch):
    src = sources(tmp_path)
    monkeypatch.setattr(facecache, "FACES_DIR", src.faces_dir)
    save_result(src, 1, 32, PRESETS["thorough"])
    save_result(src, 2, 32, PRESETS["thorough"])
    log(src, START, LIBRARY, FIRST, "2026-10-25 15:19:30,000 INFO 2 clip2.mp4 frames=10 faces=8 | Alice=0.5/3:auto", REPORT)
    reanalyse = settings_view(src, Settings("fast", True), NOW)["reanalyse"]
    assert reanalyse["videos"] == 2 and reanalyse["pace_seconds"] == 9  # 18 s for 2 videos
    assert settings_view(sources(tmp_path / "other"), Settings(), NOW)["reanalyse"]["pace_seconds"] is None


# -- the folder of a video ------------------------------------------------------------------------------

def test_a_video_carries_its_folder_when_the_report_has_one_and_none_for_an_older_report(tmp_path):
    src = sources(tmp_path)
    save_report(src, "run-20261025-101500.json", [row(1, folder_id=640), row(2)])
    first, second = library_view(src)["videos"]
    assert first["folder_id"] == 640 and second["folder_id"] is None
