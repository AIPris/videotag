import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pytest

from videotag import analysis, descache, facecache
from videotag.analysis import analyse_videos
from videotag.describe import FRAMES, Description, evenly
from videotag.faces import Face
from videotag.synology import SynologyError

HELPER = "videotag-prefetch"
FRAMES_PER_VIDEO = 20


def video(number):
    return {"id": number, "filename": f"clip{number}.mp4", "filesize": 100 + number, "time": 7}


class World:
    """Fakes for the NAS download, the face engine and the description model, with hooks to hold or break a video."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.prepared, self.faced, self.described = [], [], []   # video ids in the order they happened
        self.threads = {"download": set(), "faces": set(), "describe": set()}
        self.frame_names = {}                                      # what the describer received, by video id
        self.download_fails, self.engine_fails = {}, {}            # video id -> exception
        self.on_prepared, self.on_describe = {}, {}                # video id -> callable
        self.client, self.engine, self.describer = object(), self, self

    @contextmanager
    def video_frames(self, client, item, fps):
        self.threads["download"].add(threading.current_thread().name)
        self.prepared.append(item["id"])
        if item["id"] in self.download_fails:
            raise self.download_fails[item["id"]]
        if item["id"] in self.on_prepared:
            self.on_prepared[item["id"]]()
        with tempfile.TemporaryDirectory() as folder:
            frames = []
            for number in range(1, FRAMES_PER_VIDEO + 1):
                frame = Path(folder) / f"f{number:04d}.jpg"
                frame.write_text(str(item["id"]))
                frames.append(frame)
            yield frames

    def faces(self, frame):
        self.threads["faces"].add(threading.current_thread().name)
        number = int(frame.read_text())
        self.faced.append(number)
        if number in self.engine_fails:
            raise self.engine_fails[number]
        return [Face(np.array([number, int(frame.stem[1:])], dtype=float), 1.0, (0.0, 0.0, 1.0, 1.0))]

    def describe(self, frames):
        self.threads["describe"].add(threading.current_thread().name)
        number = int(frames[0].read_text())
        self.described.append(number)
        self.frame_names[number] = [frame.name for frame in frames]
        if number in self.on_describe:
            self.on_describe[number]()
        return Description(f"video {number}", f"Video {number}")


@pytest.fixture
def world(tmp_path, monkeypatch):
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    monkeypatch.setattr(facecache, "FACES_DIR", tmp_path / "faces")
    monkeypatch.setattr(descache, "DESCRIBE_DIR", tmp_path / "describe")
    fakes = World(tmp_path / "tmp")
    monkeypatch.setattr(analysis, "video_frames", fakes.video_frames)
    return fakes


@pytest.fixture
def folders(monkeypatch):
    """The temporary folders made during a test, and which of them were cleaned on purpose (not left to the garbage collector)."""
    class Recording(tempfile.TemporaryDirectory):
        made, cleaned = [], []

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            Recording.made.append(self.name)

        def cleanup(self):
            Recording.cleaned.append(self.name)
            super().cleanup()

    monkeypatch.setattr(tempfile, "TemporaryDirectory", Recording)
    return Recording


def analyse(world, numbers, describer=True, prefetch=8):
    return analyse_videos(world.client, world.engine, world.describer if describer else None,
                          [video(n) for n in numbers], 2.0, prefetch)


def with_a_full_queue(world, prefetch):
    """An iterator that has handed out its first outcome while the helper had filled the queue and held one more video."""
    filled = threading.Event()
    world.on_prepared[prefetch + 2] = filled.set
    world.on_describe[1] = lambda: filled.wait(5)
    outcomes = analyse(world, range(1, 12), prefetch=prefetch)
    next(outcomes)
    return outcomes


def same_faces(first, second):
    return len(first) == len(second) and all(
        len(a) == len(b) and all(np.array_equal(x.embedding, y.embedding) for x, y in zip(a, b, strict=True))
        for a, b in zip(first, second, strict=True))


# -- what comes out -----------------------------------------------------------------------------

def test_every_video_gets_its_faces_and_description_in_library_order_and_both_are_cached(world):
    outcomes = list(analyse(world, [3, 1, 2]))
    assert [o.video["id"] for o in outcomes] == [3, 1, 2]
    assert all(o.error is None and len(o.faces) == FRAMES_PER_VIDEO for o in outcomes)
    assert [o.description.summary_en for o in outcomes] == ["video 3", "video 1", "video 2"]
    for o in outcomes:
        assert same_faces(facecache.load(o.video["id"], facecache.cache_key(o.video, 2.0)), o.faces)
        assert descache.load(o.video["id"], descache.cache_key(o.video)) == (True, o.description)


def test_the_description_model_sees_the_frames_spread_evenly_over_the_whole_video(world):
    list(analyse(world, [1]))
    every_frame = [f"f{number:04d}.jpg" for number in range(1, FRAMES_PER_VIDEO + 1)]
    assert world.frame_names[1] == evenly(every_frame, FRAMES)


def test_a_cached_video_is_not_downloaded_again(world):
    first = list(analyse(world, [1]))
    world.prepared.clear()
    again = list(analyse(world, [1]))
    assert world.prepared == [] and again[0].description == first[0].description and same_faces(again[0].faces, first[0].faces)


def test_only_the_description_is_made_when_the_faces_are_cached(world):
    item = video(1)
    facecache.save(1, facecache.cache_key(item, 2.0), [[Face(np.array([1.0, 0.0]), 1.0, (0.0, 0.0, 1.0, 1.0))]])
    outcome = next(analyse(world, [1]))
    assert world.faced == [] and world.described == [1] and len(outcome.faces) == 1


def test_only_the_faces_are_made_when_the_description_is_cached(world):
    item = video(1)
    cached = Description("cached", "gespeichert")
    descache.save(1, descache.cache_key(item), cached)
    outcome = next(analyse(world, [1]))
    assert world.described == [] and len(world.faced) == FRAMES_PER_VIDEO and outcome.description == cached


def test_without_a_describer_only_the_faces_are_made(world):
    outcome = next(analyse(world, [1], describer=False))
    assert outcome.description is None and len(outcome.faces) == FRAMES_PER_VIDEO and world.described == []


# -- the overlap ----------------------------------------------------------------------------------

def test_the_next_video_is_prepared_while_the_current_one_is_described(world):
    second_ready, waited = threading.Event(), []
    world.on_prepared[2] = second_ready.set
    world.on_describe[1] = lambda: waited.append(second_ready.wait(5))  # only a pass that overlaps can ever see it set
    list(analyse(world, [1, 2]))
    assert waited == [True]


def test_it_runs_ahead_by_the_prefetch_limit_and_no_further(world):
    fourth, fifth, seen = threading.Event(), threading.Event(), {}
    world.on_prepared[4], world.on_prepared[5] = fourth.set, fifth.set
    world.on_describe[1] = lambda: seen.update(ahead=fourth.wait(5), too_far=fifth.wait(0.5))
    list(analyse(world, range(1, 9), prefetch=2))  # video 1 is being described, 2 and 3 wait in the queue, 4 waits to be put
    assert seen == {"ahead": True, "too_far": False}


def test_the_model_runs_on_the_calling_thread_and_everything_else_on_the_helper(world):
    list(analyse(world, [1, 2]))
    assert world.threads == {"download": {HELPER}, "faces": {HELPER}, "describe": {threading.current_thread().name}}


# -- errors -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("error", [SynologyError("download refused"), RuntimeError("ffmpeg was not found on PATH"),
                                   subprocess.CalledProcessError(1, "ffmpeg")])
def test_a_video_that_cannot_be_fetched_or_cut_is_reported_and_the_pass_goes_on(world, error):
    world.download_fails[2] = error
    first, broken, last = list(analyse(world, [1, 2, 3]))
    assert broken.error is error and broken.faces is None and broken.description is None
    assert first.error is None and last.error is None and last.description.summary_en == "video 3"


def test_any_other_error_stops_the_pass_at_that_video_after_the_ones_before_it(world):
    world.engine_fails[2] = ValueError("the face engine broke")
    outcomes = analyse(world, [1, 2, 3])
    assert next(outcomes).video["id"] == 1
    with pytest.raises(ValueError, match="face engine broke"):
        next(outcomes)


# -- clean-up -----------------------------------------------------------------------------------------

def test_nothing_is_left_in_the_temp_folder_after_a_full_pass(world):
    list(analyse(world, range(1, 5)))
    assert list(world.tmp.iterdir()) == []


def test_closing_early_stops_the_helper_and_leaves_nothing_behind(world):
    with_a_full_queue(world, prefetch=3).close()
    assert not any(thread.name == HELPER for thread in threading.enumerate())
    assert list(world.tmp.iterdir()) == []


def test_videos_prepared_but_never_used_are_cleaned_up_on_purpose_when_closing(world, folders):
    with_a_full_queue(world, prefetch=3).close()
    assert len(folders.made) >= 5 and set(folders.made) == set(folders.cleaned)  # 3 queued and 1 handed out, plus the frames' own


def test_nothing_is_left_behind_after_an_error_either(world):
    world.engine_fails[3] = ValueError("boom")
    with pytest.raises(ValueError, match="boom"):
        list(analyse(world, range(1, 6), prefetch=3))
    assert not any(thread.name == HELPER for thread in threading.enumerate())
    assert list(world.tmp.iterdir()) == []
