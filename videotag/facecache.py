"""Per-video faces (embedding and position) kept on disk.

Matching is cheap, downloading and detecting is not, so a changed reference set (or threshold) never
needs the videos again. A result belongs to one file (size and time) and the face models, and records the sampling rule
that made it. It is reused whatever rule is in use now: changing the preset only affects videos without a result.

Results made before presets existed carry the sampling rate at the end of their key instead of a stored rule. They are
still read, as the uniform rule of that rate.
"""
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import STATE_DIR
from .faces import MODEL_VERSION, Face
from .sampling import Sampling

FACES_DIR = STATE_DIR / "faces"
EMBEDDING_SIZE = 128
LAYOUT = "v2-boxes"  # bumped when the stored fields change, which makes older entries get rebuilt


@dataclass(frozen=True)
class Result:
    faces: list[list[Face]]  # one inner list per frame
    sampling: Sampling       # how the frames were cut, so a frame can be found again


@dataclass(frozen=True)
class Summary:
    video_id: int
    sampling: Sampling
    frames: int


def file_key(video: dict) -> str:
    return f"{LAYOUT}|{MODEL_VERSION}|{video['filesize']}|{video['time']}"


def _sampling_of(stored_key: str, data, key: str) -> Sampling | None:
    """The rule of an entry for this file, or None when the entry is not one for this file or its rule is unreadable."""
    try:
        if stored_key == key:
            return Sampling.parse(str(data["sampling"]))
        if stored_key.startswith(key + "|"):  # made before presets existed: the rate follows the file in the key
            return Sampling.parse(stored_key[len(key) + 1:])
    except (KeyError, ValueError):
        return None
    return None


def load(video_id: int, video: dict) -> Result | None:
    path = FACES_DIR / f"{video_id}.npz"
    if not path.exists():
        return None
    key = file_key(video)
    with np.load(path) as data:
        sampling = _sampling_of(str(data["key"]), data, key)
        if sampling is None:
            return None
        counts, embeddings, boxes = data["counts"], data["embeddings"], data["boxes"]
    if counts.size == 0:
        return Result([], sampling)
    faces = [Face(embedding, float((box[2] - box[0]) * (box[3] - box[1])), tuple(float(v) for v in box))
             for embedding, box in zip(embeddings, boxes, strict=True)]
    bounds = np.concatenate([[0], np.cumsum(counts)])
    return Result([faces[start:end] for start, end in zip(bounds[:-1], bounds[1:], strict=True)], sampling)


def summaries(folder: Path | None = None) -> list[Summary]:
    """The rule and the number of frames of every usable result, without loading the faces. Results made for other face
    models (which would be rebuilt anyway) and files that cannot be read are left out."""
    found = []
    for path in sorted((folder or FACES_DIR).glob("*.npz")):
        try:
            with np.load(path) as data:
                layout, models, _size, _time, *old_rate = str(data["key"]).split("|")
                if (layout, models) != (LAYOUT, MODEL_VERSION):
                    continue
                rule = old_rate[0] if old_rate else str(data["sampling"])
                found.append(Summary(int(path.stem), Sampling.parse(rule), int(data["counts"].size)))
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            continue
    return found


def save(video_id: int, video: dict, frame_faces: list[list[Face]], sampling: Sampling) -> None:
    FACES_DIR.mkdir(parents=True, exist_ok=True)
    counts = np.array([len(faces) for faces in frame_faces], dtype=np.int32)
    flat = [face for faces in frame_faces for face in faces]
    embeddings = np.stack([f.embedding for f in flat]).astype(np.float32) if flat else np.empty((0, EMBEDDING_SIZE), np.float32)
    boxes = np.array([f.box for f in flat], dtype=np.float32).reshape(-1, 4)
    np.savez_compressed(FACES_DIR / f"{video_id}.npz", key=file_key(video), sampling=sampling.spec, counts=counts,
                        embeddings=embeddings, boxes=boxes)
