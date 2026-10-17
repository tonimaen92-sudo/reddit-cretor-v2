# UTF-8
"""Complete Reddit About you through the Interests screen. No Skip."""

from __future__ import annotations

import os
import re
import statistics
import sys
import time
from calendar import monthrange
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import DUMP_DIR
from ssh_device import Phone, PhoneError, load_secrets
from ui_tree import TapStack, collapsed_text, dump_named, find_label_exact

os.environ["SKIP_SHOT"] = "1"

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
BIRTHDAY_WHEEL_CENTER_Y = 675
BIRTHDAY_WHEEL_MAX_DISTANCE = 30
BIRTHDAY_ADJACENT_ROW_MIN_DISTANCE = 25
BIRTHDAY_ADJACENT_ROW_MAX_DISTANCE = 80
BIRTHDAY_SUMMARY_CENTER_Y_MIN = 390
BIRTHDAY_SUMMARY_CENTER_Y_MAX = 455
BIRTHDAY_TRANSITION_MAX_DUMPS = 8
BIRTHDAY_OCR_RETRY_DUMPS = 3
BIRTH_YEAR_MIN = 2002
BIRTH_YEAR_MAX = 2006
BIRTHDAY_AXIS_MAX_MOVES = {"year": 40, "month": 8, "day": 14}
BIRTHDAY_AXIS_UNCHANGED_LIMIT = 2
BIRTHDAY_MAX_ROWS_PER_GESTURE = 3
BIRTHDAY_MIN_DRAG_FACTOR = 1.35
BIRTHDAY_TITLE_LABELS = ("Birthday", "Birthdav")


def _normalized_month_text(text: str) -> str:
    """Normalize one proven birthday-month OCR token locally."""
    value = str(text or "").casefold()
    if value == "\u043c\u0430\u0443":
        return "may"
    return value


def log(msg: str) -> None:
    print(msg, flush=True)


def _wheel_values(nodes, predicate):
    candidates = []
    for node in nodes:
        if node.ntype == "ocr-line" or not node.visible or not node.on_screen():
            continue
        text = collapsed_text(node.labels())
        if predicate(text, node):
            candidates.append(node)
    return candidates


def _nearest_wheel_value(nodes, predicate):
    candidates = _wheel_values(nodes, predicate)
    if not candidates:
        return None
    node = min(candidates, key=lambda item: abs(item.center()[1] - BIRTHDAY_WHEEL_CENTER_Y))
    if abs(node.center()[1] - BIRTHDAY_WHEEL_CENTER_Y) > BIRTHDAY_WHEEL_MAX_DISTANCE:
        return None
    return node


def selected_birthday_nodes(nodes):
    """Return the centered day, month, and year OCR nodes, or None."""
    day = _nearest_wheel_value(
        nodes,
        lambda text, node: node.center()[0] < 230
        and text.isdigit()
        and 1 <= int(text) <= 31,
    )
    month = _nearest_wheel_value(
        nodes,
        lambda text, node: 220 <= node.center()[0] < 500
        and _normalized_month_text(text) in MONTHS,
    )
    year = _nearest_wheel_value(
        nodes,
        lambda text, node: node.center()[0] >= 480
        and re.fullmatch(r"(?:19|20)\d{2}", text) is not None,
    )
    if day is None or month is None or year is None:
        return None
    return day, month, year


def _birthday_summary_value(nodes):
    """Return the single bounded Reddit birthday summary, or None."""
    if (
        find_label_exact(nodes, ("About you",)) is None
        or find_label_exact(nodes, BIRTHDAY_TITLE_LABELS) is None
        or find_label_exact(nodes, ("Continue",)) is None
    ):
        return None
    month_pattern = "|".join(name.title() for name in MONTHS)
    pattern = re.compile(
        rf"^(\d{{1,2}})\s+({month_pattern})\s+((?:19|20)\d{{2}})$",
        re.IGNORECASE,
    )
    matches = []
    for node in nodes:
        if node.ntype == "ocr-line" or not node.visible or not node.on_screen():
            continue
        _x, center_y = node.center()
        if not BIRTHDAY_SUMMARY_CENTER_Y_MIN <= center_y <= BIRTHDAY_SUMMARY_CENTER_Y_MAX:
            continue
        match = pattern.fullmatch(collapsed_text(node.labels()))
        if match is None:
            continue
        day = int(match.group(1))
        month = MONTHS[match.group(2).casefold()]
        year = int(match.group(3))
        try:
            date(year, month, day)
        except ValueError:
            continue
        matches.append((day, month, year))
    return matches[0] if len(matches) == 1 else None


