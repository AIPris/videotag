"""Cut still frames out of a video with ffmpeg (handles HEVC and rotation metadata)."""
import shutil
import subprocess
from pathlib import Path

from .sampling import Sampling

QUALITY = ["-q:v", "2"]


def _cut(ffmpeg: str, before_input: list[str], video: Path, after_input: list[str], out_dir: Path) -> None:
    subprocess.run([ffmpeg, "-v", "error", *before_input, "-i", str(video), *after_input, *QUALITY, str(out_dir / "f%04d.jpg")],
                   check=True, capture_output=True)


def _seconds_long(video: Path) -> float | None:
    """How long the video is according to ffprobe (which ships with ffmpeg), or None when that cannot be told."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    probed = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                            capture_output=True, text=True)
    try:
        return float(probed.stdout.strip())
    except ValueError:
        return None


def sample_frames(video: Path, out_dir: Path, sampling: Sampling) -> list[Path]:
    """Frames named f0001.jpg, f0002.jpg ... in video order; `sampling.times` says where each one sits.

    A uniform rule is one ffmpeg call. A slowing rule is two: the first seconds at the fast rate, which makes exactly the
    frames the uniform call would make, then the rest of the clip at the slow rate. A clip that ends inside the first
    seconds needs only the first call, so it is cut exactly as under the uniform rule.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg was not found on PATH")
    if sampling.uniform:
        _cut(ffmpeg, [], video, ["-vf", f"fps={sampling.fps}"], out_dir)
        return sorted(out_dir.glob("f*.jpg"))
    _cut(ffmpeg, [], video, ["-t", str(sampling.head_seconds), "-vf", f"fps={sampling.fps}"], out_dir)
    head = sorted(out_dir.glob("f*.jpg"))
    if len(head) >= sampling.head_frames:  # the clip goes on
        try:
            _cut(ffmpeg, ["-ss", str(sampling.head_seconds)], video,
                 ["-vf", f"fps={sampling.tail_fps}", "-start_number", str(len(head) + 1)], out_dir)
        except subprocess.CalledProcessError:
            # ffmpeg fails, rather than writing nothing, when no frame lies past the cut. That is a clip that ends right
            # there. A failure that wrote frames is a real one, and so is one on a clip that goes on well past the cut:
            # keeping that as a result would cache a truncated one.
            length = _seconds_long(video)
            if len(sorted(out_dir.glob("f*.jpg"))) > len(head) or (length is not None and length > sampling.head_seconds + 1):
                raise
    return sorted(out_dir.glob("f*.jpg"))
