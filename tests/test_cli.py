import logging
import subprocess
import sys
from contextlib import nullcontext
from datetime import datetime

import pytest
from test_pipeline import RecordingClient, prepare

from videotag import admin, cli, settings
from videotag.accounts import Identity, Layout, claim_data
from videotag.config import DEFAULT_MAX_WRITES, NO_LIMIT
from videotag.credentials import CredentialError, Credentials, load_credentials, store_credentials
from videotag.dashboard import Sources, run_status
from videotag.live import LIVE_NAME
from videotag.live import read as read_live
from videotag.sampling import PRESETS
from videotag.settings import Settings
from videotag.slices import Slice
from videotag.state import State


def formatted_log(caplog) -> str:
    """The records of a run, formatted the way the log file handler formats them."""
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    return "\n".join(formatter.format(record) for record in caplog.records)


def test_negative_write_limit_is_rejected_during_argument_parsing():
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        cli._write_limit("-1")


def test_zero_write_limit_is_allowed():
    assert cli._write_limit("0") == 0


@pytest.fixture
def main_without_the_data_home(monkeypatch, tmp_path, caplog):
    """main() with no log file, no run lock and reports in a temporary folder: tests never touch the real data home."""
    monkeypatch.setattr(cli, "_setup_logging", lambda: None)
    monkeypatch.setattr(cli, "single_run", nullcontext)
    monkeypatch.setattr(cli, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(cli, "EVIDENCE_DIR", tmp_path / "evidence")
    monkeypatch.setattr(cli, "claim_data", lambda credentials: None)
    caplog.set_level(logging.INFO, logger="videotag")
    return caplog


def the_dashboard_view_of(caplog, tmp_path):
    src = Sources(reports_dir=tmp_path / "reports", state_file=tmp_path / "state.json", describe_dir=tmp_path / "describe",
                  log_file=tmp_path / "videotag.log", lock_file=tmp_path / "run.lock")
    src.log_file.write_text(formatted_log(caplog) + "\n", encoding="utf-8")
    return run_status(src, datetime.now())


def test_the_whole_log_of_a_run_is_understood_by_the_run_log_reader(main_without_the_data_home, monkeypatch, tmp_path):
    client = RecordingClient()
    prepare(monkeypatch)
    monkeypatch.setattr(cli, "load_credentials", lambda: (object(), "test"))
    monkeypatch.setattr(cli, "PhotosClient", lambda credentials: nullcontext(client))
    monkeypatch.setattr(cli, "FaceEngine", lambda: None)
    monkeypatch.setattr(cli.State, "load", classmethod(lambda cls: State(tmp_path / "state.json")))
    assert cli.main(["run", "--apply", "--no-describe"]) == 0
    assert len(list((tmp_path / "reports").glob("run-*.json"))) == 1
    assert not (tmp_path / "reports" / LIVE_NAME).exists()  # the report took its place
    status = the_dashboard_view_of(main_without_the_data_home, tmp_path)
    run = status["run"]
    assert status["state"] == "idle" and run["finished"]
    assert (run["mode"], run["total"], run["done"], run["errors"]) == ("apply", 1, 1, 0)
    assert run["current"] == "1 clip.mp4" and run["planned_tags"] == 1
    assert run["written"] == {"tags": 1, "faces": 1, "descriptions": 1, "albums": 0}  # a confident match goes into no album


def test_a_run_that_cannot_log_in_still_leaves_a_marker_and_the_reason(main_without_the_data_home, monkeypatch, tmp_path, capsys):
    def no_login():
        raise CredentialError("no login stored")
    monkeypatch.setattr(cli, "load_credentials", no_login)
    assert cli.main(["run"]) == 1
    status = the_dashboard_view_of(main_without_the_data_home, tmp_path)
    assert status["state"] == "interrupted" and (status["run"]["mode"], status["run"]["errors"]) == ("dry run", 1)
    assert "no login stored" in formatted_log(main_without_the_data_home)


def test_an_unexpected_crash_of_a_run_is_logged_with_its_reason(main_without_the_data_home, monkeypatch, tmp_path):
    def crash():
        raise RuntimeError("boom")
    monkeypatch.setattr(cli, "load_credentials", crash)
    with pytest.raises(RuntimeError, match="boom"):
        cli.main(["run", "--apply"])
    log = formatted_log(main_without_the_data_home)
    assert "run crashed: RuntimeError: boom" in log
    status = the_dashboard_view_of(main_without_the_data_home, tmp_path)
    assert status["state"] == "interrupted" and status["run"]["mode"] == "apply"


def test_the_gui_command_needs_no_nas_login_and_passes_its_options_on(monkeypatch):
    calls = []
    monkeypatch.setattr("videotag.gui.serve", lambda src, port, open_browser: calls.append((port, open_browser)) or 0)
    monkeypatch.setattr(cli, "load_credentials", lambda: pytest.fail("the dashboard must start without a NAS login"))
    assert cli.main(["gui", "--port", "0", "--no-browser"]) == 0
    assert cli.main(["gui"]) == 0
    assert calls == [(0, False), (8765, True)]


def test_the_gui_command_is_not_an_unattended_run(monkeypatch):
    monkeypatch.setattr("videotag.gui.serve", lambda src, port, open_browser: 0)
    monkeypatch.setattr(cli, "single_run", lambda: pytest.fail("the dashboard must never hold the run lock"))
    assert cli.main(["gui", "--no-browser"]) == 0


def test_the_command_line_does_not_load_the_gui_unless_asked_to():
    code = "import sys, videotag.cli; assert 'videotag.gui' not in sys.modules and 'http.server' not in sys.modules"
    assert subprocess.run([sys.executable, "-I", "-c", code], capture_output=True).returncode == 0


# -- the settings -----------------------------------------------------------------------------------

@pytest.fixture
def run_options(main_without_the_data_home, monkeypatch, tmp_path):
    """The RunOptions a `videotag run` builds, with everything past that point stubbed out."""
    seen = []
    monkeypatch.setattr(cli, "load_credentials", lambda: (object(), "test"))
    monkeypatch.setattr(cli, "PhotosClient", lambda credentials: nullcontext(object()))
    monkeypatch.setattr(cli, "FaceEngine", lambda: None)
    monkeypatch.setattr(cli.State, "load", classmethod(lambda cls: State(tmp_path / "state.json")))
    monkeypatch.setattr(cli, "run", lambda client, engine, describer, options, state, live=None, **more:
                        seen.append((options, describer)) or [])
    return seen


def test_a_run_without_flags_does_what_the_tool_always_did(run_options):
    assert cli.main(["run"]) == 0
    options, describer = run_options[0]
    assert options.sampling == PRESETS["thorough"] and options.describe and describer is not None and not options.reanalyse


def test_a_run_uses_the_saved_settings(run_options):
    settings.save(Settings("balanced", False))
    assert cli.main(["run"]) == 0
    options, describer = run_options[0]
    assert options.sampling == PRESETS["balanced"] and not options.describe and describer is None


def test_a_flag_wins_over_the_saved_settings_for_that_one_run(run_options):
    settings.save(Settings("balanced", False))
    assert cli.main(["run", "--preset", "fast", "--describe"]) == 0
    assert cli.main(["run"]) == 0
    first, second = run_options[0][0], run_options[1][0]
    assert first.sampling == PRESETS["fast"] and first.describe
    assert second.sampling == PRESETS["balanced"] and not second.describe  # the file itself was not touched


def test_no_describe_still_works_as_a_flag(run_options):
    assert cli.main(["run", "--no-describe"]) == 0
    assert not run_options[0][0].describe


def test_re_analysing_is_a_flag_of_one_run(run_options):
    assert cli.main(["run", "--reanalyse"]) == 0
    assert run_options[0][0].reanalyse


def test_a_settings_file_that_holds_a_per_run_choice_stops_the_run_and_the_log_says_why(run_options, main_without_the_data_home,
                                                                                         tmp_path, capsys):
    (settings.config.SETTINGS_FILE).write_text('{"apply": true}', encoding="utf-8")
    assert cli.main(["run"]) == 1
    assert run_options == [] and "'apply' is a choice for one run" in capsys.readouterr().out
    assert "'apply' is a choice for one run" in formatted_log(main_without_the_data_home)


def test_the_settings_command_shows_the_choices_and_where_they_are_kept(capsys):
    assert cli.main(["settings"]) == 0
    out = capsys.readouterr().out
    assert "preset: thorough" in out and "describe: yes" in out and "settings.json" in out


def test_the_settings_command_saves_what_it_is_given_and_keeps_the_rest(capsys):
    assert cli.main(["settings", "--preset", "fast"]) == 0
    assert settings.load() == Settings("fast", True)
    assert cli.main(["settings", "--no-describe"]) == 0
    assert settings.load() == Settings("fast", False)
    assert "preset: fast" in capsys.readouterr().out


# -- slices ---------------------------------------------------------------------------------------------

def test_a_run_without_slice_flags_looks_at_the_whole_library(run_options):
    assert cli.main(["run"]) == 0
    assert run_options[0][0].slice == Slice() and not run_options[0][0].slice.narrows


def test_the_slice_flags_reach_the_run(run_options):
    assert cli.main(["run", "--limit", "5", "--new-only", "--album", "12"]) == 0
    assert run_options[0][0].slice == Slice(limit=5, new_only=True, album_id=12)


@pytest.mark.parametrize("flags", [["--limit", "5"], ["--new-only"], ["--album", "12"]])
def test_a_slice_cannot_be_applied_and_nothing_starts_or_is_logged(run_options, main_without_the_data_home, capsys, flags):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["run", "--apply", *flags])
    assert stopped.value.code == 2 and "dry run" in capsys.readouterr().err
    assert run_options == [] and main_without_the_data_home.records == []


