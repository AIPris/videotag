"""Working with a library video's frames: download once to a temporary file, cut frames, clean up."""
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .faces import Face, FaceEngine, crop_jpeg
from .frames import sample_frames
from .matcher import Sighting
from .synology import PhotosClient


def frame_faces_of(engine: FaceEngine, video: Path, fps: float) -> list[list[Face]]:
    """The faces of every sampled frame of a local video (one inner list per frame)."""
    with tempfile.TemporaryDirectory() as tmp:
        return [engine.faces(frame) for frame in sample_frames(video, Path(tmp), fps)]


@contextmanager
def video_frames(client: PhotosClient, video: dict, fps: float) -> Iterator[list[Path]]:
    """The sampled frames of a library video. The download and the frames are deleted afterwards.

    Sampling is deterministic, so frame `n` is the same frame every time the video is opened this way.
    """
    with tempfile.TemporaryDirectory() as tmp:
        local = client.download(video["id"], Path(tmp) / f"video{Path(video['filename']).suffix.lower()}")
        frames_dir = Path(tmp) / "frames"
        frames_dir.mkdir()
        yield sample_frames(local, frames_dir, fps)


def frame_crop(client: PhotosClient, video: dict, fps: float, sighting: Sighting) -> bytes:
    """The face of a sighting as a JPEG."""
    with video_frames(client, video, fps) as frames:
        return crop_jpeg(frames[sighting.frame_index], sighting.box)
