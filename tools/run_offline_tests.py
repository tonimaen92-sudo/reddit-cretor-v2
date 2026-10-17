"""Run the repository's offline tests in isolated subprocesses.

This runner never invokes GUI main functions and does not create accounts,
post, rotate proxies, or control the phone. Some legacy regressions still
depend on missing live dump files; their exits are reported, not hidden.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS = sorted((ROOT / "tests" / "regression").glob("*_regress.py")) + sorted(
    (ROOT / "tests" / "unit").glob("test_*.py")
)


def main() -> int:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + environment.get(
        "PYTHONPATH", ""
    )
    failures: list[tuple[str, int]] = []
    for test in TESTS:
        relative = test.relative_to(ROOT)
        print(f"\n=== {relative} ===", flush=True)
        result = subprocess.run(
            [sys.executable, str(test)],
            cwd=ROOT,
            env=environment,
            timeout=180,
        )
        if result.returncode:
            failures.append((str(relative), result.returncode))
    print(f"\nOFFLINE {len(TESTS) - len(failures)} passed, {len(failures)} nonzero")
    for name, code in failures:
        print(f"NONZERO {code}: {name}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
