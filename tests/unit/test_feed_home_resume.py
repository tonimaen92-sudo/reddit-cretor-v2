import unittest
from unittest import mock

from friend_poster.reddit import reddit_create_day1 as create
from friend_poster.reddit.feed_swipe import FeedVisualRecoveryExhausted
from friend_poster.reddit.reddit_open import make_created_account_feed_resume_proof
from friend_poster.services.ui_recovery_agent import is_ui_recoverable_error
from friend_poster.device.ui_tree import Node


class RunningFeedResumeTests(unittest.TestCase):
    @staticmethod
    def _node(text, x, y, w=260, h=24):
        return Node(ntype="ocr", text=text, x=x, y=y, w=w, h=h)

    def test_durable_created_account_accepts_two_scrolled_feed_cards(self):
        nodes = [
            self._node("Join", 580, 170, 90),
            self._node("First distinct card body text", 40, 300, 520),
            self._node("Second distinct card body text", 40, 600, 520),
            self._node("Join", 580, 1030, 90),
            self._node("Third distinct card body text", 40, 1100, 520),
            self._node("Fourth distinct card body text", 40, 1140, 520),
        ]
        self.assertIsNotNone(make_created_account_feed_resume_proof(nodes))

    def test_created_account_card_proof_rejects_one_card(self):
        nodes = [
            self._node("Join", 580, 170, 90),
            self._node("First distinct card body text", 40, 300, 520),
            self._node("Second distinct card body text", 40, 600, 520),
            self._node("Third distinct card body text", 40, 900, 520),
            self._node("Fourth distinct card body text", 40, 1120, 520),
        ]
        self.assertIsNone(make_created_account_feed_resume_proof(nodes))

    def test_created_account_card_proof_rejects_onboarding_title(self):
        nodes = [
            self._node("Choose your interests", 100, 90, 440),
            self._node("Join", 580, 170, 90),
            self._node("First distinct card body text", 40, 300, 520),
            self._node("Second distinct card body text", 40, 600, 520),
            self._node("Join", 580, 1030, 90),
            self._node("Third distinct card body text", 40, 1100, 520),
            self._node("Fourth distinct card body text", 40, 1140, 520),
        ]
        self.assertIsNone(make_created_account_feed_resume_proof(nodes))

    def test_running_reddit_is_foregrounded_without_crane(self):
        stack = mock.Mock()
        stack.app_is_running.return_value = True
        icon = object()
        feed_nodes = [object()]
        messages = []

        with (
            mock.patch.object(create, "_find_reddit_from_home", return_value=icon),
            mock.patch.object(create, "tap_home_icon") as tap,
            mock.patch.object(
                create, "dump_named", return_value=("synthetic", feed_nodes)
            ),
            mock.patch.object(create, "protected_reddit_marker", return_value=""),
            mock.patch.object(
                create, "proven_reddit_feed_structure", return_value=True
            ),
            mock.patch.object(create, "open_container") as open_container,
        ):
            result = create._foreground_running_reddit_feed(
                "a0142", object(), stack, emit=messages.append
            )

        self.assertIs(result, feed_nodes)
        tap.assert_called_once_with(stack, icon)
        open_container.assert_not_called()
        self.assertTrue(any("without reopening Crane" in item for item in messages))

    def test_stopped_reddit_leaves_exact_container_fallback_to_caller(self):
        stack = mock.Mock()
        stack.app_is_running.return_value = False
        with mock.patch.object(create, "_find_reddit_from_home") as find_icon:
            result = create._foreground_running_reddit_feed(
                "a0142", object(), stack
            )
        self.assertIsNone(result)
        find_icon.assert_not_called()

    def test_exhausted_unknown_feed_state_opts_into_one_ai_fallback(self):
        error = FeedVisualRecoveryExhausted("local feed handlers exhausted")
        self.assertTrue(error.ai_recoverable)
        self.assertTrue(is_ui_recoverable_error(error))


if __name__ == "__main__":
    unittest.main()
