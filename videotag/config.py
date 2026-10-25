"""Where the tool keeps its files. Code and data are separate: everything it writes lives in one data home.

Default: `%LOCALAPPDATA%\\videotag` on Windows, `$XDG_DATA_HOME/videotag` (or `~/.local/share/videotag`) elsewhere.
Set `VIDEOTAG_HOME` to put it somewhere else, for example a mounted volume in a container.
"""
import os
from pathlib import Path


def _data_home() -> Path:
    override = os.environ.get("VIDEOTAG_HOME")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "videotag"


HOME = _data_home()
CACHE_DIR = HOME / "cache"      # downloaded reference photos and cover crops
MODELS_DIR = HOME / "models"    # face detection and recognition models
STATE_DIR = HOME / "state"      # what the tool wrote and what the user did about it, plus per-video caches
REPORTS_DIR = HOME / "reports"  # one JSON report per run
LOG_DIR = HOME / "logs"
