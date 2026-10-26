from types import SimpleNamespace

import numpy as np
import pytest

from videotag import pipeline
from videotag.albums import ALBUM_NAME
from videotag.analysis import Outcome
from videotag.describe import Description
from videotag.descriptions import extract_block
from videotag.evidence import Evidence
from videotag.library import Library
from videotag.live import LiveRun
from videotag.live import read as read_live
from videotag.matcher import PersonModel
from videotag.references import ReferenceSet, Source
from videotag.sampling import PRESETS
from videotag.slices import Slice, SliceError
from videotag.state import State
from videotag.synology import SynologyError


class RecordingClient:
    def __init__(self):
        self.added, self.albums_created, self.album_adds, self.album_removes = [], [], [], []
        self.faces, self.face_uploads, self.descriptions = {}, [], {}

    def tags(self):
        return []

    def create_tag(self, name):
        return 42

    def add_tag(self, item_id, tag_id):
        self.added.append((item_id, tag_id))

    def remove_tag(self, item_id, tag_id):
        self.added.remove((item_id, tag_id))

    def set_description(self, item_id, text):
        self.descriptions[item_id] = text

    def albums(self):
        return [{"id": 900}] if self.albums_created else []

    def album_item_ids(self, album_id):
        return {item for _, ids in self.album_adds for item in ids}

    def create_album(self, name):
        self.albums_created.append(name)
        return 900

    def add_to_album(self, album_id, ids):
        self.album_adds.append((album_id, ids))

    def remove_from_album(self, album_id, ids):
        self.album_removes.append((album_id, ids))

    def item_faces(self, video_id):
        return [dict(face) for face in self.faces.get(video_id, [])]

    def add_face(self, video_id, person_id, box):
        self.faces.setdefault(video_id, []).append({"face_id": 300 + len(self.faces), "person_id": person_id})

    def upload_face_crop(self, face_id, jpeg):
        self.face_uploads.append((face_id, jpeg))

    def person_item_ids(self, person_id):
        return {video for video, faces in self.faces.items() if any(f["person_id"] == person_id for f in faces)}


ALICE_FACE = np.array([1.0, 0.0])
FRAMES = [[SimpleNamespace(embedding=ALICE_FACE, box=(0.1, 0.1, 0.4, 0.4))]] * 3
VIDEO = {"id": 1, "filename": "clip.mp4", "filesize": 10, "time": 1, "additional": {"tag": [], "description": ""}}
CAT = Description("A cat.", "Eine Katze.", ("cat",), ("Katze",), ("cat",), ("Katze",))
THOROUGH = PRESETS["thorough"]


def analysis_of(*outcomes):
    """A stand-in for analyse_videos. A generator, like the real one, so that it can be closed."""
    def analyse(client, engine, describer, videos, sampling, reanalyse=False, evidence=None):
        yield from outcomes
    return analyse


def every_video_with(description):
    def analyse(client, engine, describer, videos, sampling, reanalyse=False, evidence=None):
        yield from [Outcome(item, FRAMES, description, sampling=THOROUGH) for item in videos]
    return analyse


def prepare(monkeypatch, description=CAT, video=VIDEO, can_auto=True):
    model = PersonModel(7, "Alice", np.stack([ALICE_FACE]), threshold=0.5, can_auto=can_auto)
    monkeypatch.setattr(pipeline, "load_library", lambda client: Library([], [video]))
    monkeypatch.setattr(pipeline, "build_references", lambda client, engine, photos: [])
    monkeypatch.setattr(pipeline, "person_models", lambda sets, threshold: [model])
    monkeypatch.setattr(pipeline, "analyse_videos", every_video_with(description))
    monkeypatch.setattr(pipeline, "frame_crop", lambda client, video, sampling, sighting: b"jpeg")


def run(client, state, apply):
    options = pipeline.RunOptions(apply=apply, sampling=THOROUGH, min_frames=2, max_writes=10)
    return pipeline.run(client, None, object(), options, state)


