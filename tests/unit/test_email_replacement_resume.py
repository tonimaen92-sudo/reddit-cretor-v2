"""Offline regressions for durable Reddit verification alias rotation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from friend_poster.apps import creator
from friend_poster.reddit import reddit_create_day1 as create
from friend_poster.reddit import reddit_to_verify as to_verify
from friend_poster.core import slots
from friend_poster.device.icloud_code import VerificationCodeTimeout
from friend_poster.device.ssh_device import PhoneError
from friend_poster.device.ui_tree import Node, parse_nodes
from friend_poster.core.paths import DUMP_DIR


def node(text: str, y: int = 200) -> Node:
    return Node(ntype="ocr", text=text, x=40, y=y, w=500, h=30)


VERIFY_NODES = [
    node("Verify your email", 120),
    node("Verification code", 260),
    node("If you don't see our email, check spam", 380),
    node("Continue", 900),
]
USERNAME_NODES = [node("Create your username", 150)]
ABOUT_NODES = [node("About you", 150), node("Birthday", 300)]


class VerifyBackProofTests(unittest.TestCase):
    def test_actual_879_dump_proves_only_the_bounded_verify_screen(self):
        saved = DUMP_DIR / "879_verify.json"
        nodes = parse_nodes(saved.read_text(encoding="utf-8"))
        self.assertTrue(to_verify.proven_verify_back_screen(nodes))
        self.assertFalse(
            to_verify.proven_verify_back_screen(
                [node("Verify your email"), node("Continue")]
            )
        )


class SlotEmailCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.slot_path = str(Path(self.temp.name) / "slots.json")
        self.patches = (
            mock.patch.object(slots, "LOG_DIR", self.temp.name),
            mock.patch.object(slots, "SLOTS_PATH", self.slot_path),
        )
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        slots.save_slots([
            {
                "id": "a0049",
                "ghost": True,
                "email": "original@icloud.com",
                "email_label": "a0049",
                "reddit": False,
                "day1": False,
            }
        ])

    def test_pending_and_activation_are_resumable_and_preserve_old_alias(self):
        slots.mark_email_verification(
            "a0049", 0, "a0049", "timed_out"
        )
        slots.mark_email_verification(
            "a0049", 1, "a0049-1", "replacement_pending"
        )
        pending = slots.email_verification_state(slots.get_slot("a0049"))
        self.assertEqual(pending["phase"], "replacement_pending")
        self.assertEqual(slots.get_slot("a0049")["email"], "original@icloud.com")

        slots.replace_slot_email(
            "a0049",
            "original@icloud.com",
            "replacement1@icloud.com",
            label="a0049-1",
            replacement_index=1,
        )
        active = slots.get_slot("a0049")
        self.assertEqual(active["email"], "replacement1@icloud.com")
        self.assertEqual(active["email_label"], "a0049-1")
        self.assertEqual(
            active["email_alias_history"],
            [{"email": "original@icloud.com", "label": "a0049"}],
        )
        self.assertEqual(
            slots.email_verification_state(active)["phase"], "replacement_ready"
        )
        with self.assertRaisesRegex(ValueError, "email changed"):
            slots.replace_slot_email(
                "a0049",
                "original@icloud.com",
                "replacement2@icloud.com",
                label="a0049-2",
                replacement_index=2,
            )

    def test_exact_pending_label_reconciles_without_blind_creation(self):
        records = [
            {"label": "a0049", "email": "original@icloud.com"},
            {"label": "a0049-1", "email": "replacement1@icloud.com"},
        ]
        self.assertEqual(
            creator._reconcile_pipeline_email(
                "a0049", slots.load_slots(), records, label="a0049-1"
            ),
            "replacement1@icloud.com",
        )
        self.assertIsNone(
            creator._reconcile_pipeline_email(
                "a0049", slots.load_slots(), records, label="a0049-2"
            )
        )


class VerificationRotationStateMachineTests(unittest.TestCase):
    def test_post_password_verify_reuses_active_email_and_reaches_about(self):
        state = {
            "id": "a0049",
            "email": "alias0@icloud.com",
            "email_label": "a0049",
            "email_replacement_index": 0,
            "email_verification": None,
        }
        marks = []
        clears = []

        def mark(_sid, index, label, phase, _log=None):
            marks.append((index, label, phase))
            state["email_verification"] = {
                "replacement_index": index,
                "label": label,
                "phase": phase,
            }

        with (
            mock.patch.object(create, "get_slot", side_effect=lambda _sid: dict(state)),
            mock.patch.object(create, "mark_email_verification", side_effect=mark),
            mock.patch.object(
                create,
                "clear_email_verification",
                side_effect=lambda *_args: clears.append("cleared"),
            ),
            mock.patch.object(create, "poll_reddit_code", return_value="123456") as poll,
            mock.patch.object(create, "type_code", return_value=ABOUT_NODES),
            mock.patch.object(
                create,
                "wait_verification_outcome",
                return_value=("about", ABOUT_NODES),
            ) as wait,
            mock.patch.object(
                create,
                "run_to_verify",
                side_effect=AssertionError("post-password Verify resubmitted email"),
            ),
        ):
            result = create._verify_email_after_password(
                "a0049", object(), object(), VERIFY_NODES
            )

        self.assertEqual(result, ABOUT_NODES)
        self.assertEqual(marks, [(0, "a0049", "submitted")])
        self.assertEqual(clears, ["cleared"])
        self.assertEqual(poll.call_args.kwargs["email_hint"], "alias0@icloud.com")
        self.assertEqual(wait.call_args.kwargs["success_outcome"], "about")

    def test_post_password_verify_unknown_transition_preserves_checkpoint(self):
        state = {
            "id": "a0049",
            "email": "alias0@icloud.com",
            "email_label": "a0049",
            "email_replacement_index": 0,
            "email_verification": None,
        }

        def mark(_sid, index, label, phase, _log=None):
            state["email_verification"] = {
                "replacement_index": index,
                "label": label,
                "phase": phase,
            }

        with (
            mock.patch.object(create, "get_slot", side_effect=lambda _sid: dict(state)),
            mock.patch.object(create, "mark_email_verification", side_effect=mark),
            mock.patch.object(create, "clear_email_verification") as clear,
            mock.patch.object(create, "poll_reddit_code", return_value="123456"),
            mock.patch.object(create, "type_code", return_value=[node("Unknown")]),
            mock.patch.object(
                create,
                "wait_verification_outcome",
                return_value=("unknown", [node("Unknown")]),
            ),
        ):
            with self.assertRaisesRegex(PhoneError, "did not reach"):
                create._verify_email_after_password(
                    "a0049", object(), object(), VERIFY_NODES
                )
        clear.assert_not_called()
        self.assertEqual(state["email_verification"]["phase"], "submitted")

    def test_post_password_verify_accepts_proven_username_replay_screen(self):
        state = {
            "id": "a0049",
            "email": "alias0@icloud.com",
            "email_label": "a0049",
            "email_replacement_index": 0,
            "email_verification": None,
        }

        def mark(_sid, index, label, phase, _log=None):
            state["email_verification"] = {
                "replacement_index": index,
                "label": label,
                "phase": phase,
            }

        clears = []
        with (
            mock.patch.object(create, "get_slot", side_effect=lambda _sid: dict(state)),
            mock.patch.object(create, "mark_email_verification", side_effect=mark),
            mock.patch.object(
                create,
                "clear_email_verification",
                side_effect=lambda *_args: clears.append("cleared"),
            ),
            mock.patch.object(create, "poll_reddit_code", return_value="123456"),
            mock.patch.object(create, "type_code", return_value=USERNAME_NODES),
            mock.patch.object(
                create,
                "wait_verification_outcome",
                return_value=("username", USERNAME_NODES),
            ),
        ):
            result = create._verify_email_after_password(
                "a0049", object(), object(), VERIFY_NODES
            )
        self.assertEqual(result, USERNAME_NODES)
        self.assertEqual(clears, ["cleared"])

    def test_replacement_reopens_active_bundle_without_crane_selection(self):
        class Stack:
            def __init__(self):
                self.opened = []

            def open_bundle(self, bundle):
                self.opened.append(bundle)

        stack = Stack()
        with (
            mock.patch.object(
                to_verify, "dump_named", return_value=("", VERIFY_NODES)
            ),
            mock.patch.object(
                to_verify, "wait_ui", return_value=VERIFY_NODES
            ),
            mock.patch.object(to_verify, "step", return_value="VERIFY"),
            mock.patch.object(
                to_verify,
                "open_container",
                side_effect=AssertionError("replacement reselected Crane container"),
            ),
        ):
            to_verify.run_to_verify(
                stack,
                object(),
                "a0049",
                "replacement@icloud.com",
                reuse_active_container=True,
            )
        self.assertEqual(stack.opened, [to_verify.REDDIT_BUNDLE])

    def test_migrated_timeout_reopens_exact_container_from_proven_home_only(self):
        home_nodes = parse_nodes(
            (DUMP_DIR / "880_pre_alias_resume_check.json").read_text(
                encoding="utf-8"
            )
        )
        verify_nodes = parse_nodes(
            (DUMP_DIR / "879_verify.json").read_text(encoding="utf-8")
        )
        state = {
            "id": "a0049",
            "email": "alias0@icloud.com",
            "email_label": "a0049",
            "email_replacement_index": 0,
            "email_verification": None,
        }
        opened = []

        def mark(_sid, index, label, phase, _log=None):
            state["email_verification"] = {
                "replacement_index": index,
                "label": label,
                "phase": phase,
                "updated_at": "2026-10-16T21:28:00Z",
            }
            return dict(state)

        with (
            mock.patch.object(create, "get_slot", side_effect=lambda _sid: dict(state)),
            mock.patch.object(
                create,
                "email_verification_state",
                side_effect=lambda row: row.get("email_verification"),
            ),
            mock.patch.object(create, "mark_email_verification", side_effect=mark),
            mock.patch.object(
                create,
                "open_container",
                side_effect=lambda *_args: opened.append("a0049"),
            ),
            mock.patch.object(
                create, "dump_named", return_value=("", verify_nodes)
            ),
        ):
            with self.assertRaises(create.EmailReplacementExhausted):
                create._verify_email_with_replacements(
                    "a0049",
                    object(),
                    object(),
                    home_nodes,
                    rotate_before_poll=True,
                    max_email_replacements=0,
                )
        self.assertEqual(opened, ["a0049"])
        self.assertEqual(state["email_verification"]["phase"], "timed_out")

        state["email_verification"] = None
        with (
            mock.patch.object(create, "get_slot", side_effect=lambda _sid: dict(state)),
            mock.patch.object(
                create,
                "email_verification_state",
                side_effect=lambda row: row.get("email_verification"),
            ),
            mock.patch.object(
                create,
                "open_container",
                side_effect=AssertionError("unrecognized screen opened a container"),
            ),
        ):
            with self.assertRaisesRegex(PhoneError, "exact Verify screen"):
                create._verify_email_with_replacements(
                    "a0049",
                    object(),
                    object(),
                    [node("Settings")],
                    rotate_before_poll=True,
                )

    def _run(self, success_attempt: int | None):
        state = {
            "id": "a0049",
            "email": "alias0@icloud.com",
            "email_label": "a0049",
            "email_replacement_index": 0,
            "email_verification": None,
        }
        labels = []
        polls = []
        submissions = []
        self.last_labels = labels
        self.last_polls = polls

        def get_slot(_sid):
            return dict(state)

        def mark(_sid, index, label, phase, _log=None):
            state["email_verification"] = {
                "replacement_index": index,
                "label": label,
                "phase": phase,
                "updated_at": "2026-10-16T21:28:00Z",
            }
            return dict(state)

        def clear(_sid, _log=None):
            state["email_verification"] = None
            return dict(state)

        def replace(old, index, label):
            self.assertEqual(old, state["email"])
            labels.append(label)
            state["email"] = f"alias{index}@icloud.com"
            state["email_label"] = label
            state["email_replacement_index"] = index
            mark("a0049", index, label, "replacement_ready")
            return state["email"]

        def poll(**kwargs):
            attempt = len(polls)
            polls.append(kwargs["email_hint"])
            if success_attempt is not None and attempt == success_attempt:
                return "123456"
            raise VerificationCodeTimeout("TimeoutError")

        def submit(*_args, **kwargs):
            submissions.append(
                (state["email"], bool(kwargs.get("reuse_active_container")))
            )

        def dump(*_args, **_kwargs):
            return "", list(VERIFY_NODES)

        patches = (
            mock.patch.object(create, "get_slot", side_effect=get_slot),
            mock.patch.object(create, "mark_email_verification", side_effect=mark),
            mock.patch.object(create, "clear_email_verification", side_effect=clear),
            mock.patch.object(create, "poll_reddit_code", side_effect=poll),
            mock.patch.object(create, "run_to_verify", side_effect=submit),
            mock.patch.object(create, "dump_named", side_effect=dump),
            mock.patch.object(create, "return_verify_to_email_entry", return_value=[]),
            mock.patch.object(create, "type_code", return_value=USERNAME_NODES),
            mock.patch.object(
                create,
                "wait_verification_outcome",
                return_value=("username", USERNAME_NODES),
            ),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
            result = create._verify_email_with_replacements(
                "a0049",
                object(),
                object(),
                VERIFY_NODES,
                email_replacer=replace,
                rotate_before_poll=False,
                max_email_replacements=3,
            )
        return result, labels, polls, submissions, state

    def test_success_on_original_and_each_numbered_replacement(self):
        for success_attempt in range(4):
            with self.subTest(success_attempt=success_attempt):
                result, labels, polls, _submissions, state = self._run(success_attempt)
                self.assertEqual(result, USERNAME_NODES)
                self.assertEqual(
                    labels,
                    [f"a0049-{index}" for index in range(1, success_attempt + 1)],
                )
                self.assertEqual(len(polls), success_attempt + 1)
                self.assertIsNone(state["email_verification"])

    def test_three_replacements_exhaust_without_creating_a_fourth(self):
        with self.assertRaises(create.EmailReplacementExhausted):
            self._run(None)
        self.assertEqual(
            self.last_labels, ["a0049-1", "a0049-2", "a0049-3"]
        )
        self.assertEqual(len(self.last_polls), 4)
        self.assertTrue(issubclass(create.EmailReplacementExhausted, PhoneError))
        self.assertEqual(create.MAX_EMAIL_REPLACEMENTS, 3)
        self.assertNotIn("a0049-4", self.last_labels)


class ReplacementPreparationTests(unittest.TestCase):
    def _app(self):
        app = creator.App.__new__(creator.App)
        app.log = lambda _message: None
        app.stop = mock.Mock()
        app.stop.is_set.return_value = False
        return app

    def test_new_alias_is_created_without_an_ip_reset(self):
        app = self._app()
        order = []
        checkpoint = {
            "replacement_index": 1,
            "label": "a0049-1",
            "phase": "replacement_pending",
        }
        with (
            mock.patch.object(creator, "get_slot", return_value={}),
            mock.patch.object(
                creator, "email_verification_state", return_value=checkpoint
            ),
            mock.patch.object(creator, "load_slots", return_value=[]),
            mock.patch.object(creator, "_load_pipeline_email_records", return_value=[]),
            mock.patch.object(creator, "_reconcile_pipeline_email", return_value=None),
            mock.patch.object(
                creator,
                "create_one_hide_my_email",
                side_effect=lambda *_args, **_kwargs: (
                    order.append("email") or "new@icloud.com"
                ),
            ),
            mock.patch.object(creator, "replace_slot_email"),
        ):
            result = app._replace_verification_email(
                "a0049",
                object(),
                object(),
                "old@icloud.com",
                1,
                "a0049-1",
            )
        self.assertEqual(result, "new@icloud.com")
        self.assertEqual(order, ["email"])

    def test_replacement_does_not_depend_on_ip_reset_configuration(self):
        app = self._app()
        checkpoint = {
            "replacement_index": 1,
            "label": "a0049-1",
            "phase": "replacement_pending",
        }
        create_alias = mock.Mock(return_value="new@icloud.com")
        with (
            mock.patch.object(creator, "get_slot", return_value={}),
            mock.patch.object(
                creator, "email_verification_state", return_value=checkpoint
            ),
            mock.patch.object(creator, "load_slots", return_value=[]),
            mock.patch.object(creator, "_load_pipeline_email_records", return_value=[]),
            mock.patch.object(creator, "_reconcile_pipeline_email", return_value=None),
            mock.patch.object(
                creator, "create_one_hide_my_email", create_alias
            ),
            mock.patch.object(creator, "replace_slot_email"),
        ):
            result = app._replace_verification_email(
                "a0049",
                object(),
                object(),
                "old@icloud.com",
                1,
                "a0049-1",
            )
        self.assertEqual(result, "new@icloud.com")
        create_alias.assert_called_once()

    def test_reconciled_alias_does_not_trigger_a_second_ip_reset(self):
        app = self._app()
        checkpoint = {
            "replacement_index": 1,
            "label": "a0049-1",
            "phase": "replacement_pending",
        }
        with (
            mock.patch.object(creator, "get_slot", return_value={}),
            mock.patch.object(
                creator, "email_verification_state", return_value=checkpoint
            ),
            mock.patch.object(creator, "load_slots", return_value=[]),
            mock.patch.object(creator, "_load_pipeline_email_records", return_value=[]),
            mock.patch.object(
                creator,
                "_reconcile_pipeline_email",
                return_value="existing@icloud.com",
            ),
            mock.patch.object(creator, "replace_slot_email"),
        ):
            app._replace_verification_email(
                "a0049",
                object(),
                object(),
                "old@icloud.com",
                1,
                "a0049-1",
            )


if __name__ == "__main__":
    unittest.main()
