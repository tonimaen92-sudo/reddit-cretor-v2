# UTF-8
"""Offline tests for the Birthday picker recovery when OCR hides the center.

The centered wheel row is bold, so OCR drops it when the selected day is a
single digit. The day wheel cycles 1..31 in every month, so neighbour rows can
never be derived from the month length; the summary line plus the month and
year wheel rows are the bounded evidence.

Run: python -m unittest tests.unit.test_reddit_birthday_fallback
"""

from __future__ import annotations

import os
import unittest
from datetime import date
from unittest import mock

from friend_poster.core.paths import FIXTURES_DIR
from friend_poster.device.ui_tree import Node, parse_nodes
from friend_poster.reddit import reddit_about as about
from friend_poster.services import ui_recovery_agent as recovery


def node(text: str, x: int, center_y: int, *, w: int = 80, h: int = 20) -> Node:
    return Node(ntype="ocr", text=text, x=x, y=center_y - h // 2, w=w, h=h)


def picker_nodes(
    *,
    summary: str | None = "1 february 2005",
    month_wheel: str = "february",
    year_wheel: str = "2005",
    centered_day: str | None = None,
    day_rows: tuple[str, ...] = (),
    with_continue: bool = True,
) -> list[Node]:
    """Build the canonical About you / Birthday frame."""
    nodes = [
        node("About you", 40, 110, w=180),
        node("Birthday", 40, 190, w=160),
        node(month_wheel, 285, about.BIRTHDAY_WHEEL_CENTER_Y, w=130),
        node(year_wheel, 545, about.BIRTHDAY_WHEEL_CENTER_Y, w=100),
    ]
    if with_continue:
        nodes.append(node("Continue", 280, 1180, w=190, h=50))
    if summary is not None:
        nodes.append(node(summary, 185, 426, w=259, h=27))
    if centered_day is not None:
        nodes.append(
            node(centered_day, 90, about.BIRTHDAY_WHEEL_CENTER_Y, w=60, h=25)
        )
    rows = (625, 725)
    nodes.extend(
        node(value, 90, center_y, w=60, h=25)
        for value, center_y in zip(day_rows, rows)
    )
    return nodes


def crash_fixture_nodes() -> list[Node]:
    path = os.path.join(FIXTURES_DIR, "a0119_birthday_missing_centered_day.json")
    with open(path, encoding="utf-8") as handle:
        return parse_nodes(handle.read())


class MissingCenteredDayRecoveryTests(unittest.TestCase):
    def test_missing_centered_day_is_recovered_from_summary(self):
        nodes = picker_nodes(day_rows=("31", "2"))
        self.assertEqual(about.selected_birthday(nodes), (1, 2, 2005))

    def test_recovery_does_not_require_neighbour_rows(self):
        self.assertEqual(about.selected_birthday(picker_nodes()), (1, 2, 2005))

    def test_month_length_neighbours_are_not_required(self):
        """February neighbours may be 28/2 or 31/2; both are irrelevant."""
        self.assertEqual(
            about.selected_birthday(picker_nodes(day_rows=("28", "2"))),
            (1, 2, 2005),
        )
        self.assertEqual(
            about.selected_birthday(picker_nodes(day_rows=("31", "2"))),
            (1, 2, 2005),
        )

    def test_recovered_date_is_proven_adult(self):
        self.assertTrue(about.adult_birthday(picker_nodes(day_rows=("31", "2"))))

    def test_summary_missing_is_rejected(self):
        self.assertIsNone(about.selected_birthday(picker_nodes(summary=None)))

    def test_continue_missing_is_rejected(self):
        nodes = picker_nodes(with_continue=False)
        self.assertIsNone(about.selected_birthday(nodes))

    def test_month_wheel_disagreement_is_rejected(self):
        nodes = picker_nodes(month_wheel="march")
        self.assertIsNone(about.selected_birthday(nodes))

    def test_year_wheel_disagreement_is_rejected(self):
        nodes = picker_nodes(year_wheel="2004")
        self.assertIsNone(about.selected_birthday(nodes))

    def test_invalid_summary_date_is_rejected(self):
        nodes = picker_nodes(summary="30 february 2005")
        self.assertIsNone(about.selected_birthday(nodes))

    def test_unrelated_screen_without_picker_is_rejected(self):
        nodes = [node("About you", 40, 110, w=180)]
        self.assertIsNone(about.selected_birthday(nodes))


class FrozenCrashFrameTests(unittest.TestCase):
    """The real a0119 frame that paused the batch at 13:16."""

    def test_frozen_crash_frame_recovers_the_configured_date(self):
        nodes = crash_fixture_nodes()
        self.assertEqual(about.selected_birthday(nodes), (1, 2, 2005))
        self.assertTrue(about.adult_birthday(nodes))

    def test_frozen_crash_frame_still_hides_the_centered_day(self):
        nodes = crash_fixture_nodes()
        self.assertIsNone(about.selected_birthday_nodes(nodes))

    def test_frozen_crash_frame_is_the_birthday_screen(self):
        nodes = crash_fixture_nodes()
        self.assertTrue(about.birthday_picker(nodes))
        self.assertTrue(about.birthday_screen(nodes))


class ReadableCenterPathTests(unittest.TestCase):
    def test_readable_centered_day_still_uses_the_wheel(self):
        nodes = picker_nodes(
            summary=None,
            centered_day="25",
            day_rows=("24", "26"),
        )
        self.assertEqual(about.selected_birthday(nodes), (25, 2, 2005))


class TransientInvalidWheelTests(unittest.TestCase):
    def test_month_transition_allows_temporary_31_february(self):
        initial = picker_nodes(
            summary=None,
            month_wheel="january",
            centered_day="31",
            day_rows=("30", "1"),
        )
        initial.append(node("february", 285, 725, w=130))
        transitioned = picker_nodes(
            summary=None,
            month_wheel="february",
            centered_day="31",
            day_rows=("30", "1"),
        )
        stack = mock.Mock()
        with mock.patch.object(about, "stable_birthday_dump", return_value=transitioned):
            result = about._set_birthday_axis(
                stack,
                mock.Mock(),
                initial,
                "month",
                2,
                date(2005, 2, 28),
                locked_axes={"year": 2005},
            )
        self.assertEqual(about.selected_birthday(result), (31, 2, 2005))

    def test_exhausted_birthday_recovery_explicitly_opts_into_ai(self):
        error = about.BirthdayRecoveryExhausted("local handlers exhausted")
        self.assertTrue(error.ai_recoverable)
        self.assertTrue(recovery.is_ui_recoverable_error(error))

    def test_readable_centered_day_wins_over_a_wrong_summary(self):
        """The primary wheel path is untouched by the summary recovery."""
        nodes = picker_nodes(
            summary="1 february 2005",
            centered_day="25",
            day_rows=("24", "26"),
        )
        self.assertEqual(about.selected_birthday(nodes), (25, 2, 2005))


if __name__ == "__main__":
    unittest.main()
