# UTF-8
"""Offline tests for the Birthday picker recovery when OCR hides the center.

The centered wheel row is bold, so OCR drops it when the selected day is a
single digit. Short months can still render faded rows through 31, so neighbour
rows cannot prove the selected date; the summary line plus the month and year
wheel rows are the bounded evidence.

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


def readable_picker_with_day_window(
    centered_day: int,
    visible_days: tuple[int, ...],
    *,
    month_wheel: str = "february",
) -> list[Node]:
    """Build a complete synthetic picker with live-like 50-point row spacing."""
    if centered_day not in visible_days:
        raise ValueError("centered day must be visible")
    nodes = picker_nodes(
        summary=None,
        month_wheel=month_wheel,
        centered_day=None,
        day_rows=(),
    )
    centered_index = visible_days.index(centered_day)
    nodes.extend(
        node(
            str(value),
            90,
            about.BIRTHDAY_WHEEL_CENTER_Y + ((index - centered_index) * 50),
            w=60,
            h=25,
        )
        for index, value in enumerate(visible_days)
    )
    return nodes


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

    def test_summary_backed_geometry_selects_only_adjacent_target_direction(self):
        nodes = crash_fixture_nodes()

        adjacent = about._summary_backed_adjacent_day_node(
            nodes,
            3,
            date(2005, 2, 3),
            2,
        )

        self.assertIsNotNone(adjacent)
        self.assertEqual(adjacent.text, "2")

    def test_summary_backed_geometry_rejects_mismatch_and_unbracketed_rows(self):
        mismatch = picker_nodes(
            summary="1 february 2005",
            month_wheel="march",
            day_rows=("31", "2"),
        )
        unbracketed = picker_nodes(
            summary="1 february 2005",
            day_rows=("31",),
        )

        for nodes in (mismatch, unbracketed):
            with self.subTest(nodes=nodes):
                self.assertIsNone(
                    about._summary_backed_adjacent_day_node(
                        nodes,
                        3,
                        date(2005, 2, 3),
                        2,
                    )
                )

    def test_missing_centered_day_advances_locally_before_submit_fallback(self):
        initial = crash_fixture_nodes()
        adjacent = picker_nodes(
            summary="2 february 2005",
            day_rows=("1", "3"),
        )
        exact = picker_nodes(
            summary="3 february 2005",
            centered_day="3",
            day_rows=("2", "4"),
        )
        stack = mock.Mock()

        with mock.patch.object(
            about,
            "stable_birthday_dump",
            side_effect=(adjacent, exact),
        ):
            result = about._set_birthday_axis(
                stack,
                mock.Mock(),
                initial,
                "day",
                3,
                date(2005, 2, 3),
                locked_axes={"year": 2005, "month": 2},
            )

        self.assertEqual(about.selected_birthday(result), (3, 2, 2005))
        first_adjacent = next(
            node_ for value, node_ in about._axis_nodes(initial, "day") if value == 2
        )
        exact_target = next(
            node_ for value, node_ in about._axis_nodes(adjacent, "day") if value == 3
        )
        self.assertEqual(
            stack.tap_xy.call_args_list,
            [mock.call(*first_adjacent.center()), mock.call(*exact_target.center())],
        )
        self.assertFalse(stack.interp_flick.called)

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
    def test_day_distance_never_wraps_across_the_month_boundary(self):
        target = date(2005, 2, 3)

        self.assertEqual(about._axis_distance("day", 28, 3, target), -25)
        self.assertEqual(about._axis_distance("day", 3, 28, date(2005, 2, 28)), 25)
        # A month change can temporarily leave an invalid centered row.  It is
        # recoverable toward a valid target, but can never be a final date.
        self.assertEqual(about._axis_distance("day", 31, 28, date(2005, 2, 28)), -3)
        self.assertGreaterEqual(about.BIRTHDAY_AXIS_MAX_MOVES["day"], 60)

    def test_month_end_to_early_day_uses_a_long_decreasing_drag(self):
        initial = readable_picker_with_day_window(
            28,
            (25, 26, 27, 28, 29, 30, 31),
        )
        exact = readable_picker_with_day_window(3, (1, 2, 3, 4, 5, 6))
        stack = mock.Mock()

        with mock.patch.object(about, "stable_birthday_dump", return_value=exact):
            result = about._set_birthday_axis(
                stack,
                mock.Mock(),
                initial,
                "day",
                3,
                date(2005, 2, 3),
                locked_axes={"year": 2005, "month": 2},
            )

        self.assertEqual(about.selected_birthday(result), (3, 2, 2005))
        self.assertEqual(stack.interp_flick.call_count, 1)
        self.assertFalse(stack.tap_xy.called)
        x1, y1, x2, y2 = stack.interp_flick.call_args.args[:4]
        self.assertEqual(x1, x2)
        self.assertGreater(y2, y1)
        self.assertGreater(y2 - y1, 100)

    def test_readable_centered_day_still_uses_the_wheel(self):
        nodes = picker_nodes(
            summary=None,
            centered_day="25",
            day_rows=("24", "26"),
        )
        self.assertEqual(about.selected_birthday(nodes), (25, 2, 2005))

    def test_verified_noop_uses_closer_visible_row_before_another_flick(self):
        """The live picker ignored a short flick; do not repeat it unchanged."""
        initial = readable_picker_with_day_window(14, (11, 12, 13, 14, 15, 16))
        closer = readable_picker_with_day_window(16, (13, 14, 15, 16, 17, 18))
        exact = readable_picker_with_day_window(17, (14, 15, 16, 17, 18, 19))
        stack = mock.Mock()

        with mock.patch.object(
            about,
            "stable_birthday_dump",
            side_effect=(initial, closer, exact),
        ):
            result = about._set_birthday_axis(
                stack,
                mock.Mock(),
                initial,
                "day",
                17,
                date(2005, 2, 17),
                locked_axes={"year": 2005, "month": 2},
            )

        self.assertEqual(about.selected_birthday(result), (17, 2, 2005))
        self.assertEqual(stack.interp_flick.call_count, 1)
        self.assertEqual(stack.tap_xy.call_count, 2)
        closer_row = next(
            node_ for value, node_ in about._axis_nodes(initial, "day") if value == 16
        )
        self.assertEqual(stack.tap_xy.call_args_list[0], mock.call(*closer_row.center()))

    def test_wrap_with_exact_target_visible_is_recovered_by_exact_tap(self):
        """Unexpected momentum is safe only when it exposes the exact row."""
        initial = readable_picker_with_day_window(
            30,
            (27, 28, 29, 30, 31, 1),
            month_wheel="june",
        )
        closer = readable_picker_with_day_window(
            1,
            (29, 30, 31, 1, 2, 3),
            month_wheel="june",
        )
        exact = readable_picker_with_day_window(
            2,
            (30, 31, 1, 2, 3, 4),
            month_wheel="june",
        )
        stack = mock.Mock()

        with mock.patch.object(
            about,
            "stable_birthday_dump",
            side_effect=(closer, exact),
        ):
            result = about._set_birthday_axis(
                stack,
                mock.Mock(),
                initial,
                "day",
                2,
                date(2005, 6, 2),
                locked_axes={"year": 2005, "month": 6},
            )

        self.assertEqual(about.selected_birthday(result), (2, 6, 2005))
        self.assertEqual(stack.interp_flick.call_count, 1)
        self.assertEqual(stack.tap_xy.call_count, 1)
        exact_target = next(
            node_ for value, node_ in about._axis_nodes(closer, "day") if value == 2
        )
        self.assertEqual(stack.tap_xy.call_args_list[0], mock.call(*exact_target.center()))

    def test_verified_noop_skips_faded_invalid_rows_and_taps_valid_progress(self):
        initial = readable_picker_with_day_window(
            28,
            (25, 26, 27, 28, 29, 30, 31),
        )
        closer = readable_picker_with_day_window(
            25,
            (22, 23, 24, 25, 26, 27, 28),
        )
        exact = readable_picker_with_day_window(3, (1, 2, 3, 4, 5, 6))
        stack = mock.Mock()

        with mock.patch.object(
            about,
            "stable_birthday_dump",
            side_effect=(initial, closer, exact),
        ):
            result = about._set_birthday_axis(
                stack,
                mock.Mock(),
                initial,
                "day",
                3,
                date(2005, 2, 3),
                locked_axes={"year": 2005, "month": 2},
            )

        self.assertEqual(about.selected_birthday(result), (3, 2, 2005))
        self.assertEqual(stack.interp_flick.call_count, 2)
        self.assertEqual(stack.tap_xy.call_count, 1)
        day_25 = next(
            node_ for value, node_ in about._axis_nodes(initial, "day") if value == 25
        )
        self.assertEqual(stack.tap_xy.call_args, mock.call(*day_25.center()))


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


class BirthdaySubmitFallbackTests(unittest.TestCase):
    def exact_nodes(self, *, label: str = "Continue") -> list[Node]:
        nodes = picker_nodes(
            summary="3 february 2005",
            centered_day="3",
            day_rows=("2", "4"),
        )
        submit = next(node_ for node_ in nodes if node_.text == "Continue")
        submit.text = label
        return nodes

    def test_exact_date_accepts_one_bottom_continue(self):
        nodes = self.exact_nodes()

        submit = about._proven_birthday_submit_control(
            nodes,
            date(2005, 2, 3),
        )

        self.assertIsNotNone(submit)
        self.assertEqual(submit.text, "Continue")

    def test_unproven_create_alias_is_not_tapped(self):
        self.assertIsNone(
            about._proven_birthday_submit_control(
                self.exact_nodes(label="Create"),
                date(2005, 2, 3),
            )
        )

    def test_wrong_date_never_authorizes_bottom_submit(self):
        self.assertIsNone(
            about._proven_birthday_submit_control(
                self.exact_nodes(),
                date(2005, 2, 4),
            )
        )

    def test_duplicate_bottom_submit_is_ambiguous(self):
        nodes = self.exact_nodes()
        nodes.append(node("Continue", 280, 1130, w=190, h=50))

        self.assertIsNone(
            about._proven_birthday_submit_control(
                nodes,
                date(2005, 2, 3),
            )
        )

    def test_disabled_bottom_continue_is_not_tapped(self):
        nodes = self.exact_nodes()
        submit = next(node_ for node_ in nodes if node_.text == "Continue")
        submit.enabled = False

        self.assertIsNone(
            about._proven_birthday_submit_control(
                nodes,
                date(2005, 2, 3),
            )
        )

    def test_exhausted_local_handler_taps_once_only_after_exact_fresh_recheck(self):
        nodes = self.exact_nodes()
        submit = next(node_ for node_ in nodes if node_.text == "Continue")
        interests = [node("Interests", 40, 190, w=160)]
        stack = mock.Mock()

        with (
            mock.patch.object(about, "dump_named", return_value=("", nodes)),
            mock.patch.object(
                about,
                "select_configured_birthday",
                side_effect=(about.PhoneError("stuck"), about.PhoneError("stuck")),
            ),
            mock.patch.object(about, "stable_birthday_dump", return_value=nodes),
            mock.patch.object(
                about,
                "_finish_birthday_confirmation",
                return_value=interests,
            ) as finish,
            mock.patch.object(about, "_finish_gender") as finish_gender,
        ):
            about.tap_woman(
                stack,
                mock.Mock(),
                birth_date=date(2005, 2, 3),
            )

        self.assertEqual(stack.tap_xy.call_args_list, [mock.call(*submit.center())])
        finish.assert_called_once()
        finish_gender.assert_called_once_with(stack, mock.ANY, interests)

    def test_normal_local_recovery_remains_preferred(self):
        nodes = self.exact_nodes()
        submit = next(node_ for node_ in nodes if node_.text == "Continue")
        interests = [node("Interests", 40, 190, w=160)]
        stack = mock.Mock()

        with (
            mock.patch.object(about, "dump_named", return_value=("", nodes)),
            mock.patch.object(
                about,
                "select_configured_birthday",
                return_value=nodes,
            ) as local_select,
            mock.patch.object(about, "stable_birthday_dump", return_value=nodes),
            mock.patch.object(
                about,
                "_finish_birthday_confirmation",
                return_value=interests,
            ),
            mock.patch.object(about, "_finish_gender"),
        ):
            about.tap_woman(
                stack,
                mock.Mock(),
                birth_date=date(2005, 2, 3),
            )

        local_select.assert_called_once()
        self.assertEqual(stack.tap_xy.call_args_list, [mock.call(*submit.center())])

    def test_exhausted_local_handler_taps_one_proven_adult_continue(self):
        nodes = self.exact_nodes()
        submit = next(node_ for node_ in nodes if node_.text == "Continue")
        interests = [node("Interests", 40, 190, w=160)]
        stack = mock.Mock()

        with (
            mock.patch.object(about, "dump_named", return_value=("", nodes)),
            mock.patch.object(
                about,
                "select_configured_birthday",
                side_effect=(about.PhoneError("stuck"), about.PhoneError("stuck")),
            ),
            mock.patch.object(about, "stable_birthday_dump", return_value=nodes),
            mock.patch.object(
                about,
                "_finish_birthday_confirmation",
                return_value=interests,
            ) as finish,
            mock.patch.object(about, "_finish_gender") as finish_gender,
        ):
            about.tap_woman(
                stack,
                mock.Mock(),
                birth_date=date(2005, 2, 4),
            )

        self.assertEqual(stack.tap_xy.call_args_list, [mock.call(*submit.center())])
        self.assertEqual(finish.call_args.args[3], date(2005, 2, 3))
        finish_gender.assert_called_once_with(stack, mock.ANY, interests)

    def test_exhausted_handler_refuses_unproven_or_unsafe_continue(self):
        unknown = [
            node("About you", 40, 110, w=180),
            node("Birthday", 40, 190, w=160),
            node("Continue", 280, 1180, w=190, h=50),
        ]
        non_adult = picker_nodes(
            summary="3 february 2015",
            year_wheel="2015",
            centered_day="3",
            day_rows=("2", "4"),
        )
        duplicate = self.exact_nodes()
        duplicate.append(node("Continue", 280, 1120, w=190, h=50))
        disabled = self.exact_nodes()
        next(node_ for node_ in disabled if node_.text == "Continue").enabled = False

        for nodes in (unknown, non_adult, duplicate, disabled):
            with self.subTest(nodes=nodes):
                stack = mock.Mock()
                with (
                    mock.patch.object(about, "dump_named", return_value=("", nodes)),
                    mock.patch.object(
                        about,
                        "select_configured_birthday",
                        side_effect=(
                            about.PhoneError("stuck"),
                            about.PhoneError("stuck"),
                        ),
                    ),
                    mock.patch.object(
                        about,
                        "stable_birthday_dump",
                        return_value=nodes,
                    ),
                ):
                    with self.assertRaises(about.BirthdayRecoveryExhausted):
                        about.tap_woman(
                            stack,
                            mock.Mock(),
                            birth_date=date(2005, 2, 4),
                        )

                self.assertFalse(stack.tap_xy.called)


if __name__ == "__main__":
    unittest.main()
