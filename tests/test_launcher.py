import os
import subprocess
import sys
from datetime import datetime

import pytest

from videotag.dashboard import Sources
from videotag.launcher import LaunchError, start_run
from videotag.lock import AlreadyRunning

NOW = datetime(2026, 10, 25, 15, 19, 40)


class FakePopen:
    launched = []

    def __init__(self, command, **kwargs):
        FakePopen.launched.append((command, kwargs))


@pytest.fixture(autouse=True)
def no_real_processes():
    FakePopen.launched = []


def sources(tmp_path) -> Sources:
    return Sources(reports_dir=tmp_path / "reports", state_file=tmp_path / "state.json", describe_dir=tmp_path / "describe",
                   log_file=tmp_path / "videotag.log", lock_file=tmp_path / "run.lock")


def launch(tmp_path, **options):
    start_run(sources(tmp_path), NOW, popen=FakePopen, **{"apply": False, "max_writes": 100, "describe": True, **options})
    return FakePopen.launched[0]


def test_a_dry_run_starts_the_real_command_in_isolated_mode(tmp_path):
    command, kwargs = launch(tmp_path)
    assert command == [sys.executable, "-I", "-m", "videotag", "run", "--describe"]
    assert kwargs["stdout"] == kwargs["stderr"] == kwargs["stdin"] == subprocess.DEVNULL  # the run logs to its own file


def test_apply_passes_the_write_cap(tmp_path):
    command, _ = launch(tmp_path, apply=True, max_writes=25)
    assert command[5:8] == ["--apply", "--max-writes", "25"]


def test_describing_can_be_switched_off(tmp_path):
    command, _ = launch(tmp_path, describe=False)
    assert command[-1] == "--no-describe"


def test_what_the_dialog_showed_is_passed_as_a_flag_so_it_beats_the_saved_settings(tmp_path):
    assert launch(tmp_path, describe=True)[0][-1] == "--describe"  # the saved file may say no; the dialog said yes


def test_a_preset_is_passed_on_and_left_out_when_the_page_did_not_choose_one(tmp_path):
    assert launch(tmp_path, preset="balanced")[0][-2:] == ["--preset", "balanced"]
    FakePopen.launched.clear()
    assert "--preset" not in launch(tmp_path)[0]


def test_a_slice_is_passed_on_to_a_dry_run(tmp_path):
    command, _ = launch(tmp_path, limit=20, new_only=True, album=12)
    assert command[-5:] == ["--limit", "20", "--new-only", "--album", "12"]


def test_re_analysing_is_passed_on(tmp_path):
    assert launch(tmp_path, reanalyse=True)[0][-1] == "--reanalyse"


@pytest.mark.parametrize("slice_", [{"limit": 5}, {"new_only": True}, {"album": 3}])
def test_a_slice_cannot_be_applied(tmp_path, slice_):
    with pytest.raises(LaunchError, match="dry run"):
        launch(tmp_path, apply=True, **slice_)
    assert FakePopen.launched == []


@pytest.mark.parametrize("options", [{"preset": "turbo"}, {"preset": 3}, {"limit": 0}, {"limit": -1}, {"limit": "5"},
                                     {"limit": True}, {"album": "x"}, {"album": True}, {"album": 0}, {"album": -4}])
def test_nonsense_options_are_refused_before_anything_starts(tmp_path, options):
    with pytest.raises(LaunchError):
        launch(tmp_path, **options)
    assert FakePopen.launched == []


def test_the_run_outlives_the_gui_window(tmp_path):
    _, kwargs = launch(tmp_path)
    assert kwargs.get("start_new_session") or kwargs.get("creationflags", 0) & subprocess.CREATE_NEW_PROCESS_GROUP


@pytest.mark.parametrize("cap", [0, -3, "ten", True, 100_001])
def test_a_nonsense_write_cap_is_refused(tmp_path, cap):
    with pytest.raises(LaunchError, match="max_writes"):
        launch(tmp_path, apply=True, max_writes=cap)
    assert FakePopen.launched == []


def test_nothing_starts_while_another_run_holds_the_lock(tmp_path):
    src = sources(tmp_path)
    src.lock_file.write_text("", encoding="utf-8")
    os.utime(src.lock_file, (NOW.timestamp() - 5, NOW.timestamp() - 5))
    with pytest.raises(AlreadyRunning, match="already in progress"):
        start_run(src, NOW, apply=False, max_writes=100, describe=True, popen=FakePopen)
    assert FakePopen.launched == []
