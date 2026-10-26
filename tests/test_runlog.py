import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler

import pytest

from videotag.config import LOG_BACKUPS
from videotag.runlog import (
    UNDO_STARTED,
    last_run,
    parse_lines,
    read_lines,
    read_tail,
    references_message,
    removed_message,
    slice_message,
    started_message,
)

START = "2026-10-25 15:19:19,100 INFO run started: dry run"
LIBRARY = "2026-10-25 15:19:20,200 INFO library: 9 photos, 3 videos"
FIRST = "2026-10-25 15:19:25,000 INFO 101 clip one.mp4 frames=40 faces=33 | Alice=0.61/12:auto Bob=-/0:none"
BROKEN = "2026-10-25 15:19:30,000 ERROR 102 broken.mp4: download failed"
THIRD = "2026-10-25 15:19:35,000 INFO 103 clip-three.mp4 frames=10 faces=0 | Alice=-/0:none"
PLANNED = "2026-10-25 15:19:36,000 INFO 2 tag change(s) planned (1 confident, 1 suggestions, 0 removals)"
DRY = "2026-10-25 15:19:36,500 INFO dry run: nothing written, pass --apply to write"
REPORT = "2026-10-25 15:19:37,000 INFO report: C:\\data\\reports\\run-20261025-151937.json"
NOW = datetime(2026, 10, 25, 15, 19, 40)
EARLIER_RUN = ("2026-10-24 09:00:00,000 INFO run started: apply", "2026-10-24 09:00:01,000 INFO library: 1 photos, 99 videos")


def lines(*rows: str):
    return parse_lines("\n".join(rows))


def test_parse_lines_reads_time_level_and_text_and_skips_traceback_lines():
    parsed = lines(START, "Traceback (most recent call last):", '  File "x.py", line 1', BROKEN)
    assert [(row.level, row.text) for row in parsed] == [("INFO", "run started: dry run"),
                                                          ("ERROR", "102 broken.mp4: download failed")]
    assert parsed[0].time == datetime(2026, 10, 25, 15, 19, 19, 100000)


def test_no_run_marker_means_no_run():
    assert last_run(lines(LIBRARY, FIRST), NOW) is None


def test_a_finished_dry_run_is_summarised():
    run = last_run(lines(START, LIBRARY, FIRST, BROKEN, THIRD, PLANNED, DRY, REPORT), NOW)
    assert (run.mode, run.total, run.done, run.errors, run.planned_tags) == ("dry run", 3, 3, 1, 2)
    assert run.current == "103 clip-three.mp4"
    assert run.finished and run.phase == "Finished" and run.written is None
    assert run.started == datetime(2026, 10, 25, 15, 19, 19, 100000)
    assert run.ended == datetime(2026, 10, 25, 15, 19, 37)
    assert run.remaining_seconds is None


def test_a_run_in_the_middle_of_the_videos_estimates_what_is_left():
    run = last_run(lines(START, LIBRARY, FIRST), NOW)
    assert (run.done, run.total, run.phase, run.finished) == (1, 3, "Analysing videos", False)
    assert run.current == "101 clip one.mp4"
    assert run.ended is None
    assert run.remaining_seconds == pytest.approx(2 * 4.8)  # one video took 4.8 s from the library line to its own line


def test_before_the_library_is_read_the_phase_says_so():
    run = last_run(lines(START), NOW)
    assert run.phase == "Reading the library and learning the faces"
    assert run.total is None and run.done == 0 and run.remaining_seconds is None


def test_an_apply_run_reports_what_it_wrote():
    run = last_run(lines(
        "2026-10-25 15:20:00,000 INFO run started: apply", LIBRARY, PLANNED,
        "2026-10-25 15:20:05,000 INFO 2 tag change(s) written",
        "2026-10-25 15:20:06,000 INFO 1 face change(s) written",
        "2026-10-25 15:20:07,000 INFO 3 description(s) written",
        "2026-10-25 15:20:08,000 INFO 4 album item change(s) written",
        REPORT), NOW)
    assert run.mode == "apply"
    assert run.written == {"tags": 2, "faces": 1, "descriptions": 3, "albums": 4}


