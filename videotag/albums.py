"""One album, "to check", that lists every video carrying a `Name?` suggestion, so they can be worked through in one place.

Photos has no inbox for suggestions, but an album can hold videos. The album mirrors the suggestion tags: a video joins
when it gets a `Name?` tag and leaves when the last one is gone (the user confirmed it by adding `Name`, or rejected it by
removing the tag). Edit tags, not the album: a video taken out by hand is added again while its suggestion stands.
Only videos are ever removed, so anything else the user puts into the album stays.
"""
import logging
from dataclasses import dataclass

from .state import State
from .synology import PhotosClient, SynologyError
from .tagging import TagAction, suggested_videos

ALBUM_NAME = "videotag: to check"
CHUNK = 200
log = logging.getLogger("videotag")


@dataclass(frozen=True)
class AlbumPlan:
    create: bool
    add: tuple[int, ...]
    remove: tuple[int, ...]


def plan_album_changes(people: dict[int, str], videos: list[dict], tag_actions: list[TagAction],
                       existing: set[int] | None) -> AlbumPlan | None:
    """`existing` holds the item ids in the album, or None when the album does not exist yet. None is returned when
    nothing has to change."""
    wanted = suggested_videos(people, videos, tag_actions)
    if existing is None and not wanted:
        return None  # no album just to have an empty one
    current = existing or set()
    add = sorted(wanted - current)
    remove = sorted((current & {video["id"] for video in videos}) - wanted)
    if existing is None or add or remove:
        return AlbumPlan(existing is None, tuple(add), tuple(remove))
    return None


def current_album(client: PhotosClient, state: State) -> set[int] | None:
    """The item ids in the album this tool made, or None when there is none (never made, or deleted by the user)."""
    live = {album["id"] for album in client.albums()}
    return client.album_item_ids(state.review_album) if state.review_album in live else None


def execute_album(client: PhotosClient, state: State, plan: AlbumPlan | None, budget: int) -> int:
    """Create the album and move items, at most `budget` items in all. Returns how many items were moved."""
    if plan is None or budget <= 0:
        return 0
    done = 0
    try:
        if plan.create:
            state.review_album = client.create_album(ALBUM_NAME)
            state.save()
            log.info("created album '%s'", ALBUM_NAME)
        moves = ((plan.add, client.add_to_album, "added to"), (plan.remove, client.remove_from_album, "removed from"))
        for ids, change, verb in moves:
            allowed = ids[:max(budget - done, 0)]
            for start in range(0, len(allowed), CHUNK):
                change(state.review_album, list(allowed[start:start + CHUNK]))
            if allowed:
                log.info("%d item(s) %s album '%s'", len(allowed), verb, ALBUM_NAME)
            done += len(allowed)
            if len(allowed) < len(ids):
                log.warning("write limit of %d reached, album '%s' is partly updated", budget, ALBUM_NAME)
                break
    except SynologyError as exc:
        log.error("album '%s' failed: %s", ALBUM_NAME, exc)
    return done


def delete_album(client: PhotosClient, state: State) -> int:
    """Delete the album this tool created, if there is one on record. The items in it stay in the library."""
    if state.review_album is None:
        return 0
    try:
        client.delete_album(state.review_album)
    except SynologyError as exc:
        log.error("deleting album %d failed: %s", state.review_album, exc)
        return 0
    state.review_album = None
    state.save()
    return 1
