from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.execution import AcceptanceCriterion
from repopilot_agent.memory import MemoryStore
from repopilot_agent.models import (
    AgentAcceptanceUpdate,
    AgentPlanUpdate,
    AgentStateUpdate,
)
from repopilot_agent.runtime import (
    AGENT_WORKING_STATE_VERSION,
    MAX_RECENT_OBSERVATIONS,
    AgentRuntime,
    RuntimeAction,
    RuntimeObservation,
    SQLiteRuntimeStore,
    advance_agent_working_state,
    agent_completion_blockers,
    agent_completion_ready,
    agent_working_state_from_record,
    apply_agent_state_update,
    create_agent_working_state,
    latest_agent_working_state,
    prepare_post_write_acceptance,
    stop_agent_working_state,
)


class AgentWorkingStateTests(unittest.TestCase):
    def test_validation_acceptance_is_bound_to_exact_successful_command(self) -> None:
        command = "python -m unittest tests.test_parser"
        state = create_agent_working_state(
            "fix parser",
            acceptance_criteria=[
                AcceptanceCriterion(
                    criterion_id="validation_1",
                    kind="validation",
                    description=f"Validation passes: {command}",
                    evidence_ref=command,
                )
            ],
        )
        wrong_command = "python -m unittest tests.test_other"
        state = advance_agent_working_state(
            state,
            RuntimeAction(
                kind="validate",
                arguments={"command": wrong_command},
                action_id="validate-wrong",
            ),
            RuntimeObservation(
                action_id="validate-wrong",
                action_kind="validate",
                status="completed",
                summary="Other validation passed.",
                data={"command": wrong_command, "passed": True, "exit_code": 0},
            ),
            selected_paths=[],
        )

        self.assertEqual(state.acceptance_criteria[0].status, "pending")
        with self.assertRaises(ValueError):
            apply_agent_state_update(
                state,
                AgentStateUpdate(
                    acceptance_updates=[
                        AgentAcceptanceUpdate(
                            criterion_id="validation_1",
                            kind="validation",
                            description=f"Validation passes: {command}",
                            required=True,
                            evidence_action_ids=["validate-wrong"],
                            evidence_summary="A different command passed.",
                        )
                    ]
                ),
            )

        failed = advance_agent_working_state(
            state,
            RuntimeAction(
                kind="validate",
                arguments={"command": command},
                action_id="validate-failed",
            ),
            RuntimeObservation(
                action_id="validate-failed",
                action_kind="validate",
                status="verification_failed",
                summary="Parser validation failed.",
                data={"command": command, "passed": False, "exit_code": 1},
            ),
            selected_paths=[],
        )
        self.assertEqual(failed.acceptance_criteria[0].status, "failed")
        self.assertIn("acceptance:validation_1", agent_completion_blockers(failed))

        passed = advance_agent_working_state(
            failed,
            RuntimeAction(
                kind="validate",
                arguments={"command": command},
                action_id="validate-passed",
            ),
            RuntimeObservation(
                action_id="validate-passed",
                action_kind="validate",
                status="completed",
                summary="Parser validation passed.",
                data={"command": command, "passed": True, "exit_code": 0},
            ),
            selected_paths=[],
        )
        criterion = passed.acceptance_criteria[0]
        self.assertEqual(criterion.status, "passed")
        self.assertEqual(criterion.evidence_action_ids, ["validate-passed"])
        self.assertEqual(criterion.evidence_ref, command)

    def test_post_write_acceptance_resets_exact_validation_cycle(self) -> None:
        command = "python -m unittest discover -s tests"
        state = create_agent_working_state("update parser")
        state = advance_agent_working_state(
            state,
            RuntimeAction(
                kind="apply_patch",
                arguments={"path": "src/parser.py"},
                action_id="write-1",
            ),
            RuntimeObservation(
                action_id="write-1",
                action_kind="apply_patch",
                status="applied",
                summary="Applied parser patch.",
                data={"applied": True, "changed_files": ["src/parser.py"]},
            ),
            selected_paths=["src/parser.py"],
        )

        prepared = prepare_post_write_acceptance(
            state,
            write_action_id="write-1",
            changed_paths=["src/parser.py"],
            validation_commands=[command, command],
        )
        by_id = {item.criterion_id: item for item in prepared.acceptance_criteria}

        self.assertEqual(by_id["task_change"].status, "passed")
        self.assertEqual(by_id["approval_scope"].status, "passed")
        self.assertEqual(by_id["validation_1"].status, "pending")
        self.assertEqual(by_id["validation_1"].evidence_ref, command)
        self.assertNotIn("validation_2", by_id)
        self.assertIn("acceptance:validation_1", agent_completion_blockers(prepared))

        many_commands = [f"python -m unittest test_{index}" for index in range(16)]
        prepared_many = prepare_post_write_acceptance(
            state,
            write_action_id="write-1",
            changed_paths=["src/parser.py"],
            validation_commands=many_commands,
        )
        many_by_id = {
            item.criterion_id: item
            for item in prepared_many.acceptance_criteria
        }
        self.assertIn("task_change", many_by_id)
        self.assertIn("approval_scope", many_by_id)
        self.assertEqual(many_by_id["validation_16"].evidence_ref, many_commands[-1])

    def test_state_progress_is_bounded_and_excludes_unsafe_paths(self) -> None:
        state = create_agent_working_state("x" * 3_000)
        for iteration in range(MAX_RECENT_OBSERVATIONS + 3):
            state = advance_agent_working_state(
                state,
                RuntimeAction(
                    kind="read_file",
                    arguments={"path": "src/main.py"},
                    action_id=f"read-{iteration}",
                ),
                RuntimeObservation(
                    action_id=f"read-{iteration}",
                    action_kind="read_file",
                    status="completed",
                    summary="s" * 700,
                ),
                selected_paths=[
                    "src\\main.py",
                    "../secret.txt",
                    "C:\\Users\\secret.txt",
                    "src/main.py",
                ],
            )

        self.assertEqual(len(state.objective), 2_000)
        self.assertEqual(state.iteration, MAX_RECENT_OBSERVATIONS + 3)
        self.assertEqual(len(state.recent_observations), MAX_RECENT_OBSERVATIONS)
        self.assertEqual(len(state.recent_observations[-1].summary), 500)
        self.assertEqual(state.selected_paths, ["src/main.py"])
        self.assertEqual(state.phase, "inspection")
        self.assertEqual(state.status, "running")

    def test_terminal_and_legacy_records_are_normalized(self) -> None:
        state = create_agent_working_state("inspect repository")
        finished = stop_agent_working_state(state, "finished")
        restored = agent_working_state_from_record(
            {
                **finished.to_dict(),
                "version": -4,
                "iteration": -2,
                "selected_paths": ["src/main.py", "../../outside", 3],
                "recent_observations": ["invalid"],
                "api_key": "must-be-ignored",
                "base_url": "https://must-not-be-loaded.example",
            }
        )

        self.assertIsNotNone(restored)
        self.assertEqual(restored.version, AGENT_WORKING_STATE_VERSION)
        self.assertEqual(restored.iteration, 0)
        self.assertEqual(restored.selected_paths, ["src/main.py"])
        self.assertEqual(restored.status, "completed")
        self.assertNotIn("api_key", restored.to_dict())
        self.assertNotIn("base_url", restored.to_dict())

    def test_structured_updates_are_deterministic_bounded_and_resolve_questions(self) -> None:
        state = create_agent_working_state("inspect repository")
        state = advance_agent_working_state(
            state,
            RuntimeAction(kind="search_files", action_id="search-1"),
            RuntimeObservation(
                action_id="search-1",
                action_kind="search_files",
                status="completed",
                summary="Found candidate files.",
            ),
            selected_paths=[],
            state_update=AgentStateUpdate(
                focus="  Locate   the parser ",
                add_findings=["README mentions parser", "readme mentions parser"],
                add_open_questions=["Where is parse implemented?"],
            ),
            expected_evidence="Paths containing parser symbols.",
        )
        state = advance_agent_working_state(
            state,
            RuntimeAction(kind="read_file", action_id="read-1"),
            RuntimeObservation(
                action_id="read-1",
                action_kind="read_file",
                status="completed",
                summary="Read main.py.",
            ),
            selected_paths=["main.py"],
            state_update=AgentStateUpdate(
                add_findings=[
                    "README mentions parser",
                    *[f"Finding {index}" for index in range(25)],
                ],
                resolve_open_questions=[" where IS parse implemented? "],
            ),
            expected_evidence="Parser implementation details.",
        )

        self.assertEqual(state.version, AGENT_WORKING_STATE_VERSION)
        self.assertEqual(state.focus, "Locate   the parser")
        self.assertEqual(len(state.findings), 20)
        self.assertEqual(state.findings[-1], "Finding 24")
        self.assertEqual(state.open_questions, [])
        self.assertEqual(state.expected_evidence, "Parser implementation details.")

    def test_version_one_record_defaults_new_fields_without_losing_compatibility(self) -> None:
        restored = agent_working_state_from_record(
            {
                "version": 1,
                "objective": "inspect repository",
                "phase": "exploration",
                "status": "running",
                "iteration": 1,
            }
        )

        self.assertEqual(restored.version, 1)
        self.assertEqual(restored.focus, "")
        self.assertEqual(restored.findings, [])
        self.assertEqual(restored.open_questions, [])
        self.assertEqual(restored.expected_evidence, "")
        self.assertEqual(restored.plan, [])
        self.assertEqual(restored.acceptance_criteria, [])
        self.assertEqual(restored.proposed_edits, [])

    def test_virtual_proposal_state_requires_latest_revision_inspection(self) -> None:
        state = create_agent_working_state("update parser")
        proposed = advance_agent_working_state(
            state,
            RuntimeAction(
                kind="propose_patch",
                arguments={"path": "src/main.py"},
                action_id="proposal-1",
            ),
            RuntimeObservation(
                action_id="proposal-1",
                action_kind="propose_patch",
                status="completed",
                summary="Prepared virtual edit.",
                data={
                    "proposal_status": "proposed",
                    "path": "src/main.py",
                    "base_sha256": "a" * 64,
                    "current_sha256": "b" * 64,
                    "revision": 1,
                    "hunk_count": 2,
                    "status": "proposed",
                    "inspected": False,
                },
            ),
            selected_paths=["src/main.py"],
        )

        self.assertEqual(proposed.version, AGENT_WORKING_STATE_VERSION)
        self.assertEqual(proposed.phase, "proposal")
        self.assertEqual(proposed.proposed_edits[0].revision, 1)
        self.assertIn(
            "proposal:src/main.py:uninspected",
            agent_completion_blockers(proposed),
        )
        self.assertNotIn("base content", str(proposed.to_dict()))

        inspected = advance_agent_working_state(
            proposed,
            RuntimeAction(kind="inspect_proposed_diff", action_id="inspect-1"),
            RuntimeObservation(
                action_id="inspect-1",
                action_kind="inspect_proposed_diff",
                status="completed",
                summary="Inspected virtual diff.",
                data={
                    "proposal_status": "inspected",
                    "files": [
                        {
                            **proposed.proposed_edits[0].to_dict(),
                            "status": "inspected",
                            "inspected": True,
                        }
                    ],
                },
            ),
            selected_paths=["src/main.py"],
        )

        self.assertEqual(inspected.phase, "proposal_review")
        self.assertTrue(inspected.proposed_edits[0].inspected)
        self.assertNotIn(
            "proposal:src/main.py:uninspected",
            agent_completion_blockers(inspected),
        )

        stale_revision = advance_agent_working_state(
            inspected,
            RuntimeAction(
                kind="propose_patch",
                arguments={"path": "src/main.py"},
                action_id="proposal-stale",
            ),
            RuntimeObservation(
                action_id="proposal-stale",
                action_kind="propose_patch",
                status="conflict",
                summary="Stale virtual revision.",
                data={"conflicts": [{"kind": "stale_virtual_revision"}]},
            ),
            selected_paths=["src/main.py"],
        )
        self.assertTrue(stale_revision.proposed_edits[0].inspected)

        stale_repository = advance_agent_working_state(
            stale_revision,
            RuntimeAction(
                kind="propose_patch",
                arguments={"path": "src/main.py"},
                action_id="proposal-disk-stale",
            ),
            RuntimeObservation(
                action_id="proposal-disk-stale",
                action_kind="propose_patch",
                status="conflict",
                summary="Stale real baseline.",
                data={"conflicts": [{"kind": "stale_repository"}]},
            ),
            selected_paths=["src/main.py"],
        )
        self.assertEqual(stale_repository.proposed_edits[0].status, "conflict")
        self.assertIn(
            "proposal:src/main.py:conflict",
            agent_completion_blockers(stale_repository),
        )

    def test_plan_and_acceptance_updates_require_completed_observation_evidence(self) -> None:
        state = create_agent_working_state("inspect repository")
        state = advance_agent_working_state(
            state,
            RuntimeAction(kind="read_file", action_id="read-1"),
            RuntimeObservation(
                action_id="read-1",
                action_kind="read_file",
                status="completed",
                summary="Read README.md.",
            ),
            selected_paths=["README.md"],
        )
        state = apply_agent_state_update(
            state,
            AgentStateUpdate(
                plan_updates=[
                    AgentPlanUpdate(
                        step_id="investigate_repository",
                        title="Investigate repository evidence",
                        detail="Read repository documentation.",
                        status="completed",
                        evidence_action_ids=["read-1"],
                    )
                ],
                acceptance_updates=[
                    AgentAcceptanceUpdate(
                        criterion_id="analysis_complete",
                        kind="analysis",
                        description="Repository evidence addresses the task.",
                        required=False,
                        evidence_action_ids=["read-1"],
                        evidence_summary="README.md was read successfully.",
                    )
                ],
            ),
        )

        self.assertTrue(agent_completion_ready(state))
        self.assertEqual(agent_completion_blockers(state), [])
        self.assertEqual(state.plan[0].status, "completed")
        self.assertEqual(state.plan[0].evidence_action_ids, ["read-1"])
        self.assertEqual(state.acceptance_criteria[0].status, "passed")
        self.assertTrue(state.acceptance_criteria[0].required)
        self.assertEqual(
            state.acceptance_criteria[0].evidence_action_ids,
            ["read-1"],
        )

    def test_unknown_or_finish_observations_cannot_be_completion_evidence(self) -> None:
        state = create_agent_working_state("inspect repository")
        state = advance_agent_working_state(
            state,
            RuntimeAction(kind="finish", action_id="finish-1"),
            RuntimeObservation(
                action_id="finish-1",
                action_kind="finish",
                status="completed",
                summary="Unsupported finish attempt.",
            ),
            selected_paths=[],
        )

        for action_id in ["missing-action", "finish-1"]:
            with self.subTest(action_id=action_id), self.assertRaises(ValueError):
                apply_agent_state_update(
                    state,
                    AgentStateUpdate(
                        plan_updates=[
                            AgentPlanUpdate(
                                step_id="investigate_repository",
                                title="Investigate repository evidence",
                                detail="Inspect repository evidence.",
                                status="completed",
                                evidence_action_ids=[action_id],
                            )
                        ]
                    ),
                )

    def test_search_candidates_are_not_completion_evidence(self) -> None:
        state = create_agent_working_state("inspect repository")
        state = advance_agent_working_state(
            state,
            RuntimeAction(kind="search_files", action_id="search-1"),
            RuntimeObservation(
                action_id="search-1",
                action_kind="search_files",
                status="completed",
                summary="Found README.md.",
            ),
            selected_paths=[],
        )

        with self.assertRaises(ValueError):
            apply_agent_state_update(
                state,
                AgentStateUpdate(
                    acceptance_updates=[
                        AgentAcceptanceUpdate(
                            criterion_id="analysis_complete",
                            kind="analysis",
                            description="Repository evidence addresses the task.",
                            required=True,
                            evidence_action_ids=["search-1"],
                            evidence_summary="README.md appeared in search.",
                        )
                    ]
                ),
            )

    def test_state_api_and_record_restore_reject_unsupported_completion(self) -> None:
        state = create_agent_working_state("inspect repository")

        with self.assertRaises(ValueError):
            apply_agent_state_update(
                state,
                AgentStateUpdate(
                    plan_updates=[
                        AgentPlanUpdate(
                            step_id="investigate_repository",
                            title="Investigate repository evidence",
                            detail="Inspect repository evidence.",
                            status="completed",
                            evidence_action_ids=[],
                        )
                    ]
                ),
            )

        restored = agent_working_state_from_record(
            {
                **state.to_dict(),
                "plan": [
                    {
                        "step_id": "unsupported",
                        "title": "Unsupported plan",
                        "detail": "No evidence exists.",
                        "status": "completed",
                        "evidence_action_ids": [],
                    }
                ],
                "acceptance_criteria": [
                    {
                        "criterion_id": "unsupported",
                        "kind": "analysis",
                        "description": "Unsupported acceptance claim.",
                        "required": True,
                        "status": "passed",
                        "evidence_action_ids": [],
                        "evidence_summary": "",
                    }
                ],
            }
        )

        self.assertEqual(restored.plan[0].status, "pending")
        self.assertEqual(restored.acceptance_criteria[0].status, "pending")
        self.assertFalse(agent_completion_ready(restored))

    def test_invalid_latest_snapshot_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = AgentRuntime(tmp, "inspect repository", run_id="invalid-state")
            expected = create_agent_working_state("inspect repository")
            runtime.record_working_state(expected)
            runtime.store.append_event(
                runtime.run_id,
                "working_state_updated",
                payload={"working_state": {"api_key": "invalid"}},
            )

            restored = latest_agent_working_state(runtime.events)

            self.assertEqual(restored, expected)

    def test_sqlite_runtime_restores_latest_working_state_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_path = Path(tmp) / "memory.sqlite3"
            store = SQLiteRuntimeStore(MemoryStore(memory_path))
            runtime = AgentRuntime(
                tmp,
                "inspect repository",
                run_id="state-run",
                store=store,
            )
            state = create_agent_working_state("inspect repository")
            runtime.record_working_state(state)
            advanced = advance_agent_working_state(
                state,
                RuntimeAction(kind="search_files", action_id="search-1"),
                RuntimeObservation(
                    action_id="search-1",
                    action_kind="search_files",
                    status="completed",
                    summary="Found two relevant files.",
                ),
                selected_paths=["src/main.py"],
            )
            runtime.record_working_state(advanced)

            reopened = AgentRuntime(
                tmp,
                "inspect repository",
                run_id="state-run",
                store=SQLiteRuntimeStore(MemoryStore(memory_path)),
            )

            self.assertEqual(reopened.working_state, advanced)
            state_events = [
                event
                for event in reopened.events
                if event.event_type == "working_state_updated"
            ]
            self.assertEqual(len(state_events), 2)


if __name__ == "__main__":
    unittest.main()
