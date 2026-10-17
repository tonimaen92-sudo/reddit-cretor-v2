# UTF-8
"""Offline regressions for birthday_pool; never writes the real pool."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import birthday_pool  # noqa: E402


PASSED = 0
TOTAL = 0


def check(name: str, condition: bool) -> None:
    global PASSED, TOTAL
    TOTAL += 1
    print(("PASS " if condition else "FAIL ") + name, flush=True)
    if not condition:
        raise AssertionError(name)
    PASSED += 1


def write_pool(folder: Path, content: str) -> Path:
    path = folder / "pool.txt"
    path.write_text(content, encoding="utf-8", newline="")
    return path


def main() -> int:
    # Read the real fixture only to prove its shape, then validate a temp copy.
    real_before = birthday_pool.DEFAULT_POOL_PATH.read_bytes()
    real_content = birthday_pool.DEFAULT_POOL_PATH.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as raw_folder:
        folder = Path(raw_folder)
        fixture = write_pool(folder, real_content)
        check("real-fixture-has-expected-count", birthday_pool.validate_pool(fixture) == 145)
        check("real-fixture-copy-is-not-source", fixture != birthday_pool.DEFAULT_POOL_PATH)
        result = birthday_pool.consume_first(fixture)
        check("real-fixture-copy-consumes-first", result == "VALID 24 | POOL REMAINING: 144")
        check("real-fixture-copy-now-has-144", birthday_pool.validate_pool(fixture) == 144)
    check(
        "real-fixture-source-remains-unchanged",
        birthday_pool.DEFAULT_POOL_PATH.read_bytes() == real_before,
    )

    with tempfile.TemporaryDirectory() as raw_folder:
        folder = Path(raw_folder)
        pool = write_pool(folder, "2002-01-01\r\n2002-01-02\r\n")
        result = birthday_pool.consume_first(pool)
        check("valid-consume-output", result == "VALID 24 | POOL REMAINING: 1")
        check("valid-consume-removes-one", pool.read_text(encoding="utf-8") == "2002-01-02\n")
        check("valid-output-does-not-contain-source", "2002-01-01" not in result)

    for name, content in (
        ("impossible", "2002-02-30\n2002-03-01\n"),
        ("malformed", "01:01:2002\n2002-03-01\n"),
    ):
        with tempfile.TemporaryDirectory() as raw_folder:
            folder = Path(raw_folder)
            pool = write_pool(folder, content)
            before = pool.read_bytes()
            result = birthday_pool.consume_first(pool)
            check(f"{name}-returns-invalid", result == "INVALID format | POOL REMAINING: 2")
            check(f"{name}-does-not-mutate", pool.read_bytes() == before)

    with tempfile.TemporaryDirectory() as raw_folder:
        folder = Path(raw_folder)
        pool = write_pool(folder, "2013-06-06\n2002-01-01\n")
        before = pool.read_bytes()
        result = birthday_pool.consume_first(pool)
        check("under-thirteen-output", result == "INVALID under 13 | POOL REMAINING: 2")
        check("under-thirteen-does-not-mutate", pool.read_bytes() == before)

    with tempfile.TemporaryDirectory() as raw_folder:
        folder = Path(raw_folder)
        pool = write_pool(folder, "2013-06-05\n")
        before = pool.read_bytes()
        result = birthday_pool.consume_first(pool)
        check("exactly-thirteen-is-invalid", result == "INVALID under 13 | POOL REMAINING: 1")
        check("exactly-thirteen-is-preserved", pool.read_bytes() == before)

    with tempfile.TemporaryDirectory() as raw_folder:
        folder = Path(raw_folder)
        pool = write_pool(folder, "2012-06-04\n")
        result = birthday_pool.consume_first(pool)
        check("fourteen-is-valid", result == "VALID 14 | POOL REMAINING: 0")

    with tempfile.TemporaryDirectory() as raw_folder:
        folder = Path(raw_folder)
        pool = write_pool(folder, "2002-01-01\n2002-01-01\n2002-01-02\n")
        birthday_pool.consume_first(pool)
        check(
            "duplicate-removes-only-first",
            pool.read_text(encoding="utf-8") == "2002-01-01\n2002-01-02\n",
        )

    with tempfile.TemporaryDirectory() as raw_folder:
        folder = Path(raw_folder)
        pool = write_pool(folder, "\n \t\n2002-01-01\n\n2002-01-02\n")
        result = birthday_pool.consume_first(pool)
        check("blank-and-whitespace-lines-not-counted", result == "VALID 24 | POOL REMAINING: 1")
        check(
            "order-and-separators-preserved",
            pool.read_text(encoding="utf-8") == "\n \t\n\n2002-01-02\n",
        )

    with tempfile.TemporaryDirectory() as raw_folder:
        folder = Path(raw_folder)
        pool = write_pool(folder, "2002-01-01\n2002-01-02\n")
        before = pool.read_bytes()
        original_replace = birthday_pool.os.replace
        birthday_pool.os.replace = lambda _source, _target: (_ for _ in ()).throw(
            OSError("simulated replace failure")
        )
        try:
            try:
                birthday_pool.consume_first(pool)
            except OSError:
                pass
            else:
                raise AssertionError("replace failure was not propagated")
        finally:
            birthday_pool.os.replace = original_replace
        check("interrupted-write-preserves-source", pool.read_bytes() == before)
        check("interrupted-write-cleans-temp", list(folder.glob(".pool.txt.*.tmp")) == [])

    with tempfile.TemporaryDirectory() as raw_folder:
        folder = Path(raw_folder)
        pool = write_pool(folder, "\n\r\n")
        before = pool.read_bytes()
        result = birthday_pool.consume_first(pool)
        check("empty-pool-output", result == birthday_pool.EXHAUSTED_OUTPUT)
        check("empty-pool-not-rewritten", pool.read_bytes() == before)

    with tempfile.TemporaryDirectory() as raw_folder:
        folder = Path(raw_folder)
        pool = write_pool(folder, "2002-01-01\n")
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            code = birthday_pool.main([str(pool)])
        output = capture.getvalue().strip()
        check("cli-exits-successfully", code == 0)
        check("cli-output-is-exact", output == "VALID 24 | POOL REMAINING: 0")
        check("cli-output-hides-date", "2002-01-01" not in output)

    print(f"RESULT {PASSED}/{TOTAL} passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
