"""The faces and the description of every video, in one pass over the library.

A video that is not cached yet needs two kinds of work that use different hardware: fetching it, cutting frames and
reading the faces (network and CPU), and describing it with the vision-language model (GPU). A helper thread does the
first for the next videos while the calling thread runs the model on the current one. The helper keeps only the frames the
model needs, so running ahead costs next to nothing on disk.

While a pass is running only the helper uses `client` and `engine`, and only the calling thread uses the model. Results
are the same as handling one video at a time, and they come back in library order.
"""
import logging
import queue
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from . import descache, facecache
from .describe import FRAMES, Describer, Description, evenly
from .faces import Face, FaceEngine
from .scanner import video_frames
from .synology import PhotosClient, SynologyError

log = logging.getLogger("videotag")

PREFETCH = 8         # videos prepared ahead of the one being described
POLL_SECONDS = 0.2   # how often a waiting thread looks at the stop flag, so Ctrl-C and closing are never stuck for long
SKIPPABLE = (SynologyError, RuntimeError, subprocess.CalledProcessError)  # a video that cannot be fetched or cut is skipped


@dataclass(frozen=True)
class Outcome:
    video: dict
    faces: list[list[Face]] | None = None
    description: Description | None = None
    error: Exception | None = None


@dataclass
class _Prepared:
    video: dict
    faces: list[list[Face]] | None = None
    description: Description | None = None
    frames: list[Path] | None = None                   # what the model is to look at; None when it has nothing to do
    folder: tempfile.TemporaryDirectory | None = None  # holds `frames`
    error: Exception | None = None

    def discard(self) -> None:
        if self.folder is not None:
            self.folder.cleanup()
            self.folder = None


@dataclass(frozen=True)
class _Failed:
    error: Exception


_END = object()


def _describe(describer: Describer, video: dict, frames: list[Path]) -> Description | None:
    """One description, cached. A model or GPU failure is logged and not cached, so it is tried again next run."""
    key = descache.cache_key(video)
    try:
        description = describer.describe(evenly(frames, FRAMES))
    except Exception as exc:  # noqa: BLE001 - a broken model must not stop the people pipeline
        log.error("describing %s failed: %s: %s", video["id"], type(exc).__name__, exc)
        return None
    descache.save(video["id"], key, description)
    return description


def _prepare(client: PhotosClient, engine: FaceEngine, describer: Describer | None, video: dict, fps: float) -> _Prepared:
    """Everything about one video that does not need the model, from the caches where possible.

    The video is downloaded at most once, and only if something is missing from the caches.
    """
    face_key = facecache.cache_key(video, fps)
    faces = facecache.load(video["id"], face_key)
    described, description = descache.load(video["id"], descache.cache_key(video)) if describer else (True, None)
    prepared = _Prepared(video, faces, description)
    if faces is not None and described:
        return prepared
    try:
        with video_frames(client, video, fps) as frames:
            if faces is None:
                prepared.faces = [engine.faces(frame) for frame in frames]
                facecache.save(video["id"], face_key, prepared.faces)
            if not described:
                prepared.folder = tempfile.TemporaryDirectory()
                prepared.frames = [Path(shutil.copy2(frame, prepared.folder.name)) for frame in evenly(frames, FRAMES)]
    except SKIPPABLE as exc:
        prepared.discard()
        return _Prepared(video, error=exc)
    except BaseException:
        prepared.discard()
        raise
    return prepared


def _finish(describer: Describer | None, prepared: _Prepared) -> Outcome:
    """Describe the video, if it needs it. This runs on the calling thread, the only one that uses the model."""
    try:
        if prepared.error is not None:
            return Outcome(prepared.video, error=prepared.error)
        description = prepared.description
        if prepared.frames:
            description = _describe(describer, prepared.video, prepared.frames)
        return Outcome(prepared.video, prepared.faces, description)
    finally:
        prepared.discard()


class _Helper:
    """A thread that prepares the videos in order and hands them over through a short queue."""

    def __init__(self, prepare: Callable[[dict], _Prepared], videos: Iterable[dict], depth: int):
        self._prepare, self._videos = prepare, videos
        self._queue: queue.Queue = queue.Queue(maxsize=depth)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="videotag-prefetch", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def get(self):
        while True:
            try:
                return self._queue.get(timeout=POLL_SECONDS)
            except queue.Empty:
                continue

    def close(self) -> None:
        """Stop, wait for the video in hand, and throw away whatever was prepared but not used."""
        self._stop.set()
        self._thread.join()
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, _Prepared):
                item.discard()

    def _put(self, item) -> bool:
        while not self._stop.is_set():
            try:
                self._queue.put(item, timeout=POLL_SECONDS)
                return True
            except queue.Full:
                continue
        if isinstance(item, _Prepared):
            item.discard()
        return False

    def _run(self) -> None:
        try:
            for video in self._videos:
                if self._stop.is_set() or not self._put(self._prepare(video)):
                    return
            self._put(_END)
        except Exception as exc:  # noqa: BLE001 - raised again, in order, by the calling thread
            self._put(_Failed(exc))


def analyse_videos(client: PhotosClient, engine: FaceEngine, describer: Describer | None, videos: Iterable[dict],
                   fps: float, prefetch: int = PREFETCH) -> Iterator[Outcome]:
    """One Outcome per video, in order. A video that cannot be fetched or cut comes back with its error and the pass
    goes on; any other error is raised when its turn comes. Close the iterator to stop early (`contextlib.closing`)."""
    helper = _Helper(lambda video: _prepare(client, engine, describer, video, fps), videos, prefetch)
    helper.start()
    try:
        while (item := helper.get()) is not _END:
            if isinstance(item, _Failed):
                raise item.error
            yield _finish(describer, item)
    finally:
        helper.close()
