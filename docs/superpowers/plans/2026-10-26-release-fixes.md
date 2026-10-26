# Release readiness fixes

> **For agentic workers:** Use the reviewed defect list below as the implementation contract. Follow test-driven development and obtain a fresh review before integration.

**Goal:** Correct the release review findings, verify the complete release, and merge `next-version` into `main` and push as requested.

**Architecture:** Keep the current module boundaries. Preserve valid per-video caches, derive album membership from actual writes, and bind cached dashboard views and evidence to the data that produced them.

**Tech stack:** Python 3.11+, pytest, Ruff; standard browser JavaScript with Node's built-in test runner for asynchronous UI regressions.

**Spec:** The release review in this conversation and `docs/roadmap.md`; the user explicitly requested fixing all reported defects.

## Constraints

- Work on the existing `next-version` branch; preserve unrelated changes on `main` during integration.
- No NAS writes, real credential access, or modifications to personal data during tests.
- Preserve valid saved analysis unless explicit reanalysis requests replacement.
- Keep dry runs read-only on the NAS and respect write budgets independently for each kind of write.
- No extra runtime dependencies or unrelated refactors.
- Keep model failures retryable while preserving completed unusable description markers.

## Review focus

- Twins with different saved presets and existing descriptions; cached twins when the first video fails.
- Capped additions and failed removals must not make the review album contradict current suggestion tags.
- Unknown, invalid, or unavailable duration cannot establish that a failed extraction reached the clip's end.
- Changed reference models can change winning faces even when the matched person IDs remain the same.
- Account changes and interrupted or expired runs must invalidate displayed data, including requests still in flight.

## Task 1: Correct release regressions

**Files:** `videotag/analysis.py`, `frames.py`, `albums.py`, `pipeline.py`, `tagging.py`, `evidence.py`, `dashboard.py`, `gui_assets/app.js`, corresponding tests, and CI configuration if needed for JavaScript regression tests.

**Interfaces:** Retain public CLI behavior and existing function return contracts where practical. Any new success/provenance information must be explicitly passed between its producer and consumer, not inferred from proposed actions.

- [x] Add failing tests for each defect and run them before implementation.
- [x] Preserve a twin's valid cached faces and description rather than unconditionally overwriting them; still reuse work for missing results and honor explicit reanalysis.
- [x] Preserve the cached description-completion marker when an answer was attempted but unusable, without caching failures.
- [x] Reject failed tail extraction unless the clip's end is positively established; retain correct short-clip behavior.
- [x] Prevent album creation or mutation with a zero budget; consistently reject negative caps.
- [x] Build applied review-album membership from successful tag changes/current tags, while keeping dry-run previews.
- [x] Record evidence provenance and refresh crops when the current winning sightings change; migrate old crops safely.
- [x] Clear/account-bind spot-checks and discard stale responses after account changes.
- [x] Refresh library views when a live run ends or its lock expires, including interrupted runs without a report.
- [x] Run focused tests and self-review the complete fix diff.

## Task 2: Review, integrate, and verify

- [ ] Independently review the fixes and resolve any remaining release blockers.
- [ ] Run the full Python suite, Ruff, JavaScript regression/syntax checks, and package build/import checks.
- [ ] Merge current `origin/main` into the release branch, preserving its landing page and metadata while updating the changed feature descriptions.
- [ ] Verify the combined tree and commit the integration.
- [ ] Merge into `main`, push the authorized branches, and verify GitHub CI and remote commit IDs.

## Progress

- Baseline `f2349ef`: 669 Python tests passed; Ruff, JavaScript syntax, wheel build/import and CLI smoke checks passed during the release review.
- All reported defects have offline reproductions; no source changes were made during that review.
- Fix validation: 701 Python tests, six Node behavioral tests, Ruff and JavaScript syntax checks passed. An isolated real-browser check passed settings, dry/apply controls, undo confirmation, account/sample switching and interrupted-live recovery without NAS access.
