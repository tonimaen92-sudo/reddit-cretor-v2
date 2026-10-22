import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from friend_poster.reddit import reddit_interests
from friend_poster.reddit import reddit_open
from friend_poster.device.ui_tree import Node


class InterestsCheckpointSaveTests(unittest.TestCase):
    def test_settings_style_ads_consent_is_a_local_reddit_postcondition(self):
        nodes = [
            Node(ntype="ocr", text="Settings", x=20, y=48, w=120, h=20),
            Node(
                ntype="ocr", text="Reddit allows you to manage your ads experience.",
                x=40, y=175, w=520, h=20,
            ),
            Node(
                ntype="ocr", text="Read our Privacy Policy and Cookie Policy.",
                x=40, y=960, w=520, h=20,
            ),
            Node(ntype="ocr", text="Reject", x=320, y=1144, w=100, h=23),
            Node(ntype="ocr", text="Accept", x=320, y=1234, w=110, h=25),
        ]

        self.assertTrue(reddit_interests.ads_personalization_screen(nodes))
        self.assertEqual(reddit_open.post_selection_state(nodes, "a0231"), "reddit")

    def test_ads_reject_accepts_notification_modal_over_unchanged_ads_background(self):
        ads = [
            Node(
                ntype="ocr", text="Your ads personalization choices",
                x=35, y=150, w=520, h=35,
            ),
            Node(ntype="ocr", text="Reject", x=320, y=1180, w=100, h=30),
        ]
        # Real a0236 transition shape: Reject succeeded and Reddit raised its
        # own notification modal while the ads page remained visible behind it.
        notification_over_ads = ads + [
            Node(
                ntype="ocr", text="Please turn on notifications",
                x=160, y=560, w=430, h=35,
            ),
            Node(ntype="ocr", text="Not now", x=150, y=720, w=160, h=30),
            Node(ntype="ocr", text="OK", x=480, y=720, w=80, h=30),
        ]
        feed = [
            Node(ntype="ocr", text="Home", x=20, y=1120, w=90, h=25),
            Node(ntype="ocr", text="Create", x=300, y=1120, w=100, h=25),
        ]
        stack = mock.Mock()

        with (
            mock.patch.object(
                reddit_interests,
                "dump_named",
                side_effect=[(None, notification_over_ads), (None, feed)],
            ) as dump,
            mock.patch.object(reddit_interests.time, "sleep"),
        ):
            result = reddit_interests.advance_post_interests(
                stack, mock.Mock(), initial_nodes=ads
            )

        self.assertIs(result, feed)
        self.assertEqual(dump.call_count, 2)
        self.assertEqual(
            stack.tap_xy.call_args_list,
            [mock.call(370, 1195), mock.call(520, 735)],
        )

    def test_ads_reject_notification_then_closes_proven_ads_sheet_once(self):
        ads = [
            Node(
                ntype="ocr", text="Your ads personalization choices",
                x=279, y=160, w=547, h=23,
            ),
            Node(ntype="ocr", text="Reject", x=373, y=1210, w=97, h=20),
        ]
        notification_over_ads = ads + [
            Node(
                ntype="ocr", text="Please turn on notifications",
                x=147, y=584, w=447, h=25,
            ),
            Node(ntype="ocr", text="Not now", x=233, y=769, w=147, h=24),
            Node(ntype="ocr", text="OK", x=506, y=767, w=62, h=23),
        ]
        ads_after_notification = [
            Node(
                ntype="ocr", text="Your ads personalization choices",
                x=276, y=160, w=553, h=27,
            ),
            Node(ntype="ocr", text="Х", x=683, y=158, w=40, h=23),
            Node(ntype="ocr", text="Relect", x=372, y=1209, w=100, h=19),
        ]
        feed = [
            Node(ntype="ocr", text="Home", x=20, y=1120, w=90, h=25),
            Node(ntype="ocr", text="Create", x=300, y=1120, w=100, h=25),
        ]
        stack = mock.Mock()

        with (
            mock.patch.object(
                reddit_interests,
                "dump_named",
                side_effect=[
                    (None, notification_over_ads),
                    (None, ads_after_notification),
                    (None, feed),
                ],
            ) as dump,
            mock.patch.object(reddit_interests.time, "sleep"),
        ):
            result = reddit_interests.advance_post_interests(
                stack, mock.Mock(), initial_nodes=ads
            )

        self.assertIs(result, feed)
        self.assertEqual(dump.call_count, 3)
        self.assertEqual(
            stack.tap_xy.call_args_list,
            [mock.call(421, 1220), mock.call(537, 778), mock.call(703, 169)],
        )

    def test_checked_prefix_mapping_is_generic_for_every_expected_topic(self):
        cases = {
            "• hiking": "hiking",
            "✓ software engineering": "software engineering",
            "✔ dalmatian coast": "dalmatian coast",
            "/ movies": "movies",
            "\\ italy": "italy",
            "v project management": "project management",
        }
        expected = set(cases.values())

        for observed, canonical in cases.items():
            with self.subTest(observed=observed):
                self.assertEqual(
                    reddit_interests._canonical_checked_label(observed, expected),
                    canonical,
                )

    def test_checked_prefix_mapping_never_trims_an_unexpected_topic(self):
        self.assertEqual(
            reddit_interests._canonical_checked_label(
                "• hiking", {"movies", "software engineering"}
            ),
            "• hiking",
        )
        self.assertEqual(
            reddit_interests._canonical_checked_label(
                "new hiking", {"hiking"}
            ),
            "new hiking",
        )

    def test_every_checked_prefix_collapses_to_the_saved_pool_key(self):
        selected = {"italy", "hiking", "project management"}
        visible = ["V Italy", "• Hiking", "✔ Project Management", "Movies"]
        keys = {
            reddit_interests._canonical_checked_label(label.casefold(), selected)
            for label in visible
        }

        self.assertEqual(
            keys,
            {"italy", "hiking", "project management", "movies"},
        )

    def test_checked_prefix_allows_one_ocr_edit_for_saved_topic(self):
        self.assertEqual(
            reddit_interests._canonical_checked_label(
                "~ prsut", {"baking", "netflix", "prout"}
            ),
            "prout",
        )
        self.assertEqual(
            reddit_interests._canonical_checked_label(
                "~ unrelated", {"baking", "netflix", "prout"}
            ),
            "~ unrelated",
        )

    def test_pending_ocr_match_allows_one_insert_delete_or_substitution(self):
        self.assertTrue(reddit_interests._at_most_one_ocr_edit("pr$ut", "prut"))
        self.assertTrue(reddit_interests._at_most_one_ocr_edit("prut", "pr$ut"))
        self.assertTrue(reddit_interests._at_most_one_ocr_edit("pr§ut", "pršut"))
        self.assertFalse(reddit_interests._at_most_one_ocr_edit("movies", "hiking"))
        self.assertFalse(reddit_interests._at_most_one_ocr_edit("pr$$ut", "prut"))

    def test_pending_checked_pill_can_bind_by_unique_same_row_overlap(self):
        pending = {"label": "prout", "bounds": [44, 768, 80, 18]}
        checked = Node(
            ntype="ocr", text="~ Prsut", x=108, y=776, w=116, h=21
        )
        adjacent = Node(
            ntype="ocr", text="/ Netflix", x=476, y=777, w=122, h=20
        )

        self.assertTrue(
            reddit_interests._pending_checked_pill_matches(
                checked, "~ prsut", pending
            )
        )
        self.assertFalse(
            reddit_interests._pending_checked_pill_matches(
                adjacent, "/ netflix", pending
            )
        )

    def test_pending_checked_pill_never_binds_overlap_on_another_row(self):
        pending = {"label": "prout", "bounds": [44, 768, 80, 18]}
        other_row = Node(
            ntype="ocr", text="unrelated", x=60, y=650, w=116, h=21
        )
        self.assertFalse(
            reddit_interests._pending_checked_pill_matches(
                other_row, "unrelated", pending
            )
        )

    def test_pending_capture_retries_fresh_frames_without_another_tap(self):
        pending = {"label": "hiking", "bounds": [525, 833, 90, 23]}
        expected = {"italy", "movies", "hiking"}
        proven = ([], expected)
        with (
            mock.patch.object(
                reddit_interests,
                "_capture_customize_state",
                side_effect=[
                    reddit_interests.PhoneError("pending frame ambiguous"),
                    proven,
                ],
            ) as capture,
            mock.patch.object(reddit_interests.time, "sleep") as sleep,
        ):
            result = reddit_interests._capture_pending_customize_state(
                mock.Mock(), mock.Mock(), "925_customize_verified_3", expected, pending
            )

        self.assertEqual(result, proven)
        self.assertEqual(capture.call_count, 2)
        self.assertEqual(
            capture.call_args_list[1].args[2],
            "925_customize_verified_3_retry_1",
        )
        sleep.assert_called_once_with(
            reddit_interests.CUSTOMIZE_PENDING_VERIFY_SETTLE_SECONDS
        )

    def test_pending_capture_raises_after_bounded_fresh_frames(self):
        pending = {"label": "hiking", "bounds": [525, 833, 90, 23]}
        with (
            mock.patch.object(
                reddit_interests,
                "_capture_customize_state",
                side_effect=reddit_interests.PhoneError("still ambiguous"),
            ) as capture,
            mock.patch.object(reddit_interests.time, "sleep"),
        ):
            with self.assertRaisesRegex(
                reddit_interests.CustomizeSelectionUnclear, "still ambiguous"
            ) as raised:
                reddit_interests._capture_pending_customize_state(
                    mock.Mock(), mock.Mock(), "925", {"hiking"}, pending
                )

        self.assertEqual(
            capture.call_count,
            reddit_interests.CUSTOMIZE_PENDING_VERIFY_RETRIES + 1,
        )
        self.assertFalse(raised.exception.ai_recoverable)

    def test_save_retries_transient_onedrive_replace_lock(self):
        state = {
            "version": 2,
            "slot_id": "a0140",
            "initial": [],
            "customize": ["technology"],
            "pending_customize": None,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "a0140.interests_checkpoint.json"
            target.write_text('{"old": true}\n', encoding="utf-8")
            actual_replace = reddit_interests.os.replace
            attempts = []

            def flaky_replace(source, destination):
                attempts.append((source, destination))
                if len(attempts) < 3:
                    raise PermissionError(13, "OneDrive file temporarily locked")
                return actual_replace(source, destination)

            with (
                mock.patch.object(reddit_interests, "LOG_DIR", temp_dir),
                mock.patch.object(
                    reddit_interests.os, "replace", side_effect=flaky_replace
                ),
                mock.patch.object(reddit_interests.time, "sleep") as sleep,
            ):
                reddit_interests._save_customize_checkpoint("a0140", state)

            self.assertEqual(len(attempts), 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), state)
            self.assertEqual(list(Path(temp_dir).glob(".a0140.interests-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
