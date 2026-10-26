import http.client
import json
import logging
import re
import subprocess
import sys
import threading

import pytest

from videotag.accounts import AccountError, Identity, Layout
from videotag.credentials import Credentials, load_credentials, store_credentials
from videotag.dashboard import Sources
from videotag.gui import API_VERSION, ASSETS_DIR, TOKEN_HEADER, make_server
from videotag.launcher import LaunchError
from videotag.lock import AlreadyRunning

CHECKS = [{"name": "ffmpeg", "status": "ok", "detail": "ffmpeg version 7"}]


class Gui:
    """A real server on a free port with fake side effects, and a tiny HTTP client for it."""

    def __init__(self, tmp_path):
        assets = tmp_path / "assets"
        assets.mkdir()
        (assets / "index.html").write_text('<meta name="token" content="__TOKEN__">hello', encoding="utf-8")
        (assets / "app.js").write_text("// js", encoding="utf-8")
        (assets / "style.css").write_text("/* css */", encoding="utf-8")
        self.src = Sources(reports_dir=tmp_path / "reports", state_file=tmp_path / "state.json",
                           describe_dir=tmp_path / "describe", log_file=tmp_path / "videotag.log",
                           lock_file=tmp_path / "run.lock")
        self.launched, self.checked, self.launch_error = [], [], None
        self.layout = Layout(state=tmp_path / "d-state", reports=tmp_path / "d-reports", cache=tmp_path / "d-cache",
                             logs=tmp_path / "d-logs", parked=tmp_path / "d-accounts")
        self.verified, self.verify_error = [], None
        self.server = make_server(self.src, 0, assets=assets, doctor=self._doctor, launch=self._launch,
                                  verify=self._verify, layout=self.layout)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def _doctor(self):
        self.checked.append(1)
        return CHECKS

    def _verify(self, credentials):
        if self.verify_error:
            raise self.verify_error
        self.verified.append(Identity.of(credentials))

    def _launch(self, src, now, **options):
        if self.launch_error:
            raise self.launch_error
        self.launched.append(options)

    def request(self, method, path, body=None, *, token=True, host=None, origin=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port)
        connection.putrequest(method, path, skip_host=True)
        connection.putheader("Host", host or f"127.0.0.1:{self.port}")
        if token:
            connection.putheader(TOKEN_HEADER, self.server.token if token is True else token)
        if origin and method == "POST":
            connection.putheader("Origin", origin if isinstance(origin, str) else f"http://127.0.0.1:{self.port}")
        data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else b""
        connection.putheader("Content-Length", str(len(data)))
        connection.endheaders(data)
        response = connection.getresponse()
        payload = response.read()
        headers = dict(response.getheaders())
        connection.close()
        return response.status, headers, payload

    def get_json(self, path):
        status, _, payload = self.request("GET", path)
        assert status == 200
        return json.loads(payload)


@pytest.fixture
def gui(tmp_path):
    instance = Gui(tmp_path)
    yield instance
    instance.server.shutdown()
    instance.server.server_close()


def test_the_server_only_listens_on_the_loopback_address(gui):
    assert gui.server.server_address[0] == "127.0.0.1"


def test_the_page_is_served_with_the_session_token_and_locked_down_headers(gui):
    status, headers, payload = gui.request("GET", "/")
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert gui.server.token in payload.decode() and "__TOKEN__" not in payload.decode()
    policy = headers["Content-Security-Policy"]
    assert "default-src 'self'" in policy and "frame-ancestors 'none'" in policy
    assert headers["X-Content-Type-Options"] == "nosniff" and headers["Cache-Control"] == "no-store"


def test_script_and_style_are_served_with_their_types(gui):
    assert gui.request("GET", "/app.js")[1]["Content-Type"].startswith("text/javascript")
    assert gui.request("GET", "/style.css")[1]["Content-Type"].startswith("text/css")


