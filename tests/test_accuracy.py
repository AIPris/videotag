import json
from datetime import datetime

import numpy as np
import pytest

from videotag.accuracy import report, save
from videotag.calibration import person_models
from videotag.references import ReferenceSet, Source


def unit(*values):
    vector = np.array(values, dtype=float)
    return vector / np.linalg.norm(vector)


def people(*sets, threshold=None):
    return list(sets), person_models(list(sets), threshold)


def refs(person_id, name, vectors, kinds=None):
    kinds = kinds or ["single"] * len(vectors)
    return ReferenceSet(person_id, name, np.stack(vectors), [Source(kind, 100 + i) for i, kind in enumerate(kinds)])


ALICE = [unit(1, 0.0, 0), unit(1, 0.1, 0), unit(1, -0.1, 0), unit(1, 0.05, 0)]
BOB = [unit(0, 1, 0.0), unit(0, 1, 0.1), unit(0, 1, -0.1), unit(0, 1, 0.05)]


def test_people_who_look_nothing_alike_are_well_separated_and_never_mixed_up():
    sets, models = people(refs(1, "Alice", ALICE), refs(2, "Bob", BOB))
    result = report(sets, models)
    alice = result["people"][0]
    assert (alice["person_id"], alice["name"], alice["references"]) == (1, "Alice", 4)
    assert alice["same_person"]["n"] == 4 and alice["others"]["n"] == 4
    assert alice["same_person"]["min"] > 0.9 and alice["others"]["max"] < 0.1
    assert alice["separation"] == pytest.approx(alice["same_person"]["mean"] - alice["others"]["max"])
    assert result["mixed_up"] == []


def test_the_threshold_and_whether_confident_tags_are_allowed_are_the_ones_the_run_uses():
    sets, models = people(refs(1, "Alice", ALICE), refs(2, "Bob", BOB[:2]))   # Bob has too few references for confident tags
    alice, bob = report(sets, models)["people"]
    assert alice["threshold"] == pytest.approx(models[0].threshold) and alice["confident_allowed"] is True
    assert bob["confident_allowed"] is False


def test_the_kinds_of_reference_are_counted():
    sets, models = people(refs(1, "Alice", ALICE, ["single", "single", "group", "cover"]), refs(2, "Bob", BOB))
    assert report(sets, models)["people"][0]["kinds"] == {"single": 2, "group": 1, "cover": 1}


def test_a_person_with_one_reference_cannot_be_compared_with_themselves():
    sets, models = people(refs(1, "Alice", ALICE[:1]), refs(2, "Bob", BOB))
    alice = report(sets, models)["people"][0]
    assert alice["same_person"] == {"n": 0, "min": None, "mean": None, "max": None} and alice["separation"] is None
    assert alice["recognised"] == {"n": 0, "of": 0}  # nothing to judge it by


def test_a_person_alone_has_nobody_to_be_told_apart_from():
    sets, models = people(refs(1, "Alice", ALICE))
    alice = report(sets, models)["people"][0]
    assert alice["others"]["n"] == 0 and alice["separation"] is None


BROTHER = [unit(1, 0.12, 0), unit(1, 0.2, 0), unit(1, 0.0, 0.05), unit(1, 0.3, 0)]  # looks like Alice


def test_with_the_calibrated_thresholds_nobody_is_mixed_up_because_each_is_set_above_the_best_lookalike():
    sets, models = people(refs(1, "Alice", ALICE), refs(2, "Brother", BROTHER))
    assert report(sets, models)["mixed_up"] == []


def test_with_a_threshold_that_is_too_lenient_the_pairs_who_get_mixed_up_are_listed_with_how_many_faces_it_concerns():
    sets, models = people(refs(1, "Alice", ALICE), refs(2, "Brother", BROTHER), threshold=0.5)
    mixed = report(sets, models)["mixed_up"]
    assert mixed and all(pair["person"] != pair["taken_for"] for pair in mixed)
    assert mixed == sorted(mixed, key=lambda pair: (-pair["faces"], pair["person"]))
    assert all(0 < pair["faces"] <= pair["of"] == 4 for pair in mixed)


def test_people_whose_faces_resemble_each_other_are_listed_closest_first_and_strangers_are_not():
    carol = [unit(0, 0, 1), unit(0, 0.1, 1), unit(0, -0.1, 1), unit(0, 0.05, 1)]
    sets, models = people(refs(1, "Alice", ALICE), refs(2, "Brother", BROTHER), refs(3, "Carol", carol))
    lookalikes = report(sets, models)["lookalikes"]
    assert {(pair["person"], pair["resembles"]) for pair in lookalikes} == {("Alice", "Brother"), ("Brother", "Alice")}
    assert lookalikes == sorted(lookalikes, key=lambda pair: -pair["similarity"])
    assert all(pair["similarity"] > 0.9 and 0 < pair["faces"] <= 4 for pair in lookalikes)


def test_how_many_of_a_persons_own_faces_would_be_recognised_at_their_threshold():
    sets, models = people(refs(1, "Alice", ALICE), refs(2, "Bob", BOB))
    assert report(sets, models)["people"][0]["recognised"] == {"n": 4, "of": 4}
    alike_sets, alike_models = people(refs(1, "Alice", ALICE), refs(2, "Brother", BROTHER))
    assert report(alike_sets, alike_models)["people"][0]["recognised"] == {"n": 0, "of": 4}  # the bar is above any lookalike


def test_the_report_is_plain_data_that_can_be_written_as_json(tmp_path):
    sets, models = people(refs(1, "Alice", ALICE), refs(2, "Bob", BOB))
    path = tmp_path / "reports" / "accuracy.json"
    save(path, report(sets, models), clock=lambda: datetime(2026, 10, 26, 12, 0, 0))
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["time"] == "2026-10-26T12:00:00" and [p["name"] for p in saved["people"]] == ["Alice", "Bob"]
    assert [entry.name for entry in path.parent.iterdir()] == ["accuracy.json"]
