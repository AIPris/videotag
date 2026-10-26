"""The faces and the description of every video, in one pass over the library.

A video that is not cached yet needs two kinds of work that use different hardware: fetching it, cutting frames and
reading the faces (network and CPU), and describing it with the vision-language model (GPU). A helper thread does the
first for the next videos while the calling thread runs the model on the current one. The helper keeps only the frames the
model needs, so running ahead costs next to nothing on disk.

While a pass is running only the helper uses `client` and `engine`, and only the calling thread uses the model. Results
are the same as handling one video at a time, and they come back in library order.

A file that sits in the library under two ids (same name, size and time) is a twin pair: it is fetched and described once,
and the other id gets a copy of the result, saved under its own id too so that it counts as analysed from then on.
"""
import logging
import queue
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path

from . import descache, facecache
from .describe import FRAMES, Describer, Description, evenly, evenly_in_time
from .faces import Face, FaceEngine
from .sampling import Sampling
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
    sampling: Sampling | None = None  # how the frames behind `faces` were cut: a frame index means nothing without it
    twin_of: int | None = None        # the id of the video whose result this one shares: the same file under another id


@dataclass
class _Prepared:
    video: dict
    faces: list[list[Face]] | None = None
    description: Description | None = None
    sampling: Sampling | None = None
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


def _model_frames(frames: list[Path], sampling: Sampling) -> list[Path]:
    """The frames the model looks at, spread over the whole clip in time whatever the rule."""
    if sampling.uniform:
        return evenly(frames, FRAMES)
    return evenly_in_time(frames, sampling.times(len(frames)), FRAMES)


def _keep_evidence(evidence, video: dict, frames: list[Path], faces: list[list[Face]]) -> None:
    """The crops are a convenience: failing to make them must never cost the video its results."""
    try:
        evidence.keep(video, frames, faces)
    except Exception as exc:  # noqa: BLE001
        log.warning("evidence of %s could not be kept: %s: %s", video["id"], type(exc).__name__, exc)


def _prepare(client: PhotosClient, engine: FaceEngine, describer: Describer | None, video: dict, sampling: Sampling,
             reanalyse: bool, evidence=None) -> _Prepared:
    """Everything about one video that does not need the model, from the caches where possible.

    The video is downloaded at most once, and only if something is missing from the caches: faces, a description, or the
    crops of the faces that made a match (`evidence`). A saved result is used whatever rule made it, unless `reanalyse`
    asks for the ones made under another rule to be redone. The video is cut once, under the rule of its faces, so that a
    frame number means the same frame to the crops and to the model.
    """
    saved = facecache.load(video["id"], video)
    if reanalyse and saved is not None and saved.sampling != sampling:
        saved = None
    described, description = descache.load(video["id"], descache.cache_key(video)) if describer else (True, None)
    prepared = _Prepared(video, saved.faces if saved else None, description, saved.sampling if saved else None)
    needs_crops = evidence is not None and saved is not None and evidence.missing(video, saved.faces)
    if saved is not None and described and not needs_crops:
        return prepared
    rule = saved.sampling if saved is not None else sampling
    try:
        with video_frames(client, video, rule) as frames:
            if saved is None:
                prepared.faces, prepared.sampling = [engine.faces(frame) for frame in frames], sampling
                facecache.save(video["id"], video, prepared.faces, sampling)
            if evidence is not None and (saved is None or needs_crops):
                _keep_evidence(evidence, video, frames, prepared.faces)
            if not described:
                prepared.folder = tempfile.TemporaryDirectory()
                prepared.frames = [Path(shutil.copy2(frame, prepared.folder.name)) for frame in _model_frames(frames, rule)]
    except SKIPPABLE as exc:
        prepared.discard()
        if saved is not None and described:
            # the video was only fetched for its crops: the saved results stand, whatever happened to the download
            log.warning("evidence of %s could not be made, the video could not be fetched or cut: %s", video["id"], exc)
            return _Prepared(video, saved.faces, description, saved.sampling)
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
        return Outcome(prepared.video, prepared.faces, description, sampling=prepared.sampling)
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


def _twin_key(video: dict) -> tuple:
    return video["filename"], video["filesize"], video["time"]


def _first_of_each(videos: list[dict]) -> tuple[list[dict], dict[int, int]]:
    """The videos to analyse (the first of each set of twins) and, for every later twin, the id of its first."""
    firsts, first_with, original_of = [], {}, {}
    for video in videos:
        original = first_with.setdefault(_twin_key(video), video["id"])
        if original == video["id"]:
            firsts.append(video)
        else:
            original_of[video["id"]] = original
    return firsts, original_of


def _copy_to_twin(original: Outcome, twin: dict) -> Outcome:
    """The twin's own result: the original's, saved under the twin's id so the twin never needs fetching on its own."""
    if original.error is None:
        facecache.save(twin["id"], twin, original.faces, original.sampling)
        if original.description is not None:
            descache.save(twin["id"], descache.cache_key(twin), original.description)
    return replace(original, video=twin, twin_of=original.video["id"])


def is_analysed(video: dict, describe: bool) -> bool:
    """Whether the video already has its saved results. The sampling rule that made them does not matter."""
    if facecache.load(video["id"], video) is None:
        return False
    return not describe or descache.load(video["id"], descache.cache_key(video))[0]


def analyse_videos(client: PhotosClient, engine: FaceEngine, describer: Describer | None, videos: Iterable[dict],
                   sampling: Sampling, prefetch: int = PREFETCH, reanalyse: bool = False, evidence=None) -> Iterator[Outcome]:
    """One Outcome per video, in order. A video that cannot be fetched or cut comes back with its error and the pass
    goes on; any other error is raised when its turn comes. Close the iterator to stop early (`contextlib.closing`)."""
    videos = list(videos)
    firsts, original_of = _first_of_each(videos)
    twinned = set(original_of.values())
    helper = _Helper(lambda video: _prepare(client, engine, describer, video, sampling, reanalyse, evidence), firsts, prefetch)
    helper.start()
    outcomes_with_twins: dict[int, Outcome] = {}
    try:
        for video in videos:
            if video["id"] in original_of:
                yield _copy_to_twin(outcomes_with_twins[original_of[video["id"]]], video)
                continue
            item = helper.get()
            if isinstance(item, _Failed):
                raise item.error
            outcome = _finish(describer, item)
            if video["id"] in twinned:
                outcomes_with_twins[video["id"]] = outcome
            yield outcome
    finally:
        helper.close()
