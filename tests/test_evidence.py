from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from videotag import evidence as evidence_module
from videotag.evidence import Evidence, crops
from videotag.matcher import PersonModel

ALICE_REFS = np.stack([np.array([1.0, 0.0]), np.array([1.0, 0.1]) / np.hypot(1, 0.1), np.array([1.0, -0.1]) / np.hypot(1, 0.1)])
BOB_REFS = np.stack([np.array([0.0, 1.0])])
ALICE_FACE = np.array([1.0, 0.02]) / np.hypot(1, 0.02)
BOB_FACE = np.array([0.0, 1.0])
VIDEO = {"id": 12, "filename": "clip.mp4", "filesize": 10, "time": 1}
BOX = (0.25, 0.25, 0.75, 0.75)


def models():
    return [PersonModel(7, "Alice", ALICE_REFS, 0.8), PersonModel(8, "Bob", BOB_REFS, 0.8)]


def faces_of(*embeddings):
    return [SimpleNamespace(embedding=embedding, box=BOX) for embedding in embeddings]


def make_frames(folder, count):
    """Frame number n is a flat grey of n*20, so a crop shows which frame it was cut from."""
    paths = []
    for number in range(count):
        path = folder / f"f{number + 1:04d}.jpg"
        cv2.imwrite(str(path), np.full((100, 100, 3), number * 20, np.uint8))
        paths.append(path)
    return paths


def shows_frame(crop_path):
    return round(float(cv2.imread(str(crop_path)).mean()) / 20)


@pytest.fixture
def evidence(tmp_path):
    return Evidence(models(), min_frames=2, folder=tmp_path / "evidence")


@pytest.fixture
def frames(tmp_path):
    (tmp_path / "frames").mkdir()
    return make_frames(tmp_path / "frames", 6)


def alice_in_frames(*numbers):
    """Six frames with Alice (a face like her references) in the given ones and a stranger's face in the others."""
    stranger = np.array([-1.0, 0.0])
    return [faces_of(ALICE_FACE) if number in numbers else faces_of(stranger) for number in range(6)]


def test_a_matching_person_gets_their_best_faces_as_files_numbered_from_the_best(evidence, frames, tmp_path):
    third_best, second_best = np.array([1.0, 0.3]) / np.hypot(1, 0.3), np.array([1.0, 0.1]) / np.hypot(1, 0.1)
    faces = [faces_of(third_best), faces_of(ALICE_FACE), faces_of(second_best)] + alice_in_frames()[3:]
    evidence.keep(VIDEO, frames, faces)
    found = crops(tmp_path / "evidence", 12)
    assert list(found) == [7] and found[7] == ["7-1.jpg", "7-2.jpg", "7-3.jpg"]
    assert shows_frame(tmp_path / "evidence" / "12" / "7-1.jpg") == 1  # frame 1 holds the face most like Alice's references


def test_a_video_without_a_match_keeps_nothing_and_no_folder(evidence, frames, tmp_path):
    evidence.keep(VIDEO, frames, alice_in_frames(2)[:1] + alice_in_frames()[1:])
    assert crops(tmp_path / "evidence", 12) == {} and not (tmp_path / "evidence" / "12").exists()


def test_two_people_in_one_video_each_get_their_own_crops(evidence, frames, tmp_path):
    faces = [faces_of(ALICE_FACE, BOB_FACE)] * 3 + alice_in_frames()[3:]
    evidence.keep(VIDEO, frames, faces)
    assert set(crops(tmp_path / "evidence", 12)) == {7, 8}


def test_only_a_suggestion_still_has_evidence_because_that_is_what_the_user_wants_to_check(evidence, frames, tmp_path):
    evidence.keep(VIDEO, frames, alice_in_frames(4))  # one hit frame: a suggestion, not a confident match
    assert list(crops(tmp_path / "evidence", 12)) == [7]


def test_keeping_again_replaces_the_crops_and_removes_those_of_people_who_no_longer_match(evidence, frames, tmp_path):
    evidence.keep(VIDEO, frames, [faces_of(ALICE_FACE, BOB_FACE)] * 3 + alice_in_frames()[3:])
    evidence.keep(VIDEO, frames, alice_in_frames(0, 1, 2))
    assert list(crops(tmp_path / "evidence", 12)) == [7]
    assert sorted(path.name for path in (tmp_path / "evidence" / "12").glob("*.jpg")) == ["7-1.jpg", "7-2.jpg", "7-3.jpg"]


def test_nothing_is_left_half_written_and_no_temporary_file_stays(evidence, frames, tmp_path):
    evidence.keep(VIDEO, frames, alice_in_frames(0, 1, 2))
    assert not list((tmp_path / "evidence" / "12").glob("*.part"))
    assert all(cv2.imread(str(path)) is not None for path in (tmp_path / "evidence" / "12").glob("*.jpg"))


def test_frames_that_do_not_line_up_with_the_faces_are_never_cropped(evidence, frames, tmp_path, caplog):
    with caplog.at_level("WARNING", logger="videotag"):
        evidence.keep(VIDEO, frames[:4], alice_in_frames(0, 1, 2))   # six frames' worth of faces, four frames
    assert crops(tmp_path / "evidence", 12) == {} and "do not line up" in caplog.text


def test_crops_are_missing_until_every_matching_person_has_some(evidence, frames, tmp_path):
    faces = [faces_of(ALICE_FACE, BOB_FACE)] * 3 + alice_in_frames()[3:]
    assert evidence.missing(VIDEO, faces) is True
    evidence.keep(VIDEO, frames, faces)
    assert evidence.missing(VIDEO, faces) is False


