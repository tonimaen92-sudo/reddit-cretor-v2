"""Offline tests for the MacroDroid/FalconProxy IP-change helper."""

from __future__ import annotations

import socket
from datetime import datetime, timedelta, timezone
import sys
import unittest
import urllib.error
import urllib.request
from unittest import mock
from ip_evidence import validate_pair, validate_attempt

import ip_change


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)

    def now(self):
        return datetime(2026, 10, 16, 12, tzinfo=timezone.utc) + timedelta(seconds=self.value)


class CancelEvent:
    def __init__(self, clock: Clock, *, cancel_on_wait: bool = False) -> None:
        self.clock = clock
        self.cancel_on_wait = cancel_on_wait
        self.flag = False

    def is_set(self) -> bool:
        return self.flag

    def wait(self, seconds: float) -> bool:
        self.clock.advance(seconds)
        if self.cancel_on_wait:
            self.flag = True
        return self.flag


class MacroDroidIpChangeTests(unittest.TestCase):
    webhook = "https://trigger.invalid/private-token"
    proxy = "http://proxy-user:proxy-pass@proxy.invalid:1080"

    def test_default_proxy_check_policy_is_fifteen_seconds_and_two_attempts(self):
        self.assertEqual(ip_change.IP_CHECK_TIMEOUT_SECONDS, 15.0)
        self.assertEqual(ip_change.IP_CHECK_ATTEMPTS, 2)

    def run_macro(
        self,
        values,
        *,
        trigger_result=None,
        observation_seconds=13,
        cancel_on_wait=False,
        observation_collector=None,
        require_baseline=False,
        baseline_cycles=1,
    ):
        clock = Clock()
        cancel = CancelEvent(clock, cancel_on_wait=cancel_on_wait)
        trigger_calls = []
        fetch_calls = []
        events = []
        sequence = iter(values)
        last = values[-1] if values else ip_change.IpChangeError("offline")

        def fetcher(url, proxy_url, timeout):
            fetch_calls.append((url, proxy_url, timeout))
            try:
                value = next(sequence)
            except StopIteration:
                value = last
            if isinstance(value, Exception):
                raise value
            return value

        def triggerer(url, timeout):
            trigger_calls.append((url, timeout))
            return trigger_result or {
                "acknowledged": True,
                "uncertain": False,
                "status": 200,
                "error": "",
            }

        result = ip_change.run_macrodroid_change(
            self.webhook,
            self.proxy,
            cancel_event=cancel,
            progress=events.append,
            on_observation=observation_collector,
            operation_id="4dab4c4c-a333-4a1d-88e4-fab1479054d1",
            triggerer=triggerer,
            fetcher=fetcher,
            monotonic=clock.monotonic,
            now=clock.now,
            waiter=cancel.wait,
            observation_seconds=observation_seconds,
            require_baseline=require_baseline,
            baseline_cycles=baseline_cycles,
        )
        return result, trigger_calls, fetch_calls, events

    def test_structured_observations_prove_verified_pair(self):
        observations = []
        result, triggers, _fetches, _events = self.run_macro(
            ["198.51.100.1", "198.51.100.2", "198.51.100.2"],
            observation_collector=observations.append,
        )
        proof = result["verification_evidence"]
        self.assertEqual(result["outcome"], "verified")
        self.assertEqual(len(triggers), 1)
        self.assertEqual([row["phase"] for row in observations],
                         ["baseline", "observation", "observation"])
        self.assertEqual(proof["baseline"], observations[0])
        self.assertEqual(proof["confirmations"], observations[1:])
        self.assertEqual([row["check_sequence"] for row in observations], [1, 2, 3])
        self.assertTrue(all(row["observed_at_utc"].endswith("Z") for row in observations))

    def test_one_trigger_attempt_http_200_without_change(self):
        result, triggers, fetches, _events = self.run_macro(["198.51.100.1"] * 8)
        self.assertEqual(result["outcome"], "no_change")
        self.assertEqual(len(triggers), 1)
        self.assertGreaterEqual(len(fetches), 2)
        self.assertEqual(result["new_ip"], "")

    def test_uncertain_timeout_observes_without_resending(self):
        uncertain = {
            "acknowledged": False,
            "uncertain": True,
            "status": None,
            "error": "Trigger delivery uncertain: request timed out.",
        }
        result, triggers, _fetches, events = self.run_macro(
            ["198.51.100.10", "198.51.100.11", "198.51.100.11"],
            trigger_result=uncertain,
        )
        self.assertEqual(result["outcome"], "verified")
        self.assertTrue(result["trigger_uncertain"])
        self.assertEqual(len(triggers), 1)
        self.assertTrue(any(event["phase"] == "trigger_uncertain" for event in events))

    def test_temporary_loss_then_two_matching_checks_verifies(self):
        offline = ip_change.IpChangeError("FalconProxy IP check is offline or timed out.", category="timeout")
        result, triggers, _fetches, events = self.run_macro(
            ["198.51.100.20", offline, "198.51.100.21", "198.51.100.21"],
            observation_seconds=17,
        )
        self.assertEqual(result["outcome"], "verified")
        self.assertEqual(result["new_ip"], "198.51.100.21")
        self.assertEqual(len(triggers), 1)
        self.assertTrue(any(event["phase"] == "observation_retry" for event in events))

    def test_invalid_and_address_family_mismatch_never_verify(self):
        result, _triggers, _fetches, _events = self.run_macro(
            ["198.51.100.30", "not-an-ip", "2001:db8::30", "2001:db8::30"],
            observation_seconds=13,
        )
        self.assertEqual(result["outcome"], "verification_failed")
        self.assertEqual(result["new_ip"], "")

    def test_candidate_must_be_seen_twice_consecutively(self):
        result, _triggers, _fetches, _events = self.run_macro(
            ["198.51.100.31", "198.51.100.32", "198.51.100.31", "198.51.100.32"],
            observation_seconds=13,
        )
        self.assertNotEqual(result["outcome"], "verified")
        self.assertEqual(result["new_ip"], "")

    def test_proxy_failure_still_triggers_once_without_direct_fallback(self):
        result, triggers, fetches, _events = self.run_macro(
            [ip_change.IpChangeError("proxy unavailable")]
        )
        self.assertEqual(result["outcome"], "verification_failed")
        self.assertEqual(len(triggers), 1)
        self.assertGreaterEqual(len(fetches), 2)
        self.assertTrue(all(call[1] == self.proxy for call in fetches))
        self.assertEqual(result["old_ip"], "")
        self.assertEqual(result["new_ip"], "")
        opener = ip_change.build_proxy_opener(self.proxy)
        strict = next(
            handler
            for handler in opener.handlers
            if isinstance(handler, ip_change.StrictProxyHandler)
        )
        request = urllib.request.Request(ip_change.DEFAULT_PUBLIC_IP_URL)
        with mock.patch.object(urllib.request, "proxy_bypass", return_value=True):
            strict.proxy_open(request, self.proxy, "https")
        self.assertEqual(request.host, "proxy.invalid:1080")

    def test_baseline_timeout_retries_then_triggers_once_and_verifies(self):
        offline = ip_change.IpChangeError("FalconProxy IP check is offline or timed out.", category="timeout")
        result, triggers, fetches, events = self.run_macro(
            [offline, "198.51.100.50", "198.51.100.51", "198.51.100.51"],
            observation_seconds=20,
        )
        self.assertEqual(result["outcome"], "verified")
        self.assertEqual(result["old_ip"], "198.51.100.50")
        self.assertEqual(result["new_ip"], "198.51.100.51")
        self.assertEqual(len(triggers), 1)
        self.assertEqual(len(fetches), 4)
        self.assertTrue(all(call[2] <= 15.0 for call in fetches))
        self.assertTrue(any(event["phase"] == "baseline_retry" for event in events))

    def test_missing_baseline_triggers_but_never_claims_change(self):
        offline = ip_change.IpChangeError("FalconProxy IP check is offline or timed out.", category="timeout")
        result, triggers, fetches, events = self.run_macro(
            [offline, offline, "198.51.100.60", "198.51.100.60"],
            observation_seconds=13,
        )
        self.assertEqual(len(triggers), 1)
        self.assertEqual(len(fetches[:2]), 2)
        self.assertEqual(result["outcome"], "observed_unverified")
        self.assertEqual(result["old_ip"], "")
        self.assertEqual(result["new_ip"], "")
        self.assertEqual(result["current_ip"], "198.51.100.60")
        self.assertTrue(any(event["phase"] == "baseline_warning" for event in events))
        self.assertGreaterEqual(result["elapsed_seconds"], 13.0)

    def test_bulk_baseline_recovers_before_one_trigger(self):
        offline = ip_change.IpChangeError("FalconProxy IP check is offline or timed out.", category="timeout")
        result, triggers, fetches, events = self.run_macro(
            [offline, offline, offline, offline, "198.51.100.60", "198.51.100.61", "198.51.100.61"],
            observation_seconds=13, require_baseline=True, baseline_cycles=4,
        )
        self.assertEqual(result["outcome"], "verified")
        self.assertEqual((result["old_ip"], result["new_ip"]), ("198.51.100.60", "198.51.100.61"))
        self.assertEqual(len(triggers), 1)
        self.assertEqual(len(fetches), 7)
        self.assertTrue(any(event["phase"] == "baseline_warning" for event in events))

    def test_required_baseline_exhaustion_never_submits_unverifiable_reset(self):
        offline = ip_change.IpChangeError("FalconProxy IP check is offline or timed out.", category="timeout")
        result, triggers, fetches, events = self.run_macro(
            [offline], require_baseline=True, baseline_cycles=4,
        )
        self.assertEqual(result["outcome"], "baseline_unavailable")
        self.assertEqual(result["old_ip"], "")
        self.assertFalse(result["trigger_attempted"])
        self.assertEqual(len(triggers), 0)
        self.assertGreaterEqual(len(fetches), 8)
        self.assertTrue(any(
            event.get("phase") == "baseline_warning"
            and "will not submit" in event.get("status", "")
            for event in events
        ))
        self.assertIn("no reset was submitted", result["error"].casefold())

    def test_invalid_configuration_still_blocks_webhook(self):
        clock = Clock()
        cancel = CancelEvent(clock)
        triggers = []
        result = ip_change.run_macrodroid_change(
            self.webhook,
            "not-a-proxy-url",
            cancel_event=cancel,
            triggerer=lambda *_args: triggers.append(True),
            monotonic=clock.monotonic,
            waiter=cancel.wait,
        )
        self.assertEqual(result["outcome"], "verification_failed")
        self.assertEqual(triggers, [])

    def test_cancel_during_baseline_retry_does_not_trigger(self):
        clock = Clock()
        cancel = CancelEvent(clock, cancel_on_wait=True)
        triggers = []
        fetches = []

        def offline(*_args):
            fetches.append(True)
            raise ip_change.IpChangeError("FalconProxy IP check is offline or timed out.", category="timeout")

        result = ip_change.run_macrodroid_change(
            self.webhook,
            self.proxy,
            cancel_event=cancel,
            fetcher=offline,
            triggerer=lambda *_args: triggers.append(True),
            monotonic=clock.monotonic,
            waiter=cancel.wait,
        )
        self.assertEqual(result["outcome"], "cancelled")
        self.assertEqual(len(fetches), 1)
        self.assertEqual(triggers, [])

    def test_setup_fields_encode_socks_credentials(self):
        proxy = ip_change.build_socks5h_proxy_url(
            "proxy.invalid", "1080", "user:name", "p@ss/word"
        )
        self.assertEqual(
            proxy, "socks5h://user%3Aname:p%40ss%2Fword@proxy.invalid:1080"
        )
        self.assertEqual(ip_change.sanitized_endpoint(proxy), "proxy.invalid:1080")
        with self.assertRaises(ip_change.IpChangeError):
            ip_change.validate_proxy_url("socks5://user:pass@proxy.invalid:1080")

    def test_socks_probe_uses_explicit_proxy_without_environment_fallback(self):
        proxy = "socks5h://user:private-token@proxy.invalid:1080"

        class Response:
            status_code = 200
            content = b'{"ip":"198.51.100.99"}'

        class Session:
            def __init__(self):
                self.trust_env = True
                self.calls = []
                self.adapters = []
                self.closed = False

            def mount(self, prefix, adapter):
                self.adapters.append((prefix, adapter))

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()

            def close(self):
                self.closed = True

        session = Session()
        with mock.patch.dict(sys.modules, {"socks": mock.MagicMock()}), mock.patch.object(
            ip_change.requests, "Session", return_value=session
        ), mock.patch.dict("os.environ", {"HTTPS_PROXY": "http://wrong.invalid:8080"}):
            observed = ip_change.fetch_proxy_public_ip(
                ip_change.DEFAULT_PUBLIC_IP_URL, proxy, 10
            )
        self.assertEqual(observed, "198.51.100.99")
        self.assertFalse(session.trust_env)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][1]["proxies"]["https"], proxy)
        self.assertFalse(session.calls[0][1]["allow_redirects"])
        self.assertEqual(session.adapters[0][1].max_retries.total, 0)
        self.assertTrue(session.closed)

    def test_socks_auth_failure_is_redacted_and_never_falls_back(self):
        proxy = "socks5h://user:private-token@proxy.invalid:1080"

        class Session:
            trust_env = True
            calls = 0

            def mount(self, *_args):
                pass

            def get(self, *_args, **_kwargs):
                self.calls += 1
                raise ip_change.requests.exceptions.ProxyError("private-token")

            def close(self):
                pass

        session = Session()
        with mock.patch.dict(sys.modules, {"socks": mock.MagicMock()}), mock.patch.object(
            ip_change.requests, "Session", return_value=session
        ):
            with self.assertRaises(ip_change.IpChangeError) as caught:
                ip_change.fetch_proxy_public_ip(ip_change.DEFAULT_PUBLIC_IP_URL, proxy, 10)
        self.assertEqual(session.calls, 1)
        self.assertFalse(session.trust_env)
        self.assertNotIn("private-token", str(caught.exception))
        self.assertIn("login", str(caught.exception))

    def test_cancel_after_submission_keeps_deadline_metadata(self):
        result, triggers, _fetches, _events = self.run_macro(
            ["198.51.100.40"], cancel_on_wait=True
        )
        self.assertEqual(result["outcome"], "cancelled")
        self.assertTrue(result["trigger_attempted"])
        self.assertGreater(result["trigger_deadline"], result["elapsed_seconds"])
        self.assertEqual(len(triggers), 1)

    def test_trigger_redirect_is_not_followed_and_secret_is_redacted(self):
        self.assertEqual(ip_change.sanitized_endpoint(self.proxy), "proxy.invalid:1080")

        class RedirectOpener:
            def __init__(self):
                self.calls = 0

            def open(self, request, timeout):
                del timeout
                self.calls += 1
                raise urllib.error.HTTPError(request.full_url, 302, "redirect", {}, None)

        redirect = RedirectOpener()
        result = ip_change.trigger_webhook_once(self.webhook, opener=redirect)
        self.assertFalse(result["acknowledged"])
        self.assertEqual(result["status"], 302)
        self.assertEqual(redirect.calls, 1)
        self.assertIsNone(
            ip_change.NoRedirectHandler().redirect_request(
                None, None, 302, "redirect", {}, "https://elsewhere.invalid"
            )
        )

        class LeakyOpener:
            def open(self, _request, timeout):
                del timeout
                raise RuntimeError("private-token")

        safe = ip_change.trigger_webhook_once(self.webhook, opener=LeakyOpener())
        self.assertNotIn("private-token", safe["error"])

    def test_only_http_200_is_acknowledged(self):
        class Response:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class Opener:
            def open(self, _request, timeout):
                self.timeout = timeout
                return Response()

        opener = Opener()
        result = ip_change.trigger_webhook_once(self.webhook, opener=opener)
        self.assertFalse(result["acknowledged"])
        self.assertEqual(result["status"], 204)
        self.assertEqual(opener.timeout, ip_change.TRIGGER_TIMEOUT_SECONDS)

    def test_real_timeout_shape_is_uncertain(self):
        class TimeoutOpener:
            def open(self, _request, timeout):
                del timeout
                raise socket.timeout("possibly delivered")

        result = ip_change.trigger_webhook_once(self.webhook, opener=TimeoutOpener())
        self.assertTrue(result["uncertain"])
        self.assertFalse(result["acknowledged"])

    def test_durable_hook_precedes_single_webhook_and_failure_blocks_it(self):
        clock = Clock()
        cancel = CancelEvent(clock)
        events = []
        result = ip_change.run_macrodroid_change(
            self.webhook, self.proxy, cancel_event=cancel,
            fetcher=lambda *_args: "198.51.100.1",
            before_trigger=lambda proof: events.append(("checkpoint", proof["baseline_ip"])),
            triggerer=lambda *_args: events.append(("webhook", None)) or {
                "acknowledged": False, "uncertain": False, "error": "not sent"
            },
            monotonic=clock.monotonic, waiter=cancel.wait,
        )
        self.assertEqual(events, [("checkpoint", "198.51.100.1"), ("webhook", None)])
        self.assertEqual(result["outcome"], "verification_failed")
        events.clear()
        ip_change.run_macrodroid_change(
            self.webhook, self.proxy, cancel_event=cancel,
            fetcher=lambda *_args: "198.51.100.1",
            before_trigger=lambda _proof: (_ for _ in ()).throw(OSError("disk")),
            triggerer=lambda *_args: events.append("webhook"),
            monotonic=clock.monotonic, waiter=cancel.wait,
        )
        self.assertEqual(events, [])

    def test_read_only_recheck_requires_two_matching_observations(self):
        clock = Clock()
        cancel = CancelEvent(clock)
        values = iter(["198.51.100.2", "198.51.100.2"])
        seen = []
        result = ip_change.observe_proxy_without_trigger(
            self.proxy, "198.51.100.1", require_change=True,
            cancel_event=cancel, observation_seconds=10,
            fetcher=lambda *_args: seen.append(True) or next(values),
            monotonic=clock.monotonic, waiter=cancel.wait,
        )
        self.assertEqual((result["outcome"], result["ip"], len(seen)),
                         ("verified", "198.51.100.2", 2))

    def test_recorded_outage_pattern_confirms_without_another_trigger(self):
        fail = ip_change.IpChangeError("Timeout", category="timeout")
        result, triggers, _fetches, _events = self.run_macro(
            ["31.217.20.252", "31.217.14.168", fail, fail, "31.217.14.168"],
            observation_seconds=25,
        )
        self.assertEqual(result["outcome"], "verified")
        self.assertEqual(len(triggers), 1)
        baseline, first, second = validate_pair(result)
        self.assertEqual((baseline["ip"], first["ip"], second["ip"]),
                         ("31.217.20.252", "31.217.14.168", "31.217.14.168"))
        self.assertGreater(second["check_sequence"] - first["check_sequence"], 1)

    def test_invalid_response_inside_retry_discards_prior_candidate(self):
        result, _triggers, fetches, _events = self.run_macro(
            ["198.51.100.1", "198.51.100.2", "invalid", "198.51.100.2", "198.51.100.2"],
            observation_seconds=25,
        )
        self.assertEqual(result["outcome"], "verified")
        self.assertEqual(len(fetches), 5)
        self.assertEqual([item["check_sequence"] for item in result["verification_evidence"]["confirmations"]], [4, 5])

    def test_expired_candidate_cannot_confirm(self):
        clock = Clock()
        cancel = CancelEvent(clock)
        count = [0]
        def fetch(*_args):
            count[0] += 1
            if count[0] == 2:
                clock.advance(91)
            return "198.51.100.2"
        result = ip_change.observe_proxy_without_trigger(
            self.proxy, "198.51.100.1", require_change=True, cancel_event=cancel,
            observation_seconds=98, fetcher=fetch, monotonic=clock.monotonic, now=clock.now,
        )
        self.assertEqual(result["outcome"], "verification_failed")
        self.assertEqual(count[0], 2)

    def test_hard_failures_do_not_bridge_candidates_and_are_redacted(self):
        for category in ("configuration", "unknown", "tls"):
            with self.subTest(category=category):
                error = ip_change.IpChangeError("private-token proxy-pass", category=category)
                result, triggers, fetches, _events = self.run_macro(
                    ["198.51.100.1", "198.51.100.2", error, "198.51.100.2"], observation_seconds=30,
                )
                self.assertEqual(result["outcome"], "verification_failed")
                self.assertEqual(len(fetches), 3)
                self.assertEqual(len(triggers), 1)
                self.assertNotIn("private-token", str(result))
                self.assertIsNone(result["observation_state"]["candidate"])

    def test_authentication_recovers_without_bridging_old_candidate(self):
        auth = ip_change.IpChangeError("private-token proxy-pass", category="authentication")
        result, triggers, fetches, events = self.run_macro(
            ["198.51.100.1", "198.51.100.2", auth, "198.51.100.2", "198.51.100.2"],
            observation_seconds=30,
        )
        self.assertEqual(result["outcome"], "verified")
        self.assertEqual(len(triggers), 1)
        self.assertEqual(len(fetches), 5)
        self.assertEqual(
            [item["check_sequence"] for item in result["verification_evidence"]["confirmations"]],
            [4, 5],
        )
        self.assertTrue(any("authentication unavailable" in event.get("status", "") for event in events))
        self.assertNotIn("private-token", str(result) + str(events))

    def test_timeout_connection_then_authentication_recovers_in_one_trigger(self):
        timeout = ip_change.IpChangeError("private-token", category="timeout")
        connection = ip_change.IpChangeError("private-token", category="connection")
        auth = ip_change.IpChangeError("private-token", category="authentication")
        result, triggers, fetches, events = self.run_macro(
            ["198.51.100.1", timeout, connection, auth, "198.51.100.2", "198.51.100.2"],
            observation_seconds=30,
        )
        self.assertEqual(result["outcome"], "verified")
        self.assertEqual(len(triggers), 1)
        self.assertEqual(len(fetches), 6)
        self.assertEqual(
            [item["check_sequence"] for item in result["verification_evidence"]["confirmations"]],
            [5, 6],
        )
        self.assertNotIn("private-token", str(result) + str(events))

    def test_persistent_authentication_pauses_after_deadline(self):
        auth = ip_change.IpChangeError("private-token proxy-pass", category="authentication")
        result, triggers, fetches, events = self.run_macro(
            ["198.51.100.1", auth], observation_seconds=12,
        )
        self.assertEqual(result["outcome"], "verification_failed")
        self.assertEqual(result["error_category"], "authentication")
        self.assertIn("authentication did not recover", result["error"])
        self.assertEqual(len(triggers), 1)
        self.assertGreaterEqual(len(fetches), 2)
        self.assertIsNone(result["verification_evidence"])
        self.assertNotIn("private-token", str(result) + str(events))

    def test_response_after_deadline_or_stop_never_verifies(self):
        for reason in ("late", "stop"):
            with self.subTest(reason=reason):
                clock = Clock()
                cancel = CancelEvent(clock)
                count = [0]
                def fetch(*_args):
                    count[0] += 1
                    if count[0] == 2:
                        if reason == "late":
                            clock.advance(30)
                        else:
                            cancel.flag = True
                    return "198.51.100.2"
                observations = []
                result = ip_change.observe_proxy_without_trigger(
                    self.proxy, "198.51.100.1", require_change=True, cancel_event=cancel,
                    observation_seconds=10, fetcher=fetch, monotonic=clock.monotonic,
                    now=clock.now, on_observation=observations.append,
                )
                self.assertNotEqual(result["outcome"], "verified")
                self.assertEqual(len(observations), 1)

    def test_restart_can_reuse_fresh_durable_candidate_without_webhook(self):
        clock = Clock()
        cancel = CancelEvent(clock)
        saved = []
        def checkpoint(state):
            validate_attempt(state)
            saved.append(state)
            if state["candidate"]:
                cancel.flag = True
        first = ip_change.observe_proxy_without_trigger(
            self.proxy, "198.51.100.1", require_change=True, cancel_event=cancel,
            fetcher=lambda *_args: "198.51.100.2", monotonic=clock.monotonic, now=clock.now,
            proxy_scope="a" * 64, on_checkpoint=checkpoint,
        )
        self.assertEqual(first["outcome"], "cancelled")
        cancel.flag = False
        clock.advance(10)
        second = ip_change.observe_proxy_without_trigger(
            self.proxy, "198.51.100.1", require_change=True, cancel_event=cancel,
            fetcher=lambda *_args: "198.51.100.2", monotonic=clock.monotonic, now=clock.now,
            proxy_scope="a" * 64, attempt=saved[-1], observation_seconds=170,
        )
        self.assertEqual(second["outcome"], "verified")
        self.assertTrue(second["observation_only"])
        validate_pair(second)

    def test_changed_proxy_scope_stops_read_only_recovery_before_network(self):
        clock = Clock()
        cancel = CancelEvent(clock)
        result = ip_change.observe_proxy_without_trigger(
            self.proxy, "198.51.100.1", require_change=True, cancel_event=cancel,
            fetcher=lambda *_args: "198.51.100.2", monotonic=clock.monotonic, now=clock.now,
            proxy_scope="a" * 64,
        )
        with mock.patch.object(ip_change, "trigger_webhook_once", side_effect=AssertionError("reset")):
            changed = ip_change.observe_proxy_without_trigger(
                self.proxy, "198.51.100.1", require_change=True, cancel_event=cancel,
                fetcher=lambda *_args: self.fail("network call on changed scope"),
                attempt=result["observation_state"], proxy_scope="b" * 64,
            )
        self.assertEqual(changed["error_category"], "configuration")

    def test_structured_errors_never_parse_or_return_secret_exception_text(self):
        import socks
        secret = "socks5h://user:private-token@proxy.invalid:1080"
        cases = [
            (socks.SOCKS5AuthError(secret), "authentication"),
            (ip_change.requests.exceptions.ConnectTimeout(secret), "timeout"),
            (ConnectionRefusedError(secret), "connection"),
            (ip_change.requests.exceptions.InvalidURL(secret), "configuration"),
            (ip_change.requests.exceptions.SSLError(secret), "tls"),
            (RuntimeError(secret), "unknown"),
        ]
        for cause, category in cases:
            with self.subTest(category=category):
                wrapper = ip_change.requests.exceptions.ConnectionError(secret)
                wrapper.__cause__ = cause
                # An unclassified wrapper is not proof that its unknown cause is transient.
                error = ip_change._network_error(cause if category == "unknown" else wrapper)
                self.assertEqual(error.category, category)
                self.assertNotIn("private-token", str(error))

    def test_contradictory_success_requires_a_new_pair(self):
        for value in ("198.51.100.1", "198.51.100.3", "2001:db8::2", "invalid"):
            with self.subTest(value=value):
                result, _, fetches, _ = self.run_macro(
                    ["198.51.100.1", "198.51.100.2", value, "198.51.100.2", "198.51.100.2"],
                    observation_seconds=30,
                )
                self.assertEqual(result["outcome"], "verified")
                self.assertEqual(len(fetches), 5)
                self.assertEqual([e["check_sequence"] for e in result["verification_evidence"]["confirmations"]], [4, 5])

    def test_recovery_cannot_extend_expired_saved_deadline(self):
        clock = Clock()
        cancel = CancelEvent(clock)
        first = ip_change.observe_proxy_without_trigger(
            self.proxy, "198.51.100.1", require_change=True, cancel_event=cancel,
            fetcher=lambda *_a: "198.51.100.2", monotonic=clock.monotonic, now=clock.now,
            proxy_scope="a" * 64,
        )
        clock.advance(181)
        result = ip_change.observe_proxy_without_trigger(
            self.proxy, "198.51.100.1", require_change=True, cancel_event=cancel,
            fetcher=lambda *_a: self.fail("request after saved deadline"),
            monotonic=clock.monotonic, now=clock.now, proxy_scope="a" * 64,
            attempt=first["observation_state"], observation_seconds=180,
        )
        self.assertEqual(result["outcome"], "verification_failed")
        self.assertFalse(result["trigger_attempted"])

    def test_checkpoint_failure_cannot_report_verification(self):
        clock = Clock()
        cancel = CancelEvent(clock)
        def checkpoint(state):
            if state["verification_evidence"]:
                raise OSError("private-token")
        result = ip_change.observe_proxy_without_trigger(
            self.proxy, "198.51.100.1", require_change=True, cancel_event=cancel,
            fetcher=lambda *_a: "198.51.100.2", monotonic=clock.monotonic, now=clock.now,
            on_checkpoint=checkpoint,
        )
        self.assertEqual((result["outcome"], result["error_category"]), ("verification_failed", "storage"))
        self.assertIsNone(result["verification_evidence"])
        self.assertNotIn("private-token", str(result))


if __name__ == "__main__":
    unittest.main(verbosity=2)
