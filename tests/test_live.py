from datetime import datetime

import pytest

from videotag.live import LIVE_NAME, LiveRun, read

STARTED = datetime(2026, 10, 26, 9, 30, 0, 250000)


@pytest.fixture
def path(tmp_path):
    return tmp_path / "reports" / LIVE_NAME


def test_the_live_file_is_not_a_report_the_dashboard_could_mistake_for_one():
    assert not LIVE_NAME.startswith("run-")  # reports are found by the pattern run-*.json


def test_rows_are_appended_one_line_each_and_read_back_with_the_start_of_their_run(path):
    live = LiveRun(path, clock=lambda: STARTED)
    live.begin()
    live.add({"video_id": 1, "filename": "a.mp4", "frames": 10})
    live.add({"video_id": 2, "filename": "b.mp4", "error": "download failed"})
    found = read(path)
    assert found.started == STARTED
    assert found.rows == [{"video_id": 1, "filename": "a.mp4", "frames": 10},
                          {"video_id": 2, "filename": "b.mp4", "error": "download failed"}]


def test_beginning_a_run_starts_the_file_afresh_so_an_older_runs_rows_never_show(path):
    old = LiveRun(path, clock=lambda: datetime(2026, 10, 25, 8, 0, 0))
    old.begin()
    old.add({"video_id": 99, "filename": "old.mp4"})
    new = LiveRun(path, clock=lambda: STARTED)
    new.begin()
    assert read(path).rows == [] and read(path).started == STARTED


def test_finishing_removes_the_file_because_the_report_replaces_it(path):
    live = LiveRun(path, clock=lambda: STARTED)
    live.begin()
    live.finish()
    assert not path.exists()
    live.finish()  # and finishing twice is fine


def test_a_row_that_is_only_half_written_is_left_out_not_an_error(path):
    live = LiveRun(path, clock=lambda: STARTED)
    live.begin()
    live.add({"video_id": 1, "filename": "a.mp4"})
    with path.open("a", encoding="utf-8") as out:
        out.write('{"video_id": 2, "file')
    assert [row["video_id"] for row in read(path).rows] == [1]


@pytest.mark.parametrize("content", ["", "not json\n", '{"video_id": 1}\n', '{"live": {"started": "yesterday"}}\n', "[1]\n"])
def test_a_file_without_a_proper_first_line_is_nothing(path, content):
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    assert read(path) is None


def test_a_missing_file_is_nothing(path):
    assert read(path) is None


def test_adding_before_beginning_is_a_mistake_that_is_not_silently_swallowed(path):
    with pytest.raises(RuntimeError, match="begin"):
        LiveRun(path, clock=lambda: STARTED).add({"video_id": 1})