def test_a_video_that_matches_nobody_is_never_missing_anything(evidence):
    assert evidence.missing(VIDEO, alice_in_frames()) is False


def test_asking_what_is_missing_removes_the_crops_of_someone_who_no_longer_matches_without_needing_the_video(
        evidence, frames, tmp_path):
    evidence.keep(VIDEO, frames, [faces_of(ALICE_FACE, BOB_FACE)] * 3 + alice_in_frames()[3:])
    assert evidence.missing(VIDEO, alice_in_frames(0, 1, 2)) is False   # Bob no longer matches, Alice still has hers
    assert list(crops(tmp_path / "evidence", 12)) == [7]


def test_a_person_who_newly_matches_makes_the_crops_missing_even_if_others_have_theirs(evidence, frames):
    evidence.keep(VIDEO, frames, alice_in_frames(0, 1, 2))
    assert evidence.missing(VIDEO, [faces_of(ALICE_FACE, BOB_FACE)] * 3 + alice_in_frames()[3:]) is True


def test_a_folder_that_does_not_exist_has_no_crops(tmp_path):
    assert crops(tmp_path / "nowhere", 1) == {}


def test_files_that_are_not_crops_are_left_alone_and_not_listed(evidence, frames, tmp_path):
    evidence.keep(VIDEO, frames, alice_in_frames(0, 1, 2))
    (tmp_path / "evidence" / "12" / "notes.txt").write_text("mine", encoding="utf-8")
    assert list(crops(tmp_path / "evidence", 12)) == [7]
    evidence.keep(VIDEO, frames, alice_in_frames(0, 1, 2))
    assert (tmp_path / "evidence" / "12" / "notes.txt").exists()


# -- crops that cannot be made are not asked for again --------------------------------------------------------------

def test_frames_that_do_not_line_up_are_remembered_so_the_video_is_not_cut_again_for_the_same_faces(evidence, frames):
    faces = alice_in_frames(0, 1, 2)
    evidence.keep(VIDEO, frames[:4], faces)
    assert evidence.missing(VIDEO, faces) is False


def test_a_crop_that_fails_is_a_warning_and_is_remembered_too(evidence, frames, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(evidence_module, "crop_jpeg", lambda path, box: (_ for _ in ()).throw(ValueError("could not encode")))
    faces = alice_in_frames(0, 1, 2)
    with caplog.at_level("WARNING", logger="videotag"):
        evidence.keep(VIDEO, frames, faces)
    assert "could not encode" in caplog.text and crops(tmp_path / "evidence", 12) == {}
    assert evidence.missing(VIDEO, faces) is False


def test_other_faces_or_another_file_are_tried_again(evidence, frames):
    faces = alice_in_frames(0, 1, 2)
    evidence.keep(VIDEO, frames[:4], faces)
    assert evidence.missing(VIDEO, faces + faces[:1]) is True             # the faces were rebuilt under another rule
    assert evidence.missing({**VIDEO, "filesize": 99}, faces) is True      # the file changed


def test_a_later_success_clears_the_memory_of_a_failure(evidence, frames, tmp_path):
    faces = alice_in_frames(0, 1, 2)
    evidence.keep(VIDEO, frames[:4], faces)
    evidence.keep(VIDEO, frames, faces)
    assert list(crops(tmp_path / "evidence", 12)) == [7]
    assert not (tmp_path / "evidence" / "12" / "skipped.txt").exists()


def test_the_memory_of_a_failure_is_a_file_in_the_video_folder_and_never_listed_as_a_crop(evidence, frames, tmp_path):
    evidence.keep(VIDEO, frames[:4], alice_in_frames(0, 1, 2))
    assert crops(tmp_path / "evidence", 12) == {} and (tmp_path / "evidence" / "12" / "skipped.txt").exists()


def test_changed_models_that_swap_the_same_peoples_winners_require_new_crops(evidence, frames, tmp_path):
    faces = [faces_of(ALICE_FACE)] * 3 + [faces_of(BOB_FACE)] * 3
    evidence.keep(VIDEO, frames, faces)
    swapped = Evidence([PersonModel(7, "Alice", BOB_REFS, 0.8), PersonModel(8, "Bob", ALICE_REFS, 0.8)], 2,
                       tmp_path / "evidence")
    assert swapped.missing(VIDEO, faces) is True
    swapped.keep(VIDEO, frames, faces)
    assert shows_frame(tmp_path / "evidence" / "12" / "7-1.jpg") == 3
    assert swapped.missing(VIDEO, faces) is False


def test_rebuilt_faces_and_changed_files_invalidate_existing_crops(evidence, frames):
    evidence.keep(VIDEO, frames, alice_in_frames(0, 1, 2))
    assert evidence.missing(VIDEO, alice_in_frames(3, 4, 5)) is True
    assert evidence.missing({**VIDEO, "time": 2}, alice_in_frames(0, 1, 2)) is True


def test_changed_winners_invalidate_a_skipped_marker(evidence, frames):
    evidence.keep(VIDEO, frames[:1], alice_in_frames(0, 1, 2))
    assert evidence.missing(VIDEO, alice_in_frames(3, 4, 5)) is True


def test_old_crops_without_provenance_are_rebuilt(evidence, tmp_path):
    folder = tmp_path / "evidence" / "12"
    folder.mkdir(parents=True)
    (folder / "7-1.jpg").write_bytes(b"legacy crop")
    assert evidence.missing(VIDEO, alice_in_frames(0, 1, 2)) is True
