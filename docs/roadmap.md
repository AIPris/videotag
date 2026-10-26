# Next version

Decisions from an interview on 2026-10-26, after the first full dry run on the real library (1,553 videos, 29,531 photos).
Nothing is built or designed in detail yet. **My call** marks a decision made without a question (you could object);
everything else is what was asked for or chosen.

## What hurt in the first real run

- About 13 hours with nothing to see: Library and History stay empty until the report is written at the very end.
- A silent 28½ minutes at the start (20:12 to 20:40) while the reference faces were built, with no line in the log and
  only a label on the dashboard.

## Must-haves

### 1. Limit, slices and albums (dry run only)

- A limit on how many videos a pass looks at: `--limit N` and a field next to the dashboard's Start button.
- It can be narrowed two ways, and combined with the limit:
  - **Not analysed yet**: skip videos that already have a saved result, so each limited run adds N more.
  - **Pick an album**: only the videos of one of your own albums. **My call:** not the per-person albums this tool creates.
- **Dry run only.** A real `--apply` always covers the whole library. A slice's report holds just that slice, and the Library
  view keeps showing the newest report (an adding-up Library was not chosen).
- Not chosen: a random sample.
- "Not analysed yet" must mean "has a saved result at any frame rate" (see 2), or changing the preset would make every video
  look new again.

### 2. Speed presets for the frame rate

- Today every video is cut at 2 frames per second, which gave a 4½-minute clip 534 frames. In the first run the longest 100
  clips (7%) held 45% of all frames.
- Presets instead of free numbers, with the **default unchanged** (today's behaviour, today's results):

  | Preset | Rule | Saving on the first run's 1,431 clips |
  |---|---|---|
  | Thorough (default) | 2 fps throughout, as today | none |
  | Balanced | 2 fps for the first 30 s, then 0.5 fps | 38% fewer frames, 402 clips affected |
  | Fast | 2 fps for the first 30 s, then 0.25 fps | 45% fewer frames, 402 clips affected |

  The names and values are **my call**; the percentages were computed from the logged frame counts (median clip 16 s, 90th
  percentile 72 s, longest 17.6 min).
- The GUI shows the estimated saving for the user's own library before a preset is chosen.
- **Old results are kept** when the preset changes: only videos without a result use the new rate. A deliberate
  **Re-analyse** action exists (optional, all videos), and the GUI shows how many videos it touches and about how long it
  takes before anything is redone.
  - Catch for the builder: today a saved result is keyed by file size, modification time, **sampling rate** and model, so
    any rate change re-analyses everything. Keeping old results means finding a result without the rate and recording the
    rate inside it.
- The six frames the description model sees are spread evenly over the clip and do not depend on the rate.
- Not chosen: a per-video frame cap, or free fps and strictness fields in the GUI (`--min-frames` and `--threshold` stay
  command-line flags).

### 3. Counter at the start, and cached reference faces

- A counter for the phase that reads the library and builds the named people's reference faces: photos done of photos total
  and time left. It is written to the log, so the dashboard, a scheduled run and a terminal all see it, like the per-video
  progress.
- The reference faces are kept between runs and rebuilt on their own when the named people or their photos change
  (**my call**), so the 28 minutes are paid once and not on every run. This also keeps a 20-video test run from costing
  28 minutes. Not measured: how long that phase takes on a second run, when the photos are already downloaded
  (`cache/photos` holds 4.6 GB for 2,432 photos).

### 4. Live view of the current run

- The current run's results appear in the dashboard as they are made, instead of only in the final report. Per-video
  results are already saved as they are produced (`state/faces`, `state/describe`).
- Current run only. Not chosen: a view that adds up everything analysed across runs, and a label with the settings of each
  result.

## Settings

- **One saved file** in the data home, used by the GUI, the terminal and any scheduled run. A command-line flag overrides
  the file for that one run.
- **My call:** the file never holds the run mode or the write cap, so a settings file can never switch on writing to
  Photos.
- **My call:** per-run choices (limit, slice, album) are not saved either, so a saved limit cannot quietly shorten a
  scheduled run. The file holds standing choices such as the preset and descriptions on or off.

## Also wanted (not yet ranked against the must-haves)

### Twins: the same file under two ids

