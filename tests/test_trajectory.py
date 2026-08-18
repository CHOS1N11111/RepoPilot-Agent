from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.models import LLMCallTrace
from repopilot_agent.trajectory import (
    MAX_TRAJECTORY_FRAMES,
    build_agent_trajectory,
    build_agent_trajectory_from_record,
)


def runtime_event(
    sequence: int,
    event_type: str,
    *,
    action_id: str = "",
    action_kind: str = "",
    payload: dict | None = None,
) -> dict:
    data = dict(payload or {})
    if action_kind:
        data.setdefault(
            "action",
            {"kind": action_kind, "arguments": {}, "action_id": action_id},
        )
    return {
        "event_id": f"event-{sequence}",
        "run_id": "trajectory-run",
        "sequence": sequence,
        "event_type": event_type,
        "created_at": f"2026-08-18T00:00:{sequence:02d}+00:00",
        "action_id": action_id or None,
        "payload": data,
    }


class AgentTrajectoryTests(unittest.TestCase):
    def test_builds_stable_secret_safe_metrics_for_parallel_agent_run(self) -> None:
        events = [
            runtime_event(1, "run_started"),
            runtime_event(
                2,
                "decision_recorded",
                action_id="batch-1",
                action_kind="parallel_read",
            ),
            runtime_event(
                3,
                "action_authorized",
                action_id="batch-1",
                action_kind="parallel_read",
            ),
            runtime_event(
                4,
                "action_started",
                action_id="batch-1",
                action_kind="parallel_read",
                payload={"tool_call_cost": 2},
            ),
            runtime_event(
                5,
                "action_completed",
                action_id="batch-1",
                action_kind="parallel_read",
                payload={
                    "observation": {
                        "action_id": "batch-1",
                        "action_kind": "parallel_read",
                        "status": "completed",
                        "summary": "Read files with Authorization: Bearer abcdefghijklmnop",
                        "data": {
                            "results": [
                                {"action_kind": "read_file", "status": "completed"},
                                {"action_kind": "inspect_diff", "status": "completed"},
                            ]
                        },
                    }
                },
            ),
            runtime_event(
                6,
                "decision_recorded",
                action_id="finish-1",
                action_kind="finish",
            ),
            runtime_event(
                7,
                "action_authorized",
                action_id="finish-1",
                action_kind="finish",
            ),
            runtime_event(
                8,
                "action_started",
                action_id="finish-1",
                action_kind="finish",
                payload={"tool_call_cost": 1},
            ),
            runtime_event(
                9,
                "action_completed",
                action_id="finish-1",
                action_kind="finish",
                payload={
                    "observation": {
                        "action_id": "finish-1",
                        "action_kind": "finish",
                        "status": "completed",
                        "summary": "Finished with evidence.",
                    }
                },
            ),
            runtime_event(10, "run_stopped", payload={"reason": "finished"}),
        ]
        working_state = {
            "plan": [
                {
                    "step_id": "inspect",
                    "status": "completed",
                    "evidence_action_ids": ["batch-1"],
                }
            ],
            "acceptance_criteria": [
                {
                    "criterion_id": "analysis_complete",
                    "status": "passed",
                    "evidence_action_ids": ["batch-1"],
                }
            ],
        }
        traces = [
            LLMCallTrace(
                name="agent_step_1",
                model="fake",
                prompt_preview="inspect files",
                raw_output='{"action":"parallel_read"}',
                parsed=True,
                latency_ms=25,
                input_tokens=80,
                output_tokens=20,
                total_tokens=100,
            )
        ]

        trajectory = build_agent_trajectory(
            events,
            working_state=working_state,
            traces=traces,
            completion_ready=True,
        ).to_dict()

        self.assertTrue(trajectory["integrity"]["valid"])
        self.assertEqual(trajectory["metrics"]["action_sequence"], ["parallel_read", "finish"])
        self.assertEqual(trajectory["metrics"]["tool_calls"], 3)
        self.assertEqual(trajectory["metrics"]["successful_tool_results"], 3)
        self.assertEqual(trajectory["metrics"]["evidence_coverage"], 1.0)
        self.assertEqual(trajectory["metrics"]["evidence_tool_efficiency"], 0.6667)
        self.assertEqual(trajectory["metrics"]["unauthorized_side_effects"], 0)
        self.assertEqual(trajectory["metrics"]["stop_reason"], "finished")
        self.assertEqual(trajectory["metrics"]["llm"]["total_tokens"], 100)
        self.assertEqual(trajectory["metrics"]["llm"]["token_source"], "provider")
        self.assertNotIn("payload", trajectory["frames"][4])
        self.assertNotIn("abcdefghijklmnop", str(trajectory["frames"]))
        self.assertIn("[REDACTED]", trajectory["frames"][4]["summary"])

        changed_timestamps = copy.deepcopy(events)
        for event in changed_timestamps:
            event["created_at"] = "2030-01-01T12:00:00+00:00"
        rebuilt = build_agent_trajectory(
            changed_timestamps,
            working_state=working_state,
            traces=traces,
        )
        self.assertEqual(trajectory["fingerprint"], rebuilt.fingerprint)

    def test_detects_side_effect_started_without_authorization(self) -> None:
        action = {"action_id": "write-1", "kind": "edit_file", "arguments": {}}
        unauthorized = build_agent_trajectory(
            [
                runtime_event(1, "run_started"),
                runtime_event(
                    2,
                    "action_started",
                    action_id="write-1",
                    action_kind="edit_file",
                ),
            ]
        )
        authorized = build_agent_trajectory(
            [
                runtime_event(1, "run_started"),
                runtime_event(
                    2,
                    "action_authorized",
                    action_id="write-1",
                    payload={"action": action},
                ),
                runtime_event(
                    3,
                    "action_started",
                    action_id="write-1",
                    payload={"action": action},
                ),
            ]
        )

        self.assertEqual(unauthorized.metrics["unauthorized_side_effects"], 1)
        self.assertEqual(unauthorized.metrics["unauthorized_action_ids"], ["write-1"])
        self.assertEqual(authorized.metrics["unauthorized_side_effects"], 0)

    def test_hides_input_answers_and_bounds_replay_frames(self) -> None:
        events = [
            runtime_event(
                index,
                "input_received" if index == 2 else "working_state_updated",
                payload={
                    "answer": "OPENAI_API_KEY=sk-supersecretvalue",
                    "working_state": {"iteration": index, "status": "active"},
                },
            )
            for index in range(1, MAX_TRAJECTORY_FRAMES + 6)
        ]

        trajectory = build_agent_trajectory(events)

        self.assertEqual(trajectory.event_count, MAX_TRAJECTORY_FRAMES + 5)
        self.assertEqual(trajectory.frame_count, MAX_TRAJECTORY_FRAMES)
        self.assertEqual(trajectory.omitted_frames, 5)
        self.assertEqual(trajectory.frames[0].sequence, 1)
        self.assertEqual(trajectory.frames[-1].sequence, MAX_TRAJECTORY_FRAMES + 5)
        answer_frame = next(frame for frame in trajectory.frames if frame.sequence == 2)
        self.assertEqual(
            answer_frame.summary,
            "Bound a user answer to the pending input request.",
        )
        self.assertNotIn("supersecretvalue", str(trajectory.to_dict()))

    def test_record_adapter_accepts_serialized_workflow_data(self) -> None:
        record = {
            "agent_run_id": "record-run",
            "agent_events": [],
            "agent_stop_reason": "step_limit",
            "agent_completion_ready": False,
            "execution_budget": {"usage": {"tool_calls": 4}},
        }

        trajectory = build_agent_trajectory_from_record(record)

        self.assertEqual(trajectory["run_id"], "record-run")
        self.assertEqual(trajectory["metrics"]["stop_reason"], "step_limit")
        self.assertEqual(trajectory["metrics"]["tool_calls"], 4)

    def test_repair_cycles_exclude_initial_validation_attempt(self) -> None:
        trajectory = build_agent_trajectory(
            [],
            repair_history=[
                {"attempt": 0, "status": "validation_failed"},
                {"attempt": 1, "status": "proposal_ready"},
                {"attempt": 2, "status": "completed"},
            ],
        )

        self.assertEqual(trajectory.metrics["repair_attempts"], [1, 2])
        self.assertEqual(trajectory.metrics["repair_cycles"], 2)


if __name__ == "__main__":
    unittest.main()
