"""The faces that made a video match a person, kept on this PC so the dashboard can show why.

For every person a video matches (confidently or as a suggestion) the best few of their faces are cut out of the frames
and kept as `<video id>/<person id>-<n>.jpg`, best first. They are what the user looks at to judge a match, they open at
once, and they never leave this PC: nothing is uploaded, and a video without a match keeps none. Only a face the person
actually won counts (see `matcher.hit_sightings`), so a sibling's face is never offered as evidence of someone else.

The crops are cut from frames of the video, so they are only right when the frames are the ones the faces were found
in: the caller cuts the video under the sampling rule stored with the faces, and a frame count that does not agree with
the faces' is refused rather than guessed at. When crops cannot be made for a video's faces (the frames do not line up, or
a crop fails) that is written down (`skipped.txt` in the video's folder) so the video is not fetched again on every run; it
is tried again when the file or its faces change. A failed download is not written down: that is worth trying again.
"""
import contextlib
import hashlib
import json
import logging
import os
import re
from pathlib import Path

from .facecache import file_key
from .faces import crop_jpeg
from .matcher import Decision, PersonModel, claim, hit_sightings, match_video

log = logging.getLogger("videotag")

_CROP = re.compile(r"^(\d+)-(\d+)\.jpg$")
SKIPPED = "skipped.txt"  # in a video's folder: the faces for which no crops could be made
PROVENANCE = "provenance.json"


def crops(folder: Path, video_id: int) -> dict[int, list[str]]:
    """The crop files of a video by person id, best first. Nothing for a video without any."""
    found: dict[int, list[tuple[int, str]]] = {}
    try:
        names = os.listdir(folder / str(video_id))
    except FileNotFoundError:
        return {}
    for name in names:
        match = _CROP.match(name)
        if match:
            found.setdefault(int(match[1]), []).append((int(match[2]), name))
    return {person: [name for _, name in sorted(files)] for person, files in sorted(found.items())}


class Evidence:
    def __init__(self, models: list[PersonModel], min_frames: int, folder: Path):
        self._models, self._min_frames, self._folder = models, min_frames, folder

    def _wanted(self, faces: list[list]) -> dict[int, int]:
        """Person id to the person's place in the models, for everyone the video matches."""
        matches = match_video(faces, self._models, self._min_frames)
        return {match.person_id: index for index, match in enumerate(matches) if match.decision is not Decision.NONE}

    def missing(self, video: dict, faces: list[list]) -> bool:
        """Whether someone the video matches has no crops yet, so the video has to be cut to make them.
        Crops of people the video no longer matches are removed on the way: that needs no frames. Faces for which crops
        could not be made before count as done."""
        wanted, present = self._provenance(video, faces), crops(self._folder, video["id"])
        saved = self._read_provenance(video)
        for person_id in present.keys() - wanted.keys():
            self._remove(video["id"], present[person_id])
        outdated = {person for person, signature in wanted.items()
                    if saved.get(str(person)) != signature or person not in present}
        for person_id in outdated & present.keys():
            self._remove(video["id"], present[person_id])  # never show a crop of an old winner while rebuilding
        return bool(outdated) and not self._given_up(video, wanted)

    def _provenance(self, video: dict, faces: list[list]) -> dict[int, str]:
        """Bind each crop set to the file and the few actual winning faces, without hashing the whole video or cache."""
        found = {}
        for person_id, index in self._wanted(faces).items():
            sightings = []
            for sighting in hit_sightings(faces, self._models, index):
                embeddings = [face.embedding.tolist() for face in faces[sighting.frame_index]
                              if face.box == sighting.box and claim(face.embedding, self._models)[0] == index]
                sightings.append((sighting.frame_index, sighting.box, embeddings))
            identity = json.dumps([file_key(video), len(faces), sightings], sort_keys=True)
            found[person_id] = hashlib.sha256(identity.encode()).hexdigest()
        return found

    def _read_provenance(self, video: dict) -> dict:
        try:
            value = json.loads((self._folder / str(video["id"]) / PROVENANCE).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _given_up(self, video: dict, signatures: dict[int, str]) -> bool:
        try:
            saved = (self._folder / str(video["id"]) / SKIPPED).read_text(encoding="utf-8")
            return saved == json.dumps(signatures, sort_keys=True)
        except OSError:
            return False

    def _give_up(self, video: dict, faces: list[list]) -> None:
        folder = self._folder / str(video["id"])
        folder.mkdir(parents=True, exist_ok=True)
        for leftover in folder.glob("*.part"):
            leftover.unlink(missing_ok=True)
        self._remove(video["id"], [name for files in crops(self._folder, video["id"]).values() for name in files])
        folder.mkdir(parents=True, exist_ok=True)
        (folder / SKIPPED).write_text(json.dumps(self._provenance(video, faces), sort_keys=True), encoding="utf-8")

    def keep(self, video: dict, frames: list[Path], faces: list[list]) -> None:
        """Cut the crops of everyone the video matches, replacing any earlier ones."""
        if len(frames) != len(faces):
            log.warning("evidence of %s: the frames do not line up with the saved faces (%d against %d), no crops kept",
                        video["id"], len(frames), len(faces))
            self._give_up(video, faces)
            return
        folder, written = self._folder / str(video["id"]), []
        try:
            for person_id, index in self._wanted(faces).items():
                for number, sighting in enumerate(hit_sightings(faces, self._models, index), start=1):
                    name = f"{person_id}-{number}.jpg"
                    folder.mkdir(parents=True, exist_ok=True)
                    scratch = folder / (name + ".part")
                    scratch.write_bytes(crop_jpeg(frames[sighting.frame_index], sighting.box))
                    os.replace(scratch, folder / name)  # the dashboard never sees half a picture
                    written.append(name)
        except Exception as exc:  # noqa: BLE001 - a crop is a convenience, and one that fails once would fail on every run
            log.warning("evidence of %s could not be made: %s: %s", video["id"], type(exc).__name__, exc)
            self._give_up(video, faces)
            return
        (folder / SKIPPED).unlink(missing_ok=True)
        self._remove(video["id"], [name for files in crops(self._folder, video["id"]).values() for name in files
                                   if name not in written])
        if written:
            scratch = folder / (PROVENANCE + ".part")
            scratch.write_text(json.dumps(self._provenance(video, faces), sort_keys=True), encoding="utf-8")
            os.replace(scratch, folder / PROVENANCE)

    def _remove(self, video_id: int, names: list[str]) -> None:
        folder = self._folder / str(video_id)
        for name in names:
            (folder / name).unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            folder.rmdir()  # only when nothing else is in it
