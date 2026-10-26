import shutil
import subprocess

import pytest

from videotag import frames
from videotag.sampling import PRESETS, Sampling

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


def make_clip(path, seconds):
    source = f"testsrc=duration={seconds}:size=160x120:rate=10"
    subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", source, "-pix_fmt", "yuv420p", str(path)],
                   check=True)
    return path


def frames_before_presets_existed(video, out_dir, fps):
    """The call `sample_frames` made before presets: every result in the caches was made with exactly this."""
    subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-i", str(video), "-vf", f"fps={fps}", "-q:v", "2",
                    str(out_dir / "f%04d.jpg")], check=True, capture_output=True)
    return sorted(out_dir.glob("f*.jpg"))


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    folder = tmp_path_factory.mktemp("clips")
    return {name: make_clip(folder / f"{name}.mp4", seconds) for name, seconds in (("long", 70), ("short", 16.3), ("exact", 30))}


def cut(video, sampling, tmp_path, name="out"):
    folder = tmp_path / name
    folder.mkdir()
    return frames.sample_frames(video, folder, sampling)


def same_bytes(first, second):
    return [path.read_bytes() for path in first] == [path.read_bytes() for path in second]


@needs_ffmpeg
def test_thorough_cuts_exactly_the_frames_the_tool_always_cut(clips, tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    assert same_bytes(cut(clips["long"], PRESETS["thorough"], tmp_path), frames_before_presets_existed(clips["long"], old, 2.0))


@needs_ffmpeg
def test_balanced_keeps_the_first_thirty_seconds_and_then_takes_a_frame_every_two_seconds(clips, tmp_path):
    thorough = cut(clips["long"], PRESETS["thorough"], tmp_path, "thorough")
    balanced = cut(clips["long"], PRESETS["balanced"], tmp_path, "balanced")
    assert [path.name for path in balanced] == [f"f{number:04d}.jpg" for number in range(1, 81)]  # 60, then 30 s to 68 s
    assert same_bytes(balanced[:60], thorough[:60])


@needs_ffmpeg
def test_fast_takes_a_frame_every_four_seconds_after_the_first_thirty(clips, tmp_path):
    assert len(cut(clips["long"], PRESETS["fast"], tmp_path)) == 60 + 10


@needs_ffmpeg
@pytest.mark.parametrize("name", ["short", "exact"])
@pytest.mark.parametrize("preset", ["balanced", "fast"])
def test_a_clip_that_ends_within_the_first_thirty_seconds_is_cut_like_thorough_without_an_error(clips, tmp_path, name, preset):
    thorough = cut(clips[name], PRESETS["thorough"], tmp_path, "thorough")
    slowed = cut(clips[name], PRESETS[preset], tmp_path, "slowed")
    assert same_bytes(slowed, thorough)


@needs_ffmpeg
def test_a_file_that_is_not_a_video_is_an_error_under_every_preset(tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video at all")
    for sampling in (PRESETS["thorough"], PRESETS["balanced"]):
        with pytest.raises(subprocess.CalledProcessError):
            cut(broken, sampling, tmp_path, f"out-{sampling.spec}")


def test_without_ffmpeg_the_error_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(frames.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="ffmpeg was not found on PATH"):
        frames.sample_frames(tmp_path / "x.mp4", tmp_path, Sampling(2.0))


def fail_the_second_pass(monkeypatch):
    """Make the pass that cuts after the first thirty seconds fail without writing anything, whatever the clip."""
    real = frames._cut

    def cut(ffmpeg, before_input, video, after_input, out_dir):
        if "-ss" in before_input:
            raise subprocess.CalledProcessError(1, "ffmpeg")
        real(ffmpeg, before_input, video, after_input, out_dir)

    monkeypatch.setattr(frames, "_cut", cut)


@needs_ffmpeg
def test_a_second_pass_that_fails_on_a_clip_that_goes_on_is_an_error_not_a_truncated_result(clips, tmp_path, monkeypatch):
    fail_the_second_pass(monkeypatch)
    with pytest.raises(subprocess.CalledProcessError):
        cut(clips["long"], PRESETS["balanced"], tmp_path)   # the clip is 70 s long: there are frames after the first 30


@needs_ffmpeg
def test_a_second_pass_that_fails_on_a_clip_that_ends_there_is_just_the_end_of_the_clip(clips, tmp_path, monkeypatch):
    fail_the_second_pass(monkeypatch)
    assert len(cut(clips["exact"], PRESETS["balanced"], tmp_path)) == 60


@needs_ffmpeg
def test_without_ffprobe_a_failing_second_pass_is_taken_for_the_end_of_the_clip(clips, tmp_path, monkeypatch):
    fail_the_second_pass(monkeypatch)
    real = frames.shutil.which
    monkeypatch.setattr(frames.shutil, "which", lambda name: None if name == "ffprobe" else real(name))
    assert len(cut(clips["long"], PRESETS["balanced"], tmp_path)) == 60
