from __future__ import annotations

import io
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from PIL import Image

from friend_poster.device.ssh_device import PhoneError
from friend_poster.apps import creator_controller as creator
from friend_poster.reddit.reddit_username import UsernameAvailabilityUnclear
from friend_poster.services import ui_recovery_agent as recovery


def latest_frame_id(body) -> str:
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
                        payload = __import__("json").loads(line)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(payload, dict) and payload.get("frame_id"):
                        return str(payload["frame_id"])
    raise AssertionError("request body has no frame_id")


def png_bytes(color=(240, 240, 240)) -> bytes:
    image = Image.new("RGB", (750, 1334), color)
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


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
  <XCUIElementTypeSecureTextField name="Password" label="Password" value="hunter2" visible="true" enabled="true" x="100" y="300" width="550" height="80" />
  <XCUIElementTypeButton name="Delete account" label="Delete account" visible="true" enabled="true" x="220" y="800" width="310" height="70" />
</XCUIElementTypeApplication>"""

SCREEN_USERNAME = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication visible="true" enabled="true" x="0" y="0" width="750" height="1334">
  <XCUIElementTypeStaticText name="u/cozypout24" label="u/cozypout24" visible="true" enabled="true" x="100" y="250" width="300" height="60" />
</XCUIElementTypeApplication>"""

SCREEN_CRANE_A0151 = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication visible="true" enabled="true" x="0" y="0" width="750" height="1334">
  <XCUIElementTypeStaticText label="RamavA Ann" visible="true" enabled="true" x="270" y="245" width="210" height="26" />
  <XCUIElementTypeButton label="Container" visible="true" enabled="true" x="236" y="405" width="205" height="42" />
  <XCUIElementTypeButton label="Default" visible="true" enabled="true" x="273" y="493" width="125" height="27" />
  <XCUIElementTypeButton label="a0151" visible="true" enabled="true" x="276" y="565" width="92" height="28" />
  <XCUIElementTypeButton label="New Container" visible="true" enabled="true" x="272" y="652" width="238" height="28" />
  <XCUIElementTypeButton label="Settings" visible="true" enabled="true" x="278" y="746" width="136" height="31" />
