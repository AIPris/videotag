from videotag.describe import Description
from videotag.descriptions import (
    END,
    START,
    DescriptionAction,
    delete_descriptions,
    execute_descriptions,
    extract_block,
    merge_block,
    plan_description_changes,
    render_block,
    strip_block,
    sync_description_feedback,
)
from videotag.state import State
from videotag.synology import SynologyError

CAT = Description("A black cat explores a Christmas tree", "Eine schwarze Katze erkundet einen Weihnachtsbaum.",
                  ("christmas", "cat"), ("Weihnachten", "Katze"), ("cat", "christmas tree"), ("Katze", "Weihnachtsbaum"))
TOKYO = Description("A film set.", "Ein Filmset.", place="Tokyo", place_evidence="a studio logo")


def video(item_id, description=""):
    return {"id": item_id, "additional": {"description": description}}


# -- the block --------------------------------------------------------------------------------

def test_the_block_holds_both_languages_and_is_marked():
    block = render_block(CAT)
    lines = block.split("\n")
    assert lines[0] == START and lines[-1] == END
    assert lines[1] == "EN: A black cat explores a Christmas tree. | christmas, cat, christmas tree"
    assert lines[2] == "DE: Eine schwarze Katze erkundet einen Weihnachtsbaum. | Weihnachten, Katze, Weihnachtsbaum"


def test_a_place_gets_its_own_line_with_the_evidence():
    assert "@ Tokyo (a studio logo)" in render_block(TOKYO).split("\n")


def test_empty_lists_are_left_out():
    assert render_block(Description("A cat.", "Eine Katze.")).split("\n")[1] == "EN: A cat."


def test_the_block_carries_no_label_words_that_every_video_would_share():
    # Photos search matches any part of the text, so a shared word would return the whole library
    text = render_block(CAT).lower()
    for word in ("topic", "object", "themen", "objekt", "video", "tag", "place", "ort"):
        assert word not in text.replace("weihnachtsbaum", "")


def test_a_block_with_the_earlier_marker_is_still_recognised_and_gets_rewritten():
    old = "[videotag]\nEN: A black cat. Topics: cat.\n[/videotag]"
    assert extract_block("Note.\n\n" + old) == old and strip_block("Note.\n\n" + old) == "Note."
    state = State(descriptions={1: old})
    sync_description_feedback(state, [video(1, old)])
    assert state.description_rejected == set()  # not mistaken for a deletion
    (action,) = plan_description_changes(state, [video(1, "Note.\n\n" + old)], {1: CAT})
    assert action.text == "Note.\n\n" + render_block(CAT) and "[videotag]" not in action.text


def test_the_block_is_appended_after_the_users_own_text_and_the_text_is_kept():
    merged = merge_block("My own note.", render_block(CAT))
    assert merged.startswith("My own note.\n\n" + START) and strip_block(merged) == "My own note."


def test_an_empty_description_becomes_just_the_block():
    assert merge_block("", render_block(CAT)) == render_block(CAT)


def test_merging_again_replaces_only_our_block():
    first = merge_block("Before.\n\n", render_block(CAT))
    second = merge_block(first + "\n\nAfter.", render_block(TOKYO))
    assert second.count(START) == 1 and "Tokyo" in second and "Katze" not in second
    assert second.startswith("Before.") and second.endswith("After.")


def test_extract_and_strip_without_a_block():
    assert extract_block("only text") is None and strip_block("only text") == "only text"


# -- feedback and planning --------------------------------------------------------------------

def test_a_new_video_gets_the_block():
    (action,) = plan_description_changes(State(), [video(1)], {1: CAT})
    assert action == DescriptionAction(1, render_block(CAT), render_block(CAT))


def test_a_video_without_a_description_result_is_skipped():
    assert plan_description_changes(State(), [video(1)], {}) == []


