"""A per-person album that shows the person's photos together with their matched videos.

Photos cannot add a video to a person's People page (membership is face based), but an album can hold both.
The album mirrors the tags: it holds the person's photos and every video carrying the plain `Name` tag
(confident matches and ones the user confirmed, never `Name?` suggestions). Edit tags, not the album:
a video taken out of the album by hand is added again, a video whose tag is gone leaves the album.
Photos are only ever added; only videos are removed.
"""
import logging
from dataclasses import dataclass

from .library import listed_people
from .state import State
from .synology import PhotosClient, SynologyError
from .tagging import TagAction, confident_videos

CHUNK = 200
log = logging.getLogger("videotag")


@dataclass(frozen=True)
class AlbumPlan:
    person_id: int
    name: str
    create: bool
    add: tuple[int, ...]
    remove: tuple[int, ...]


def plan_album_changes(people: dict[int, str], photos: list[dict], videos: list[dict], tag_actions: list[TagAction],
                       existing: dict[int, set[int] | None]) -> list[AlbumPlan]:
    """`existing` maps a person to the item ids in their album, or None when the album does not exist yet."""
    video_ids = {video["id"] for video in videos}
    plans = []
    for person_id, name in people.items():
        wanted_videos = confident_videos(person_id, name, videos, tag_actions)
        current = existing.get(person_id)
        if current is None and not wanted_videos:
            continue  # an album with only photos would add nothing to the People page
        person_photos = {photo["id"] for photo in photos if any(p["id"] == person_id for p in listed_people(photo))}
        add = sorted((wanted_videos | person_photos) - (current or set()))
        remove = sorted(((current or set()) & video_ids) - wanted_videos)
        if current is None or add or remove:
            plans.append(AlbumPlan(person_id, name, current is None, tuple(add), tuple(remove)))
    return plans


def current_albums(client: PhotosClient, state: State, people: dict[int, str]) -> dict[int, set[int] | None]:
    live = {album["id"] for album in client.albums()}
    return {person_id: client.album_item_ids(state.albums[person_id]) if state.albums.get(person_id) in live else None
            for person_id in people}


def execute_albums(client: PhotosClient, state: State, plans: list[AlbumPlan], budget: int) -> int:
    """Create albums and move items, at most `budget` items in total. Returns how many items were moved."""
    done = 0
    for plan in plans:
        if done >= budget:
            log.warning("write limit of %d reached, album changes left for the next run", budget)
            break
        try:
            if plan.create:
                state.albums[plan.person_id] = client.create_album(plan.name)
                state.save()
                log.info("created album '%s'", plan.name)
            album_id = state.albums[plan.person_id]
            moves = ((plan.add, client.add_to_album, "added to"), (plan.remove, client.remove_from_album, "removed from"))
            for ids, change, verb in moves:
                allowed = ids[:max(budget - done, 0)]
                for start in range(0, len(allowed), CHUNK):
                    change(album_id, list(allowed[start:start + CHUNK]))
                if allowed:
                    log.info("%d item(s) %s album '%s'", len(allowed), verb, plan.name)
                done += len(allowed)
                if len(allowed) < len(ids):
                    log.warning("write limit of %d reached, album '%s' is partly updated", budget, plan.name)
                    return done
        except SynologyError as exc:
            log.error("album '%s' failed: %s", plan.name, exc)
    return done


def delete_albums(client: PhotosClient, state: State) -> int:
    """Delete every album this tool created. The items in them stay in the library."""
    deleted = 0
    for person_id, album_id in list(state.albums.items()):
        try:
            client.delete_album(album_id)
        except SynologyError as exc:
            log.error("deleting album %d failed: %s", album_id, exc)
            continue
        del state.albums[person_id]
        state.save()
        deleted += 1
    return deleted