def now_reported_by_photos(video, client, *tag_names):
    """The video as Photos would list it after the writes in `client`; by default with our tag `Alice`."""
    tags = [{"id": 42 + i, "name": name} for i, name in enumerate(tag_names or ("Alice",))]
    return {**video, "additional": {"tag": tags, "description": client.descriptions.get(video["id"], "")}}


def test_a_dry_run_writes_nothing_and_saves_no_state(tmp_path, monkeypatch):
    prepare(monkeypatch)
    client, state = RecordingClient(), State(tmp_path / "state.json")
    rows = run(client, state, apply=False)
    assert client.added == [] and client.albums_created == [] and client.faces == {} and client.descriptions == {}
    assert not (tmp_path / "state.json").exists()
    assert rows[0]["matches"][0]["decision"] == "auto" and rows[0]["description"] == "A cat."


def test_an_applied_run_writes_the_tag_and_saves_state(tmp_path, monkeypatch):
    prepare(monkeypatch)
    client, state = RecordingClient(), State(tmp_path / "state.json")
    run(client, state, apply=True)
    assert client.added == [(1, 42)]
    assert [t.tag_name for t in State.load(tmp_path / "state.json").written] == ["Alice"]


def test_an_applied_run_puts_a_suggested_video_into_the_to_check_album(tmp_path, monkeypatch):
    prepare(monkeypatch, can_auto=False)
    client, state = RecordingClient(), State(tmp_path / "state.json")
    run(client, state, apply=True)
    assert client.albums_created == [ALBUM_NAME] and client.album_adds == [(900, [1])]
    assert State.load(tmp_path / "state.json").review_album == 900


def test_capped_tag_additions_put_only_successfully_suggested_videos_in_the_album(tmp_path, monkeypatch):
    prepare(monkeypatch, description=None, can_auto=False)
    videos = [{**VIDEO, "id": number} for number in [2, 1]]
    monkeypatch.setattr(pipeline, "load_library", lambda client: Library([], videos))
    client, state = RecordingClient(), State(tmp_path / "state.json")
    options = pipeline.RunOptions(True, THOROUGH, 2, 1)
    pipeline.run(client, None, None, options, state)
    assert client.added == [(2, 42)]
    assert client.album_adds == [(900, [2])]


def test_a_failed_suggestion_removal_keeps_the_video_in_the_album(tmp_path, monkeypatch):
    prepare(monkeypatch, description=None, can_auto=False)
    client, state = RecordingClient(), State(tmp_path / "state.json")
    run(client, state, apply=True)
    prepare(monkeypatch, description=None, can_auto=False, video=now_reported_by_photos(VIDEO, client, "Alice?", "Alice"))
    def refuse_removal(item_id, tag_id):
        raise SynologyError("remove refused")
    monkeypatch.setattr(client, "remove_tag", refuse_removal)
    run(client, state, apply=True)
    assert client.album_removes == []


def test_negative_run_write_budget_is_refused_before_execution():
    with pytest.raises(ValueError, match="max_writes"):
        pipeline.RunOptions(True, THOROUGH, 2, -1)


def test_zero_run_budget_makes_no_nas_writes(tmp_path, monkeypatch):
    prepare(monkeypatch, can_auto=False)
    client, state = RecordingClient(), State(tmp_path / "state.json")
    pipeline.run(client, None, None, pipeline.RunOptions(True, THOROUGH, 2, 0), state)
    assert client.added == [] and client.albums_created == [] and client.descriptions == {} and client.faces == {}


def test_a_confident_video_goes_into_no_album(tmp_path, monkeypatch):
    prepare(monkeypatch)
    client, state = RecordingClient(), State(tmp_path / "state.json")
    run(client, state, apply=True)
    assert client.albums_created == [] and State.load(tmp_path / "state.json").review_album is None


