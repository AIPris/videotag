"""Writing descriptions into Photos, where the item description is searchable.

The tool owns one marked block inside a video's description and leaves the user's own text around it alone:

    <the user's text>

    [vt]
    EN: <summary> | <topic and object keywords>
    DE: <Zusammenfassung> | <Schlagwörter>
    @ <place> (<evidence>)
    [/vt]

Search matches any part of the text, so the block carries no label words ("topics", "objects", "video" ...)
that every video would share: searching for those would return the whole library. The marker is short for the
same reason. Blocks written with the earlier marker `[videotag]` are still recognised, and get rewritten in the
current format, instead of looking as if the user had deleted them.

If the user deletes the block it counts as a rejection and is never written again; a block the user edited is
left alone. Undo removes only the block.
"""
import logging
import re
from dataclasses import dataclass

from .describe import Description
from .state import State
from .synology import PhotosClient, SynologyError

START, END = "[vt]", "[/vt]"
_BLOCK = re.compile(r"\[(?:vt|videotag)\].*?\[/(?:vt|videotag)\]", re.DOTALL)

log = logging.getLogger("videotag")


@dataclass(frozen=True)
class DescriptionAction:
    video_id: int
    text: str    # the complete new description
    block: str   # the part this tool owns


def _sentence(text: str) -> str:
    return text if text[-1] in ".!?" else text + "."


def _line(prefix: str, summary: str, topics: tuple[str, ...], objects: tuple[str, ...]) -> str:
    unique: dict[str, str] = {}
    for word in (*topics, *objects):  # topics first, no repeats
        unique.setdefault(word.lower(), word)
    keywords = list(unique.values())
    line = f"{prefix} {_sentence(summary)}"
    return f"{line} | {', '.join(keywords)}" if keywords else line


def render_block(description: Description) -> str:
    lines = [START,
             _line("EN:", description.summary_en, description.topics_en, description.objects_en),
             _line("DE:", description.summary_de, description.topics_de, description.objects_de)]
    if description.place:
        lines.append(f"@ {description.place} ({description.place_evidence})")
    lines.append(END)
    return "\n".join(lines)


def extract_block(text: str) -> str | None:
    found = _BLOCK.search(text)
    return found.group(0) if found else None


def merge_block(text: str, block: str) -> str:
    """Replace our block in `text`, or append it after the user's own text."""
    if extract_block(text) is not None:
        return _BLOCK.sub(lambda _: block, text, count=1)
    return f"{text.rstrip()}\n\n{block}" if text.strip() else block


def strip_block(text: str) -> str:
    return _BLOCK.sub("", text, count=1).strip()


def sync_description_feedback(state: State, videos: list[dict]) -> None:
    """A block this tool wrote that is gone from its description was deleted by the user: a rejection."""
    current = {video["id"]: video["additional"]["description"] for video in videos}
    for video_id in list(state.descriptions):
        text = current.get(video_id)
        if text is not None and extract_block(text) is None:
            del state.descriptions[video_id]
            state.description_rejected.add(video_id)


def plan_description_changes(state: State, videos: list[dict],
                             descriptions: dict[int, Description]) -> list[DescriptionAction]:
    actions = []
    for video in videos:
        description = descriptions.get(video["id"])
        if description is None or video["id"] in state.description_rejected:
            continue
        current, block = video["additional"]["description"], render_block(description)
        existing, written = extract_block(current), state.descriptions.get(video["id"])
        if existing is None or (existing == written and existing != block):
            actions.append(DescriptionAction(video["id"], merge_block(current, block), block))
        # otherwise it is up to date, or the user edited the block, which is theirs now
    return actions


def execute_descriptions(client: PhotosClient, state: State, actions: list[DescriptionAction], budget: int) -> int:
    """Write at most `budget` descriptions, saving the state after each. Returns how many were written."""
    done = 0
    for action in actions:
        if done >= budget:
            log.warning("write limit of %d reached, %d description(s) left for the next run", budget, len(actions) - done)
            break
        try:
            client.set_description(action.video_id, action.text)
        except SynologyError as exc:
            log.error("description of video %d failed: %s", action.video_id, exc)
            continue
        state.descriptions[action.video_id] = action.block
        state.save()
        done += 1
        log.info("wrote description of video %d", action.video_id)
    return done


def delete_descriptions(client: PhotosClient, state: State) -> int:
    """Remove every block this tool wrote, keeping whatever else is in the descriptions.

    A block the user edited is theirs by now and stays.
    """
    removed = 0
    for video_id, written in list(state.descriptions.items()):
        try:
            current = client.item_description(video_id)
            if extract_block(current) == written:
                client.set_description(video_id, strip_block(current))
        except SynologyError as exc:
            log.error("removing the description block of video %d failed: %s", video_id, exc)
            continue
        del state.descriptions[video_id]
        state.save()
        removed += 1
    return removed
