from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.execution import ExecutionBudget, ExecutionUsage
from repopilot_agent.models import FileEditProposal, ValidationResult
from repopilot_agent.repair_loop import record_validation_outcome
from repopilot_agent.web_sessions import (
    clear_proposal_sessions,
    create_proposal_session,
    proposal_session_from_record,
    proposal_session_to_record,
)


class ProposalSessionExecutionContractTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_proposal_sessions()

    def test_structured_patch_budget_and_evidence_survive_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("old\n", encoding="utf-8")
            session = create_proposal_session(
                repo_path=str(root),
                task="update notes",
                file_edits=[FileEditProposal("notes.txt", "new\n", "Update notes.")],
                validation_commands=["python -m unittest"],
                timeline=[],
                execution_budget=ExecutionBudget(
                    max_agent_steps=4,
                    max_tool_calls=8,
                    max_validation_commands=2,
                    max_elapsed_seconds=120,
                ),
                execution_usage=ExecutionUsage(agent_steps=2, tool_calls=3, elapsed_ms=500),
            )

            record = proposal_session_to_record(session)
            clear_proposal_sessions()
            restored = proposal_session_from_record(record)
            public = restored.to_public_dict()

            self.assertEqual(restored.structured_patches[0].path, "notes.txt")
            self.assertEqual(restored.structured_patches[0].hunks[0].new_text, "new\n")
            self.assertEqual(restored.acceptance_criteria[-1].kind, "validation")
            self.assertEqual(restored.completion_evidence.status, "pending")
            self.assertEqual(public["execution_budget"]["remaining"]["tool_calls"], 5)

    def test_repair_history_and_stop_state_survive_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("old\n", encoding="utf-8")
            validation = [ValidationResult("python -m unittest", True, 1, "", "failed")]
            history, _ = record_validation_outcome(
                [],
                attempt=0,
                validation=validation,
                summary="Validation failed.",
            )
            session = create_proposal_session(
                repo_path=str(root),
                task="repair notes",
                root_task="update notes",
                file_edits=[FileEditProposal("notes.txt", "new\n", "Update notes.")],
                validation_commands=["python -m unittest"],
                timeline=[],
                repair_history=history,
                repair_stop_reason="repeated_validation_failure",
                repair_stop_message="No progress.",
                auto_repair_enabled=True,
            )

            restored = proposal_session_from_record(proposal_session_to_record(session))
            public = restored.to_public_dict()

            self.assertEqual(restored.root_task, "update notes")
            self.assertEqual(restored.repair_history, history)
            self.assertEqual(public["repair_stop_reason"], "repeated_validation_failure")
            self.assertTrue(public["auto_repair_enabled"])

    def test_legacy_record_rebuilds_new_execution_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("old\n", encoding="utf-8")
            session = create_proposal_session(
                repo_path=str(root),
                task="update notes",
                file_edits=[FileEditProposal("notes.txt", "new\n", "Update notes.")],
                validation_commands=[],
                timeline=[],
            )
            record = proposal_session_to_record(session)
            for key in [
                "structured_patches",
                "acceptance_criteria",
                "execution_budget",
                "execution_usage",
                "completion_evidence",
            ]:
                record.pop(key)

            clear_proposal_sessions()
            restored = proposal_session_from_record(record)

            self.assertEqual(len(restored.structured_patches), 1)
            self.assertTrue(restored.acceptance_criteria)
            self.assertEqual(restored.execution_budget, ExecutionBudget())
            self.assertEqual(restored.completion_evidence.status, "pending")


if __name__ == "__main__":
    unittest.main()
