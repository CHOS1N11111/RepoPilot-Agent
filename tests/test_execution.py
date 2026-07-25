from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.execution import (
    ExecutionBudget,
    ExecutionUsage,
    build_acceptance_criteria,
    evaluate_completion,
    execution_budget_state,
    pending_completion_evidence,
)
from repopilot_agent.models import ValidationResult


class ExecutionContractTests(unittest.TestCase):
    def test_change_workflow_derives_scope_and_validation_criteria(self) -> None:
        criteria = build_acceptance_criteria(
            "fix parser",
            ["src/parser.py", "src/parser.py"],
            ["python -m unittest tests.test_parser"],
        )

        self.assertEqual(
            [criterion.criterion_id for criterion in criteria],
            ["task_change", "approval_scope", "validation_1"],
        )
        self.assertTrue(all(criterion.required for criterion in criteria))
        self.assertEqual(criteria[-1].evidence_ref, "python -m unittest tests.test_parser")
        self.assertEqual(pending_completion_evidence(criteria).status, "pending")

    def test_analysis_only_workflow_is_complete_without_repository_edits(self) -> None:
        criteria = build_acceptance_criteria("explain this repository", [], [])

        evidence = pending_completion_evidence(criteria)

        self.assertEqual(evidence.status, "passed")
        self.assertEqual(evidence.criteria[0].status, "passed")

    def test_completion_requires_changed_approved_files_and_passing_validation(self) -> None:
        command = "python -m unittest tests.test_parser"
        criteria = build_acceptance_criteria("fix parser", ["src/parser.py"], [command])

        passed = evaluate_completion(
            criteria,
            changed_files=["src/parser.py"],
            approved_paths=["src/parser.py"],
            validation=[ValidationResult(command, True, 0, "ok", "")],
            diff="--- a/src/parser.py\n+++ b/src/parser.py\n",
        )
        failed = evaluate_completion(
            criteria,
            changed_files=["src/parser.py", "src/other.py"],
            approved_paths=["src/parser.py"],
            validation=[ValidationResult(command, True, 1, "", "failed")],
            diff="diff",
        )

        self.assertEqual(passed.status, "passed")
        self.assertTrue(passed.diff_available)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(
            {item.criterion_id for item in failed.criteria if item.status == "failed"},
            {"approval_scope", "validation_1"},
        )

    def test_missing_automated_validation_is_advisory(self) -> None:
        criteria = build_acceptance_criteria("update docs", ["README.md"], [])

        evidence = evaluate_completion(
            criteria,
            changed_files=["README.md"],
            approved_paths=["README.md"],
            validation=[],
            diff="diff",
        )

        self.assertEqual(evidence.status, "passed")
        self.assertEqual(evidence.criteria[-1].status, "not_run")

    def test_budget_state_tracks_remaining_capacity_and_exhaustion(self) -> None:
        budget = ExecutionBudget(
            max_agent_steps=3,
            max_tool_calls=4,
            max_validation_commands=2,
            max_elapsed_seconds=5,
        )
        usage = ExecutionUsage(agent_steps=2, tool_calls=5, validation_commands=1, elapsed_ms=2500)

        state = execution_budget_state(budget, usage)

        self.assertTrue(state["exhausted"])
        self.assertIn("tool call budget exceeded", state["exhausted_reasons"])
        self.assertEqual(state["remaining"]["agent_steps"], 1)
        self.assertEqual(state["remaining"]["elapsed_ms"], 2500)
        self.assertEqual(ExecutionBudget.from_dict(budget.to_dict()), budget)
        self.assertEqual(ExecutionUsage.from_dict(usage.to_dict()), usage)


if __name__ == "__main__":
    unittest.main()