def test_confirming_a_suggestion_in_photos_takes_the_video_out_of_the_album(tmp_path, monkeypatch):
    prepare(monkeypatch, can_auto=False)
    client = RecordingClient()
    run(client, State(tmp_path / "state.json"), apply=True)
    prepare(monkeypatch, video=now_reported_by_photos(VIDEO, client, "Alice?", "Alice"), can_auto=False)  # the user adds Alice
    run(client, State.load(tmp_path / "state.json"), apply=True)
    assert client.album_removes == [(900, [1])]


def test_removing_the_suggestion_in_photos_takes_the_video_out_of_the_album_for_good(tmp_path, monkeypatch):
    prepare(monkeypatch, can_auto=False)
    client = RecordingClient()
    run(client, State(tmp_path / "state.json"), apply=True)
    prepare(monkeypatch, video={**VIDEO, "additional": {"tag": [], "description": ""}}, can_auto=False)  # the user removes Alice?
    reloaded = State.load(tmp_path / "state.json")
    run(client, reloaded, apply=True)
    assert client.album_removes == [(900, [1])] and (1, 7) in reloaded.rejected
    assert client.added == [(1, 42)]  # the suggestion was not written again


def test_an_applied_run_links_the_video_to_the_person_with_one_face_and_a_crop(tmp_path, monkeypatch):
    prepare(monkeypatch)
    client, state = RecordingClient(), State(tmp_path / "state.json")
    run(client, state, apply=True)
    assert client.faces == {1: [{"face_id": 301, "person_id": 7}]}
    assert client.face_uploads == [(301, b"jpeg")]
    assert State.load(tmp_path / "state.json").faces == {(1, 7): 301}


def test_an_applied_run_writes_the_bilingual_description_block(tmp_path, monkeypatch):
    prepare(monkeypatch)
    client, state = RecordingClient(), State(tmp_path / "state.json")
    run(client, state, apply=True)
    block = extract_block(client.descriptions[1])
    assert block is not None and "EN: A cat." in block and "DE: Eine Katze." in block
    assert State.load(tmp_path / "state.json").descriptions == {1: block}


def test_a_video_without_a_usable_description_is_simply_left_without_one(tmp_path, monkeypatch):
    prepare(monkeypatch, description=None)
    client, state = RecordingClient(), State(tmp_path / "state.json")
    run(client, state, apply=True)
    assert client.descriptions == {} and client.added == [(1, 42)]


def test_a_second_applied_run_changes_nothing(tmp_path, monkeypatch):
    prepare(monkeypatch)
    client, state = RecordingClient(), State(tmp_path / "state.json")
    run(client, state, apply=True)
    prepare(monkeypatch, video=now_reported_by_photos(VIDEO, client))
    run(client, State.load(tmp_path / "state.json"), apply=True)
    assert client.added == [(1, 42)] and len(client.faces[1]) == 1 and len(client.face_uploads) == 1
    assert len(client.descriptions) == 1 and client.album_adds == []


def test_a_second_applied_run_leaves_the_to_check_album_as_it_is(tmp_path, monkeypatch):
    prepare(monkeypatch, can_auto=False)
    client = RecordingClient()
    run(client, State(tmp_path / "state.json"), apply=True)
    prepare(monkeypatch, video=now_reported_by_photos(VIDEO, client, "Alice?"), can_auto=False)
    run(client, State.load(tmp_path / "state.json"), apply=True)
    assert client.albums_created == [ALBUM_NAME] and client.album_adds == [(900, [1])] and client.album_removes == []


def test_deleting_the_description_block_is_a_rejection_that_is_never_undone(tmp_path, monkeypatch):
    prepare(monkeypatch)
    client, state = RecordingClient(), State(tmp_path / "state.json")
    run(client, state, apply=True)
    client.descriptions[1] = ""  # the user deletes the block in Photos
    prepare(monkeypatch, video=now_reported_by_photos(VIDEO, client))
    reloaded = State.load(tmp_path / "state.json")
    run(client, reloaded, apply=True)
    assert client.descriptions[1] == "" and 1 in reloaded.description_rejected