def test_an_apply_run_that_has_planned_but_not_finished_is_writing():
    run = last_run(lines("2026-10-25 15:20:00,000 INFO run started: apply", LIBRARY, PLANNED), NOW)
    assert run.phase == "Writing to Photos"


def test_a_dry_run_that_has_planned_but_not_finished_is_planning():
    assert last_run(lines(START, LIBRARY, PLANNED), NOW).phase == "Planning changes"


def test_only_the_latest_run_counts():
    older = ["2026-10-24 09:00:00,000 INFO run started: apply", "2026-10-24 09:00:01,000 INFO library: 5 photos, 9 videos",
             "2026-10-24 09:00:30,000 ERROR 7 old.mp4: boom", "2026-10-24 09:01:00,000 INFO report: C:\\old.json"]
    run = last_run(lines(*older, START, LIBRARY, FIRST), NOW)
    assert (run.mode, run.total, run.done, run.errors) == ("dry run", 3, 1, 0)


def test_errors_without_a_video_number_still_count_as_errors_but_not_as_progress():
    run = last_run(lines(START, LIBRARY, "2026-10-25 15:19:21,000 ERROR describing 101 failed: CUDA out of memory"), NOW)
    assert (run.errors, run.done) == (1, 0)


def test_read_tail_returns_whole_small_file_and_nothing_for_a_missing_one(tmp_path):
    log = tmp_path / "videotag.log"
    log.write_text("one\ntwo\n", encoding="utf-8")
    assert read_tail(log, 1000) == "one\ntwo\n"
    assert read_tail(tmp_path / "missing.log", 1000) == ""


def test_read_tail_drops_the_line_it_cut_in_half(tmp_path):
    log = tmp_path / "videotag.log"
    log.write_bytes(b"first line\nsecond line\nthird line\n")
    assert read_tail(log, 25) == "second line\nthird line\n"


@pytest.mark.parametrize(("apply", "mode"), [(True, "apply"), (False, "dry run")])
def test_the_marker_a_run_writes_is_the_marker_the_reader_looks_for(apply, mode):
    run = last_run(lines(f"2026-10-25 15:19:19,100 INFO {started_message(apply)}"), NOW)
    assert run.mode == mode


# -- the phase before the videos, and slices -----------------------------------------------------------

def refs(done, total, second):
    return f"2026-10-25 15:19:{second:02d},000 INFO {references_message(done, total)}"


def test_the_reference_phase_shows_its_progress_and_estimates_the_time_left_from_its_own_pace():
    run = last_run(lines(START, refs(0, 100, 20), refs(25, 100, 30)), NOW)
    assert run.references == (25, 100) and run.references_remaining_seconds == pytest.approx(30)  # 25 photos took 10 s
    assert run.phase == "Learning the faces of your people" and run.total is None and run.done == 0 and run.errors == 0


def test_the_reference_phase_has_no_estimate_before_the_first_photo_is_done():
    run = last_run(lines(START, refs(0, 100, 20)), NOW)
    assert run.references == (0, 100) and run.references_remaining_seconds is None


def test_after_the_library_line_the_phase_is_the_videos_and_the_references_stay_for_the_record():
    run = last_run(lines(START, refs(0, 100, 20), refs(100, 100, 40), LIBRARY.replace("15:19:20", "15:19:41"), FIRST), NOW)
    assert run.phase == "Analysing videos" and run.references == (100, 100) and run.references_remaining_seconds is None
    assert run.done == 1


def test_a_run_without_reference_lines_has_none():
    assert last_run(lines(START, LIBRARY), NOW).references is None


def test_a_slice_replaces_the_library_as_the_total_and_remembers_how_big_the_library_is():
    run = last_run(lines(START, LIBRARY, "2026-10-25 15:19:20,300 INFO " + slice_message(2, 3), FIRST), NOW)
    assert (run.total, run.slice_of, run.done) == (2, 3, 1)
    assert run.remaining_seconds == pytest.approx(4.8)  # one more video of the two


def test_a_run_without_a_slice_has_no_slice():
    assert last_run(lines(START, LIBRARY, FIRST), NOW).slice_of is None


