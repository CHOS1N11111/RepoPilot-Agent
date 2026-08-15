from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.agent_loop import run_agent_loop
from repopilot_agent.agent_write import execute_pending_agent_write
from repopilot_agent.llm.base import LLMMessage
from repopilot_agent.models import RepoFile
from repopilot_agent.runtime import InMemoryRuntimeStore
from repopilot_agent.worktree_sandbox import (
    create_worktree_sandbox,
    remove_worktree_sandbox,
)


class FakeLLMClient:
    model = "fake-managed-write"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[list[LLMMessage]] = []

    def complete(self, messages: list[LLMMessage]) -> str:
        self.calls.append(messages)
        return self.responses.pop(0)


def decision(
    kind: str,
    arguments: dict,
    *,
    plan_updates: list[dict] | None = None,
    acceptance_updates: list[dict] | None = None,
) -> str:
    return json.dumps(
        {
            "version": 2,
            "rationale": f"Use {kind} for the managed write trajectory.",
            "action": {"kind": kind, "arguments": arguments},
            "expected_evidence": f"Typed evidence from {kind}.",
            "state_update": {
                "focus": "Apply the inspected virtual change safely.",
                "add_findings": [],
                "add_open_questions": [],
                "resolve_open_questions": [],
                "plan_updates": plan_updates or [],
                "acceptance_updates": acceptance_updates or [],
            },
            "finish_reason": "",
            "user_question": "",
        }
    )


def initialize_repository(path: Path, content: str) -> None:
    path.mkdir()
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "tester@example.local"], cwd=path, check=True)
    (path / "main.py").write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "main.py"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=path, check=True, capture_output=True, text=True)