def test_removing_the_person_from_the_video_in_photos_is_a_rejection_that_also_takes_our_tag_back(tmp_path, monkeypatch):
    prepare(monkeypatch)
    client, state = RecordingClient(), State(tmp_path / "state.json")
    run(client, state, apply=True)
    client.faces = {}  # the user removes Alice from the video in the People view
    prepare(monkeypatch, video=now_reported_by_photos(VIDEO, client))
    reloaded = State.load(tmp_path / "state.json")
    run(client, reloaded, apply=True)
    assert client.added == [] and client.faces == {}
    assert (1, 7) in reloaded.rejected


def test_a_video_that_could_not_be_fetched_becomes_an_error_row_and_the_run_goes_on(tmp_path, monkeypatch, caplog):
    prepare(monkeypatch)
    broken = {**VIDEO, "id": 2, "filename": "bad.mp4"}
    monkeypatch.setattr(pipeline, "load_library", lambda client: Library([], [broken, VIDEO]))
    monkeypatch.setattr(pipeline, "analyse_videos", analysis_of(Outcome(broken, error=RuntimeError("ffmpeg broke")),
                                                                Outcome(VIDEO, FRAMES, CAT, sampling=THOROUGH)))
    with caplog.at_level("INFO", logger="videotag"):
        rows = run(RecordingClient(), State(tmp_path / "state.json"), apply=False)
    assert rows[0] == {"video_id": 2, "filename": "bad.mp4", "error": "ffmpeg broke"}
    assert rows[1]["video_id"] == 1 and rows[1]["matches"][0]["decision"] == "auto"
    assert "2 bad.mp4: ffmpeg broke" in caplog.text


def test_the_analysis_is_finished_before_anything_is_written_to_photos(tmp_path, monkeypatch):
    prepare(monkeypatch)
    finished, seen = [], []

    def analysis(client, engine, describer, videos, sampling, reanalyse=False, evidence=None):
        try:
            yield from [Outcome(item, FRAMES, CAT, sampling=THOROUGH) for item in videos]
        finally:
            finished.append(True)  # the helper thread is joined here; after this only the calling thread uses the client

    monkeypatch.setattr(pipeline, "analyse_videos", analysis)
    client = RecordingClient()
    client.add_tag = lambda item_id, tag_id: seen.append(bool(finished))
    run(client, State(tmp_path / "state.json"), apply=True)
    assert seen == [True]


def test_the_analysis_is_stopped_when_the_run_fails_midway(tmp_path, monkeypatch):
    prepare(monkeypatch)
    finished = []

    def analysis(client, engine, describer, videos, sampling, reanalyse=False, evidence=None):
        try:
            yield from [Outcome(item, FRAMES, CAT, sampling=THOROUGH) for item in videos]
        finally:
            finished.append(True)

    monkeypatch.setattr(pipeline, "analyse_videos", analysis)
    monkeypatch.setattr(pipeline, "match_video", lambda *args: (_ for _ in ()).throw(ValueError("matcher broke")))
    with pytest.raises(ValueError, match="matcher broke"):
        run(RecordingClient(), State(tmp_path / "state.json"), apply=False)
    assert finished == [True]


def test_the_face_crop_is_cut_with_the_rule_the_result_was_made_under_not_the_one_in_use_now(tmp_path, monkeypatch):
    prepare(monkeypatch)
    rules = []
    monkeypatch.setattr(pipeline, "frame_crop", lambda client, video, sampling, sighting: rules.append(sampling) or b"jpeg")
    options = pipeline.RunOptions(apply=True, sampling=PRESETS["fast"], min_frames=2, max_writes=10)
    pipeline.run(RecordingClient(), None, object(), options, State(tmp_path / "state.json"))
    assert rules == [THOROUGH]  # the saved result was made under thorough, so its frame 0 is a thorough frame