@pytest.mark.parametrize("limit", ["0", "-3", "many"])
def test_a_limit_that_is_not_a_positive_whole_number_is_refused(run_options, capsys, limit):
    with pytest.raises(SystemExit):
        cli.main(["run", "--limit", limit])
    assert "positive whole number" in capsys.readouterr().err


def test_the_albums_command_lists_your_own_albums_and_not_the_one_made_by_the_tool(monkeypatch, tmp_path, capsys):
    class Client:
        def albums(self):
            return [{"id": 5, "name": "Holiday"}, {"id": 900, "name": "To check"}]

    monkeypatch.setattr(cli, "claim_data", lambda credentials: None)
    monkeypatch.setattr(cli, "load_credentials", lambda: (object(), "test"))
    monkeypatch.setattr(cli, "PhotosClient", lambda credentials: nullcontext(Client()))
    monkeypatch.setattr(cli.State, "load", classmethod(lambda cls: State(tmp_path / "state.json", review_album=900)))
    assert cli.main(["albums"]) == 0
    out = capsys.readouterr().out
    assert "Holiday" in out and "5" in out and "To check" not in out


# -- the account guard ---------------------------------------------------------------------------

A = Identity("https://nas.example.com", "claude")
B = Identity("https://nas.example.com", "chris")


