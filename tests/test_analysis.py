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
from videotag.evidence import Evidence
from videotag.faces import Face
from videotag.matcher import PersonModel
from videotag.sampling import PRESETS
from videotag.synology import SynologyError

HELPER = "videotag-prefetch"
FRAMES_PER_VIDEO = 20
THOROUGH = PRESETS["thorough"]


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
        self.frames_per_video = FRAMES_PER_VIDEO
        self.cut_under = []                                        # the sampling rule of every cut

    @contextmanager
    def video_frames(self, client, item, sampling):
        self.threads["download"].add(threading.current_thread().name)
        self.prepared.append(item["id"])
        self.cut_under.append(sampling)
        if item["id"] in self.download_fails:
            raise self.download_fails[item["id"]]
        if item["id"] in self.on_prepared:
            self.on_prepared[item["id"]]()
        with tempfile.TemporaryDirectory() as folder:
            frames = []
            for number in range(1, self.frames_per_video + 1):
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


def analyse(world, numbers, describer=True, prefetch=8, sampling=THOROUGH, reanalyse=False, evidence=None):
    return analyse_videos(world.client, world.engine, world.describer if describer else None,
                          [video(n) for n in numbers], sampling, prefetch, reanalyse, evidence)


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
        assert same_faces(facecache.load(o.video["id"], o.video).faces, o.faces)
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
    facecache.save(1, item, [[Face(np.array([1.0, 0.0]), 1.0, (0.0, 0.0, 1.0, 1.0))]], THOROUGH)
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


# -- the sampling rule ----------------------------------------------------------------------------------

def test_each_result_records_the_rule_its_frames_were_cut_with(world):
    outcome = next(analyse(world, [1], sampling=PRESETS["balanced"]))
    assert outcome.sampling == PRESETS["balanced"] and facecache.load(1, video(1)).sampling == PRESETS["balanced"]


def test_a_result_made_under_another_preset_is_used_as_it_is_and_keeps_its_own_rule(world):
    list(analyse(world, [1]))
    world.prepared.clear()
    world.faced.clear()
    outcome = next(analyse(world, [1], sampling=PRESETS["fast"]))
    assert world.prepared == [] and world.faced == []
    assert outcome.sampling == THOROUGH and len(outcome.faces) == FRAMES_PER_VIDEO


def test_re_analysing_redoes_only_the_results_made_under_another_rule(world):
    list(analyse(world, [1]))                                       # video 1 under thorough
    list(analyse(world, [2], sampling=PRESETS["balanced"]))         # video 2 under balanced
    world.prepared.clear()
    outcomes = list(analyse(world, [1, 2], sampling=PRESETS["balanced"], reanalyse=True))
    assert world.prepared == [1]
    assert [o.sampling for o in outcomes] == [PRESETS["balanced"], PRESETS["balanced"]]
    assert facecache.load(1, video(1)).sampling == PRESETS["balanced"]


def test_without_re_analysing_nothing_is_redone_when_the_preset_changes(world):
    list(analyse(world, [1]))
    world.prepared.clear()
    list(analyse(world, [1], sampling=PRESETS["balanced"]))
    assert world.prepared == []


def test_a_missing_description_is_made_from_frames_cut_under_the_rule_of_the_saved_faces(world):
    list(analyse(world, [1], describer=False))
    world.prepared.clear()
    world.faced.clear()
    outcome = next(analyse(world, [1], sampling=PRESETS["balanced"]))
    assert world.prepared == [1] and world.faced == [] and outcome.sampling == THOROUGH
    assert outcome.description is not None
    assert world.cut_under == [THOROUGH, THOROUGH]  # the first cut made the faces, the second the description


def test_the_description_model_sees_frames_spread_over_the_time_of_a_slowed_down_clip(world):
    world.frames_per_video = 70  # 60 frames in the first 30 s, then one every 2 s up to 48 s
    list(analyse(world, [1], sampling=PRESETS["balanced"]))
    assert world.frame_names[1] == ["f0001.jpg", "f0020.jpg", "f0039.jpg", "f0059.jpg", "f0065.jpg", "f0070.jpg"]


# -- what counts as analysed ------------------------------------------------------------------------------

def test_a_video_with_a_saved_result_counts_as_analysed_under_any_preset(world):
    list(analyse(world, [1], sampling=PRESETS["fast"]))
    assert analysis.is_analysed(video(1), describe=True) and analysis.is_analysed(video(1), describe=False)


