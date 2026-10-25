"""Face detection and embedding with OpenCV's YuNet (detector) and SFace (recognizer)."""
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import requests

from .config import MODELS_DIR

DETECTOR_MODEL = MODELS_DIR / "face_detection_yunet_2023mar.onnx"
RECOGNIZER_MODEL = MODELS_DIR / "face_recognition_sface_2021dec.onnx"
MODEL_SOURCES = {
    DETECTOR_MODEL: "face_detection_yunet",
    RECOGNIZER_MODEL: "face_recognition_sface",
}
MODEL_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/{folder}/{name}"
MODEL_VERSION = "yunet-2023mar+sface-2021dec"  # stored with cached embeddings so they are redone if the models change


def ensure_models() -> None:
    for path, folder in MODEL_SOURCES.items():
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        response = requests.get(MODEL_URL.format(folder=folder, name=path.name), timeout=120)
        response.raise_for_status()
        path.write_bytes(response.content)


Box = tuple[float, float, float, float]  # x1, y1, x2, y2 as fractions of the image width and height


@dataclass(frozen=True)
class Face:
    embedding: np.ndarray  # L2-normalised
    area: float            # bounding box area in pixels of the (possibly downscaled) image
    box: Box               # where the face sits, independent of any downscaling


def _box(row: np.ndarray, width: int, height: int) -> Box:
    x, y, w, h = (float(value) for value in row[:4])
    clip = lambda value: min(max(value, 0.0), 1.0)  # noqa: E731
    return clip(x / width), clip(y / height), clip((x + w) / width), clip((y + h) / height)


def crop_jpeg(image_path: Path, box: Box, margin: float = 0.4) -> bytes:
    """The face as a JPEG, with some margin around the detected box (like a People-page thumbnail)."""
    image = cv2.imread(str(image_path))
    height, width = image.shape[:2]
    x1, y1, x2, y2 = box
    pad_x, pad_y = (x2 - x1) * margin, (y2 - y1) * margin
    left, right = int(max(x1 - pad_x, 0) * width), int(min(x2 + pad_x, 1) * width)
    top, bottom = int(max(y1 - pad_y, 0) * height), int(min(y2 + pad_y, 1) * height)
    ok, encoded = cv2.imencode(".jpg", image[top:bottom, left:right])
    if not ok:
        raise ValueError("could not encode the face crop")
    return encoded.tobytes()


class FaceEngine:
    def __init__(self, min_score: float = 0.8, max_side: int = 1280):
        ensure_models()
        self._detector = cv2.FaceDetectorYN.create(str(DETECTOR_MODEL), "", (320, 320), min_score)
        self._recognizer = cv2.FaceRecognizerSF.create(str(RECOGNIZER_MODEL), "")
        self._max_side = max_side

    def faces(self, image_path: Path) -> list[Face]:
        """Every face found in the image (EXIF rotation is applied)."""
        image = cv2.imread(str(image_path))
        if image is None:
            return []
        image = self._downscale(image)
        height, width = image.shape[:2]
        self._detector.setInputSize((width, height))
        _, rows = self._detector.detect(image)
        if rows is None:
            return []
        return [Face(self._embed(image, row), float(row[2] * row[3]), _box(row, width, height)) for row in rows]

    def embeddings(self, image_path: Path) -> list[np.ndarray]:
        return [face.embedding for face in self.faces(image_path)]

    def main_face(self, image_path: Path) -> np.ndarray | None:
        """The embedding of the largest face, for crops that may show a neighbour's edge too."""
        faces = self.faces(image_path)
        return max(faces, key=lambda face: face.area).embedding if faces else None

    def _embed(self, image: np.ndarray, row: np.ndarray) -> np.ndarray:
        feature = self._recognizer.feature(self._recognizer.alignCrop(image, row)).flatten()
        return feature / np.linalg.norm(feature)

    def _downscale(self, image: np.ndarray) -> np.ndarray:
        scale = self._max_side / max(image.shape[:2])
        if scale >= 1:
            return image
        return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
