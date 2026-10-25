# Architecture

`videotag` is a command-line program with no server and no database. A run reads the Photos library through its web API, analyses the videos it has not seen, decides what to write, and writes it back. Everything it needs to remember is a few small files in one data folder. Diagram sources (`.mmd`, editable `.drawio`) are in `docs/diagrams/`.

![System overview](diagrams/system-overview/system-overview.png)

| Piece | Role |
|---|---|
| **Task Scheduler** | Starts `videotag run --apply` once a day. A lock file stops two runs overlapping. |
| **videotag** | The program: orchestration, decisions, and the adapters to the outside world. |
| **ffmpeg** | Cuts frames out of a video (handles HEVC and rotation metadata). |
| **FaceEngine** | OpenCV YuNet (finds faces) and SFace (turns a face into a 128-number embedding). CPU. |
| **Describer** | Qwen3-VL 4B on the GPU: reads 6 frames and writes a bilingual description. Loaded only when a video needs describing. |
| **Credential Manager** | Holds the NAS login, encrypted for the Windows user. |
| **Data home** | `state/` (what was written, user feedback, per-video caches), `cache/`, `models/`, `logs/`, `reports/`. |
| **Photos web API** | The only way in and out of the library. Photos itself stores the results: tags, face records, descriptions, albums. |

## One run

![One run](diagrams/run-pipeline/run-pipeline.png)

1. **load_library**: all photos and videos with their tags, people and descriptions.
2. **build_references**: for every *named* person, collect known faces (see below).
3. **person_models**: one model per person with its own similarity threshold.
4. **sync_feedback**: compare what the tool wrote last time with what is in Photos now, to learn what the user rejected or confirmed.
5. **_analyse every video**: faces and description, from the caches where possible.
6. **match_video**: who is in the video.
7. **plan_***: four pure planners compute the changes for tags, People-page face links, descriptions and albums. Nothing has been written yet.
8. **--apply?** Without it the plan is only logged. With it, the changes are executed (capped by `--max-writes` per kind), the state is saved after each write, and a JSON report is written.

### Analysing a video once

![Video analysis](diagrams/video-analysis/video-analysis.png)

Downloading and detecting are the slow parts, matching is cheap. Per video the tool caches the faces (embedding and position, `state/faces/<id>.npz`) and the description (`state/describe/<id>.json`), keyed by file size, modification time, sampling rate and model/prompt version. A changed reference set or threshold therefore never needs the videos again, and an unchanged library costs seconds per run. The video is downloaded at most once per run, to a temporary folder that is deleted afterwards.

## How people are recognised

Photos has no faces for videos, so the tool borrows the knowledge Photos has about **photos**:

- **References.** For each named person: photos where Photos lists exactly that one person and one face is found; the person's cover crop as a seed; and faces in group photos, assigned to the people Photos lists on that photo (one to one, similarity of at least 0.45, and a margin of 0.10 over the runner-up, otherwise skipped). The face found in the cover's source photo replaces the crop, so the same face is never counted twice.
- **Thresholds.** Per person: the best score of any *other* named person's faces, plus 0.05, never below OpenCV's documented 0.363. Other named people are known strangers, so no answer key is needed.
- **Matching.** Per frame, each face counts only for the person it resembles most, and only above that person's threshold. A video is a confident match with at least `--min-frames` (2) matching frames, a suggestion with one, and nothing with none.
- **Thin references.** A person with fewer than 3 reference faces never gets a confident tag: their matches become `Name?` suggestions until a human confirms.

## What is written, and who owns it

| Output | Stored in Photos as | Added when | Removed when |
|---|---|---|---|
| Tag `Name` | A general tag | Confident match | Match rejected, or `undo` |
| Tag `Name?` | A general tag | Suggestion | Confirmed, upgraded or rejected, or `undo` |
| People-page link | A face record on the video (position, crop, person) | Confident or confirmed | Rejected, or `undo` |
| Album `Name` | A normal album | Person has a confident or confirmed video | Videos leave when their tag is gone; `undo` deletes it |
| Description | A marked block inside the description | Video described | Block deleted by the user (a rejection), or `undo` |

Every write the tool makes is recorded in `state/state.json`. **Only what is recorded is ever removed**, so the user's own tags, notes, albums and faces are never touched.

### The feedback loop

![Feedback states](diagrams/feedback-states/feedback-states.png)

