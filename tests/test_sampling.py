import pytest

from videotag.sampling import DEFAULT_PRESET, PRESETS, Sampling, preset_named


def test_the_presets_are_the_ones_the_roadmap_names():
    assert {"thorough": Sampling(2.0), "balanced": Sampling(2.0, 30.0, 0.5), "fast": Sampling(2.0, 30.0, 0.25)} == PRESETS
    assert DEFAULT_PRESET == "thorough"


def test_an_unknown_preset_name_is_refused_with_the_valid_ones():
    with pytest.raises(ValueError, match="thorough, balanced, fast"):
        preset_named("turbo")


@pytest.mark.parametrize("sampling", [Sampling(2.0), Sampling(1.0), Sampling(2.0, 30.0, 0.5), Sampling(2.0, 30.0, 0.25)])
def test_a_rule_survives_being_written_down_and_read_back(sampling):
    assert Sampling.parse(sampling.spec) == sampling


def test_the_old_cache_key_suffix_reads_as_todays_rule():
    assert Sampling.parse("2.0") == PRESETS["thorough"]  # entries made before presets end in "|2.0"


@pytest.mark.parametrize("spec", ["", "fast", "0", "-1", "2.0@30", "2.0@30+0", "2.0@0+0.5", "nan", "2.0@30+0.5+1"])
def test_a_rule_that_makes_no_sense_is_not_read(spec):
    with pytest.raises(ValueError):
        Sampling.parse(spec)


def test_the_times_of_the_frames_under_a_thorough_rule_are_half_a_second_apart():
    assert PRESETS["thorough"].times(4) == [0.0, 0.5, 1.0, 1.5]


def test_the_times_slow_down_after_the_first_thirty_seconds_under_balanced():
    times = PRESETS["balanced"].times(63)
    assert times[59] == 29.5 and times[60:] == [30.0, 32.0, 34.0]


def test_how_many_frames_a_clip_gives_under_each_preset():
    seconds = 1230  # 30 s at 2 fps, then 1200 s at the slower rate
    assert PRESETS["thorough"].frames_for(seconds) == 2460
    assert PRESETS["balanced"].frames_for(seconds) == 60 + 600
    assert PRESETS["fast"].frames_for(seconds) == 60 + 300


def test_a_clip_shorter_than_the_first_thirty_seconds_gives_the_same_frames_under_every_preset():
    assert {sampling.frames_for(16) for sampling in PRESETS.values()} == {32}


def test_the_length_of_a_clip_is_recovered_from_its_frame_count_and_rule():
    for sampling in PRESETS.values():
        assert sampling.seconds_for(sampling.frames_for(240)) == pytest.approx(240, abs=4)