def _birthday_from_summary_with_adjacent_day_proof(nodes):
    """Recover one OCR-missing centered day using bounded independent evidence."""
    summary = _birthday_summary_value(nodes)
    if summary is None:
        return None
    summary_day, summary_month, summary_year = summary

    month_node = _nearest_wheel_value(
        nodes,
        lambda text, node: 220 <= node.center()[0] < 500
        and _normalized_month_text(text) in MONTHS,
    )
    year_node = _nearest_wheel_value(
        nodes,
        lambda text, node: node.center()[0] >= 480
        and re.fullmatch(r"(?:19|20)\d{2}", text) is not None,
    )
    if month_node is None or year_node is None:
        return None
    if (
        MONTHS[_normalized_month_text(collapsed_text(month_node.labels()))]
        != summary_month
        or int(collapsed_text(year_node.labels())) != summary_year
    ):
        return None

    day_rows = _axis_nodes(nodes, "day")
    above = [item for item in day_rows if item[1].center()[1] < BIRTHDAY_WHEEL_CENTER_Y]
    below = [item for item in day_rows if item[1].center()[1] > BIRTHDAY_WHEEL_CENTER_Y]
    if not above or not below:
        return None
    above_value, above_node = max(above, key=lambda item: item[1].center()[1])
    below_value, below_node = min(below, key=lambda item: item[1].center()[1])
    above_distance = BIRTHDAY_WHEEL_CENTER_Y - above_node.center()[1]
    below_distance = below_node.center()[1] - BIRTHDAY_WHEEL_CENTER_Y
    if not (
        BIRTHDAY_ADJACENT_ROW_MIN_DISTANCE
        <= above_distance
        <= BIRTHDAY_ADJACENT_ROW_MAX_DISTANCE
        and BIRTHDAY_ADJACENT_ROW_MIN_DISTANCE
        <= below_distance
        <= BIRTHDAY_ADJACENT_ROW_MAX_DISTANCE
    ):
        return None
    month_days = monthrange(summary_year, summary_month)[1]
    expected_above = month_days if summary_day == 1 else summary_day - 1
    expected_below = 1 if summary_day == month_days else summary_day + 1
    if above_value != expected_above or below_value != expected_below:
        return None
    return summary


def selected_birthday(nodes):
    """Return a strictly proven wheel date, including one bounded OCR fallback."""
    selected = selected_birthday_nodes(nodes)
    if selected is None:
        return _birthday_from_summary_with_adjacent_day_proof(nodes)
    day, month, year = selected
    return (
        int(collapsed_text(day.labels())),
        MONTHS[_normalized_month_text(collapsed_text(month.labels()))],
        int(collapsed_text(year.labels())),
    )


def parse_reddit_birth_date(raw, *, today: date | None = None) -> date:
    """Validate the private configured birthday without echoing it in errors."""
    value = str(raw or "").strip()
    if not value:
        raise PhoneError(
            "Missing private reddit_birth_date in secrets.json. Use ISO YYYY-MM-DD."
        )
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise PhoneError(
            "Private reddit_birth_date must be a real ISO date in YYYY-MM-DD format."
        )
    try:
        configured = date.fromisoformat(value)
    except ValueError as exc:
        raise PhoneError(
            "Private reddit_birth_date must be a real ISO date in YYYY-MM-DD format."
        ) from exc
    if not BIRTH_YEAR_MIN <= configured.year <= BIRTH_YEAR_MAX:
        raise PhoneError(
            "Private reddit_birth_date year must be between 2002 and 2006."
        )
    now = today or date.today()
    age = now.year - configured.year - (
        (now.month, now.day) < (configured.month, configured.day)
    )
    if age < 18:
        raise PhoneError("Private reddit_birth_date must be at least 18 years old.")
    return configured


def configured_reddit_birth_date(*, secrets=None, today: date | None = None) -> date:
    """Load and validate the birthday from the ignored secrets.json file."""
    payload = load_secrets() if secrets is None else secrets
    if not isinstance(payload, dict):
        payload = {}
    return parse_reddit_birth_date(payload.get("reddit_birth_date"), today=today)