@pytest.fixture
def account_layout(tmp_path, monkeypatch, main_without_the_data_home):
    """Saved data that belongs to account A, a login for account B, and a NAS client that must never be created."""
    layout = Layout(state=tmp_path / "state", reports=tmp_path / "reports-a", cache=tmp_path / "cache",
                    logs=tmp_path / "logs", parked=tmp_path / "accounts")
    layout.state.mkdir()
    (layout.state / "state.json").write_text("claude's", encoding="utf-8")
    layout.owner_file.write_text('{"url": "https://nas.example.com", "username": "claude"}', encoding="utf-8")
    monkeypatch.setattr(cli, "claim_data", lambda credentials: claim_data(credentials, layout))
    monkeypatch.setattr(cli, "load_credentials", lambda: (Credentials("chris", "pw", B.url), "test"))

    def no_client(credentials):
        pytest.fail("the NAS must not be contacted with another account's data on disk")

    monkeypatch.setattr(cli, "PhotosClient", no_client)
    return layout


@pytest.mark.parametrize("command", ["run", "undo", "refs", "calibrate"])
def test_no_command_that_logs_in_touches_the_nas_with_another_accounts_data_on_disk(account_layout, command, capsys):
    assert cli.main([command]) == 1
    assert "belongs to claude at nas.example.com" in capsys.readouterr().out
    assert (account_layout.state / "state.json").read_text(encoding="utf-8") == "claude's"


def test_the_refusal_of_a_run_is_in_the_log_so_the_dashboard_can_show_why(account_layout, main_without_the_data_home, tmp_path):
    assert cli.main(["run"]) == 1
    status = the_dashboard_view_of(main_without_the_data_home, tmp_path)
    assert status["state"] == "interrupted" and "belongs to claude" in formatted_log(main_without_the_data_home)