@pytest.mark.parametrize("path", ["/nope", "/../state.json", "/%2e%2e/state.json", "/api/../app.js", "/index.html",
                                  "//etc/passwd"])
def test_only_the_known_pages_exist(gui, path):
    assert gui.request("GET", path)[0] == 404


def test_a_request_with_a_foreign_host_name_is_refused_so_dns_rebinding_cannot_read_the_data(gui):
    assert gui.request("GET", "/api/library", host="evil.example")[0] == 403
    assert gui.request("GET", "/api/library", host=f"evil.example:{gui.port}")[0] == 403
    assert gui.request("GET", "/api/library", host=f"localhost:{gui.port}")[0] == 200


def test_status_carries_the_run_the_log_and_the_data_version(gui):
    status = gui.get_json("/api/status")
    assert status["status"]["state"] == "never" and status["log"] == [] and "version" in status


def test_the_library_endpoint_returns_the_dashboard_view(gui):
    assert gui.get_json("/api/library")["videos"] == []


def test_the_history_endpoint_returns_a_list(gui):
    assert gui.get_json("/api/history") == []


def test_the_health_check_runs_only_on_request_and_is_then_remembered(gui):
    assert gui.get_json("/api/doctor") == {"checks": None, "at": None}
    assert gui.checked == []
    status, _, payload = gui.request("POST", "/api/doctor")
    assert status == 200 and json.loads(payload)["checks"] == CHECKS
    remembered = gui.get_json("/api/doctor")
    assert remembered["checks"] == CHECKS and remembered["at"] and gui.checked == [1]


def test_a_post_without_the_token_is_refused(gui):
    assert gui.request("POST", "/api/run", {"apply": False}, token=False)[0] == 403
    assert gui.request("POST", "/api/doctor", token=False)[0] == 403
    assert gui.launched == [] and gui.checked == []


def test_a_post_with_a_wrong_token_is_refused(gui):
    assert gui.request("POST", "/api/run", {"apply": False}, token="something else")[0] == 403
    assert gui.launched == []


def test_a_post_from_another_origin_is_refused(gui):
    assert gui.request("POST", "/api/run", {"apply": False}, origin="http://evil.example")[0] == 403
    assert gui.launched == []


def test_a_run_is_started_with_the_options_from_the_page(gui):
    body = {"apply": True, "max_writes": 5, "describe": False}
    status, _, payload = gui.request("POST", "/api/run", body)
    assert status == 202 and json.loads(payload) == {"started": True}
    assert gui.launched == [body]


def test_the_defaults_are_a_dry_run_with_descriptions(gui):
    assert gui.request("POST", "/api/run", {})[0] == 202
    assert gui.launched == [{"apply": False, "max_writes": 100, "describe": True}]


def test_a_run_that_is_already_going_is_a_conflict(gui):
    gui.launch_error = AlreadyRunning("a run is already in progress")
    status, _, payload = gui.request("POST", "/api/run", {})
    assert status == 409 and json.loads(payload)["error"] == "a run is already in progress"


def test_refused_options_are_a_bad_request(gui):
    gui.launch_error = LaunchError("max_writes must be a whole number from 1 to 100000")
    status, _, payload = gui.request("POST", "/api/run", {"max_writes": 0})
    assert status == 400 and "max_writes" in json.loads(payload)["error"]


@pytest.mark.parametrize("body", [b"{ not json", b"[1, 2]", b'{"apply": "yes"}', b'{"describe": 1}'])
def test_a_malformed_run_request_is_a_bad_request(gui, body):
    assert gui.request("POST", "/api/run", body)[0] == 400
    assert gui.launched == []


def test_an_oversized_body_is_refused(gui):
    assert gui.request("POST", "/api/run", b" " * 100_000)[0] == 413


def test_unknown_posts_and_methods_are_refused(gui):
    assert gui.request("POST", "/api/library")[0] == 404
    assert gui.request("PUT", "/api/run")[0] in (405, 501)
    assert gui.request("DELETE", "/api/run")[0] in (405, 501)