def test_the_rule_in_use_and_the_wish_to_re_analyse_reach_the_analysis(tmp_path, monkeypatch):
    prepare(monkeypatch)
    seen = []

    def analysis(client, engine, describer, videos, sampling, reanalyse=False, evidence=None):
        seen.append((sampling, reanalyse))
        yield from []

    monkeypatch.setattr(pipeline, "analyse_videos", analysis)
    options = pipeline.RunOptions(apply=False, sampling=PRESETS["balanced"], min_frames=2, max_writes=10, reanalyse=True)
    pipeline.run(RecordingClient(), None, object(), options, State(tmp_path / "state.json"))
    assert seen == [(PRESETS["balanced"], True)]


# -- slices ---------------------------------------------------------------------------------------------------

OTHER = {**VIDEO, "id": 2, "filename": "other.mp4"}
THIRD = {**VIDEO, "id": 3, "filename": "third.mp4"}


def prepare_three(monkeypatch):
    prepare(monkeypatch)
    monkeypatch.setattr(pipeline, "load_library", lambda client: Library([], [VIDEO, OTHER, THIRD]))
    seen = []

    def analysis(client, engine, describer, videos, sampling, reanalyse=False, evidence=None):
        seen.append([item["id"] for item in videos])
        yield from [Outcome(item, FRAMES, CAT, sampling=THOROUGH) for item in videos]

    monkeypatch.setattr(pipeline, "analyse_videos", analysis)
    return seen


def sliced(chosen, state, client=None, **more):
    options = pipeline.RunOptions(apply=False, sampling=THOROUGH, min_frames=2, max_writes=10, slice=chosen, **more)
    return pipeline.run(client or RecordingClient(), None, object(), options, state)


def test_a_limited_dry_run_analyses_and_reports_only_its_slice(tmp_path, monkeypatch, caplog):
    seen = prepare_three(monkeypatch)
    with caplog.at_level("INFO", logger="videotag"):
        rows = sliced(Slice(limit=2), State(tmp_path / "state.json"))
    assert seen == [[1, 2]] and [row["video_id"] for row in rows] == [1, 2]
    assert "slice: 2 of 3 videos" in caplog.text
    assert "2 tag change(s) planned" in caplog.text  # for the two it looked at, nothing for the third


def test_not_analysed_yet_leaves_out_the_videos_that_have_their_results(tmp_path, monkeypatch):
    seen = prepare_three(monkeypatch)
    monkeypatch.setattr(pipeline, "is_analysed", lambda video, describe: video["id"] == 1)
    rows = sliced(Slice(new_only=True), State(tmp_path / "state.json"))
    assert seen == [[2, 3]] and [row["video_id"] for row in rows] == [2, 3]


def test_not_analysed_yet_asks_about_descriptions_only_when_they_are_wanted(tmp_path, monkeypatch):
    prepare_three(monkeypatch)
    asked = []
    monkeypatch.setattr(pipeline, "is_analysed", lambda video, describe: asked.append(describe) or False)
    sliced(Slice(new_only=True), State(tmp_path / "state.json"), describe=False)
    assert set(asked) == {False}


class AlbumsClient(RecordingClient):
    def albums(self):
        return [{"id": 5, "name": "Holiday"}, {"id": 900, "name": "Alice"}]

    def album_item_ids(self, album_id):
        return {2, 3, 777} if album_id == 5 else {1}


def test_an_album_slice_looks_at_the_videos_of_one_of_your_albums(tmp_path, monkeypatch):
    seen = prepare_three(monkeypatch)
    rows = sliced(Slice(album_id=5), State(tmp_path / "state.json"), AlbumsClient())
    assert seen == [[2, 3]] and len(rows) == 2


@pytest.mark.parametrize("album_id", [900, 12345])
def test_an_album_this_tool_made_or_a_missing_one_is_refused_before_the_reference_phase(tmp_path, monkeypatch, album_id):
    seen = prepare_three(monkeypatch)
    monkeypatch.setattr(pipeline, "build_references", lambda *args: pytest.fail("the reference phase must not start"))
    with pytest.raises(SliceError, match="no album"):
        sliced(Slice(album_id=album_id), State(tmp_path / "state.json", review_album=900), AlbumsClient())
    assert seen == []


