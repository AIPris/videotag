"""How densely a video is cut into frames, and the named presets for it.

A rule is stored with every saved result, as a short text (`spec`), so a result stays usable and its frames can be cut
again exactly as they were when the preset in use has changed. `2.0` is every half second throughout (the rule of every
result made before presets existed); `2.0@30.0+0.5` is two frames a second for the first 30 s, then one every 2 s.
"""
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Sampling:
    fps: float                        # frames per second; only for the first `head_seconds` when `tail_fps` is set
    head_seconds: float | None = None
    tail_fps: float | None = None     # frames per second after `head_seconds`; None: `fps` throughout

    @property
    def uniform(self) -> bool:
        return self.tail_fps is None

    @property
    def spec(self) -> str:
        return f"{self.fps}" if self.uniform else f"{self.fps}@{self.head_seconds}+{self.tail_fps}"

    @classmethod
    def parse(cls, spec: str) -> "Sampling":
        try:
            rate, _, rest = spec.partition("@")
            head, _, tail = rest.partition("+")
            sampling = cls(float(rate)) if not rest else cls(float(rate), float(head), float(tail))
        except ValueError:
            raise ValueError(f"not a sampling rule: {spec!r}") from None
        numbers = [sampling.fps] if sampling.uniform else [sampling.fps, sampling.head_seconds, sampling.tail_fps]
        if not all(math.isfinite(number) and number > 0 for number in numbers):
            raise ValueError(f"not a sampling rule: {spec!r}")
        return sampling

    @property
    def head_frames(self) -> int:
        return round(self.head_seconds * self.fps)

    def times(self, count: int) -> list[float]:
        """Where in the video each of `count` frames sits, in seconds."""
        if self.uniform:
            return [index / self.fps for index in range(count)]
        return [index / self.fps if index < self.head_frames else self.head_seconds + (index - self.head_frames) / self.tail_fps
                for index in range(count)]

    def frames_for(self, seconds: float) -> int:
        """About how many frames a clip of this length gives."""
        if self.uniform or seconds <= self.head_seconds:
            return max(1, round(seconds * self.fps))
        return self.head_frames + round((seconds - self.head_seconds) * self.tail_fps)

    def seconds_for(self, frames: int) -> float:
        """About how long a clip is that gave this many frames (the opposite of `frames_for`)."""
        if self.uniform or frames <= self.head_frames:
            return frames / self.fps
        return self.head_seconds + (frames - self.head_frames) / self.tail_fps


PRESETS = {"thorough": Sampling(2.0), "balanced": Sampling(2.0, 30.0, 0.5), "fast": Sampling(2.0, 30.0, 0.25)}
DEFAULT_PRESET = "thorough"  # today's behaviour and today's results


def preset_named(name: str) -> Sampling:
    if name not in PRESETS:
        raise ValueError(f"unknown preset {name!r}; choose one of: {', '.join(PRESETS)}")
    return PRESETS[name]