def test_a_video_without_a_description_is_not_analysed_while_descriptions_are_wanted(world):
    list(analyse(world, [1], describer=False))
    assert analysis.is_analysed(video(1), describe=False) and not analysis.is_analysed(video(1), describe=True)


def test_a_video_nobody_has_looked_at_or_that_changed_since_is_not_analysed(world):
    list(analyse(world, [1]))
    assert not analysis.is_analysed(video(2), describe=False)
    assert not analysis.is_analysed({**video(1), "filesize": 5}, describe=False)


# -- evidence: the faces that made a match ---------------------------------------------------------------------

class Hook:
    """Stands in for `evidence.Evidence`: records what it was asked and what it was given."""

    def __init__(self, missing=True, fail=False):
        self.wants, self.fail = missing, fail
        self.asked, self.kept = [], []

    def missing(self, video, faces):
        self.asked.append(video["id"])
        return self.wants

    def keep(self, video, frames, faces):
        if self.fail:
            raise RuntimeError("could not cut a crop")
        self.kept.append((video["id"], len(frames), len(faces)))


def test_a_video_that_is_analysed_now_gets_its_evidence_from_the_frames_already_cut(world):
    hook = Hook()
    list(analyse(world, [1], evidence=hook))
    assert hook.kept == [(1, FRAMES_PER_VIDEO, FRAMES_PER_VIDEO)] and world.prepared == [1]  # one cut did everything


def test_a_cached_video_whose_evidence_is_missing_is_fetched_once_under_its_stored_rule_and_its_faces_stay(world):
    list(analyse(world, [1]))
    world.prepared.clear()
    world.faced.clear()
    world.cut_under.clear()
    hook = Hook(missing=True)
    outcome = next(analyse(world, [1], sampling=PRESETS["fast"], evidence=hook))
    assert world.prepared == [1] and world.faced == [] and world.cut_under == [THOROUGH]  # the rule of the saved faces
    assert hook.kept == [(1, FRAMES_PER_VIDEO, FRAMES_PER_VIDEO)] and outcome.sampling == THOROUGH


def test_a_cached_video_with_its_evidence_in_place_is_not_fetched(world):
    list(analyse(world, [1]))
    world.prepared.clear()
    hook = Hook(missing=False)
    list(analyse(world, [1], evidence=hook))
    assert world.prepared == [] and hook.asked == [1] and hook.kept == []


def test_without_an_evidence_hook_a_cached_video_is_still_not_fetched(world):
    list(analyse(world, [1]))
    world.prepared.clear()
    list(analyse(world, [1], evidence=None))
    assert world.prepared == []


def test_missing_evidence_and_a_missing_description_cost_one_download_and_one_cut(world):
    list(analyse(world, [1], describer=False))
    world.prepared.clear()
    hook = Hook(missing=True)
    outcome = next(analyse(world, [1], evidence=hook))
    assert world.prepared == [1] and hook.kept and outcome.description is not None


def test_a_crop_that_cannot_be_made_never_costs_the_video_its_results(world, caplog):
    with caplog.at_level("WARNING", logger="videotag"):
        outcome = next(analyse(world, [1], evidence=Hook(fail=True)))
    assert outcome.error is None and len(outcome.faces) == FRAMES_PER_VIDEO and outcome.description is not None
    assert "evidence of 1" in caplog.text and "could not cut a crop" in caplog.text


FETCH_FAILURES = [SynologyError("download refused"), RuntimeError("ffmpeg was not found on PATH"),
                  subprocess.CalledProcessError(1, "ffmpeg")]


@pytest.mark.parametrize("failure", FETCH_FAILURES)
def test_a_download_that_was_only_for_the_crops_never_costs_a_cached_video_its_results(world, caplog, failure):
    first = next(analyse(world, [1]))
    world.download_fails[1] = failure
    with caplog.at_level("WARNING", logger="videotag"):
        outcome = next(analyse(world, [1], evidence=Hook(missing=True)))
    assert outcome.error is None and same_faces(outcome.faces, first.faces) and outcome.sampling == THOROUGH
    assert outcome.description == first.description
    assert "evidence of 1" in caplog.text


