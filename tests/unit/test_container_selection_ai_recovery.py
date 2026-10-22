from __future__ import annotations

from datetime import date
from types import SimpleNamespace
import unittest
from unittest import mock

from friend_poster.apps import creator_controller as creator
from friend_poster.services import ui_recovery_agent as recovery
from test_bulk_preserve_skip import make_app


class _QuietWatch:
    def __init__(self, *_args, **_kwargs):
        pass

    def start(self):
        pass

    def stop(self):
        pass


class _ControlledWatch(_QuietWatch):
    timed_out = None

    def __init__(self, timed_out, *_args, **_kwargs):
        type(self).timed_out = timed_out


def _run_controlled_container_selection_failure(before_raise, agent_class):
    app = make_app(None, 1)
    logs = []
    reports = []
    timeout_slots = []
    app.log = logs.append
    app.status = mock.Mock()
    app.anthropic_recovery_enabled_var = True
    app._pending_ui_recovery = None
    app._pending_ui_recovery_agent = None
    app._pending_ui_recovery_chain = None
    app._report_attempt_failure = (
        lambda _sid, **kwargs: reports.append(kwargs)
    )

    def record_timeout(slot_id):
        timeout_slots.append(slot_id)
        app._last_pipeline_attempt_timed_out = True
        app._last_pipeline_failure_code = creator.BULK_INACTIVITY_TIMEOUT_CODE

    app._record_pipeline_timeout = record_timeout

    class Phone:
        progress_note = None

        def close(self):
            pass

    class Stack:
        cancel_event = None

    def connect(_self, _password):
        phone = Phone()
        app.phone = phone
        return phone, Stack()

    def run_reddit(*_args, **_kwargs):
        before_raise(app)
        raise creator.ContainerSelectionError(
            "compact Crane menu did not prove the requested slot"
        )

    slot = {
        "id": "a0220",
        "ghost": True,
        "ghost_container_state": "ready",
        "ghost_container_instance": "instance-a0220",
        "crane_container_instance": "INSTANCE-A0220-FULL",
        "email": "masked@icloud.com",
        "reddit_birth_date": "20.04.2005",
        "username": "saved-user",
        "password": "saved-password",
        "reddit": True,
    }
    with (
        mock.patch.object(creator, "_InactivityWatch", _ControlledWatch),
        mock.patch.object(creator.App, "_connect", connect),
        mock.patch.object(creator, "get_slot", return_value=slot),
        mock.patch.object(creator, "slot_container_deleted", return_value=False),
        mock.patch.object(creator, "ghost_container_state", return_value="ready"),
        mock.patch.object(creator, "slot_container_available", return_value=True),
        mock.patch.object(creator, "run_create_reddit", side_effect=run_reddit),
        mock.patch.object(creator, "is_recoverable_bridge_failure", return_value=False),
        mock.patch.object(creator, "is_ui_recoverable_error", return_value=True),
        mock.patch.object(creator, "UIRecoveryAgent", agent_class),
    ):
        result = creator.App._run_creation_pipeline(
            app,
            "offline-password",
            "a0220",
            date(2005, 4, 20),
            10,
            manage_worker=False,
        )
    return result, app, logs, reports, timeout_slots


