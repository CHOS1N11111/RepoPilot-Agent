from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.runtime import (
    AgentRuntime,
    InMemoryRuntimeStore,
    RuntimeAction,
    RuntimeObservation,
    RuntimePolicy,
    SQLiteRuntimeStore,
    advance_agent_working_state,
    analyze_runtime_recovery,
    create_agent_working_state,
)
from repopilot_agent.memory import MemoryStore


def decision_record(kind: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "version": 2,
        "rationale": "Collect exact durable evidence.",
        "action": {"kind": kind, "arguments": arguments},
        "expected_evidence": "A persisted Runtime observation.",
        "state_update": {
            "focus": "Recover the interrupted action.",
            "add_findings": [],
            "add_open_questions": [],
            "resolve_open_questions": [],
            "plan_updates": [],
            "acceptance_updates": [],
        },
        "finish_reason": "",
        "user_question": "",
    }


class RuntimeRecoveryTests(unittest.TestCase):
    def test_reopen_restores_active_virtual_proposal_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("before\n", encoding="utf-8")
            store = InMemoryRuntimeStore()
            runtime = AgentRuntime(root, "edit notes", run_id="virtual-recovery", store=store)
            state = create_agent_working_state("edit notes")
            runtime.record_working_state(state)
            propose = RuntimeAction(
                kind="propose_patch",
                arguments={
                    "path": "notes.txt",
                    "expected_sha256": hashlib.sha256(b"before\n").hexdigest(),
                    "hunks": [{"old_text": "before", "new_text": "after"}],
                },
                action_id="propose-1",
            )
            proposed = runtime.execute(propose)
            state = advance_agent_working_state(
                state,
                propose,
                proposed,
                selected_paths=["notes.txt"],
            )
            runtime.record_working_state(state)
            inspect = RuntimeAction(
                kind="inspect_proposed_diff",
                action_id="inspect-proposal-1",
            )
            inspected = runtime.execute(inspect)
            state = advance_agent_working_state(
                state,
                inspect,
                inspected,
                selected_paths=["notes.txt"],
            )
            runtime.record_working_state(state)

            recovery_plan = analyze_runtime_recovery(
                runtime.events,
                objective="edit notes",
            )
            reopened = AgentRuntime(
                root,
                "edit notes",
                run_id=runtime.run_id,
                store=store,
            )

            self.assertEqual(
                [item.path for item in recovery_plan.working_state.proposed_edits],
                ["notes.txt"],
            )
            self.assertEqual(len(recovery_plan.context_actions), 1)
            self.assertIn("-before", reopened.proposed_diff)
            self.assertIn("+after", reopened.proposed_diff)
            self.assertEqual(reopened.proposed_edits[0]["path"], "notes.txt")
            self.assertTrue(reopened.working_state.proposed_edits[0].inspected)

    def test_invalid_newest_snapshot_falls_back_to_last_valid_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("# Fixture\n", encoding="utf-8")
            store = InMemoryRuntimeStore()
            runtime = AgentRuntime(root, "read docs", run_id="recover-snapshot", store=store)
            runtime.record_working_state(create_agent_working_state("read docs"))
            action = RuntimeAction(
                kind="read_file",
                arguments={"path": "README.md"},
                action_id="read-1",
            )
            runtime.record_decision(action, decision_record("read_file", {"path": "README.md"}))
            observation = runtime.execute(action)
            store.append_event(
                runtime.run_id,
                "working_state_updated",
                payload={"working_state": {"version": "invalid"}},
            )

            plan = analyze_runtime_recovery(runtime.events, objective="read docs")

            self.assertEqual(observation.status, "completed")
            self.assertEqual(plan.latest_snapshot_sequence, 2)
            self.assertEqual(plan.working_state.iteration, 1)
            self.assertEqual(plan.working_state.selected_paths, ["README.md"])

    def test_sqlite_restart_retries_exact_read_only_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("# Fixture\n", encoding="utf-8")
            database = root / "runtime.sqlite3"
            first_store = SQLiteRuntimeStore(MemoryStore(database))
            runtime = AgentRuntime(
                root,
                "read docs",
                run_id="sqlite-recovery",
                store=first_store,
            )
            runtime.record_working_state(create_agent_working_state("read docs"))
            action = RuntimeAction(
                kind="read_file",
                arguments={"path": "README.md"},
                action_id="read-1",
                idempotency_key="read-1",
            )
            runtime.record_decision(action, decision_record("read_file", {"path": "README.md"}))
            first_store.reserve(runtime.run_id, action)
            first_store.append_event(
                runtime.run_id,
                "action_started",
                action=action,
                payload={"action": action.to_dict()},
            )

            reopened = AgentRuntime(
                root,
                "read docs",
                run_id=runtime.run_id,
                store=SQLiteRuntimeStore(MemoryStore(database)),
            )
            observation = reopened.resume_recoverable_action()

            self.assertEqual(observation.status, "completed")
            self.assertEqual(reopened.recovery_plan.next_step, "next_decision")
            self.assertEqual(reopened.recovery_plan.working_state.iteration, 1)
            sequences = [event.sequence for event in reopened.events]
            self.assertEqual(sequences, list(range(1, len(sequences) + 1)))

    def test_terminal_observation_after_snapshot_is_folded_into_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("# Fixture\n", encoding="utf-8")
            store = InMemoryRuntimeStore()
            runtime = AgentRuntime(root, "read docs", run_id="recover-terminal", store=store)
            runtime.record_working_state(create_agent_working_state("read docs"))
            action = RuntimeAction(
                kind="read_file",
                arguments={"path": "README.md"},
                action_id="read-1",
                idempotency_key="read-1",
            )
            runtime.record_decision(action, decision_record("read_file", {"path": "README.md"}))
            observation = runtime.execute(action)

            plan = analyze_runtime_recovery(store.list_events("recover-terminal"), objective="read docs")

            self.assertEqual(observation.status, "completed")
            self.assertEqual(plan.next_step, "next_decision")
            self.assertEqual(plan.working_state.iteration, 1)
            self.assertEqual(plan.working_state.focus, "Recover the interrupted action.")
            self.assertEqual(plan.working_state.selected_paths, ["README.md"])
            self.assertEqual(plan.replayed_observations[0].action_id, "read-1")
            self.assertTrue(plan.replayed_observations[0].replayed)

    def test_interrupted_read_only_action_is_retried_and_snapshotted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("# Fixture\n", encoding="utf-8")
            store = InMemoryRuntimeStore()
            runtime = AgentRuntime(root, "read docs", run_id="recover-read", store=store)
            runtime.record_working_state(create_agent_working_state("read docs"))
            action = RuntimeAction(
                kind="read_file",
                arguments={"path": "README.md"},
                action_id="read-1",
                idempotency_key="read-1",
            )
            runtime.record_decision(action, decision_record("read_file", {"path": "README.md"}))
            store.reserve(runtime.run_id, action)
            store.append_event(
                runtime.run_id,
                "action_started",
                action=action,
                payload={"action": action.to_dict()},
            )

            reopened = AgentRuntime(root, "read docs", run_id=runtime.run_id, store=store)
            before = reopened.recovery_plan
            observation = reopened.resume_recoverable_action()
            after = reopened.recovery_plan

            self.assertEqual(before.next_step, "retry_read_only")
            self.assertIsNotNone(observation)
            self.assertEqual(observation.status, "completed")
            self.assertEqual(after.next_step, "next_decision")
            self.assertEqual(after.working_state.iteration, 1)
            event_types = [event.event_type for event in reopened.events]
            self.assertIn("action_recovery_started", event_types)
            self.assertIn("action_recovered", event_types)
            self.assertIn("runtime_recovery_state_reconstructed", event_types)

    def test_interrupted_side_effect_requires_exact_confirmation_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "notes.txt"
            target.write_text("before\n", encoding="utf-8")
            store = InMemoryRuntimeStore()
            runtime = AgentRuntime(
                root,
                "edit notes",
                run_id="recover-write",
                store=store,
                policy=RuntimePolicy.sandboxed(allowed_edit_paths=["notes.txt"]),
            )
            runtime.record_working_state(create_agent_working_state("edit notes"))
            action = RuntimeAction(
                kind="edit_file",
                arguments={"path": "notes.txt", "new_content": "after\n"},
                action_id="edit-1",
                idempotency_key="edit-1",
            )
            runtime.record_decision(
                action,
                decision_record(
                    "edit_file",
                    {"path": "notes.txt", "new_content": "after\n"},
                ),
            )
            store.reserve(runtime.run_id, action)
            store.append_event(
                runtime.run_id,
                "action_started",
                action=action,
                payload={"action": action.to_dict()},
            )

            reopened = AgentRuntime(
                root,
                "edit notes",
                run_id=runtime.run_id,
                store=store,
                policy=RuntimePolicy.sandboxed(allowed_edit_paths=["notes.txt"]),
            )
            plan = reopened.prepare_recovery()

            self.assertEqual(plan.next_step, "confirm_side_effect")
            self.assertTrue(plan.requires_confirmation)
            self.assertIsNone(reopened.resume_recoverable_action())
            self.assertEqual(target.read_text(encoding="utf-8"), "before\n")
            with self.assertRaisesRegex(ValueError, "token"):
                reopened.confirm_ambiguous_side_effect("edit-1", "stale")

            observation = reopened.confirm_ambiguous_side_effect(
                "edit-1",
                plan.pending_action.confirmation_token,
            )

            self.assertEqual(observation.status, "outcome_unknown")
            self.assertFalse(observation.data["side_effect_replayed"])
            self.assertEqual(target.read_text(encoding="utf-8"), "before\n")
            self.assertEqual(reopened.recovery_plan.next_step, "next_decision")
            self.assertEqual(reopened.recovery_plan.working_state.iteration, 1)

    def test_completed_reservation_repairs_missing_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("# Fixture\n", encoding="utf-8")
            store = InMemoryRuntimeStore()
            runtime = AgentRuntime(root, "read docs", run_id="recover-table", store=store)
            runtime.record_working_state(create_agent_working_state("read docs"))
            action = RuntimeAction(
                kind="read_file",
                arguments={"path": "README.md"},
                action_id="read-1",
                idempotency_key="read-1",
            )
            runtime.record_decision(action, decision_record("read_file", {"path": "README.md"}))
            store.reserve(runtime.run_id, action)
            store.append_event(
                runtime.run_id,
                "action_started",
                action=action,
                payload={"action": action.to_dict()},
            )
            durable_observation = RuntimeObservation(
                action_id="read-1",
                action_kind="read_file",
                status="completed",
                summary="README.md was already read.",
                data={"path": "README.md", "content": "# Fixture\n"},
            )
            store.complete(runtime.run_id, action, durable_observation)

            reopened = AgentRuntime(root, "read docs", run_id=runtime.run_id, store=store)
            plan = reopened.prepare_recovery()

            self.assertEqual(plan.next_step, "next_decision")
            self.assertEqual(plan.working_state.iteration, 1)
            recovered = [
                event
                for event in reopened.events
                if event.event_type == "action_recovered"
            ]
            self.assertEqual(recovered[-1].payload["source"], "completed_reservation")

    def test_unresolved_approval_keeps_original_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("before\n", encoding="utf-8")
            runtime = AgentRuntime(
                root,
                "edit notes",
                policy=RuntimePolicy.sandboxed(allowed_edit_paths=["notes.txt"]),
            )
            action = RuntimeAction(
                kind="edit_file",
                arguments={"path": "notes.txt", "new_content": "after\n"},
                action_id="edit-1",
            )
            observation = runtime.execute(action)
            checkpoint = observation.data["approval_request"]["checkpoint"]

            plan = runtime.prepare_recovery()

            self.assertEqual(plan.next_step, "await_approval")
            self.assertEqual(plan.pending_action.classification, "awaiting_approval")
            self.assertEqual(plan.working_state.iteration, 1)
            self.assertEqual(plan.working_state.stop_reason, "approval_required")
            self.assertEqual(runtime.pending_approval["checkpoint"], checkpoint)
            self.assertEqual(root.joinpath("notes.txt").read_text(encoding="utf-8"), "before\n")

    def test_public_recovery_plan_omits_write_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("before\n", encoding="utf-8")
            store = InMemoryRuntimeStore()
            runtime = AgentRuntime(
                root,
                "edit notes",
                run_id="public-plan",
                store=store,
                policy=RuntimePolicy.sandboxed(allowed_edit_paths=["notes.txt"]),
            )
            action = RuntimeAction(
                kind="edit_file",
                arguments={"path": "notes.txt", "new_content": "private-content\n"},
                action_id="edit-private",
            )
            store.reserve(runtime.run_id, action)
            store.append_event(
                runtime.run_id,
                "action_started",
                action=action,
                payload={"action": action.to_dict()},
            )

            public = runtime.recovery_plan.to_dict()

            self.assertNotIn("private-content", str(public))
            self.assertEqual(public["pending_action"]["arguments"]["new_content_chars"], 16)

    def test_public_recovery_plan_redacts_secrets_from_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = InMemoryRuntimeStore()
            runtime = AgentRuntime(root, "validate", run_id="secret-command", store=store)
            action = RuntimeAction(
                kind="validate",
                arguments={"command": "python verify.py --api-key=supersecretvalue1234"},
                action_id="validate-secret",
            )
            store.reserve(runtime.run_id, action)
            store.append_event(
                runtime.run_id,
                "action_started",
                action=action,
                payload={"action": action.to_dict()},
            )

            public_text = str(runtime.recovery_plan.to_dict())

            self.assertNotIn("supersecretvalue1234", public_text)
            self.assertIn("REDACTED", public_text)


if __name__ == "__main__":
    unittest.main()
