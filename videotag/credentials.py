"""Where the NAS login comes from. The password never goes into the project, a log or a report.

Order of lookup:
1. Environment: `VIDEOTAG_URL`, `VIDEOTAG_USER`, `VIDEOTAG_PASSWORD`. Each can instead be given as
   `<NAME>_FILE`, the path of a file holding the value (a Docker or Kubernetes secret).
2. The operating system's credential store through `keyring` (Windows Credential Manager, macOS Keychain,
   Secret Service), where `videotag credentials set` puts it. It is encrypted for the logged-in user.

There is deliberately no plaintext file fallback. The login is sent over HTTPS only, unless
`VIDEOTAG_ALLOW_INSECURE_HTTP=1` is set for a trusted test network.
"""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

SERVICE = "videotag"
ACCOUNT = "nas"
ENV_NAMES = {"url": "VIDEOTAG_URL", "username": "VIDEOTAG_USER", "password": "VIDEOTAG_PASSWORD"}
HOW_TO_SET = ("run `videotag credentials set` in a terminal, "
              "or set the VIDEOTAG_URL, VIDEOTAG_USER and VIDEOTAG_PASSWORD variables")


class CredentialError(RuntimeError):
    """A problem with the stored login. Messages never contain the password."""


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str = field(repr=False)
    base_url: str


def clean_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url.startswith("https://") or (url.startswith("http://") and os.environ.get("VIDEOTAG_ALLOW_INSECURE_HTTP") == "1"):
        return url
    if url.startswith("http://"):
        raise CredentialError("the NAS address must start with https:// (the login would travel unencrypted)")
    raise CredentialError("the NAS address must look like https://nas.example.com")


def _from_value_or_file(name: str) -> str | None:
    value = os.environ.get(name)
    if value is not None:
        return value
    path = os.environ.get(f"{name}_FILE")
    if path is None:
        return None
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CredentialError(f"{name}_FILE points at a file that cannot be read ({type(exc).__name__})") from None


def from_environment() -> Credentials | None:
    values = {key: _from_value_or_file(name) for key, name in ENV_NAMES.items()}
    if all(value is None for value in values.values()):
        return None
    missing = [ENV_NAMES[key] for key, value in values.items() if not value]
    if missing:
        raise CredentialError("incomplete login in the environment, missing: " + ", ".join(missing))
    return Credentials(values["username"], values["password"], clean_url(values["url"]))


def _vault():
    """The `keyring` module, after making sure it has a real secure backend and not a silent fallback."""
    try:
        import keyring
    except ImportError:
        raise CredentialError("the 'keyring' package is not installed") from None
    backend = keyring.get_keyring()
    if backend.priority <= 0:
        raise CredentialError("this machine has no secure credential store; use the VIDEOTAG_* environment variables "
                              "or secret files instead")
    return keyring


def vault_name() -> str:
    backend = _vault().get_keyring()
    return f"{type(backend).__module__}.{type(backend).__name__}"


def from_vault() -> Credentials | None:
    blob = _vault().get_password(SERVICE, ACCOUNT)
    if blob is None:
        return None
    try:
        data = json.loads(blob)
        return Credentials(data["username"], data["password"], clean_url(data["url"]))
    except (ValueError, KeyError, TypeError):
        raise CredentialError("the stored login is unreadable; run `videotag credentials set` again") from None


def load_credentials() -> tuple[Credentials, str]:
    """The login and where it came from ("environment" or "credential store")."""
    for source, loader in (("environment", from_environment), ("credential store", from_vault)):
        credentials = loader()
        if credentials is not None:
            return credentials, source
    raise CredentialError("no NAS login found: " + HOW_TO_SET)


def store_credentials(credentials: Credentials) -> None:
    _vault().set_password(SERVICE, ACCOUNT, json.dumps(
        {"url": credentials.base_url, "username": credentials.username, "password": credentials.password}))


def delete_credentials() -> bool:
    keyring = _vault()
    if keyring.get_password(SERVICE, ACCOUNT) is None:
        return False
    keyring.delete_password(SERVICE, ACCOUNT)
    return True


def parse_legacy_file(text: str) -> Credentials:
    """The old plaintext `creds.txt` (`Username:`, `Passwort:`/`Password:`, `Domain:`), only used to migrate from it."""
    fields = {}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip().lower()] = value.strip()
    try:
        password = fields.get("passwort") or fields["password"]
        return Credentials(fields["username"], password, clean_url(fields["domain"]))
    except KeyError as missing:
        raise CredentialError(f"the credentials file has no {missing} entry") from None
