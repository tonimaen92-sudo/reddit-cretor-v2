# Development guide

## Environment

Python 3.11 is required. The existing `.venv` remains the launcher environment.

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

`requirements.txt` remains authoritative for launcher setup; `pyproject.toml`
provides package metadata, editable installation, entry points, and test-tool
configuration.

## Safe verification

The following checks are offline and do not intentionally control a phone or
contact an external service:

```powershell
.\.venv\Scripts\python.exe -m compileall -q src tests misc
.\.venv\Scripts\python.exe tools\run_offline_tests.py
.\.venv\Scripts\python.exe misc\creator_prompt_regress.py
```

The historical baseline is in [BASELINE_TESTS.md](BASELINE_TESTS.md). Some
regressions depend on runtime dumps and have pre-existing failures; compare
their result to that baseline instead of treating the repository as initially
green.

## Change rules

- Preserve root runtime paths and filenames.
- Add implementation under `src/friend_poster`; retain a matching legacy
  wrapper when an old `misc` command exists.
- Use explicit `friend_poster...` imports. Do not add `sys.path` mutation to
  implementation modules.
- Keep network and phone operations behind explicit user actions.
- Never commit credentials, account records, dumps, logs, media, or browser
  profiles.
- Separate mechanical moves from behavior changes in review.

The untouched source baseline is Git tag `pre-organization`. The checksum
manifest is `docs/PRE_REFACTOR_SHA256.txt`.
