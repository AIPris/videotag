"""Which Synology account the data on disk belongs to, and switching between accounts.

Everything the tool remembers (state, caches, reports, log) describes one account's library: item ids, tag ids, face
ids. Replaying it against another account would read every tag that is missing there as "removed by the user" and
remember that for good, would reuse the other account's cached photos as reference faces, and `undo` would delete
items by ids that mean something else. So each account has its own copy of those folders: the active account's sit
at the usual place, the others are set aside under `accounts/<account>/` and come back when you switch to them.

Accounts are compared exactly (address and name as stored). Telling two spellings of one account apart is
recoverable, because the data comes back when you switch to the other spelling; merging two accounts is not.
"""
import contextlib
import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .config import CACHE_DIR, HOME, LOG_DIR, REPORTS_DIR, STATE_DIR
from .credentials import CredentialError, Credentials, load_credentials, store_credentials, vault_name
from .lock import LOCK_FILE, STALE_AFTER_SECONDS
from .synology import PhotosClient, SynologyError

OWNER_FILE = "account.json"
NOT_DATA = {OWNER_FILE, LOCK_FILE.name}  # what a folder may hold and still count as empty
LOGIN_PROBLEMS = {
    400: "the NAS rejected this account name or password",
    401: "this account is disabled",
    402: "this account is not allowed to log in",
    403: "this account needs a 2-step verification code, which videotag cannot enter",
    404: "2-step verification failed",
}


class AccountError(RuntimeError):
    """A problem with accounts. Messages never contain a password."""


class AccountBusy(AccountError):
    pass


class AccountMismatch(AccountError):
    pass


@dataclass(frozen=True)
class Identity:
    url: str
    username: str

    @classmethod
    def of(cls, credentials: Credentials) -> "Identity":
        return cls(credentials.base_url, credentials.username)

    @property
    def host(self) -> str:
        return self.url.split("://", 1)[-1]

    @property
    def label(self) -> str:
        return f"{self.username} at {self.host}"

    @property
    def slug(self) -> str:
        """A folder name that is safe on every file system and different for every exact (address, name) pair."""
        readable = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{self.username}@{self.host}").lstrip(".")[:60]
        exact = f"{self.url}\n{self.username}"
        return f"{readable}-{hashlib.sha256(exact.encode()).hexdigest()[:10]}"

    def as_dict(self) -> dict:
        return {"url": self.url, "username": self.username, "label": self.label}


@dataclass(frozen=True)
class Layout:
    state: Path = STATE_DIR
    reports: Path = REPORTS_DIR
    cache: Path = CACHE_DIR
    logs: Path = LOG_DIR
    parked: Path = HOME / "accounts"

    @property
    def folders(self) -> dict[str, Path]:
        return {"state": self.state, "reports": self.reports, "cache": self.cache, "logs": self.logs}

    @property
    def owner_file(self) -> Path:
        return self.state / OWNER_FILE

    @property
    def lock_file(self) -> Path:
        return self.state / LOCK_FILE.name


def default_layout() -> Layout:
    """Where the real data lives. The tests replace this, so that no test can ever move the real data."""
    return Layout()


@dataclass(frozen=True)
class Switch:
    previous: Identity | None
    parked: bool     # the old account's data was set aside
    restored: bool   # the new account's earlier data came back


# -- who owns the data ---------------------------------------------------------------------------

def read_owner(layout: Layout) -> Identity | None:
    try:
        data = json.loads(layout.owner_file.read_text(encoding="utf-8"))
        return Identity(data["url"], data["username"])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        raise AccountError(f"{layout.owner_file.name} in the state folder is unreadable; "
                           "it says which account the saved data belongs to") from None


def _write_owner(layout: Layout, identity: Identity) -> None:
    layout.state.mkdir(parents=True, exist_ok=True)
    layout.owner_file.write_text(json.dumps({"url": identity.url, "username": identity.username}), encoding="utf-8")


def claim_data(credentials: Credentials, layout: Layout | None = None) -> None:
    """Before a command logs in: the saved data must belong to this login. Data without an owner is claimed."""
    layout = layout or default_layout()
    identity, owner = Identity.of(credentials), read_owner(layout)
    if owner is None:
        _write_owner(layout, identity)
    elif owner != identity:
        raise AccountMismatch(
            f"the saved data belongs to {owner.label}, but the login is for {identity.label}. Mixing them would "
            "corrupt both. Switch accounts from the dashboard (`videotag gui`) or with `videotag credentials set`, "
            "which sets the other account's data aside and brings it back when you switch back")


# -- switching the data --------------------------------------------------------------------------

def is_busy(layout: Layout) -> bool:
    try:
        return time.time() - layout.lock_file.stat().st_mtime < STALE_AFTER_SECONDS
    except FileNotFoundError:
        return False