def test_the_shipped_page_references_only_files_the_server_ships_and_holds_the_token_slot():
    index = (ASSETS_DIR / "index.html").read_text(encoding="utf-8")
    assert "__TOKEN__" in index
    for name in ("app.js", "style.css"):
        assert (ASSETS_DIR / name).is_file() and f'"/{name}"' in index
    assert not re.search(r"""(src|href|action|data)=["']\s*(https?:)?//""", index)  # nothing is loaded from the internet
    script = (ASSETS_DIR / "app.js").read_text(encoding="utf-8")
    assert not re.search(r"https?://", script)  # the script never names an address either


def test_importing_the_gui_does_not_load_the_gpu_libraries():
    code = "import sys, videotag.gui; assert 'torch' not in sys.modules and 'transformers' not in sys.modules"
    assert subprocess.run([sys.executable, "-I", "-c", code], capture_output=True).returncode == 0


def test_a_port_that_is_taken_gives_a_plain_message_and_a_failure_exit(tmp_path, capsys):
    import socket

    from videotag.gui import serve
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        assert serve(Sources(), taken.getsockname()[1], open_browser=False) == 1
    assert "probably in use" in capsys.readouterr().out


# -- the account ---------------------------------------------------------------------------------

CLAUDE = Identity("https://nas.example.com", "claude")
CHRIS = Identity("https://nas.example.com", "chris")
LOGIN = {"url": CHRIS.url, "username": "chris", "password": "pw-never-echoed-123"}


def sign_in_as_claude(gui):
    """A stored login for Claude and saved data that belongs to Claude."""
    store_credentials(Credentials("claude", "claude-pw", CLAUDE.url))
    gui.layout.state.mkdir(parents=True)
    (gui.layout.state / "state.json").write_text("claude's", encoding="utf-8")
    gui.layout.owner_file.write_text(json.dumps({"url": CLAUDE.url, "username": "claude"}), encoding="utf-8")


def test_the_account_view_names_the_login_and_never_the_password(gui):
    sign_in_as_claude(gui)
    status, _, payload = gui.request("GET", "/api/account")
    view = json.loads(payload)
    assert status == 200 and view["current"]["label"] == "claude at nas.example.com" and view["busy"] is False
    assert "claude-pw" not in payload.decode()


def test_a_login_for_another_account_is_checked_stored_and_sets_the_old_data_aside(gui):
    sign_in_as_claude(gui)
    status, _, payload = gui.request("POST", "/api/account", LOGIN)
    view = json.loads(payload)
    assert status == 200 and gui.verified == [CHRIS]
    assert view["current"]["label"] == "chris at nas.example.com" and view["parked"][0]["label"] == CLAUDE.label
    assert load_credentials()[0].username == "chris" and load_credentials()[0].password == LOGIN["password"]
    assert not (gui.layout.state / "state.json").exists()
    assert (gui.layout.parked / CLAUDE.slug / "state" / "state.json").read_text(encoding="utf-8") == "claude's"
    assert LOGIN["password"] not in payload.decode()


def test_a_new_password_for_the_same_account_moves_nothing(gui):
    sign_in_as_claude(gui)
    status, _, _ = gui.request("POST", "/api/account", {**LOGIN, "url": CLAUDE.url, "username": "claude", "password": "rotated"})
    assert status == 200 and load_credentials()[0].password == "rotated"
    assert (gui.layout.state / "state.json").read_text(encoding="utf-8") == "claude's" and not gui.layout.parked.exists()


def test_a_login_the_nas_refuses_changes_nothing(gui):
    sign_in_as_claude(gui)
    gui.verify_error = AccountError("the NAS rejected this account name or password")
    status, _, payload = gui.request("POST", "/api/account", LOGIN)
    assert status == 400 and "rejected" in json.loads(payload)["error"]
    assert load_credentials()[0].username == "claude" and (gui.layout.state / "state.json").exists()
    assert not gui.layout.parked.exists()


