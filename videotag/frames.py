"""Cut still frames out of a video with ffmpeg (handles HEVC and rotation metadata)."""
import shutil
import subprocess
from pathlib import Path


def sample_frames(video: Path, out_dir: Path, fps: float) -> list[Path]:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg was not found on PATH")
    subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(video), "-vf", f"fps={fps}", "-q:v", "2", str(out_dir / "f%04d.jpg")],
        check=True, capture_output=True,
    )
    return sorted(out_dir.glob("f*.jpg"))