def test_a_slice_cannot_be_combined_with_writing_to_photos():
    with pytest.raises(SliceError, match="dry run"):
        pipeline.RunOptions(apply=True, sampling=THOROUGH, min_frames=2, max_writes=10, slice=Slice(limit=5))


def test_a_run_without_a_slice_covers_the_whole_library_and_logs_no_slice(tmp_path, monkeypatch, caplog):
    seen = prepare_three(monkeypatch)
    with caplog.at_level("INFO", logger="videotag"):
        sliced(Slice(), State(tmp_path / "state.json"))
    assert seen == [[1, 2, 3]] and "slice:" not in caplog.text


# -- the live file ---------------------------------------------------------------------------------------

def test_every_row_reaches_the_live_file_as_soon_as_it_is_made_and_not_before_the_analysis_starts(tmp_path, monkeypatch):
    prepare_three(monkeypatch)
    live = LiveRun(tmp_path / "current-run.jsonl")
    seen = []
    monkeypatch.setattr(pipeline, "build_references",
                        lambda *args: seen.append(("references", (tmp_path / "current-run.jsonl").exists())) or [])

    def analysis(client, engine, describer, videos, sampling, reanalyse=False, evidence=None):
        for item in videos:
            yield Outcome(item, FRAMES, CAT, sampling=THOROUGH) if item["id"] != 2 else Outcome(item, error=RuntimeError("bad"))
            seen.append((item["id"], [row["video_id"] for row in read_live(tmp_path / "current-run.jsonl").rows]))

    monkeypatch.setattr(pipeline, "analyse_videos", analysis)
    options = pipeline.RunOptions(apply=False, sampling=THOROUGH, min_frames=2, max_writes=10)
    rows = pipeline.run(RecordingClient(), None, object(), options, State(tmp_path / "state.json"), live)
    assert seen == [("references", False), (1, [1]), (2, [1, 2]), (3, [1, 2, 3])]
    assert read_live(tmp_path / "current-run.jsonl").rows == rows  # the same rows the report will hold, errors included


def test_a_run_without_a_live_file_works_as_before(tmp_path, monkeypatch):
    prepare(monkeypatch)
    assert len(run(RecordingClient(), State(tmp_path / "state.json"), apply=False)) == 1


def test_a_twin_gets_the_same_matches_and_its_row_says_whose_result_it_shares(tmp_path, monkeypatch):
    prepare(monkeypatch)
    twin_video = {**VIDEO, "id": 9}
    monkeypatch.setattr(pipeline, "load_library", lambda client: Library([], [VIDEO, twin_video]))
    monkeypatch.setattr(pipeline, "analyse_videos", analysis_of(Outcome(VIDEO, FRAMES, CAT, sampling=THOROUGH),
                                                                Outcome(twin_video, FRAMES, CAT, sampling=THOROUGH, twin_of=1)))
    client = RecordingClient()
    rows = run(client, State(tmp_path / "state.json"), apply=True)
    assert "twin_of" not in rows[0] and rows[1]["twin_of"] == 1
    assert rows[1]["matches"] == rows[0]["matches"]
    assert sorted(item for item, _ in client.added) == [1, 9]  # both videos are tagged


# -- evidence and the accuracy report ---------------------------------------------------------------------

def test_a_run_given_a_folder_for_evidence_hands_the_analysis_a_hook_that_knows_this_runs_people(tmp_path, monkeypatch):
    prepare(monkeypatch)
    seen = []

    def analysis(client, engine, describer, videos, sampling, reanalyse=False, evidence=None):
        seen.append(evidence)
        yield from []

    monkeypatch.setattr(pipeline, "analyse_videos", analysis)
    options = pipeline.RunOptions(apply=False, sampling=THOROUGH, min_frames=2, max_writes=10)
    pipeline.run(RecordingClient(), None, object(), options, State(tmp_path / "state.json"), evidence_dir=tmp_path / "evidence")
    assert isinstance(seen[0], Evidence)
    assert seen[0].missing(VIDEO, FRAMES) is True   # Alice is in all three frames: she matches, and has no crops yet