def test_setting_a_login_for_another_account_from_the_terminal_sets_the_data_aside_like_the_dashboard(
        account_layout, monkeypatch, capsys):
    store_credentials(Credentials("claude", "old-pw", A.url))
    monkeypatch.setattr(admin, "verify_login", lambda credentials: None)
    monkeypatch.setattr(admin, "apply_login", lambda credentials, current: admin_apply(credentials, current, account_layout))
    monkeypatch.setattr(admin.sys.stdin, "isatty", lambda: True)
    answers = iter(["chris", B.url])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(admin.getpass, "getpass", lambda prompt="": "new-pw")
    cli.cmd_credentials_set(None, None)
    out = capsys.readouterr().out
    assert "set aside" in out and "claude at nas.example.com" in out and "new-pw" not in out
    assert load_credentials()[0].username == "chris"
    assert (account_layout.parked).is_dir() and not (account_layout.state / "state.json").exists()


def admin_apply(credentials, current, layout):
    from videotag.accounts import apply_login
    return apply_login(credentials, current, layout)


def test_a_run_gives_the_pipeline_a_live_file_beside_the_reports_and_removes_it_when_the_report_is_saved(
        run_options, monkeypatch, tmp_path):
    seen = []

    def watching_run(client, engine, describer, options, state, live=None, **more):
        live.begin()
        live.add({"video_id": 1, "filename": "a.mp4"})
        seen.append(read_live(tmp_path / "reports" / LIVE_NAME).rows)
        return [{"video_id": 1, "filename": "a.mp4"}]

    monkeypatch.setattr(cli, "run", watching_run)
    assert cli.main(["run"]) == 0
    assert seen == [[{"video_id": 1, "filename": "a.mp4"}]]
    assert not (tmp_path / "reports" / LIVE_NAME).exists() and len(list((tmp_path / "reports").glob("run-*.json"))) == 1


def test_a_run_that_dies_leaves_its_live_file_but_never_a_report(run_options, monkeypatch, tmp_path):
    def dying_run(client, engine, describer, options, state, live=None, **more):
        live.begin()
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "run", dying_run)
    with pytest.raises(RuntimeError, match="boom"):
        cli.main(["run"])
    assert (tmp_path / "reports" / LIVE_NAME).exists() and list((tmp_path / "reports").glob("run-*.json")) == []


def test_a_run_is_told_where_to_keep_its_evidence_and_its_accuracy_report(run_options, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(cli, "run", lambda client, engine, describer, options, state, live=None, **more: seen.append(more) or [])
    assert cli.main(["run"]) == 0
    assert seen == [{"evidence_dir": tmp_path / "evidence", "accuracy_file": tmp_path / "reports" / "accuracy.json"}]


# -- the write cap ----------------------------------------------------------------------------------------

def test_the_write_cap_is_a_number_and_all_means_no_limit(run_options):
    for flags in ([], ["--max-writes", "7"], ["--max-writes", "all"]):
        assert cli.main(["run", "--apply", *flags]) == 0
    assert [options.max_writes for options, _ in run_options] == [DEFAULT_MAX_WRITES, 7, NO_LIMIT]


@pytest.mark.parametrize("cap", ["lots", "All", "1.5", ""])
def test_a_write_cap_that_is_neither_a_whole_number_nor_all_is_refused(run_options, capsys, cap):
    with pytest.raises(SystemExit):
        cli.main(["run", "--apply", "--max-writes", cap])
    assert "whole number or 'all'" in capsys.readouterr().err and run_options == []


# -- undo ---------------------------------------------------------------------------------------------------

def test_an_undo_leaves_the_marker_and_the_summary_the_dashboard_reads(main_without_the_data_home, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "load_credentials", lambda: (object(), "test"))
    monkeypatch.setattr(cli, "PhotosClient", lambda credentials: nullcontext(object()))
    monkeypatch.setattr(cli.State, "load", classmethod(lambda cls: State(tmp_path / "state.json")))
    for name, removed in (("undo_all", 5), ("delete_faces", 4), ("delete_descriptions", 3), ("delete_album", 2)):
        monkeypatch.setattr(cli, name, lambda client, state, removed=removed: removed)
    assert cli.main(["undo"]) == 0
    run = the_dashboard_view_of(main_without_the_data_home, tmp_path)["run"]
    assert (run["mode"], run["finished"]) == ("undo", True)
    assert run["written"] == {"tags": 5, "faces": 4, "descriptions": 3, "albums": 2}
