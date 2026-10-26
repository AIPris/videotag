"""Commands that look after the installation rather than the library: credentials and a health check."""
import getpass
import importlib.util
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import __version__, config, settings
from .accounts import Identity, apply_login, verify_login
from .config import HOME, MODELS_DIR
from .credentials import (
    CredentialError,
    Credentials,
    clean_url,
    delete_credentials,
    load_credentials,
    parse_legacy_file,
    vault_name,
)
from .settings import SettingsError
from .state import State, StateError
from .synology import PhotosClient, SynologyError

# -- credentials ------------------------------------------------------------------------------

def _current_identity() -> Identity | None:
    try:
        return Identity.of(load_credentials()[0])
    except CredentialError:
        return None


def _use_login(credentials: Credentials) -> None:
    """Check the login, then use it. For another account this sets the saved data of the old one aside."""
    verify_login(credentials)  # a wrong address or password is caught before anything is stored or moved
    switched = apply_login(credentials, current=_current_identity())
    if switched.parked:
        print(f"The saved data of {switched.previous.label} was set aside. It comes back when you switch to that account again.")
    if switched.restored:
        print(f"The saved data of {Identity.of(credentials).label} came back.")


def cmd_credentials_set(client, args) -> None:
    if not sys.stdin.isatty():
        raise CredentialError("run this in an interactive terminal: the password is typed in, never passed as an argument")
    credentials = Credentials(input("Account name: ").strip(), getpass.getpass("Password (not shown): "),
                              clean_url(input("NAS address (https://...): ")))
    _use_login(credentials)
    print(f"Login works and is stored for this Windows user in the credential store ({vault_name()}).")


def cmd_credentials_status(client, args) -> None:
    try:
        credentials, source = load_credentials()
    except CredentialError as exc:
        print(f"no login available: {exc}")
        return
    print(f"source:   {source}" + (f" ({vault_name()})" if source == "credential store" else ""))
    print(f"address:  {credentials.base_url}")
    print(f"account:  {credentials.username}")
    print("password: set (never shown)")


def cmd_credentials_delete(client, args) -> None:
    print("deleted from the credential store" if delete_credentials() else "nothing was stored")


def cmd_credentials_migrate(client, args) -> None:
    """Move the login from the old plaintext file into the credential store, then (optionally) delete the file."""
    path = Path(args.file)
    credentials = parse_legacy_file(path.read_text(encoding="utf-8-sig"))
    _use_login(credentials)
    stored, _ = load_credentials()
    verify_login(stored)  # the login that was just stored has to work when read back from the store
    print(f"Moved the login from {path.name} into the credential store ({vault_name()}); logging in with it works.")
    if args.delete:
        path.unlink()
        print(f"Deleted {path.name}.")
    else:
        print(f"{path.name} still holds the password in plain text: delete it (or rerun with --delete).")


# -- doctor -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Check:
    name: str
    status: str   # "ok", "warn" or "fail"
    detail: str


def _check_install() -> Check:
    return Check("install", "ok", f"videotag {__version__}, Python {sys.version.split()[0]}, data in {HOME}")


def _check_data_home() -> Check:
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=HOME):
            pass
    except OSError as exc:
        return Check("data folder", "fail", f"{HOME} is not writable ({type(exc).__name__})")
    free_gb = shutil.disk_usage(HOME).free / 1e9
    return Check("data folder", "ok" if free_gb > 20 else "warn", f"{HOME}, {free_gb:.0f} GB free")


def _check_ffmpeg() -> Check:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return Check("ffmpeg", "fail", "not found on PATH (a scheduled task may have a shorter PATH than your terminal)")
    version = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True).stdout.splitlines()[0]
    return Check("ffmpeg", "ok", version)


def _check_credentials() -> tuple[Check, Credentials | None]:
    try:
        credentials, source = load_credentials()
    except CredentialError as exc:
        return Check("credentials", "fail", str(exc)), None
    return Check("credentials", "ok", f"{source}: {credentials.username} at {credentials.base_url}"), credentials


