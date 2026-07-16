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
    FileChangeProposal,
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
                                "expect": {"relevant_files": ["main.py"]},
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


if __name__ == "__main__":
    unittest.main()
