"""Anthropic Messages API schema regressions for UI recovery tool results.

These are offline tests.  They intentionally inspect the exact request body
handed to the injected fake HTTP transport so a malformed conversation can
never reach Anthropic or turn into an endless HTTP 400 retry loop.
"""

from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from friend_poster.device.ssh_device import PhoneError
from friend_poster.services import ui_recovery_agent as recovery


SCREEN_CONTINUE = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication visible="true" enabled="true" x="0" y="0" width="750" height="1334">
  <XCUIElementTypeButton name="Continue" label="Continue" visible="true" enabled="true" x="260" y="1050" width="230" height="70" />
</XCUIElementTypeApplication>"""

SCREEN_READY = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication visible="true" enabled="true" x="0" y="0" width="750" height="1334">
  <XCUIElementTypeStaticText name="Home Popular Watch Chat Inbox" label="Home Popular Watch Chat Inbox" visible="true" enabled="true" x="40" y="1200" width="670" height="70" />
</XCUIElementTypeApplication>"""

SCREEN_SENSITIVE = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication visible="true" enabled="true" x="0" y="0" width="750" height="1334">
  <XCUIElementTypeSecureTextField name="Password" label="Password" value="hidden" visible="true" enabled="true" x="100" y="300" width="550" height="80" />
