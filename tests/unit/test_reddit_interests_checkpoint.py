import json
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from friend_poster.reddit import reddit_interests
from friend_poster.reddit import reddit_open
from friend_poster.device.ui_tree import Node, parse_nodes


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"
A0267_BEFORE = "a0267_customize_before_pending"
A0267_AFTER = "a0267_customize_after_pending_ocr_missing"


def _fixture_nodes(stem: str):
    return parse_nodes(
        (FIXTURE_DIR / f"{stem}.json").read_text(encoding="utf-8")
    )


def _copy_visual_fixture(temp_dir: str, source_stem: str, target_stem: str) -> None:
    """Copy JSON before PNG so the paired screenshot cannot look stale."""
    target = Path(temp_dir)
    (target / f"{target_stem}.json").write_bytes(
        (FIXTURE_DIR / f"{source_stem}.json").read_bytes()
    )
    (target / f"{target_stem}.png").write_bytes(
        (FIXTURE_DIR / f"{source_stem}.png").read_bytes()
    )


def _a0267_pending():
    before = _fixture_nodes(A0267_BEFORE)
    node = next(
        node
        for node in reddit_interests.customize_topics(before)
        if reddit_interests.customize_topic_label(node) == "prut"
    )
    return {"label": "prut", "bounds": [node.x, node.y, node.w, node.h]}


def _classify_fixture(
    source_stem: str,
    expected: set[str],
    *,
    pending: dict | None = None,
    selected_proofs: dict[str, list[float]] | None = None,
):
    nodes = _fixture_nodes(source_stem)
    with tempfile.TemporaryDirectory() as temp_dir:
        stem = "paired_customize_frame"
        _copy_visual_fixture(temp_dir, source_stem, stem)
        with mock.patch.object(reddit_interests, "DUMP_DIR", temp_dir):
            return reddit_interests._selected_customize_topics(
                nodes,
                stem,
                expected_labels=expected,
                pending=pending,
                selected_proofs=selected_proofs,
            )


def _run_verified_pending_resume_flow():
    """Exercise the pending-resume path without issuing a real device action."""
    pending = {"label": "prut", "bounds": [50, 639, 78, 18]}
    selected = {"movies", "netflix", "prut", "sports"}
    checkpoint = {
        "version": 3,
        "slot_id": "a0267",
        "initial": [],
        "customize": ["movies", "netflix", "sports"],
        "selected_proofs": {},
        "pending_customize": copy.deepcopy(pending),
    }
    saved_states = []
    final_capture_proofs = []
    stack = mock.Mock()

    def verify_pending(*_args, **kwargs):
        kwargs["observed_selected_bounds"]["prut"] = list(pending["bounds"])
        return ["pending-verified"], set(selected)

    def capture_final(*_args, **kwargs):
        final_capture_proofs.append(copy.deepcopy(kwargs["selected_proofs"]))
        return ["ready"], set(selected)

    def save_state(_sid, state):
        saved_states.append(copy.deepcopy(state))

    with (
        mock.patch.object(
            reddit_interests,
            "_load_customize_checkpoint",
            return_value=copy.deepcopy(checkpoint),
        ),
        mock.patch.object(
            reddit_interests,
            "_save_customize_checkpoint",
            side_effect=save_state,
        ),
        mock.patch.object(
            reddit_interests,
            "_prove_customize_anchor",
            return_value=["live"],
        ),
        mock.patch.object(
            reddit_interests,
            "_capture_pending_customize_state",
            side_effect=verify_pending,
        ),
        mock.patch.object(
            reddit_interests,
            "_capture_customize_state",
            side_effect=capture_final,
        ),
        mock.patch.object(reddit_interests, "tap_continue_bar", return_value=True),
        mock.patch.object(reddit_interests, "_wait_screen_close", return_value=[]),
        mock.patch.object(reddit_interests, "_record_interests"),
        mock.patch.object(reddit_interests, "advance_post_interests"),
        mock.patch.object(reddit_interests.CREATION_PROGRESS, "note"),
    ):
        result = reddit_interests.pick_customize_feed(
            stack,
            mock.Mock(),
            "a0267",
            initial_nodes=["live"],
        )

    return pending, selected, saved_states, final_capture_proofs, stack, result


