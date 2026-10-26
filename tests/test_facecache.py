import numpy as np
import pytest

from videotag import facecache
from videotag.faces import MODEL_VERSION, Face
from videotag.sampling import PRESETS, Sampling

VIDEO = {"id": 7, "filesize": 10, "time": 1}


@pytest.fixture(autouse=True)
def faces_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(facecache, "FACES_DIR", tmp_path)
    return tmp_path


def make_face(rng, box):
    return Face(rng.random(128), (box[2] - box[0]) * (box[3] - box[1]), box)


def write_entry_as_the_tool_did_before_presets(folder, video_id, key, frames):
    """Byte for byte what the previous version saved: the sampling rate sat at the end of the key and nowhere else."""
    flat = [face for faces in frames for face in faces]
    np.savez_compressed(folder / f"{video_id}.npz", key=key, counts=np.array([len(f) for f in frames], dtype=np.int32),
                        embeddings=np.stack([f.embedding for f in flat]).astype(np.float32),
                        boxes=np.array([f.box for f in flat], dtype=np.float32).reshape(-1, 4))


def test_a_saved_result_keeps_embeddings_and_positions_including_empty_frames_and_the_rule_that_made_it():
    rng = np.random.default_rng(0)
    frames = [[make_face(rng, (0.1, 0.2, 0.3, 0.4))], [],
              [make_face(rng, (0.5, 0.5, 0.75, 0.9)), make_face(rng, (0.0, 0.0, 0.25, 0.5))]]
    facecache.save(7, VIDEO, frames, PRESETS["balanced"])
    loaded = facecache.load(7, VIDEO)
    assert loaded.sampling == PRESETS["balanced"]
    assert [len(f) for f in loaded.faces] == [1, 0, 2]
    assert np.allclose(loaded.faces[2][1].embedding, frames[2][1].embedding, atol=1e-6)
    assert np.allclose(loaded.faces[2][0].box, (0.5, 0.5, 0.75, 0.9), atol=1e-6)


def test_a_result_is_found_whatever_rule_is_in_use_now_and_zero_frames_are_fine():
    facecache.save(7, VIDEO, [], PRESETS["fast"])
    assert facecache.load(7, VIDEO).faces == [] and facecache.load(7, VIDEO).sampling == PRESETS["fast"]


def test_a_result_is_not_used_for_another_file_another_video_or_other_face_models(monkeypatch):
    facecache.save(7, VIDEO, [], PRESETS["thorough"])
    assert facecache.load(7, {**VIDEO, "filesize": 11}) is None
    assert facecache.load(7, {**VIDEO, "time": 2}) is None
    assert facecache.load(8, VIDEO) is None
    monkeypatch.setattr(facecache, "MODEL_VERSION", "other-models")
    assert facecache.load(7, VIDEO) is None


def test_a_result_made_before_presets_existed_is_still_found_and_counts_as_thorough(faces_dir):
    rng = np.random.default_rng(1)
    frames = [[make_face(rng, (0.1, 0.2, 0.3, 0.4))], [make_face(rng, (0.5, 0.5, 0.75, 0.9))]]
    write_entry_as_the_tool_did_before_presets(faces_dir, 7, f"v2-boxes|{MODEL_VERSION}|10|1|2.0", frames)
    loaded = facecache.load(7, VIDEO)
    assert loaded.sampling == PRESETS["thorough"] and [len(f) for f in loaded.faces] == [1, 1]
    assert np.allclose(loaded.faces[1][0].embedding, frames[1][0].embedding, atol=1e-6)


def test_a_result_made_with_another_old_rate_keeps_that_rate(faces_dir):
    write_entry_as_the_tool_did_before_presets(faces_dir, 7, f"v2-boxes|{MODEL_VERSION}|10|1|1.0",
                                               [[make_face(np.random.default_rng(2), (0.1, 0.1, 0.2, 0.2))]])
    assert facecache.load(7, VIDEO).sampling == Sampling(1.0)


@pytest.mark.parametrize("key", [f"v2-boxes|{MODEL_VERSION}|10|1", f"v2-boxes|{MODEL_VERSION}|10|1|2.0@x",
                                 f"v2-boxes|{MODEL_VERSION}|100|1|2.0", f"v1|{MODEL_VERSION}|10|1|2.0"])
def test_an_entry_that_cannot_be_read_as_a_result_for_this_file_is_not_used(faces_dir, key):
    write_entry_as_the_tool_did_before_presets(faces_dir, 7, key, [[make_face(np.random.default_rng(3), (0.1, 0.1, 0.2, 0.2))]])
    assert facecache.load(7, VIDEO) is None
