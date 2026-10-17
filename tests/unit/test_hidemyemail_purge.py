"""Pure safety checks for destructive Hide My Email cleanup classification."""

from __future__ import annotations

import unittest

from friend_poster.device.hidemyemail_purge import (
    _detail_label_values,
    _detail_labels,
    _active_list,
    keep_label,
    slot_index,
)
from friend_poster.device.ui_tree import Node


class HideMyEmailPurgeTests(unittest.TestCase):
    def test_preserves_only_requested_slot_range(self):
        for label in ("024", "a0024", "r24", "a0049-1", "a0064", "064"):
            with self.subTest(label=label):
                self.assertTrue(keep_label(label))

    def test_deletes_everything_outside_range_including_unrelated_labels(self):
        for label in (
            "001", "a0001", "a0023", "a0065", "a0069-3", "Tiktok1", "Personal"
        ):
            with self.subTest(label=label):
                self.assertFalse(keep_label(label))

    def test_slot_parser_is_exact(self):
        self.assertEqual(slot_index("a0049-2"), 49)
        self.assertEqual(slot_index(" 064 "), 64)
        self.assertIsNone(slot_index("account a0049"))
        self.assertIsNone(slot_index("a0049 personal"))

    def test_detail_label_row_recovers_from_bad_navigation_title_ocr(self):
        nodes = [
            Node(text="Tiktok?", x=330, y=98, w=128, h=26),
            Node(text="Label", x=56, y=593, w=97, h=28),
            Node(text="Tiktok1 >", x=531, y=593, w=156, h=28),
        ]
        self.assertEqual(_detail_labels(nodes), ["tiktok?", "tiktok1"])
        self.assertEqual(_detail_label_values(nodes), ["tiktok1"])

    def test_detail_label_row_still_exposes_protected_slot(self):
        nodes = [
            Node(text="scoerch", x=330, y=98, w=128, h=26),
            Node(text="a0060 >", x=531, y=593, w=156, h=28),
        ]
        self.assertIn("a0060", _detail_labels(nodes))
        self.assertEqual(_detail_label_values(nodes), ["a0060"])

    def test_active_list_is_recognized_at_scrolled_bottom(self):
        nodes = [
            Node(text="Hide My Email", x=250, y=98, w=240, h=25),
            Node(text="Inactive addresses", x=56, y=1199, w=294, h=23),
        ]
        self.assertTrue(_active_list(nodes))


if __name__ == "__main__":
    unittest.main()
