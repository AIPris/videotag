from videotag.albums import AlbumPlan, delete_albums, execute_albums, plan_album_changes
from videotag.state import State
from videotag.synology import SynologyError
from videotag.tagging import TagAction

ALICE = 1
PEOPLE = {ALICE: "Alice"}


def photo(item_id, *person_ids):
    return {"id": item_id, "additional": {"person": [{"id": pid} for pid in person_ids]}}


def video(item_id, *tag_names):
    return {"id": item_id, "additional": {"tag": [{"id": i, "name": name} for i, name in enumerate(tag_names)]}}


def new_auto(item_id):
    return TagAction("add", item_id, ALICE, "Alice", "auto")


def plan(photos, videos, actions=(), existing=None):
    return plan_album_changes(PEOPLE, photos, videos, list(actions), existing or {ALICE: None})


def test_a_matched_video_creates_the_album_with_the_persons_photos_and_the_video():
    (album,) = plan([photo(10, ALICE), photo(11, ALICE), photo(12)], [video(20)], [new_auto(20)])
    assert album == AlbumPlan(ALICE, "Alice", True, (10, 11, 20), ())


def test_a_person_without_matched_videos_gets_no_album():
    assert plan([photo(10, ALICE)], [video(20)]) == []


def test_suggestions_do_not_count():
    assert plan([photo(10, ALICE)], [video(20, "Alice?")]) == []


def test_a_video_the_user_tagged_with_the_plain_name_counts():
    (album,) = plan([photo(10, ALICE)], [video(20, "Alice")])
    assert album.add == (10, 20)


def test_only_videos_whose_tag_is_gone_leave_the_album_and_photos_stay():
    existing = {ALICE: {10, 99, 20, 21}}  # 99 is a photo the user added themselves; 21 lost its tag
    (album,) = plan([photo(10, ALICE)], [video(20, "Alice"), video(21)], existing=existing)
    assert (album.create, album.add, album.remove) == (False, (), (21,))


def test_an_up_to_date_album_needs_no_change():
    assert plan([photo(10, ALICE)], [video(20, "Alice")], existing={ALICE: {10, 20}}) == []


# -- executing --------------------------------------------------------------------------------

class FakeClient:
    def __init__(self):
        self.created, self.added, self.removed, self.deleted = [], [], [], []

    def create_album(self, name):
        self.created.append(name)
        return 500 + len(self.created)

    def add_to_album(self, album_id, ids):
        self.added.append((album_id, ids))

    def remove_from_album(self, album_id, ids):
        self.removed.append((album_id, ids))

    def delete_album(self, album_id):
        if album_id == 666:
            raise SynologyError("nope")
        self.deleted.append(album_id)


def test_executing_creates_the_album_remembers_its_id_and_adds_the_items(tmp_path):
    client, state = FakeClient(), State(tmp_path / "state.json")
    moved = execute_albums(client, state, [AlbumPlan(ALICE, "Alice", True, (10, 20), ())], budget=50)
    assert moved == 2 and client.created == ["Alice"] and client.added == [(501, [10, 20])]
    assert State.load(tmp_path / "state.json").albums == {ALICE: 501}


def test_the_write_limit_cuts_an_album_update_short(tmp_path):
    client, state = FakeClient(), State(tmp_path / "state.json", albums={ALICE: 7})
    moved = execute_albums(client, state, [AlbumPlan(ALICE, "Alice", False, (1, 2, 3, 4), (9,))], budget=3)
    assert moved == 3 and client.added == [(7, [1, 2, 3])] and client.removed == []


def test_removals_and_additions_both_reach_an_existing_album(tmp_path):
    client, state = FakeClient(), State(tmp_path / "state.json", albums={ALICE: 7})
    execute_albums(client, state, [AlbumPlan(ALICE, "Alice", False, (5,), (9,))], budget=10)
    assert client.added == [(7, [5])] and client.removed == [(7, [9])]


def test_deleting_albums_only_forgets_the_ones_that_were_deleted(tmp_path):
    client = FakeClient()
    state = State(tmp_path / "state.json", albums={1: 501, 2: 666})
    assert delete_albums(client, state) == 1
    assert client.deleted == [501] and state.albums == {2: 666}
