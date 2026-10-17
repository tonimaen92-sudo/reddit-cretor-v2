# UTF-8
"""Standalone, local birthday-pool validation and consumption.

This module has no phone, account, or Creator integration.  Executing it consumes
the first nonblank ISO date in the project-root ``Birthdays.txt.txt`` file.
"""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from datetime import date
from pathlib import Path
from typing import Sequence

from friend_poster.core.paths import PROJECT_ROOT

DEFAULT_POOL_PATH = PROJECT_ROOT / "Birthdays.txt.txt"
REFERENCE_DATE = date(2026, 6, 5)
ISO_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
EXHAUSTED_OUTPUT = "POOL EXHAUSTED. All dates used."


class BirthdayPoolFormatError(ValueError):
    """Raised when a nonblank pool entry is not an exact real ISO date."""


def parse_pool_date(raw: str) -> date:
    """Parse one exact ``YYYY-MM-DD`` value without echoing it in errors."""
    if ISO_DATE_PATTERN.fullmatch(raw) is None:
        raise BirthdayPoolFormatError("birthday pool entry has invalid format")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise BirthdayPoolFormatError(
            "birthday pool entry has invalid format"
        ) from exc


def age_in_years(birthday: date, *, reference_date: date = REFERENCE_DATE) -> int:
    """Return the completed age in years on the fixed reference date."""
    return reference_date.year - birthday.year - (
        (reference_date.month, reference_date.day)
        < (birthday.month, birthday.day)
    )


def _read_physical_lines(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.readlines()


def _entry_text(line: str) -> str:
    return line.rstrip("\r\n")


def _nonblank_indices(lines: Sequence[str]) -> list[int]:
    return [
        index
        for index, line in enumerate(lines)
        if _entry_text(line).strip() != ""
    ]


def validate_pool(path: str | os.PathLike[str] = DEFAULT_POOL_PATH) -> int:
    """Validate every nonblank entry and return the remaining pool size."""
    lines = _read_physical_lines(Path(path))
    indices = _nonblank_indices(lines)
    for index in indices:
        parse_pool_date(_entry_text(lines[index]))
    return len(indices)


def _atomic_write(path: Path, lines: Sequence[str]) -> None:
    """Replace *path* atomically using a flushed temporary file beside it."""
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.writelines(lines)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def consume_first(path: str | os.PathLike[str] = DEFAULT_POOL_PATH) -> str:
    """Validate and consume only the first nonblank pool entry.

    Invalid or age-ineligible entries are left untouched.  The returned status
    never includes the source date.
    """
    pool_path = Path(path)
    lines = _read_physical_lines(pool_path)
    indices = _nonblank_indices(lines)
    remaining = len(indices)
    if remaining == 0:
        return EXHAUSTED_OUTPUT

    first_index = indices[0]
    try:
        birthday = parse_pool_date(_entry_text(lines[first_index]))
    except BirthdayPoolFormatError:
        return f"INVALID format | POOL REMAINING: {remaining}"

    age = age_in_years(birthday)
    if age <= 13:
        return f"INVALID under 13 | POOL REMAINING: {remaining}"

    del lines[first_index]
    _atomic_write(pool_path, lines)
    return f"VALID {age} | POOL REMAINING: {remaining - 1}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Consume one valid date from a local ISO birthday pool."
    )
    parser.add_argument(
        "pool",
        nargs="?",
        default=str(DEFAULT_POOL_PATH),
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    print(consume_first(args.pool), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
