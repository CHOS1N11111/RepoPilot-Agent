from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.models import FileEditProposal, ValidationResult
from repopilot_agent.repair_loop import (
    STOP_REPEATED_FAILURE,
    STOP_REPEATED_PROPOSAL,
    agent_write_proposal_fingerprint,
    latest_failure_fingerprint,
    proposal_changes_repository,
    record_repair_proposal,
    record_validation_outcome,
    repair_attempts_from_records,
    repair_proposal_fingerprint,
    validation_failure_fingerprint,
)
from repopilot_agent.runtime import RuntimeAction


def failed_validation(output: str = "AssertionError: expected true in 0.42s") -> list[ValidationResult]:
    return [
        ValidationResult(
            command="python -m unittest tests.test_auth",
            allowed=True,
            exit_code=1,
            stdout="",
            stderr=output,
        )
    ]


class RepairLoopContractTests(unittest.TestCase):
    def test_failure_fingerprint_ignores_duration_address_and_whitespace_noise(self) -> None:
        first = failed_validation("AssertionError at 0xABC in 0.42s\n expected true")
        second = failed_validation("AssertionError at 0x123 in 8.1 seconds expected   true")

        self.assertEqual(
            validation_failure_fingerprint(first),
            validation_failure_fingerprint(second),
        )

    def test_allowed_validation_without_exit_code_is_still_a_failure(self) -> None:
        result = ValidationResult(
            command="python -m unittest test_auth",
            allowed=True,
            exit_code=None,
            stdout="",
            stderr="Command did not return an exit code.",
        )

        self.assertTrue(validation_failure_fingerprint([result]))

    def test_proposal_fingerprint_is_order_independent_and_content_sensitive(self) -> None:
        first = [
            FileEditProposal("a.txt", "new a\n", "A"),
            FileEditProposal("b.txt", "new b\n", "B"),
        ]
        reordered = list(reversed(first))
        changed = [FileEditProposal("a.txt", "different\n", "A")]

        self.assertEqual(repair_proposal_fingerprint(first), repair_proposal_fingerprint(reordered))
        self.assertNotEqual(repair_proposal_fingerprint(first), repair_proposal_fingerprint(changed))

    def test_agent_write_fingerprint_ignores_runtime_identity(self) -> None:
        arguments = {
            "path": "auth.py",
            "expected_sha256": "a" * 64,
            "hunks": [{"old_text": "return False", "new_text": "return True"}],
        }
        first = RuntimeAction(
            "apply_patch",
            arguments,
            action_id="repair-one",
            idempotency_key="first",
        )
        repeated = RuntimeAction(
            "apply_patch",
            {**arguments, "expected_sha256": "b" * 64},
            action_id="repair-two",
            idempotency_key="second",
        )
        changed = RuntimeAction(
            "apply_patch",
            {
                **arguments,
                "hunks": [{"old_text": "return False", "new_text": "return None"}],
            },
            action_id="repair-three",
        )

        self.assertEqual(
            agent_write_proposal_fingerprint(first),
            agent_write_proposal_fingerprint(repeated),
        )
        self.assertNotEqual(
            agent_write_proposal_fingerprint(first),
            agent_write_proposal_fingerprint(changed),
        )

    def test_proposal_change_detection_compares_current_repository_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("current\n", encoding="utf-8")

            self.assertFalse(
                proposal_changes_repository(
                    root,
                    [FileEditProposal("notes.txt", "current\n", "No-op")],
                )
            )
            self.assertTrue(
                proposal_changes_repository(
                    root,
                    [FileEditProposal("notes.txt", "updated\n", "Update")],
                )
            )

    def test_same_failure_after_repair_stops_without_progress(self) -> None:
        history, initial = record_validation_outcome(
            [],
            attempt=0,
            validation=failed_validation(),
            summary="Initial failure.",
        )
        history, proposal = record_repair_proposal(
            history,
            attempt=1,
            trigger_failure_fingerprint=initial.fingerprint,
            edits=[FileEditProposal("auth.py", "return True\n", "Fix auth")],
            summary="Repair auth.",
        )
        history, result = record_validation_outcome(
            history,
            attempt=1,
            validation=failed_validation(),
            summary="Still failing.",
        )

        self.assertTrue(proposal.accepted)
        self.assertFalse(result.accepted)
        self.assertEqual(result.stop_reason, STOP_REPEATED_FAILURE)
        self.assertEqual(history[-1].status, "stopped")
        self.assertEqual(latest_failure_fingerprint(history), result.fingerprint)

    def test_changed_failure_allows_another_bounded_attempt(self) -> None:
        history, initial = record_validation_outcome(
            [],
            attempt=0,
            validation=failed_validation(),
            summary="Initial failure.",
        )
        history, _ = record_repair_proposal(
            history,
            attempt=1,
            trigger_failure_fingerprint=initial.fingerprint,
            edits=[FileEditProposal("auth.py", "return True\n", "Fix auth")],
            summary="Repair auth.",
        )
        _, result = record_validation_outcome(
            history,
            attempt=1,
            validation=failed_validation("ImportError: changed failure"),
            summary="Different failure.",
        )

        self.assertTrue(result.accepted)
        self.assertIsNone(result.stop_reason)

    def test_repeated_repair_proposal_is_rejected(self) -> None:
        edits = [FileEditProposal("auth.py", "return True\n", "Fix auth")]
        history, _ = record_repair_proposal(
            [],
            attempt=1,
            trigger_failure_fingerprint="failure-a",
            edits=edits,
            summary="First proposal.",
        )
        history, repeated = record_repair_proposal(
            history,
            attempt=2,
            trigger_failure_fingerprint="failure-b",
            edits=edits,
            summary="Repeated proposal.",
        )

        self.assertFalse(repeated.accepted)
        self.assertEqual(repeated.stop_reason, STOP_REPEATED_PROPOSAL)
        self.assertEqual(history[-1].status, "stopped")

    def test_attempt_records_round_trip_from_json_compatible_data(self) -> None:
        history, _ = record_validation_outcome(
            [],
            attempt=0,
            validation=failed_validation(),
            summary="Initial failure.",
        )

        restored = repair_attempts_from_records([item.to_dict() for item in history])

        self.assertEqual(restored, history)


if __name__ == "__main__":
    unittest.main()
