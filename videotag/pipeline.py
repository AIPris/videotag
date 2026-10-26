"""One unattended pass: read Photos, match every video, learn from edits, write tags, faces and albums."""
import logging
from contextlib import closing
from dataclasses import dataclass

from .albums import current_albums, execute_albums, plan_album_changes
from .analysis import analyse_videos
from .calibration import person_models
from .describe import Describer
from .descriptions import execute_descriptions, plan_description_changes, sync_description_feedback
from .facelinks import execute_faces, plan_face_changes, sync_face_feedback, verify_links
from .faces import FaceEngine
from .library import load_library
from .matcher import best_sighting, match_video
from .references import build_references
from .scanner import frame_crop
from .state import State
from .synology import PhotosClient
from .tagging import confident_videos, execute, plan_tag_changes, sync_feedback

log = logging.getLogger("videotag")


@dataclass(frozen=True)
class RunOptions:
    apply: bool
    fps: float
    min_frames: int
    max_writes: int
    threshold: float | None = None  # overrides the calibrated per-person thresholds
    describe: bool = True           # also write bilingual descriptions (needs the vision-language model)


def run(client: PhotosClient, engine: FaceEngine, describer: Describer | None, options: RunOptions,
        state: State) -> list[dict]:
    """Returns one report row per video. A dry run changes nothing in Photos and saves no state."""
    library = load_library(client)
    sets = build_references(client, engine, library.photos)
    models = person_models(sets, options.threshold)
    for model in models:
        log.info("%s: %d references, threshold %.3f, confident tags %s", model.name, len(model.references),
                 model.threshold, "allowed" if model.can_auto else "not allowed (suggestions only)")
    log.info("library: %d photos, %d videos", len(library.photos), len(library.videos))  # cue for runlog.py: analysis starts

    people = {model.person_id: model.name for model in models}
    sync_feedback(state, library.videos, people)
    faces_by_video = {video_id: client.item_faces(video_id) for video_id in {video for video, _ in state.faces}}
    sync_face_feedback(state, faces_by_video)
    sync_description_feedback(state, library.videos)

    rows, matches, frame_faces_by_video, descriptions = [], {}, {}, {}
    with closing(analyse_videos(client, engine, describer, library.videos, options.fps)) as outcomes:
        for outcome in outcomes:
            video = outcome.video
            row = {"video_id": video["id"], "filename": video["filename"]}
            if outcome.error is not None:
                log.error("%s %s: %s", video["id"], video["filename"], outcome.error)
                rows.append({**row, "error": str(outcome.error)})
                continue
            frame_faces, description = outcome.faces, outcome.description
            frame_faces_by_video[video["id"]] = frame_faces
            if description is not None:
                descriptions[video["id"]] = description
            matches[video["id"]] = match_video(frame_faces, models, options.min_frames)
            rows.append({**row, "frames": len(frame_faces), "faces": sum(len(f) for f in frame_faces),
                         "description": description.summary_en if description else None,
                         "place": description.place if description else None,
                         "matches": [{"person_id": m.person_id, "name": m.name, "best": m.best,
                                      "hit_frames": m.hit_frames, "decision": m.decision.value}
                                     for m in matches[video["id"]]]})
            log.info("%s %s frames=%d faces=%d | %s", video["id"], video["filename"][:40], rows[-1]["frames"], rows[-1]["faces"],
                     " ".join(f"{m.name}={'-' if m.best is None else f'{m.best:.2f}'}/{m.hit_frames}:{m.decision.value}"
                              for m in matches[video["id"]]))

    actions = plan_tag_changes(state, library.videos, matches)
    log.info("%d tag change(s) planned (%d confident, %d suggestions, %d removals)", len(actions),
             sum(a.kind == "add" and a.level == "auto" for a in actions),
             sum(a.kind == "add" and a.level == "review" for a in actions), sum(a.kind == "remove" for a in actions))
    for action in actions:
        log.info("plan: %s '%s' on item %d", action.kind, action.tag_name, action.item_id)

    desired = {(video_id, person_id) for person_id, name in people.items()
               for video_id in confident_videos(person_id, name, library.videos, actions)}
    for video_id in {video for video, _ in desired} - set(faces_by_video):
        faces_by_video[video_id] = client.item_faces(video_id)
    face_actions = plan_face_changes(desired, state, faces_by_video)
    for face_action in face_actions:
        log.info("plan: %s face of person %d on video %d", face_action.kind, face_action.person_id, face_action.video_id)

    models_by_id = {model.person_id: model for model in models}
    sightings = {(a.video_id, a.person_id): best_sighting(frame_faces_by_video[a.video_id], models_by_id[a.person_id])
                 for a in face_actions if a.kind == "add" and a.video_id in frame_faces_by_video}
    face_actions = [a for a in face_actions if a.kind == "remove" or (a.video_id, a.person_id) in sightings]

    description_actions = plan_description_changes(state, library.videos, descriptions)
    for description_action in description_actions:
        log.info("plan: description of video %d (%d characters)", description_action.video_id,
                 len(description_action.block))

    album_plans = plan_album_changes(people, library.photos, library.videos, actions,
                                     current_albums(client, state, people))
    for plan in album_plans:
        log.info("plan: album '%s' %s, +%d -%d item(s)", plan.name, "created" if plan.create else "updated",
                 len(plan.add), len(plan.remove))

    if options.apply:
        done = execute(client, state, actions, options.max_writes)
        state.save()  # also keeps what the feedback steps learned
        log.info("%d tag change(s) written", done)
        videos_by_id = {video["id"]: video for video in library.videos}
        crop_for = lambda video_id, sighting: frame_crop(client, videos_by_id[video_id], options.fps, sighting)  # noqa: E731
        linked = execute_faces(client, state, face_actions, sightings, crop_for, options.max_writes)
        log.info("%d face change(s) written", linked)
        verify_links(client, state)
        written = execute_descriptions(client, state, description_actions, options.max_writes)
        log.info("%d description(s) written", written)
        moved = execute_albums(client, state, album_plans, options.max_writes)
        log.info("%d album item change(s) written", moved)
    else:
        log.info("dry run: nothing written, pass --apply to write")
    return rows
