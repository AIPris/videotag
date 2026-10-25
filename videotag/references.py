"""Reference faces per person, derived only from what Synology Photos already knows.

Seeds: photos Photos lists exactly one person on (with one face found), plus each person's cover crop.
Group photos then give more faces: each face goes to the listed person it resembles, if unambiguous.
A cover crop is only a seed; the face found in the photo it was cut from replaces it.
"""
from dataclasses import dataclass, field

import numpy as np

from .assignment import assign_faces
from .faces import FaceEngine
from .library import cover_source_photo, fetch, fetch_cover, listed_people
from .synology import PhotosClient


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


def build_references(client: PhotosClient, engine: FaceEngine, photos: list[dict]) -> list[ReferenceSet]:
    people = {person["id"]: person for person in client.persons() if person.get("name")}
    faces_by_photo = {photo["id"]: engine.embeddings(fetch(client, photo))
                      for photo in photos if any(p["id"] in people for p in listed_people(photo))}
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
