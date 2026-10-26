"""Which tags to add or remove for each video, and how edits made in Synology Photos are learned from.

Tags on a video, per person:
  `Name`   confident match (or the user's own tag)
  `Name?`  suggestion; the user confirms by adding `Name` in Photos
A tag this tool wrote that is later gone counts as a rejection and is never added again.
A `Name` tag the tool did not write counts as confirmed and is never touched.
"""
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from .matcher import Decision, PersonMatch
from .state import State, WrittenTag
from .synology import PhotosClient, SynologyError

REVIEW_SUFFIX = "?"

log = logging.getLogger("videotag")


@dataclass(frozen=True)
class TagAction:
    kind: str            # "add" or "remove"
    item_id: int
    person_id: int
    tag_name: str
    level: str           # "auto" or "review"
    tag_id: int | None = None  # known for removals


def _current_tags(video: dict) -> dict[int, str]:
    return {tag["id"]: tag["name"] for tag in video["additional"]["tag"]}


def confident_videos(person_id: int, name: str, videos: list[dict], tag_actions: list[TagAction]) -> set[int]:
    """Videos that carry the person's plain name tag now (ours or the user's), or are about to get it,
    minus the ones whose tag is about to be taken back."""
    tagged = {video["id"] for video in videos if name in _current_tags(video).values()}
    coming = {a.item_id for a in tag_actions if a.kind == "add" and a.level == "auto" and a.person_id == person_id}
    going = {a.item_id for a in tag_actions if a.kind == "remove" and a.person_id == person_id and a.tag_name == name}
    return (tagged | coming) - going


def suggested_videos(people: dict[int, str], videos: list[dict], tag_actions: list[TagAction]) -> set[int]:
    """Videos that carry a `Name?` suggestion of a known person now (ours or the user's), or are about to get one,
    minus the ones whose last suggestion is about to be taken back."""
    suggestions = {name + REVIEW_SUFFIX for name in people.values()}
    pending = {video["id"]: set(_current_tags(video).values()) & suggestions for video in videos}
    for action in tag_actions:
        if action.tag_name in suggestions and action.item_id in pending:
            if action.kind == "add":
                pending[action.item_id].add(action.tag_name)
            else:
                pending[action.item_id].discard(action.tag_name)
    return {video_id for video_id, tags in pending.items() if tags}


def sync_feedback(state: State, videos: list[dict], names: dict[int, str]) -> None:
    """Learn from tag edits made in Photos since the last run (`names` maps person id to name)."""
    current = {video["id"]: _current_tags(video) for video in videos}
    ours = {(tag.item_id, tag.tag_id) for tag in state.written}
    by_name = {name: person_id for person_id, name in names.items()}

    for item_id, tags in current.items():  # the user tagged a person themselves
        for tag_id, tag_name in tags.items():
            person_id = by_name.get(tag_name)
            if person_id is not None and (item_id, tag_id) not in ours:
                state.confirmed.add((item_id, person_id))
                state.rejected.discard((item_id, person_id))

    for tag in list(state.written):  # a tag we wrote is gone: rejected, unless it was replaced by the user's own
        tags = current.get(tag.item_id)
        if tags is not None and tag.tag_id not in tags:
            state.written.remove(tag)
            if (tag.item_id, tag.person_id) not in state.confirmed:
                state.rejected.add((tag.item_id, tag.person_id))


def plan_tag_changes(state: State, videos: list[dict], matches: dict[int, list[PersonMatch]]) -> list[TagAction]:
    actions = []
    ours_review = {(tag.item_id, tag.person_id): tag for tag in state.written if tag.level == "review"}
    for video in videos:
        names = set(_current_tags(video).values())
        for match in matches.get(video["id"], []):
            key = (video["id"], match.person_id)
            if match.decision is Decision.NONE or key in state.rejected or key in state.confirmed:
                continue
            review_name = match.name + REVIEW_SUFFIX
            if match.decision is Decision.AUTO and match.name not in names:
                if key in ours_review:  # upgrade our own suggestion
                    actions.append(TagAction("remove", *key, review_name, "review", ours_review[key].tag_id))
                actions.append(TagAction("add", *key, match.name, "auto"))
            elif match.decision is Decision.REVIEW and match.name not in names and review_name not in names:
                actions.append(TagAction("add", *key, review_name, "review"))
    for key, tag in ours_review.items():  # the user confirmed: our suggestion is now redundant
        if key in state.confirmed:
            actions.append(TagAction("remove", *key, tag.tag_name, "review", tag.tag_id))
    for tag in state.written:  # rejected some other way (e.g. the user removed the person from the video): take our tag back
        if (tag.item_id, tag.person_id) in state.rejected:
            actions.append(TagAction("remove", tag.item_id, tag.person_id, tag.tag_name, tag.level, tag.tag_id))
    return actions


def _tag_id(client: PhotosClient, name: str, known: dict[str, int]) -> int:
    if name not in known:
        known.update({tag["name"]: tag["id"] for tag in client.tags()})
    if name not in known:
        known[name] = client.create_tag(name)
    return known[name]


def execute(client: PhotosClient, state: State, actions: list[TagAction], max_writes: int) -> int:
    """Perform at most `max_writes` actions, saving the state after each. Returns how many were done."""
    known: dict[str, int] = {}
    done = 0
    for action in actions:
        if done >= max_writes:
            log.warning("write limit of %d reached, %d action(s) left for the next run", max_writes, len(actions) - done)
            break
        try:
            if action.kind == "remove":
                client.remove_tag(action.item_id, action.tag_id)
                state.written = [t for t in state.written if (t.item_id, t.tag_id) != (action.item_id, action.tag_id)]
            else:
                tag_id = _tag_id(client, action.tag_name, known)
                client.add_tag(action.item_id, tag_id)
                state.written.append(WrittenTag(action.item_id, action.person_id, tag_id, action.tag_name,
                                                action.level, datetime.now(UTC).isoformat()))
        except SynologyError as exc:
            log.error("%s '%s' on item %d failed: %s", action.kind, action.tag_name, action.item_id, exc)
            continue
        state.save()
        done += 1
        log.info("%s '%s' on item %d", action.kind, action.tag_name, action.item_id)
    return done


def undo_all(client: PhotosClient, state: State) -> int:
    """Remove every tag this tool still has on record. Rejections and confirmations are kept."""
    removed = 0
    for tag in list(state.written):
        try:
            client.remove_tag(tag.item_id, tag.tag_id)
        except SynologyError as exc:
            log.error("removing '%s' from item %d failed: %s", tag.tag_name, tag.item_id, exc)
            continue
        state.written.remove(tag)
        state.save()
        removed += 1
    return removed
