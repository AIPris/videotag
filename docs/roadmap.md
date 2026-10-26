# Next version

Decisions from an interview on 2026-10-26, after the first full dry run on the real library (1,553 videos, 29,531 photos).
**Status (2026-10-26, same day): everything below is built.** Each section says what was built under **Built**. **My call** marks a decision made without a
question (you could object); everything else is what was asked for or chosen.

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
- **Built:** `run --limit N`, `--new-only`, `--album ID` (and `videotag albums` to list your own albums), and the same three
  fields in the dashboard's run dialog (*Load my albums* asks the NAS). A slice combined with `--apply` is refused in the CLI,
  in the launcher and in the dashboard's server. The album is checked before the long reference step, so a wrong number costs
  nothing. "Analysed" means a saved face result at any rate, plus a saved description when descriptions are on.

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
- **Built:** `run --preset thorough|balanced|fast`, a saved result that records its rule (`2.0`, or `2.0@30.0+0.5`), the
  Settings dialog with the estimated saving for the user's own library, and **Re-analyse** (`run --reanalyse`, a dry run) with
  the number of videos it touches and an upper bound for the time before it starts. Thorough is the unchanged ffmpeg call.
  The variable presets are two ffmpeg passes (the first 30 s at 2 fps, then the slow rate), and a clip that ends inside the
  first 30 s is cut exactly as under Thorough. Results made before presets existed (key ending `|2.0`) are read as Thorough.
  Two traps the roadmap did not mention: the face crop written to Photos is cut under the rule stored with the result, not
  the preset in use now (a frame number only means something under the rule that counted it), and the description model's
  six frames are picked by time, not by position, under the variable presets.
- **Measured on the real data, read-only:** all 1,548 saved results of the first run read under the new code (rule `2.0`,
  nothing to rebuild). The estimate from their frame counts is 37% fewer frames under Balanced and 44% under Fast (the
  roadmap's 38% and 45% came from the logged counts of 1,431 clips); 429 clips are longer than 30 s. On four real clips (two
  of 1,059 s, a rotated phone video of 283 s, one of 100 s; H.264, rotations 0, -180 and -90) Thorough reproduced the saved
  frame counts exactly (2,116, 567, 200), Balanced and Fast landed within one frame of the prediction, and the first 60
  frames were byte-identical across the three presets.

### 3. Counter at the start, and cached reference faces

- A counter for the phase that reads the library and builds the named people's reference faces: photos done of photos total
  and time left. It is written to the log, so the dashboard, a scheduled run and a terminal all see it, like the per-video
  progress.
- The reference faces are kept between runs and rebuilt on their own when the named people or their photos change
  (**my call**), so the 28 minutes are paid once and not on every run. This also keeps a 20-video test run from costing
  28 minutes. Not measured: how long that phase takes on a second run, when the photos are already downloaded
  (`cache/photos` holds 4.6 GB for 2,432 photos).
- **Built:** the faces found in each photo are saved per photo (`state/reffaces/`, keyed by the photo's size, time and the face
  models), so a new person, a new photo or a changed one only costs its own photos. The phase logs `references: D of T
  photos` (about 100 lines however many photos), and the dashboard shows photos done and time left.
- **Measured on the real library** (scratch data home, photos already local, a dry run of 5 videos with no description
  model): the first run read all 2,432 photos in **2 min 40 s** (it was 28 minutes with the download through the VPN); the
  second run read none and took **10 s**. The 45 named people had identical reference counts and thresholds in the overnight
  run's log, in the first run and in the second, and 60 of 60 saved photos were array-identical to a fresh read on the real
  face engine. What is left of the time before the counter starts (about 46 s) is listing 29,534 photos and 1,553 videos.

### 4. Live view of the current run

- The current run's results appear in the dashboard as they are made, instead of only in the final report. Per-video
  results are already saved as they are produced (`state/faces`, `state/describe`).
- Current run only. Not chosen: a view that adds up everything analysed across runs, and a label with the settings of each
  result.
- **Built:** every row is appended to `reports/current-run.jsonl` as it is made (the same row a report holds) and the run
  removes the file when its report is saved. The Videos and Overview tabs show those rows, with a "Live view" note, while a run
  holds the lock. The file is not a report (`run-*.json`), a run that died leaves it behind and it is never shown for a later
  run, and a row that is half written is skipped.

## Settings

- **One saved file** in the data home, used by the GUI, the terminal and any scheduled run. A command-line flag overrides
  the file for that one run.
- **My call:** the file never holds the run mode or the write cap, so a settings file can never switch on writing to
  Photos.
- **My call:** per-run choices (limit, slice, album) are not saved either, so a saved limit cannot quietly shorten a
  scheduled run. The file holds standing choices such as the preset and descriptions on or off.
- **Built:** `settings.json` in the data home holds `preset` and `describe` and nothing else; a file that contains a per-run
  choice (or anything unknown) is refused with a message rather than ignored, `videotag doctor` checks it, and flags win
  (`--preset`, `--describe`, `--no-describe`). The dashboard's run dialog always passes its choices as flags, so what it shows
  is what runs.