@pytest.mark.parametrize("failure", FETCH_FAILURES)
def test_a_download_that_was_for_a_missing_description_is_still_an_error_row(world, failure):
    list(analyse(world, [1], describer=False))
    world.download_fails[1] = failure
    outcome = next(analyse(world, [1], evidence=Hook(missing=True)))
    assert outcome.error is failure


def test_a_video_whose_frames_do_not_line_up_with_its_saved_faces_is_fetched_once_not_every_run(world, tmp_path):
    model = PersonModel(7, "Alice", np.stack([np.array([1.0, 0.0])] * 3), threshold=0.5)
    item = video(1)
    matching = Face(np.array([1.0, 0.0]), 1.0, (0.0, 0.0, 1.0, 1.0))
    facecache.save(1, item, [[matching]] * (FRAMES_PER_VIDEO + 5), THOROUGH)   # five more frames than the cut will give
    evidence = Evidence([model], min_frames=2, folder=tmp_path / "evidence")
    list(analyse(world, [1], describer=False, evidence=evidence))
    assert world.prepared == [1]
    list(analyse(world, [1], describer=False, evidence=evidence))
    list(analyse(world, [1], describer=False, evidence=evidence))
    assert world.prepared == [1]  # the first run found out that the crops cannot be made, and later runs left the video alone


# -- twins: the same file under two ids -----------------------------------------------------------------------

def twin(number, of):
    """Another id for the same file: same name, size and time."""
    return {**video(of), "id": number}


def analyse_these(world, videos, **more):
    return list(analyse_videos(world.client, world.engine, world.describer, videos, THOROUGH, **more))


def test_twins_are_analysed_once_and_each_gets_the_result_in_library_order(world):
    outcomes = analyse_these(world, [video(1), twin(2, of=1), video(3)])
    assert world.prepared == [1, 3] and world.described == [1, 3]  # the twin was never fetched or described
    assert [o.video["id"] for o in outcomes] == [1, 2, 3]
    assert same_faces(outcomes[1].faces, outcomes[0].faces) and outcomes[1].description == outcomes[0].description
    assert outcomes[1].sampling == outcomes[0].sampling and [o.twin_of for o in outcomes] == [None, 1, None]


def test_the_first_of_several_twins_is_the_one_analysed_wherever_the_others_sit(world):
    outcomes = analyse_these(world, [video(1), video(3), twin(4, of=3), twin(5, of=1), twin(6, of=1)])
    assert world.prepared == [1, 3]
    assert [(o.video["id"], o.twin_of) for o in outcomes] == [(1, None), (3, None), (4, 3), (5, 1), (6, 1)]


def test_the_same_name_with_another_size_or_time_is_not_a_twin(world):
    other_size = {**video(1), "id": 2, "filesize": 999}
    other_time = {**video(1), "id": 3, "time": 8}
    outcomes = analyse_these(world, [video(1), other_size, other_time])
    assert world.prepared == [1, 2, 3] and [o.twin_of for o in outcomes] == [None, None, None]


def test_the_same_size_and_time_under_another_name_is_not_a_twin(world):
    renamed = {**video(1), "id": 2, "filename": "other.mp4"}
    assert [o.twin_of for o in analyse_these(world, [video(1), renamed])] == [None, None]
    assert world.prepared == [1, 2]


def test_a_twin_has_its_own_saved_result_afterwards_so_it_counts_as_analysed_and_costs_no_download(world):
    analyse_these(world, [video(1), twin(2, of=1)])
    assert analysis.is_analysed(twin(2, of=1), describe=True)
    world.prepared.clear()
    analyse_these(world, [twin(2, of=1)])   # on its own, for example in a "not analysed yet" slice
    assert world.prepared == []


def test_a_twin_of_a_video_that_could_not_be_fetched_gets_the_same_error_and_no_saved_result(world):
    world.download_fails[1] = SynologyError("download refused")
    first, second = analyse_these(world, [video(1), twin(2, of=1)])
    assert first.error is second.error and second.twin_of == 1 and second.faces is None
    assert not analysis.is_analysed(twin(2, of=1), describe=False)


def test_without_a_describer_a_twin_is_copied_without_a_description(world):
    first, second = list(analyse_videos(world.client, world.engine, None, [video(1), twin(2, of=1)], THOROUGH))
    assert second.description is None and same_faces(second.faces, first.faces)


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
