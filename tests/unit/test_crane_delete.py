"""Offline safety tests for exact Crane container deletion."""

from __future__ import annotations

import plistlib
import unittest
from unittest import mock

from friend_poster.device import crane_delete
from friend_poster.device.ui_tree import Node


def node(label: str, *, x: int = 100, y: int = 300, w: int = 120, h: int = 40) -> Node:
    return Node(label=label, x=x, y=y, w=w, h=h)


def edit_nodes(*items: Node) -> list[Node]:
    return [
        node("Reddit", x=303, y=96, w=124, h=25),
        node("Done", x=647, y=98, w=75, h=23),
        node("CONTAINERS", x=56, y=403, w=175, h=20),
        *items,
    ]


def crane_prefs(rows, *, active: str = "DEFAULT") -> bytes:
    return plistlib.dumps(
        {
            "appSettings_com.reddit.Reddit": {
                "activeContainer": active,
                "Containers": [
                    {"name": name, "identifier": identifier}
                    for name, identifier in rows
                ],
            }
        }
    )


class Stack:
    def __init__(self):
        self.taps = []
        self.homes = 0

    def tap_xy(self, x, y):
        self.taps.append((x, y))

    def press_home(self):
        self.homes += 1

    def check_cancelled(self):
        pass


class CraneDeleteTests(unittest.TestCase):
    def test_recovery_finds_the_only_container_without_expected_slot_name(self):
        nodes = [
            node("Done", x=650, y=40),
            node("Default", y=330),
            node("- a0143", x=69, y=529, w=153, h=26),
        ]

        self.assertEqual(
            crane_delete._visible_user_container_name(nodes), "a0143"
        )

    def test_recovery_delete_uses_the_visible_container_not_failed_sid(self):
        stack = mock.Mock()
        visible = [node("Done", x=650, y=40), node("a0143", y=410)]
        with (
            mock.patch.object(
                crane_delete, "back_to_reddit_list", return_value=visible
            ),
            mock.patch.object(crane_delete, "tap_edit", return_value=visible),
            mock.patch.object(
                crane_delete,
                "tap_red_delete_r1",
                return_value=[node("Default", y=330)],
            ) as delete,
            mock.patch.object(crane_delete, "dump", return_value=(b"", [])),
            mock.patch.object(crane_delete.time, "sleep"),
        ):
            outcome = crane_delete.delete_only_container(stack, object())

        delete.assert_called_once_with(stack, mock.ANY, visible, "a0143")
        self.assertEqual(outcome, "deleted")

    def test_live_reddit_settings_accepts_back_chevron_applications_header(self):
        nodes = [
            node("< Applications", x=6, y=92, w=244, h=30),
            node("Reddit", x=303, y=96, w=124, h=25),
            node("Edit", x=647, y=98, w=75, h=23),
            node("CONTAINERS", x=56, y=403, w=175, h=20),
            node("a0103", x=53, y=534, w=107, h=27),
        ]

        self.assertTrue(crane_delete.is_reddit_list(nodes))

    def test_edit_waits_for_complete_crane_postcondition_before_row_scan(self):
        stack = Stack()
        pane = [
            node("< Applications", x=6, y=92, w=244, h=30),
            node("Reddit", x=303, y=96, w=124, h=25),
            node("Edit", x=647, y=98, w=75, h=23),
            node("CONTAINERS", x=56, y=403, w=175, h=20),
            node("a0103", x=53, y=534, w=107, h=27),
        ]
        transient = [node("Settings", x=89, y=56, w=96, h=15)]
        edit = [
            node("Reddit", x=303, y=96, w=124, h=25),
            node("Done", x=647, y=98, w=75, h=23),
            node("CONTAINERS", x=56, y=403, w=175, h=20),
            node("- a0103", x=69, y=529, w=153, h=26),
        ]

        with (
            mock.patch.object(
                crane_delete,
                "dump",
                side_effect=[(b"", transient), (b"", edit)],
            ) as dump,
            mock.patch.object(crane_delete.time, "sleep"),
        ):
            result = crane_delete.tap_edit(stack, object(), pane)

        self.assertIs(result, edit)
        self.assertEqual(stack.taps, [pane[2].center()])
        self.assertEqual(dump.call_count, 2)

    def test_edit_dismisses_unknown_containers_prompt_non_destructively(self):
        stack = Stack()
        pane = [
            node("< Applications", x=6, y=92, w=244, h=30),
            node("Reddit", x=303, y=96, w=124, h=25),
            node("Edit", x=647, y=98, w=75, h=23),
            node("CONTAINERS", x=56, y=403, w=175, h=20),
            node("a0103", x=53, y=534, w=107, h=27),
        ]
        remind = node("Remind Me Later", x=230, y=880, w=290, h=35)
        prompt = [
            *pane,
            node("Unknown Containers Found", x=150, y=455, w=450, h=30),
            node("Import Containers", x=230, y=730, w=290, h=35),
            node("Delete Containers", x=230, y=805, w=290, h=35),
            remind,
        ]
        edit = [
            node("Reddit", x=303, y=96, w=124, h=25),
            node("Done", x=647, y=98, w=75, h=23),
            node("CONTAINERS", x=56, y=403, w=175, h=20),
            node("- a0103", x=69, y=529, w=153, h=26),
        ]

        with (
            mock.patch.object(
                crane_delete,
                "dump",
                side_effect=[(b"", prompt), (b"", pane), (b"", edit)],
            ),
            mock.patch.object(crane_delete.time, "sleep"),
        ):
            result = crane_delete.tap_edit(stack, object(), pane)

        self.assertIs(result, edit)
        self.assertEqual(
            stack.taps,
            [pane[2].center(), remind.center(), pane[2].center()],
        )

    def test_row_scan_aborts_if_transition_falls_back_to_reddit_feed(self):
        stack = Stack()
        edit = [
            node("Reddit", x=303, y=96, w=124, h=25),
            node("Done", x=647, y=98, w=75, h=23),
            node("CONTAINERS", x=56, y=403, w=175, h=20),
            node("a0102", x=53, y=534, w=107, h=27),
        ]
        foreign_feed = [node("< Settings", x=81, y=55), node("Reddit", y=105)]

        with (
            mock.patch.object(crane_delete, "flick_list"),
            mock.patch.object(
                crane_delete, "dump", return_value=(b"", foreign_feed)
            ),
            mock.patch.object(crane_delete.time, "sleep"),
        ):
            with self.assertRaisesRegex(
                crane_delete.PhoneError, "Edit pane was lost"
            ):
                crane_delete.tap_red_delete_r1(
                    stack, object(), edit, "a0103", absent_ok=True
                )

        self.assertEqual(stack.taps, [])

    def test_live_edit_row_targets_minus_inside_combined_ocr_node(self):
        # Sanitized geometry captured from the live Crane Reddit edit pane:
        # XXTouch merges the red minus and slot name into one OCR node.
        row = node("- a0103", x=69, y=529, w=153, h=26)

        x, y, source = crane_delete._red_minus_target(row, "a0103")

        self.assertEqual((x, y), (89, 542))
        self.assertEqual(source, "leading-minus OCR")

    def test_live_combined_edit_row_is_found_as_exact_container(self):
        row = node("- a0103", x=69, y=529, w=153, h=26)

        self.assertIs(crane_delete.find_crane_name([row], "a0103"), row)
        self.assertIsNone(crane_delete.find_crane_name([row], "a0102"))

    def test_name_only_edit_row_targets_icon_immediately_left_of_name(self):
        # A second live frame omitted the minus glyph entirely.  This exact
        # derived point opened Delete on-device without tapping Delete itself.
        row = node("a0103", x=113, y=535, w=109, h=25)

        x, y, source = crane_delete._red_minus_target(row, "a0103")

        self.assertEqual((x, y), (84, 547))
        self.assertEqual(source, "name-only OCR")

    def test_red_minus_geometry_refuses_wrong_slot(self):
        row = node("- a0102", x=69, y=529, w=153, h=26)

        with self.assertRaisesRegex(crane_delete.PhoneError, "exact a0103"):
            crane_delete._red_minus_target(row, "a0103")

    def test_delete_scrolls_edit_list_and_targets_exact_named_row(self):
        stack = Stack()
        target = node("a0069", x=110, y=410)
        delete = node("Delete", x=600, y=410)
        after = [node("Done", x=650, y=40)]
        initial = edit_nodes(node("a0068", y=500))
        found = edit_nodes(target)

        with (
            mock.patch.object(crane_delete, "flick_list") as flick,
            mock.patch.object(
                crane_delete,
                "dump",
                side_effect=[
                    (b"", found),
                    (b"", [target, delete]),
                    (b"", after),
                ],
            ),
            mock.patch.object(crane_delete.time, "sleep"),
        ):
            result = crane_delete.tap_red_delete_r1(
                stack, object(), initial, "a0069"
            )

        flick.assert_called_once_with(stack)
        self.assertEqual(stack.taps[0][1], target.center()[1])
        self.assertEqual(stack.taps[1], delete.center())
        self.assertIs(result, after)

    def test_delete_refuses_to_tap_when_exact_container_never_appears(self):
        stack = Stack()
        unchanged = edit_nodes(node("a0068", y=500))

        with (
            mock.patch.object(crane_delete, "flick_list"),
            mock.patch.object(crane_delete, "flick_list_toward_top"),
            mock.patch.object(
                crane_delete,
                "dump",
                side_effect=[(b"", unchanged)] * 8,
            ),
            mock.patch.object(crane_delete.time, "sleep"),
        ):
            with self.assertRaisesRegex(crane_delete.PhoneError, "a0069 row"):
                crane_delete.tap_red_delete_r1(
                    stack, object(), unchanged, "a0069"
                )

        self.assertEqual(stack.taps, [])

    def test_cleanup_enters_container_picker_then_settings_from_haptic(self):
        from friend_poster.reddit import reddit_open

        stack = Stack()
        icon = node("Reddit", y=900)
        marker = node("Select Container", y=300)
        system = node("Edit Home Screen", y=900)
        settings = node("Settings", y=800)
        picker = [node("Containers", y=120), node("a0069", y=500), settings]
        pane = [
            node("Applications", x=20, y=80),
            node("Reddit", y=80),
            node("Edit", x=650, y=80),
            node("CONTAINERS", y=300),
            node("a0069", y=410),
        ]
        with (
            mock.patch.object(
                reddit_open, "_find_reddit_from_home", return_value=icon
            ),
            mock.patch.object(reddit_open, "long_press_home_icon") as long_press,
            mock.patch.object(
                reddit_open, "haptic_system_action", return_value=system
            ),
            mock.patch.object(
                reddit_open, "crane_submenu_marker", return_value=marker
            ),
            mock.patch.object(
                reddit_open, "crane_container_list", return_value=True
            ),
            mock.patch.object(
                crane_delete,
                "dump",
                side_effect=[
                    (b"", [node("Home")]),
                    (b"", [marker, system]),
                    (b"", picker),
                    (b"", pane),
                ],
            ),
            mock.patch.object(crane_delete.time, "sleep"),
        ):
            result = crane_delete.back_to_reddit_list(stack, object())
        long_press.assert_called_once_with(stack, icon, crane_delete.log)
        self.assertEqual(stack.taps, [marker.center(), settings.center()])
        self.assertIs(result, pane)

    def test_cleanup_accepts_live_singular_chevron_container_doorway(self):
        from friend_poster.reddit import reddit_open

        stack = Stack()
        icon = node("Reddit", y=900)
        marker = node("> Container", x=238, y=492, w=197, h=43)
        haptic = [
            node("Remove App", x=275, y=277, w=203, h=28),
            node("Edit Home Screen", x=275, y=422, w=284, h=25),
            marker,
            node("a0103", x=278, y=530, w=90, h=23),
        ]
        settings = node("Settings", y=800)
        picker = [node("Containers", y=120), node("a0103", y=500), settings]
        pane = [
            node("Applications", x=20, y=80),
            node("Reddit", y=80),
            node("Edit", x=650, y=80),
            node("CONTAINERS", y=300),
            node("a0103", y=410),
        ]
        with (
            mock.patch.object(
                reddit_open, "_find_reddit_from_home", return_value=icon
            ),
            mock.patch.object(reddit_open, "long_press_home_icon"),
            mock.patch.object(
                reddit_open, "crane_container_list", return_value=True
            ),
            mock.patch.object(
                crane_delete,
                "dump",
                side_effect=[
                    (b"", [node("Home")]),
                    (b"", haptic),
                    (b"", picker),
                    (b"", pane),
                ],
            ),
            mock.patch.object(crane_delete.time, "sleep"),
        ):
            result = crane_delete.back_to_reddit_list(stack, object())

        self.assertEqual(stack.taps, [marker.center(), settings.center()])
        self.assertIs(result, pane)

    def test_cleanup_retries_from_home_when_settings_transition_is_stale(self):
        from friend_poster.reddit import reddit_open

        stack = Stack()
        icon = node("Reddit", y=900)
        marker = node("Container", y=350)
        system = node("Edit Home Screen", y=900)
        settings = node("Settings", y=800)
        haptic = [marker, system]
        picker = [
            node("Container", y=120),
            node("Default", y=400),
            node("a0270", y=500),
            node("New Container", y=650),
            settings,
        ]
        stale_hme = [node("All Set", y=340), node("Done", y=1200)]
        pane = [
            node("< Applications", x=6, y=92, w=244, h=30),
            node("Reddit", y=80),
            node("Edit", x=650, y=80),
            node("CONTAINERS", y=300),
            node("a0270", y=410),
        ]

        with (
            mock.patch.object(
                reddit_open, "_find_reddit_from_home", return_value=icon
            ) as find_home,
            mock.patch.object(reddit_open, "long_press_home_icon") as long_press,
            mock.patch.object(
                reddit_open, "haptic_system_action", return_value=system
            ),
            mock.patch.object(
                reddit_open, "crane_submenu_marker", return_value=marker
            ),
            mock.patch.object(
                reddit_open, "crane_container_list", return_value=True
            ),
            mock.patch.object(
                crane_delete,
                "dump",
                side_effect=[
                    (b"", [node("Home")]),
                    (b"", haptic),
                    (b"", picker),
                    (b"", stale_hme),
                    (b"", haptic),
                    (b"", picker),
                    (b"", pane),
                ],
            ),
            mock.patch.object(crane_delete.time, "sleep") as sleep,
        ):
            result = crane_delete.back_to_reddit_list(stack, object())

        self.assertEqual(find_home.call_count, 2)
        self.assertEqual(long_press.call_count, 2)
        self.assertEqual(stack.homes, 1)
        self.assertEqual(
            stack.taps,
            [marker.center(), settings.center(), marker.center(), settings.center()],
        )
        self.assertEqual(sleep.call_args_list.count(mock.call(3.0)), 2)
        self.assertIs(result, pane)

    def test_live_expanded_picker_requires_all_safe_anchors(self):
        frame = [
            node("Remove App", x=275, y=296, w=196, h=25),
            node("~ Container", x=234, y=462, w=208, h=54),
            node("a0103", x=278, y=508, w=90, h=26),
            node("Default", x=272, y=580, w=124, h=24),
            node("New Container", x=272, y=725, w=237, h=25),
            node("Settings", x=280, y=808, w=132, h=30),
        ]

        self.assertTrue(crane_delete._is_expanded_haptic_container_picker(frame))
        self.assertFalse(
            crane_delete._is_expanded_haptic_container_picker(
                [item for item in frame if "New Container" not in item.labels()]
            )
        )

    def test_live_a0151_picker_survives_corrupted_remove_app_ocr(self):
        # Sanitized reproduction of the reported frame.  The iOS system row
        # was OCRed as nonsense, but the complete Crane submenu geometry is
        # still unambiguous and Settings must remain reachable.
        frame = [
            node("RamavA Ann", x=270, y=245, w=210, h=26),
            node("Container", x=236, y=405, w=205, h=42),
            node("Default", x=273, y=493, w=125, h=27),
            node("a0151", x=276, y=565, w=92, h=28),
            node("New Container", x=272, y=652, w=238, h=28),
            node("Settings", x=278, y=746, w=136, h=31),
        ]

        self.assertTrue(crane_delete._is_expanded_haptic_container_picker(frame))

    def test_delete_fails_if_target_row_remains_visible_after_tap(self):
        stack = Stack()
        target = node("a0069", x=110, y=410)
        delete = node("Delete", x=600, y=410)
        initial = edit_nodes(target)

        with (
            mock.patch.object(
                crane_delete,
                "dump",
                side_effect=[
                    (b"", [target, delete]),
                    (b"", [target]),
                ],
            ),
            mock.patch.object(crane_delete.time, "sleep"),
        ):
            with self.assertRaisesRegex(
                crane_delete.PhoneError, "deletion was not confirmed"
            ):
                crane_delete.tap_red_delete_r1(
                    stack, object(), initial, "a0069"
                )


