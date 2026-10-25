from videotag.matcher import Decision, PersonMatch
from videotag.state import State, WrittenTag
from videotag.synology import SynologyError
from videotag.tagging import TagAction, confident_videos, execute, plan_tag_changes, sync_feedback, undo_all

ALICE = 1


def video(item_id, *tags):
    return {"id": item_id, "filename": f"clip{item_id}.mp4",
            "additional": {"tag": [{"id": tag_id, "name": name} for tag_id, name in tags]}}


def match(decision, person_id=ALICE, name="Alice"):
    return PersonMatch(person_id, name, 0.7, 3, decision)


def new_auto(item_id):
    return TagAction("add", item_id, ALICE, "Alice", "auto")


def written(item_id, tag_id, name, level):
    return WrittenTag(item_id, ALICE, tag_id, name, level, "2026-01-01T00:00:00+00:00")


def plan(state, videos, decision):
    return plan_tag_changes(state, videos, {v["id"]: [match(decision)] for v in videos})


def summary(actions):
    return [(a.kind, a.item_id, a.tag_name, a.level) for a in actions]


class FakeClient:
    def __init__(self):
        self.created, self.added, self.removed, self.fail_removal_for = [], [], [], set()

    def tags(self):
        return []

    def create_tag(self, name):
        self.created.append(name)
        return 100 + len(self.created)

    def add_tag(self, item_id, tag_id):
        self.added.append((item_id, tag_id))

    def remove_tag(self, item_id, tag_id):
        if item_id in self.fail_removal_for:
            raise SynologyError("rejected")
        self.removed.append((item_id, tag_id))


# -- planning ---------------------------------------------------------------------------------

def test_a_confident_match_adds_the_plain_name():
    assert summary(plan(State(), [video(1)], Decision.AUTO)) == [("add", 1, "Alice", "auto")]


def test_an_unsure_match_adds_a_suggestion():
    assert summary(plan(State(), [video(1)], Decision.REVIEW)) == [("add", 1, "Alice?", "review")]


def test_no_match_adds_nothing():
    assert plan(State(), [video(1)], Decision.NONE) == []


def test_a_tag_the_user_already_set_is_never_touched():
    state = State()
    videos = [video(1, (9, "Alice"))]
    sync_feedback(state, videos, {ALICE: "Alice"})
    assert (1, ALICE) in state.confirmed
    assert plan(state, videos, Decision.AUTO) == []


def test_a_tag_we_wrote_and_the_user_removed_is_never_added_again():
    state = State(written=[written(1, 5, "Alice", "auto")])
    videos = [video(1)]  # tag 5 is gone
    sync_feedback(state, videos, {ALICE: "Alice"})
    assert (1, ALICE) in state.rejected and state.written == []
    assert plan(state, videos, Decision.AUTO) == []


def test_replacing_our_suggestion_with_the_users_own_tag_is_a_confirmation_not_a_rejection():
    state = State(written=[written(1, 7, "Alice?", "review")])
    videos = [video(1, (9, "Alice"))]  # suggestion 7 gone, user added Alice
    sync_feedback(state, videos, {ALICE: "Alice"})
    assert (1, ALICE) in state.confirmed and not state.rejected and state.written == []


def test_our_suggestion_is_removed_once_the_user_has_confirmed_next_to_it():
    state = State(written=[written(1, 7, "Alice?", "review")])
    videos = [video(1, (7, "Alice?"), (9, "Alice"))]
    sync_feedback(state, videos, {ALICE: "Alice"})
    assert summary(plan(state, videos, Decision.REVIEW)) == [("remove", 1, "Alice?", "review")]


def test_our_own_suggestion_is_upgraded_when_the_match_becomes_confident():
    state = State(written=[written(1, 7, "Alice?", "review")])
    videos = [video(1, (7, "Alice?"))]
    sync_feedback(state, videos, {ALICE: "Alice"})
    assert summary(plan(state, videos, Decision.AUTO)) == [("remove", 1, "Alice?", "review"), ("add", 1, "Alice", "auto")]


def test_our_tag_is_taken_back_when_the_pair_was_rejected_another_way():
    state = State(written=[written(1, 5, "Alice", "auto")], rejected={(1, ALICE)})  # e.g. the user removed Alice from the video
    videos = [video(1, (5, "Alice"))]
    assert summary(plan(state, videos, Decision.AUTO)) == [("remove", 1, "Alice", "auto")]


def test_confident_videos_are_the_tagged_ones_plus_the_ones_about_to_be_tagged():
    videos = [video(1, (11, "Alice")), video(2, (12, "Alice?")), video(3), video(4)]
    assert confident_videos(ALICE, "Alice", videos, [new_auto(3)]) == {1, 3}


def test_a_video_whose_tag_is_being_taken_back_is_no_longer_confident():
    videos = [video(1, (11, "Alice")), video(2, (12, "Alice"))]
    takeback = TagAction("remove", 1, ALICE, "Alice", "auto", 11)
    assert confident_videos(ALICE, "Alice", videos, [takeback]) == {2}


def test_a_suggestion_we_already_made_is_not_repeated():
    state = State(written=[written(1, 7, "Alice?", "review")])
    videos = [video(1, (7, "Alice?"))]
    sync_feedback(state, videos, {ALICE: "Alice"})
    assert plan(state, videos, Decision.REVIEW) == []


# -- executing --------------------------------------------------------------------------------

def test_the_tag_is_created_once_and_every_write_is_recorded(tmp_path):
    client, state = FakeClient(), State(tmp_path / "state.json")
    actions = plan(state, [video(1), video(2)], Decision.AUTO)
    assert execute(client, state, actions, max_writes=10) == 2
    assert client.created == ["Alice"]
    assert [(t.item_id, t.tag_name, t.level) for t in state.written] == [(1, "Alice", "auto"), (2, "Alice", "auto")]
    assert [t.item_id for t in State.load(tmp_path / "state.json").written] == [1, 2]


def test_the_write_limit_stops_a_run(tmp_path):
    client, state = FakeClient(), State(tmp_path / "state.json")
    actions = plan(state, [video(1), video(2), video(3)], Decision.AUTO)
    assert execute(client, state, actions, max_writes=2) == 2
    assert len(client.added) == 2


def test_undo_removes_only_what_we_wrote_and_keeps_failures_on_record(tmp_path):
    client = FakeClient()
    state = State(tmp_path / "state.json", written=[written(1, 5, "Alice", "auto"), written(2, 6, "Alice", "auto")])
    client.fail_removal_for = {2}
    assert undo_all(client, state) == 1
    assert client.removed == [(1, 5)]
    assert [t.item_id for t in state.written] == [2]
