import numpy as np
import pytest

from videotag import facecache
from videotag.estimates import preset_estimates, reanalysis
from videotag.faces import MODEL_VERSION, Face
from videotag.sampling import PRESETS, Sampling

FACE = Face(np.zeros(128, np.float32), 1.0, (0.0, 0.0, 1.0, 1.0))


@pytest.fixture(autouse=True)
def faces_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(facecache, "FACES_DIR", tmp_path)
    return tmp_path


def save(video_id, frames, sampling=PRESETS["thorough"]):
    facecache.save(video_id, {"filesize": 10 + video_id, "time": 1}, [[FACE]] * frames, sampling)


def entries(folder):
    return facecache.summaries(folder)


def test_a_summary_has_the_rule_and_the_number_of_frames_without_loading_the_faces(faces_dir):
    save(3, 40, PRESETS["balanced"])
    (summary,) = entries(faces_dir)
    assert (summary.video_id, summary.frames, summary.sampling) == (3, 40, PRESETS["balanced"])


def test_results_from_before_presets_are_summarised_too(faces_dir):
    np.savez_compressed(faces_dir / "5.npz", key=f"v2-boxes|{MODEL_VERSION}|10|1|2.0", counts=np.ones(7, np.int32),
                        embeddings=np.zeros((7, 128), np.float32), boxes=np.zeros((7, 4), np.float32))
    (summary,) = entries(faces_dir)
    assert (summary.video_id, summary.frames, summary.sampling) == (5, 7, PRESETS["thorough"])


def test_results_that_would_be_rebuilt_anyway_or_cannot_be_read_are_left_out(faces_dir):
    save(1, 10)
    np.savez_compressed(faces_dir / "2.npz", key="v1|old-models|10|1|2.0", counts=np.ones(3, np.int32),
                        embeddings=np.zeros((3, 128), np.float32), boxes=np.zeros((3, 4), np.float32))
    (faces_dir / "3.npz").write_bytes(b"half a file")
    (faces_dir / "notes.txt").write_text("not a result", encoding="utf-8")
    assert [summary.video_id for summary in entries(faces_dir)] == [1]


def test_a_missing_folder_has_no_results(tmp_path):
    assert facecache.summaries(tmp_path / "nowhere") == []


def test_the_saving_of_each_preset_is_worked_out_from_the_frames_the_library_has(faces_dir):
    save(1, 32)                      # a 16 s clip: the same under every preset
    save(2, 2460)                    # a 20.5 min clip: 30 s at 2 fps, then 1200 s at the slower rate
    estimate = preset_estimates(entries(faces_dir))
    assert estimate["videos"] == 2
    assert estimate["frames"] == {"thorough": 2492, "balanced": 32 + 660, "fast": 32 + 360}
    assert estimate["saving"] == {"thorough": 0, "balanced": 72, "fast": 84}  # 1 - 692/2492, 1 - 392/2492


def test_an_estimate_does_not_depend_on_the_rule_the_results_were_made_under(faces_dir):
    save(1, 32)
    save(2, 660, PRESETS["balanced"])   # the 20.5 min clip, cut under balanced
    assert preset_estimates(entries(faces_dir))["frames"] == {"thorough": 32 + 2460, "balanced": 32 + 660, "fast": 32 + 360}


def test_without_any_result_there_is_nothing_to_estimate_from(faces_dir):
    assert preset_estimates(entries(faces_dir)) == {"videos": 0, "frames": {"thorough": 0, "balanced": 0, "fast": 0},
                                                    "saving": {"thorough": None, "balanced": None, "fast": None}}


def test_a_clip_without_frames_is_not_counted(faces_dir):
    save(1, 0)
    assert preset_estimates(entries(faces_dir))["videos"] == 0


def test_re_analysing_touches_the_results_made_under_another_rule(faces_dir):
    save(1, 32)
    save(2, 60, PRESETS["balanced"])
    save(3, 40, Sampling(1.0))
    assert reanalysis(entries(faces_dir), PRESETS["balanced"]) == {"videos": 2, "of": 3}
    assert reanalysis(entries(faces_dir), PRESETS["thorough"]) == {"videos": 2, "of": 3}
    assert reanalysis(entries(faces_dir), Sampling(1.0)) == {"videos": 2, "of": 3}


def test_re_analysing_touches_nothing_when_every_result_was_made_under_the_rule_in_use(faces_dir):
    save(1, 32)
    save(2, 50)
    assert reanalysis(entries(faces_dir), PRESETS["thorough"]) == {"videos": 0, "of": 2}
