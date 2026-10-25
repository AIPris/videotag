from types import SimpleNamespace

import numpy as np
import pytest

from videotag.matcher import Decision, PersonModel, best_sighting, decide, face_similarity, match_video


def unit(*values: float) -> np.ndarray:
    vector = np.array(values, dtype=float)
    return vector / np.linalg.norm(vector)


def seen(*vectors: np.ndarray, box=(0.1, 0.1, 0.4, 0.4)) -> list:
    """The faces of one frame: anything with an `.embedding` (and a `.box`) will do."""
    return [SimpleNamespace(embedding=vector, box=box) for vector in vectors]


ALICE_REFS = np.stack([unit(1, 0), unit(1, 0.1), unit(1, -0.1)])
ALICE_FACE = unit(1, 0.02)
STRANGER_FACE = unit(0, 1)


def alice(threshold: float = 0.8, can_auto: bool = True) -> PersonModel:
    return PersonModel(1, "Alice", ALICE_REFS, threshold, can_auto)


def test_similarity_is_the_mean_of_the_top_three_references():
    references = np.stack([unit(1, 0), unit(0, 1), unit(1, 1), unit(-1, 0)])
    # similarities to (1, 0): 1.0, 0.0, 0.707, -1.0 -> top three are 1.0, 0.707, 0.0
    assert face_similarity(unit(1, 0), references) == pytest.approx((1.0 + 0.7071 + 0.0) / 3, abs=1e-3)


@pytest.mark.parametrize("hits, expected", [(0, Decision.NONE), (1, Decision.REVIEW), (2, Decision.AUTO), (5, Decision.AUTO)])
def test_decision_tiers(hits, expected):
    assert decide(hits, min_frames=2, can_auto=True) == expected


def test_thin_references_never_reach_auto():
    assert decide(10, min_frames=2, can_auto=False) == Decision.REVIEW
    assert decide(0, min_frames=2, can_auto=False) == Decision.NONE


def test_enough_matching_frames_is_auto():
    frames = [seen(ALICE_FACE), seen(STRANGER_FACE), seen(ALICE_FACE, STRANGER_FACE)]
    (match,) = match_video(frames, [alice()])
    assert (match.name, match.hit_frames, match.decision) == ("Alice", 2, Decision.AUTO)


def test_a_single_matching_frame_goes_to_review():
    (match,) = match_video([seen(ALICE_FACE), seen(STRANGER_FACE)], [alice()])
    assert (match.hit_frames, match.decision) == (1, Decision.REVIEW)


def test_only_strangers_is_none():
    (match,) = match_video([seen(STRANGER_FACE)] * 3, [alice()])
    assert (match.hit_frames, match.decision) == (0, Decision.NONE)


def test_video_without_faces_has_no_score():
    (match,) = match_video([[], []], [alice()])
    assert (match.best, match.decision) == (None, Decision.NONE)


def test_every_person_is_scored_against_their_own_threshold():
    bob = PersonModel(2, "Bob", np.stack([unit(0, 1)]), 0.8)
    strict_carol = PersonModel(3, "Carol", ALICE_REFS, 0.999999)
    matches = match_video([seen(ALICE_FACE)] * 2, [alice(), bob, strict_carol])
    assert [(m.name, m.decision) for m in matches] == [
        ("Alice", Decision.AUTO), ("Bob", Decision.NONE), ("Carol", Decision.NONE)]


def test_a_face_counts_only_for_the_person_it_resembles_most():
    alice_like_bob = PersonModel(2, "Bob", np.stack([unit(1, 0.6)]), 0.5)  # Alice's face also clears Bob's bar
    matches = match_video([seen(ALICE_FACE)] * 3, [alice(), alice_like_bob])
    assert [(m.name, m.hit_frames) for m in matches] == [("Alice", 3), ("Bob", 0)]
    assert matches[1].best > 0.5  # the similarity is still reported


def test_two_people_in_one_frame_each_get_their_hit():
    bob = PersonModel(2, "Bob", np.stack([STRANGER_FACE]), 0.8)
    matches = match_video([seen(ALICE_FACE, STRANGER_FACE)] * 2, [alice(), bob])
    assert [(m.name, m.hit_frames, m.decision) for m in matches] == [
        ("Alice", 2, Decision.AUTO), ("Bob", 2, Decision.AUTO)]


def test_the_best_sighting_is_the_most_similar_face_in_the_video():
    good_box = (0.5, 0.2, 0.7, 0.4)
    frames = [seen(STRANGER_FACE), seen(unit(1, 0.5), box=(0.0, 0.0, 0.1, 0.1)), seen(ALICE_FACE, box=good_box)]
    sighting = best_sighting(frames, alice())
    assert (sighting.frame_index, sighting.box) == (2, good_box)


def test_no_faces_means_no_sighting():
    assert best_sighting([[], []], alice()) is None