class AgentWriteLoopTests(unittest.TestCase):
    def test_approved_write_stays_in_managed_worktree_and_records_evidence(self) -> None:
        original = "def value():\n    return 1\n"
        expected_sha256 = hashlib.sha256(original.encode("utf-8")).hexdigest()
        responses = [
            decision("read_file", {"path": "main.py"}),
            decision(
                "propose_patch",
                {
                    "path": "main.py",
                    "expected_sha256": expected_sha256,
                    "hunks": [{"old_text": "return 1", "new_text": "return 2"}],
                },
                plan_updates=[
                    {
                        "step_id": "investigate_repository",
                        "title": "Inspect target",
                        "detail": "Read main.py before changing it.",
                        "status": "completed",
                        "evidence_action_ids": ["explore-1"],
                    }
                ],
                acceptance_updates=[
                    {
                        "criterion_id": "analysis_complete",
                        "kind": "analysis",
                        "description": "Repository evidence supports the requested change.",
                        "required": True,
                        "evidence_action_ids": ["explore-1"],
                        "evidence_summary": "main.py was read with its complete SHA-256.",
                    }
                ],
            ),
            decision("inspect_proposed_diff", {}),
            decision(
                "apply_patch",
                {
                    "path": "main.py",
                    "expected_sha256": expected_sha256,
                    "hunks": [{"old_text": "return 1", "new_text": "return 2"}],
                },
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            managed = root / "managed"
            initialize_repository(source, original)
            sandbox = create_worktree_sandbox(
                source,
                name="agent-write",
                worktree_root=managed,
            )
            sandbox_path = Path(sandbox.path)
            store = InMemoryRuntimeStore()
            client = FakeLLMClient(responses)
            files = [
                RepoFile(
                    path=sandbox_path / "main.py",
                    relative_path="main.py",
                    size_bytes=len(original.encode("utf-8")),
                    language="python",
                    content=original,
                )
            ]
            try:
                waiting = run_agent_loop(
                    "change value to 2",
                    sandbox_path,
                    files,
                    [],
                    client,
                    max_steps=4,
                    runtime_run_id="managed-write-run",
                    runtime_store=store,
                    allow_write_actions=True,
                    managed_worktree_root=managed,
                )

                self.assertEqual(waiting.stop_reason, "approval_required")
                self.assertEqual(waiting.pending_approval["action_kind"], "apply_patch")
                self.assertIn("+    return 2", waiting.pending_approval["diff"])
                self.assertEqual((sandbox_path / "main.py").read_text(encoding="utf-8"), original)
                self.assertEqual((source / "main.py").read_text(encoding="utf-8"), original)
                self.assertIn("managed-worktree", client.calls[-1][0].content)

                request = waiting.pending_approval
                completed = execute_pending_agent_write(
                    source,
                    sandbox_path,
                    "change value to 2",
                    "managed-write-run",
                    store,
                    checkpoint=request["checkpoint"],
                    payload_hash=request["payload_hash"],
                    file_scope=request["file_scope"],
                    command_allowlist=request["command_allowlist"],
                    worktree_root=managed,
                )

                self.assertEqual(completed.status, "completed")
                self.assertIn("return 2", (sandbox_path / "main.py").read_text(encoding="utf-8"))
                self.assertEqual((source / "main.py").read_text(encoding="utf-8"), original)
                evidence = completed.write_observation.data["write_evidence"][0]
                self.assertEqual(evidence["before_sha256"], expected_sha256)
                self.assertEqual(
                    evidence["after_sha256"],
                    hashlib.sha256("def value():\n    return 2\n".encode("utf-8")).hexdigest(),
                )
                self.assertEqual(completed.rollback_snapshot_paths, ["main.py"])
                self.assertIn("+    return 2", completed.diff_observation.data["diff"])
                self.assertEqual(completed.working_state["proposed_edits"], [])
                snapshots = store.list_events("managed-write-run")
                snapshot_events = [
                    event for event in snapshots if event.event_type == "rollback_snapshot_recorded"
                ]
                self.assertEqual(len(snapshot_events), 1)
                self.assertEqual(
                    snapshot_events[0].payload["snapshots"][0]["original_content"],
                    original,
                )
            finally:
                if sandbox_path.exists():
                    remove_worktree_sandbox(
                        source,
                        sandbox_path,
                        force=True,
                        worktree_root=managed,
                    )

    def test_write_must_match_the_latest_inspected_virtual_revision(self) -> None:
        original = "value = 1\n"
        expected_sha256 = hashlib.sha256(original.encode("utf-8")).hexdigest()
        client = FakeLLMClient(
            [
                decision("read_file", {"path": "main.py"}),
                decision(
                    "propose_patch",
                    {
                        "path": "main.py",
                        "expected_sha256": expected_sha256,
                        "hunks": [{"old_text": "1", "new_text": "2"}],
                    },
                ),
                decision("inspect_proposed_diff", {}),
                decision(
                    "apply_patch",
                    {
                        "path": "main.py",
                        "expected_sha256": expected_sha256,
                        "hunks": [{"old_text": "1", "new_text": "3"}],
                    },
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            managed = root / "managed"
            initialize_repository(source, original)
            sandbox = create_worktree_sandbox(
                source,
                name="mismatch",
                worktree_root=managed,
            )
            sandbox_path = Path(sandbox.path)
            files = [
                RepoFile(
                    path=sandbox_path / "main.py",
                    relative_path="main.py",
                    size_bytes=len(original.encode("utf-8")),
                    language="python",
                    content=original,
                )
            ]
            try:
                result = run_agent_loop(
                    "change value to 2",
                    sandbox_path,
                    files,
                    [],
                    client,
                    max_steps=4,
                    allow_write_actions=True,
                    managed_worktree_root=managed,
                )

                self.assertEqual(result.stop_reason, "step_limit")
                self.assertEqual(result.pending_approval, {})
                self.assertIn("does not reproduce", result.steps[-1].observation)
                self.assertEqual((sandbox_path / "main.py").read_text(encoding="utf-8"), original)
                self.assertNotIn(
                    "approval_required",
                    [event.event_type for event in result.events],
                )
            finally:
                if sandbox_path.exists():
                    remove_worktree_sandbox(
                        source,
                        sandbox_path,
                        force=True,
                        worktree_root=managed,
                    )


if __name__ == "__main__":
    unittest.main()
