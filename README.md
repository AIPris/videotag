# videotag

Makes the **videos** in a Synology Photos library searchable, unattended, by

- **who is in them**: tags, the person's People page and a per-person album,
- **what they show**: a bilingual (English and German) description with topics and objects, and a place when a landmark or readable sign shows one.

Synology Photos recognises faces in photos only. `videotag` reuses what Photos already knows (the people you named and their photos), matches video frames against those faces on your own PC, and writes the results back through the normal Photos features, so you search, browse and correct everything inside Photos. Nothing leaves your network except the one-time model downloads.

![Overview](docs/diagrams/system-overview/system-overview.png)

## What you get in Photos

| Where | What appears |
|---|---|
| Search box | A person's name finds their photos **and** their videos; so do topics and objects in either language (`Katze`, `cat`, `scuba`, `Weihnachtsbaum`). |
| People page of a person | Their photos plus every video they were matched in. |
| Tags | `Name` for confident matches, `Name?` for suggestions you can confirm. |
| Albums | One album per person: their photos plus their matched videos. |
| A video's description | A marked block `[vt] ... [/vt]` after your own text, in English and German. |

You correct results **in Photos**: remove a tag, remove a person from a video, delete or edit the description block, add the tag yourself. The tool learns from that and never fights you. See [the how-to](docs/howto.md#correcting-results).

## Quick start

Needs Windows, Python 3.11+, `ffmpeg` on PATH, Synology Photos (DSM 7) and, for the descriptions, an NVIDIA GPU with about 10 GB free memory.

```
pip install torch --index-url https://download.pytorch.org/whl/cu128   # CUDA build; skip with --no-describe
pip install --no-user -e ".[describe]"
videotag credentials set      # asks for the NAS address, account and password; stored in Windows Credential Manager
videotag doctor               # checks everything an unattended run needs
videotag run                  # DRY RUN: reads Photos, writes nothing, logs what it would do
videotag run --apply --max-writes 5     # first small write; check the result in Photos, then run without the limit
```

Full instructions, scheduling a daily run, correcting results, backups and troubleshooting: **[docs/howto.md](docs/howto.md)**.

## Safe by design

- **Dry run is the default.** Nothing is written without `--apply`, and `--max-writes` caps each kind of write per run.
- **Fully reversible:** `videotag undo` removes every tag, face link, description block and album the tool created, and nothing else. Your own tags, notes and edits are never touched.
- **Credentials** live in the OS credential store (or environment variables / secret files), never in the repository, a log or a report. HTTPS only, certificates always verified. See [docs/security.md](docs/security.md).
- **Local only:** faces and frames are processed on your PC. The description model runs locally on your GPU.

## Documentation

| | |
|---|---|
| [docs/howto.md](docs/howto.md) | Install, first run, scheduling, correcting results, maintenance, troubleshooting |
| [docs/architecture.md](docs/architecture.md) | How a run works, the modules, the files it keeps, the rules behind the feedback loop (with diagrams) |
| [docs/security.md](docs/security.md) | Credential storage, what data is kept where, what leaves the machine |
| [docs/synology-photos-api.md](docs/synology-photos-api.md) | The Photos web API calls used, including the undocumented ones, and their quirks |

## Status and limits

Version 0.1, proof of concept proven on one NAS (DS224+, DSM 7.4.1) with a small test library.

- Match and description quality has not been audited against a large library. The description model is small (4B parameters) and can over-reach; the German has some awkward words.
- Places are written only when a named landmark or readable text shows one. The Photos map stays empty: it needs GPS, and the tool never invents coordinates.
- The face-link calls are not documented by Synology. They come from captures of the Photos web UI by the [synopticon](https://github.com/fdebijl/synopticon) project and were verified on this NAS. A Photos update could change them.
- Tested on Windows 11 and Python 3.13. The scheduled task has not been registered on the reference machine yet; the how-to says how to test one.
- No license has been chosen yet.

## Development

```
pip install -e ".[dev]"
python -m pytest        # unit tests: no NAS, no GPU, never touches the real credential store
ruff check .
```
