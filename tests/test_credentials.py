import keyring
import pytest
from keyring.backends import fail

from videotag import admin
from videotag.credentials import (
    SERVICE,
    CredentialError,
    Credentials,
    clean_url,
    delete_credentials,
    from_environment,
    load_credentials,
    parse_legacy_file,
    store_credentials,
)

LOGIN = Credentials("svc-account", "s3cret-pass", "https://nas.example.com")


def set_environment(monkeypatch, url="https://nas.example.com", user="env-user", password="env-pass"):
    monkeypatch.setenv("VIDEOTAG_URL", url)
    monkeypatch.setenv("VIDEOTAG_USER", user)
    monkeypatch.setenv("VIDEOTAG_PASSWORD", password)


# -- the safety net itself --------------------------------------------------------------------

def test_the_tests_run_against_an_in_memory_vault_and_never_the_real_one():
    assert type(keyring.get_keyring()).__name__ == "MemoryKeyring"


# -- sources ----------------------------------------------------------------------------------

def test_the_vault_round_trip_gives_the_same_login_and_says_where_it_came_from():
    store_credentials(LOGIN)
    assert load_credentials() == (LOGIN, "credential store")


def test_the_environment_wins_over_the_vault(monkeypatch):
    store_credentials(LOGIN)
    set_environment(monkeypatch)
    credentials, source = load_credentials()
    assert (credentials.username, source) == ("env-user", "environment")


def test_a_secret_file_can_hold_a_value_like_a_docker_secret(monkeypatch, tmp_path):
    secret = tmp_path / "password"
    secret.write_text("file-pass\n", encoding="utf-8")
    set_environment(monkeypatch)
    monkeypatch.delenv("VIDEOTAG_PASSWORD")
    monkeypatch.setenv("VIDEOTAG_PASSWORD_FILE", str(secret))
    assert from_environment().password == "file-pass"


def test_an_unreadable_secret_file_is_reported_without_its_path_contents(monkeypatch, tmp_path):
    set_environment(monkeypatch)
    monkeypatch.delenv("VIDEOTAG_PASSWORD")
    monkeypatch.setenv("VIDEOTAG_PASSWORD_FILE", str(tmp_path / "missing"))
    with pytest.raises(CredentialError, match="cannot be read"):
        from_environment()


def test_a_half_filled_environment_is_an_error_that_names_only_the_missing_variables(monkeypatch):
    monkeypatch.setenv("VIDEOTAG_USER", "someone")
    monkeypatch.setenv("VIDEOTAG_PASSWORD", "hunter2")
    with pytest.raises(CredentialError) as raised:
        from_environment()
    assert "VIDEOTAG_URL" in str(raised.value) and "hunter2" not in str(raised.value)


def test_no_login_anywhere_says_how_to_set_one():
    with pytest.raises(CredentialError, match="credentials set"):
        load_credentials()


def test_an_unreadable_vault_entry_is_an_error_not_a_crash(isolated_secrets):
    isolated_secrets.store[(SERVICE, "nas")] = "not json"
    with pytest.raises(CredentialError, match="unreadable"):
        load_credentials()


def test_a_machine_without_a_secure_store_is_refused_instead_of_silently_falling_back():
    keyring.set_keyring(fail.Keyring())
    with pytest.raises(CredentialError, match="no secure credential store"):
        store_credentials(LOGIN)


def test_deleting_removes_the_login_and_reports_whether_there_was_one():
    store_credentials(LOGIN)
    assert delete_credentials() is True and delete_credentials() is False


# -- address and secrecy ----------------------------------------------------------------------

def test_only_https_addresses_are_accepted():
    assert clean_url(" https://nas.example.com/ ") == "https://nas.example.com"
    with pytest.raises(CredentialError, match="https://"):
        clean_url("http://nas.example.com")
    with pytest.raises(CredentialError, match="look like"):
        clean_url("nas.example.com")


def test_plain_http_needs_an_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("VIDEOTAG_ALLOW_INSECURE_HTTP", "1")
    assert clean_url("http://192.0.2.10:5000") == "http://192.0.2.10:5000"


def test_the_password_never_appears_in_the_repr():
    assert "s3cret-pass" not in repr(LOGIN)