</XCUIElementTypeApplication>"""


class FakeStack:
    backend = "xxtouch"

    def __init__(self, screens=None):
        self.screens = list(screens or [SCREEN_CONTINUE, SCREEN_READY])
        self.index = 0
        self.taps = []
        self.swipes = []
        self.homes = 0
        self.launches = []
        self.long_taps = []

    def dump_xml(self):
        return self.screens[min(self.index, len(self.screens) - 1)]

    def screenshot_png(self):
        return png_bytes((240 - self.index * 30, 240, 240))

    def tap_xy(self, x, y):
        self.taps.append((x, y))
        self.index = min(self.index + 1, len(self.screens) - 1)

    def swipe(self, x1, y1, x2, y2):
        self.swipes.append((x1, y1, x2, y2))
        self.index = min(self.index + 1, len(self.screens) - 1)

    def long_tap(self, x, y, hold_ms=550):
        self.long_taps.append((x, y, hold_ms))
        self.index = min(self.index + 1, len(self.screens) - 1)

    def press_home(self):
        self.homes += 1

    def open_bundle(self, bundle):
        self.launches.append(bundle)


class FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.headers = {}

    def json(self):
        return self._data


class ToolSequence:
    def __init__(self):
        self.calls = 0

    def __call__(self, _url, *, headers, json, timeout):
        self.calls += 1
        self.last_headers = headers
        self.last_body = json
        if self.calls == 1:
            frame_id = latest_frame_id(json)
            return FakeResponse(
                {
                    "model": "fake-sonnet",
                    "usage": {"input_tokens": 10, "output_tokens": 4},
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "tap_element",
                            "input": {"frame_id": frame_id, "element_id": "e0001"},
                        }
                    ],
                }
            )
        return FakeResponse(
            {
                "model": "fake-sonnet",
                "usage": {"input_tokens": 8, "output_tokens": 2},
                "content": [
                    {"type": "tool_use", "id": "tool-2", "name": "resume_pipeline", "input": {}}
                ],
            }
        )


class ResumeThenTapSequence:
    def __init__(self):
        self.calls = 0

    def __call__(self, _url, *, headers, json, timeout):
        self.calls += 1
        frame_id = latest_frame_id(json)
        if self.calls == 1:
            tool = {"type": "tool_use", "id": "resume-1", "name": "resume_pipeline", "input": {}}
        elif self.calls == 2:
            tool = {
                "type": "tool_use", "id": "tap-1", "name": "tap_element",
                "input": {"frame_id": frame_id, "element_id": "e0001"},
            }
        else:
            tool = {"type": "tool_use", "id": "resume-2", "name": "resume_pipeline", "input": {}}
        return FakeResponse({"model": "fake-sonnet", "content": [tool]})


class UIRecoveryAgentTests(unittest.TestCase):
    def test_deterministic_username_failure_is_never_ai_recoverable(self):
        error = UsernameAvailabilityUnclear(
            "Reddit did not prove availability or rejection."
        )
        self.assertFalse(recovery.is_ui_recoverable_error(error))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "recipes.sqlite3"
        self.logs = []
        self.stop = threading.Event()

    def tearDown(self):
        self.temp.cleanup()

    def agent(self, stack, *, post=None):
        return recovery.UIRecoveryAgent(
            stack,
            object(),
            stop=self.stop,
            log=self.logs.append,
            store=recovery.RecipeStore(self.db_path),
            post=post,
            # Multi-turn protocol tests intentionally exercise conversation
            # mechanics. Production Creator supplies the shared one-call
            # attempt budget explicitly.
            attempt_budget=recovery.RecoveryAttemptBudget(max_calls=6),
        )

    def test_error_eligibility_excludes_non_visual_failures(self):
        self.assertFalse(
            recovery.is_ui_recoverable_error(
                PhoneError("Pending Customize tap did not map to one checked pill")
            )
        )
        self.assertTrue(
            recovery.is_ui_recoverable_error(
                recovery.UnknownVisualStateError("unknown visual state")
            )
        )
        self.assertFalse(
            recovery.is_ui_recoverable_error(
                PhoneError("Interests checkpoint could not be saved: Access is denied")
            )
        )
        self.assertFalse(recovery.is_ui_recoverable_error(RuntimeError("bug")))

    def test_redaction_masks_private_values_and_blurs_sensitive_node(self):
        text = recovery.redact_for_model(
            "email test@example.com IP 10.0.0.1 birthday 2000-01-02 code 123456"
        )
        self.assertNotIn("test@example.com", text)
        self.assertNotIn("10.0.0.1", text)
        self.assertNotIn("2000-01-02", text)
        stack = FakeStack([SCREEN_SENSITIVE])
        observed = self.agent(stack).observe(PhoneError("screen stuck"))
        self.assertTrue(all(not item["safe_to_tap"] for item in observed.public_nodes))
        self.assertTrue(
            any(
                "Password field" in str(item["label"])
                for item in observed.public_nodes
            )
        )
        self.assertTrue(
            all("hunter2" not in str(item["label"]) for item in observed.public_nodes)
        )
        with Image.open(io.BytesIO(observed.redacted_png)) as image:
            self.assertLess(sum(image.getpixel((375, 340))), 100)

    def test_reddit_username_is_removed_from_text_tree_and_screenshot(self):
        text = recovery.redact_for_model(
            "profile u/cozypout24, handle @cozypout24, username=cozypout24"
        )
        self.assertNotIn("cozypout24", text)
        observed = self.agent(FakeStack([SCREEN_USERNAME])).observe(
            PhoneError("profile screen stuck")
        )
        self.assertNotIn("cozypout24", observed.text_payload())
        self.assertIn("Username control", observed.public_nodes[0]["label"])
        self.assertFalse(observed.public_nodes[0]["safe_to_tap"])
        with Image.open(io.BytesIO(observed.redacted_png)) as image:
            self.assertLess(sum(image.getpixel((250, 280))), 100)

    def test_known_bare_slot_username_is_loaded_as_a_redaction_secret(self):
        secrets_path = Path(self.temp.name) / "secrets.json"
        slots_path = Path(self.temp.name) / "slots.json"
        secrets_path.write_text("{}", encoding="utf-8")
        slots_path.write_text(
            '[{"id":"a0220","username":"BareSlotName42"}]',
            encoding="utf-8",
        )

        with patch.object(recovery, "SECRETS_PATH", str(secrets_path)), \
                patch.object(recovery, "LOG_DIR", str(Path(self.temp.name))):
            private_values = recovery._secret_values()
            redacted = recovery.redact_for_model(
                "profiles bareslotname42 and BARESLOTNAME42", private_values
            )

        self.assertIn("BareSlotName42", private_values)
        self.assertNotIn("bareslotname42", redacted.casefold())
        self.assertEqual(redacted.count("<redacted>"), 2)

    def test_large_model_image_is_downscaled_and_bounded(self):
        noisy = Image.effect_noise((1125, 2436), 100).convert("RGB")
        source = io.BytesIO()
        noisy.save(source, "PNG")

        rendered = recovery._redact_image(source.getvalue(), [], [])

        self.assertLessEqual(len(rendered), recovery._MODEL_IMAGE_MAX_BYTES)
        with Image.open(io.BytesIO(rendered)) as image:
            self.assertLessEqual(image.width, recovery._MODEL_IMAGE_MAX_SIZE[0])
            self.assertLessEqual(image.height, recovery._MODEL_IMAGE_MAX_SIZE[1])

    def test_conversation_keeps_only_latest_screenshot(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "old"},
                    {"type": "image", "source": {"data": "old-image"}},
                ],
            },
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "observe-1"}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "observe-1",
                        "content": [
                            {"type": "text", "text": "new"},
                            {"type": "image", "source": {"data": "new-image"}},
                        ],
                    }
                ],
            },
        ]

        self.assertEqual(recovery._compact_prior_images(messages), 1)
        serialized = repr(messages)
        self.assertNotIn("old-image", serialized)
        self.assertIn("new-image", serialized)
        self.assertEqual(serialized.count("'type': 'image'"), 1)

    def test_sensitive_and_destructive_taps_are_rejected(self):
        stack = FakeStack([SCREEN_SENSITIVE])
        agent = self.agent(stack)
        observed = agent.observe(PhoneError("screen stuck"))
        with self.assertRaisesRegex(ValueError, "sensitive"):
            agent._execute_action(
                "tap_element",
                {"frame_id": observed.frame_id, "element_id": "e0001"},
                PhoneError("screen stuck"),
            )
        self.assertEqual(stack.taps, [])

    def test_a0151_crane_picker_is_classified_and_settings_is_safe(self):
        stack = FakeStack([SCREEN_CRANE_A0151])
        agent = self.agent(stack)
        observed = agent.observe(PhoneError("container picker stuck"))

        self.assertEqual(observed.screen_class, "crane_container_picker")
        settings = next(
            item for item in observed.public_nodes
            if str(item["label"]).startswith("Settings")
        )
        self.assertTrue(settings["safe_to_tap"])
        new_container = next(
            item for item in observed.public_nodes
            if str(item["label"]).startswith("New Container")
        )
        self.assertFalse(new_container["safe_to_tap"])

    def test_scoped_verified_workflow_tool_runs_local_handler(self):
        calls = []
        stack = FakeStack([SCREEN_CRANE_A0151, SCREEN_READY])

        def delete_and_verify(payload):
            calls.append(payload)
            stack.index = 1
            return "deleted"

        agent = recovery.UIRecoveryAgent(
            stack,
            object(),
            stop=self.stop,
            log=self.logs.append,
            store=recovery.RecipeStore(self.db_path),
            workflow_scope="container_cleanup",
            workflow_tools={
                "verified_delete_only_container": recovery.RecoveryWorkflowTool(
                    description="Delete and verify the sole user container.",
                    handler=delete_and_verify,
                )
            },
        )
        initial = agent.observe(PhoneError("container cleanup stuck"))
        observed, recipe = agent._execute_action(
            "verified_delete_only_container",
            {"frame_id": initial.frame_id},
            PhoneError("container cleanup stuck"),
        )

        self.assertEqual(calls, [{}])
        self.assertEqual(observed.screen_class, "reddit")
        self.assertEqual(
            recipe,
            {
                "tool": "workflow",
                "name": "verified_delete_only_container",
                "input": {},
            },
        )

    def test_container_selection_scope_exposes_only_bound_verified_tool(self):
        calls = []
        stack = FakeStack([SCREEN_CRANE_A0151, SCREEN_READY])

        def select_and_verify(payload):
            calls.append(payload)
            stack.index = 1
            return "requested container selected and Reddit proven"

        agent = recovery.UIRecoveryAgent(
            stack,
            object(),
            stop=self.stop,
            log=self.logs.append,
            store=recovery.RecipeStore(self.db_path),
            workflow_scope="container_selection",
            workflow_tools={
                "verified_select_existing_container": recovery.RecoveryWorkflowTool(
                    description="Select the controller-bound container.",
                    handler=select_and_verify,
                )
            },
        )
        initial = agent.observe(PhoneError("container selection failed"))
        observed, recipe = agent._execute_action(
            "verified_select_existing_container",
            {"frame_id": initial.frame_id},
            PhoneError("container selection failed"),
        )

        self.assertEqual(calls, [{}])
        self.assertEqual(observed.screen_class, "reddit")
        self.assertEqual(recipe["name"], "verified_select_existing_container")
        definitions = {item["name"] for item in agent._tool_definitions()}
        self.assertIn("verified_select_existing_container", definitions)

    def test_unexposed_workflow_tool_is_rejected(self):
        agent = self.agent(FakeStack([SCREEN_CRANE_A0151]))
        agent.observe(PhoneError("container cleanup stuck"))
        with self.assertRaisesRegex(ValueError, "unsupported recovery tool"):
            agent._execute_action(
                "verified_delete_only_container",
                {},
                PhoneError("container cleanup stuck"),
            )

    def test_destructive_workflow_tool_is_rejected_outside_its_scope(self):
        with self.assertRaisesRegex(ValueError, "not authorized"):
            recovery.UIRecoveryAgent(
                FakeStack([SCREEN_CRANE_A0151]),
                object(),
                stop=self.stop,
                log=self.logs.append,
                store=recovery.RecipeStore(self.db_path),
                workflow_scope="creation_pipeline",
                workflow_tools={
                    "verified_delete_only_container": recovery.RecoveryWorkflowTool(
                        description="Delete container.",
                        handler=lambda _payload: "deleted",
                    )
                },
            )

    def test_discard_tool_returns_only_a_supervisor_directive(self):
        stack = FakeStack([SCREEN_CONTINUE])
        agent = recovery.UIRecoveryAgent(
            stack,
            object(),
            stop=self.stop,
            log=self.logs.append,
            store=recovery.RecipeStore(self.db_path),
            workflow_scope="creation_pipeline",
            workflow_tools={
                "discard_restart_current_attempt": recovery.RecoveryWorkflowTool(
                    description="Request deterministic full reset.",
                    handler=lambda _payload: recovery.RecoveryDirective(
                        "discard_restart_current_attempt", "verified by owner"
                    ),
                    cacheable=False,
                )
            },
        )
        initial = agent.observe(PhoneError("stuck"))
        _observed, recipe = agent._execute_action(
            "discard_restart_current_attempt",
            {"frame_id": initial.frame_id},
            PhoneError("stuck"),
        )

        self.assertIsNone(recipe)
        self.assertEqual(
            agent._requested_directive.name, "discard_restart_current_attempt"
        )

    def test_workflow_directive_is_not_published_when_fresh_observation_fails(self):
        class ObservationFailsAfterInitialFrame(FakeStack):
            def __init__(self):
                super().__init__([SCREEN_CONTINUE])
                self.dumps = 0

            def dump_xml(self):
                self.dumps += 1
                if self.dumps > 1:
                    raise PhoneError("fresh UI dump unavailable")
                return super().dump_xml()

        stack = ObservationFailsAfterInitialFrame()
        agent = recovery.UIRecoveryAgent(
            stack,
            object(),
            stop=self.stop,
            log=self.logs.append,
            store=recovery.RecipeStore(self.db_path),
            workflow_scope="container_selection",
            workflow_tools={
                "verified_select_existing_container": recovery.RecoveryWorkflowTool(
                    description="Select the controller-bound container.",
                    handler=lambda _payload: recovery.RecoveryDirective(
                        "resume_pipeline", "container selection verified"
                    ),
                )
            },
        )
        initial = agent.observe(PhoneError("container selection stuck"))

        with self.assertRaisesRegex(PhoneError, "fresh UI dump unavailable"):
            agent._execute_action(
                "verified_select_existing_container",
                {"frame_id": initial.frame_id},
                PhoneError("container selection stuck"),
            )

        self.assertIsNone(agent._requested_directive)

    def test_diverged_cached_workflow_replay_clears_directive(self):
        stack = FakeStack([SCREEN_CONTINUE, SCREEN_READY])

        def select_container(_payload):
            stack.index = 1
            return recovery.RecoveryDirective(
                "resume_pipeline", "container selection verified"
            )

        agent = recovery.UIRecoveryAgent(
            stack,
            object(),
            stop=self.stop,
            log=self.logs.append,
            store=recovery.RecipeStore(self.db_path),
            workflow_scope="container_selection",
            workflow_tools={
                "verified_select_existing_container": recovery.RecoveryWorkflowTool(
                    description="Select the controller-bound container.",
                    handler=select_container,
                )
            },
        )
        initial = agent.observe(PhoneError("container selection stuck"))
        recipe = {
            "status": "candidate",
            "model": "fake-sonnet",
            "actions": [
                {
                    "tool": "workflow",
                    "name": "verified_select_existing_container",
                    "input": {},
                }
            ],
            "expected": ["not-the-observed-fingerprint"],
        }

        with self.assertRaisesRegex(recovery.RecipeDiverged, "unexpected screen"):
            agent._replay_recipe(
                recipe, initial, PhoneError("container selection stuck")
            )

        self.assertIsNone(agent._requested_directive)

    def test_store_promotes_after_two_verified_successes_and_quarantines(self):
        store = recovery.RecipeStore(self.db_path)
        first = recovery.RecoverySession(
            incident_fingerprint="abc",
            final_fingerprint="def",
            actions=[{"tool": "wait", "seconds": 1}],
            expected_fingerprints=["def"],
            model="fake",
            source="anthropic",
        )
        self.assertEqual(store.record_success(first), "candidate")
        cached = recovery.RecoverySession(**{**first.__dict__, "source": "cache"})
        self.assertEqual(store.record_success(cached), "promoted")
        self.assertEqual(store.get("abc")["status"], "promoted")
        store.quarantine("abc", "unexpected screen")
        self.assertIsNone(store.get("abc"))

    def test_malformed_cached_recipe_is_quarantined_and_ignored(self):
        store = recovery.RecipeStore(self.db_path)
        conn = store._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO recipes (
                        fingerprint,schema_version,status,actions_json,
                        expected_json,success_count,failure_count,model,
                        last_error,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "broken", recovery.SCHEMA_VERSION, "candidate",
                        "{not-json", "[]", 1, 0, "fake-sonnet", "", 1.0, 1.0,
                    ),
                )
        finally:
            conn.close()

        self.assertIsNone(store.get("broken"))
        conn = store._connect()
        try:
            row = conn.execute(
                "SELECT status,failure_count FROM recipes WHERE fingerprint=?",
                ("broken",),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "quarantined")
        self.assertEqual(row["failure_count"], 1)

    def test_anthropic_tool_turn_becomes_candidate_then_cache_promotes(self):
        sequence = ToolSequence()
        with patch.object(recovery, "load_anthropic_settings", return_value=("sk-ant-api-test", "fake-sonnet")):
            stack = FakeStack()
            agent = self.agent(stack, post=sequence)
            session = agent.recover(PhoneError("ambiguous selected state"))
            self.assertEqual(len(session.actions), 1)
            self.assertEqual(stack.taps, [(375, 1085)])
            self.assertEqual(sequence.last_headers["Authorization"], "Bearer sk-ant-api-test")
            self.assertNotIn("x-api-key", sequence.last_headers)
            agent.confirm_success(session)
            self.assertEqual(recovery.RecipeStore(self.db_path).get(session.incident_fingerprint)["status"], "candidate")

            replay_stack = FakeStack()
            replay_agent = self.agent(
                replay_stack,
                post=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("cache replay must not call Anthropic")
                ),
            )
            replay = replay_agent.recover(PhoneError("ambiguous selected state"))
            self.assertEqual(replay.source, "cache")
            replay_agent.confirm_success(replay)
            self.assertEqual(recovery.RecipeStore(self.db_path).get(session.incident_fingerprint)["status"], "promoted")

    def test_duplicate_action_on_unchanged_screen_is_blocked(self):
        stack = FakeStack([SCREEN_CONTINUE])
        agent = self.agent(stack)
        observed = agent.observe(PhoneError("stuck"))
        payload = {"frame_id": observed.frame_id, "element_id": "e0001"}
        next_observation, _ = agent._execute_action("tap_element", payload, PhoneError("stuck"))
        # The one-screen fake remains unchanged, so a semantically identical
        # action on the new frame must be rejected by fingerprint+action.
        with self.assertRaisesRegex(ValueError, "duplicate"):
            agent._execute_action(
                "tap_element",
                {"frame_id": next_observation.frame_id, "element_id": "e0001"},
                PhoneError("stuck"),
            )

    def test_point_tap_cannot_repeat_element_tap_on_same_semantic_control(self):
        stack = FakeStack([SCREEN_CONTINUE])
        agent = self.agent(stack)
        observed = agent.observe(PhoneError("stuck"))
        next_observation, _ = agent._execute_action(
            "tap_element",
            {"frame_id": observed.frame_id, "element_id": "e0001"},
            PhoneError("stuck"),
        )

        with self.assertRaisesRegex(ValueError, "duplicate"):
            agent._execute_action(
                "tap_point",
                {"frame_id": next_observation.frame_id, "x": 375, "y": 1085},
                PhoneError("stuck"),
            )

    def test_nested_label_and_button_share_one_semantic_tap_identity(self):
        nested_screen = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication visible="true" enabled="true" x="0" y="0" width="750" height="1334">
  <XCUIElementTypeButton name="Continue" label="Continue" visible="true" enabled="true" x="250" y="1040" width="250" height="90">
    <XCUIElementTypeStaticText name="Continue" label="Continue" visible="true" enabled="true" x="320" y="1065" width="110" height="35" />
  </XCUIElementTypeButton>
</XCUIElementTypeApplication>"""
        stack = FakeStack([nested_screen])
        agent = self.agent(stack)
        observed = agent.observe(PhoneError("stuck"))
        label_id = next(
            element_id
            for element_id, node in observed.elements.items()
            if "statictext" in str(node.ntype).casefold()
        )
        next_observation, _ = agent._execute_action(
            "tap_element",
            {"frame_id": observed.frame_id, "element_id": label_id},
            PhoneError("stuck"),
        )

        with self.assertRaisesRegex(ValueError, "duplicate"):
            agent._execute_action(
                "tap_point",
                {"frame_id": next_observation.frame_id, "x": 375, "y": 1082},
                PhoneError("stuck"),
            )

    def test_nested_label_and_button_share_one_long_press_identity(self):
        nested_screen = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication visible="true" enabled="true" x="0" y="0" width="750" height="1334">
  <XCUIElementTypeButton name="Continue" label="Continue" visible="true" enabled="true" x="250" y="1040" width="250" height="90">
    <XCUIElementTypeStaticText name="Continue" label="Continue" visible="true" enabled="true" x="320" y="1065" width="110" height="35" />
  </XCUIElementTypeButton>
