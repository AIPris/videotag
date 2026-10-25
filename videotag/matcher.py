"""Pure scoring: turn per-frame face embeddings into a per-person decision."""
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

TOP_K = 3
DEFAULT_MATCH = 0.363  # OpenCV's documented SFace cosine threshold; per-person values are calibrated
DEFAULT_MIN_FRAMES = 2


class Decision(StrEnum):
    AUTO = "auto"      # enough frames match and the references are solid: safe to tag
    REVIEW = "review"  # some frames match, or the references are thin: a human should confirm
    NONE = "none"


@dataclass(frozen=True)
class PersonModel:
    person_id: int
    name: str
    references: np.ndarray  # one L2-normalised embedding per row
    threshold: float
    can_auto: bool = True   # False while the references are too thin to trust a confident tag


@dataclass(frozen=True)
class PersonMatch:
    person_id: int
    name: str
    best: float | None  # highest per-frame similarity, None if no face was seen at all
    hit_frames: int
    decision: Decision


def face_similarity(face: np.ndarray, references: np.ndarray) -> float:
    """Mean of the top-k cosine similarities to the reference faces (all L2-normalised)."""
    return float(np.sort(references @ face)[-TOP_K:].mean())


def decide(hit_frames: int, min_frames: int, can_auto: bool) -> Decision:
    if hit_frames >= min_frames and can_auto:
        return Decision.AUTO
    return Decision.REVIEW if hit_frames else Decision.NONE


@dataclass(frozen=True)
class Sighting:
    frame_index: int
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 as fractions of the frame
    similarity: float


def best_sighting(frame_faces: list[list], model: PersonModel) -> Sighting | None:
    """Where the person is seen best: the frame and face most similar to their references."""
    best = None
    for index, faces in enumerate(frame_faces):
        for face in faces:
            similarity = face_similarity(face.embedding, model.references)
            if best is None or similarity > best.similarity:
                best = Sighting(index, face.box, similarity)
    return best


def match_video(frame_faces: list[list], models: list[PersonModel],
                min_frames: int = DEFAULT_MIN_FRAMES) -> list[PersonMatch]:
    """Score one video against every known person.

    `frame_faces` holds the faces of each sampled frame (anything with an `.embedding`). A face only counts
    for the one person it resembles most, and only if that similarity reaches the person's threshold; a
    frame is a hit for a person when any of its faces counts for them. `best` is the highest similarity to
    the person over all faces, whoever the face was claimed by.
    """
    best: list[float | None] = [None] * len(models)
    hits = [0] * len(models)
    for faces in frame_faces:
        frame_hits = set()
        for face in faces:
            similarities = [face_similarity(face.embedding, model.references) for model in models]
            for index, similarity in enumerate(similarities):
                best[index] = similarity if best[index] is None else max(best[index], similarity)
            winner = int(np.argmax(similarities))
            if similarities[winner] >= models[winner].threshold:
                frame_hits.add(winner)
        for index in frame_hits:
            hits[index] += 1
    return [PersonMatch(model.person_id, model.name, best[index], hits[index],
                        decide(hits[index], min_frames, model.can_auto))
            for index, model in enumerate(models)]
