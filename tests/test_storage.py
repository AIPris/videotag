import os
import threading
import time

import pytest

from videotag import descache
from videotag.describe import Description
from videotag.lock import AlreadyRunning, single_run, taken_at
from videotag.state import State, StateError, WrittenTag


def test_state_survives_a_restart(tmp_path):
    path = tmp_path / "state.json"
    state = State(path, [WrittenTag(1, 2, 3, "Alice?", "review", "2026-01-01")], {(4, 2)}, {(5, 2)}, {2: 77}, {(8, 2): 900},
                  {9: "[videotag]\nEN: x\n[/videotag]"}, {10})
    state.save()
    loaded = State.load(path)
    assert loaded.written == state.written and loaded.rejected == {(4, 2)} and loaded.confirmed == {(5, 2)}
    assert loaded.albums == {2: 77} and loaded.faces == {(8, 2): 900}
    assert loaded.descriptions == {9: "[videotag]\nEN: x\n[/videotag]"} and loaded.description_rejected == {10}


def test_a_state_file_from_before_albums_and_faces_existed_still_loads(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"written": [], "rejected": [], "confirmed": []}', encoding="utf-8")
    loaded = State.load(path)
    assert loaded.albums == {} and loaded.faces == {} and loaded.descriptions == {} and loaded.description_rejected == set()


def test_description_cache_round_trip_including_an_unusable_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(descache, "DESCRIBE_DIR", tmp_path)
    description = Description("A cat.", "Eine Katze.", ("cat",), ("Katze",), ("cat",), ("Katze",), "Tokyo", "a logo")
    assert descache.load(1, "key") == (False, None)
    descache.save(1, "key", description)
    assert descache.load(1, "key") == (True, description)
    assert descache.load(1, "other-key") == (False, None)
    descache.save(2, "key", None)  # tried, nothing usable: not tried again
    assert descache.load(2, "key") == (True, None)


def test_a_missing_state_file_is_an_empty_state(tmp_path):
    assert State.load(tmp_path / "none.json").written == []


def test_a_state_file_saved_with_a_byte_order_mark_still_loads(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"written": [], "rejected": [[1, 2]], "confirmed": []}', encoding="utf-8-sig")
    assert State.load(path).rejected == {(1, 2)}


@pytest.mark.parametrize("content", ["not json", '{"written": []}', "[]"])
def test_an_unreadable_state_file_stops_the_run_instead_of_being_ignored(tmp_path, content):
    path = tmp_path / "state.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(StateError):
        State.load(path)


def test_a_second_run_is_refused_while_the_first_holds_the_lock(tmp_path):
    lock = tmp_path / "run.lock"
    with single_run(lock), pytest.raises(AlreadyRunning), single_run(lock):
        pass
    with single_run(lock):  # released afterwards
        pass


def test_a_stale_lock_is_taken_over(tmp_path):
    lock = tmp_path / "run.lock"
    lock.write_text("")
    old = time.time() - 10_000
    os.utime(lock, (old, old))
    with single_run(lock, stale_after=3600):
        assert lock.exists()


def test_a_run_that_outlasts_the_stale_limit_keeps_its_lock(tmp_path):
    lock = tmp_path / "run.lock"
    with single_run(lock, stale_after=1.0, heartbeat=0.05):
        time.sleep(1.6)  # longer than the limit: only a refreshed lock is still held
        with pytest.raises(AlreadyRunning), single_run(lock, stale_after=1.0):
            pass
        assert time.time() - lock.stat().st_mtime < 1.0


def test_the_heartbeat_ends_with_the_run_and_does_not_bring_the_lock_back(tmp_path):
    lock = tmp_path / "run.lock"
    with single_run(lock, heartbeat=0.05):
        pass
    time.sleep(0.2)
    assert not lock.exists()
    assert not any(thread.name == "videotag-lock-heartbeat" for thread in threading.enumerate())


def test_the_lock_records_when_it_was_taken_even_after_it_was_refreshed(tmp_path):
    lock = tmp_path / "run.lock"
    before = time.time()
    with single_run(lock, heartbeat=0.05):
        time.sleep(0.3)
        assert before - 0.01 <= taken_at(lock) <= before + 0.2
        assert lock.stat().st_mtime > taken_at(lock) + 0.1


def test_a_lock_without_a_record_was_taken_when_it_was_last_written(tmp_path):
    lock = tmp_path / "run.lock"
    lock.write_text("")
    os.utime(lock, (1_000, 1_000))
    assert taken_at(lock) == 1_000
    assert taken_at(tmp_path / "missing.lock") is None
