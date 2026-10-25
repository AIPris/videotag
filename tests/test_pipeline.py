from types import SimpleNamespace

import numpy as np

from videotag import pipeline
from videotag.describe import Description
from videotag.descriptions import extract_block
from videotag.library import Library
from videotag.matcher import PersonModel
from videotag.state import State


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


def prepare(monkeypatch, description=CAT, video=VIDEO):
    model = PersonModel(7, "Alice", np.stack([ALICE_FACE]), threshold=0.5)
    monkeypatch.setattr(pipeline, "load_library", lambda client: Library([], [video]))
    monkeypatch.setattr(pipeline, "build_references", lambda client, engine, photos: [])
    monkeypatch.setattr(pipeline, "person_models", lambda sets, threshold: [model])
    monkeypatch.setattr(pipeline, "_analyse", lambda client, engine, describer, video, fps: (FRAMES, description))
    monkeypatch.setattr(pipeline, "frame_crop", lambda client, video, fps, sighting: b"jpeg")


def run(client, state, apply):
    options = pipeline.RunOptions(apply=apply, fps=2.0, min_frames=2, max_writes=10)
    return pipeline.run(client, None, object(), options, state)


def now_reported_by_photos(video, client, tag=True):
    """The video as Photos would list it after the writes in `client`."""
    tags = [{"id": 42, "name": "Alice"}] if tag else []
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


def test_an_applied_run_also_puts_the_matched_video_into_the_persons_album(tmp_path, monkeypatch):
    prepare(monkeypatch)
    client, state = RecordingClient(), State(tmp_path / "state.json")
    run(client, state, apply=True)
    assert client.albums_created == ["Alice"] and client.album_adds == [(900, [1])]
    assert State.load(tmp_path / "state.json").albums == {7: 900}


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
    assert len(client.descriptions) == 1 and client.album_adds == [(900, [1])]


def test_deleting_the_description_block_is_a_rejection_that_is_never_undone(tmp_path, monkeypatch):
    prepare(monkeypatch)
    client, state = RecordingClient(), State(tmp_path / "state.json")
    run(client, state, apply=True)
    client.descriptions[1] = ""  # the user deletes the block in Photos
    prepare(monkeypatch, video=now_reported_by_photos(VIDEO, client))
    reloaded = State.load(tmp_path / "state.json")
    run(client, reloaded, apply=True)
    assert client.descriptions[1] == "" and 1 in reloaded.description_rejected


def test_removing_the_person_from_the_video_in_photos_is_a_rejection_that_also_takes_our_tag_back_and_empties_the_album(
        tmp_path, monkeypatch):
    prepare(monkeypatch)
    client, state = RecordingClient(), State(tmp_path / "state.json")
    run(client, state, apply=True)
    client.faces = {}  # the user removes Alice from the video in the People view
    prepare(monkeypatch, video=now_reported_by_photos(VIDEO, client))
    client.remove_tag = lambda item_id, tag_id: client.added.remove((item_id, tag_id))
    reloaded = State.load(tmp_path / "state.json")
    run(client, reloaded, apply=True)
    assert client.added == [] and client.faces == {}
    assert (1, 7) in reloaded.rejected
    assert client.album_removes == [(900, [1])]  # in the same run, not only the next one
