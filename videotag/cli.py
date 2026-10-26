"""Command line: run, undo, refs, calibrate, credentials, doctor, gui."""
import argparse
import json
import logging
import statistics
from collections import Counter
from contextlib import nullcontext
from datetime import datetime
from logging.handlers import RotatingFileHandler

from .accounts import AccountError, claim_data
from .admin import (
    cmd_credentials_delete,
    cmd_credentials_migrate,
    cmd_credentials_set,
    cmd_credentials_status,
    cmd_doctor,
)
from .albums import delete_albums
from .calibration import calibrate_person, leave_one_out
from .config import DEFAULT_MAX_WRITES, DEFAULT_PORT, LOG_BACKUPS, LOG_FILE, LOG_MAX_BYTES, REPORTS_DIR
from .credentials import CredentialError, load_credentials
from .describe import Describer
from .descriptions import delete_descriptions
from .facelinks import delete_faces
from .faces import FaceEngine
from .library import load_library
from .lock import AlreadyRunning, single_run
from .matcher import DEFAULT_MATCH, DEFAULT_MIN_FRAMES
from .pipeline import RunOptions, run
from .references import build_references
from .runlog import started_message
from .state import State, StateError
from .synology import PhotosClient, SynologyError
from .tagging import undo_all


def _setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"),
    ])


def _spread(values: list[float]) -> str:
    if not values:
        return "none"
    return f"n={len(values)} min={min(values):.3f} mean={statistics.fmean(values):.3f} max={max(values):.3f}"


def _prepare(client: PhotosClient):
    engine = FaceEngine()
    library = load_library(client)
    return engine, library, build_references(client, engine, library.photos)


def cmd_run(client: PhotosClient, args) -> None:
    options = RunOptions(args.apply, args.fps, args.min_frames, args.max_writes, args.threshold, not args.no_describe)
    rows = run(client, FaceEngine(), Describer() if options.describe else None, options, State.load())
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report = REPORTS_DIR / f"run-{datetime.now():%Y%m%d-%H%M%S}.json"
    report.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    logging.getLogger("videotag").info("report: %s", report)


def cmd_undo(client: PhotosClient, args) -> None:
    state = State.load()
    removed = undo_all(client, state)
    unlinked = delete_faces(client, state)
    cleared = delete_descriptions(client, state)
    deleted = delete_albums(client, state)
    logging.getLogger("videotag").info(
        "removed %d tag(s), %d face link(s), %d description block(s) and %d album(s) this tool had created",
        removed, unlinked, cleared, deleted)


def cmd_refs(client: PhotosClient, args) -> None:
    _, library, sets = _prepare(client)
    print(f"library: {len(library.photos)} photos, {len(library.videos)} videos")
    for refs in sets:
        kinds = Counter(source.kind for source in refs.sources)
        print(f"{refs.name} (person {refs.person_id}): {refs.size} reference faces "
              f"({kinds['single']} single, {kinds['group']} group, {kinds['cover']} cover)")
        scores = leave_one_out(refs.embeddings) if refs.size > 1 else [float("nan")] * refs.size
        for source, loo in zip(refs.sources, scores, strict=True):
            detail = "" if source.score is None else f" assigned at {source.score:.3f}"
            print(f"    {source.kind:<6} photo {source.photo_id}{detail}  leave-one-out {loo:.3f}")


def cmd_calibrate(client: PhotosClient, args) -> None:
    engine, library, sets = _prepare(client)
    photos_by_id = {photo["id"]: photo for photo in library.photos}
    for refs in sets:
        if refs.size < 2:
            print(f"{refs.name}: only {refs.size} reference face, cannot calibrate")
            continue
        result = calibrate_person(client, engine, refs, sets, photos_by_id, args.fps)
        confident = "allowed" if result.can_auto else "NOT allowed: too few"
        print(f"== {result.name} ({refs.size} references, confident tags {confident})")
        print(f"same person (leave-one-out): {_spread(result.positives)}")
        print(f"other named people         : {_spread(result.strangers)}")
        print(f"calibrated threshold       : {result.threshold:.3f}")
        control = result.video_control
        if control is None:
            print("video control              : skipped (needs 3 single-face photos to hold out)")
        else:
            best = "no face found" if control.best is None else f"best {control.best:.3f}"
            print(f"video control              : {control.decision.value} ({best}, {control.hit_frames} matching frames)")


