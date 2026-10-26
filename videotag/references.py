"""Reference faces per person, derived only from what Synology Photos already knows.

Seeds: photos Photos lists exactly one person on (with one face found), plus each person's cover crop.
Group photos then give more faces: each face goes to the listed person it resembles, if unambiguous.
A cover crop is only a seed; the face found in the photo it was cut from replaces it.
"""
import logging
from dataclasses import dataclass, field

import numpy as np

from . import refcache
from .assignment import assign_faces
from .faces import FaceEngine
from .library import cover_source_photo, fetch, fetch_cover, listed_people
from .runlog import references_message
from .synology import PhotosClient

log = logging.getLogger("videotag")
PROGRESS_LINES = 100  # the counter writes about this many lines, however many photos there are


@dataclass(frozen=True)
class Source:
    kind: str                    # "single" | "group" | "cover"
    photo_id: int | None
    score: float | None = None   # group photos: the similarity that justified the assignment


@dataclass
class ReferenceSet:
    person_id: int
    name: str
    embeddings: np.ndarray = field(default_factory=lambda: np.empty((0, 128)))
    sources: list[Source] = field(default_factory=list)  # parallel to `embeddings`

    @property
    def size(self) -> int:
        return len(self.sources)


def _faces_in(client: PhotosClient, engine: FaceEngine, photos: list[dict]) -> dict[int, list[np.ndarray]]:
    """The face embeddings of every photo, from the saved faces where the photo is unchanged. Logs a counter."""
    step = max(1, len(photos) // PROGRESS_LINES)
    log.info(references_message(0, len(photos)))
    found, reused = {}, 0
    for done, photo in enumerate(photos, start=1):
        faces = refcache.load(photo)
        if faces is None:
            faces = engine.embeddings(fetch(client, photo))
            refcache.save(photo, faces)
        else:
            reused += 1
        found[photo["id"]] = faces
        if done % step == 0 or done == len(photos):
            log.info(references_message(done, len(photos)))
    log.info("references: %d photos read, %d taken from the saved faces", len(photos) - reused, reused)
    return found


def build_references(client: PhotosClient, engine: FaceEngine, photos: list[dict]) -> list[ReferenceSet]:
    people = {person["id"]: person for person in client.persons() if person.get("name")}
    named = [photo for photo in photos if any(p["id"] in people for p in listed_people(photo))]
    faces_by_photo = _faces_in(client, engine, named)
    found: dict[int, dict[int, tuple[np.ndarray, Source]]] = {pid: {} for pid in people}  # person -> photo -> face

    single = set()
    for photo in photos:
        listed, faces = listed_people(photo), faces_by_photo.get(photo["id"], [])
        if len(listed) == 1 and listed[0]["id"] in people and len(faces) == 1:
            found[listed[0]["id"]][photo["id"]] = (faces[0], Source("single", photo["id"]))
            single.add(photo["id"])

    for pid, person in people.items():
        cover = fetch_cover(client, person)
        face = engine.main_face(cover) if cover else None
        if face is not None:
            source_photo = cover_source_photo(person)
            found[pid].setdefault(source_photo if source_photo is not None else -pid, (face, Source("cover", source_photo)))

    seeds = {pid: np.stack([face for face, _ in entries.values()]) for pid, entries in found.items() if entries}
    for photo in photos:
        if photo["id"] in single:
            continue
        candidates = {p["id"]: seeds[p["id"]] for p in listed_people(photo) if p["id"] in seeds}
        faces = faces_by_photo.get(photo["id"], [])
        for pid, (index, score) in assign_faces(faces, candidates).items():
            found[pid][photo["id"]] = (faces[index], Source("group", photo["id"], score))  # replaces a cover from this photo

    sets = []
    for pid, entries in found.items():
        if entries:
            faces = np.stack([face for face, _ in entries.values()])
            sets.append(ReferenceSet(pid, people[pid]["name"], faces, [source for _, source in entries.values()]))
    return sets
