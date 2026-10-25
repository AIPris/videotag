"""Per-video descriptions kept on disk, so a video is only described again when it or the prompt/model changed.

An unusable answer is cached as "tried, nothing": describing is deterministic, so trying again would only repeat it.
"""
import json

from .config import STATE_DIR
from .describe import MODEL_ID, PROMPT_VERSION, Description

DESCRIBE_DIR = STATE_DIR / "describe"


def cache_key(video: dict) -> str:
    return f"{PROMPT_VERSION}|{MODEL_ID}|{video['filesize']}|{video['time']}"


def load(video_id: int, key: str) -> tuple[bool, Description | None]:
    """(found, description). `found` is False when the video has to be described."""
    path = DESCRIBE_DIR / f"{video_id}.json"
    if not path.exists():
        return False, None
    entry = json.loads(path.read_text(encoding="utf-8"))
    if entry["key"] != key:
        return False, None
    return True, Description.from_dict(entry["description"]) if entry["description"] else None


def save(video_id: int, key: str, description: Description | None) -> None:
    DESCRIBE_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"key": key, "description": description.to_dict() if description else None}
    (DESCRIBE_DIR / f"{video_id}.json").write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