def _run_typed_doorway_failure(*, active_bulk: bool, reddit_created: bool):
    """Exercise the controller catch without entering Anthropic recovery."""
    app = make_app(None, 1)
    app.bulk_state.update(
        stage="creating" if active_bulk else "idle",
        slot_id="a0225" if active_bulk else None,
    )
    logs = []
    reports = []
    app.log = logs.append
    app.status = mock.Mock()
    app.anthropic_recovery_enabled_var = True
    app._pending_ui_recovery = None
    app._pending_ui_recovery_agent = None
    app._pending_ui_recovery_chain = None
    app._report_attempt_failure = lambda _sid, **kwargs: reports.append(kwargs)

    class Phone:
        progress_note = None

        def close(self):
            pass

    class Stack:
        cancel_event = None

    def connect(_self, _password):
        phone = Phone()
        app.phone = phone
        return phone, Stack()

    slot = {
        "id": "a0225",
        "ghost": True,
        "ghost_container_state": "ready",
        "ghost_container_instance": "INSTANCE-A0225",
        "crane_container_instance": "INSTANCE-A0225-FULL",
        "email": "masked@icloud.com",
        "reddit_birth_date": "20.04.2005",
        "username": "saved-user" if reddit_created else None,
        "password": "saved-password" if reddit_created else None,
        "reddit": reddit_created,
    }
    with (
        mock.patch.object(creator, "_InactivityWatch", _QuietWatch),
        mock.patch.object(creator.App, "_connect", connect),
        mock.patch.object(creator, "get_slot", return_value=slot),
        mock.patch.object(creator, "slot_container_deleted", return_value=False),
        mock.patch.object(creator, "ghost_container_state", return_value="ready"),
        mock.patch.object(creator, "slot_container_available", return_value=True),
        mock.patch.object(
            creator,
            "run_create_reddit",
            side_effect=creator.CraneDoorwayUnavailable(
                "stock Reddit haptic had no Crane doorway"
            ),
        ),
        mock.patch.object(creator, "UIRecoveryAgent") as agent,
    ):
        result = creator.App._run_creation_pipeline(
            app,
            "offline-password",
            "a0225",
            date(2005, 4, 20),
            10,
            manage_worker=False,
        )
    return result, app, logs, reports, agent


def _run_container_setup_failure(
    agent_class,
    *,
    typed_reconcile: bool = False,
    post_apply_unready: bool = False,
    tool_post_apply_unready: bool = False,
):
    """Exercise recovery while a new Ghost container is still provisioning."""
    app = make_app(None, 1)
    app.bulk_state.update(stage="creating", slot_id="a0227")
    logs = []
    reports = []
    app.log = logs.append
    app.status = mock.Mock()
    app.anthropic_recovery_enabled_var = True
    app._pending_ui_recovery = None
    app._pending_ui_recovery_agent = None
    app._pending_ui_recovery_chain = None
    app._report_attempt_failure = lambda _sid, **kwargs: reports.append(kwargs)

    class Phone:
        progress_note = None

        def close(self):
            pass

    class Stack:
        cancel_event = None

    def connect(_self, _password):
        phone = Phone()
        app.phone = phone
        return phone, Stack()

    slot = {
        "id": "a0227",
        "ghost": True,
        "ghost_container_state": "provisioning",
        "reddit_birth_date": "20.04.2005",
        "reddit": False,
    }
    app._test_initial_queue = [
        dict(item) for item in list(app.bulk_state.get("queue") or [])
    ]
    app._test_initial_slot = dict(slot)
    app._test_slot = slot
    setup_error = (
        creator.ContainerProvisioningReconcileError(
            "Ghost inventory did not prove exactly one new a0227 container"
        )
        if typed_reconcile
        else creator.PhoneError("post-Create screen returned to Home")
    )
    if tool_post_apply_unready:
        apply_calls = 0

        def apply_side_effect(*_args, **_kwargs):
            nonlocal apply_calls
            apply_calls += 1
            if apply_calls == 1:
                raise setup_error
            return None
    else:
        apply_side_effect = None if post_apply_unready else setup_error
    with (
        mock.patch.object(creator, "_InactivityWatch", _QuietWatch),
        mock.patch.object(creator.App, "_connect", connect),
        mock.patch.object(creator, "get_slot", return_value=slot),
        mock.patch.object(creator, "slot_container_deleted", return_value=False),
        mock.patch.object(creator, "ghost_container_state", return_value="provisioning"),
        mock.patch.object(creator, "slot_container_available", return_value=True),
        mock.patch.object(
            creator, "apply_reddit_containers", side_effect=apply_side_effect
        ),
        mock.patch.object(creator, "is_recoverable_bridge_failure", return_value=False),
        mock.patch.object(creator, "is_ui_recoverable_error", return_value=True),
        mock.patch.object(creator, "UIRecoveryAgent", agent_class) as agent,
    ):
        result = creator.App._run_creation_pipeline_attempt(
            app,
            "offline-password",
            "a0227",
            date(2005, 4, 20),
            10,
            manage_worker=False,
        )
    return result, app, logs, reports, agent