</XCUIElementTypeApplication>"""


def _png_bytes(color=(240, 240, 240)) -> bytes:
    image = Image.new("RGB", (750, 1334), color)
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


class _FakeStack:
    backend = "xxtouch"

    def __init__(self, screens):
        self.screens = list(screens)
        self.index = 0
        self.taps = []

    def dump_xml(self):
        return self.screens[min(self.index, len(self.screens) - 1)]

    def screenshot_png(self):
        return _png_bytes((240 - self.index * 20, 240, 240))

    def tap_xy(self, x, y):
        self.taps.append((x, y))
        self.index = min(self.index + 1, len(self.screens) - 1)

    def swipe(self, _x1, _y1, _x2, _y2):
        self.index = min(self.index + 1, len(self.screens) - 1)

    def long_tap(self, _x, _y, hold_ms=550):
        del hold_ms
        self.index = min(self.index + 1, len(self.screens) - 1)

    def press_home(self):
        return None

    def open_bundle(self, _bundle):
        return None


class _FakeResponse:
    headers = {}

    def __init__(self, content, *, status_code=200, error=""):
        self._content = content
        self.status_code = status_code
        self._error = error

    def json(self):
        if self.status_code != 200:
            return {
                "error": {
                    "type": "invalid_request_error",
                    "message": self._error,
                }
            }
        return {"model": "fake-sonnet", "content": self._content}


def _frame_id(body) -> str:
    for message in reversed(body.get("messages", [])):
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        for block in reversed(blocks):
            nested = block.get("content") if isinstance(block, dict) else None
            candidates = (
                nested
                if isinstance(nested, list)
                else [{"type": "text", "text": nested}]
                if isinstance(nested, str)
                else [block]
            )
            for candidate in reversed(candidates):
                if not isinstance(candidate, dict) or candidate.get("type") != "text":
                    continue
                for line in reversed(str(candidate.get("text") or "").splitlines()):
                    try:
                        payload = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(payload, dict) and payload.get("frame_id"):
                        return str(payload["frame_id"])
    raise AssertionError("request body has no frame_id")


def _tool_results(body):
    for message in body.get("messages", []):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                yield block


def _assert_valid_tool_result(testcase: unittest.TestCase, result: dict) -> None:
    testcase.assertIsInstance(result.get("tool_use_id"), str)
    testcase.assertTrue(result["tool_use_id"].strip())
    content = result.get("content")
    if isinstance(content, str):
        testcase.assertTrue(content.strip())
        return
    testcase.assertIsInstance(content, list)
    testcase.assertTrue(content)
    for block in content:
        testcase.assertIsInstance(block, dict)
        testcase.assertIn(block.get("type"), {"text", "image"})
    if result.get("is_error") is True:
        testcase.assertTrue(
            all(block.get("type") == "text" for block in content),
            "Anthropic requires text-only tool_result content when is_error=true",
        )


class UIRecoveryPayloadSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.stop = threading.Event()

    def tearDown(self):
        self.temp.cleanup()

    def _agent(self, stack, post):
        return recovery.UIRecoveryAgent(
            stack,
            object(),
            stop=self.stop,
            log=lambda _message: None,
            store=recovery.RecipeStore(Path(self.temp.name) / "recipes.sqlite3"),
            post=post,
            # These tests exercise multi-turn payload repair. Production uses
            # the shared one-call budget asserted in test_ui_recovery_agent.
            attempt_budget=recovery.RecoveryAttemptBudget(max_calls=6),
        )

    def _recover(self, agent, *, previous=None):
        with patch.object(
            recovery,
            "load_anthropic_settings",
            return_value=("sk-ant-api-test", "fake-sonnet"),
        ):
            return agent.recover(PhoneError("visual screen stuck"), previous=previous)

    def test_rejected_sensitive_tap_sends_text_only_error_result(self):
        bodies = []

        def post(_url, *, headers, json, timeout):
            del headers, timeout
            bodies.append(json)
            if len(bodies) == 1:
                return _FakeResponse(
                    [
                        {
                            "type": "tool_use",
                            "id": "tap-sensitive",
                            "name": "tap_point",
                            "input": {"frame_id": _frame_id(json), "x": 375, "y": 340},
                        }
                    ]
                )
            return _FakeResponse(
                [{"type": "tool_use", "id": "resume", "name": "resume_pipeline", "input": {}}]
            )

        agent = self._agent(_FakeStack([SCREEN_SENSITIVE]), post)
        self._recover(agent)

        rejected = next(
            result
            for result in _tool_results(bodies[1])
            if result.get("tool_use_id") == "tap-sensitive"
        )
        self.assertIs(rejected.get("is_error"), True)
        _assert_valid_tool_result(self, rejected)

    def test_repeated_resume_rejection_is_also_text_only(self):
        bodies = []

        def post(_url, *, headers, json, timeout):
            del headers, timeout
            bodies.append(json)
            if len(bodies) == 1:
                tool = {
                    "type": "tool_use",
                    "id": "resume-too-early",
                    "name": "resume_pipeline",
                    "input": {},
                }
            elif len(bodies) == 2:
                tool = {
                    "type": "tool_use",
                    "id": "change-screen",
                    "name": "tap_element",
                    "input": {"frame_id": _frame_id(json), "element_id": "e0001"},
                }
            else:
                tool = {
                    "type": "tool_use",
                    "id": "resume-after-change",
                    "name": "resume_pipeline",
                    "input": {},
                }
            return _FakeResponse([tool])

        stack = _FakeStack([SCREEN_CONTINUE, SCREEN_READY])
        agent = self._agent(stack, post)
        initial = agent.observe(PhoneError("visual screen stuck"))
        previous = recovery.RecoverySession(
            incident_fingerprint=initial.fingerprint,
            final_fingerprint=initial.fingerprint,
        )
        self._recover(agent, previous=previous)

        rejected = next(
            result
            for result in _tool_results(bodies[1])
            if result.get("tool_use_id") == "resume-too-early"
        )
        self.assertIs(rejected.get("is_error"), True)
        _assert_valid_tool_result(self, rejected)

    def test_successful_observe_result_may_include_fresh_image(self):
        bodies = []

        def post(_url, *, headers, json, timeout):
            del headers, timeout
            bodies.append(json)
            if len(bodies) == 1:
                return _FakeResponse(
                    [{"type": "tool_use", "id": "observe-1", "name": "observe", "input": {}}]
                )
            return _FakeResponse(
                [{"type": "tool_use", "id": "resume", "name": "resume_pipeline", "input": {}}]
            )

        agent = self._agent(_FakeStack([SCREEN_CONTINUE]), post)
        self._recover(agent)

        observed = next(
            result
            for result in _tool_results(bodies[1])
            if result.get("tool_use_id") == "observe-1"
        )
        self.assertIsNot(observed.get("is_error"), True)
        _assert_valid_tool_result(self, observed)
        self.assertIn("image", {block["type"] for block in observed["content"]})

    def test_malformed_tool_use_never_creates_empty_tool_result_id(self):
        bodies = []

        def post(_url, *, headers, json, timeout):
            del headers, timeout
            bodies.append(json)
            if len(bodies) == 1:
                return _FakeResponse(
                    [{"type": "tool_use", "id": "", "name": "not_a_tool", "input": {}}]
                )
            return _FakeResponse(
                [{"type": "tool_use", "id": "resume", "name": "resume_pipeline", "input": {}}]
            )

        agent = self._agent(_FakeStack([SCREEN_CONTINUE]), post)
        self._recover(agent)

        for result in _tool_results(bodies[1]):
            _assert_valid_tool_result(self, result)

    def test_api_boundary_repairs_error_result_before_http_post(self):
        bodies = []

        def post(_url, *, headers, json, timeout):
            del headers, timeout
            bodies.append(json)
            return _FakeResponse([])

        agent = self._agent(_FakeStack([SCREEN_CONTINUE]), post)
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tool-1", "name": "observe", "input": {}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "is_error": True,
                        "content": [
                            {"type": "text", "text": "rejected"},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": "not-real-image-data",
                                },
                            },
                        ],
                    }
                ],
            },
        ]
        with patch.object(
            recovery,
            "load_anthropic_settings",
            return_value=("sk-ant-api-test", "fake-sonnet"),
        ):
            agent._api_call(messages, 1)

        sent = next(_tool_results(bodies[0]))
        _assert_valid_tool_result(self, sent)

    def test_http_400_tool_result_schema_error_rebuilds_and_calls_again_immediately(self):
        bodies = []

        def post(_url, *, headers, json, timeout):
            del headers, timeout
            bodies.append(json)
            if len(bodies) == 1:
                return _FakeResponse(
                    [],
                    status_code=400,
                    error=(
                        "messages.18.content.0.tool_result: all content must be "
                        "type `text` if `is_error` is true"
                    ),
                )
            return _FakeResponse(
                [
                    {
                        "type": "tool_use",
                        "id": "resume-after-rebuild",
                        "name": "resume_pipeline",
                        "input": {},
                    }
                ]
            )

        agent = self._agent(_FakeStack([SCREEN_CONTINUE]), post)
        with patch.object(agent, "_wait") as wait:
            session = self._recover(agent)

        self.assertEqual(session.source, "anthropic")
        self.assertEqual(len(bodies), 2)
        wait.assert_not_called()
        self.assertEqual([message["role"] for message in bodies[1]["messages"]], ["user"])
        rebuilt_text = bodies[1]["messages"][0]["content"][0]["text"]
        self.assertIn("previous API conversation was structurally rejected", rebuilt_text)
        for result in _tool_results(bodies[1]):
            _assert_valid_tool_result(self, result)

    def test_empty_anthropic_content_calls_again_without_poisoning_history(self):
        bodies = []

        def post(_url, *, headers, json, timeout):
            del headers, timeout
            bodies.append(json)
            if len(bodies) == 1:
                return _FakeResponse([])
            return _FakeResponse(
                [
                    {
                        "type": "tool_use",
                        "id": "resume-after-empty",
                        "name": "resume_pipeline",
                        "input": {},
                    }
                ]
            )

        agent = self._agent(_FakeStack([SCREEN_CONTINUE]), post)
        with patch.object(agent, "_wait") as wait:
            session = self._recover(agent)

        self.assertEqual(session.source, "anthropic")
        self.assertEqual(len(bodies), 2)
        wait.assert_not_called()
        self.assertEqual([message["role"] for message in bodies[1]["messages"]], ["user"])

    def test_malformed_top_level_content_requests_another_tool_turn(self):
        bodies = []

        def post(_url, *, headers, json, timeout):
            del headers, timeout
            bodies.append(json)
            if len(bodies) == 1:
                return _FakeResponse({"not": "a content block list"})
            return _FakeResponse(
                [
                    {
                        "type": "tool_use",
                        "id": "resume-after-malformed",
                        "name": "resume_pipeline",
                        "input": {},
                    }
                ]
            )

        agent = self._agent(_FakeStack([SCREEN_CONTINUE]), post)
        with patch.object(agent, "_wait") as wait:
            session = self._recover(agent)

        self.assertEqual(session.source, "anthropic")
        self.assertEqual(len(bodies), 2)
        wait.assert_not_called()

    def test_identical_wait_is_rejected_on_unchanged_screen(self):
        agent = self._agent(_FakeStack([SCREEN_CONTINUE]), lambda *_args, **_kwargs: None)
        observed = agent.observe(PhoneError("visual screen stuck"))
        with patch.object(agent, "_wait"):
            next_observation, _recipe = agent._execute_action(
                "wait",
                {"seconds": 2},
                PhoneError("visual screen stuck"),
            )
            with self.assertRaisesRegex(ValueError, "duplicate action"):
                agent._execute_action(
                    "wait",
                    {"seconds": 2},
                    PhoneError("visual screen stuck"),
                )


if __name__ == "__main__":
    unittest.main()
