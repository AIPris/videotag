import re

import numpy as np
import pytest

from videotag import refcache, references
from videotag.runlog import last_run, parse_lines

PEOPLE = [{"id": 1, "name": "Alice", "additional": {}}, {"id": 2, "name": "Bob", "additional": {}}, {"id": 3, "name": ""}]


def photo(number, people, size=100):
    return {"id": number, "filename": f"p{number}.jpg", "filesize": size, "time": 5,
            "additional": {"person": [{"id": p} for p in people]}}


def vector(person, variant=0):
    """A face of a person: people point in different directions, the variants of one person are close to each other."""
    value = np.zeros(128, np.float32)
    value[person] = 1.0
    value[10 + variant % 100] += 0.2
    return value / np.linalg.norm(value)


class World:
    """A NAS with a few photos and a face engine that finds the faces listed in `faces`, counting what is downloaded and read."""

    def __init__(self):
        self.downloaded, self.read = [], []
        self.faces = {}  # photo id -> embeddings the engine finds in it
        self.engine = self

    def persons(self):
        return PEOPLE

    def fetched(self, client, item):
        self.downloaded.append(item["id"])
        return item["id"]  # stands in for the downloaded file

    def embeddings(self, path):
        self.read.append(path)
        return list(self.faces.get(path, []))

    def main_face(self, path):
        return None


@pytest.fixture
def world(monkeypatch, tmp_path):
    fakes = World()
    monkeypatch.setattr(refcache, "REFS_DIR", tmp_path / "reffaces")
    monkeypatch.setattr(references, "fetch", fakes.fetched)
    monkeypatch.setattr(references, "fetch_cover", lambda client, person: None)
    fakes.faces = {11: [vector(1, 0)], 12: [vector(1, 1)], 13: [vector(3, 0)], 14: [vector(1, 2), vector(3, 1)], 15: []}
    return fakes


PHOTOS = [photo(11, [1]), photo(12, [1]), photo(13, [2]), photo(14, [1, 2]), photo(15, [2]), photo(16, [])]


def build(world, photos=PHOTOS):
    return references.build_references(world, world.engine, photos)


def same(first, second):
    assert [(r.person_id, r.name, r.sources) for r in first] == [(r.person_id, r.name, r.sources) for r in second]
    assert all(np.array_equal(a.embeddings, b.embeddings) and a.embeddings.dtype == b.embeddings.dtype
               for a, b in zip(first, second, strict=True))


def test_the_first_run_reads_the_photos_of_named_people_and_nobody_elses(world):
    sets = build(world)
    assert sorted(world.downloaded) == [11, 12, 13, 14, 15] and sorted(world.read) == [11, 12, 13, 14, 15]  # 16 lists nobody
    assert {s.name: s.size for s in sets} == {"Alice": 3, "Bob": 2}  # 11, 12, group 14 for Alice; 13, group 14 for Bob


def test_a_second_run_downloads_nothing_reads_nothing_and_builds_the_same_references(world):
    first = build(world)
    world.downloaded.clear()
    world.read.clear()
    second = build(world)
    assert world.downloaded == [] and world.read == []
    same(first, second)


def test_the_saved_faces_are_exactly_the_faces_a_fresh_build_finds(world, tmp_path, monkeypatch):
    first = build(world)
    monkeypatch.setattr(refcache, "REFS_DIR", tmp_path / "other-empty-folder")
    same(first, build(world))  # nothing saved there: everything is read again, and the result is the same
    same(first, build(world))  # and this one comes from the saved faces


def test_a_photo_that_changed_is_read_again_and_the_others_are_not(world):
    build(world)
    world.downloaded.clear()
    world.read.clear()
    world.faces[12] = [vector(1, 7)]
    build(world, [photo(12, [1], size=999) if p["id"] == 12 else p for p in PHOTOS])
    assert world.downloaded == [12] and world.read == [12]


def test_a_photo_without_faces_is_remembered_as_having_none(world):
    build(world)
    world.read.clear()
    build(world)
    assert 15 not in world.read  # photo 15 listed Bob but held no face; that is not asked again


def test_a_person_named_later_costs_only_their_own_photos(world):
    build(world, [p for p in PHOTOS if p["id"] in (11, 12)])
    world.downloaded.clear()
    build(world)
    assert sorted(world.downloaded) == [13, 14, 15]


def test_a_saved_entry_that_cannot_be_read_is_replaced_and_not_a_crash(world):
    build(world)
    (refcache.REFS_DIR / "11.npz").write_bytes(b"half a file")
    world.read.clear()
    sets = build(world)
    assert world.read == [11] and {s.name: s.size for s in sets}["Alice"] == 3


def test_faces_saved_for_other_face_models_are_not_used(world, monkeypatch):
    build(world)
    monkeypatch.setattr(refcache, "MODEL_VERSION", "other-models")
    world.read.clear()
    build(world)
    assert sorted(world.read) == [11, 12, 13, 14, 15]


# -- the counter in the log ----------------------------------------------------------------------------

def progress_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("references: ") and " of " in r.getMessage()]


def test_the_counter_starts_at_zero_ends_at_the_total_and_only_goes_up(world, caplog):
    with caplog.at_level("INFO", logger="videotag"):
        build(world)
    lines = progress_lines(caplog)
    done = [int(re.match(r"references: (\d+) of 5 photos", line)[1]) for line in lines]
    assert done[0] == 0 and done[-1] == 5 and done == sorted(done)


def test_a_long_phase_logs_about_a_hundred_lines_not_one_per_photo(world, caplog):
    many = [photo(1000 + n, [1]) for n in range(1000)]
    world.faces.update({1000 + n: [vector(1, n)] for n in range(1000)})
    with caplog.at_level("INFO", logger="videotag"):
        build(world, many)
    assert 90 <= len(progress_lines(caplog)) <= 115


def test_the_log_of_the_phase_is_understood_by_the_run_log_reader_and_none_of_it_looks_like_a_video(world, caplog):
    with caplog.at_level("INFO", logger="videotag"):
        build(world)
    text = "\n".join(f"2026-10-26 10:00:{n:02d},000 INFO {record.getMessage()}" for n, record in enumerate(caplog.records))
    text = "2026-10-26 09:59:59,000 INFO run started: dry run\n" + text
    run = last_run(parse_lines(text), parse_lines(text)[-1].time)
    assert run.references == (5, 5) and run.done == 0 and run.errors == 0
    assert not [r for r in caplog.records if r.getMessage()[:1].isdigit()]


def test_the_summary_says_how_many_photos_came_from_the_saved_faces(world, caplog):
    build(world)
    with caplog.at_level("INFO", logger="videotag"):
        build(world)
    assert "references: 0 photos read, 5 taken from the saved faces" in caplog.text