def test_container_selection_recovery_is_target_bound_and_verified_before_resume():
    app = make_app(None, 1)
    logs = []
    app.log = logs.append
    app.status = mock.Mock()
    app.anthropic_recovery_enabled_var = True
    app._pending_ui_recovery = None
    app._pending_ui_recovery_agent = None
    app._pending_ui_recovery_chain = None

    connects = []
    reddit_runs = []
    selector_calls = []
    selector_kwargs = []
    confirmed = []
    agents = []
    sparse_proof = creator.FeedCleanupProof(tuple(), sparse_continuity=True)

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
            raise creator.ContainerSelectionError(
                "compact Crane menu did not prove the requested slot"
            )

    session = recovery.RecoverySession(
        incident_fingerprint="incident",
        final_fingerprint="reddit",
        incident_semantic_key="container-menu",
        final_semantic_key="reddit",
        actions=[
            {
                "tool": "workflow",
                "name": "verified_select_existing_container",
                "input": {},
            }
        ],
        expected_fingerprints=["reddit"],
        model="fake",
        directive="resume_pipeline",
    )

    class Agent:
        def __init__(self, *_args, **kwargs):
            self.kwargs = kwargs
            agents.append(self)

        def recover(self, _exc, *, previous=None):
            assert previous is None
            assert self.kwargs["workflow_scope"] == "container_selection"
            context = self.kwargs["context"]
            assert context["target_slot"] == "a0220"
            assert "a0220" in context["required_result"]

            guard = self.kwargs["resume_guard"]
            allowed, reason = guard(SimpleNamespace(nodes=[]))
            assert not allowed
            assert "verified_select_existing_container" in reason

            tool = self.kwargs["workflow_tools"][
                "verified_select_existing_container"
            ]
            directive = tool.handler({})
            assert directive.name == "resume_pipeline"

            allowed, reason = guard(SimpleNamespace(nodes=[]))
            assert allowed, reason
            return session

        def confirm_success(self, value):
            confirmed.append(value)

    slot = {
        "id": "a0220",
        "ghost": True,
        "ghost_container_state": "ready",
        "ghost_container_instance": "instance-a0220",
        "crane_container_instance": "INSTANCE-A0220-FULL",
        "email": "masked@icloud.com",
        "reddit_birth_date": "20.04.2005",
        "username": "saved-user",
        "password": "saved-password",
        "reddit": True,
    }

    def select_compact(_stack, _phone, _log, name, **_kwargs):
        selector_calls.append(name)
        selector_kwargs.append(_kwargs)
        return sparse_proof

    with (
        mock.patch.object(creator, "_InactivityWatch", _QuietWatch),
        mock.patch.object(creator.App, "_connect", connect),
        mock.patch.object(creator, "get_slot", return_value=slot),
        mock.patch.object(creator, "slot_container_deleted", return_value=False),
        mock.patch.object(creator, "ghost_container_state", return_value="ready"),
        mock.patch.object(creator, "slot_container_available", return_value=True),
        mock.patch.object(creator, "slot_reddit_ready", return_value=True),
        mock.patch.object(
            creator, "creation_feed_swipe_progress", return_value=(10, 10)
        ),
        mock.patch.object(creator, "run_create_reddit", side_effect=run_reddit),
        mock.patch.object(creator, "is_recoverable_bridge_failure", return_value=False),
        mock.patch.object(creator, "is_ui_recoverable_error", return_value=True),
        mock.patch.object(creator, "post_selection_state", return_value="unknown"),
        mock.patch.object(
            creator, "advance_feed_cleanup_proof", return_value=sparse_proof
        ) as advance_sparse,
        mock.patch.object(
            creator,
            "select_existing_container_from_compact_menu",
            side_effect=select_compact,
        ),
        mock.patch.object(creator, "UIRecoveryAgent", Agent),
    ):
        result = creator.App._run_creation_pipeline(
            app,
            "offline-password",
            "a0220",
            date(2005, 4, 20),
            10,
            manage_worker=False,
        )

    assert result is True
    assert len(connects) == 2
    assert len(reddit_runs) == 2
    assert selector_calls == ["a0220"]
    assert selector_kwargs == [{"allow_existing_sparse_feed": True}]
    advance_sparse.assert_called_once_with(sparse_proof, [])
    assert confirmed == [session]
    assert len(agents) == 1
    assert app._pending_ui_recovery is None
    assert app._pending_ui_recovery_agent is None
    assert app._pending_ui_recovery_chain is None
    assert any("locally verified" in line for line in logs)