def cmd_gui(client, args) -> int:
    from .dashboard import Sources
    from .gui import serve  # imported here so that headless runs never load the web server
    return serve(Sources(), args.port, not args.no_browser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="videotag", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run", help="one unattended pass over all videos; dry run unless --apply")
    run_parser.add_argument("--apply", action="store_true", help="write tags to Photos (default: report only)")
    run_parser.add_argument("--fps", type=float, default=2.0, help="frames sampled per second of video")
    run_parser.add_argument("--min-frames", type=int, default=DEFAULT_MIN_FRAMES,
                            help="matching frames needed for a confident tag")
    run_parser.add_argument("--max-writes", type=int, default=DEFAULT_MAX_WRITES, help="most tag changes written per run")
    run_parser.add_argument("--threshold", type=float, default=None,
                            help=f"use this similarity for everyone instead of the calibrated values (floor is {DEFAULT_MATCH})")
    run_parser.add_argument("--no-describe", action="store_true",
                            help="skip the bilingual descriptions (the vision-language model is not loaded)")
    run_parser.set_defaults(run=cmd_run, unattended=True)

    commands.add_parser("undo", help="remove every tag, face link, description block and album this tool created"
                        ).set_defaults(run=cmd_undo, unattended=True)
    commands.add_parser("refs", help="show the reference faces found for each named person").set_defaults(run=cmd_refs)
    calibrate = commands.add_parser("calibrate", help="check the matcher against known data")
    calibrate.add_argument("--fps", type=float, default=2.0, help="frames sampled per second of video")
    calibrate.set_defaults(run=cmd_calibrate)

    gui = commands.add_parser("gui", help="optional local web dashboard: progress, results, health (runs fine without it)")
    gui.add_argument("--port", type=int, default=DEFAULT_PORT, help="port on 127.0.0.1 (0 picks a free one)")
    gui.add_argument("--no-browser", action="store_true", help="do not open the page in your browser")
    gui.set_defaults(run=cmd_gui, needs_client=False)

    commands.add_parser("doctor", help="check that everything an unattended run needs is in place"
                        ).set_defaults(run=cmd_doctor, needs_client=False)
    credentials = commands.add_parser("credentials", help="store, inspect or remove the NAS login")
    actions = credentials.add_subparsers(dest="action", required=True)
    actions.add_parser("set", help="type in the login; it is verified, then stored in the OS credential store"
                       ).set_defaults(run=cmd_credentials_set, needs_client=False)
    actions.add_parser("status", help="show where the login comes from (never the password)"
                       ).set_defaults(run=cmd_credentials_status, needs_client=False)
    actions.add_parser("delete", help="remove the stored login").set_defaults(run=cmd_credentials_delete, needs_client=False)
    migrate = actions.add_parser("migrate", help="move the login from an old plaintext creds.txt into the credential store")
    migrate.add_argument("file", help="the old creds.txt (Username:, Passwort:, Domain:)")
    migrate.add_argument("--delete", action="store_true", help="delete the file once the stored login has been verified")
    migrate.set_defaults(run=cmd_credentials_migrate, needs_client=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    unattended = getattr(args, "unattended", False)
    if unattended:
        _setup_logging()
    try:
        with single_run() if unattended else nullcontext():
            if args.command == "run":  # before the login, so a run that dies early still shows up as a stopped run
                logging.getLogger("videotag").info(started_message(args.apply))
            if not getattr(args, "needs_client", True):
                return int(args.run(None, args) or 0)
            credentials, _ = load_credentials()
            claim_data(credentials)  # before any call to the NAS: the saved data must belong to this account
            with PhotosClient(credentials) as client:
                return int(args.run(client, args) or 0)
    except AlreadyRunning as exc:
        print(f"skipped: {exc}")
        return 2
    except (SynologyError, StateError, CredentialError, AccountError) as exc:
        logging.getLogger("videotag").error("%s", exc)
        print(f"error: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - an unattended run's crash must reach the log, not only a terminal nobody watches
        if unattended:
            logging.getLogger("videotag").exception("%s crashed: %s: %s", args.command, type(exc).__name__, exc)
        raise
