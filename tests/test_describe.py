import json
from pathlib import Path

import pytest

from videotag.describe import Description, evenly, parse_description


def answer(**overrides) -> str:
    data = {
        "summary": {"en": "A black cat explores a Christmas tree.", "de": "Eine schwarze Katze erkundet einen Weihnachtsbaum."},
        "topics": {"en": ["christmas", "cat"], "de": ["Weihnachten", "Katze"]},
        "objects": {"en": ["cat", "christmas tree"], "de": ["Katze", "Weihnachtsbaum"]},
        "place": None,
    }
    data.update(overrides)
    return json.dumps(data)


def test_a_complete_answer_is_parsed():
    description = parse_description(answer())
    assert description.summary_de == "Eine schwarze Katze erkundet einen Weihnachtsbaum."
    assert description.topics_en == ("christmas", "cat") and description.objects_de == ("Katze", "Weihnachtsbaum")
    assert description.place is None


def test_text_around_the_json_is_ignored():
    assert parse_description("Sure! Here you go:\n```json\n" + answer() + "\n```") is not None


@pytest.mark.parametrize("text", ["not json at all", "[]", "{}", '{"summary": "just a string"}',
                                  json.dumps({"summary": {"en": "only english"}})])
def test_unusable_answers_give_nothing(text):
    assert parse_description(text) is None


def test_a_place_needs_a_name_and_visible_evidence():
    kept = parse_description(answer(place={"name": "Tokyo", "evidence": "Warner Bros. Studio Tour Tokyo logo"}))
    assert (kept.place, kept.place_evidence) == ("Tokyo", "Warner Bros. Studio Tour Tokyo logo")


@pytest.mark.parametrize("place", [
    {"name": "Paris", "evidence": "no recognizable landmarks"},   # the model rating the absence of evidence
    {"name": "Paris", "evidence": ""},
    {"name": "null", "evidence": "a sign"},
    {"name": None, "evidence": "a sign"},
    "Paris",
])
def test_a_place_without_real_evidence_is_dropped(place):
    assert parse_description(answer(place=place)).place is None


def test_lists_are_cleaned_deduplicated_and_limited():
    many = [f"object {i}" for i in range(20)]
    description = parse_description(answer(objects={"en": ["cat", " cat ", "", 5, *many], "de": ["Katze"]}))
    assert description.objects_en[0] == "cat" and description.objects_en.count("cat") == 1
    assert len(description.objects_en) == 8


def test_a_description_survives_the_cache_round_trip():
    description = parse_description(answer(place={"name": "Tokyo", "evidence": "a logo"}))
    assert Description.from_dict(json.loads(json.dumps(description.to_dict()))) == description


def test_evenly_spreads_frames_over_the_whole_video():
    frames = [Path(f"f{i}.jpg") for i in range(11)]
    assert evenly(frames, 3) == [frames[0], frames[5], frames[10]]
    assert evenly(frames[:2], 6) == frames[:2]
