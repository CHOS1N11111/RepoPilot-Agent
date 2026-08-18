from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.evaluation import (
    EvalCase,
    EvalConfigurationError,
    EvalExpectations,
    evaluate_report,
    load_eval_cases,
    run_eval_suite,
    write_eval_report,
)
from repopilot_agent.models import (
    AgentStep,
    FileChangeProposal,
    FileEditProposal,
    LLMCallTrace,
    PatchProposal,
    PlanStep,
    SearchHit,
    ValidationResult,
    WorkflowReport,
)


class EvaluationTests(unittest.TestCase):
    def test_load_eval_cases_resolves_relative_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "fixture"
            repo.mkdir()
            (repo / "main.py").write_text("def parse():\n    return True\n", encoding="utf-8")
            suite = root / "suite.json"
            suite.write_text(
                json.dumps(
                    {
                        "suite": "sample",
                        "cases": [
                            {
                                "id": "parser",
                                "repo": "fixture",
                                "task": "fix parser",
                                "expect": {
                                    "relevant_files": ["main.py"],
                                    "expected_agent_actions": [],
                                    "required_agent_actions": ["finish"],
                                    "required_runtime_events": ["run_stopped"],
                                    "expected_stop_reason": "finished",
                                    "min_evidence_coverage": 0.5,
                                    "edit_files": ["main.py"],
                                    "patch_contains": ["return True"],
                                    "proposal_apply_ready": True,
                                    "max_tool_calls": 4,
                                    "max_unauthorized_side_effects": 0,
                                    "min_recovery_events": 0,
                                    "max_repair_cycles": 1,
                                    "expected_repair_stop_reason": "validation_passed",
                                    "max_llm_latency_ms": 1000,
                                    "max_total_tokens": 1000,
                                    "max_duration_ms": 5000
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            cases = load_eval_cases(suite)

            self.assertEqual(len(cases), 1)
            self.assertEqual(cases[0].case_id, "parser")
            self.assertEqual(cases[0].repo_path, repo.resolve())
            self.assertEqual(cases[0].expectations.relevant_files, ["main.py"])
            self.assertEqual(cases[0].expectations.expected_agent_actions, [])
            self.assertEqual(cases[0].expectations.min_evidence_coverage, 0.5)
            self.assertEqual(cases[0].expectations.edit_files, ["main.py"])

    def test_load_eval_cases_rejects_unknown_expectation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fixture").mkdir()
            suite = root / "suite.json"
            suite.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "id": "bad-case",
                                "repo": "fixture",
                                "task": "inspect parser",
                                "expect": {"unknown_metric": True},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(EvalConfigurationError) as context:
                load_eval_cases(suite)

            self.assertIn("unknown expectation", str(context.exception))

    def test_load_eval_cases_rejects_out_of_range_trajectory_fraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fixture").mkdir()
            suite = root / "suite.json"
            suite.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "id": "bad-fraction",
                                "repo": "fixture",
                                "task": "inspect parser",
                                "expect": {"min_evidence_coverage": 1.1},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(EvalConfigurationError) as context:
                load_eval_cases(suite)

            self.assertIn("number from 0 to 1", str(context.exception))

    def test_evaluate_report_scores_criteria_and_real_provider_calls(self) -> None:
        case = EvalCase(
            case_id="auth",
            description="Auth retrieval",
            task="fix auth token",
            repo_path=Path("fixture").resolve(),
            validation_commands=["python -m unittest"],
            expectations=EvalExpectations(
                relevant_files=["src/auth.py"],
                top_relevant_file="src/auth.py",
                proposal_files=["src/auth.py"],
                proposal_ready=True,
                min_plan_steps=2,
                validation_passed=True,
                max_llm_failures=0,
                max_fallbacks=1,
            ),
            source_path=Path("suite.json").resolve(),
        )
        report = WorkflowReport(
            task=case.task,
            repo_path=str(case.repo_path),
            files_scanned=2,
            relevant_files=[SearchHit("src/auth.py", 20, ["path match"], "def login():")],
            plan=[
                PlanStep(1, "Inspect", "Inspect auth"),
                PlanStep(2, "Validate", "Run tests"),
            ],
            patch_proposal=PatchProposal(
                objective="Fix auth",
                files=[FileChangeProposal("src/auth.py", "bugfix", "Fix token", ["Update logic"], "high")],
                risks=[],
                validation_suggestions=[],
                ready_for_patch=True,
            ),
            llm_traces=[
                LLMCallTrace(
                    name="planner",
                    model="fake",
                    prompt_preview="prompt",
                    raw_output="{}",
                    parsed=True,
                    latency_ms=17,
                ),
                LLMCallTrace(
                    name="patch_proposal",
                    model="fake",
                    prompt_preview="",
                    raw_output="",
                    parsed=False,
                    fallback_used=True,
                    error="fallback marker",
                    latency_ms=None,
                ),
            ],
            validation=[ValidationResult("python -m unittest", True, 0, "", "")],
        )

        result = evaluate_report(case, report, duration_ms=25)

        self.assertTrue(result.passed)
        self.assertEqual(result.score, 100.0)
        self.assertEqual(result.llm_calls, 1)
        self.assertEqual(result.llm_failures, 0)
        self.assertEqual(result.fallback_count, 1)
        self.assertEqual(result.llm_latency_ms, 17)
        self.assertEqual(result.relevant_file_recall, 1.0)

    def test_evaluate_report_scores_trajectory_patch_safety_and_cost(self) -> None:
        case = EvalCase(
            case_id="trajectory-auth",
            description="Trajectory quality",
            task="fix expired token",
            repo_path=Path("fixture").resolve(),
            validation_commands=["python -m unittest"],
            expectations=EvalExpectations(
                expected_agent_actions=["read_file", "finish"],
                required_agent_actions=["finish"],
                required_runtime_events=["action_completed", "run_stopped"],
                expected_stop_reason="finished",
                min_evidence_coverage=1.0,
                edit_files=["src/auth.py"],
                patch_contains=["expired"],
                proposal_apply_ready=True,
                max_tool_calls=2,
                max_unauthorized_side_effects=0,
                min_recovery_events=0,
                max_repair_cycles=0,
                max_llm_latency_ms=20,
                max_total_tokens=100,
                max_duration_ms=30,
            ),
            source_path=Path("suite.json").resolve(),
        )

        def event(sequence, event_type, action_id="", action_kind="", observation=None):
            payload = {}
            if action_kind:
                payload["action"] = {
                    "kind": action_kind,
                    "arguments": {},
                    "action_id": action_id,
                }
            if observation:
                payload["observation"] = observation
            if event_type == "run_stopped":
                payload["reason"] = "finished"
            return {
                "event_id": f"event-{sequence}",
                "run_id": "eval-runtime",
                "sequence": sequence,
                "event_type": event_type,
                "created_at": f"2026-08-18T00:00:0{sequence}+00:00",
                "action_id": action_id or None,
                "payload": payload,
            }

        events = [
            event(1, "run_started"),
            event(2, "decision_recorded", "read-1", "read_file"),
            event(3, "action_authorized", "read-1", "read_file"),
            event(4, "action_started", "read-1", "read_file"),
            event(
                5,
                "action_completed",
                "read-1",
                "read_file",
                {
                    "action_id": "read-1",
                    "action_kind": "read_file",
                    "status": "completed",
                    "summary": "Read authentication source.",
                },
            ),
            event(6, "decision_recorded", "finish-1", "finish"),
            event(7, "action_authorized", "finish-1", "finish"),
            event(8, "action_started", "finish-1", "finish"),
            event(
                9,
                "action_completed",
                "finish-1",
                "finish",
                {
                    "action_id": "finish-1",
                    "action_kind": "finish",
                    "status": "completed",
                    "summary": "Finished.",
                },
            ),
            event(10, "run_stopped"),
        ]
        report = WorkflowReport(
            task=case.task,
            repo_path=str(case.repo_path),
            files_scanned=2,
            patch_proposal=PatchProposal(
                objective="Reject expired tokens",
                files=[
                    FileChangeProposal(
                        "src/auth.py",
                        "bugfix",
                        "Check expiry",
                        ["Reject expired tokens"],
                        "high",
                    )
                ],
                risks=[],
                validation_suggestions=[],
                ready_for_patch=True,
                file_edits=[
                    FileEditProposal(
                        "src/auth.py",
                        "return active and not expired\n",
                        "Reject expired tokens.",
                    )
                ],
                proposed_diff="+ return active and not expired",
                apply_ready=True,
            ),
            agent_steps=[
                AgentStep(1, "read_file", "Inspect", "src/auth.py", "Read source"),
                AgentStep(2, "finish", "Done", "", "Finished"),
            ],
            agent_run_id="eval-runtime",
            agent_events=events,
            agent_state={
                "plan": [
                    {
                        "step_id": "inspect",
                        "status": "completed",
                        "evidence_action_ids": ["read-1"],
                    }
                ],
                "acceptance_criteria": [
                    {
                        "criterion_id": "analysis",
                        "status": "passed",
                        "evidence_action_ids": ["read-1"],
                    }
                ],
            },
            agent_stop_reason="finished",
            agent_completion_ready=True,
            llm_traces=[
                LLMCallTrace(
                    name="agent_step_1",
                    model="fake",
                    prompt_preview="inspect",
                    raw_output="{}",
                    parsed=True,
                    latency_ms=17,
                    input_tokens=75,
                    output_tokens=25,
                    total_tokens=100,
                )
            ],
        )

        result = evaluate_report(case, report, duration_ms=25)

        self.assertTrue(result.passed)
        self.assertEqual(result.score, 100.0)
        self.assertEqual(result.action_sequence, ["read_file", "finish"])
        self.assertEqual(result.stop_reason, "finished")
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual(result.evidence_coverage, 1.0)
        self.assertEqual(result.unauthorized_side_effects, 0)
        self.assertEqual(result.total_tokens, 100)
        self.assertEqual(result.token_source, "provider")
        self.assertEqual(len(result.trajectory_fingerprint), 64)

    def test_run_eval_suite_continues_after_case_error_and_disables_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "fixture"
            repo.mkdir()
            (repo / "main.py").write_text("def parse():\n    return True\n", encoding="utf-8")
            suite = root / "suite.json"
            suite.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "id": "broken",
                                "repo": "fixture",
                                "task": "broken workflow",
                                "expect": {"relevant_files": ["main.py"]},
                            },
                            {
                                "id": "working",
                                "repo": "fixture",
                                "task": "working workflow",
                                "expect": {"relevant_files": ["main.py"]},
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            calls: list[dict[str, object]] = []

            def fake_runner(repo_path, task, validation_commands, **kwargs):
                calls.append(kwargs)
                if task == "broken workflow":
                    raise RuntimeError("case failed")
                return WorkflowReport(
                    task=task,
                    repo_path=str(repo_path),
                    files_scanned=1,
                    relevant_files=[SearchHit("main.py", 10, ["match"], "def parse():")],
                )

            result = run_eval_suite(suite, workflow_runner=fake_runner)

            self.assertEqual(result.total_cases, 2)
            self.assertEqual(result.passed_cases, 1)
            self.assertEqual(result.failed_cases, 1)
            self.assertIn("case failed", result.cases[0].error or "")
            self.assertTrue(result.cases[1].passed)
            self.assertTrue(all(call["use_memory"] is False for call in calls))

    def test_write_eval_report_excludes_raw_llm_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "results" / "report.json"
            bundled = run_eval_suite(ROOT / "evals" / "cases")

            written = write_eval_report(bundled, output)
            payload = json.loads(written.read_text(encoding="utf-8"))

            self.assertEqual(payload["passed_cases"], 3)
            self.assertNotIn("prompt_preview", written.read_text(encoding="utf-8"))
            self.assertNotIn("raw_output", written.read_text(encoding="utf-8"))

    def test_write_eval_report_wraps_output_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocked_parent = Path(tmp) / "not-a-directory"
            blocked_parent.write_text("file", encoding="utf-8")
            bundled = run_eval_suite(ROOT / "evals" / "cases")

            with self.assertRaises(EvalConfigurationError) as context:
                write_eval_report(bundled, blocked_parent / "report.json")

            self.assertIn("Could not write evaluation report", str(context.exception))

    def test_bundled_core_suite_passes(self) -> None:
        result = run_eval_suite(ROOT / "evals" / "cases")

        self.assertTrue(result.passed)
        self.assertEqual(result.total_cases, 3)
        self.assertEqual(result.pass_rate, 100.0)
        self.assertEqual(result.average_relevant_file_recall, 1.0)
        self.assertEqual(result.average_proposal_file_recall, 1.0)

    def test_opt_in_llm_trajectory_suite_is_valid_but_not_in_default_cases(self) -> None:
        llm_cases = load_eval_cases(ROOT / "evals" / "llm_cases")
        default_cases = load_eval_cases(ROOT / "evals" / "cases")

        self.assertEqual([case.case_id for case in llm_cases], ["expired-token-agent-fix"])
        self.assertNotIn(llm_cases[0].case_id, {case.case_id for case in default_cases})
        self.assertEqual(
            llm_cases[0].repo_path,
            (ROOT / "evals" / "fixtures" / "trajectory_auth_bug").resolve(),
        )
        self.assertEqual(llm_cases[0].expectations.max_unauthorized_side_effects, 0)


if __name__ == "__main__":
    unittest.main()
