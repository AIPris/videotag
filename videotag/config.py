"""Where the tool keeps its files. Code and data are separate: everything it writes lives in one data home.

Default: `%LOCALAPPDATA%\\videotag` on Windows, `$XDG_DATA_HOME/videotag` (or `~/.local/share/videotag`) elsewhere.
Set `VIDEOTAG_HOME` to put it somewhere else, for example a mounted volume in a container.
"""
import os
import sys
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
EVIDENCE_DIR = STATE_DIR / "evidence"  # the best few face crops of each match, so the dashboard can show why (never uploaded)
LOG_DIR = HOME / "logs"
SETTINGS_FILE = HOME / "settings.json"  # standing choices, used by the terminal, the dashboard and scheduled runs alike
LOG_FILE = LOG_DIR / "videotag.log"
LOG_MAX_BYTES = 1_000_000  # the log rotates at this size ...
LOG_BACKUPS = 3            # ... and keeps this many older files (videotag.log.1 is the newest of them)

DEFAULT_MAX_WRITES = 100  # most changes of each kind a run writes unless told otherwise
NO_LIMIT = sys.maxsize  # `--max-writes all`: more than any library holds, so the write loops need no special case
DEFAULT_PORT = 8765  # where `videotag gui` listens on 127.0.0.1
