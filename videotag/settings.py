"""The saved settings: the standing choices that every run uses unless a command-line flag says otherwise.

One file in the data home serves the terminal, the dashboard and a scheduled task. It holds the sampling preset and
whether videos are described, and nothing else. It never holds how a run writes (dry or apply, the write cap) or what a
run covers (a limit, a slice, an album), so a saved file cannot switch on writing to Photos or quietly shorten a
scheduled run. A file that holds such a choice, or anything else it does not know, is refused rather than ignored.
"""
import json
import os
from dataclasses import asdict, dataclass

from . import config
from .sampling import DEFAULT_PRESET, PRESETS

PER_RUN_CHOICES = ("apply", "mode", "max_writes", "limit", "new_only", "album", "album_id", "reanalyse")


class SettingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    preset: str = DEFAULT_PRESET
    describe: bool = True


def _check(key: str, value, name: str) -> None:
    if key in PER_RUN_CHOICES:
        raise SettingsError(f"{name}: '{key}' is a choice for one run, not a setting. This file never holds how a run writes "
                            "or what it covers, so that it cannot switch on writing to Photos or shorten a scheduled run; "
                            "remove it")
    if key == "preset" and not (isinstance(value, str) and value in PRESETS):
        raise SettingsError(f"{name}: unknown preset {value!r}; choose one of: {', '.join(PRESETS)}")
    if key == "describe" and not isinstance(value, bool):
        raise SettingsError(f"{name}: 'describe' must be true or false")
    if key not in Settings.__dataclass_fields__:
        raise SettingsError(f"{name}: unknown setting '{key}'; the settings are: {', '.join(Settings.__dataclass_fields__)}")


def load(path=None) -> Settings:
    """A missing file means the defaults. An unreadable one stops the run: guessing could change what a run does."""
    path = path or config.SETTINGS_FILE
    if not path.exists():
        return Settings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))  # editors on Windows may add a BOM
    except (OSError, ValueError):
        raise SettingsError(f"{path.name} is unreadable; fix or delete it ({path})") from None
    if not isinstance(raw, dict):
        raise SettingsError(f"{path.name} must hold a JSON object ({path})")
    return from_dict(raw, path.name)


def from_dict(raw: dict, name: str = "settings") -> Settings:
    """Settings from a dictionary, refusing per-run choices, unknown names and values that make no sense."""
    for key, value in raw.items():
        _check(key, value, name)
    return Settings(**raw)


def save(chosen: Settings, path=None) -> None:
    path = path or config.SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + ".part")
    scratch.write_text(json.dumps(asdict(chosen), indent=2), encoding="utf-8")
    os.replace(scratch, path)  # a run that starts meanwhile reads the old file or the new one, never half of one


def resolve(saved: Settings, preset: str | None, describe: bool | None) -> Settings:
    """The settings of one run: a flag given on the command line wins over the file."""
    return Settings(saved.preset if preset is None else preset, saved.describe if describe is None else describe)