def _has_data(folder: Path) -> bool:
    return folder.is_dir() and any(entry.name not in NOT_DATA for entry in folder.iterdir())


def _move(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)


def _move_all(moves: list[tuple[Path, Path]]) -> None:
    """All of them, or none: a move that fails puts the earlier ones back."""
    done = []
    try:
        for source, destination in moves:
            _move(source, destination)
            done.append((source, destination))
    except OSError as exc:
        for source, destination in reversed(done):
            _move(destination, source)
        raise AccountError(f"could not move the saved data ({type(exc).__name__}); close any program that is using "
                           "the videotag data folder (Explorer, a virus scanner, a run) and try again") from None


def switch_data(layout: Layout, target: Identity, current: Identity | None = None) -> Switch:
    """Make `target` the account the saved data belongs to. `current` is the login used so far: it owns old data
    that has no owner file yet. Nothing moves when the target already owns the data (a new password only)."""
    if is_busy(layout):
        raise AccountBusy("a run is in progress; switch accounts when it has finished")
    owner = read_owner(layout)
    holds_data = any(_has_data(folder) for folder in layout.folders.values())
    if owner is None and holds_data:
        owner = current
        if owner is None:
            raise AccountError("the saved data has no owner recorded and no login is stored to say whose it is; "
                               "log in with the account it belongs to first")
    if owner == target:
        _write_owner(layout, target)
        return Switch(owner, False, False)

    aside = layout.parked / owner.slug if owner else None
    if holds_data and aside.is_dir() and any(aside.iterdir()):
        raise AccountError(f"{owner.label} already has data set aside; remove {aside} first")
    source = layout.parked / target.slug
    moves = []
    if holds_data:
        moves += [(folder, aside / name) for name, folder in layout.folders.items() if folder.exists()]
    else:
        for folder in layout.folders.values():  # nothing worth keeping: only an owner file or a lock is left
            shutil.rmtree(folder, ignore_errors=True)
    restoring = source.is_dir()
    if restoring:
        moves += [(source / name, folder) for name, folder in layout.folders.items() if (source / name).exists()]
    if holds_data and owner is not None and not layout.owner_file.exists():
        _write_owner(layout, owner)  # so the copy set aside knows whose it is
    _move_all(moves)
    if restoring:
        shutil.rmtree(source, ignore_errors=True)
    if not layout.owner_file.exists() or read_owner(layout) != target:
        _write_owner(layout, target)
    return Switch(owner, holds_data, restoring)


def parked_accounts(layout: Layout) -> list[Identity]:
    found = []
    for folder in sorted(layout.parked.iterdir()) if layout.parked.is_dir() else []:
        try:
            data = json.loads((folder / "state" / OWNER_FILE).read_text(encoding="utf-8"))
            found.append(Identity(data["url"], data["username"]))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return found


# -- the login -----------------------------------------------------------------------------------

def verify_login(credentials: Credentials) -> None:
    """Log in once and out again, so a wrong address or password is caught before anything is stored or moved."""
    try:
        with PhotosClient(credentials):
            pass
    except SynologyError as exc:
        raise AccountError(LOGIN_PROBLEMS.get(exc.code, str(exc))) from None


def apply_login(credentials: Credentials, current: Identity | None = None, layout: Layout | None = None,
                store=store_credentials) -> Switch:
    """Use this login from now on: check the credential store works, switch the data, then store the login.

    The caller has verified the login. A new password for the same account moves nothing."""
    layout = layout or default_layout()
    vault_name()  # raises when there is no secure credential store, before anything has moved
    switched = switch_data(layout, Identity.of(credentials), current)
    try:
        store(credentials)
    except Exception:  # noqa: BLE001 - the credential store raises its own errors, and whatever it raises, the data goes back
        if switched.previous is not None and (switched.parked or switched.restored):
            with contextlib.suppress(AccountError):  # best effort: runs refuse mismatched data anyway
                switch_data(layout, switched.previous)
        raise
    return switched


def describe_account(layout: Layout | None = None) -> dict:
    """What the dashboard shows about the account. Never the password."""
    layout = layout or default_layout()
    view = {"current": None, "source": None, "locked": False, "problem": None, "owner": None, "mismatch": False,
            "parked": [identity.as_dict() for identity in parked_accounts(layout)]}
    current = owner = None
    try:
        credentials, source = load_credentials()
        current = Identity.of(credentials)
        view.update(current=current.as_dict(), source=source, locked=source == "environment")
    except CredentialError as exc:
        view["problem"] = str(exc)
    try:
        owner = read_owner(layout)
    except AccountError as exc:
        view["problem"] = str(exc)
    if owner:
        view.update(owner=owner.as_dict(), mismatch=current is not None and owner != current)
    return view