def _check_nas(credentials: Credentials) -> Check:
    try:
        with PhotosClient(credentials) as client:
            items = client.items()
            people = [person for person in client.persons() if person.get("name")]
    except SynologyError as exc:
        return Check("NAS login", "fail", str(exc))
    videos = sum(item["type"] == "video" for item in items)
    return Check("NAS login", "ok", f"{videos} videos, {len(items) - videos} photos, {len(people)} named people")


def _check_face_models() -> Check:
    missing = [name for name in ("face_detection_yunet_2023mar.onnx", "face_recognition_sface_2021dec.onnx")
               if not (MODELS_DIR / name).exists()]
    if missing:
        return Check("face models", "warn", "will be downloaded on the first run: " + ", ".join(missing))
    return Check("face models", "ok", f"present in {MODELS_DIR}")


def _check_description_model() -> Check:
    if importlib.util.find_spec("torch") is None or importlib.util.find_spec("transformers") is None:
        return Check("descriptions", "warn", "torch/transformers are not installed: runs need --no-describe")
    import torch
    from huggingface_hub import try_to_load_from_cache

    from .describe import MODEL_ID
    if not torch.cuda.is_available():
        return Check("descriptions", "fail", "PyTorch cannot see a CUDA GPU: runs need --no-describe")
    gpu = f"{torch.cuda.get_device_name(0)}, {torch.cuda.get_device_properties(0).total_memory / 1e9:.0f} GB"
    cached = isinstance(try_to_load_from_cache(MODEL_ID, "config.json"), str)
    if cached:
        return Check("descriptions", "ok", f"{gpu}; {MODEL_ID} is in the local cache")
    return Check("descriptions", "warn", f"{gpu}; {MODEL_ID} (about 9 GB) will be downloaded by the first description run")


def _check_state() -> Check:
    try:
        state = State.load()
    except StateError as exc:
        return Check("state file", "fail", str(exc))
    return Check("state file", "ok", f"{len(state.written)} tags, {len(state.faces)} face links, "
                                     f"{len(state.descriptions)} descriptions, "
                                     f"{'an' if state.review_album is not None else 'no'} album on record")


def _check_settings() -> Check:
    try:
        chosen = settings.load()
    except SettingsError as exc:
        return Check("settings", "fail", str(exc))
    where = "saved" if config.SETTINGS_FILE.exists() else "no settings file, the defaults"
    return Check("settings", "ok", f"{where}: preset {chosen.preset}, descriptions {'on' if chosen.describe else 'off'}")


def _check_task() -> Check:
    if sys.platform != "win32":
        return Check("scheduled task", "warn", "not checked on this platform")
    result = subprocess.run(["schtasks", "/Query", "/TN", "videotag"], capture_output=True, text=True)
    return Check("scheduled task", "ok" if result.returncode == 0 else "warn",
                 "registered" if result.returncode == 0 else "not registered (see docs/howto.md)")


def run_checks() -> list[Check]:
    checks = [_check_install(), _check_data_home(), _check_ffmpeg()]
    credentials_check, credentials = _check_credentials()
    checks.append(credentials_check)
    if credentials is not None:
        checks.append(_check_nas(credentials))
    checks += [_check_face_models(), _check_description_model(), _check_state(), _check_settings(), _check_task()]
    return checks


def cmd_doctor(client, args) -> int:
    marks = {"ok": "[ ok ]", "warn": "[warn]", "fail": "[FAIL]"}
    checks = run_checks()
    for check in checks:
        print(f"{marks[check.status]} {check.name:<15} {check.detail}")
    failed = sum(check.status == "fail" for check in checks)
    print("everything needed for an unattended run is in place" if not failed else f"{failed} problem(s) to fix first")
    return 1 if failed else 0