def _axis_value(axis: str, text: str, node) -> int | None:
    x = node.center()[0]
    if axis == "day":
        if x >= 230 or not text.isdigit() or not 1 <= int(text) <= 31:
            return None
        return int(text)
    if axis == "month":
        if not 220 <= x < 500:
            return None
        return MONTHS.get(_normalized_month_text(text))
    if axis == "year":
        if x < 480 or re.fullmatch(r"(?:19|20)\d{2}", text) is None:
            return None
        return int(text)
    raise ValueError(f"unknown birthday axis: {axis}")


def _axis_nodes(nodes, axis: str):
    result = []
    for node in nodes:
        if node.ntype == "ocr-line" or not node.visible or not node.on_screen():
            continue
        text = collapsed_text(node.labels())
        value = _axis_value(axis, text, node)
        if value is not None:
            result.append((value, node))
    return sorted(result, key=lambda item: item[1].center()[1])


def _axis_row_pitch(nodes, axis: str) -> int:
    visible = _axis_nodes(nodes, axis)
    gaps = [
        later.center()[1] - earlier.center()[1]
        for (_av, earlier), (_bv, later) in zip(visible, visible[1:])
        if 25 <= later.center()[1] - earlier.center()[1] <= 80
    ]
    if not gaps:
        raise PhoneError(
            f"Birthday {axis} wheel spacing is not proven. Did not move the picker."
        )
    return int(round(statistics.median(gaps)))


def _axis_distance(axis: str, current: int, target: int, target_date: date) -> int:
    """Return signed rows; positive values move toward increasing wheel values."""
    if axis == "year":
        return target - current
    size = 12 if axis == "month" else monthrange(target_date.year, target_date.month)[1]
    if not 1 <= current <= size or not 1 <= target <= size:
        raise PhoneError(
            f"Birthday {axis} wheel contains an invalid centered value. Did not move it."
        )
    forward = (target - current) % size
    backward = forward - size
    return forward if abs(forward) <= abs(backward) else backward


def _visible_target_node(nodes, axis: str, target: int):
    matches = [node for value, node in _axis_nodes(nodes, axis) if value == target]
    if not matches:
        return None
    return min(matches, key=lambda node: abs(node.center()[1] - BIRTHDAY_WHEEL_CENTER_Y))


def _day_target_just_outside_visible_window(
    nodes,
    target: int,
    target_date: date,
    signed_distance: int,
) -> bool:
    """Prove that the target is exactly one row beyond the visible day edge."""
    size = monthrange(target_date.year, target_date.month)[1]
    selected_nodes = selected_birthday_nodes(nodes)
    rows = _axis_nodes(nodes, "day")
    if selected_nodes is None or not rows:
        return False
    centered_node = selected_nodes[0]
    centered_index = min(
        range(len(rows)),
        key=lambda index: abs(
            rows[index][1].center()[1] - centered_node.center()[1]
        ),
    )

    first = centered_index
    while first > 0:
        previous_value, previous_node = rows[first - 1]
        current_value, current_node = rows[first]
        gap = current_node.center()[1] - previous_node.center()[1]
        if not 25 <= gap <= 80 or current_value != (previous_value % size) + 1:
            break
        first -= 1

    last = centered_index
    while last + 1 < len(rows):
        current_value, current_node = rows[last]
        next_value, next_node = rows[last + 1]
        gap = next_node.center()[1] - current_node.center()[1]
        if not 25 <= gap <= 80 or next_value != (current_value % size) + 1:
            break
        last += 1

    visible = [value for value, _node in rows[first : last + 1]]
    if not visible or target in visible:
        return False
    if signed_distance < 0:
        predecessor = ((visible[0] - 2) % size) + 1
        return target == predecessor
    if signed_distance > 0:
        successor = (visible[-1] % size) + 1
        return target == successor
    return False


