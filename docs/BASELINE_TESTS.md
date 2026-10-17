# Pre-refactor test baseline

Captured on 2026-10-17 with the existing `.venv` Python 3.11 interpreter,
before moving implementation modules into `src/friend_poster`.

| Command | Result | Notes |
| --- | ---: | --- |
| `python misc/create_regress.py` | 261 pass, 12 fail | Existing fixture/expectation failures; organization work must not silently reinterpret them as new regressions. |
| `python misc/commenter_regress.py` | Aborted by missing fixture | Existing `dumps/a3851_typed.json` dependency is absent. |
| `python misc/creator_prompt_regress.py` | 113 pass, 0 fail | Clean baseline. |
| Python 3.11 AST parse of every `misc/*.py` file | Pass | All source files parse successfully. |

The reorganization acceptance rule is behavioral parity with this baseline:
known failures may remain, but the refactor must not add failures. Tests that
depend on live runtime dumps are reported separately from self-contained tests.

An expanded post-move inventory was also run after confirming implementation
files differed from the tagged originals only by package imports and documented
path plumbing. It exposed additional already-present red tests:

- `qa_scroll_regress.py`: 36 pass, 1 fail.
- `reply_regress.py`: missing `dumps/3031_hunt4.json`.
- `reddit_interests_regress.py`: current live dump does not satisfy its pinned
  checked-pill expectation.
- `test_bulk_creation.py`: 7 failures and 3 errors from expectations that do
  not match the tagged implementation.

These were not corrected because this migration is organization-only.
