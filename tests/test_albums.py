from videotag.albums import ALBUM_NAME, AlbumPlan, delete_album, execute_album, plan_album_changes
from videotag.config import NO_LIMIT
from videotag.state import State
from videotag.synology import SynologyError
from videotag.tagging import TagAction

ALICE = 1
PEOPLE = {ALICE: "Alice"}


def video(item_id, *tag_names):
    return {"id": item_id, "additional": {"tag": [{"id": i, "name": name} for i, name in enumerate(tag_names)]}}


def new_review(item_id):
    return TagAction("add", item_id, ALICE, "Alice?", "review")


def plan(videos, actions=(), existing=None):
    """`existing=None` is an album that does not exist yet."""
    return plan_album_changes(PEOPLE, videos, list(actions), existing)


def test_a_suggested_video_creates_the_album_with_that_video():
    assert plan([video(20, "Alice?"), video(21)]) == AlbumPlan(True, (20,), ())


def test_a_suggestion_about_to_be_written_counts():
    assert plan([video(20)], [new_review(20)]) == AlbumPlan(True, (20,), ())


def test_without_suggestions_there_is_no_album():
    assert plan([video(20), video(21, "Alice")]) is None


def test_a_confident_video_does_not_belong_in_the_album():
    assert plan([video(20, "Alice")], existing={20}) == AlbumPlan(False, (), (20,))


def test_only_videos_without_a_suggestion_leave_the_album_and_other_items_stay():
    existing = {20, 21, 99}  # 99 is a photo the user put there themselves; 21 lost its suggestion
    assert plan([video(20, "Alice?"), video(21)], existing=existing) == AlbumPlan(False, (), (21,))


def test_a_video_leaves_the_album_when_its_suggestion_is_upgraded():
    upgrade = [TagAction("remove", 20, ALICE, "Alice?", "review", 0), TagAction("add", 20, ALICE, "Alice", "auto")]
    assert plan([video(20, "Alice?")], upgrade, existing={20}) == AlbumPlan(False, (), (20,))


def test_an_up_to_date_album_needs_no_change():
    assert plan([video(20, "Alice?")], existing={20}) is None


def test_an_empty_album_with_nothing_to_add_is_left_alone():
    assert plan([video(20)], existing=set()) is None


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
    moved = execute_album(client, state, AlbumPlan(True, (10, 20), ()), budget=50)
    assert moved == 2 and client.created == [ALBUM_NAME] and client.added == [(501, [10, 20])]
    assert State.load(tmp_path / "state.json").review_album == 501


def test_no_plan_moves_nothing(tmp_path):
    client = FakeClient()
    assert execute_album(client, State(tmp_path / "state.json"), None, budget=50) == 0
    assert client.created == [] and client.added == [] and client.removed == []


def test_the_write_limit_cuts_an_album_update_short(tmp_path):
    client, state = FakeClient(), State(tmp_path / "state.json", review_album=7)
    moved = execute_album(client, state, AlbumPlan(False, (1, 2, 3, 4), (9,)), budget=3)
    assert moved == 3 and client.added == [(7, [1, 2, 3])] and client.removed == []


def test_no_limit_moves_every_item(tmp_path):
    client, state = FakeClient(), State(tmp_path / "state.json", review_album=7)
    moved = execute_album(client, state, AlbumPlan(False, (1, 2, 3, 4), (9,)), budget=NO_LIMIT)
    assert moved == 5 and client.added == [(7, [1, 2, 3, 4])] and client.removed == [(7, [9])]


def test_removals_and_additions_both_reach_an_existing_album(tmp_path):
    client, state = FakeClient(), State(tmp_path / "state.json", review_album=7)
    execute_album(client, state, AlbumPlan(False, (5,), (9,)), budget=10)
    assert client.added == [(7, [5])] and client.removed == [(7, [9])]


def test_deleting_the_album_forgets_it(tmp_path):
    client, state = FakeClient(), State(tmp_path / "state.json", review_album=501)
    assert delete_album(client, state) == 1
    assert client.deleted == [501] and state.review_album is None


def test_an_album_that_could_not_be_deleted_stays_on_record(tmp_path):
    client, state = FakeClient(), State(tmp_path / "state.json", review_album=666)
    assert delete_album(client, state) == 0
    assert state.review_album == 666


def test_deleting_without_an_album_on_record_does_nothing(tmp_path):
    client = FakeClient()
    assert delete_album(client, State(tmp_path / "state.json")) == 0
    assert client.deleted == []
