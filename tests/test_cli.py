import logging
import subprocess
import sys
from contextlib import nullcontext
from datetime import datetime

import pytest
from test_pipeline import RecordingClient, prepare

from videotag import admin, cli
from videotag.accounts import Identity, Layout, claim_data
from videotag.credentials import CredentialError, Credentials, load_credentials, store_credentials
from videotag.dashboard import Sources, run_status
from videotag.state import State


def formatted_log(caplog) -> str:
    """The records of a run, formatted the way the log file handler formats them."""
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    return "\n".join(formatter.format(record) for record in caplog.records)


@pytest.fixture
def main_without_the_data_home(monkeypatch, tmp_path, caplog):
    """main() with no log file, no run lock and reports in a temporary folder: tests never touch the real data home."""
    monkeypatch.setattr(cli, "_setup_logging", lambda: None)
    monkeypatch.setattr(cli, "single_run", nullcontext)
    monkeypatch.setattr(cli, "REPORTS_DIR", tmp_path / "reports")
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
    status = the_dashboard_view_of(main_without_the_data_home, tmp_path)
    run = status["run"]
    assert status["state"] == "idle" and run["finished"]
    assert (run["mode"], run["total"], run["done"], run["errors"]) == ("apply", 1, 1, 0)
    assert run["current"] == "1 clip.mp4" and run["planned_tags"] == 1
    assert run["written"] == {"tags": 1, "faces": 1, "descriptions": 1, "albums": 1}


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