## Also wanted (not yet ranked against the must-haves)

### Twins: the same file under two ids

- In the first run 71 file names appear under two ids; 68 of those have the same frame count in both copies, 3 do not, so a
  name alone never makes a twin.
- A twin is the same name **and** the same size (and time). The item listing already returns `filesize` and `time`, so this
  needs no extra call.
- A twin is analysed once and its result is copied to the other id, so both videos get tags and descriptions.
- The GUI lists twins so they can be cleaned up in Photos. The tool never deletes a video.
- **Built:** the first of a set of twins is analysed and each later one gets a copy, saved under its own id (so it counts as
  analysed from then on and "not analysed yet" does not pick it up again). A twin whose original fails gets the same error.
  Rows say `twin_of`, and the Overview lists the files that sit in Photos under more than one number. The real library has
  such pairs: two of the longest clips used for the check above are the same 1,059 s file.

### Trust tools, before applying to the real library

All three were chosen. The first is as asked; the other two were left to me.

- **Spot-check:** a random sample of matches per person, to look at in Photos.
  **Built:** Overview, *Draw a sample*: per person, a few confident matches and a few suggestions from the latest report
  (what you already confirmed or removed, copies of a file and failed videos are left out). It names the file, because the
  link into Photos below is not built.
- **Accuracy report** (**my call**): per person, the number of reference faces, the threshold, how well they separate from
  the other named people and whether confident tags are allowed, plus which pairs of people get mixed up. It uses what
  `videotag calibrate` already works out. Comparing two runs and the known-video test are left out for now.
  **Built, and computed by every run** from the reference sets and models it already has, so no command and no NAS call:
  `reports/accuracy.json`. Per person: reference faces by kind, the threshold, whether confident tags are allowed, how many of
  the person's own faces (leave-one-out) would be recognised, own-faces against the closest other person, and a list of
  look-alike pairs. A finding while building it: with the calibrated thresholds "who gets mixed up" is empty by construction
  (each threshold is set above the best look-alike), so the report lists the look-alikes the thresholds had to be raised for,
  and "mixed up" appears only when a threshold was set by hand.
- **Evidence screen** (**my call**): during a run, keep the best few face crops of each match or suggestion in the data
  home, so they open instantly. They stay on this PC; nothing is uploaded to Photos, and videos without a match keep none.
  No full frames.
  **Built:** `state/evidence/<video>/<person>-<n>.jpg`, up to three per person, shown under each person in a video's detail.
  Only faces that person actually won count (a sibling's face is never offered as evidence of someone else). They are cut
  wherever the frames are already in hand: when a video is analysed, and once for a cached video that matches someone but has
  no crops yet, which downloads it once more (a cached video with its crops, or without a match, is never downloaded for this).

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
- **Built (folder route).** Two addresses were copied from the Photos web app, both from the account used for testing rather than the stored login:
  `https://<nas>/?launchApp=SYNO.Foto.AppInstance#/person/personal_space/<person id>/item/<item id>` (a video opened from a
  person's page) and `.../#/personal_space/folder/<folder id>/item/<item id>` (opened from a folder). The folder route needs no
  person, and `folder_id` is a field of every item in the listing (all 1,553 videos have one), so it works for any video, dry
  run or applied. The report row now records `folder_id`; the page links the file name (Videos tab and spot-check) in a new
  tab, only while the saved results belong to the current login. Reports made before this have no folder: no link until the
  next dry run.
- **Not verified:** the stored login cannot see the example ids (they belong to the testing account), so the link was
  checked against the format only. What a click opens for the stored login's own videos is the one thing left to look at; two
  real links for that are in the hand-over message of this session (a video of folder 635 and one of folder 107). The person
  route was not used: it would only open a video that is already on that person's page, which an unapplied match is not.

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
- Twins (above) also save downloads and model time, about 5% of videos. **Built** with the twins.

## Not picked in the interview (parked, not planned)

A stop button for a running dashboard run, a notification when a run ends, choosing which people are matched, a scheduled
daily run, running on the NAS (Docker), applying a slice, an adding-up Library view, and showing each result's settings.
Any of them can come back; none was asked for.

## Open

- That a click on a link opens the right video for the stored login (see above).
- Not measured yet, because it needs a long run: a full pass with the download-while-describing pipeline, the saved reference
  faces and a preset together (the estimate was about 4.6 hours, model-bound, for Thorough).
- The saved reference photos (`cache/photos`, 4.6 GB) are still kept in full; only their faces needed keeping.
- The diagrams in `docs/diagrams` still show the old flow; the text in `docs/architecture.md` is current.
- A first run after updating re-downloads each video that matches someone once, to cut its crops. A cached video is never
  downloaded for this otherwise.
- **How it was built (the order I suggested, and it held):** the saved-result key and the settings file first, then presets,
  slices, the counter with saved faces, the live view, the dashboard, twins and the trust tools.
