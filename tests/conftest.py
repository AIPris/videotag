import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from videotag.credentials import ENV_NAMES


class MemoryKeyring(KeyringBackend):
    """A credential store that lives in memory, so tests can never read or overwrite the real login."""

    priority = 1

    def __init__(self):
        super().__init__()
        self.store = {}

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def get_password(self, service, username):
        return self.store.get((service, username))

    def delete_password(self, service, username):
        if (service, username) not in self.store:
            raise PasswordDeleteError("not found")
        del self.store[(service, username)]


@pytest.fixture(autouse=True)
def isolated_secrets(monkeypatch):
    """Every test gets an empty in-memory vault and no login in the environment."""
    previous = keyring.get_keyring()
    memory = MemoryKeyring()
    keyring.set_keyring(memory)
    for name in (*ENV_NAMES.values(), *(f"{n}_FILE" for n in ENV_NAMES.values()), "VIDEOTAG_ALLOW_INSECURE_HTTP"):
        monkeypatch.delenv(name, raising=False)
    yield memory
    keyring.set_keyring(previous)
