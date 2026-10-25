import numpy as np

from videotag.assignment import assign_faces


def unit(*values: float) -> np.ndarray:
    vector = np.array(values, dtype=float)
    return vector / np.linalg.norm(vector)


A, B, C = unit(1, 0, 0), unit(0, 1, 0), unit(0, 0, 1)


def test_each_face_goes_to_the_person_it_resembles():
    result = assign_faces([B, A], {1: np.stack([A]), 2: np.stack([B])})
    assert {pid: face for pid, (face, _) in result.items()} == {1: 1, 2: 0}


def test_only_the_listed_people_are_candidates():
    # the second face is a stranger (C); with only person 1 listed it must stay unassigned
    result = assign_faces([A, C], {1: np.stack([A])})
    assert list(result) == [1] and result[1][0] == 0


def test_a_face_below_the_minimum_similarity_is_not_assigned():
    assert assign_faces([unit(1, 2, 2)], {1: np.stack([A])}) == {}  # cosine 0.33, below the 0.45 minimum


def test_two_near_identical_faces_are_ambiguous_so_neither_is_assigned():
    twin_a, twin_b = unit(1, 0.01, 0), unit(1, -0.01, 0)
    assert assign_faces([twin_a, twin_b], {1: np.stack([A])}) == {}


def test_nothing_to_do_without_faces_or_seeds():
    assert assign_faces([], {1: np.stack([A])}) == {}
    assert assign_faces([A], {}) == {}
