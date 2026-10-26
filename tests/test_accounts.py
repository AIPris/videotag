import json
import os
import time

import keyring
import pytest

from videotag import accounts
from videotag.accounts import (
    AccountBusy,
    AccountError,
    AccountMismatch,
    Identity,
    Layout,
    apply_login,
    claim_data,
    describe_account,
    parked_accounts,
    switch_data,
    verify_login,
)
from videotag.credentials import SERVICE, CredentialError, Credentials, load_credentials, store_credentials
from videotag.lock import STALE_AFTER_SECONDS
from videotag.synology import SynologyError

A = Identity("https://nas.example.com", "claude")
B = Identity("https://nas.example.com", "chris")
OTHER_NAS = Identity("https://other.example.com", "claude")


def credentials_of(identity: Identity, password="secret-pw") -> Credentials:
    return Credentials(identity.username, password, identity.url)


@pytest.fixture
def layout(tmp_path) -> Layout:
    return Layout(state=tmp_path / "state", reports=tmp_path / "reports", cache=tmp_path / "cache", logs=tmp_path / "logs",
                  parked=tmp_path / "accounts")


def put_data(layout: Layout, owner: Identity | None, marker: str = "data") -> None:
    """The kinds of files a real data home holds, each saying whose they are."""
    for folder, name in [(layout.state, "state.json"), (layout.state / "describe", "1.json"), (layout.reports, "run-1.json"),
                         (layout.cache / "photos", "1.jpg"), (layout.logs, "videotag.log")]:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(marker, encoding="utf-8")
    if owner:
        layout.owner_file.write_text(json.dumps({"url": owner.url, "username": owner.username}), encoding="utf-8")


def markers(layout: Layout) -> set[str]:
    files = [layout.state / "state.json", layout.state / "describe" / "1.json", layout.reports / "run-1.json",
             layout.cache / "photos" / "1.jpg", layout.logs / "videotag.log"]
    return {path.read_text(encoding="utf-8") for path in files if path.exists()}


