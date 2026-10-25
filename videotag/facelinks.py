"""Linking matched videos to the person in Photos itself, so they appear in the person's People view.

Photos finds no faces in videos, but its manual "tag a person" feature stores a face record (position, crop,
person) on any item. This tool creates exactly one such record per confidently matched video and person,
remembers its id, and deletes it again when the match is rejected or on undo. The position is where the
detector saw the person best.

Photos' replies are not trusted blindly: a video whose thumbnail is unusable gets error 117 from `add_face`
and the face is stored anyway, and deleting one of two duplicate faces of the same person drops the video from
the person's view. So the tool never creates a second face for a person, and checks what was really stored.
"""
import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from .matcher import Sighting
from .state import State
from .synology import PhotosClient, SynologyError

log = logging.getLogger("videotag")


@dataclass(frozen=True)
class FaceAction:
    kind: str                # "add" or "remove"
    video_id: int
    person_id: int
    face_id: int | None = None  # known for removals


def sync_face_feedback(state: State, faces_by_video: dict[int, list[dict]]) -> None:
    """A face record this tool created that is gone from its video was removed by the user: a rejection."""
    for (video_id, person_id), face_id in list(state.faces.items()):
        faces = faces_by_video.get(video_id)
        if faces is not None and face_id not in {face["face_id"] for face in faces}:
            del state.faces[(video_id, person_id)]
            state.rejected.add((video_id, person_id))


def plan_face_changes(desired: set[tuple[int, int]], state: State, faces_by_video: dict[int, list[dict]]) -> list[FaceAction]:
    """`desired` holds the (video, person) pairs that should be linked; `faces_by_video` what Photos has now."""
    actions = []
    for key in sorted(desired - set(state.faces)):
        video_id, person_id = key
        already_linked = any(face["person_id"] == person_id for face in faces_by_video.get(video_id, []))
        if key not in state.rejected and not already_linked:
            actions.append(FaceAction("add", video_id, person_id))
    for (video_id, person_id), face_id in sorted(state.faces.items()):
        if (video_id, person_id) not in desired:
            actions.append(FaceAction("remove", video_id, person_id, face_id))
    return actions


def _add(client: PhotosClient, action: FaceAction, sighting: Sighting, crop: Callable[[], bytes]) -> int:
    before = {face["face_id"] for face in client.item_faces(action.video_id)}
    try:
        client.add_face(action.video_id, action.person_id, sighting.box)
    except SynologyError as exc:
        log.info("add_face on video %d answered code %s, checking what was stored", action.video_id, exc.code)
    stored = [face for face in client.item_faces(action.video_id)
              if face["face_id"] not in before and face["person_id"] == action.person_id]
    if not stored:
        raise SynologyError(f"no face was stored on video {action.video_id}")
    face_id = stored[0]["face_id"]
    try:
        client.upload_face_crop(face_id, crop())
    except SynologyError as exc:
        log.warning("crop upload for face %d failed: %s", face_id, exc)
    return face_id


def execute_faces(client: PhotosClient, state: State, actions: list[FaceAction],
                  sightings: dict[tuple[int, int], Sighting], crop_for: Callable[[int, Sighting], bytes],
                  budget: int) -> int:
    """Perform at most `budget` actions, saving the state after each. Returns how many were done."""
    done = 0
    for action in actions:
        key = (action.video_id, action.person_id)
        if done >= budget:
            log.warning("write limit of %d reached, %d face change(s) left for the next run", budget, len(actions) - done)
            break
        try:
            if action.kind == "remove":
                client.delete_face(action.face_id, action.person_id)
                del state.faces[key]
            else:
                sighting = sightings[key]
                state.faces[key] = _add(client, action, sighting, partial(crop_for, action.video_id, sighting))
        except SynologyError as exc:
            log.error("%s face on video %d failed: %s", action.kind, action.video_id, exc)
            continue
        state.save()
        done += 1
        log.info("%s face of person %d on video %d", action.kind, action.person_id, action.video_id)
    return done


def verify_links(client: PhotosClient, state: State) -> None:
    """Warn about videos this tool linked that Photos does not list for the person."""
    for person_id in sorted({person for _, person in state.faces}):
        listed = client.person_item_ids(person_id)
        for video_id in sorted(video for video, person in state.faces if person == person_id and video not in listed):
            log.warning("video %d is linked to person %d but Photos does not list it for them", video_id, person_id)


def delete_faces(client: PhotosClient, state: State) -> int:
    """Delete every face record this tool created."""
    deleted = 0
    for (video_id, person_id), face_id in list(state.faces.items()):
        try:
            client.delete_face(face_id, person_id)
        except SynologyError as exc:
            log.error("deleting face %d failed: %s", face_id, exc)
            continue
        del state.faces[(video_id, person_id)]
        state.save()
        deleted += 1
    return deleted