def test_the_lines_that_are_not_about_a_video_are_never_counted_as_one():
    run = last_run(lines(START, refs(120, 2432, 21), LIBRARY, "2026-10-25 15:19:20,300 INFO " + slice_message(20, 1553)), NOW)
    assert run.done == 0 and run.errors == 0 and run.current is None


# -- a run longer than one log file ------------------------------------------------------------

def rotated(log, number, *rows):
    log.with_name(f"{log.name}.{number}").write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_a_run_whose_marker_rotated_into_a_backup_is_still_found(tmp_path):
    log = tmp_path / "videotag.log"
    rotated(log, 1, START, LIBRARY, FIRST)
    log.write_text("\n".join([BROKEN, THIRD]) + "\n", encoding="utf-8")
    run = last_run(read_lines(log), NOW)
    assert (run.total, run.done, run.errors) == (3, 3, 1)
    assert run.started == datetime(2026, 10, 25, 15, 19, 19, 100000)


def test_the_backups_are_read_only_as_far_back_as_the_newest_run_marker(tmp_path):
    log = tmp_path / "videotag.log"
    rotated(log, 2, *EARLIER_RUN)
    rotated(log, 1, START, LIBRARY)
    log.write_text(FIRST + "\n", encoding="utf-8")
    run = last_run(read_lines(log), NOW)
    assert (run.mode, run.total, run.done) == ("dry run", 3, 1)


def test_a_backup_is_ignored_when_the_current_file_already_holds_a_marker(tmp_path):
    log = tmp_path / "videotag.log"
    rotated(log, 1, *EARLIER_RUN)
    log.write_text("\n".join([START, LIBRARY]) + "\n", encoding="utf-8")
    assert last_run(read_lines(log), NOW).total == 3


def test_a_marker_beyond_the_kept_backups_is_not_found_and_a_missing_log_is_empty(tmp_path):
    log = tmp_path / "videotag.log"
    rotated(log, 2, START, LIBRARY)
    log.write_text(FIRST + "\n", encoding="utf-8")
    assert last_run(read_lines(log, backups=1), NOW) is None
    assert read_lines(tmp_path / "missing.log") == []


def test_the_backups_are_the_files_the_rotating_handler_really_writes(tmp_path):
    log = tmp_path / "videotag.log"
    handler = RotatingFileHandler(log, maxBytes=400, backupCount=LOG_BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("rotation-contract")
    logger.propagate, logger.level = False, logging.INFO
    logger.addHandler(handler)
    try:
        logger.info(started_message(False))
        logger.info("library: 9 photos, 8 videos")
        for number in range(8):
            logger.info("%d clip%d.mp4 frames=10 faces=1 | Alice=0.5/3:auto", number, number)
    finally:
        logger.removeHandler(handler)
        handler.close()
    assert "run started" not in log.read_text(encoding="utf-8")  # the marker really did rotate away
    run = last_run(read_lines(log), datetime.now())
    assert (run.total, run.done) == (8, 8)


# -- an undo is a run too ----------------------------------------------------------------------------------

UNDO = f"2026-10-25 15:19:19,100 INFO {UNDO_STARTED}"
REMOVED = f"2026-10-25 15:19:31,000 INFO {removed_message(5, 4, 3, 2)}"


def test_an_undo_in_progress_is_not_mistaken_for_reading_the_library():
    run = last_run(lines(UNDO), NOW)
    assert (run.mode, run.phase, run.finished, run.ended) == ("undo", "Removing what videotag wrote", False, None)


def test_an_undo_is_finished_by_its_summary_and_the_summary_says_what_was_removed():
    run = last_run(lines(UNDO, REMOVED), NOW)
    assert (run.phase, run.finished, run.ended) == ("Finished", True, datetime(2026, 10, 25, 15, 19, 31))
    assert run.written == {"tags": 5, "faces": 4, "descriptions": 3, "albums": 2}


def test_an_undo_after_a_run_is_the_latest_run():
    run = last_run(lines(*EARLIER_RUN, UNDO), NOW)
    assert run.mode == "undo" and run.total is None and run.done == 0