class InterestsCheckpointSaveTests(unittest.TestCase):
    def test_checked_long_pill_ignores_right_shifted_ocr_sample(self):
        from PIL import Image, ImageDraw

        bounds = [175, 776, 174, 20]
        selected = Image.new("RGB", (750, 1334), "white")
        ImageDraw.Draw(selected).rounded_rectangle(
            (35, 740, 283, 815), radius=20, fill=(201, 215, 222)
        )
        self.assertEqual(
            reddit_interests._customize_pill_pixel_state(selected, bounds),
            "selected",
        )

        unselected = Image.new("RGB", (750, 1334), "white")
        self.assertEqual(
            reddit_interests._customize_pill_pixel_state(unselected, bounds),
            "unselected",
        )

    def test_exact_checked_label_in_pre_action_band_is_locally_proven(self):
        from PIL import Image, ImageDraw

        node = Node(ntype="ocr", text="/ Hair Care", x=128, y=1157, w=174, h=18)
        image = Image.new("RGB", (750, 1334), "white")
        ImageDraw.Draw(image).rounded_rectangle(
            (35, 1120, 305, 1179), radius=18, fill=(201, 215, 222)
        )

        matches = reddit_interests._clipped_expected_customize_nodes(
            [node], "hair care"
        )
        self.assertEqual(matches, [node])
        self.assertEqual(
            reddit_interests._customize_pill_pixel_state(
                image, [node.x, node.y, node.w, node.h]
            ),
            "selected",
        )

    def test_v2_live_pending_checkpoint_migrates_to_v3_without_losing_action(self):
        pending = {"label": "prut", "bounds": [50, 639, 78, 18]}
        version_two = {
            "version": 2,
            "slot_id": "a0267",
            "initial": ["technology"],
            "customize": ["netflix"],
            "pending_customize": pending,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "a0267.interests_checkpoint.json").write_text(
                json.dumps(version_two),
                encoding="utf-8",
            )
            with mock.patch.object(reddit_interests, "LOG_DIR", temp_dir):
                loaded = reddit_interests._load_customize_checkpoint("a0267")

        self.assertEqual(loaded["version"], 3)
        self.assertEqual(loaded["pending_customize"], pending)
        self.assertEqual(loaded["selected_proofs"], {})
        self.assertEqual(loaded["customize"], ["netflix"])

    def test_verified_pending_is_atomically_committed_without_tap_replay(self):
        pending, selected, saves, _final_proofs, stack, result = (
            _run_verified_pending_resume_flow()
        )

        self.assertGreaterEqual(len(saves), 1)
        committed = saves[0]
        self.assertEqual(committed["version"], 3)
        self.assertEqual(set(committed["customize"]), selected)
        self.assertIsNone(committed["pending_customize"])
        self.assertEqual(
            committed["selected_proofs"],
            {"prut": pending["bounds"]},
        )
        stack.tap_xy.assert_not_called()
        self.assertEqual(set(result), selected)

    def test_final_capture_receives_proof_promoted_from_verified_pending(self):
        pending, _selected, _saves, final_proofs, _stack, _result = (
            _run_verified_pending_resume_flow()
        )

        self.assertEqual(final_proofs, [{"prut": pending["bounds"]}])

    def test_partial_resume_carries_promoted_proof_through_next_tap_and_final_capture(self):
        old_pending = {"label": "prut", "bounds": [50, 639, 78, 18]}
        sports = Node(ntype="ocr", text="Sports", x=300, y=730, w=90, h=20)
        live_nodes = [
            Node(ntype="ocr", text="Customize your feed", x=130, y=180, w=490, h=30),
            Node(
                ntype="ocr",
                text="Find more of your interests",
                x=100,
                y=370,
                w=500,
                h=25,
            ),
            sports,
            Node(ntype="ocr", text="Skip", x=150, y=1230, w=70, h=25),
            Node(ntype="ocr", text="Continue", x=500, y=1230, w=130, h=25),
        ]
        checkpoint = {
            "version": 3,
            "slot_id": "a0267",
            "initial": [],
            "customize": ["movies", "netflix"],
            "selected_proofs": {},
            "pending_customize": copy.deepcopy(old_pending),
        }
        selected_after_resume = {"movies", "netflix", "prut"}
        selected_after_new_tap = selected_after_resume | {"sports"}
        pending_capture_proofs = []
        final_capture_proofs = []
        pending_calls = 0

        def capture_pending(*_args, **kwargs):
            nonlocal pending_calls
            pending_calls += 1
            observed = kwargs["observed_selected_bounds"]
            observed.clear()
            observed["prut"] = list(old_pending["bounds"])
            if pending_calls == 1:
                return live_nodes, set(selected_after_resume)

            pending_capture_proofs.append(copy.deepcopy(kwargs["selected_proofs"]))
            new_pending = kwargs["pending"]
            self.assertEqual(new_pending["label"], "sports")
            observed["sports"] = list(new_pending["bounds"])
            return live_nodes, set(selected_after_new_tap)

        def capture_final(*_args, **kwargs):
            final_capture_proofs.append(copy.deepcopy(kwargs["selected_proofs"]))
            return live_nodes, set(selected_after_new_tap)

        stack = mock.Mock()
        with (
            mock.patch.object(
                reddit_interests,
                "_load_customize_checkpoint",
                return_value=copy.deepcopy(checkpoint),
            ),
            mock.patch.object(reddit_interests, "_save_customize_checkpoint"),
            mock.patch.object(
                reddit_interests,
                "_prove_customize_anchor",
                return_value=live_nodes,
            ) as prove_anchor,
            mock.patch.object(
                reddit_interests,
                "_capture_pending_customize_state",
                side_effect=capture_pending,
            ),
            mock.patch.object(
                reddit_interests,
                "_capture_existing_customize_state",
                side_effect=capture_final,
            ),
            mock.patch.object(reddit_interests, "tap_continue_bar", return_value=True),
            mock.patch.object(reddit_interests, "_wait_screen_close", return_value=[]),
            mock.patch.object(reddit_interests, "_record_interests"),
            mock.patch.object(reddit_interests, "advance_post_interests"),
            mock.patch.object(reddit_interests.CREATION_PROGRESS, "note"),
            mock.patch.object(reddit_interests.time, "sleep"),
        ):
            result = reddit_interests.pick_customize_feed(
                stack,
                mock.Mock(),
                "a0267",
                initial_nodes=live_nodes,
            )

        self.assertEqual(pending_calls, 2)
        self.assertFalse(prove_anchor.call_args.kwargs["allow_scroll"])
        self.assertEqual(stack.tap_xy.call_args_list, [mock.call(*sports.center())])
        self.assertNotEqual(sports.center(), (89, 648))
        self.assertEqual(
            pending_capture_proofs,
            [{"prut": old_pending["bounds"]}],
        )
        self.assertEqual(
            final_capture_proofs[0]["prut"],
            old_pending["bounds"],
        )
        self.assertIn("sports", final_capture_proofs[0])
        self.assertEqual(set(result), selected_after_new_tap)

    def test_anchor_guard_does_not_scroll_with_durable_selection_coordinates(self):
        stack = mock.Mock()
        with (
            mock.patch.object(reddit_interests, "small_scroll") as small_scroll,
            mock.patch.object(reddit_interests, "dump_named") as dump_named,
            mock.patch.object(reddit_interests.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(
                reddit_interests.CustomizeSelectionUnclear,
                "durable selection bounds",
            ):
                reddit_interests._prove_customize_anchor(
                    stack,
                    mock.Mock(),
                    [],
                    "924_customize_live",
                    expected_labels={"prut"},
                    # pick_customize_feed sets this guard whenever its v3
                    # checkpoint contains a pending action or saved proof.
                    allow_scroll=False,
                )

        small_scroll.assert_not_called()
        dump_named.assert_not_called()
        sleep.assert_not_called()

    def test_a0292_anchorless_complete_selection_uses_read_only_verification(self):
        nodes = [
            Node(ntype="ocr", text="Customize your feed", x=130, y=180, w=490, h=30),
            Node(ntype="ocr", text="Find more of your interests", x=100, y=370, w=500, h=25),
            Node(ntype="ocr", text="Stock Market", x=177, y=583, w=176, h=20),
            Node(ntype="ocr", text="Investing", x=415, y=584, w=124, h=23),
            Node(ntype="ocr", text="Earthquakes", x=170, y=711, w=166, h=20),
            Node(ntype="ocr", text="Voting Rights", x=529, y=970, w=172, h=23),
            Node(ntype="ocr", text="Skip", x=193, y=1240, w=68, h=23),
            Node(ntype="ocr", text="Continue", x=548, y=1240, w=140, h=23),
        ]
        stack = mock.Mock()

        with (
            mock.patch.object(reddit_interests, "small_scroll") as small_scroll,
            mock.patch.object(reddit_interests, "dump_named") as dump_named,
            mock.patch.object(reddit_interests.time, "sleep") as sleep,
        ):
            result = reddit_interests._prove_customize_anchor(
                stack,
                mock.Mock(),
                nodes,
                "924_customize_live",
                expected_labels={
                    "stock market",
                    "investing",
                    "earthquakes",
                    "voting rights",
                },
                allow_scroll=False,
            )

        self.assertIs(result, nodes)
        small_scroll.assert_not_called()
        dump_named.assert_not_called()
        sleep.assert_not_called()

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

    def test_a0262_pixel_selected_prut_symbol_drift_reconciles_to_checkpoint(self):
        from PIL import Image, ImageDraw

        expected = {"prut", "movies", "sports", "travel"}
        nodes = [
            Node(ntype="ocr", text="Customize your feed", x=130, y=180, w=490, h=30),
            Node(ntype="ocr", text="Find more of your interests", x=100, y=370, w=500, h=25),
            Node(ntype="ocr", text="Pr§ut", x=50, y=500, w=100, h=22),
            Node(ntype="ocr", text="Movies", x=190, y=500, w=100, h=22),
            Node(ntype="ocr", text="Sports", x=330, y=500, w=100, h=22),
            Node(ntype="ocr", text="Travel", x=470, y=500, w=100, h=22),
            Node(ntype="ocr", text="Skip", x=150, y=1230, w=70, h=25),
            Node(ntype="ocr", text="Continue", x=500, y=1230, w=130, h=25),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            stem = "a0262_customize_selected"
            Path(temp_dir, stem + ".json").write_text("[]\n", encoding="utf-8")
            image = Image.new("RGB", (750, 1334), "white")
            draw = ImageDraw.Draw(image)
            for node in nodes[2:6]:
                cx, cy = node.center()
                draw.rectangle(
                    (cx - 55, cy - 25, cx + 55, cy + 25),
                    fill=(201, 215, 222),
                )
            image.save(Path(temp_dir, stem + ".png"))

            pixel_selected = set()
            with mock.patch.object(reddit_interests, "DUMP_DIR", temp_dir):
                selected = reddit_interests._selected_customize_topics(
                    nodes,
                    stem,
                    expected_labels=expected,
                    pixel_selected_labels=pixel_selected,
                )
                pending_selected = reddit_interests._selected_customize_topics(
                    nodes,
                    stem,
                    expected_labels=expected,
                    pending={"label": "travel", "bounds": [470, 500, 100, 22]},
                )

        self.assertEqual(selected, expected)
        self.assertEqual(pending_selected, expected)
        self.assertEqual(pixel_selected, {"pr§ut", "movies", "sports", "travel"})

    def test_a0292_clipped_bottom_topic_is_filtered_before_pixel_classification(self):
        """A partial pill behind the fixed action bar must not abort a proven tap."""
        from PIL import Image, ImageDraw

        expected = {"stock market", "investing", "earthquakes", "voting rights"}
        nodes = [
            Node(ntype="ocr", text="Customize your feed", x=130, y=180, w=490, h=30),
            Node(ntype="ocr", text="Find more of your interests", x=100, y=370, w=500, h=25),
            Node(ntype="ocr", text="Stock Market", x=177, y=583, w=176, h=20),
            Node(ntype="ocr", text="Investing", x=415, y=584, w=124, h=23),
            Node(ntype="ocr", text="Earthquakes", x=170, y=711, w=166, h=20),
            Node(ntype="ocr", text="Voting Rights", x=529, y=970, w=172, h=23),
            Node(ntype="ocr", text="Relationship Advice", x=392, y=1098, w=246, h=18),
            # Real OCR, but the pill is partly hidden by the fixed action bar.
            Node(ntype="ocr", text="Political Theory", x=145, y=1157, w=216, h=18),
            Node(ntype="ocr", text="Skip", x=193, y=1240, w=68, h=23),
            Node(ntype="ocr", text="Continue", x=548, y=1240, w=140, h=23),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            stem = "a0292_customize_verified_4"
            Path(temp_dir, stem + ".json").write_text("[]\n", encoding="utf-8")
            image = Image.new("RGB", (750, 1334), "white")
            draw = ImageDraw.Draw(image)
            for node in nodes[2:6]:
                cx, cy = node.center()
                draw.rectangle(
                    (cx - node.w / 2, cy - 25, cx + node.w / 2, cy + 25),
                    fill=(201, 215, 222),
                )
            image.save(Path(temp_dir, stem + ".png"))

            with mock.patch.object(reddit_interests, "DUMP_DIR", temp_dir):
                selected = reddit_interests._selected_customize_topics(
                    nodes,
                    stem,
                    expected_labels=expected,
                )

        self.assertEqual(selected, expected)
        self.assertNotIn(
            "political theory",
            {
                reddit_interests.customize_topic_label(node)
                for node in reddit_interests.customize_topics(nodes)
            },
        )

    def test_a0267_pending_selection_uses_saved_bounds_when_ocr_node_disappears(self):
        pending = _a0267_pending()
        after = _fixture_nodes(A0267_AFTER)
        visible = {
            reddit_interests.customize_topic_label(node)
            for node in reddit_interests.customize_topics(after)
        }

        self.assertNotIn("prut", visible)
        self.assertEqual(
            _classify_fixture(
                A0267_AFTER,
                {"netflix", "prut"},
                pending=pending,
            ),
            {"netflix", "prut"},
        )

    def test_first_pending_selection_follows_unique_pixel_only_reflow(self):
        from PIL import Image, ImageDraw

        nodes = [
            Node(ntype="ocr", text="Customize your feed", x=130, y=180, w=490, h=30),
            Node(ntype="ocr", text="Find more of your interests", x=100, y=370, w=500, h=25),
            Node(ntype="ocr", text="Movies", x=260, y=580, w=100, h=22),
            Node(ntype="ocr", text="Sports", x=410, y=580, w=100, h=22),
            Node(ntype="ocr", text="Show More Topics", x=40, y=970, w=220, h=22),
            Node(ntype="ocr", text="Skip", x=150, y=1230, w=70, h=25),
            Node(ntype="ocr", text="Continue", x=500, y=1230, w=130, h=25),
        ]
        pending = {"label": "prut", "bounds": [590, 500, 80, 20]}

        with tempfile.TemporaryDirectory() as temp_dir:
            stem = "first_pending_reflow"
            Path(temp_dir, stem + ".json").write_text("[]\n", encoding="utf-8")
            image = Image.new("RGB", (750, 1334), "white")
            ImageDraw.Draw(image).rectangle(
                (35, 555, 190, 610), fill=(201, 215, 222)
            )
            image.save(Path(temp_dir, stem + ".png"))
            observed_bounds = {}
            with mock.patch.object(reddit_interests, "DUMP_DIR", temp_dir):
                selected = reddit_interests._selected_customize_topics(
                    nodes,
                    stem,
                    expected_labels={"prut"},
                    pending=pending,
                    observed_selected_bounds=observed_bounds,
                )

        self.assertEqual(selected, {"prut"})
        self.assertEqual(set(observed_bounds), {"prut"})
        self.assertNotEqual(observed_bounds["prut"], pending["bounds"])

    def test_first_pending_selection_rejects_multiple_pixel_only_pills(self):
        from PIL import Image, ImageDraw

        nodes = [
            Node(ntype="ocr", text="Customize your feed", x=130, y=180, w=490, h=30),
            Node(ntype="ocr", text="Find more of your interests", x=100, y=370, w=500, h=25),
            Node(ntype="ocr", text="Show More Topics", x=40, y=970, w=220, h=22),
            Node(ntype="ocr", text="Skip", x=150, y=1230, w=70, h=25),
            Node(ntype="ocr", text="Continue", x=500, y=1230, w=130, h=25),
        ]
        pending = {"label": "prut", "bounds": [590, 500, 80, 20]}

        with tempfile.TemporaryDirectory() as temp_dir:
            stem = "ambiguous_first_pending_reflow"
            Path(temp_dir, stem + ".json").write_text("[]\n", encoding="utf-8")
            image = Image.new("RGB", (750, 1334), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((35, 555, 190, 610), fill=(201, 215, 222))
            draw.rectangle((300, 555, 455, 610), fill=(201, 215, 222))
            image.save(Path(temp_dir, stem + ".png"))
            with (
                mock.patch.object(reddit_interests, "DUMP_DIR", temp_dir),
                self.assertRaises(reddit_interests.CustomizeSelectionUnclear),
            ):
                reddit_interests._selected_customize_topics(
                    nodes,
                    stem,
                    expected_labels={"prut"},
                    pending=pending,
                )

    def test_a0267_pending_recovery_never_issues_a_second_tap(self):
        expected = {"netflix", "prut"}
        pending = _a0267_pending()
        nodes = _fixture_nodes(A0267_AFTER)
        stack = mock.Mock()

        with tempfile.TemporaryDirectory() as temp_dir:
            stem = "a0267_pending_fresh"
            _copy_visual_fixture(temp_dir, A0267_AFTER, stem)
            with (
                mock.patch.object(reddit_interests, "DUMP_DIR", temp_dir),
                mock.patch.object(
                    reddit_interests,
                    "dump_named",
                    return_value=(None, nodes),
                ),
            ):
                captured_nodes, selected = (
                    reddit_interests._capture_pending_customize_state(
                        stack,
                        mock.Mock(),
                        stem,
                        expected,
                        pending,
                    )
                )

        self.assertIs(captured_nodes, nodes)
        self.assertEqual(selected, expected)
        stack.tap_xy.assert_not_called()

    def test_a0267_white_pre_tap_bounds_do_not_prove_pending_selection(self):
        with self.assertRaises(reddit_interests.PhoneError):
            _classify_fixture(
                A0267_BEFORE,
                {"netflix", "prut"},
                pending=_a0267_pending(),
            )

    def test_a0267_selected_proof_survives_after_pending_is_cleared(self):
        proof = _a0267_pending()

        self.assertEqual(
            _classify_fixture(
                A0267_AFTER,
                {"netflix", "prut"},
                selected_proofs={"prut": proof["bounds"]},
            ),
            {"netflix", "prut"},
        )

    def test_a0267_selected_proof_cannot_claim_another_visible_pill(self):
        after = _fixture_nodes(A0267_AFTER)
        netflix = next(
            node
            for node in reddit_interests.customize_topics(after)
            if reddit_interests.customize_topic_label(node) == "netflix"
        )
        overlapping_proof = [netflix.x, netflix.y, netflix.w, netflix.h]

        with self.assertRaises(reddit_interests.PhoneError):
            _classify_fixture(
                A0267_AFTER,
                {"netflix", "prut"},
                selected_proofs={"prut": overlapping_proof},
            )

    def test_a0267_selected_proof_rejects_malformed_or_out_of_range_bounds(self):
        malformed = (
            [float("nan"), 639, 78, 18],
            [50, 639, 0, 18],
            [50, 1229, 78, 18],
        )
        for bounds in malformed:
            with self.subTest(bounds=bounds):
                with self.assertRaises(reddit_interests.PhoneError):
                    _classify_fixture(
                        A0267_AFTER,
                        {"netflix", "prut"},
                        selected_proofs={"prut": bounds},
                    )

    def test_partial_resume_pool_excludes_exact_pixel_proven_ocr_alias(self):
        nodes = [
            Node(ntype="ocr", text="Customize your feed", x=130, y=180, w=490, h=30),
            Node(ntype="ocr", text="Find more of your interests", x=100, y=370, w=500, h=25),
            Node(ntype="ocr", text="Prut", x=50, y=500, w=100, h=22),
            Node(ntype="ocr", text="Movies", x=190, y=500, w=100, h=22),
            Node(ntype="ocr", text="Sports", x=330, y=500, w=100, h=22),
            Node(ntype="ocr", text="Travel", x=470, y=500, w=100, h=22),
            Node(ntype="ocr", text="Skip", x=150, y=1230, w=70, h=25),
            Node(ntype="ocr", text="Continue", x=500, y=1230, w=130, h=25),
        ]

        pool = reddit_interests._customize_topic_pool(
            nodes,
            selected={"pr§ut"},
            pixel_selected_labels={"prut"},
        )

        self.assertNotIn("prut", pool)
        self.assertEqual(set(pool), {"movies", "sports", "travel"})

    def test_selected_reconciliation_reserves_exact_matches_first(self):
        self.assertEqual(
            reddit_interests._reconcile_pixel_selected_labels(
                {"cat", "cot"}, {"cat", "cut"}
            ),
            {"cat", "cut"},
        )

    def test_selected_reconciliation_rejects_ambiguous_bijection(self):
        self.assertIsNone(
            reddit_interests._reconcile_pixel_selected_labels(
                {"cat", "cot"}, {"cut", "cit"}
            )
        )

    def test_selected_reconciliation_rejects_missing_or_extra_labels(self):
        cases = (
            ({"movies", "sports", "travel"}, {"prut", "movies", "sports", "travel"}),
            (
                {"prut", "movies", "sports", "travel", "music"},
                {"prut", "movies", "sports", "travel"},
            ),
        )
        for observed, expected in cases:
            with self.subTest(observed_count=len(observed), expected_count=len(expected)):
                self.assertIsNone(
                    reddit_interests._reconcile_pixel_selected_labels(observed, expected)
                )

    def test_final_visual_selection_mismatch_is_ai_recoverable(self):
        saved = {"prut", "movies", "sports", "travel"}
        checkpoint = {
            "version": 2,
            "slot_id": "a0262",
            "initial": [],
            "customize": sorted(saved),
            "pending_customize": None,
        }
        with (
            mock.patch.object(
                reddit_interests, "_load_customize_checkpoint", return_value=checkpoint
            ),
            mock.patch.object(reddit_interests, "_save_customize_checkpoint"),
            mock.patch.object(reddit_interests, "_prove_customize_anchor", return_value=[]),
            mock.patch.object(
                reddit_interests,
                "_capture_customize_state",
                side_effect=[([], saved), ([], {"pr§ut", "movies", "sports", "travel"})],
            ),
        ):
            with self.assertRaises(reddit_interests.CustomizeSelectionUnclear) as raised:
                reddit_interests.pick_customize_feed(
                    mock.Mock(), mock.Mock(), "a0262", initial_nodes=[]
                )

        self.assertTrue(raised.exception.ai_recoverable)

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
                    reddit_interests.CustomizeSelectionUnclear(
                        "pending frame ambiguous"
                    ),
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
                side_effect=reddit_interests.CustomizeSelectionUnclear(
                    "still ambiguous"
                ),
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
        self.assertTrue(raised.exception.ai_recoverable)

    def test_customize_search_allows_one_ocr_edit_in_unique_bounded_anchor(self):
        nodes = [
            Node(ntype="ocr", text="Customize your feed", x=370, y=197, w=490, h=35),
            Node(ntype="ocr", text="O Find more of vour interests", x=287, y=378, w=468, h=23),
            Node(ntype="ocr", text="Skip", x=195, y=1240, w=65, h=23),
            Node(ntype="ocr", text="Continue", x=548, y=1240, w=140, h=23),
        ]

        self.assertIsNotNone(reddit_interests.customize_search_node(nodes))
        self.assertTrue(reddit_interests.customize_screen(nodes))

    def test_customize_search_rejects_broadly_corrupted_anchor(self):
        nodes = [
            Node(ntype="ocr", text="Customize your feed", x=370, y=197, w=490, h=35),
            Node(ntype="ocr", text="O Find several unrelated things", x=287, y=378, w=468, h=23),
            Node(ntype="ocr", text="Skip", x=195, y=1240, w=65, h=23),
            Node(ntype="ocr", text="Continue", x=548, y=1240, w=140, h=23),
        ]

        self.assertIsNone(reddit_interests.customize_search_node(nodes))
        self.assertFalse(reddit_interests.customize_screen(nodes))

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