def test_the_switch_waits_for_a_run_to_finish_and_does_not_even_try_the_login(gui):
    sign_in_as_claude(gui)
    gui.layout.lock_file.write_text("", encoding="utf-8")
    status, _, payload = gui.request("POST", "/api/account", LOGIN)
    assert status == 409 and "run is in progress" in json.loads(payload)["error"]
    assert gui.verified == [] and load_credentials()[0].username == "claude"


def test_a_login_from_the_environment_cannot_be_replaced_from_here(gui, monkeypatch):
    monkeypatch.setenv("VIDEOTAG_URL", CLAUDE.url)
    monkeypatch.setenv("VIDEOTAG_USER", "claude")
    monkeypatch.setenv("VIDEOTAG_PASSWORD", "from-env")
    status, _, payload = gui.request("POST", "/api/account", LOGIN)
    assert status == 409 and "VIDEOTAG_" in json.loads(payload)["error"] and gui.verified == []
    view = gui.get_json("/api/account")
    assert view["locked"] is True and "from-env" not in json.dumps(view)


@pytest.mark.parametrize("body", [{}, {**LOGIN, "password": ""}, {**LOGIN, "username": "  "}, {**LOGIN, "url": 5},
                                  {**LOGIN, "password": "x" * 600}])
def test_incomplete_or_absurd_logins_are_a_bad_request(gui, body):
    assert gui.request("POST", "/api/account", body)[0] == 400
    assert gui.verified == []


@pytest.mark.parametrize("url", ["http://nas.example.com", "nas.example.com", "ftp://nas.example.com"])
def test_only_https_addresses_are_accepted(gui, url):
    status, _, payload = gui.request("POST", "/api/account", {**LOGIN, "url": url})
    assert status == 400 and "https://" in json.loads(payload)["error"] and gui.verified == []


def test_changing_the_account_needs_the_session_token_and_the_pages_own_origin(gui):
    assert gui.request("POST", "/api/account", LOGIN, token=False)[0] == 403
    assert gui.request("POST", "/api/account", LOGIN, origin="http://evil.example")[0] == 403
    assert gui.verified == []


def test_a_successful_switch_clears_the_old_health_result(gui):
    sign_in_as_claude(gui)
    gui.request("POST", "/api/doctor")
    assert gui.get_json("/api/doctor")["checks"] == CHECKS
    gui.request("POST", "/api/account", LOGIN)
    assert gui.get_json("/api/doctor") == {"checks": None, "at": None}


def test_the_password_reaches_no_log_and_no_error_message(gui, caplog):
    caplog.set_level(logging.DEBUG)
    sign_in_as_claude(gui)
    gui.verify_error = AccountError("the NAS rejected this account name or password")
    answers = [gui.request("POST", "/api/account", LOGIN)[2], gui.request("POST", "/api/account", {**LOGIN, "url": "http://x"})[2]]
    gui.verify_error = None
    answers.append(gui.request("POST", "/api/account", LOGIN)[2])
    assert all(LOGIN["password"].encode() not in answer for answer in answers) and LOGIN["password"] not in caplog.text


# -- page and server must be the same version ----------------------------------------------------

def test_the_status_says_which_version_of_the_api_this_server_speaks(gui):
    assert gui.get_json("/api/status")["api"] == API_VERSION


def test_the_page_knows_the_api_version_it_needs_and_it_is_the_one_this_server_speaks():
    """An update changes the page files at once, but a dashboard that is already open keeps running the old code."""
    script = (ASSETS_DIR / "app.js").read_text(encoding="utf-8")
    needed = re.search(r"^const API_VERSION = (\d+);", script, re.MULTILINE)
    assert needed and int(needed.group(1)) == API_VERSION


def test_the_page_has_a_place_to_say_the_server_is_older_than_it(gui):
    index = (ASSETS_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="stale"' in index and "start it again" in index