def _move_birthday_axis(
    stack: TapStack,
    nodes,
    axis: str,
    direction: int,
    rows: int,
    *,
    retry: int = 0,
) -> None:
    selected_nodes = selected_birthday_nodes(nodes)
    if selected_nodes is None:
        raise PhoneError("Birthday picker values are not proven. Did not move a wheel.")
    selected_node = {
        "day": selected_nodes[0],
        "month": selected_nodes[1],
        "year": selected_nodes[2],
    }[axis]
    visible = _axis_nodes(nodes, axis)
    if len(visible) < 2:
        raise PhoneError(
            f"Birthday {axis} wheel bounds are not proven. Did not move the picker."
        )
    pitch = _axis_row_pitch(nodes, axis)
    x, center_y = selected_node.center()
    rows = max(1, min(int(rows), BIRTHDAY_MAX_ROWS_PER_GESTURE))
    top = visible[0][1].center()[1] + max(6, pitch // 5)
    bottom = visible[-1][1].center()[1] - max(6, pitch // 5)
    available = bottom - top
    if available < pitch:
        raise PhoneError(
            f"Birthday {axis} wheel drag area is not proven. Did not move the picker."
        )
    distance = max(
        int(round(pitch * BIRTHDAY_MIN_DRAG_FACTOR)),
        int(round(pitch * (rows + 0.25 + (0.5 * retry)))),
    )
    distance = min(distance, available)
    half = distance // 2
    if direction > 0:
        y1 = min(bottom, center_y + half)
        y2 = y1 - distance
        if y2 < top:
            y2 = top
            y1 = top + distance
    else:
        y1 = max(top, center_y - half)
        y2 = y1 + distance
        if y2 > bottom:
            y2 = bottom
            y1 = bottom - distance
    steps = max(12, min(22, distance // 11))
    stack.interp_flick(x, y1, x, y2, steps=steps, ms=20)


def _set_birthday_axis(
    stack: TapStack,
    phone: Phone,
    nodes,
    axis: str,
    target: int,
    target_date: date,
    *,
    locked_axes: dict[str, int] | None = None,
):
    """Set one wheel with bounded, dump-verified taps or gestures."""
    axis_index = {"day": 0, "month": 1, "year": 2}[axis]
    unchanged = 0
    current_nodes = nodes
    day_reversal_pending = False
    day_reversal_used = False
    for move_count in range(BIRTHDAY_AXIS_MAX_MOVES[axis] + 1):
        selected = selected_birthday(current_nodes)
        if selected is None:
            raise PhoneError(
                f"Birthday {axis} selection is unclear. Did not move the picker."
            )
        current = selected[axis_index]
        if current == target:
            log(f"Birthday {axis} selection verified.")
            return current_nodes
        if move_count >= BIRTHDAY_AXIS_MAX_MOVES[axis]:
            raise PhoneError(
                f"Birthday {axis} did not reach the configured value within the movement bound."
            )

        old_signed_distance = _axis_distance(axis, current, target, target_date)
        old_distance = abs(old_signed_distance)
        visible_target = _visible_target_node(current_nodes, axis, target)
        movement_kind = "tap"
        if visible_target is not None:
            log(f"Birthday {axis}: selecting an exact visible row.")
            stack.tap_xy(*visible_target.center())
        else:
            if old_signed_distance == 0:
                raise PhoneError(
                    f"Birthday {axis} target comparison was inconsistent. Did not move it."
                )
            gesture_rows = min(old_distance, BIRTHDAY_MAX_ROWS_PER_GESTURE)
            gesture_retry = unchanged
            if day_reversal_pending:
                if axis != "day" or day_reversal_used:
                    raise PhoneError(
                        "Birthday day-wheel reversal state was inconsistent. "
                        "Did not move the picker."
                    )
                movement_kind = "reversal"
                gesture_rows = 1
                gesture_retry = 0
                day_reversal_pending = False
                day_reversal_used = True
                log("Birthday day: one bounded overshoot reversal.")
            elif axis == "day" and old_distance <= 3:
                movement_kind = "approach"
                gesture_rows = 1
                gesture_retry = 0
                log("Birthday day: one-row final approach.")
            else:
                movement_kind = "normal"
                log(f"Birthday {axis}: bounded adjustment {move_count + 1}.")
            _move_birthday_axis(
                stack,
                current_nodes,
                axis,
                1 if old_signed_distance > 0 else -1,
                gesture_rows,
                retry=gesture_retry,
            )

        fresh = stable_birthday_dump(
            stack,
            phone,
            f"910_birthday_{axis}_{move_count + 1}",
            shot=False,
        )
        fresh_selected = selected_birthday(fresh)
        if fresh_selected is None:
            raise PhoneError(
                f"Birthday {axis} picker became unclear after a movement."
            )
        fresh_values = {
            "day": fresh_selected[0],
            "month": fresh_selected[1],
            "year": fresh_selected[2],
        }
        for locked_axis, locked_value in (locked_axes or {}).items():
            if fresh_values.get(locked_axis) != locked_value:
                raise PhoneError(
                    f"Birthday {locked_axis} changed while setting {axis}. "
                    "Did not continue."
                )
        try:
            date(fresh_selected[2], fresh_selected[1], fresh_selected[0])
        except ValueError as exc:
            raise PhoneError(
                "Birthday wheels do not form a real date. Did not continue."
            ) from exc
        fresh_value = fresh_selected[axis_index]
        if fresh_value == current:
            if movement_kind == "reversal":
                raise PhoneError(
                    "Birthday day-wheel overshoot recovery did not move. "
                    "Did not try another reversal."
                )
            unchanged += 1
            if unchanged >= BIRTHDAY_AXIS_UNCHANGED_LIMIT:
                raise PhoneError(
                    f"Birthday {axis} wheel did not move after two verified attempts."
                )
            log(f"Birthday {axis} was unchanged; one bounded retry is allowed.")
            current_nodes = fresh
            continue

        new_signed_distance = _axis_distance(
            axis, fresh_value, target, target_date
        )
        new_distance = abs(new_signed_distance)
        crossed_target = old_signed_distance * new_signed_distance < 0
        fresh_visible_target = _visible_target_node(fresh, axis, target)

        if movement_kind == "reversal" and fresh_value != target:
            if crossed_target:
                raise PhoneError(
                    "Birthday day-wheel recovery crossed the configured value again. "
                    "Did not try another reversal."
                )
            if new_distance >= old_distance:
                raise PhoneError(
                    "Birthday day-wheel overshoot recovery did not reduce the distance. "
                    "Did not try another reversal."
                )
        elif axis == "day" and crossed_target and fresh_value != target:
            if fresh_visible_target is not None:
                log(
                    "Birthday day target became exactly visible after momentum; "
                    "retaining the verified screen."
                )
            elif (
                axis == "day"
                and movement_kind in ("approach", "normal")
                and not day_reversal_used
                and _day_target_just_outside_visible_window(
                    fresh,
                    target,
                    target_date,
                    new_signed_distance,
                )
            ):
                day_reversal_pending = True
                log(
                    "Birthday day target is one row beyond the visible edge; "
                    "one short reversal is allowed."
                )
            else:
                raise PhoneError(
                    f"Birthday {axis} wheel crossed the configured value without "
                    "a proven safe recovery."
                )
        elif fresh_value != target and new_distance >= old_distance:
            raise PhoneError(
                f"Birthday {axis} wheel moved away from the configured value."
            )
        unchanged = 0
        log(f"Birthday {axis} movement verified.")
        current_nodes = fresh

    raise PhoneError(f"Birthday {axis} selection failed safely.")


def select_configured_birthday(
    stack: TapStack,
    phone: Phone,
    nodes,
    target: date,
):
    """Select the exact private date, in the required year/month/day order."""
    current_nodes = stable_birthday_dump(
        stack,
        phone,
        "910_birthday_initial",
        initial_nodes=nodes,
        shot=False,
    )
    for axis, value in (
        ("year", target.year),
        ("month", target.month),
        ("day", target.day),
    ):
        locked = {}
        if axis in ("month", "day"):
            locked["year"] = target.year
        if axis == "day":
            locked["month"] = target.month
        current_nodes = _set_birthday_axis(
            stack,
            phone,
            current_nodes,
            axis,
            value,
            target,
            locked_axes=locked,
        )
    final = selected_birthday(current_nodes)
    if final != (target.day, target.month, target.year):
        raise PhoneError(
            "Birthday picker does not exactly match the private configured date. "
            "Did not tap Continue."
        )
    if not adult_birthday(current_nodes):
        raise PhoneError("Configured birthday is not proven adult. Did not tap Continue.")
    log("Birthday picker exactly matches the private configured date.")
    return current_nodes


def birthday_screen(nodes) -> bool:
    return (
        find_label_exact(nodes, ("About you",)) is not None
        and find_label_exact(nodes, BIRTHDAY_TITLE_LABELS) is not None
        and selected_birthday(nodes) is not None
    )


def birthday_picker(nodes) -> bool:
    """Recognize the Birthday page without inferring a selected wheel row."""
    return (
        find_label_exact(nodes, ("About you",)) is not None
        and find_label_exact(nodes, BIRTHDAY_TITLE_LABELS) is not None
    )


def stable_birthday_dump(
    stack: TapStack,
    phone: Phone,
    base_name: str,
    *,
    expected_day_month: tuple[int, int] | None = None,
    initial_nodes=None,
    shot: bool = True,
):
    """Return a complete centered Birthday selection after bounded OCR-only retries."""
    for attempt in range(BIRTHDAY_OCR_RETRY_DUMPS):
        if attempt == 0 and initial_nodes is not None:
            nodes = initial_nodes
        else:
            if attempt > 0 or initial_nodes is not None:
                time.sleep(0.7)
            suffix = "" if attempt == 0 else f"_retry_{attempt}"
            _blob, nodes = dump_named(
                stack,
                phone,
                f"{base_name}{suffix}",
                log,
                shot=shot,
                preview=False,
            )
        if not birthday_picker(nodes):
            raise PhoneError(
                "Birthday screen changed unexpectedly while verifying the picker. "
                "Did not move a wheel or tap Continue."
            )
        selected = selected_birthday(nodes)
        if selected is None:
            log(
                f"Birthday centered OCR incomplete; retry "
                f"{attempt + 1}/{BIRTHDAY_OCR_RETRY_DUMPS}"
            )
            continue
        day, month, _year = selected
        if expected_day_month is not None and (day, month) != expected_day_month:
            raise PhoneError(
                "A birthday movement changed the day or month. Did not tap Continue."
            )
        return nodes
    raise PhoneError(
        "Birthday centered picker OCR remained incomplete after three dumps. "
        "Did not move another wheel or tap Continue."
    )


def adult_birthday(nodes) -> bool:
    selected = selected_birthday(nodes)
    if selected is None:
        return False
    day, month, year = selected
    try:
        born = date(year, month, day)
    except ValueError:
        return False
    today = date.today()
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return 18 <= age <= 100


def gender_screen(nodes) -> bool:
    return (
        find_label_exact(nodes, ("How do you identify?", "About you")) is not None
        and find_label_exact(nodes, ("Woman",)) is not None
    )


def birthday_confirmation_screen(nodes) -> bool:
    """Recognize only Reddit's proven birthday confirmation sheet."""
    return (
        find_label_exact(nodes, ("About you", "About vou")) is not None
        and find_label_exact(nodes, ("Birthday", "Birthdav")) is not None
        and all(
            find_label_exact(nodes, (label,)) is not None
            for label in (
                "Confirm your birthday?",
                "Yes, Confirm",
                "Go Back",
            )
        )
    )


def confirmation_birthday(nodes) -> date | None:
    """Extract one unambiguous full date from Reddit's confirmation sheet."""
    if not birthday_confirmation_screen(nodes):
        return None
    month_names = "|".join(MONTHS)
    pattern = re.compile(
        rf"\b([1-9]|[12]\d|3[01])\s+({month_names})\s+((?:19|20)\d{{2}})\b"
    )
    found: set[date] = set()
    for node in nodes:
        if node.ntype == "ocr-line" or not node.visible or not node.on_screen():
            continue
        text = collapsed_text(node.labels())
        for match in pattern.finditer(text):
            try:
                found.add(
                    date(
                        int(match.group(3)),
                        MONTHS[match.group(2)],
                        int(match.group(1)),
                    )
                )
            except ValueError:
                return None
    if len(found) != 1:
        return None
    return next(iter(found))


def birthday_confirmation_matches(nodes, expected: date) -> bool:
    """Require the confirmation sheet to repeat the exact configured date."""
    return confirmation_birthday(nodes) == expected


def interests_screen(nodes) -> bool:
    return find_label_exact(nodes, ("Choose your interests", "Interests")) is not None


def gender_visual_state(nodes, screenshot_stem: str) -> str:
    """Return selected/unselected only when the saved pixels prove the known light UI."""
    woman = find_label_exact(nodes, ("Woman",))
    cont = find_label_exact(nodes, ("Continue",))
    if woman is None or cont is None:
        return "unknown"
    png_path = os.path.join(DUMP_DIR, screenshot_stem + ".png")
    dump_paths = [
        os.path.join(DUMP_DIR, screenshot_stem + ext)
        for ext in (".json", ".xml", ".txt")
        if os.path.isfile(os.path.join(DUMP_DIR, screenshot_stem + ext))
    ]
    try:
        if not dump_paths or not os.path.isfile(png_path):
            return "unknown"
        newest_dump_ns = max(os.stat(path).st_mtime_ns for path in dump_paths)
        if os.stat(png_path).st_mtime_ns < newest_dump_ns:
            return "unknown"
        from PIL import Image

        with Image.open(png_path) as source:
            image = source.convert("RGB")

            def control_pixel(node):
                cx, cy = node.center()
                # Probe clear control fill just beyond the OCR text, still inside the pill.
                px = cx + max(20, node.w // 2)
                py = cy + min(10, max(4, node.h // 3))
                sx = min(image.width - 1, max(0, round(px * image.width / 750)))
                sy = min(image.height - 1, max(0, round(py * image.height / 1334)))
                return image.getpixel((sx, sy))

            woman_rgb = control_pixel(woman)
            continue_rgb = control_pixel(cont)
    except (OSError, ValueError):
        return "unknown"

    woman_selected = (
        120 <= woman_rgb[0] <= 235
        and woman_rgb[1] - woman_rgb[0] >= 5
        and woman_rgb[2] - woman_rgb[0] >= 10
    )
    continue_enabled = (
        continue_rgb[0] >= 160
        and continue_rgb[1] <= 120
        and continue_rgb[2] <= 100
        and continue_rgb[0] - continue_rgb[1] >= 70
    )
    woman_unselected = min(woman_rgb) >= 238
    continue_disabled = (
        170 <= continue_rgb[0] <= 250
        and max(continue_rgb) - min(continue_rgb) <= 18
    )
    if woman_selected and continue_enabled:
        return "selected"
    if woman_unselected and continue_disabled:
        return "unselected"
    return "unknown"


def _finish_birthday_confirmation(
    stack: TapStack,
    phone: Phone,
    nodes,
    expected_birth_date: date,
):
    """Reach gender, allowing one exact Yes, Confirm tap and no other modal action."""
    last = nodes
    for attempt in range(BIRTHDAY_TRANSITION_MAX_DUMPS + 1):
        if gender_screen(last) or interests_screen(last):
            return last
        if birthday_confirmation_screen(last):
            if not birthday_confirmation_matches(last, expected_birth_date):
                raise PhoneError(
                    "Birthday confirmation does not exactly match the private configured date. "
                    "Did not tap Yes, Confirm or Go Back."
                )
            confirm = find_label_exact(last, ("Yes, Confirm",))
            log(
                f"Birthday confirmation -> Yes, Confirm "
                f"[{confirm.x},{confirm.y} {confirm.w}x{confirm.h}]"
            )
            stack.tap_xy(*confirm.center())
            for confirm_attempt in range(BIRTHDAY_TRANSITION_MAX_DUMPS):
                time.sleep(0.9)
                _blob, last = dump_named(
                    stack,
                    phone,
                    f"910_after_confirm_{confirm_attempt}",
                    log,
                    shot=True,
                    preview=False,
                )
                if gender_screen(last) or interests_screen(last):
                    return last
                if birthday_confirmation_screen(last):
                    if not birthday_confirmation_matches(last, expected_birth_date):
                        raise PhoneError(
                            "Birthday confirmation changed away from the private configured date. "
                            "Did not tap another action."
                        )
                    log(
                        f"Birthday confirmation transition pending; no second tap "
                        f"({confirm_attempt + 1}/{BIRTHDAY_TRANSITION_MAX_DUMPS})"
                    )
            if birthday_confirmation_screen(last):
                raise PhoneError(
                    "Birthday confirmation remained after one exact Yes, Confirm tap. "
                    "Did not tap it again or choose Go Back."
                )
            raise PhoneError(
                "Yes, Confirm did not reach the gender screen after 8 fresh dumps. "
                "Did not guess another action."
            )

        if attempt == BIRTHDAY_TRANSITION_MAX_DUMPS:
            break

        time.sleep(0.9)
        _blob, last = dump_named(
            stack,
            phone,
            f"910_after_birthday_{attempt}",
            log,
            shot=True,
            preview=False,
        )

    if birthday_picker(last):
        raise PhoneError(
            "Birthday screen remained after one exact Continue tap. "
            "Did not tap Continue again."
        )
    raise PhoneError(
        "Birthday confirmation did not reach the gender screen after 8 fresh dumps. "
        "Did not guess another action."
    )


def _finish_gender(stack: TapStack, phone: Phone, nodes) -> None:
    """Select Woman, tap the resulting exact Continue once, and prove Interests."""
    if interests_screen(nodes):
        log("Already on Interests; About you is complete.")
        return
    if not gender_screen(nodes):
        raise PhoneError("Not on the proven gender step. See 910_about.png")

    _blob, last = dump_named(stack, phone, "910_gender_ready", log, shot=True)
    if interests_screen(last):
        return
    if not gender_screen(last):
        raise PhoneError(
            "Gender screen changed before selection. Did not tap Woman, Continue, or Skip."
        )
    state = gender_visual_state(last, "910_gender_ready")
    if state == "unselected":
        woman = find_label_exact(last, ("Woman",))
        log(f"Woman [{woman.x},{woman.y} {woman.w}x{woman.h}]")
        stack.tap_xy(*woman.center())
        time.sleep(0.9)
        _blob, last = dump_named(stack, phone, "911_after_woman", log, shot=True)
        if interests_screen(last):
            return
        if not gender_screen(last):
            raise PhoneError(
                "Woman tap did not leave a proven gender screen or reach Interests. "
                "Did not tap Continue or Skip."
            )
        state = gender_visual_state(last, "911_after_woman")
    elif state == "selected":
        log("Woman is already selected; did not tap it again.")
    else:
        raise PhoneError(
            "Gender selection pixels are not proven as selected or unselected. "
            "Did not tap Woman, Continue, or Skip."
        )

    if state != "selected":
        raise PhoneError(
            "Woman tap did not produce a proven selected state with enabled Continue. "
            "Did not tap Continue or Skip."
        )
    cont = find_label_exact(last, ("Continue",))
    if cont is None:
        raise PhoneError(
            "Woman was tapped but the gender screen has no exact Continue. "
            "Did not tap Skip or guess a coordinate."
        )
    log(f"Gender selection -> Continue [{cont.x},{cont.y} {cont.w}x{cont.h}]")
    stack.tap_xy(*cont.center())

    for attempt in range(BIRTHDAY_TRANSITION_MAX_DUMPS):
        time.sleep(0.9)
        _blob, last = dump_named(
            stack,
            phone,
            f"912_after_gender_{attempt}",
            log,
            shot=True,
        )
        if interests_screen(last):
            return
        if gender_screen(last):
            log(
                f"Gender submit transition pending; no second Continue tap "
                f"({attempt + 1}/{BIRTHDAY_TRANSITION_MAX_DUMPS})"
            )

    if gender_screen(last):
        raise PhoneError(
            "Gender screen remained after one exact Continue tap. "
            "Did not tap Continue or Skip again."
        )
    raise PhoneError(
        "Gender Continue did not reach Interests after 8 fresh dumps. "
        "Did not guess another action."
    )


def tap_woman(
    stack: TapStack,
    phone: Phone,
    *,
    birth_date: date | None = None,
) -> None:
    _blob, nodes = dump_named(
        stack,
        phone,
        "910_about",
        log,
        shot=True,
        preview=False,
    )
    if interests_screen(nodes):
        log("Already on Interests; About you is complete.")
        return
    if birthday_confirmation_screen(nodes):
        expected_birth_date = birth_date or configured_reddit_birth_date()
        nodes = _finish_birthday_confirmation(
            stack,
            phone,
            nodes,
            expected_birth_date,
        )
    elif birthday_picker(nodes):
        expected_birth_date = birth_date or configured_reddit_birth_date()
        nodes = select_configured_birthday(
            stack,
            phone,
            nodes,
            expected_birth_date,
        )
        time.sleep(0.8)
        nodes = stable_birthday_dump(
            stack,
            phone,
            "910_birthday_ready",
        )
        if selected_birthday(nodes) != (
            expected_birth_date.day,
            expected_birth_date.month,
            expected_birth_date.year,
        ):
            raise PhoneError(
                "Birthday changed away from the private configured date before submission. "
                "Did not tap Continue."
            )
        if not birthday_screen(nodes) or not adult_birthday(nodes):
            raise PhoneError("Configured birthday was not proven adult. Did not tap Continue.")
        cont = find_label_exact(nodes, ("Continue",))
        if cont is None:
            raise PhoneError(
                "Birthday picker has a selected adult date but no exact Continue button. "
                "Did not guess a tap."
            )
        log(f"Private birthday confirmed -> Continue [{cont.x},{cont.y} {cont.w}x{cont.h}]")
        stack.tap_xy(*cont.center())
        nodes = _finish_birthday_confirmation(
            stack,
            phone,
            nodes,
            expected_birth_date,
        )

    _finish_gender(stack, phone, nodes)


def main() -> int:
    phone = Phone(log=log)
    try:
        phone.connect("", also_wda=False)
        stack = TapStack(phone, log)
        log("BACKEND " + stack.attach())
        tap_woman(stack, phone)
        return 0
    except PhoneError as e:
        print("ERROR", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
