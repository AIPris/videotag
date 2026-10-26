import random

import pytest

from videotag.spotcheck import sample
from videotag.state import State


def match(person_id, name, decision):
    return {"person_id": person_id, "name": name, "best": 0.5, "hit_frames": 3, "decision": decision}


def row(video_id, *matches, **extra):
    return {"video_id": video_id, "filename": f"clip{video_id}.mp4", "frames": 10, "faces": 5, "matches": list(matches), **extra}


ROWS = ([row(n, match(7, "Alice", "auto")) for n in range(1, 21)]
        + [row(n, match(7, "Alice", "review")) for n in range(21, 31)]
        + [row(n, match(8, "Bob", "auto")) for n in range(31, 34)]
        + [row(n, match(7, "Alice", "none"), ) for n in range(34, 40)])


def draw(rows=ROWS, state=None, count=5, seed=1):
    return sample(rows, state or State(), count, random.Random(seed))


def test_each_person_gets_a_few_confident_matches_and_a_few_suggestions_to_look_at():
    alice, bob = draw()
    assert (alice["name"], bob["name"]) == ("Alice", "Bob")
    assert alice["confident"]["total"] == 20 and len(alice["confident"]["sample"]) == 5
    assert alice["suggested"]["total"] == 10 and len(alice["suggested"]["sample"]) == 5
    assert bob["confident"]["total"] == 3 and len(bob["confident"]["sample"]) == 3   # fewer than asked for: all of them
    assert bob["suggested"] == {"total": 0, "sample": []}


def test_the_sample_comes_from_the_right_group_and_carries_what_is_needed_to_find_the_video_in_photos():
    alice = draw()[0]
    assert all(1 <= item["video_id"] <= 20 for item in alice["confident"]["sample"])
    assert all(21 <= item["video_id"] <= 30 for item in alice["suggested"]["sample"])
    assert all(item["filename"] == f"clip{item['video_id']}.mp4" for item in alice["confident"]["sample"])


def test_a_sample_never_names_the_same_video_twice():
    ids = [item["video_id"] for item in draw(count=20)[0]["confident"]["sample"]]
    assert len(ids) == len(set(ids)) == 20


def test_the_same_seed_gives_the_same_sample_and_another_one_can_differ():
    assert draw(seed=3) == draw(seed=3)
    assert any(draw(seed=seed) != draw(seed=3) for seed in range(4, 12))


def test_what_you_already_confirmed_or_removed_is_not_asked_about_again():
    state = State(confirmed={(n, 7) for n in range(1, 11)}, rejected={(n, 7) for n in range(11, 20)})
    alice = draw(state=state, count=20)[0]
    assert [item["video_id"] for item in alice["confident"]["sample"]] == [20] and alice["confident"]["total"] == 1


def test_a_copy_of_a_file_is_not_a_second_thing_to_check_and_a_failed_video_is_not_a_match():
    rows = [row(1, match(7, "Alice", "auto")), row(2, match(7, "Alice", "auto"), twin_of=1),
            {"video_id": 3, "filename": "broken.mp4", "error": "download refused"}]
    (alice,) = draw(rows)
    assert [item["video_id"] for item in alice["confident"]["sample"]] == [1] and alice["confident"]["total"] == 1


def test_a_person_nobody_matched_is_left_out():
    assert [person["name"] for person in draw([row(1, match(7, "Alice", "none"))])] == []


def test_no_report_means_nothing_to_sample():
    assert draw([]) == []


@pytest.mark.parametrize("count", [0, -1])
def test_asking_for_fewer_than_one_is_a_mistake(count):
    with pytest.raises(ValueError, match="at least one"):
        draw(count=count)


def test_each_sampled_video_carries_its_folder_so_the_page_can_link_to_it():
    rows = [row(1, match(7, "Alice", "auto"), folder_id=640), row(2, match(7, "Alice", "auto"))]
    (alice,) = draw(rows)
    assert {item["video_id"]: item["folder_id"] for item in alice["confident"]["sample"]} == {1: 640, 2: None}
