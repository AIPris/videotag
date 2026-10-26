"""Working with a library video's frames: download once to a temporary file, cut frames, clean up."""
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .faces import Face, FaceEngine, crop_jpeg
from .frames import sample_frames
from .matcher import Sighting
from .sampling import Sampling
from .synology import PhotosClient


def frame_faces_of(engine: FaceEngine, video: Path, sampling: Sampling) -> list[list[Face]]:
    """The faces of every sampled frame of a local video (one inner list per frame)."""
    with tempfile.TemporaryDirectory() as tmp:
        return [engine.faces(frame) for frame in sample_frames(video, Path(tmp), sampling)]


@contextmanager
def video_frames(client: PhotosClient, video: dict, sampling: Sampling) -> Iterator[list[Path]]:
    """The sampled frames of a library video. The download and the frames are deleted afterwards.

    Sampling is deterministic, so frame `n` is the same frame every time the video is opened with the same rule.
    """
    with tempfile.TemporaryDirectory() as tmp:
        local = client.download(video["id"], Path(tmp) / f"video{Path(video['filename']).suffix.lower()}")
        frames_dir = Path(tmp) / "frames"
        frames_dir.mkdir()
        yield sample_frames(local, frames_dir, sampling)


def frame_crop(client: PhotosClient, video: dict, sampling: Sampling, sighting: Sighting) -> bytes:
    """The face of a sighting as a JPEG. `sampling` must be the rule the sighting's frame index was counted under."""
    with video_frames(client, video, sampling) as frames:
        return crop_jpeg(frames[sighting.frame_index], sighting.box)
