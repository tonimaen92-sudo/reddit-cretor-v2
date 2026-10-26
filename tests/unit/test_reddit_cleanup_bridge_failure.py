from __future__ import annotations

import unittest

import paramiko

from friend_poster.device.ssh_device import BridgeTransportError, PhoneError
from friend_poster.reddit.reddit_create_day1 import _raise_cleanup_bridge_failure


class RedditCleanupBridgeFailureTests(unittest.TestCase):
    def test_raw_ssh_exception_keeps_transport_type(self):
        with self.assertRaises(BridgeTransportError):
            _raise_cleanup_bridge_failure(paramiko.SSHException("Channel closed."))

    def test_wrapped_ssh_stream_failure_keeps_transport_type(self):
        with self.assertRaises(BridgeTransportError):
            _raise_cleanup_bridge_failure(
                PhoneError("SSH stream read failed: broken pipe")
            )

    def test_deterministic_cleanup_error_is_not_retyped(self):
        _raise_cleanup_bridge_failure(
            PhoneError("Reddit Recents card was not proven; cleanup remains pending.")
        )


if __name__ == "__main__":
    unittest.main()
