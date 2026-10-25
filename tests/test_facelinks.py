import logging

from videotag.facelinks import (
    FaceAction,
    delete_faces,
    execute_faces,
    plan_face_changes,
    sync_face_feedback,
    verify_links,
)
from videotag.matcher import Sighting
from videotag.state import State
from videotag.synology import SynologyError

ALICE = 1
SIGHTING = Sighting(frame_index=3, box=(0.1, 0.2, 0.4, 0.5), similarity=0.7)


def face(face_id, person_id=ALICE):
    return {"face_id": face_id, "person_id": person_id}


# -- planning ---------------------------------------------------------------------------------

def test_a_confident_video_without_a_face_gets_one():
    assert plan_face_changes({(10, ALICE)}, State(), {10: []}) == [FaceAction("add", 10, ALICE)]


def test_no_second_face_is_created_when_the_person_is_already_on_the_video():
    assert plan_face_changes({(10, ALICE)}, State(), {10: [face(5)]}) == []


def test_a_face_of_someone_else_does_not_count_as_linked():
    assert plan_face_changes({(10, ALICE)}, State(), {10: [face(5, person_id=2)]}) == [FaceAction("add", 10, ALICE)]


def test_a_rejected_pair_is_never_linked_again():
    assert plan_face_changes({(10, ALICE)}, State(rejected={(10, ALICE)}), {10: []}) == []


def test_a_face_we_created_stays_while_the_match_stands_and_goes_when_it_does_not():
    state = State(faces={(10, ALICE): 5, (11, ALICE): 6})
    assert plan_face_changes({(10, ALICE)}, state, {10: [face(5)], 11: [face(6)]}) == [FaceAction("remove", 11, ALICE, 6)]


def test_a_face_the_user_removed_in_photos_becomes_a_rejection():
    state = State(faces={(10, ALICE): 5, (11, ALICE): 6})
    sync_face_feedback(state, {10: [], 11: [face(6)]})
    assert state.faces == {(11, ALICE): 6} and state.rejected == {(10, ALICE)}


def test_a_video_we_could_not_look_at_is_left_alone():
    state = State(faces={(10, ALICE): 5})
    sync_face_feedback(state, {})
    assert state.faces == {(10, ALICE): 5} and not state.rejected


# -- executing --------------------------------------------------------------------------------

class FakeClient:
    def __init__(self, add_code=None, stores=True, upload_fails=False):
        self.faces, self.next_id, self.uploads, self.deleted = {}, 100, [], []
        self.add_code, self.stores, self.upload_fails = add_code, stores, upload_fails

    def item_faces(self, video_id):
        return [dict(f) for f in self.faces.get(video_id, [])]

    def add_face(self, video_id, person_id, box):
        if self.stores:
            self.next_id += 1
            self.faces.setdefault(video_id, []).append(face(self.next_id, person_id))
        if self.add_code:
            raise SynologyError("add_face failed", self.add_code)

    def upload_face_crop(self, face_id, jpeg):
        if self.upload_fails:
            raise SynologyError("upload failed")
        self.uploads.append((face_id, jpeg))

    def delete_face(self, face_id, person_id):
        self.deleted.append(face_id)

    def person_item_ids(self, person_id):
        return {video for video, faces in self.faces.items() if any(f["person_id"] == person_id for f in faces)}


def crop_for(video_id, sighting):
    return f"crop-{video_id}-{sighting.frame_index}".encode()


def add_one(client, tmp_path, budget=10):
    state = State(tmp_path / "state.json")
    done = execute_faces(client, state, [FaceAction("add", 10, ALICE)], {(10, ALICE): SIGHTING}, crop_for, budget)
    return state, done


def test_adding_stores_one_face_uploads_the_crop_and_remembers_the_id(tmp_path):
    client = FakeClient()
    state, done = add_one(client, tmp_path)
    assert done == 1 and state.faces == {(10, ALICE): 101}
    assert client.uploads == [(101, b"crop-10-3")]
    assert State.load(tmp_path / "state.json").faces == {(10, ALICE): 101}


def test_error_117_with_a_stored_face_still_counts_as_linked(tmp_path):
    client = FakeClient(add_code=117)  # what Photos answers for a video without a usable thumbnail
    state, done = add_one(client, tmp_path)
    assert done == 1 and state.faces == {(10, ALICE): 101}


def test_an_error_without_a_stored_face_is_a_failure_and_nothing_is_remembered(tmp_path):
    client = FakeClient(add_code=117, stores=False)
    state, done = add_one(client, tmp_path)
    assert done == 0 and state.faces == {}


def test_a_failed_crop_upload_does_not_undo_the_link(tmp_path):
    client = FakeClient(upload_fails=True)
    state, done = add_one(client, tmp_path)
    assert done == 1 and state.faces == {(10, ALICE): 101}


def test_removing_deletes_the_face_and_forgets_it(tmp_path):
    client, state = FakeClient(), State(tmp_path / "state.json", faces={(10, ALICE): 7})
    assert execute_faces(client, state, [FaceAction("remove", 10, ALICE, 7)], {}, crop_for, 10) == 1
    assert client.deleted == [7] and state.faces == {}


def test_the_write_limit_stops_the_face_changes(tmp_path):
    client, state = FakeClient(), State(tmp_path / "state.json")
    actions = [FaceAction("add", video, ALICE) for video in (10, 11, 12)]
    sightings = {(video, ALICE): SIGHTING for video in (10, 11, 12)}
    assert execute_faces(client, state, actions, sightings, crop_for, budget=2) == 2
    assert sorted(state.faces) == [(10, ALICE), (11, ALICE)]


def test_verify_warns_about_a_link_photos_does_not_list(tmp_path, caplog):
    client = FakeClient()
    client.faces = {10: [face(5)]}
    state = State(tmp_path / "state.json", faces={(10, ALICE): 5, (11, ALICE): 6})
    with caplog.at_level(logging.WARNING, logger="videotag"):
        verify_links(client, state)
    assert [r.getMessage() for r in caplog.records] == ["video 11 is linked to person 1 but Photos does not list it for them"]


def test_deleting_all_faces_keeps_the_ones_that_could_not_be_deleted(tmp_path):
    class Picky(FakeClient):
        def delete_face(self, face_id, person_id):
            if face_id == 6:
                raise SynologyError("nope")
            super().delete_face(face_id, person_id)

    client, state = Picky(), State(tmp_path / "state.json", faces={(10, ALICE): 5, (11, ALICE): 6})
    assert delete_faces(client, state) == 1
    assert client.deleted == [5] and state.faces == {(11, ALICE): 6}
