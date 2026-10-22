"""open_reddit_containers must report a blocking iCloud modal, never a
misleading navigation failure.

The blocking-modal detection is driven by the real sanitized dump
``a0048_icloud_pause.json`` (the iCloud "Sign in to iCloud" password
sheet).  No phone, no network.

Provenance: these tests originally used ``622_apps.json`` as the
modal-over-Ghost fixture.  That file is live pipeline output --
``open_reddit_containers`` persists stem ``622_apps`` on every run -- and a
later live run overwrote the a0077 modal frame with a clean Application
List.  The never-overwritten sheet evidence ``a0048_icloud_pause.json`` is
used instead; the detector and the navigation guarantees under test are
unchanged.
"""

from __future__ import annotations

import unittest
from unittest import mock

from friend_poster.core.paths import DUMP_DIR
from friend_poster.device import ghost_live
from friend_poster.device.ssh_device import PhoneError
from friend_poster.device.ui_tree import Node, parse_nodes


def load(name: str) -> list[Node]:
    return parse_nodes((DUMP_DIR / name).read_text(encoding="utf-8"))


# Real sanitized dump: the iCloud "Sign in to iCloud" password sheet (stable
# a0048 evidence).  ``622_apps.json`` is NOT used here: it is live pipeline
# output (open_reddit_containers persists stem "622_apps" on every run) and a
# later live run overwrote the a0077 modal frame with a clean Application
# List.  The detector only needs the exact sheet title, which this dump has.
MODAL_SHEET = load("a0048_icloud_pause.json")

GHOST_HOME = [
    Node(ntype="ocr", text="Home", x=90, y=95, w=188, h=36),
    Node(ntype="ocr", text="Application List", x=145, y=900, w=240, h=25),
    Node(ntype="ocr", text="Ghost Mode", x=45, y=680, w=190, h=25),
]

# A real Application List frame with no Reddit and no modal: the classic
# "Reddit not found" failure must keep its original wording.
CLEAN_APPS_NO_REDDIT = [
    Node(ntype="ocr", text="All Apps", x=30, y=140, w=110, h=24),
    Node(ntype="ocr", text="YouTube", x=120, y=300, w=130, h=25),
    Node(ntype="ocr", text="Safari", x=130, y=380, w=110, h=25),
]

APPLE_ID_VERIFICATION = [
    Node(ntype="ocr", text="Apple ID Verification", x=200, y=596, w=340, h=28),
    Node(ntype="ocr", text="Enter the password for", x=235, y=634, w=280, h=18),
    Node(ntype="ocr", text="Not Now", x=163, y=742, w=146, h=27),
    Node(ntype="ocr", text="Settings", x=437, y=742, w=140, h=28),
]


class Stack:
    """Records navigation taps; no real device."""

    def __init__(self):
        self.row_taps = []
        self.taps = []

    def tap_row(self, node):
        self.row_taps.append(node.labels())

    def tap_xy(self, x, y):
        self.taps.append((x, y))

    def type_text_block(self, value):
        raise AssertionError(f"no typing expected ({value!r})")


class ICloudModalPresenceTests(unittest.TestCase):
    def test_real_modal_sheet_fixture_contains_the_blocking_modal(self):
        self.assertTrue(ghost_live.icloud_modal_present(MODAL_SHEET))

    def test_clean_ghost_home_has_no_modal(self):
        self.assertFalse(ghost_live.icloud_modal_present(GHOST_HOME))

    def test_observed_apple_id_verification_layout_is_a_blocking_modal(self):
        self.assertTrue(ghost_live.icloud_modal_present(APPLE_ID_VERIFICATION))
        self.assertIsNotNone(
            ghost_live.apple_id_verification_modal(APPLE_ID_VERIFICATION)
        )

    def test_blocking_modal_error_carries_the_specific_failure_code(self):
        self.assertTrue(
            issubclass(ghost_live.ICloudModalBlockingError, PhoneError)
        )
        self.assertEqual(
            ghost_live.ICloudModalBlockingError.failure_code, "icloud_modal_blocking"
        )


class OpenRedditContainersModalTests(unittest.TestCase):
    def _run(self, dumps):
        """Run open_reddit_containers against routed dump responses."""
        stack = Stack()
        seen: list[str] = []

        def dump(_stack, _phone, stem, _lg, **_kwargs):
            seen.append(stem)
            return None, dumps[stem]

        with (
            mock.patch.object(ghost_live, "dump_named", side_effect=dump),
            mock.patch.object(ghost_live.time, "sleep", return_value=None),
        ):
            ghost_live.open_reddit_containers(
                stack, object(), lambda _message: None, None
            )
        return stack, seen

    def test_modal_on_the_first_frame_stops_before_any_ghost_navigation(self):
        with mock.patch.object(
            ghost_live,
            "route_ghost_screen",
            side_effect=AssertionError("must not route into Ghost"),
        ):
            with self.assertRaises(ghost_live.ICloudModalBlockingError) as caught:
                self._run({"621_now": MODAL_SHEET})
        self.assertIn("iCloud authentication", str(caught.exception))
        self.assertNotIn("Reddit not found", str(caught.exception))

    def test_modal_behind_the_app_list_reports_blocking_modal_not_reddit_missing(self):
        # Mirrors a0077: Ghost navigation succeeds, but the frame captured at
        # the app-list step shows the iCloud sheet instead of a reachable
        # Reddit entry.
        with self.assertRaises(ghost_live.ICloudModalBlockingError) as caught:
            self._run({"621_now": GHOST_HOME, "622_apps": MODAL_SHEET})
        message = str(caught.exception)
        self.assertIn("iCloud", message)
        self.assertNotIn("Reddit not found", message)

    def test_apple_id_verification_is_dismissed_once_then_freshly_checked(self):
        stack = Stack()
        with (
            mock.patch.object(
                ghost_live,
                "dump_named",
                return_value=(None, GHOST_HOME),
            ),
            mock.patch.object(ghost_live.time, "sleep", return_value=None),
        ):
            fresh = ghost_live.dismiss_apple_id_verification_modal(
                stack, object(), lambda _message: None, APPLE_ID_VERIFICATION
            )
        self.assertIs(fresh, GHOST_HOME)
        self.assertEqual(len(stack.taps), 1)
        self.assertEqual(stack.taps[0], (236, 755))

    def test_persistent_apple_id_verification_pauses_after_one_tap(self):
        stack = Stack()
        with (
            mock.patch.object(
                ghost_live,
                "dump_named",
                return_value=(None, APPLE_ID_VERIFICATION),
            ),
            mock.patch.object(ghost_live.time, "sleep", return_value=None),
        ):
            with self.assertRaises(ghost_live.ICloudModalBlockingError):
                ghost_live.dismiss_apple_id_verification_modal(
                    stack, object(), lambda _message: None, APPLE_ID_VERIFICATION
                )
        self.assertEqual(len(stack.taps), 1)

    def test_missing_reddit_without_modal_keeps_the_original_error(self):
        stack = Stack()
        with self.assertRaises(PhoneError) as caught:
            self._run({"621_now": GHOST_HOME, "622_apps": CLEAN_APPS_NO_REDDIT})
        self.assertNotIsInstance(caught.exception, ghost_live.ICloudModalBlockingError)
        self.assertIn("Reddit not found", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
