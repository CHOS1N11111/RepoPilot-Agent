from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.agent_repair import (
    blocked_agent_repair_fingerprints,
    observe_agent_repair_proposal,
    observe_agent_validation,
    observe_agent_write,
    render_agent_repair_context,
)
from repopilot_agent.models import ValidationResult
from repopilot_agent.repair_loop import (
    STOP_NO_REPOSITORY_CHANGE,
    STOP_REPAIR_BUDGET,
    STOP_REPEATED_FAILURE,
    STOP_REPEATED_PROPOSAL,
)


def failed_validation(output: str = "AssertionError in 0.2s") -> list[ValidationResult]:
    return [
        ValidationResult(
            command="python -m unittest test_auth",
            allowed=True,
            exit_code=1,
            stdout="",
            stderr=output,
        )
    ]


class AgentRepairTransitionTests(unittest.TestCase):
    def test_baseline_failure_opens_first_bounded_repair(self) -> None:
        transition = observe_agent_validation(
            [],
            attempt=0,
            max_attempts=2,
            validation=failed_validation(),
            summary="Initial validation failed.",
        )

        self.assertTrue(transition.repair_required)
        self.assertEqual(transition.next_attempt, 1)
        self.assertEqual(transition.remaining_attempts, 2)
        self.assertEqual(transition.history[0].status, "validation_failed")
        self.assertIn("Remaining new repair attempts: 2", render_agent_repair_context(transition))

    def test_same_failure_after_agent_repair_stops(self) -> None:
        initial = observe_agent_validation(
            [],
            attempt=0,
            max_attempts=2,
            validation=failed_validation(),
            summary="Initial failure.",
        )
        proposal = observe_agent_repair_proposal(
            initial.history,
            attempt=1,
            max_attempts=2,
            trigger_failure_fingerprint=initial.trigger_failure_fingerprint,
            proposal_fingerprint="1" * 64,
            proposal_paths=["auth.py"],
            summary="Repair auth.",
        )
        repeated = observe_agent_validation(
            proposal.history,
            attempt=1,
            max_attempts=2,
            validation=failed_validation("AssertionError in 9.8 seconds"),
            summary="Still failing.",
        )

        self.assertEqual(repeated.stop_reason, STOP_REPEATED_FAILURE)
        self.assertFalse(repeated.repair_required)
        self.assertEqual(repeated.history[-1].status, "stopped")

    def test_repair_budget_zero_stops_after_baseline_failure(self) -> None:
        transition = observe_agent_validation(
            [],
            attempt=0,
            max_attempts=0,
            validation=failed_validation(),
            summary="Initial failure.",
        )

        self.assertEqual(transition.stop_reason, STOP_REPAIR_BUDGET)
        self.assertIsNone(transition.next_attempt)

    def test_repeated_agent_proposal_is_rejected(self) -> None:
        first = observe_agent_repair_proposal(
            [],
            attempt=1,
            max_attempts=3,
            trigger_failure_fingerprint="a" * 64,
            proposal_fingerprint="b" * 64,
            proposal_paths=["auth.py"],
            summary="First repair.",
        )
        repeated = observe_agent_repair_proposal(
            first.history,
            attempt=2,
            max_attempts=3,
            trigger_failure_fingerprint="c" * 64,
            proposal_fingerprint="b" * 64,
            proposal_paths=["auth.py"],
            summary="Repeated repair.",
        )

        self.assertEqual(repeated.stop_reason, STOP_REPEATED_PROPOSAL)
        self.assertEqual(blocked_agent_repair_fingerprints(repeated.history), {"b" * 64})

    def test_approved_noop_write_stops_without_validation(self) -> None:
        transition = observe_agent_write(
            [],
            attempt=1,
            max_attempts=2,
            changed_paths=[],
        )

        self.assertEqual(transition.stop_reason, STOP_NO_REPOSITORY_CHANGE)
        self.assertEqual(transition.history[0].status, "stopped")


if __name__ == "__main__":
    unittest.main()
