"""One unattended pass: read Photos, match every video, learn from edits, write tags, faces and albums."""
import logging
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from . import accuracy
from .albums import current_albums, execute_albums, plan_album_changes
from .analysis import analyse_videos, is_analysed
from .calibration import person_models
from .describe import Describer
from .descriptions import execute_descriptions, plan_description_changes, sync_description_feedback
from .evidence import Evidence
from .facelinks import execute_faces, plan_face_changes, sync_face_feedback, verify_links
from .faces import FaceEngine
from .library import load_library
from .live import LiveRun
from .matcher import best_sighting, match_video
from .references import build_references
from .runlog import slice_message
from .sampling import Sampling
from .scanner import frame_crop
from .slices import Slice, SliceError, album_video_ids, pick_videos
from .state import State
from .synology import PhotosClient
from .tagging import confident_videos, execute, plan_tag_changes, sync_feedback

log = logging.getLogger("videotag")


@dataclass(frozen=True)
class RunOptions:
    apply: bool
    sampling: Sampling              # how new videos are cut into frames; saved results keep the rule they were made under
    min_frames: int
    max_writes: int
    threshold: float | None = None  # overrides the calibrated per-person thresholds
    describe: bool = True           # also write bilingual descriptions (needs the vision-language model)
    reanalyse: bool = False         # redo the saved results that were made under another sampling rule
    slice: Slice = Slice()          # which videos a dry run looks at; a real run always covers the whole library

    def __post_init__(self) -> None:
        if self.apply and self.slice.narrows:
            raise SliceError("a slice is for a dry run: a real run always covers the whole library")


def _videos_to_analyse(videos: list[dict], options: RunOptions, album: set[int] | None) -> list[dict]:
    chosen = options.slice
    if not chosen.narrows:
        return videos
    picked = pick_videos(videos, chosen, album_item_ids=album, analysed=lambda video: is_analysed(video, options.describe))
    log.info(slice_message(len(picked), len(videos)))  # runlog.py counts progress against this
    return picked


def run(client: PhotosClient, engine: FaceEngine, describer: Describer | None, options: RunOptions,
        state: State, live: LiveRun | None = None, evidence_dir: Path | None = None,
        accuracy_file: Path | None = None) -> list[dict]:
    """Returns one report row per video. A dry run changes nothing in Photos and saves no state.

    Each row also goes to `live`, if given, as soon as it is made. With `evidence_dir` the best faces of every match are
    kept there as crops (they stay on this PC), and `accuracy_file` receives the accuracy report of this run's people."""
    library = load_library(client)
    chosen = options.slice
    album = album_video_ids(client, state, chosen.album_id) if chosen.album_id is not None else None  # refuse a bad album early
    sets = build_references(client, engine, library.photos)
    models = person_models(sets, options.threshold)
    for model in models:
        log.info("%s: %d references, threshold %.3f, confident tags %s", model.name, len(model.references),
                 model.threshold, "allowed" if model.can_auto else "not allowed (suggestions only)")
    if accuracy_file is not None:
        accuracy.save(accuracy_file, accuracy.report(sets, models))
    evidence = Evidence(models, options.min_frames, evidence_dir) if evidence_dir is not None else None
    log.info("library: %d photos, %d videos", len(library.photos), len(library.videos))  # cue for runlog.py: analysis starts
    videos = _videos_to_analyse(library.videos, options, album)

    people = {model.person_id: model.name for model in models}
    sync_feedback(state, library.videos, people)
    faces_by_video = {video_id: client.item_faces(video_id) for video_id in {video for video, _ in state.faces}}
    sync_face_feedback(state, faces_by_video)
    sync_description_feedback(state, library.videos)

    rows, matches, frame_faces_by_video, descriptions, sampling_by_video = [], {}, {}, {}, {}

    def record(row: dict) -> None:
        rows.append(row)
        if live is not None:
            live.add(row)

    if live is not None:
        live.begin()
    with closing(analyse_videos(client, engine, describer, videos, options.sampling, reanalyse=options.reanalyse,
                                evidence=evidence)) as outcomes:
        for outcome in outcomes:
            video = outcome.video
            row = {"video_id": video["id"], "filename": video["filename"]}
            if "folder_id" in video:
                row["folder_id"] = video["folder_id"]  # with the id, enough to link to the video in the Photos web app
            if outcome.twin_of is not None:
                row["twin_of"] = outcome.twin_of
            if outcome.error is not None:
                log.error("%s %s: %s", video["id"], video["filename"], outcome.error)
                record({**row, "error": str(outcome.error)})
                continue
            frame_faces, description = outcome.faces, outcome.description
            frame_faces_by_video[video["id"]] = frame_faces
            sampling_by_video[video["id"]] = outcome.sampling
            if description is not None:
                descriptions[video["id"]] = description
            matches[video["id"]] = match_video(frame_faces, models, options.min_frames)
            record({**row, "frames": len(frame_faces), "faces": sum(len(f) for f in frame_faces),
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
        crop_for = lambda video_id, sighting: frame_crop(  # noqa: E731 - the frame index is only good under the rule that counted it
            client, videos_by_id[video_id], sampling_by_video[video_id], sighting)
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
