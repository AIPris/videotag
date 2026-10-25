"""Per-video faces (embedding and position) kept on disk.

Matching is cheap, downloading and detecting is not, so a changed reference set (or threshold) never
needs the videos again. An entry is reused only for the same file, sampling rate and face models.
"""
import numpy as np

from .config import STATE_DIR
from .faces import MODEL_VERSION, Face

FACES_DIR = STATE_DIR / "faces"
EMBEDDING_SIZE = 128
LAYOUT = "v2-boxes"  # bumped when the stored fields change, which makes older entries get rebuilt


def cache_key(video: dict, fps: float) -> str:
    return f"{LAYOUT}|{MODEL_VERSION}|{video['filesize']}|{video['time']}|{fps}"


def load(video_id: int, key: str) -> list[list[Face]] | None:
    path = FACES_DIR / f"{video_id}.npz"
    if not path.exists():
        return None
    with np.load(path) as data:
        if str(data["key"]) != key:
            return None
        counts, embeddings, boxes = data["counts"], data["embeddings"], data["boxes"]
    if counts.size == 0:
        return []
    faces = [Face(embedding, float((box[2] - box[0]) * (box[3] - box[1])), tuple(float(v) for v in box))
             for embedding, box in zip(embeddings, boxes, strict=True)]
    bounds = np.concatenate([[0], np.cumsum(counts)])
    return [faces[start:end] for start, end in zip(bounds[:-1], bounds[1:], strict=True)]


def save(video_id: int, key: str, frame_faces: list[list[Face]]) -> None:
    FACES_DIR.mkdir(parents=True, exist_ok=True)
    counts = np.array([len(faces) for faces in frame_faces], dtype=np.int32)
    flat = [face for faces in frame_faces for face in faces]
    embeddings = np.stack([f.embedding for f in flat]).astype(np.float32) if flat else np.empty((0, EMBEDDING_SIZE), np.float32)
    boxes = np.array([f.box for f in flat], dtype=np.float32).reshape(-1, 4)
    np.savez_compressed(FACES_DIR / f"{video_id}.npz", key=key, counts=counts, embeddings=embeddings, boxes=boxes)
