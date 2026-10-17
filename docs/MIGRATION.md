# Organization migration summary

## What changed

- The canonical implementation moved from flat `misc/*.py` files to the
  `src/friend_poster` package, grouped by ownership.
- Every original top-level `misc/*.py` file remains as a compatibility wrapper.
- Tests moved to `tests/unit` and `tests/regression`; fixtures moved to
  `tests/fixtures`.
- GUI entry modules are stable facades over controller modules. Settings
  persistence is separated for Creator, Poster, and Commenter; Creator input
  validation is also isolated.
- Project-root paths are centralized in `friend_poster.core.paths`.
- Backup source snapshots moved to `archive/legacy-source`; two copied/stale
  virtual environments moved to ignored `archive/local-envs`.
- Packaging and development metadata now live in `pyproject.toml`.

## What did not change

- Root batch filenames and their arguments.
- Legacy `python misc\<tool>.py` commands.
- Runtime paths: `.venv`, `logs`, `dumps`, `secrets.json`, `accounts.txt`,
  `full-send`, `posts`, and `used`.
- GUI labels, defaults, callback ordering, automation behavior, and external
  side effects.
- Existing failure behavior and error messages.

## Verification on 2026-10-17

- Python 3.11 compilation: pass for `src`, `tests`, and `misc`.
- Compatibility inventory: 114 wrappers and 5 batch files resolve.
- Editable package installation: pass.
- Creator, Poster, and Commenter construct and close before scheduled USB
  probes run: pass.
- Creator prompt regression: 113 pass, 0 fail (matches baseline).
- Creation regression: 261 pass, 12 fail (matches baseline).
- Expanded offline run: 21 scripts exit zero; 6 exit nonzero. The nonzero set
  consists of pre-existing assertion drift or dependencies on absent live dump
  files: `commenter_regress`, `create_regress`, `qa_scroll_regress`,
  `reddit_interests_regress`, `reply_regress`, and `test_bulk_creation`.

No account creation, posting, messaging, proxy rotation, phone control, or
external-model operation was run during acceptance.

## Review anchors

- Untouched baseline commit: `7152144`
- Untouched baseline tag: `pre-organization`
- Original file checksum manifest: `docs/PRE_REFACTOR_SHA256.txt`
