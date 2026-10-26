"""Which videos a dry run looks at: the whole library, or a slice of it.

A slice is for finding out what the tool would do on a few videos without waiting for all of them, so it exists for dry
runs only. A real run (`--apply`) always covers the whole library. The choices can be combined: the album narrows
first, then "not analysed yet", then the limit takes the first videos of what is left, in library order.
"""
from collections.abc import Callable
from dataclasses import dataclass

from .state import State
from .synology import PhotosClient


class SliceError(RuntimeError):
    """The slice cannot be used; the message says why and is safe to show."""


@dataclass(frozen=True)
class Slice:
    limit: int | None = None       # at most this many videos
    new_only: bool = False         # skip videos that already have a saved result, at any sampling rule
    album_id: int | None = None    # only videos of this album, one of the user's own

    def __post_init__(self) -> None:
        if self.limit is not None and (not isinstance(self.limit, int) or isinstance(self.limit, bool) or self.limit < 1):
            raise SliceError("the limit must be a positive whole number")
        if self.album_id is not None and (not isinstance(self.album_id, int) or isinstance(self.album_id, bool)
                                          or self.album_id < 1):
            raise SliceError("the album must be given by its number (`videotag albums` lists them)")

    @property
    def narrows(self) -> bool:
        return self.limit is not None or self.new_only or self.album_id is not None


def own_albums(client: PhotosClient, state: State) -> list[dict]:
    """The user's albums. The "to check" album this tool created is not among them."""
    return [album for album in client.albums() if album["id"] != state.review_album]


def album_video_ids(client: PhotosClient, state: State, album_id: int) -> set[int]:
    if album_id not in {album["id"] for album in own_albums(client, state)}:
        raise SliceError(f"there is no album {album_id} of yours (the album this tool made does not count); "
                         "`videotag albums` lists yours")
    return client.album_item_ids(album_id)


def pick_videos(videos: list[dict], chosen: Slice, *, album_item_ids: set[int] | None,
                analysed: Callable[[dict], bool]) -> list[dict]:
    """`album_item_ids` holds the items of the album when the slice names one; `analysed` says if a video has its results."""
    if chosen.album_id is not None and album_item_ids is None:
        raise SliceError("an album slice needs the album's contents")
    picked = [video for video in videos if album_item_ids is None or video["id"] in album_item_ids]
    if chosen.new_only:
        picked = [video for video in picked if not analysed(video)]
    return picked if chosen.limit is None else picked[:chosen.limit]