</XCUIElementTypeApplication>"""
        stack = FakeStack([nested_screen])
        agent = self.agent(stack)
        observed = agent.observe(PhoneError("stuck"))
        label_id = next(
            element_id
            for element_id, node in observed.elements.items()
            if "statictext" in str(node.ntype).casefold()
        )
        button_id = next(
            element_id
            for element_id, node in observed.elements.items()
            if "button" in str(node.ntype).casefold()
        )
        next_observation, _ = agent._execute_action(
            "long_press_element",
            {
                "frame_id": observed.frame_id,
                "element_id": label_id,
                "hold_ms": 650,
            },
            PhoneError("stuck"),
        )

        with self.assertRaisesRegex(ValueError, "duplicate"):
            agent._execute_action(
                "long_press_element",
                {
                    "frame_id": next_observation.frame_id,
                    "element_id": button_id,
                    "hold_ms": 1200,
                },
                PhoneError("stuck"),
            )

    def test_long_press_identity_survives_reconnect_point_tool_and_nested_node(self):
        nested_screen = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication visible="true" enabled="true" x="0" y="0" width="750" height="1334">
  <XCUIElementTypeButton name="Reddit" label="Reddit" visible="true" enabled="true" x="230" y="740" width="220" height="130">
    <XCUIElementTypeStaticText name="Reddit" label="Reddit" visible="true" enabled="true" x="285" y="820" width="110" height="35" />
  </XCUIElementTypeButton>
</XCUIElementTypeApplication>"""
        reordered_screen = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication visible="true" enabled="true" x="0" y="0" width="750" height="1334">
  <XCUIElementTypeStaticText name="Reddit" label="Reddit" visible="true" enabled="true" x="285" y="820" width="110" height="35" />
  <XCUIElementTypeButton name="Reddit" label="Reddit" visible="true" enabled="true" x="230" y="740" width="220" height="130" />