class CranePrefsIdentityTests(unittest.TestCase):
    def phone(self, rows, *, active="DEFAULT"):
        phone = mock.Mock()
        phone.sftp_read_bytes.return_value = crane_prefs(rows, active=active)
        return phone

    def test_exact_recorded_uuid_binds_the_named_row(self):
        phone = self.phone(
            [
                ("Default", "DEFAULT"),
                ("a0226", "4F1A9C93-2829-4237-9156-840BFA38CC50"),
            ],
            active="4F1A9C93-2829-4237-9156-840BFA38CC50",
        )

        status, instance_id = crane_delete.resolve_crane_container_identity(
            phone,
            lambda _message: None,
            "a0226",
            "4F1A9C93",
        )

        self.assertEqual(status, "exact")
        self.assertEqual(instance_id, "4F1A9C93-2829-4237-9156-840BFA38CC50")

    def test_wrong_recorded_uuid_is_refused(self):
        phone = self.phone(
            [
                ("Default", "DEFAULT"),
                ("a0226", "4F1A9C93-2829-4237-9156-840BFA38CC50"),
            ]
        )

        with self.assertRaisesRegex(crane_delete.PhoneError, "not recorded"):
            crane_delete.resolve_crane_container_identity(
                phone,
                lambda _message: None,
                "a0226",
                "AAAAAAAA",
            )

    def test_duplicate_named_rows_are_refused(self):
        phone = self.phone(
            [
                ("Default", "DEFAULT"),
                ("a0226", "4F1A9C93-2829-4237-9156-840BFA38CC50"),
                ("a0226", "AAAAAAAA-1111-2222-3333-444444444444"),
            ]
        )

        with self.assertRaisesRegex(crane_delete.PhoneError, "ambiguous"):
            crane_delete.resolve_crane_container_identity(
                phone,
                lambda _message: None,
                "a0226",
            )

    def test_absent_is_only_allowed_when_explicit(self):
        phone = self.phone([("Default", "DEFAULT")])

        with self.assertRaisesRegex(crane_delete.PhoneError, "no non-Default"):
            crane_delete.resolve_crane_container_identity(
                phone,
                lambda _message: None,
                "a0226",
            )
        self.assertEqual(
            crane_delete.resolve_crane_container_identity(
                phone,
                lambda _message: None,
                "a0226",
                allow_absent=True,
            ),
            ("absent", ""),
        )

    def test_sole_resolution_refuses_multiple_containers(self):
        phone = self.phone(
            [
                ("Default", "DEFAULT"),
                ("a0226", "4F1A9C93-2829-4237-9156-840BFA38CC50"),
                ("a0227", "AAAAAAAA-1111-2222-3333-444444444444"),
            ]
        )

        with self.assertRaisesRegex(crane_delete.PhoneError, "exactly one"):
            crane_delete.resolve_sole_crane_container(
                None, phone, lambda _message: None
            )

    def test_settings_row_finder_ignores_active_row_above_header(self):
        nodes = [
            node("Done", x=650, y=40),
            node("a0226 >", x=500, y=239, w=130, h=24),
            node("CONTAINERS", x=56, y=403, w=175, h=20),
            node("a0226", x=113, y=535, w=109, h=25),
        ]

        found = crane_delete._find_exact_container_row(nodes, "a0226")

        self.assertIs(found, nodes[-1])

    def test_settings_row_finder_refuses_duplicate_editable_rows(self):
        nodes = [
            node("CONTAINERS", x=56, y=403, w=175, h=20),
            node("a0226", x=113, y=535, w=109, h=25),
            node("a0226", x=113, y=600, w=109, h=25),
        ]

        with self.assertRaisesRegex(crane_delete.PhoneError, "ambiguous"):
            crane_delete._find_exact_container_row(nodes, "a0226")

    def test_delete_uses_crane_settings_and_verifies_uuid_disappears(self):
        state = {"deleted": False}
        before = crane_prefs(
            [
                ("Default", "DEFAULT"),
                ("a0226", "4F1A9C93-2829-4237-9156-840BFA38CC50"),
            ],
            active="4F1A9C93-2829-4237-9156-840BFA38CC50",
        )
        after = crane_prefs([("Default", "DEFAULT")], active="DEFAULT")
        phone = mock.Mock()
        phone.sftp_read_bytes.side_effect = lambda _path: (
            after if state["deleted"] else before
        )
        stack = Stack()

        def delete_tap(*_args, **_kwargs):
            state["deleted"] = True
            return [node("Done", x=650, y=40)]

        with (
            mock.patch.object(
                crane_delete, "back_to_reddit_list", return_value=[]
            ) as back,
            mock.patch.object(crane_delete, "tap_edit", return_value=[]) as edit,
            mock.patch.object(
                crane_delete, "tap_red_delete_r1", side_effect=delete_tap
            ) as delete,
            mock.patch.object(crane_delete, "dump", return_value=(b"", [])),
            mock.patch.object(crane_delete.time, "sleep"),
        ):
            outcome = crane_delete.delete_named_crane_instance(
                stack,
                phone,
                lambda _message: None,
                "a0226",
                "4F1A9C93",
            )

        self.assertEqual(outcome, "deleted")
        back.assert_called_once()
        edit.assert_called_once()
        delete.assert_called_once()
        self.assertEqual(stack.homes, 1)

    def test_delete_is_idempotent_when_prefs_already_show_uuid_absent(self):
        recorded = "4F1A9C93-2829-4237-9156-840BFA38CC50"
        phone = self.phone([("Default", "DEFAULT")], active=recorded)
        stack = Stack()

        with (
            mock.patch.object(crane_delete, "back_to_reddit_list") as back,
            mock.patch.object(crane_delete, "tap_edit") as edit,
            mock.patch.object(crane_delete, "tap_red_delete_r1") as delete,
            mock.patch.object(crane_delete, "dump", return_value=(b"", [])),
            mock.patch.object(crane_delete.time, "sleep"),
        ):
            outcome = crane_delete.delete_named_crane_instance(
                stack,
                phone,
                lambda _message: None,
                "a0226",
                recorded,
            )

        self.assertEqual(outcome, "absent")
        back.assert_not_called()
        edit.assert_not_called()
        delete.assert_not_called()
        self.assertEqual(stack.homes, 1)

    def test_delete_accepts_stale_active_pointer_with_live_default_proof(self):
        recorded = "4F1A9C93-2829-4237-9156-840BFA38CC50"
        state = {"deleted": False}
        before = crane_prefs(
            [("Default", "DEFAULT"), ("a0226", recorded)],
            active=recorded,
        )
        after = crane_prefs([("Default", "DEFAULT")], active=recorded)
        phone = mock.Mock()
        phone.sftp_read_bytes.side_effect = lambda _path: (
            after if state["deleted"] else before
        )
        stack = Stack()

        def delete_tap(*_args, **_kwargs):
            state["deleted"] = True
            return [
                node("Done", x=650, y=40),
                node("Active Container", x=187, y=239, w=262, h=25),
                node("Default >", x=609, y=239, w=156, h=25),
                node("CONTAINERS", x=142, y=413, w=172, h=20),
            ]

        messages = []
        with (
            mock.patch.object(
                crane_delete, "back_to_reddit_list", return_value=[]
            ),
            mock.patch.object(crane_delete, "tap_edit", return_value=[]),
            mock.patch.object(
                crane_delete, "tap_red_delete_r1", side_effect=delete_tap
            ),
            mock.patch.object(crane_delete, "dump", return_value=(b"", [])),
            mock.patch.object(crane_delete.time, "sleep"),
        ):
            outcome = crane_delete.delete_named_crane_instance(
                stack,
                phone,
                messages.append,
                "a0226",
                recorded,
            )

        self.assertEqual(outcome, "deleted")
        self.assertTrue(any("stale activeContainer" in line for line in messages))
        self.assertEqual(stack.homes, 1)

    def test_delete_refuses_stale_active_pointer_without_live_proof(self):
        recorded = "4F1A9C93-2829-4237-9156-840BFA38CC50"
        state = {"deleted": False}
        before = crane_prefs(
            [("Default", "DEFAULT"), ("a0226", recorded)],
            active=recorded,
        )
        after = crane_prefs([("Default", "DEFAULT")], active=recorded)
        phone = mock.Mock()
        phone.sftp_read_bytes.side_effect = lambda _path: (
            after if state["deleted"] else before
        )
        stack = Stack()

        def delete_tap(*_args, **_kwargs):
            state["deleted"] = True
            return [node("Done", x=650, y=40)]

        with (
            mock.patch.object(
                crane_delete, "back_to_reddit_list", return_value=[]
            ),
            mock.patch.object(crane_delete, "tap_edit", return_value=[]),
            mock.patch.object(
                crane_delete, "tap_red_delete_r1", side_effect=delete_tap
            ),
            mock.patch.object(crane_delete, "dump", return_value=(b"", [])),
            mock.patch.object(crane_delete.time, "sleep"),
        ):
            with self.assertRaisesRegex(
                crane_delete.PhoneError, "does not prove"
            ):
                crane_delete.delete_named_crane_instance(
                    stack,
                    phone,
                    lambda _message: None,
                    "a0226",
                    recorded,
                )


if __name__ == "__main__":
    unittest.main()
