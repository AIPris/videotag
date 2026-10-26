import pytest

from videotag.slices import Slice, SliceError, own_albums, pick_videos
from videotag.state import State

VIDEOS = [{"id": number, "filename": f"clip{number}.mp4"} for number in range(1, 9)]


def ids(videos):
    return [video["id"] for video in videos]


def pick(chosen, analysed=(), album=None):
    return ids(pick_videos(VIDEOS, chosen, album_item_ids=album, analysed=lambda video: video["id"] in analysed))


def test_without_a_slice_every_video_is_looked_at():
    assert pick(Slice()) == [1, 2, 3, 4, 5, 6, 7, 8] and not Slice().narrows


def test_a_limit_takes_the_first_videos_in_library_order():
    assert pick(Slice(limit=3)) == [1, 2, 3] and Slice(limit=3).narrows


def test_a_limit_larger_than_the_library_takes_everything():
    assert pick(Slice(limit=100)) == [1, 2, 3, 4, 5, 6, 7, 8]


def test_not_analysed_yet_skips_videos_that_already_have_a_result_so_each_run_adds_more():
    assert pick(Slice(new_only=True), analysed={1, 2, 4}) == [3, 5, 6, 7, 8]
    assert pick(Slice(limit=2, new_only=True), analysed={1, 2, 4}) == [3, 5]


def test_an_album_keeps_only_its_videos_and_ignores_photos_in_it():
    assert pick(Slice(album_id=5), album={2, 6, 999}) == [2, 6]  # 999 is a photo, not in the video list


def test_an_empty_album_leaves_nothing_to_look_at():
    assert pick(Slice(album_id=5), album=set()) == []


def test_the_album_narrows_first_then_not_analysed_yet_then_the_limit():
    assert pick(Slice(limit=2, new_only=True, album_id=5), analysed={2, 3}, album={2, 3, 4, 6, 7}) == [4, 6]


@pytest.mark.parametrize("limit", [0, -1, 1.5, True])
def test_a_limit_that_is_not_a_positive_whole_number_is_refused(limit):
    with pytest.raises(SliceError, match="positive whole number"):
        Slice(limit=limit)


@pytest.mark.parametrize("album_id", [0, -4, "x", 2.5, True])
def test_an_album_that_is_not_a_positive_whole_number_is_refused(album_id):
    with pytest.raises(SliceError, match="album"):
        Slice(album_id=album_id)


def test_an_album_slice_cannot_be_applied_without_the_albums_contents():
    with pytest.raises(SliceError, match="album"):
        pick_videos(VIDEOS, Slice(album_id=5), album_item_ids=None, analysed=lambda video: False)


class AlbumClient:
    def albums(self):
        return [{"id": 5, "name": "Holiday"}, {"id": 900, "name": "Alice"}, {"id": 6, "name": "Pets"}]


def test_your_own_albums_exclude_the_one_this_tool_created():
    state = State(review_album=900)
    assert [album["name"] for album in own_albums(AlbumClient(), state)] == ["Holiday", "Pets"]
