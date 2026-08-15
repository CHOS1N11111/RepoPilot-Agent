from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.memory import MemoryStore
from repopilot_agent.runtime import (
    AgentRuntime,
    InMemoryRuntimeStore,
    RuntimeAction,
    RuntimeApprovalGrant,
    RuntimeApprovalRequest,
    RuntimePolicy,
    SQLiteRuntimeStore,
)


def grant_request(runtime: AgentRuntime, request: dict, *, ttl: int = 900):
    return runtime.grant_approval(
        request["checkpoint"],
        payload_hash=request["payload_hash"],
        file_scope=request["file_scope"],
        command_allowlist=request["command_allowlist"],
        expires_in_seconds=ttl,
    )


class RuntimeApprovalTests(unittest.TestCase):
    def test_completed_replay_still_respects_the_current_policy_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "notes.txt")
            target.write_text("before\n", encoding="utf-8")
            store = InMemoryRuntimeStore()
            action = RuntimeAction(
                kind="edit_file",
                arguments={"path": "notes.txt", "new_content": "after\n"},
                action_id="policy-replay",
            )
            runtime = AgentRuntime(
                tmp,
                "update notes",
                run_id="policy-replay-run",
                policy=RuntimePolicy.sandboxed(allowed_edit_paths=["notes.txt"]),
                store=store,
            )
            request = runtime.execute(action).data["approval_request"]
            grant_request(runtime, request)
            self.assertEqual(runtime.execute(action).status, "completed")

            restricted = AgentRuntime(
                tmp,
                "update notes",
                run_id="policy-replay-run",
                policy=RuntimePolicy.sandboxed(allowed_edit_paths=["other.txt"]),
                store=store,
            )
            replay = restricted.execute(action)

            self.assertEqual(replay.status, "policy_denied")
            self.assertFalse(replay.replayed)

    def test_runtime_result_exposes_the_pending_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "notes.txt").write_text("before\n", encoding="utf-8")
            action = RuntimeAction(
                kind="edit_file",
                arguments={"path": "notes.txt", "new_content": "after\n"},
                action_id="run-edit",
            )
            runtime = AgentRuntime(
                tmp,
                "update notes",
                policy=RuntimePolicy.sandboxed(allowed_edit_paths=["notes.txt"]),
            )

            result = runtime.run(lambda _observations: action, max_steps=1)

            self.assertEqual(result.status, "waiting")
            self.assertEqual(result.stop_reason, "approval_required")
            self.assertEqual(result.pending_approval["action_id"], "run-edit")
            self.assertIn("diff", result.pending_approval)

    def test_edit_request_contains_exact_action_scope_baseline_and_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "notes.txt")
            target.write_text("before\n", encoding="utf-8")
            action = RuntimeAction(
                kind="edit_file",
                arguments={"path": "notes.txt", "new_content": "after\n"},
                rationale="Update the note.",
                action_id="edit-notes",
            )
            runtime = AgentRuntime(
                tmp,
                "update notes",
                run_id="approval-preview",
                policy=RuntimePolicy.sandboxed(allowed_edit_paths=["notes.txt"]),
            )

            waiting = runtime.execute(action)
            request = waiting.data["approval_request"]

            self.assertEqual(waiting.status, "approval_required")
            self.assertEqual(request["version"], 1)
            self.assertEqual(request["run_id"], "approval-preview")
            self.assertEqual(request["action"], action.to_dict())
            self.assertEqual(request["file_scope"], ["notes.txt"])
            self.assertEqual(request["command_allowlist"], [])
            self.assertEqual(
                request["baseline_hashes"]["notes.txt"],
                hashlib.sha256(b"before\n").hexdigest(),
            )
            self.assertEqual(request["baseline_exists"], {"notes.txt": True})
            self.assertIn("-before", request["diff"])
            self.assertIn("+after", request["diff"])
            self.assertEqual(len(request["payload_hash"]), 64)
            self.assertEqual(runtime.pending_approval, request)
            self.assertEqual(target.read_text(encoding="utf-8"), "before\n")
            self.assertEqual(RuntimeApprovalRequest.from_dict(request).to_dict(), request)

    def test_grant_is_consumed_only_for_the_exact_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "notes.txt")
            target.write_text("before\n", encoding="utf-8")
            runtime = AgentRuntime(
                tmp,
                "update notes",
                policy=RuntimePolicy.sandboxed(allowed_edit_paths=["notes.txt"]),
            )
            original = RuntimeAction(
                kind="edit_file",
                arguments={"path": "notes.txt", "new_content": "approved\n"},
                action_id="same-action",
            )
            first = runtime.execute(original).data["approval_request"]
            grant = grant_request(runtime, first)

            changed = RuntimeAction(
                kind="edit_file",
                arguments={"path": "notes.txt", "new_content": "broadened payload\n"},
                action_id="same-action",
            )
            waiting = runtime.execute(changed)
            second = waiting.data["approval_request"]

            self.assertIsInstance(grant, RuntimeApprovalGrant)
            self.assertEqual(waiting.status, "approval_required")
            self.assertNotEqual(second["checkpoint"], first["checkpoint"])
            self.assertNotEqual(second["payload_hash"], first["payload_hash"])
            self.assertIn("payload", waiting.summary.lower())
            self.assertEqual(target.read_text(encoding="utf-8"), "before\n")
            self.assertIn("approval_invalidated", [event.event_type for event in runtime.events])

            with self.assertRaisesRegex(ValueError, "stale"):
                grant_request(runtime, first)

    def test_grant_rejects_hash_and_scope_broadening(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "notes.txt").write_text("before\n", encoding="utf-8")
            runtime = AgentRuntime(
                tmp,
                "update notes",
                policy=RuntimePolicy.sandboxed(allowed_edit_paths=["notes.txt"]),
            )
            action = RuntimeAction(
                kind="edit_file",
                arguments={"path": "notes.txt", "new_content": "after\n"},
                action_id="edit-scope",
            )
            request = runtime.execute(action).data["approval_request"]

            with self.assertRaisesRegex(ValueError, "payload hash"):
                runtime.grant_approval(
                    request["checkpoint"],
                    payload_hash="0" * 64,
                    file_scope=request["file_scope"],
                    command_allowlist=[],
                )
            with self.assertRaisesRegex(ValueError, "must be a string"):
                runtime.grant_approval(
                    request["checkpoint"],
                    payload_hash=123,  # type: ignore[arg-type]
                    file_scope=request["file_scope"],
                    command_allowlist=[],
                )
            with self.assertRaisesRegex(ValueError, "file scope"):
                runtime.grant_approval(
                    request["checkpoint"],
                    payload_hash=request["payload_hash"],
                    file_scope=["notes.txt", "other.txt"],
                    command_allowlist=[],
                )

            self.assertEqual(runtime.pending_approval, request)
            rejected = [
                event for event in runtime.events if event.event_type == "approval_grant_rejected"
            ]
            self.assertEqual(len(rejected), 3)

    def test_file_change_after_grant_requires_a_fresh_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "notes.txt")
            target.write_text("before\n", encoding="utf-8")
            runtime = AgentRuntime(
                tmp,
                "update notes",
                policy=RuntimePolicy.sandboxed(allowed_edit_paths=["notes.txt"]),
            )
            action = RuntimeAction(
                kind="edit_file",
                arguments={"path": "notes.txt", "new_content": "after\n"},
                action_id="edit-baseline",
            )
            first = runtime.execute(action).data["approval_request"]
            grant_request(runtime, first)
            target.write_text("external\n", encoding="utf-8")

            waiting = runtime.execute(action)
            second = waiting.data["approval_request"]

            self.assertEqual(waiting.status, "approval_required")
            self.assertNotEqual(second["checkpoint"], first["checkpoint"])
            self.assertNotEqual(second["baseline_hashes"], first["baseline_hashes"])
            self.assertIn("baseline", waiting.summary.lower())
            self.assertEqual(target.read_text(encoding="utf-8"), "external\n")

    def test_missing_file_grant_is_invalid_after_an_empty_file_appears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "new.txt")
            runtime = AgentRuntime(
                tmp,
                "create a note",
                policy=RuntimePolicy.sandboxed(allowed_edit_paths=["new.txt"]),
            )
            action = RuntimeAction(
                kind="edit_file",
                arguments={"path": "new.txt", "new_content": "created\n"},
                action_id="create-file",
            )
            first = runtime.execute(action).data["approval_request"]
            grant_request(runtime, first)
            target.write_text("", encoding="utf-8")

            waiting = runtime.execute(action)
            second = waiting.data["approval_request"]

            self.assertEqual(first["baseline_hashes"], second["baseline_hashes"])
            self.assertEqual(first["baseline_exists"], {"new.txt": False})
            self.assertEqual(second["baseline_exists"], {"new.txt": True})
            self.assertNotEqual(first["payload_hash"], second["payload_hash"])
            self.assertEqual(target.read_text(encoding="utf-8"), "")

    def test_expired_grant_requires_a_fresh_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "notes.txt").write_text("before\n", encoding="utf-8")
            now = [datetime(2026, 8, 15, 0, 0, tzinfo=timezone.utc)]
            runtime = AgentRuntime(
                tmp,
                "update notes",
                policy=RuntimePolicy.sandboxed(allowed_edit_paths=["notes.txt"]),
                clock=lambda: now[0],
            )
            action = RuntimeAction(
                kind="edit_file",
                arguments={"path": "notes.txt", "new_content": "after\n"},
                action_id="edit-expiring",
            )
            first = runtime.execute(action).data["approval_request"]
            grant = grant_request(runtime, first, ttl=30)
            now[0] += timedelta(seconds=31)

            waiting = runtime.execute(action)

            self.assertTrue(grant.is_expired(now[0]))
            self.assertEqual(waiting.status, "approval_required")
            self.assertIn("expired", waiting.summary.lower())
            self.assertNotEqual(
                waiting.data["approval_request"]["checkpoint"],
                first["checkpoint"],
            )

    def test_sqlite_reopen_uses_the_persisted_exact_grant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "notes.txt"
            target.write_text("before\n", encoding="utf-8")
            db_path = root / "memory.sqlite3"
            action = RuntimeAction(
                kind="edit_file",
                arguments={"path": "notes.txt", "new_content": "after\n"},
                action_id="persistent-edit",
                idempotency_key="persistent-edit-v1",
            )
            first_runtime = AgentRuntime(
                root,
                "update notes",
                run_id="persistent-approval",
                policy=RuntimePolicy.sandboxed(allowed_edit_paths=["notes.txt"]),
                store=SQLiteRuntimeStore(MemoryStore(db_path)),
            )
            request = first_runtime.execute(action).data["approval_request"]
            grant_request(first_runtime, request)

            reopened = AgentRuntime(
                root,
                "update notes",
                run_id="persistent-approval",
                policy=RuntimePolicy.sandboxed(allowed_edit_paths=["notes.txt"]),
                store=SQLiteRuntimeStore(MemoryStore(db_path)),
            )
            applied = reopened.execute(action)

            self.assertEqual(applied.status, "completed")
            self.assertEqual(target.read_text(encoding="utf-8"), "after\n")
            event_types = [event.event_type for event in reopened.events]
            self.assertEqual(event_types.count("run_started"), 1)
            self.assertIn("approval_consumed", event_types)
            self.assertEqual(reopened.pending_approval, {})

    def test_command_grant_requires_the_exact_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            command = "python -m unittest discover"
            runtime = AgentRuntime(
                tmp,
                "run validation",
                policy=RuntimePolicy.sandboxed(allowed_commands=[command]),
            )
            action = RuntimeAction(
                kind="validate",
                arguments={"command": command},
                action_id="validate-command",
            )
            request = runtime.execute(action).data["approval_request"]

            self.assertEqual(request["file_scope"], [])
            self.assertEqual(request["command_allowlist"], [command])
            with self.assertRaisesRegex(ValueError, "command allowlist"):
                runtime.grant_approval(
                    request["checkpoint"],
                    payload_hash=request["payload_hash"],
                    file_scope=[],
                    command_allowlist=[command, "python -m unittest tests.test_other"],
                )

    def test_rejected_request_never_authorizes_the_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "notes.txt")
            target.write_text("before\n", encoding="utf-8")
            runtime = AgentRuntime(
                tmp,
                "update notes",
                policy=RuntimePolicy.sandboxed(allowed_edit_paths=["notes.txt"]),
            )
            action = RuntimeAction(
                kind="edit_file",
                arguments={"path": "notes.txt", "new_content": "after\n"},
                action_id="rejected-edit",
            )
            first = runtime.execute(action).data["approval_request"]
            runtime.reject_approval(first["checkpoint"], "Not approved.")

            self.assertEqual(runtime.pending_approval, {})
            second_waiting = runtime.execute(action)
            self.assertEqual(second_waiting.status, "approval_required")
            self.assertNotEqual(
                second_waiting.data["approval_request"]["checkpoint"],
                first["checkpoint"],
            )
            self.assertEqual(target.read_text(encoding="utf-8"), "before\n")


if __name__ == "__main__":
    unittest.main()