def test_an_up_to_date_block_needs_nothing():
    block = render_block(CAT)
    assert plan_description_changes(State(descriptions={1: block}), [video(1, block)], {1: CAT}) == []


def test_our_block_is_updated_when_the_description_changed():
    state = State(descriptions={1: render_block(CAT)})
    (action,) = plan_description_changes(state, [video(1, "Note.\n\n" + render_block(CAT))], {1: TOKYO})
    assert action.text == "Note.\n\n" + render_block(TOKYO)


def test_a_block_the_user_edited_is_left_alone():
    edited = render_block(CAT).replace("cat", "kitten")
    assert plan_description_changes(State(descriptions={1: render_block(CAT)}), [video(1, edited)], {1: TOKYO}) == []


def test_a_foreign_block_without_a_record_is_left_alone():
    assert plan_description_changes(State(), [video(1, render_block(CAT))], {1: TOKYO}) == []


def test_a_deleted_block_is_a_rejection_and_is_never_written_again():
    state = State(descriptions={1: render_block(CAT), 2: render_block(CAT)})
    videos = [video(1, "My note only."), video(2, render_block(CAT))]
    sync_description_feedback(state, videos)
    assert state.description_rejected == {1} and list(state.descriptions) == [2]
    assert plan_description_changes(state, videos, {1: CAT}) == []


def test_a_video_we_cannot_see_is_left_alone():
    state = State(descriptions={9: render_block(CAT)})
    sync_description_feedback(state, [video(1)])
    assert state.descriptions == {9: render_block(CAT)} and not state.description_rejected


# -- executing --------------------------------------------------------------------------------

class FakeClient:
    def __init__(self, descriptions=None, failing=()):
        self.descriptions, self.failing = dict(descriptions or {}), set(failing)

    def set_description(self, item_id, text):
        if item_id in self.failing:
            raise SynologyError("nope")
        self.descriptions[item_id] = text

    def item_description(self, item_id):
        return self.descriptions[item_id]


def test_executing_writes_the_text_and_remembers_the_block(tmp_path):
    client, state = FakeClient(), State(tmp_path / "state.json")
    done = execute_descriptions(client, state, [DescriptionAction(1, "Note.\n\nBLOCK", "BLOCK")], budget=5)
    assert done == 1 and client.descriptions == {1: "Note.\n\nBLOCK"} and state.descriptions == {1: "BLOCK"}
    assert State.load(tmp_path / "state.json").descriptions == {1: "BLOCK"}


def test_a_failed_write_is_not_remembered(tmp_path):
    client, state = FakeClient(failing={1}), State(tmp_path / "state.json")
    assert execute_descriptions(client, state, [DescriptionAction(1, "T", "B")], budget=5) == 0 and state.descriptions == {}


def test_the_write_limit_stops_the_descriptions(tmp_path):
    client, state = FakeClient(), State(tmp_path / "state.json")
    actions = [DescriptionAction(i, f"t{i}", f"b{i}") for i in (1, 2, 3)]
    assert execute_descriptions(client, state, actions, budget=2) == 2 and sorted(client.descriptions) == [1, 2]


def test_undo_leaves_a_block_the_user_edited(tmp_path):
    block = render_block(CAT)
    edited = block.replace("cat", "kitten")
    client = FakeClient({1: edited})
    state = State(tmp_path / "state.json", descriptions={1: block})
    assert delete_descriptions(client, state) == 1
    assert client.descriptions == {1: edited} and state.descriptions == {}


def test_undo_removes_only_our_block_and_keeps_the_users_text(tmp_path):
    block = render_block(CAT)
    client = FakeClient({1: "Mine.\n\n" + block, 2: block, 3: "Mine only."})
    state = State(tmp_path / "state.json", descriptions={1: block, 2: block, 3: block})
    assert delete_descriptions(client, state) == 3
    assert client.descriptions == {1: "Mine.", 2: "", 3: "Mine only."} and state.descriptions == {}
