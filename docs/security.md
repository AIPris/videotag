# Security

## The NAS login

The login (address, account, password) is looked up in this order:

1. **Environment**: `VIDEOTAG_URL`, `VIDEOTAG_USER`, `VIDEOTAG_PASSWORD`. Each can instead be a `_FILE` variable holding the path of a file with the value (a Docker or Kubernetes secret): `VIDEOTAG_PASSWORD_FILE=/run/secrets/nas_password`.
2. **The operating system's credential store**, through [keyring](https://pypi.org/project/keyring/): Windows Credential Manager, macOS Keychain or Secret Service. It is encrypted for the logged-in user.

![Credential lookup](diagrams/credentials/credentials.png)

There is **no plaintext file fallback**, and `videotag` refuses to run on a machine whose keyring has no real backend (instead of quietly using a plaintext one).

```
videotag credentials set       # typed in a terminal (the password is not echoed, never a command-line argument),
                               # verified with a real login, then stored
videotag credentials status    # shows the source, address and account, never the password
videotag credentials delete
videotag credentials migrate old-creds.txt --delete   # one-time move from an old plaintext file; the file is
                                                      # deleted only after a login from the vault worked
```

Rules the code follows:

- The password is never written to the repository, the data folder, a log, a report or an error message. `Credentials.__repr__` hides it. Request errors never include URLs or form data (they carry the password and the session id).
- The address must be `https://`. Plain `http://` is refused unless `VIDEOTAG_ALLOW_INSECURE_HTTP=1` is set for a trusted test network.
- Certificates are always verified. A NAS with a private certificate authority or a self-signed certificate is trusted by pointing `VIDEOTAG_CA_BUNDLE` at its certificate file, never by switching verification off.
- The unit tests run against an in-memory vault and a guard test fails if they ever see the real one, so tests cannot overwrite the live login.

### Recommended setup

- **Use a dedicated NAS account** for this tool, not an administrator. It needs access to the Photos personal space whose videos you want processed, nothing else. It does not need DSM admin rights.
- **Rotate the password** (change it in DSM, then run `videotag credentials set`) whenever it may have been exposed: pasted into a chat, shown on screen, committed by mistake.
- Windows Credential Manager entries belong to one Windows user. A scheduled task must run as **that same user**. `videotag doctor` reads the secret the same way a task would; test a registered task once (see the how-to).

## What the tool keeps, and where

Everything lives in one data folder (`%LOCALAPPDATA%\videotag`, or `VIDEOTAG_HOME`), outside the source tree:

| Folder | Contents | Sensitivity |
|---|---|---|
| `state/state.json` | The tags, face links, descriptions and albums the tool created, and your rejections and confirmations | Low (ids and names) |
| `state/faces/` | Face embeddings and positions found in each video | **Biometric data**: treat like the photos themselves. Safe to delete, it is rebuilt |
| `state/describe/` | The generated description text per video | Describes your videos; safe to delete |
| `cache/` | Original reference photos and cover crops downloaded from Photos | **Personal photos**. Safe to delete |
| `models/` | Face detection and recognition models | None |
| `logs/`, `reports/` | What each run did: file names, scores, descriptions | Describes your videos |

Videos themselves are downloaded to a temporary folder, processed and deleted again within the same step.

## What leaves the machine

- Requests to **your NAS** (HTTPS): reading the library, downloading originals, writing tags, faces, descriptions and albums.
- **One-time downloads**: the OpenCV face models from `github.com/opencv/opencv_zoo`, and the description model `Qwen/Qwen3-VL-4B-Instruct` from Hugging Face (about 9 GB), plus Python packages at install time.
- Nothing else: no telemetry, no cloud AI service. Faces, frames and descriptions are processed locally.

## Reporting a problem

If you find a security issue, do not open a public issue with details; contact the repository owner directly.
