# Next version: implementation checklist

> Working checklist for the loop `implement the open ToDos`. Spec: `docs/roadmap.md` (decisions from the 2026-10-26
> interview). Branch `next-version`, nothing committed until the user asks. TDD: a failing test first, then the code.
> After every task: `python -m pytest -q` and `python -m ruff check .` both clean. Real-data checks only in a scratch
> `VIDEOTAG_HOME`, dry runs only, never `--apply`.

## Global constraints

- Default behaviour and results stay identical: Thorough = today's `fps=2` ffmpeg call; today's `v2-boxes|model|size|time|2.0`
  face-cache entries stay valid (old key format is read, not rebuilt).
- A saved result records the sampling rule that made it. Crops (`frame_crop`) use the rule stored with the result, not the option.
- The settings file never holds run mode, write cap, limit, slice or album (it refuses them, it does not ignore them).
  Command-line flags win over the file.
- Slices are dry-run only: refused with `--apply` in the CLI, in the launcher and in the GUI server.
- New log lines never start with digits (`runlog._VIDEO` counts lines that do as videos).
- Live-run file is not `run-*.json`; it is shown only while its run is running; an interrupted run's file is never a report.
- Nothing derived from videos or photos leaves this PC. The tool never deletes a video.

## Tasks

- [x] 1. `sampling.py`: `Sampling`, `PRESETS` (thorough / balanced / fast), spec string round trip, frame-count helpers, even-in-time picks
- [x] 2. `frames.sample_frames` takes a `Sampling`; two passes for the variable presets; Thorough byte-identical to today
- [x] 3. `facecache`: file key + stored sampling; old keys read as uniform sampling; `analysed()` ignores the rate
- [x] 4. analysis + pipeline: `RunOptions.sampling`, `Outcome.sampling`, crops from the stored rule, description frames spread in time, `reanalyse`
- [x] 5. `settings.py` + CLI (`--preset`, `--describe/--no-describe`, `videotag settings`)
- [x] 6. Slices: `--limit`, `--new-only`, `--album ID`, `videotag albums`; dry run only
- [x] 7. Reference faces: per-photo embedding cache, `references:` counter lines, `runlog` phase and time left
- [x] 8. Live view: `live.jsonl` writer in the pipeline, dashboard reads it while running, `data_version`
- [x] 9. GUI: settings dialog (preset savings estimate, describe, re-analyse), run dialog (preset, limit, new only, album), live banner; server-side validation; `API_VERSION` 3
- [x] 10. Twins: analysed once, copied to the other ids, listed in the GUI
- [x] 11. Trust tools: spot-check, accuracy report, evidence crops
- [x] 12. Docs: README, howto, architecture, roadmap, tests count; memory notes
- [x] 13. Photos link: built with the folder route from a real Photos address; only a click on the stored login's own videos is unchecked

## Review focus (inputs the spec is silent on)

- A clip shorter than 30 s under Balanced/Fast is the same as Thorough (no second pass, no error from an empty pass).
- A clip of exactly 30 s, a clip with no frames, and a video whose ffmpeg pass fails.
- A cache entry written by a newer version with a sampling spec this version cannot parse: treated as not found.
- Two videos with the same name, size and time but one fails to download: both rows carry the error.
- The settings file is missing, empty, not JSON, or holds a forbidden key.