def test_the_legacy_file_format_still_parses_for_migration():
    legacy = parse_legacy_file("Username: svc-account\nPasswort: pa:ss:word\nDomain: https://nas.example.com/\n")
    assert (legacy.username, legacy.password, legacy.base_url) == ("svc-account", "pa:ss:word", "https://nas.example.com")
    assert parse_legacy_file("username: a\npassword: b\ndomain: https://x").password == "b"
    with pytest.raises(CredentialError, match="domain"):
        parse_legacy_file("Username: a\nPasswort: b")


# -- the commands -----------------------------------------------------------------------------

def test_status_shows_account_and_address_but_never_the_password(capsys):
    store_credentials(LOGIN)
    admin.cmd_credentials_status(None, None)
    shown = capsys.readouterr().out
    assert "svc-account" in shown and "https://nas.example.com" in shown and "s3cret-pass" not in shown


def test_setting_a_login_needs_a_terminal(monkeypatch):
    monkeypatch.setattr("sys.stdin", type("NotATerminal", (), {"isatty": lambda self: False})())
    with pytest.raises(CredentialError, match="interactive terminal"):
        admin.cmd_credentials_set(None, None)


def test_setting_a_login_verifies_it_before_storing_it(monkeypatch):
    monkeypatch.setattr("sys.stdin", type("Terminal", (), {"isatty": lambda self: True})())
    answers = iter(["svc-account", "https://nas.example.com"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "s3cret-pass")
    monkeypatch.setattr(admin, "verify_login", lambda credentials: (_ for _ in ()).throw(CredentialError("wrong login")))
    with pytest.raises(CredentialError, match="wrong login"):
        admin.cmd_credentials_set(None, None)
    assert delete_credentials() is False  # nothing was stored


def test_migrating_moves_the_login_verifies_it_and_deletes_the_plaintext_file(monkeypatch, tmp_path, capsys):
    legacy = tmp_path / "creds.txt"
    legacy.write_text("Username: svc-account\nPasswort: old-secret\nDomain: https://nas.example.com\n", encoding="utf-8")
    monkeypatch.setattr(admin, "verify_login", lambda credentials: None)
    admin.cmd_credentials_migrate(None, type("Args", (), {"file": str(legacy), "delete": True})())
    assert load_credentials()[0].password == "old-secret" and not legacy.exists()
    assert "old-secret" not in capsys.readouterr().out


def test_migrating_keeps_the_file_when_the_login_does_not_work(monkeypatch, tmp_path):
    legacy = tmp_path / "creds.txt"
    legacy.write_text("Username: svc-account\nPasswort: old-secret\nDomain: https://nas.example.com\n", encoding="utf-8")
    monkeypatch.setattr(admin, "verify_login", lambda credentials: (_ for _ in ()).throw(CredentialError("wrong login")))
    with pytest.raises(CredentialError):
        admin.cmd_credentials_migrate(None, type("Args", (), {"file": str(legacy), "delete": True})())
    assert legacy.exists() and delete_credentials() is False


# -- doctor -----------------------------------------------------------------------------------

def test_doctor_fails_with_exit_code_1_when_a_check_fails(monkeypatch, capsys):
    monkeypatch.setattr(admin, "run_checks", lambda: [admin.Check("a", "ok", "fine"), admin.Check("b", "fail", "broken")])
    assert admin.cmd_doctor(None, None) == 1
    assert "[FAIL] b" in capsys.readouterr().out


def test_doctor_warnings_do_not_fail_the_run(monkeypatch):
    monkeypatch.setattr(admin, "run_checks", lambda: [admin.Check("a", "warn", "later")])
    assert admin.cmd_doctor(None, None) == 0


def test_the_credentials_check_reads_the_secret_like_a_scheduled_task_would():
    check, credentials = admin._check_credentials()
    assert check.status == "fail" and credentials is None
    store_credentials(LOGIN)
    check, credentials = admin._check_credentials()
    assert check.status == "ok" and credentials == LOGIN and "s3cret-pass" not in check.detail


def test_the_settings_check_says_what_every_run_will_use_and_fails_for_a_file_that_would_stop_a_run():
    from videotag import config, settings

    check = admin._check_settings()
    assert check.status == "ok" and "thorough" in check.detail and "no settings file" in check.detail
    settings.save(settings.Settings("fast", False))
    check = admin._check_settings()
    assert check.status == "ok" and "fast" in check.detail and "descriptions off" in check.detail
    config.SETTINGS_FILE.write_text('{"apply": true}', encoding="utf-8")
    check = admin._check_settings()
    assert check.status == "fail" and "'apply' is a choice for one run" in check.detail