- In the first run 71 file names appear under two ids; 68 of those have the same frame count in both copies, 3 do not, so a
  name alone never makes a twin.
- A twin is the same name **and** the same size (and time). The item listing already returns `filesize` and `time`, so this
  needs no extra call.
- A twin is analysed once and its result is copied to the other id, so both videos get tags and descriptions.
- The GUI lists twins so they can be cleaned up in Photos. The tool never deletes a video.

### Trust tools, before applying to the real library

All three were chosen. The first is as asked; the other two were left to me.

- **Spot-check:** a random sample of matches per person, to look at in Photos.
- **Accuracy report** (**my call**): per person, the number of reference faces, the threshold, how well they separate from
  the other named people and whether confident tags are allowed, plus which pairs of people get mixed up. It uses what
  `videotag calibrate` already works out. Comparing two runs and the known-video test are left out for now.
- **Evidence screen** (**my call**): during a run, keep the best few face crops of each match or suggestion in the data
  home, so they open instantly. They stay on this PC; nothing is uploaded to Photos, and videos without a match keep none.
  No full frames.

### Video title as a link into Photos

- Asked for on 2026-10-26, after a dry run. In the Videos tab each video shows its file name as a plain title, with the
  potential matches (`Name?` suggestions and not-yet-written tags) beside it. To check one you have to search for the name
  in Photos by hand.
- The title becomes a link that opens that video in Synology Photos, in a new browser tab, so a match can be verified in one
  click. It works on any report, dry run or applied.
- This is what makes the **Spot-check** above practical: pick a match, click, look.
- Catch for the builder: the report already stores the video's id and file name, and the dashboard knows the NAS address
  from the saved account. What is **not checked** is the URL Photos' web app uses to open a single video, and which id
  (item id or unit id) it expects. Check it against the real NAS before building; do not guess the format. A report made
  under another account would link into the wrong library, so the link must only be shown for the current account.

## Faster, same results

Measured on the first full dry run (12.2 h): downloads about half of the time, the description model about a third, frame
cutting and face detection about 5%. Everything below keeps the results identical unless it says otherwise.

- **Built (2026-10-26): download while describing.** A helper thread fetches, cuts and reads the faces of the next videos
  while the model describes the current one (`analysis.py`). On 12 real videos it took 112 s instead of 170 s on the direct
  network path and 109 s instead of 163 s on the router path (about a third less wall time). Faces and descriptions were
  identical to a one-at-a-time run for all 12 videos, and array-for-array identical to the overnight run's stored results.
  The model step itself ran 6 to 10% slower while overlapped (CPU competition), which cost less than the overlap won.
  A full pass is now limited by the model, about 10 s per video.
- **Not code, large effect: how the NAS is reached.** Reaching the NAS by its public name sends the traffic through the
  router's loop-back, which capped downloads at about 12 MB/s. The NAS answers directly on its local address at about
  105 MB/s. Making the name resolve to the local address on this network (a local DNS entry in the router, or a hosts-file
  line on the PC) needs no code. An option for the tool to connect to a local address while still checking the certificate
  for the name is possible, but nobody asked for it.
- **Open: the model step.** Batching several videos in one call, `torch.compile` or GPU video decoding could shorten it, but
  each can change the wording of a description, so each needs a proof against stored results before it is used. Untested:
  whether fewer threads for frame cutting and face detection give the model back its 6 to 10%.
- Twins (above) also save downloads and model time, about 5% of videos.

## Not picked in the interview (parked, not planned)

A stop button for a running dashboard run, a notification when a run ends, choosing which people are matched, a scheduled
daily run, running on the NAS (Docker), applying a slice, an adding-up Library view, and showing each result's settings.
Any of them can come back; none was asked for.

## Open

- Time of the reference phase on a second run (unmeasured).
- The Photos URL that opens one video, and the id it expects (needed for the video title link).
- Whether the twins and trust tools make the next version or the one after; only the four must-haves are fixed.
- **Order I would suggest (a suggestion, not a decision):** the settings file and the cache-key change for presets first,
  because slices ("not analysed yet") and presets both depend on how a saved result is found; then slices; then the start
  counter and cached faces; then the live view; then twins and the trust tools.
