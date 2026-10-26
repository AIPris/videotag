"""The faces found in each photo of a named person, kept on disk.

Building the reference faces reads every photo of every named person, which took 28 minutes on the first real library.
The faces of a photo do not change unless the photo does, so they are kept per photo and reused whatever else changes:
a new person, a new photo or a changed one only costs its own photos. An entry is used only for the same file (size and
time) and the same face models.
"""
import os
import zipfile

import numpy as np

from .config import STATE_DIR
from .faces import MODEL_VERSION

REFS_DIR = STATE_DIR / "reffaces"
EMBEDDING_SIZE = 128
LAYOUT = "v1-embeddings"  # bumped when the stored fields change, which makes older entries get rebuilt


def _key(photo: dict) -> str:
    return f"{LAYOUT}|{MODEL_VERSION}|{photo['filesize']}|{photo['time']}"


def load(photo: dict) -> list[np.ndarray] | None:
    """The embeddings of the faces in the photo (none at all is a valid answer), or None when they have to be found."""
    path = REFS_DIR / f"{photo['id']}.npz"
    try:
        with np.load(path) as data:
            if str(data["key"]) != _key(photo):
                return None
            return list(data["embeddings"])
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):  # missing, or half written by a run that was killed
        return None


def save(photo: dict, embeddings: list[np.ndarray]) -> None:
    REFS_DIR.mkdir(parents=True, exist_ok=True)
    stacked = np.stack(embeddings).astype(np.float32) if embeddings else np.empty((0, EMBEDDING_SIZE), np.float32)
    path = REFS_DIR / f"{photo['id']}.npz"
    scratch = path.with_name(path.name + ".part")
    with scratch.open("wb") as out:
        np.savez_compressed(out, key=_key(photo), embeddings=stacked)
    os.replace(scratch, path)