def hold_lock(layout: Layout, age_seconds: float = 5) -> None:
    layout.state.mkdir(parents=True, exist_ok=True)
    layout.lock_file.write_text("", encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(layout.lock_file, (stamp, stamp))


# -- identity ----------------------------------------------------------------------------------

def test_an_identity_reads_as_who_at_where():
    assert A.label == "claude at nas.example.com"


def test_the_folder_name_of_an_identity_is_safe_and_never_shared_with_another_identity():
    nasty = Identity("https://nas.example.com:5001", "../../Windows\\x")
    assert not set(nasty.slug) & set("/\\:")
    assert len({A.slug, B.slug, OTHER_NAS.slug, Identity(A.url, "Claude").slug, Identity(A.url.upper(), "claude").slug}) == 5


# -- the ownership guard -----------------------------------------------------------------------

def test_data_without_an_owner_is_claimed_by_the_first_login_that_uses_it(layout):
    put_data(layout, owner=None)
    claim_data(credentials_of(A), layout)
    assert json.loads(layout.owner_file.read_text(encoding="utf-8")) == {"url": A.url, "username": "claude"}
    claim_data(credentials_of(A), layout)  # and the same login may carry on


def test_another_login_is_refused_and_both_accounts_are_named(layout):
    put_data(layout, owner=A)
    with pytest.raises(AccountMismatch, match="claude at nas.example.com.*chris at nas.example.com"):
        claim_data(credentials_of(B), layout)


@pytest.mark.parametrize("other", [Identity("https://nas.example.com", "Claude"), Identity("https://NAS.example.com", "claude"),
                                   Identity("https://nas.example.com:443", "claude")])
def test_accounts_are_compared_exactly_because_merging_two_is_the_dangerous_mistake(layout, other):
    put_data(layout, owner=A)
    with pytest.raises(AccountMismatch):
        claim_data(credentials_of(other), layout)


def test_an_unreadable_owner_file_stops_instead_of_guessing(layout):
    put_data(layout, owner=None)
    layout.owner_file.write_text("{ half", encoding="utf-8")
    with pytest.raises(AccountError, match="unreadable"):
        claim_data(credentials_of(A), layout)


# -- switching the data ------------------------------------------------------------------------

def test_switching_sets_the_old_data_aside_and_starts_the_new_account_empty(layout):
    put_data(layout, owner=A, marker="claude's")
    result = switch_data(layout, B)
    assert (result.previous, result.parked, result.restored) == (A, True, False)
    assert markers(layout) == set()  # the new account starts with nothing
    assert json.loads(layout.owner_file.read_text(encoding="utf-8"))["username"] == "chris"
    aside = layout.parked / A.slug
    assert (aside / "state" / "state.json").read_text(encoding="utf-8") == "claude's"
    assert (aside / "reports" / "run-1.json").exists() and (aside / "cache" / "photos" / "1.jpg").exists()
    assert (aside / "logs" / "videotag.log").exists() and (aside / "state" / "describe" / "1.json").exists()


def test_switching_back_restores_everything_and_sets_the_other_aside(layout):
    put_data(layout, owner=A, marker="claude's")
    switch_data(layout, B)
    put_data(layout, owner=B, marker="chris's")  # chris' own run happened meanwhile
    result = switch_data(layout, A)
    assert (result.previous, result.parked, result.restored) == (B, True, True)
    assert markers(layout) == {"claude's"}
    assert (layout.parked / B.slug / "state" / "state.json").read_text(encoding="utf-8") == "chris's"
    assert not (layout.parked / A.slug).exists()
    assert parked_accounts(layout) == [B]


def test_the_same_account_with_a_new_password_moves_nothing(layout):
    put_data(layout, owner=A, marker="mine")
    before = sorted(str(p) for p in layout.state.parent.rglob("*"))
    result = switch_data(layout, A)
    assert (result.previous, result.parked, result.restored) == (A, False, False)
    assert sorted(str(p) for p in layout.state.parent.rglob("*")) == before and markers(layout) == {"mine"}


def test_old_data_without_an_owner_file_belongs_to_the_login_used_so_far(layout):
    put_data(layout, owner=None, marker="legacy")
    result = switch_data(layout, B, current=A)
    assert result.previous == A and result.parked
    assert (layout.parked / A.slug / "state" / "state.json").read_text(encoding="utf-8") == "legacy"
    assert json.loads((layout.parked / A.slug / "state" / "account.json").read_text(encoding="utf-8"))["username"] == "claude"


def test_old_data_of_unknown_ownership_is_not_moved_on_a_guess(layout):
    put_data(layout, owner=None, marker="legacy")
    with pytest.raises(AccountError, match="no owner"):
        switch_data(layout, B, current=None)
    assert markers(layout) == {"legacy"}


def test_nothing_is_moved_while_a_run_holds_the_lock(layout):
    put_data(layout, owner=A, marker="mine")
    hold_lock(layout)
    with pytest.raises(AccountBusy, match="run is in progress"):
        switch_data(layout, B)
    assert markers(layout) == {"mine"} and not layout.parked.exists()


def test_a_leftover_lock_from_a_crashed_run_does_not_block_switching(layout):
    put_data(layout, owner=A)
    hold_lock(layout, age_seconds=STALE_AFTER_SECONDS + 60)
    assert switch_data(layout, B).parked


def test_a_failed_move_puts_everything_back_where_it_was(layout, monkeypatch):
    put_data(layout, owner=A, marker="mine")
    real_move, calls = accounts._move, []

    def second_move_fails(source, destination):
        calls.append(source)
        if len(calls) == 2:
            raise PermissionError("held by a virus scanner")
        real_move(source, destination)

    monkeypatch.setattr(accounts, "_move", second_move_fails)
    with pytest.raises(AccountError, match="close any program"):
        switch_data(layout, B)
    assert markers(layout) == {"mine"}
    assert json.loads(layout.owner_file.read_text(encoding="utf-8"))["username"] == "claude"
    assert not (layout.parked / A.slug).exists() or not any((layout.parked / A.slug).iterdir())


def test_data_already_set_aside_for_the_old_account_is_never_overwritten(layout):
    put_data(layout, owner=A, marker="mine")
    (layout.parked / A.slug / "state").mkdir(parents=True)
    (layout.parked / A.slug / "state" / "state.json").write_text("older", encoding="utf-8")
    with pytest.raises(AccountError, match="already has data set aside"):
        switch_data(layout, B)
    assert markers(layout) == {"mine"}
    assert (layout.parked / A.slug / "state" / "state.json").read_text(encoding="utf-8") == "older"


def test_parked_accounts_ignores_folders_that_are_not_accounts(layout):
    put_data(layout, owner=A)
    switch_data(layout, B)
    (layout.parked / "junk").mkdir()
    (layout.parked / "stray.txt").write_text("x", encoding="utf-8")
    assert parked_accounts(layout) == [A]


# -- using a login -----------------------------------------------------------------------------

def stored() -> Credentials:
    return load_credentials()[0]


def test_using_a_login_for_another_account_stores_it_and_switches_the_data(layout):
    put_data(layout, owner=A, marker="claude's")
    store_credentials(credentials_of(A, "old"))
    result = apply_login(credentials_of(B, "new-pw"), current=A, layout=layout)
    assert result.parked and stored().username == "chris" and stored().password == "new-pw"
    assert markers(layout) == set() and (layout.parked / A.slug / "state" / "state.json").exists()


def test_changing_only_the_password_keeps_the_data_where_it_is(layout):
    put_data(layout, owner=A, marker="mine")
    store_credentials(credentials_of(A, "old"))
    result = apply_login(credentials_of(A, "rotated"), current=A, layout=layout)
    assert not result.parked and not result.restored and stored().password == "rotated"
    assert markers(layout) == {"mine"} and not layout.parked.exists()


def test_without_a_secure_credential_store_nothing_is_moved(layout, monkeypatch):
    put_data(layout, owner=A, marker="mine")
    monkeypatch.setattr(accounts, "vault_name", lambda: (_ for _ in ()).throw(CredentialError("no secure credential store")))
    with pytest.raises(CredentialError):
        apply_login(credentials_of(B), current=A, layout=layout)
    assert markers(layout) == {"mine"} and not layout.parked.exists()


def test_a_login_that_cannot_be_stored_leaves_the_data_with_the_old_account(layout):
    put_data(layout, owner=A, marker="claude's")

    def refuse(credentials):
        raise CredentialError("the store said no")

    with pytest.raises(CredentialError, match="said no"):
        apply_login(credentials_of(B), current=A, layout=layout, store=refuse)
    assert markers(layout) == {"claude's"}
    assert json.loads(layout.owner_file.read_text(encoding="utf-8"))["username"] == "claude"


# -- what the dashboard shows ------------------------------------------------------------------

def test_the_account_view_shows_who_is_signed_in_and_never_the_password(layout):
    put_data(layout, owner=A)
    store_credentials(credentials_of(A, "very-secret"))
    view = describe_account(layout)
    assert view["current"] == {"url": A.url, "username": "claude", "label": A.label}
    assert view["source"] == "credential store" and view["locked"] is False and view["mismatch"] is False
    assert "very-secret" not in json.dumps(view)


def test_the_account_view_warns_when_the_data_belongs_to_someone_else(layout):
    put_data(layout, owner=A)
    store_credentials(credentials_of(B))
    view = describe_account(layout)
    assert view["mismatch"] is True and view["owner"]["label"] == A.label


def test_a_login_from_the_environment_is_shown_but_cannot_be_changed_here(layout, monkeypatch):
    monkeypatch.setenv("VIDEOTAG_URL", A.url)
    monkeypatch.setenv("VIDEOTAG_USER", "claude")
    monkeypatch.setenv("VIDEOTAG_PASSWORD", "from-env")
    view = describe_account(layout)
    assert view["source"] == "environment" and view["locked"] is True and "from-env" not in json.dumps(view)


def test_with_no_login_the_view_says_how_to_add_one(layout):
    view = describe_account(layout)
    assert view["current"] is None and "no NAS login" in view["problem"]


def test_set_aside_accounts_are_listed(layout):
    put_data(layout, owner=A)
    switch_data(layout, B)
    assert describe_account(layout)["parked"] == [{"url": A.url, "username": "claude", "label": A.label}]


def test_the_vault_entry_is_what_was_stored(layout, isolated_secrets):
    store_credentials(credentials_of(A, "pw"))
    assert json.loads(keyring.get_password(SERVICE, "nas"))["username"] == "claude"


# -- checking a login --------------------------------------------------------------------------

class FakeClient:
    """Stands in for PhotosClient: logging in either works or fails the way the NAS or the network does."""

    def __init__(self, error: SynologyError | None = None):
        self.error = error

    def __call__(self, credentials):
        return self

    def __enter__(self):
        if self.error:
            raise self.error
        return self

    def __exit__(self, *exc_info):
        return False


def test_a_login_the_nas_accepts_passes(monkeypatch):
    monkeypatch.setattr(accounts, "PhotosClient", FakeClient())
    verify_login(credentials_of(A))


@pytest.mark.parametrize(("code", "words"), [(400, "rejected this account name or password"), (401, "disabled"),
                                              (403, "2-step verification code")])
def test_a_login_the_nas_refuses_is_explained_in_plain_words(monkeypatch, code, words):
    monkeypatch.setattr(accounts, "PhotosClient", FakeClient(SynologyError("SYNO.API.Auth.login failed", code)))
    with pytest.raises(AccountError, match=words):
        verify_login(credentials_of(A))


def test_a_login_that_never_reached_the_nas_keeps_the_technical_reason(monkeypatch):
    monkeypatch.setattr(accounts, "PhotosClient", FakeClient(SynologyError("SYNO.API.Auth.login request failed (SSLError)")))
    with pytest.raises(AccountError, match="SSLError"):
        verify_login(credentials_of(A))


def test_the_tests_never_resolve_to_the_real_data_home():
    from videotag import config
    assert accounts.default_layout().state != config.STATE_DIR and accounts.default_layout().parked != config.HOME / "accounts"


def test_any_failure_to_store_the_login_returns_the_data_to_its_owner(layout):
    """A credential store raises its own errors (keyring's, the OS's), not only ours."""
    put_data(layout, owner=A, marker="claude's")

    def vault_breaks(credentials):
        raise RuntimeError("the vault service stopped")

    with pytest.raises(RuntimeError, match="vault service"):
        apply_login(credentials_of(B), current=A, layout=layout, store=vault_breaks)
    assert markers(layout) == {"claude's"}
    assert json.loads(layout.owner_file.read_text(encoding="utf-8"))["username"] == "claude"