The tool's output is the question and the user's edit in Photos is the answer. On every run `sync_feedback` compares the recorded writes with what Photos shows now:

| The tool wrote | It is now | Meaning | Reaction |
|---|---|---|---|
| tag `Name` or `Name?` | gone | rejected | Remembered; the video is skipped for that person from now on |
| tag `Name?` | replaced by the user's own `Name` | confirmed | Remembered; the tool removes its own `Name?` |
| a face record | gone | rejected (People view) | As above, and the tool takes its own tag and album entry back |
| a description block | gone | rejected | Never written again |
| a description block | edited | the user owns it | Left alone, also by `undo` |
| a tag `Name` the user added | present, not recorded | confirmed | Never touched |

### Face links without duplicates

The People-page link uses Photos' own "tag a person" feature (see [the API notes](synology-photos-api.md#linking-a-video-to-a-person)). Two rules come from behaviour found on a real NAS: Photos may answer `add_face` with error 117 and store the face anyway (so the result is read back instead of trusting the code), and deleting one of two duplicate faces of a person drops the video from their page (so a second face is never created).

### The description block

```
[vt]
EN: <summary> | <topic and object keywords>
DE: <Zusammenfassung> | <Schlagwörter>
@ <place> (<evidence>)
[/vt]
```

Photos search matches any part of the text, so the block carries **no label words** that every video would share (an earlier format with "Topics:" and "Objects:" made searches for `top`, `object` or `video` return the whole library). The block is appended after the user's text. Blocks in the earlier `[videotag]` format are still recognised: if they were not, every old block would look like a user deletion. Keep that in mind if the marker ever changes again.

## Code structure

![Modules](diagrams/modules/modules.png)

| Module | Responsibility |
|---|---|
| `cli.py`, `admin.py`, `__main__.py` | Commands; credentials and `doctor` |
| `pipeline.py` | Orchestrates one run |
| `tagging.py`, `facelinks.py`, `descriptions.py`, `albums.py` | One feature each: feedback detection, planning (pure) and execution against Photos |
| `references.py`, `calibration.py`, `matcher.py`, `assignment.py` | Who is in a video: references, thresholds, matching, group-photo assignment |
| `synology.py` | The Photos web API client; errors never contain URLs or secrets |
| `faces.py`, `describe.py`, `scanner.py`, `frames.py` | Adapters: OpenCV, the vision-language model, ffmpeg |
| `state.py`, `facecache.py`, `descache.py`, `lock.py` | Files on disk |
| `credentials.py`, `config.py` | Where the login comes from; where files go |

Design rules the code follows: planning is pure and separate from executing (so every plan can be logged in a dry run and unit-tested without a NAS), anything with I/O is injected, heavy libraries (`torch`, `transformers`) are imported only when a description is needed, and the unit tests need neither a NAS, a GPU nor the real credential store.

## The data folder

`%LOCALAPPDATA%\videotag` (or `VIDEOTAG_HOME`):

```
state/state.json      what was written, rejections, confirmations (back this up)
state/faces/          per-video faces (rebuildable)
state/describe/       per-video descriptions (rebuildable)
state/run.lock        exists only while a run is active
cache/                reference photos and cover crops downloaded from Photos (rebuildable)
models/               face models (downloaded on first use)
logs/videotag.log     rotating, 1 MB x 4
reports/              one JSON per run
```

## Safety properties

- **Dry run by default**; writes need `--apply`, and each kind is capped by `--max-writes`.
- **Idempotent**: a second run with nothing new changes nothing (verified live, repeatedly).
- **Reversible**: `undo` removes everything recorded, and only that.
- **No overlapping runs**: an exclusive lock file, ignored after 6 hours.
- **Stops instead of guessing**: an unreadable state file aborts the run, because guessing "empty" could re-add things the user rejected.
- **Failures are contained**: one broken video, or a broken description model, is logged and skipped; the rest of the run continues.

## Known limits and ideas

- The face-link calls are undocumented and could change with a Photos update.
- Every run re-detects faces in the reference photos of all named people; for a library with thousands of reference photos those embeddings should be cached.
- The description model needs a GPU, so a container on the NAS could only do the people part (`--no-describe`) unless the NAS has a GPU.
- Topics, objects and places come from a small model and are written as free text, not as a controlled vocabulary.