</XCUIElementTypeApplication>"""
        chain = recovery.RecoveryChain()

        def first_callsite_error():
            try:
                raise PhoneError("stuck")
            except PhoneError as exc:
                return exc

        def reconnect_callsite_error():
            try:
                raise PhoneError("stuck")
            except PhoneError as exc:
                return exc

        first_stack = FakeStack([nested_screen])
        first = recovery.UIRecoveryAgent(
            first_stack,
            object(),
            stop=self.stop,
            log=self.logs.append,
            store=recovery.RecipeStore(self.db_path),
            chain=chain,
        )
        initial_error = first_callsite_error()
        first_observation = first.observe(initial_error)
        label_id = next(
            element_id
            for element_id, node in first_observation.elements.items()
            if "statictext" in str(node.ntype).casefold()
        )
        first._execute_action(
            "long_press_element",
            {
                "frame_id": first_observation.frame_id,
                "element_id": label_id,
                "hold_ms": 500,
            },
            initial_error,
        )

        reconnect_cases = (
            ("long_press_point", 650, reconnect_callsite_error()),
            ("long_press_element", 800, RuntimeError("different wrapper")),
            ("long_press_point", 1200, PhoneError("different message")),
        )
        reconnect_stacks = []
        for tool, hold_ms, reconnect_error in reconnect_cases:
            second_stack = FakeStack([reordered_screen])
            reconnect_stacks.append(second_stack)
            second = recovery.UIRecoveryAgent(
                second_stack,
                object(),
                stop=self.stop,
                log=self.logs.append,
                store=recovery.RecipeStore(self.db_path),
                chain=chain,
            )
            self.assertIs(first.attempt_budget, second.attempt_budget)
            second_observation = second.observe(reconnect_error)
            self.assertNotEqual(
                first_observation.frame_id, second_observation.frame_id
            )
            self.assertNotEqual(
                first_observation.semantic_key, second_observation.semantic_key
            )
            self.assertEqual(
                first_observation.screen_semantic_key,
                second_observation.screen_semantic_key,
            )
            if tool == "long_press_element":
                button_id = next(
                    element_id
                    for element_id, node in second_observation.elements.items()
                    if "button" in str(node.ntype).casefold()
                )
                payload = {
                    "frame_id": second_observation.frame_id,
                    "element_id": button_id,
                    "hold_ms": hold_ms,
                }
            else:
                payload = {
                    "frame_id": second_observation.frame_id,
                    "x": 340,
                    "y": 837,
                    "hold_ms": hold_ms,
                }
            with self.subTest(tool=tool, hold_ms=hold_ms), self.assertRaisesRegex(
                ValueError, "duplicate"
            ):
                second._execute_action(tool, payload, reconnect_error)

        self.assertEqual(first_stack.long_taps, [(340, 837, 500)])
        self.assertTrue(all(stack.long_taps == [] for stack in reconnect_stacks))

    def test_element_actions_reject_destructive_nested_child(self):
        contained_delete = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication visible="true" enabled="true" x="0" y="0" width="750" height="1334">
  <XCUIElementTypeButton name="Actions" label="Actions" visible="true" enabled="true" x="250" y="700" width="250" height="100">
    <XCUIElementTypeStaticText name="Delete" label="Delete" visible="true" enabled="true" x="260" y="710" width="70" height="30" />
  </XCUIElementTypeButton>
</XCUIElementTypeApplication>"""
        overlapping_delete = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication visible="true" enabled="true" x="0" y="0" width="750" height="1334">
  <XCUIElementTypeButton name="Actions" label="Actions" visible="true" enabled="true" x="250" y="700" width="250" height="100">
    <XCUIElementTypeStaticText name="Remove" label="Remove" visible="true" enabled="true" x="460" y="715" width="70" height="30" />
  </XCUIElementTypeButton>
