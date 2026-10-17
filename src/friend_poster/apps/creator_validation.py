"""Pure validation rules used by the Creator controller."""

from __future__ import annotations

import re
from datetime import date, datetime


def parse_slot_birth_date(raw: str, *, today: date | None, year_min: int, year_max: int, error_type: type[Exception]) -> date:
    value = str(raw or "").strip()
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value) is None:
        raise error_type("Account birth date must use strict DD.MM.YYYY format.")
    try:
        configured = datetime.strptime(value, "%d.%m.%Y").date()
    except ValueError as exc:
        raise error_type("Account birth date must be a real calendar date.") from exc
    if not year_min <= configured.year <= year_max:
        raise error_type(f"Account birth date year must be between {year_min} and {year_max}.")
    now = today or date.today()
    age = now.year - configured.year - ((now.month, now.day) < (configured.month, configured.day))
    if age < 18:
        raise error_type("Account birth date must be at least 18 years old.")
    return configured


def parse_creation_swipe_count(raw: str) -> int:
    value = str(raw or "").strip()
    if re.fullmatch(r"[1-9]\d*", value) is None:
        raise ValueError("Creation swipe count must be a positive whole number.")
    return int(value)


def parse_creation_pause_count(raw: str, swipe_count: int) -> int:
    value = str(raw or "").strip()
    if re.fullmatch(r"\d+", value) is None:
        raise ValueError("Creation pause count must be a whole number.")
    pause_count = int(value)
    if pause_count > int(swipe_count):
        raise ValueError("Creation pause count cannot exceed the swipe count.")
    return pause_count


def unpack_creation_options(options) -> tuple[date, int, int]:
    if len(options) == 2:
        birth_date, swipe_count = options
        return birth_date, int(swipe_count), 0
    birth_date, swipe_count, pause_count = options
    return birth_date, int(swipe_count), int(pause_count)
