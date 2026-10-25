"""Per-person thresholds, plus controls that show whether the matcher can be trusted (no NAS writes)."""
import tempfile
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .faces import FaceEngine
from .library import fetch
from .matcher import DEFAULT_MATCH, PersonMatch, PersonModel, face_similarity, match_video
from .references import ReferenceSet
from .scanner import frame_faces_of
from .synology import PhotosClient

MIN_SOURCES_FOR_AUTO = 3   # fewer distinct reference faces than this can only produce `Name?` suggestions
STRANGER_MARGIN = 0.05     # a person's threshold sits this far above the best-scoring stranger
HELD_OUT = 3               # single-face photos used for the control video instead of as references
VIDEO_SIZE = (480, 872)    # same shape as the generated clips
SLIDE_SECONDS = 2
VIDEO_FPS = 30


@dataclass
class Calibration:
    name: str
    positives: list[float]      # each reference face scored against the person's other references
    strangers: list[float]      # every other named person's reference faces scored against this person
    threshold: float
    can_auto: bool
    video_control: PersonMatch | None  # None when too few single-face photos to hold some out


def leave_one_out(embeddings: np.ndarray) -> list[float]:
    return [face_similarity(face, np.delete(embeddings, i, axis=0)) for i, face in enumerate(embeddings)]


def stranger_scores(refs: ReferenceSet, others: list[ReferenceSet]) -> list[float]:
    """Other named people are known to be someone else, which makes their faces clean negatives."""
    return [face_similarity(face, refs.embeddings) for other in others for face in other.embeddings]


def choose_threshold(strangers: list[float]) -> float:
    return max(DEFAULT_MATCH, max(strangers) + STRANGER_MARGIN) if strangers else DEFAULT_MATCH


def _others(refs: ReferenceSet, sets: list[ReferenceSet]) -> list[ReferenceSet]:
    return [other for other in sets if other.person_id != refs.person_id]


def person_models(sets: list[ReferenceSet], threshold: float | None = None) -> list[PersonModel]:
    """One model per person; `threshold` overrides the calibrated per-person value (for experiments)."""
    return [PersonModel(refs.person_id, refs.name, refs.embeddings,
                        threshold if threshold is not None else choose_threshold(stranger_scores(refs, _others(refs, sets))),
                        can_auto=refs.size >= MIN_SOURCES_FOR_AUTO)
            for refs in sets]


def _letterbox(image: np.ndarray) -> np.ndarray:
    width, height = VIDEO_SIZE
    scale = min(width / image.shape[1], height / image.shape[0])
    resized = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    canvas = np.zeros((height, width, 3), np.uint8)
    top, left = (height - resized.shape[0]) // 2, (width - resized.shape[1]) // 2
    canvas[top:top + resized.shape[0], left:left + resized.shape[1]] = resized
    return canvas


def write_slideshow(photos: list[Path], dest: Path) -> None:
    writer = cv2.VideoWriter(str(dest), cv2.VideoWriter_fourcc(*"mp4v"), VIDEO_FPS, VIDEO_SIZE)
    for photo in photos:
        slide = _letterbox(cv2.imread(str(photo)))
        for _ in range(SLIDE_SECONDS * VIDEO_FPS):
            writer.write(slide)
    writer.release()


def video_control(client: PhotosClient, engine: FaceEngine, refs: ReferenceSet, model: PersonModel,
                  photos_by_id: dict[int, dict], fps: float) -> PersonMatch | None:
    """Run the full video path on a local slideshow of held-out photos of the person (never uploaded)."""
    singles = [i for i, source in enumerate(refs.sources) if source.kind == "single"][:HELD_OUT]
    if len(singles) < HELD_OUT or refs.size < HELD_OUT + 2:
        return None
    kept = np.delete(refs.embeddings, singles, axis=0)
    with tempfile.TemporaryDirectory() as tmp:
        video = Path(tmp) / "control.mp4"
        write_slideshow([fetch(client, photos_by_id[refs.sources[i].photo_id]) for i in singles], video)
        frame_faces = frame_faces_of(engine, video, fps)
    held_out_model = PersonModel(model.person_id, model.name, kept, model.threshold)
    return match_video(frame_faces, [held_out_model])[0]


def calibrate_person(client: PhotosClient, engine: FaceEngine, refs: ReferenceSet, sets: list[ReferenceSet],
                     photos_by_id: dict[int, dict], fps: float) -> Calibration:
    (model,) = [m for m in person_models(sets) if m.person_id == refs.person_id]
    strangers = stranger_scores(refs, _others(refs, sets))
    return Calibration(refs.name, leave_one_out(refs.embeddings), strangers, model.threshold, model.can_auto,
                       video_control(client, engine, refs, model, photos_by_id, fps))
