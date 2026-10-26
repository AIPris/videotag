"""What a sampling preset would cost or save on the user's own library, worked out from the results already saved.

A saved result knows how many frames its video gave and under which rule, which tells roughly how long the clip is and so
how many frames each preset would cut from it. Nothing here touches Photos or a video file.
"""
from . import facecache
from .sampling import PRESETS, Sampling


def preset_estimates(summaries: list[facecache.Summary]) -> dict:
    """Frames in all under each preset, and the saving against the first (thorough) in percent."""
    clips = [(summary.sampling.seconds_for(summary.frames)) for summary in summaries if summary.frames]
    frames = {name: sum(preset.frames_for(seconds) for seconds in clips) for name, preset in PRESETS.items()}
    baseline = frames["thorough"]
    saving = {name: round(100 * (1 - total / baseline)) if baseline else None for name, total in frames.items()}
    return {"videos": len(clips), "frames": frames, "saving": saving}


def reanalysis(summaries: list[facecache.Summary], target: Sampling) -> dict:
    """How many saved results were made under another rule than `target`, out of all of them."""
    return {"videos": sum(summary.sampling != target for summary in summaries), "of": len(summaries)}