def test_a_run_without_an_evidence_folder_keeps_no_crops(tmp_path, monkeypatch):
    prepare(monkeypatch)
    seen = []

    def analysis(client, engine, describer, videos, sampling, reanalyse=False, evidence=None):
        seen.append(evidence)
        yield from []

    monkeypatch.setattr(pipeline, "analyse_videos", analysis)
    run(RecordingClient(), State(tmp_path / "state.json"), apply=False)
    assert seen == [None]


def test_a_run_writes_the_accuracy_report_of_the_people_it_matched_against(tmp_path, monkeypatch):
    prepare(monkeypatch)
    alice = ReferenceSet(7, "Alice", np.stack([ALICE_FACE] * 3), [Source("single", n) for n in range(3)])
    monkeypatch.setattr(pipeline, "build_references", lambda client, engine, photos: [alice])
    options = pipeline.RunOptions(apply=False, sampling=THOROUGH, min_frames=2, max_writes=10)
    pipeline.run(RecordingClient(), None, object(), options, State(tmp_path / "state.json"),
                 accuracy_file=tmp_path / "accuracy.json")
    import json
    saved = json.loads((tmp_path / "accuracy.json").read_text(encoding="utf-8"))
    assert [(p["name"], p["references"], p["threshold"]) for p in saved["people"]] == [("Alice", 3, 0.5)]
    assert "time" in saved


def test_the_accuracy_report_is_written_before_the_long_analysis_so_it_is_there_even_if_the_run_is_stopped(tmp_path, monkeypatch):
    prepare(monkeypatch)
    written = []

    def analysis(client, engine, describer, videos, sampling, reanalyse=False, evidence=None):
        written.append((tmp_path / "accuracy.json").exists())
        yield from []

    monkeypatch.setattr(pipeline, "analyse_videos", analysis)
    options = pipeline.RunOptions(apply=False, sampling=THOROUGH, min_frames=2, max_writes=10)
    pipeline.run(RecordingClient(), None, object(), options, State(tmp_path / "state.json"),
                 accuracy_file=tmp_path / "accuracy.json")
    assert written == [True]


# -- the folder of a video, for the link into Photos ------------------------------------------------------------

def test_a_row_carries_the_folder_photos_keeps_the_video_in_and_has_none_when_photos_gave_none(tmp_path, monkeypatch):
    prepare(monkeypatch)
    in_a_folder = {**VIDEO, "folder_id": 640}
    monkeypatch.setattr(pipeline, "load_library", lambda client: Library([], [in_a_folder, OTHER]))
    monkeypatch.setattr(pipeline, "analyse_videos", analysis_of(Outcome(in_a_folder, FRAMES, CAT, sampling=THOROUGH),
                                                                Outcome(OTHER, FRAMES, CAT, sampling=THOROUGH)))
    rows = run(RecordingClient(), State(tmp_path / "state.json"), apply=False)
    assert rows[0]["folder_id"] == 640 and "folder_id" not in rows[1]


def test_an_error_row_carries_the_folder_too(tmp_path, monkeypatch):
    prepare(monkeypatch)
    broken = {**VIDEO, "id": 2, "filename": "bad.mp4", "folder_id": 12}
    monkeypatch.setattr(pipeline, "load_library", lambda client: Library([], [broken]))
    monkeypatch.setattr(pipeline, "analyse_videos", analysis_of(Outcome(broken, error=RuntimeError("ffmpeg broke"))))
    assert run(RecordingClient(), State(tmp_path / "state.json"), apply=False)[0]["folder_id"] == 12