</XCUIElementTypeApplication>"""

        for layout, screen in (
            ("contained-off-centre", contained_delete),
            ("meaningful-overlap", overlapping_delete),
        ):
            for tool in (
                "tap_element",
                "long_press_element",
                "tap_point",
                "long_press_point",
            ):
                stack = FakeStack([screen])
                agent = self.agent(stack)
                observed = agent.observe(PhoneError("stuck"))
                button_id = next(
                    element_id
                    for element_id, node in observed.elements.items()
                    if "button" in str(node.ntype).casefold()
                )
                button_public = next(
                    item for item in observed.public_nodes
                    if item["id"] == button_id
                )
                self.assertFalse(button_public["safe_to_tap"])
                if tool.endswith("element"):
                    payload = {
                        "frame_id": observed.frame_id,
                        "element_id": button_id,
                    }
                else:
                    payload = {
                        "frame_id": observed.frame_id,
                        "x": 375,
                        "y": 750,
                    }
                if tool.startswith("long_press"):
                    payload["hold_ms"] = 900

                with self.subTest(layout=layout, tool=tool), self.assertRaisesRegex(
                    ValueError, "sensitive|destructive"
                ):
                    agent._execute_action(tool, payload, PhoneError("stuck"))
                self.assertEqual(stack.taps, [])
                self.assertEqual(stack.long_taps, [])

    def test_free_point_tap_cannot_loop_by_moving_one_pixel(self):
        stack = FakeStack([SCREEN_READY])
        agent = self.agent(stack)
        observed = agent.observe(PhoneError("stuck"))
        next_observation, _ = agent._execute_action(
            "tap_point",
            {"frame_id": observed.frame_id, "x": 40, "y": 200},
            PhoneError("stuck"),
        )

        with self.assertRaisesRegex(ValueError, "duplicate"):
            agent._execute_action(
                "tap_point",
                {"frame_id": next_observation.frame_id, "x": 41, "y": 200},
                PhoneError("stuck"),
            )

    def test_semantic_duplicate_guard_survives_reconnect_and_node_reordering(self):
        first_screen = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication visible="true" enabled="true" x="0" y="0" width="750" height="1334">
  <XCUIElementTypeButton label="Continue" visible="true" enabled="true" x="260" y="1050" width="230" height="70" />
  <XCUIElementTypeButton label="Help" visible="true" enabled="true" x="40" y="120" width="120" height="60" />
</XCUIElementTypeApplication>"""
        reordered_screen = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication visible="true" enabled="true" x="0" y="0" width="750" height="1334">
  <XCUIElementTypeButton label="Help" visible="true" enabled="true" x="40" y="120" width="120" height="60" />
  <XCUIElementTypeButton label="Continue" visible="true" enabled="true" x="260" y="1050" width="230" height="70" />
