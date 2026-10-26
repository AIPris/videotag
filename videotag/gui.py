"""The optional GUI: a local web dashboard served from this process, `videotag gui`.

It only reads the data home (see `dashboard.py`) and starts runs as separate processes (see `launcher.py`), so a
headless or scheduled run neither needs it nor notices it. Standard library only, and nothing is loaded from the
internet. It listens on the loopback address and defends itself against other web pages in your browser:

- a request whose Host header is not this server's own address is refused (DNS rebinding could otherwise read the data),
- every POST needs a random per-session token that only the served page knows, and a matching Origin if one is sent.
"""
import hmac
import json
import logging
import secrets
import sys
import threading
import webbrowser
from dataclasses import asdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .accounts import (
    AccountBusy,
    AccountError,
    Identity,
    Layout,
    apply_login,
    default_layout,
    describe_account,
    is_busy,
    verify_login,
)
from .admin import run_checks
from .config import DEFAULT_MAX_WRITES, DEFAULT_PORT
from .credentials import CredentialError, Credentials, clean_url
from .dashboard import TIME_FORMAT, Sources, data_version, history, library_view, log_tail, run_status
from .launcher import LaunchError, start_run
from .lock import AlreadyRunning

log = logging.getLogger("videotag")

ASSETS_DIR = Path(__file__).parent / "gui_assets"
TOKEN_HEADER = "X-Videotag-Token"
MAX_BODY = 4096
DRAIN_LIMIT = 1_000_000  # of an oversized request body, read this much so the refusal reaches the client
PAGES = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
         "/style.css": ("style.css", "text/css; charset=utf-8")}
API_VERSION = 2  # the page and the server must agree on this; bump it with any change the page cannot work without
MAX_LOGIN_FIELD = 512
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
                                "frame-ancestors 'none'"),
}


def _health_checks() -> list[dict]:
    return [asdict(check) for check in run_checks()]


class GuiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, src: Sources, assets: Path, doctor, launch, verify, layout: Layout, clock):
        super().__init__(("127.0.0.1", port), Handler)
        self.src, self.assets, self.doctor, self.launch, self.clock = src, assets, doctor, launch, clock
        self.verify, self.layout = verify, layout
        self.token = secrets.token_urlsafe(24)
        self.health: dict = {"checks": None, "at": None}
        self.health_busy = threading.Lock()
        self.account_busy = threading.Lock()

    @property
    def own_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.server_address[1]}", f"localhost:{self.server_address[1]}"}

    def handle_error(self, request, client_address) -> None:
        if not isinstance(sys.exc_info()[1], ConnectionError):  # a browser tab closing mid-response is not worth a traceback
            super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server: GuiServer

    def log_message(self, format, *args) -> None:  # noqa: A002 - signature of the base class
        pass

    def do_GET(self) -> None:
        self._handle(self._get)

    def do_POST(self) -> None:
        self._handle(self._post)

    # -- plumbing ------------------------------------------------------------------------------

    def _handle(self, route) -> None:
        if self.headers.get("Host") not in self.server.own_hosts:
            return self._json(403, {"error": "unexpected Host header"})
        try:
            route(self.path.split("?", 1)[0])
        except Exception:  # noqa: BLE001 - one broken request must not take the dashboard down
            log.exception("GUI request %s failed", self.path)
            self._json(500, {"error": "internal error, see the terminal running `videotag gui`"})

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json")

    def _read_json(self) -> dict | None:
        """The JSON object in the request body; answers the request itself and returns None when that fails."""
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self.rfile.read(min(length, DRAIN_LIMIT))
            self._json(413, {"error": "request too large"})
            return None
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            self._json(400, {"error": "the request body must be a JSON object"})
            return None
        return data

    # -- GET -----------------------------------------------------------------------------------

    def _get(self, path: str) -> None:
        src, now = self.server.src, self.server.clock()
        api = {"/api/status": lambda: {"api": API_VERSION, "version": data_version(src), "now": now.strftime(TIME_FORMAT),
                                       "status": run_status(src, now), "log": log_tail(src)},
               "/api/library": lambda: library_view(src), "/api/history": lambda: history(src),
               "/api/doctor": lambda: self.server.health,
               "/api/account": lambda: {**describe_account(self.server.layout), "busy": is_busy(self.server.layout)}}
        if path in api:
            return self._json(200, api[path]())
        if path not in PAGES:
            return self._json(404, {"error": "not found"})
        name, content_type = PAGES[path]
        page = (self.server.assets / name).read_text(encoding="utf-8").replace("__TOKEN__", self.server.token)
        self._send(200, page.encode("utf-8"), content_type)

    # -- POST ----------------------------------------------------------------------------------

    def _post(self, path: str) -> None:
        host, origin = self.headers.get("Host"), self.headers.get("Origin")
        token_ok = hmac.compare_digest(self.headers.get(TOKEN_HEADER, ""), self.server.token)
        if not token_ok or origin not in (None, f"http://{host}"):
            return self._json(403, {"error": "missing or wrong session token"})
        if path == "/api/doctor":
            return self._run_health_check()
        if path == "/api/run":
            return self._start_run()
        if path == "/api/account":
            return self._change_account()
        self._json(404, {"error": "not found"})

    def _run_health_check(self) -> None:
        if not self.server.health_busy.acquire(blocking=False):
            return self._json(409, {"error": "a health check is already running"})
        try:
            checks = self.server.doctor()
            self.server.health = {"checks": checks, "at": self.server.clock().strftime(TIME_FORMAT)}
        finally:
            self.server.health_busy.release()
        self._json(200, self.server.health)

    def _change_account(self) -> None:
        """Use another login. Checked against the NAS first; for another account the saved data is set aside.

        Nothing about the password is ever logged or answered: not here, and not in the messages of the calls below."""
        data = self._read_json()
        if data is None:
            return
        login = _login_from(data)
        if isinstance(login, str):
            return self._json(400, {"error": login})
        server = self.server
        if not server.account_busy.acquire(blocking=False):
            return self._json(409, {"error": "another account change is in progress"})
        try:
            view = describe_account(server.layout)
            if view["locked"]:
                return self._json(409, {"error": "the login comes from the VIDEOTAG_* environment variables, which take "
                                                 "priority over a stored one; remove them to switch accounts here"})
            if is_busy(server.layout):
                return self._json(409, {"error": "a run is in progress; switch accounts when it has finished"})
            server.verify(login)
            current = Identity(view["current"]["url"], view["current"]["username"]) if view["current"] else None
            apply_login(login, current, server.layout)
            server.health = {"checks": None, "at": None}  # the old account's health result no longer applies
        except AccountBusy as exc:
            return self._json(409, {"error": str(exc)})
        except (AccountError, CredentialError) as exc:
            return self._json(400, {"error": str(exc)})
        finally:
            server.account_busy.release()
        self._json(200, {**describe_account(server.layout), "busy": False})

    def _start_run(self) -> None:
        data = self._read_json()
        if data is None:
            return
        options = {"apply": data.get("apply", False), "max_writes": data.get("max_writes", DEFAULT_MAX_WRITES),
                   "describe": data.get("describe", True)}
        if not isinstance(options["apply"], bool) or not isinstance(options["describe"], bool):
            return self._json(400, {"error": "apply and describe must be true or false"})
        try:
            self.server.launch(self.server.src, self.server.clock(), **options)
        except AlreadyRunning as exc:
            return self._json(409, {"error": str(exc)})
        except LaunchError as exc:
            return self._json(400, {"error": str(exc)})
        self._json(202, {"started": True})


def _login_from(data: dict) -> Credentials | str:
    """The login in a request, or the reason it cannot be used."""
    values = [data.get(name) for name in ("url", "username", "password")]
    if not all(isinstance(value, str) for value in values):
        return "address, account name and password must all be text"
    url, username, password = values
    if not username.strip() or not password or not url.strip():
        return "address, account name and password are all needed"
    if max(len(url), len(username), len(password)) > MAX_LOGIN_FIELD:
        return "address, account name or password is too long"
    try:
        return Credentials(username.strip(), password, clean_url(url))
    except CredentialError as exc:
        return str(exc)


def make_server(src: Sources, port: int, *, assets: Path = ASSETS_DIR, doctor=_health_checks, launch=start_run,
                verify=verify_login, layout: Layout | None = None, clock=datetime.now) -> GuiServer:
    return GuiServer(port, src, assets, doctor, launch, verify, layout or default_layout(), clock)


def serve(src: Sources, port: int = DEFAULT_PORT, open_browser: bool = True) -> int:
    try:
        server = make_server(src, port)
    except OSError:  # the system's own message is localised and can print garbled, so say it plainly
        print(f"error: cannot listen on 127.0.0.1:{port}: it is probably in use by another `videotag gui`; try --port 0")
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"videotag dashboard: {url}  (Ctrl+C to stop; runs started from it keep going)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
