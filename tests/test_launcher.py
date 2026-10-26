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
    assert command == [sys.executable, "-I", "-m", "videotag", "run"]
    assert kwargs["stdout"] == kwargs["stderr"] == kwargs["stdin"] == subprocess.DEVNULL  # the run logs to its own file


def test_apply_passes_the_write_cap(tmp_path):
    command, _ = launch(tmp_path, apply=True, max_writes=25)
    assert command[-3:] == ["--apply", "--max-writes", "25"]


def test_describing_can_be_switched_off(tmp_path):
    command, _ = launch(tmp_path, describe=False)
    assert command[-1] == "--no-describe"


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