</XCUIElementTypeApplication>"""
        chain = recovery.RecoveryChain()
        first = recovery.UIRecoveryAgent(
            FakeStack([first_screen]), object(), stop=self.stop,
            log=self.logs.append, store=recovery.RecipeStore(self.db_path),
            chain=chain,
        )
        observed = first.observe(PhoneError("stuck"))
        continue_id = next(
            key for key, node in observed.elements.items()
            if "Continue" in node.labels()
        )
        first._execute_action(
            "tap_element",
            {"frame_id": observed.frame_id, "element_id": continue_id},
            PhoneError("stuck"),
        )

        second = recovery.UIRecoveryAgent(
            FakeStack([reordered_screen]), object(), stop=self.stop,
            log=self.logs.append, store=recovery.RecipeStore(self.db_path),
            chain=chain,
        )
        reordered = second.observe(PhoneError("stuck"))
        reordered_continue_id = next(
            key for key, node in reordered.elements.items()
            if "Continue" in node.labels()
        )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            second._execute_action(
                "tap_element",
                {
                    "frame_id": reordered.frame_id,
                    "element_id": reordered_continue_id,
                },
                PhoneError("stuck"),
            )

    def test_resume_guard_rejects_changed_screen_until_local_goal_is_proven(self):
        calls = []

        def post(_url, *, headers, json, timeout):
            del headers, timeout
            calls.append(json)
            frame_id = latest_frame_id(json)
            if len(calls) == 1:
                tool = {
                    "type": "tool_use", "id": "open-picker",
                    "name": "tap_element",
                    "input": {"frame_id": frame_id, "element_id": "e0001"},
                }
            elif len(calls) == 2:
                tool = {
                    "type": "tool_use", "id": "early-resume",
                    "name": "resume_pipeline", "input": {},
                }
            elif len(calls) == 3:
                tool = {
                    "type": "tool_use", "id": "select-slot",
                    "name": "tap_element",
                    "input": {"frame_id": frame_id, "element_id": "e0004"},
                }
            else:
                tool = {
                    "type": "tool_use", "id": "verified-resume",
                    "name": "resume_pipeline", "input": {},
                }
            return FakeResponse({"model": "fake-sonnet", "content": [tool]})

        stack = FakeStack([SCREEN_CONTINUE, SCREEN_CRANE_A0151, SCREEN_READY])
        agent = recovery.UIRecoveryAgent(
            stack, object(), stop=self.stop, log=self.logs.append,
            store=recovery.RecipeStore(self.db_path), post=post,
            resume_guard=lambda observation: (
                observation.screen_class == "reddit",
                "Reddit is not yet locally proven in the foreground",
            ),
            attempt_budget=recovery.RecoveryAttemptBudget(max_calls=6),
        )
        with patch.object(
            recovery,
            "load_anthropic_settings",
            return_value=("sk-ant-api-test", "fake-sonnet"),
        ):
            session = agent.recover(PhoneError("container selection failed"))

        self.assertEqual(len(calls), 4)
        self.assertEqual(session.final_semantic_key, agent.current.semantic_key)
        self.assertEqual(agent.current.screen_class, "reddit")
        self.assertTrue(any("postcondition" in entry for entry in self.logs))

    def test_first_run_workflow_resume_directive_must_pass_fresh_guard(self):
        calls = []

        def post(_url, *, headers, json, timeout):
            del headers, timeout
            calls.append(json)
            frame_id = latest_frame_id(json)
            if len(calls) == 1:
                tool = {
                    "type": "tool_use",
                    "id": "verified-select",
                    "name": "verified_select_existing_container",
                    "input": {"frame_id": frame_id},
                }
            elif len(calls) == 2:
                rejected = json["messages"][-1]["content"][0]
                self.assertTrue(rejected["is_error"])
                self.assertIsInstance(rejected["content"], str)
                self.assertIn("postcondition", rejected["content"])
                tool = {
                    "type": "tool_use",
                    "id": "advance",
                    "name": "tap_element",
                    "input": {"frame_id": frame_id, "element_id": "e0002"},
                }
            else:
                tool = {
                    "type": "tool_use",
                    "id": "resume-after-proof",
                    "name": "resume_pipeline",
                    "input": {},
                }
            return FakeResponse({"model": "fake-sonnet", "content": [tool]})

        agent = recovery.UIRecoveryAgent(
            FakeStack([SCREEN_CRANE_A0151, SCREEN_READY]),
            object(),
            stop=self.stop,
            log=self.logs.append,
            store=recovery.RecipeStore(self.db_path),
            post=post,
            workflow_scope="container_selection",
            workflow_tools={
                "verified_select_existing_container": recovery.RecoveryWorkflowTool(
                    description="Select the controller-bound container.",
                    handler=lambda _payload: recovery.RecoveryDirective(
                        "resume_pipeline", "selector reported success"
                    ),
                )
            },
            resume_guard=lambda observation: (
                observation.screen_class == "reddit",
                "Reddit is not yet locally proven in the foreground",
            ),
            attempt_budget=recovery.RecoveryAttemptBudget(max_calls=6),
        )
        with patch.object(
            recovery,
            "load_anthropic_settings",
            return_value=("sk-ant-api-test", "fake-sonnet"),
        ):
            session = agent.recover(
                PhoneError("container selection failed")
            )

        self.assertEqual(len(calls), 3)
        self.assertEqual(session.directive, "")
        self.assertEqual(agent.current.screen_class, "reddit")
        self.assertTrue(
            any("scoped resume directive was rejected" in line for line in self.logs)
        )

    def test_repeated_resume_is_rejected_on_same_semantic_screen(self):
        calls = []

        def post(_url, *, headers, json, timeout):
            del headers, timeout
            calls.append(json)
            frame_id = latest_frame_id(json)
            if len(calls) <= 2:
                tool = {
                    "type": "tool_use",
                    "id": f"early-resume-{len(calls)}",
                    "name": "resume_pipeline",
                    "input": {},
                }
            elif len(calls) == 3:
                tool = {
                    "type": "tool_use",
                    "id": "advance-screen",
                    "name": "tap_element",
                    "input": {"frame_id": frame_id, "element_id": "e0001"},
                }
            else:
                tool = {
                    "type": "tool_use",
                    "id": "verified-resume",
                    "name": "resume_pipeline",
                    "input": {},
                }
            return FakeResponse({"model": "fake-sonnet", "content": [tool]})

        agent = recovery.UIRecoveryAgent(
            FakeStack([SCREEN_CONTINUE, SCREEN_READY]),
            object(),
            stop=self.stop,
            log=self.logs.append,
            store=recovery.RecipeStore(self.db_path),
            post=post,
            resume_guard=lambda observation: (
                observation.screen_class == "reddit",
                "Reddit is not yet locally proven in the foreground",
            ),
            attempt_budget=recovery.RecoveryAttemptBudget(max_calls=6),
        )
        with patch.object(
            recovery,
            "load_anthropic_settings",
            return_value=("sk-ant-api-test", "fake-sonnet"),
        ):
            session = agent.recover(PhoneError("container selection failed"))

        self.assertEqual(len(calls), 4)
        self.assertEqual(len(session.actions), 1)
        self.assertTrue(any("already rejected" in entry for entry in self.logs))

    def test_recovery_chain_stops_after_bounded_api_calls(self):
        calls = []

        def post(_url, *, headers, json, timeout):
            del headers, json, timeout
            calls.append(True)
            return FakeResponse(
                {
                    "model": "fake-sonnet",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"observe-{len(calls)}",
                            "name": "observe",
                            "input": {},
                        }
                    ],
                }
            )

        chain = recovery.RecoveryChain()
        budget = recovery.RecoveryAttemptBudget(max_calls=2)
        agent = recovery.UIRecoveryAgent(
            FakeStack([SCREEN_CONTINUE]),
            object(),
            stop=self.stop,
            log=self.logs.append,
            store=recovery.RecipeStore(self.db_path),
            post=post,
            chain=chain,
            attempt_budget=budget,
        )
        with patch.object(
            recovery,
            "load_anthropic_settings",
            return_value=("sk-ant-api-test", "fake-sonnet"),
        ):
            with self.assertRaisesRegex(
                recovery.RecoveryBudgetExhausted, "API call limit reached"
            ):
                agent.recover(PhoneError("stuck"))

        self.assertEqual(len(calls), 2)
        self.assertEqual(chain.api_calls, 2)
        self.assertEqual(budget.used_calls, 2)
        self.assertTrue(any("before another paid request" in line for line in self.logs))

    def test_attempt_budget_is_shared_across_agents_and_recovery_phases(self):
        first_calls = []
        second_calls = []

        def first_post(_url, *, headers, json, timeout):
            del headers, timeout
            first_calls.append(True)
            return FakeResponse(
                {
                    "model": "fake-sonnet",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "resume-first-phase",
                            "name": "resume_pipeline",
                            "input": {},
                        }
                    ],
                }
            )

        def second_post(_url, *, headers, json, timeout):
            del headers, json, timeout
            second_calls.append(True)
            return FakeResponse(
                {
                    "model": "fake-sonnet",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"observe-second-phase-{len(second_calls)}",
                            "name": "observe",
                            "input": {},
                        }
                    ],
                }
            )

        budget = recovery.RecoveryAttemptBudget(max_calls=2)
        first = recovery.UIRecoveryAgent(
            FakeStack([SCREEN_CONTINUE]),
            object(),
            stop=self.stop,
            log=self.logs.append,
            store=recovery.RecipeStore(self.db_path),
            post=first_post,
            chain=recovery.RecoveryChain(),
            attempt_budget=budget,
        )
        second = recovery.UIRecoveryAgent(
            FakeStack([SCREEN_CONTINUE]),
            object(),
            stop=self.stop,
            log=self.logs.append,
            store=recovery.RecipeStore(self.db_path),
            post=second_post,
            chain=recovery.RecoveryChain(),
            attempt_budget=budget,
        )
        self.assertIs(first.attempt_budget, budget)
        self.assertIs(second.attempt_budget, budget)

        with patch.object(
            recovery,
            "load_anthropic_settings",
            return_value=("sk-ant-api-test", "fake-sonnet"),
        ):
            first.recover(PhoneError("selection phase stuck"))
            with self.assertRaises(recovery.RecoveryBudgetExhausted):
                second.recover(PhoneError("cleanup phase stuck"))

        self.assertEqual(len(first_calls), 1)
        self.assertEqual(len(second_calls), 1)
        self.assertEqual(budget.used_calls, 2)
        self.assertEqual(budget.remaining_calls, 0)

    def test_attempt_budget_counts_each_http_retry(self):
        calls = []

        def failing_post(_url, *, headers, json, timeout):
            del headers, json, timeout
            calls.append(True)
            raise RuntimeError("temporary transport failure")

        budget = recovery.RecoveryAttemptBudget(max_calls=2)
        agent = recovery.UIRecoveryAgent(
            FakeStack([SCREEN_CONTINUE]),
            object(),
            stop=self.stop,
            log=self.logs.append,
            store=recovery.RecipeStore(self.db_path),
            post=failing_post,
            attempt_budget=budget,
        )
        messages = [{"role": "user", "content": "Recover navigation."}]
        with (
            patch.object(
                recovery,
                "load_anthropic_settings",
                return_value=("sk-ant-api-test", "fake-sonnet"),
            ),
            patch.object(agent, "_wait", return_value=None),
        ):
            with self.assertRaises(recovery.RecoveryBudgetExhausted):
                agent._api_call(messages, 1)

        self.assertEqual(len(calls), 2)
        self.assertEqual(budget.used_calls, 2)

    def test_different_fingerprint_never_auto_confirms_previous_session(self):
        store = recovery.RecipeStore(self.db_path)
        previous = recovery.RecoverySession(
            incident_fingerprint="old-fingerprint",
            final_fingerprint="old-final",
            incident_semantic_key="different-semantic-incident",
            actions=[{"tool": "wait", "seconds": 1}],
            expected_fingerprints=["old-final"],
        )

        def post(_url, *, headers, json, timeout):
            del headers, json, timeout
            return FakeResponse(
                {
                    "model": "fake-sonnet",
                    "content": [
                        {
                            "type": "tool_use", "id": "resume",
                            "name": "resume_pipeline", "input": {},
                        }
                    ],
                }
            )

        agent = recovery.UIRecoveryAgent(
            FakeStack([SCREEN_READY]), object(), stop=self.stop,
            log=self.logs.append, store=store, post=post,
        )
        with patch.object(store, "record_success", wraps=store.record_success) as recorded, \
                patch.object(
                    recovery,
                    "load_anthropic_settings",
                    return_value=("sk-ant-api-test", "fake-sonnet"),
                ):
            agent.recover(PhoneError("later visual failure"), previous=previous)

        recorded.assert_not_called()
        self.assertTrue(any("remains pending" in entry for entry in self.logs))

    def test_repeated_incident_rejects_resume_until_screen_changes(self):
        stack = FakeStack([SCREEN_CONTINUE, SCREEN_READY])
        sequence = ResumeThenTapSequence()
        chain = recovery.RecoveryChain()
        agent = recovery.UIRecoveryAgent(
            stack, object(), stop=self.stop, log=self.logs.append,
            store=recovery.RecipeStore(self.db_path), post=sequence,
            chain=chain,
            attempt_budget=recovery.RecoveryAttemptBudget(max_calls=6),
        )
        initial = agent.observe(PhoneError("stuck"))
        previous = recovery.RecoverySession(
            incident_fingerprint=initial.fingerprint,
            final_fingerprint=initial.fingerprint,
            incident_semantic_key=initial.semantic_key,
            final_semantic_key=initial.semantic_key,
        )
        chain.pending_sessions.append(previous)
        with patch.object(
            recovery,
            "load_anthropic_settings",
            return_value=("sk-ant-api-test", "fake-sonnet"),
        ):
            session = agent.recover(PhoneError("stuck"), previous=previous)

        self.assertEqual(sequence.calls, 3)
        self.assertEqual(stack.taps, [(375, 1085)])
        self.assertEqual(len(session.actions), 1)
        self.assertNotIn(previous, chain.pending_sessions)

    def test_stop_interrupts_a_blocked_http_call_without_waiting_for_timeout(self):
        entered = threading.Event()
        release = threading.Event()

        def blocked_post(*_args, **_kwargs):
            entered.set()
            release.wait(5)
            return FakeResponse({"content": []})

        agent = self.agent(FakeStack(), post=blocked_post)
        errors = []

        def call():
            try:
                agent._post_stop_aware({}, {})
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=call)
        thread.start()
        self.assertTrue(entered.wait(1))
        self.stop.set()
        thread.join(1)
        release.set()
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], recovery.RecoveryStopped)


class CreatorRecoveryIntegrationTests(unittest.TestCase):
    class QuietWatch:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    def test_generic_ui_error_recovers_then_replays_from_checkpoint(self):
        from test_bulk_preserve_skip import make_app

        app = make_app(None, 1)
        app.status = mock.Mock()
        app.anthropic_recovery_enabled_var = True
        app._pending_ui_recovery = None
        app._pending_ui_recovery_agent = None
        connects = []
        reddit_runs = []
        confirmed = []
        recoveries = []

        class Phone:
            progress_note = None

            def close(self):
                pass

        class Stack:
            cancel_event = None

        def connect(_self, _password):
            connects.append(True)
            phone = Phone()
            app.phone = phone
            return phone, Stack()

        def run_reddit(*_args, **_kwargs):
            reddit_runs.append(True)
            if len(reddit_runs) == 1:
                raise PhoneError("Pending Customize tap did not map to one checked pill")

        session = recovery.RecoverySession(
            incident_fingerprint="incident",
            final_fingerprint="ready",
            actions=[{"tool": "wait", "seconds": 1}],
            expected_fingerprints=["ready"],
            model="fake",
        )

        class Agent:
            def __init__(self, *_args, **_kwargs):
                pass

            def recover(self, _exc, *, previous=None):
                recoveries.append(str(_exc))
                if len(recoveries) > 1:
                    raise RuntimeError("unexpected second recovery: " + str(_exc))
                self.previous = previous
                return session

            def confirm_success(self, value):
                confirmed.append(value)

        slot = {
            "id": "a0047",
            "reddit_birth_date": "20.04.2005",
            "ghost": True,
            "ghost_container_instance": "only-container",
            "crane_container_instance": "ONLY-CONTAINER-FULL",
            "email": "masked@icloud.com",
            "username": "saved-user",
            "password": "saved-password",
            "reddit": True,
        }
        with patch.object(creator, "_InactivityWatch", self.QuietWatch), \
                patch.object(creator.App, "_connect", connect), \
                patch.object(creator, "get_slot", return_value=slot), \
                patch.object(creator, "slot_container_deleted", return_value=False), \
                patch.object(creator, "ghost_container_state", return_value="ready"), \
                patch.object(creator, "slot_container_available", return_value=True), \
                patch.object(creator, "slot_reddit_ready", return_value=True), \
                patch.object(creator, "creation_feed_swipe_progress", return_value=(10, 10)), \
                patch.object(creator, "run_create_reddit", side_effect=run_reddit), \
                patch.object(creator, "is_recoverable_bridge_failure", return_value=False), \
                patch.object(creator, "is_ui_recoverable_error", return_value=True), \
                patch.object(creator, "UIRecoveryAgent", Agent):
            result = creator.App._run_creation_pipeline(
                app,
                "offline-password",
                "a0047",
                date(2005, 4, 20),
                10,
                manage_worker=False,
            )
        self.assertTrue(
            result,
            {"recoveries": recoveries, "reddit_runs": len(reddit_runs), "logs": getattr(app, "logs", [])[-8:]},
        )
        self.assertEqual(len(connects), 2)
        self.assertEqual(len(reddit_runs), 2)
        self.assertEqual(len(recoveries), 1, recoveries)
        self.assertEqual(confirmed, [session])


if __name__ == "__main__":
    unittest.main()