class ContainerSelectionAIRecoveryTests(unittest.TestCase):
    def test_target_bound_selection_is_verified_before_resume(self):
        test_container_selection_recovery_is_target_bound_and_verified_before_resume()

    def test_timed_out_container_selection_does_not_enter_ai_recovery(self):
        class NeverAgent:
            def __init__(self, *_args, **_kwargs):
                raise AssertionError("timed-out selection must not start AI recovery")

        def expire_watchdog(_app):
            _ControlledWatch.timed_out.set()

        result, app, _logs, reports, timeout_slots = (
            _run_controlled_container_selection_failure(
                expire_watchdog, NeverAgent
            )
        )

        self.assertFalse(result)
        self.assertEqual(timeout_slots, ["a0220"])
        self.assertTrue(app._last_pipeline_attempt_timed_out)
        self.assertEqual(reports[-1]["kind"], "inactivity_timeout")

    def test_stop_during_verified_selection_is_reported_as_user_stop(self):
        class CancelledAgent:
            def __init__(self, *_args, **_kwargs):
                pass

            def recover(self, _exc, *, previous=None):
                del previous
                app.stop.set()
                raise creator.OperationCancelled(
                    "stopped during verified container selection"
                )

        app = None

        def leave_watchdog_running(current_app):
            nonlocal app
            app = current_app

        result, returned_app, logs, reports, timeout_slots = (
            _run_controlled_container_selection_failure(
                leave_watchdog_running, CancelledAgent
            )
        )

        self.assertIs(app, returned_app)
        self.assertFalse(result)
        self.assertEqual(timeout_slots, [])
        self.assertEqual(reports[-1]["kind"], "user_stop")
        self.assertEqual(reports[-1]["code"], "user_stop")
        self.assertTrue(any("AI recovery stopped" in line for line in logs))

    def test_typed_missing_doorway_in_incomplete_bulk_requests_local_recreate(self):
        result, app, logs, reports, agent = _run_typed_doorway_failure(
            active_bulk=True,
            reddit_created=False,
        )

        self.assertFalse(result)
        agent.assert_not_called()
        self.assertEqual(
            app._last_pipeline_failure_code,
            creator.CONTAINER_RECREATE_FAILURE_CODE,
        )
        self.assertEqual(
            reports[-1]["code"], creator.CONTAINER_RECREATE_FAILURE_CODE
        )
        self.assertTrue(any("Anthropic calls=0" in line for line in logs))

    def test_typed_missing_doorway_preserves_manual_or_created_account(self):
        for active_bulk, reddit_created in ((False, False), (True, True)):
            with self.subTest(
                active_bulk=active_bulk, reddit_created=reddit_created
            ):
                result, app, _logs, reports, agent = _run_typed_doorway_failure(
                    active_bulk=active_bulk,
                    reddit_created=reddit_created,
                )

                self.assertFalse(result)
                agent.assert_not_called()
                self.assertEqual(
                    app._last_pipeline_failure_code,
                    creator.ContainerSelectionError.failure_code,
                )
                self.assertEqual(
                    reports[-1]["code"],
                    creator.ContainerSelectionError.failure_code,
                )


