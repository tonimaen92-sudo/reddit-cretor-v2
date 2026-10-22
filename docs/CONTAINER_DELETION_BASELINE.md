# Container-deletion work: offline baseline and measurements

Captured 2026-10-17 with the existing `.venv` Python 3.11 interpreter, entirely
offline (mocked device ops, fake clocks, temporary fixtures). No phone, no
Crane, no live state file, and no real container deletion were used.

## Full offline runner

Command:

```
$env:PYTHONPATH="src"; .venv\Scripts\python.exe tools\run_offline_tests.py
```

| State | Result | Nonzero suites |
| --- | --- | --- |
| Before this work | `OFFLINE 26 passed, 6 nonzero` | `commenter_regress.py`, `create_regress.py`, `qa_scroll_regress.py`, `reddit_interests_regress.py`, `reply_regress.py`, `test_bulk_creation.py` |
| After this work | `OFFLINE 27 passed, 6 nonzero` | the same 6 suites |

The suite count rose by one because `tests/unit/test_container_deletion.py`
(45 tests, `OK`) was added. No suite moved from passing to failing and no new
nonzero suite appeared.

`test_bulk_creation.py` is unchanged: `Ran 21 tests`, `FAILED (failures=7,
errors=3)` — identical to the counts recorded in `docs/BASELINE_TESTS.md`. Its
failures are the pre-existing v4 migration expectations, not new regressions.

Focused suites run during development (all `OK`): `test_container_deletion` (45),
`test_bulk_preserve_skip` (21), `test_bulk_creator` (50), `test_scan_progress`
(11), `test_recovery_v5` (6), `test_crane_delete` (3); regression
`slots_regress.py` (exit 0) and `ghost_provision_regress.py` (exit 0).

## Container-preparation optimization (offline operation counts)

The optimization removes a redundant close/reopen plus a second full named scan
when `apply_reddit_containers` already holds a fresh, complete allocation
inventory that has not been invalidated by a device mutation.

Per the first unmutated target, the removed device operations are:

- 1 `close_ghost_organic`
- 1 `open_reddit_containers`
- 1 `dump_named`
- 1 `stop_if_reset`
- 1 full `scan_named_container_instances` (itself an unbounded
  dump/flick/relocate loop)

The unchanged final teardown `close_ghost_organic` is retained. Subsequent
targets invalidate the inventory and take the original scanned path.

This is an **offline device-operation-count** measurement only, verified by
`tests/unit/test_container_deletion.py`
(`test_reuse_skips_reopen_and_second_scan_and_uses_inventory_ids` asserts the
reopen/scan calls do not happen and that exactly one close remains, and
`test_apply_reuses_only_the_first_unmutated_target` asserts the inventory is
invalidated after the first mutation).

**Live performance was not measured.** No live batch was run, so the wall-clock
"≤30% of baseline (~70% faster)" target is *not* verified. The optimization
shaves a known set of navigation/dump/scan calls; a real speedup figure requires
a live timed batch.
