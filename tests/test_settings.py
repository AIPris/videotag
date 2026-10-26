import json

import pytest

from videotag import settings
from videotag.settings import Settings, SettingsError


@pytest.fixture
def path(tmp_path):
    return tmp_path / "settings.json"


def test_without_a_settings_file_the_tool_behaves_as_it_always_did(path):
    assert settings.load(path) == Settings(preset="thorough", describe=True)


def test_saved_settings_come_back(path):
    settings.save(Settings(preset="fast", describe=False), path)
    assert settings.load(path) == Settings("fast", False)


def test_the_file_holds_only_the_standing_choices(path):
    settings.save(Settings("balanced", True), path)
    assert json.loads(path.read_text(encoding="utf-8")) == {"preset": "balanced", "describe": True}


def test_a_file_with_one_choice_leaves_the_other_at_its_default(path):
    path.write_text('{"preset": "balanced"}', encoding="utf-8")
    assert settings.load(path) == Settings("balanced", True)


def test_a_flag_wins_over_the_file_and_a_flag_left_out_changes_nothing():
    saved = Settings("fast", False)
    assert settings.resolve(saved, preset="thorough", describe=True) == Settings("thorough", True)
    assert settings.resolve(saved, preset=None, describe=None) == saved
    assert settings.resolve(saved, preset=None, describe=True) == Settings("fast", True)


@pytest.mark.parametrize("key", ["apply", "mode", "max_writes", "limit", "new_only", "album", "reanalyse"])
def test_a_per_run_choice_in_the_file_is_refused_not_ignored(path, key):
    path.write_text(json.dumps({"preset": "fast", key: 1}), encoding="utf-8")
    with pytest.raises(SettingsError, match=f"'{key}'.*not a setting"):
        settings.load(path)


def test_the_refusal_names_the_file_so_it_can_be_fixed(path):
    path.write_text('{"apply": true}', encoding="utf-8")
    with pytest.raises(SettingsError, match="settings.json"):
        settings.load(path)


@pytest.mark.parametrize("content", ["", "not json", "[1, 2]", '"fast"', '{"preset": "turbo"}', '{"preset": 3}', '{"preset": []}',
                                     '{"describe": "yes"}', '{"colour": "blue"}'])
def test_a_file_that_cannot_be_trusted_stops_the_run_instead_of_being_guessed_at(path, content):
    path.write_text(content, encoding="utf-8")
    with pytest.raises(SettingsError):
        settings.load(path)


def test_a_file_saved_by_a_windows_editor_with_a_byte_order_mark_is_read(path):
    path.write_text('{"preset": "fast"}', encoding="utf-8-sig")
    assert settings.load(path).preset == "fast"


def test_saving_never_leaves_a_half_written_file_behind(path):
    settings.save(Settings("fast", True), path)
    assert [entry.name for entry in path.parent.iterdir()] == ["settings.json"]