class ControllerRecoveryBudgetTests(unittest.TestCase):
    def test_container_setup_budget_exhaustion_is_pause_only(self):
        class ExhaustedAgent:
            def __init__(self, *_args, **kwargs):
                self.kwargs = kwargs

            def recover(self, _exc, *, previous=None):
                self.assert_previous(previous)
                assert self.kwargs["workflow_scope"] == "container_setup"
                assert "discard_restart_current_attempt" not in self.kwargs[
                    "workflow_tools"
                ]
                raise recovery.RecoveryBudgetExhausted(6, 6)

            @staticmethod
            def assert_previous(previous):
                assert previous is None

        result, app, logs, reports, _agent = _run_container_setup_failure(
            ExhaustedAgent
        )

        self.assertFalse(result)
        self.assertTrue(app._last_pipeline_failure_shared)
        self.assertEqual(
            app._last_pipeline_failure_code,
            creator.AI_RECOVERY_BUDGET_EXHAUSTED_CODE,
        )
        self.assertEqual(
            reports[-1]["code"], creator.AI_RECOVERY_BUDGET_EXHAUSTED_CODE
        )
        self.assertNotEqual(app._last_pipeline_failure_code, "ai_discard_restart")
        self.assertTrue(any("no destructive reset allowed" in line for line in logs))

    def test_container_setup_refuses_unsolicited_ai_discard_directive(self):
        session = recovery.RecoverySession(
            incident_fingerprint="home",
            final_fingerprint="home",
            directive="discard_restart_current_attempt",
        )

        class DiscardAgent:
            def __init__(self, *_args, **kwargs):
                assert kwargs["workflow_scope"] == "container_setup"
                assert "discard_restart_current_attempt" not in kwargs[
                    "workflow_tools"
                ]

            def recover(self, _exc, *, previous=None):
                assert previous is None
                return session

        result, app, logs, reports, _agent = _run_container_setup_failure(
            DiscardAgent
        )

        self.assertFalse(result)
        self.assertTrue(app._last_pipeline_failure_shared)
        self.assertEqual(
            app._last_pipeline_failure_code, creator.AI_DISCARD_REFUSED_CODE
        )
        self.assertEqual(reports[-1]["code"], creator.AI_DISCARD_REFUSED_CODE)
        self.assertTrue(
            any("destructive reset directive refused" in line for line in logs)
        )

    def test_container_selection_never_exposes_or_accepts_ai_discard(self):
        session = recovery.RecoverySession(
            incident_fingerprint="container-picker",
            final_fingerprint="container-picker",
            directive="discard_restart_current_attempt",
        )
        seen_scopes = []

        class DiscardAgent:
            def __init__(self, *_args, **kwargs):
                seen_scopes.append(kwargs["workflow_scope"])
                assert "discard_restart_current_attempt" not in kwargs[
                    "workflow_tools"
                ]

            def recover(self, _exc, *, previous=None):
                assert previous is None
                return session

        result, app, logs, reports, _timeout_slots = (
            _run_controlled_container_selection_failure(
                lambda _app: None,
                DiscardAgent,
            )
        )

        self.assertFalse(result)
        self.assertEqual(seen_scopes, ["container_selection"])
        self.assertTrue(app._last_pipeline_failure_shared)
        self.assertEqual(
            app._last_pipeline_failure_code, creator.AI_DISCARD_REFUSED_CODE
        )
        self.assertEqual(
            reports[-1]["code"], creator.AI_DISCARD_REFUSED_CODE
        )
        self.assertTrue(any("destructive reset directive refused" in line for line in logs))

    def test_typed_container_reconcile_failure_never_starts_ai(self):
        class NeverAgent:
            def __init__(self, *_args, **_kwargs):
                raise AssertionError("typed local reconciliation must not start AI")

        result, app, logs, reports, _agent = _run_container_setup_failure(
            NeverAgent, typed_reconcile=True
        )

        self.assertFalse(result)
        self.assertTrue(app._last_pipeline_failure_shared)
        self.assertEqual(
            app._last_pipeline_failure_code,
            creator.ContainerProvisioningReconcileError.failure_code,
        )
        self.assertEqual(
            reports[-1]["code"],
            creator.ContainerProvisioningReconcileError.failure_code,
        )
        self.assertTrue(any("Anthropic calls=0" in line for line in logs))

    def test_post_apply_unready_checkpoint_is_local_pause_without_ai(self):
        never_agent = mock.Mock(
            side_effect=AssertionError(
                "deterministic container postcondition must not start UI recovery"
            )
        )

        result, app, logs, reports, agent = _run_container_setup_failure(
            never_agent,
            post_apply_unready=True,
        )

        self.assertFalse(result)
        agent.assert_not_called()
        self.assertTrue(app._last_pipeline_failure_shared)
        self.assertEqual(
            app._last_pipeline_failure_code,
            creator.ContainerProvisioningReconcileError.failure_code,
        )
        self.assertEqual(
            reports[-1]["code"],
            creator.ContainerProvisioningReconcileError.failure_code,
        )
        self.assertEqual(app.bulk_state["queue"], app._test_initial_queue)
        self.assertEqual(app._test_slot, app._test_initial_slot)
        self.assertTrue(any("Anthropic calls=0" in line for line in logs))

    def test_verified_create_tool_uses_typed_provisioning_postcondition(self):
        typed_errors = []

        class InspectToolAgent:
            def __init__(self, *_args, **kwargs):
                self.handler = kwargs["workflow_tools"][
                    "verified_create_select_container"
                ].handler

            def recover(self, _exc, *, previous=None):
                self.assert_previous(previous)
                try:
                    self.handler({})
                except creator.ContainerProvisioningReconcileError as exc:
                    typed_errors.append(exc)
                    raise
                raise AssertionError("unready tool postcondition was accepted")

            @staticmethod
            def assert_previous(previous):
                assert previous is None

        result, app, _logs, _reports, _agent = _run_container_setup_failure(
            InspectToolAgent,
            tool_post_apply_unready=True,
        )

        self.assertFalse(result)
        self.assertEqual(len(typed_errors), 1)
        self.assertIn("verified ready checkpoint", str(typed_errors[0]))
        self.assertEqual(app.bulk_state["queue"], app._test_initial_queue)
        self.assertEqual(app._test_slot, app._test_initial_slot)

    def test_pipeline_replay_reuses_one_budget_and_propagates_exhaustion(self):
        app = make_app(None, 1)
        budget = recovery.RecoveryAttemptBudget(max_calls=1)
        seen = []

        def attempt(*_args, **kwargs):
            current = kwargs["recovery_budget"]
            seen.append(current)
            current.reserve()
            return creator._PIPELINE_RECOVERY_RETRY

        app._run_creation_pipeline_attempt = attempt
        with self.assertRaises(recovery.RecoveryBudgetExhausted):
            creator.App._run_creation_pipeline(
                app,
                "offline-password",
                "a0225",
                date(2005, 4, 20),
                10,
                manage_worker=False,
                recovery_budget=budget,
            )

        self.assertEqual(seen, [budget, budget])
        self.assertEqual(budget.used_calls, 1)

    def test_bridge_reconnect_reuses_one_budget_and_does_not_reset_exhaustion(self):
        app = make_app(None, 1)
        app._persist_bulk = lambda state: setattr(app, "bulk_state", dict(state))
        app._bridge_health_check = lambda _password: None
        budget = recovery.RecoveryAttemptBudget(max_calls=1)
        seen = []

        def pipeline(*_args, **kwargs):
            current = kwargs["recovery_budget"]
            seen.append(current)
            current.reserve()
            raise creator.BridgeTransportError("bridge disconnected")

        app._run_creation_pipeline = pipeline
        with (
            mock.patch.object(creator, "RECOVERY_DELAYS_SECONDS", (0,)),
            self.assertRaises(recovery.RecoveryBudgetExhausted),
        ):
            creator.App._run_pipeline_with_bridge_recovery(
                app,
                "offline-password",
                "a0225",
                date(2005, 4, 20),
                10,
                recovery_budget=budget,
            )

        self.assertEqual(seen, [budget, budget])
        self.assertEqual(budget.used_calls, 1)


if __name__ == "__main__":
    unittest.main()

