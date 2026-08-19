from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.agent_loop import run_agent_loop
from repopilot_agent.llm.base import LLMError, LLMMessage
from repopilot_agent.llm.schema import parse_patch_proposal_json
from repopilot_agent.models import MemoryContextItem, SearchHit
from repopilot_agent.patch_proposer import propose_patch_with_optional_llm
from repopilot_agent.planner import create_plan_with_optional_llm
from repopilot_agent.repository_instructions import discover_repository_instructions
from repopilot_agent.runtime import (
    AGENT_WORKING_STATE_VERSION,
    AgentRuntime,
    InMemoryRuntimeStore,
    RuntimeAction,
    create_agent_working_state,
)
from repopilot_agent.scanner import scan_repository
from repopilot_agent.workflow import run_workflow


def iterative_decision(
    kind: str,
    arguments: dict,
    rationale: str,
    expected_evidence: str,
    *,
    focus: str,
    findings: list[str] | None = None,
    open_questions: list[str] | None = None,
    resolved_questions: list[str] | None = None,
    finish_reason: str = "",
    plan_updates: list[dict] | None = None,
    acceptance_updates: list[dict] | None = None,
) -> str:
    return json.dumps(
        {
            "version": 2,
            "rationale": rationale,
            "action": {"kind": kind, "arguments": arguments},
            "expected_evidence": expected_evidence,
            "state_update": {
                "focus": focus,
                "add_findings": findings or [],
                "add_open_questions": open_questions or [],
                "resolve_open_questions": resolved_questions or [],
                "plan_updates": plan_updates or [],
                "acceptance_updates": acceptance_updates or [],
            },
            "finish_reason": finish_reason,
            "user_question": "",
        }
    )


class FakeLLMClient:
    def __init__(self, response: str | list[str], model: str = "fake-planner") -> None:
        self.responses = response if isinstance(response, list) else [response]
        self.model = model
        self.messages: list[LLMMessage] = []
        self.calls: list[list[LLMMessage]] = []

    def complete(self, messages: list[LLMMessage]) -> str:
        self.messages = messages
        self.calls.append(messages)
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


class LLMPlannerTests(unittest.TestCase):
    def test_patch_proposal_rejects_unsafe_file_change_paths(self) -> None:
        template = (
            '{{"objective":"Unsafe path","files":[{{"path":"{path}",'
            '"change_type":"bugfix","rationale":"Test path validation",'
            '"suggested_actions":["Inspect file"],"confidence":"high"}}],'
            '"risks":[],"validation_suggestions":[],"ready_for_patch":false,'
            '"file_edits":[]}}'
        )
        for path in ("../outside.py", "/absolute.py", "C:/absolute.py"):
            with self.subTest(path=path), self.assertRaises(LLMError) as raised:
                parse_patch_proposal_json(template.format(path=path))
            self.assertIn("Unsafe proposal path", str(raised.exception))

    def test_agent_refreshes_nested_instructions_after_selecting_a_path(self) -> None:
        client = FakeLLMClient(
            [
                iterative_decision(
                    "search_files",
                    {"query": "parser implementation"},
                    "Find the parser source.",
                    "A candidate parser path.",
                    focus="Locate parser code.",
                ),
                iterative_decision(
                    "read_file",
                    {"path": "src/parser.py"},
                    "Read the selected parser.",
                    "The parser implementation.",
                    focus="Inspect parser code.",
                ),
                iterative_decision(
                    "finish",
                    {"selected_paths": ["src/parser.py"]},
                    "The parser was inspected.",
                    "Evidence-backed completion.",
                    focus="Complete parser inspection.",
                    finish_reason="Parser behavior is understood.",
                    plan_updates=[
                        {
                            "step_id": "investigate_repository",
                            "title": "Investigate repository evidence",
                            "detail": "Read the parser source.",
                            "status": "completed",
                            "evidence_action_ids": ["explore-2"],
                        }
                    ],
                    acceptance_updates=[
                        {
                            "criterion_id": "analysis_complete",
                            "kind": "analysis",
                            "description": "Repository evidence addresses the task.",
                            "required": True,
                            "evidence_action_ids": ["explore-2"],
                            "evidence_summary": "src/parser.py was read.",
                        }
                    ],
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "parser.py").write_text(
                "def parse(value):\n    return value\n",
                encoding="utf-8",
            )
            (root / "AGENTS.md").write_text("Root instruction.", encoding="utf-8")
            (root / "src" / "AGENTS.md").write_text(
                "Nested source instruction.",
                encoding="utf-8",
            )
            result = run_agent_loop(
                "explain parser behavior",
                root,
                scan_repository(root),
                [],
                client,
                max_steps=3,
                repository_instruction_set=discover_repository_instructions(root),
            )

        self.assertEqual(result.stop_reason, "finished")
        self.assertIn("Root instruction.", client.calls[0][1].content)
        self.assertNotIn("Nested source instruction.", client.calls[0][1].content)
        self.assertNotIn("Nested source instruction.", client.calls[1][1].content)
        self.assertIn("Nested source instruction.", client.calls[2][1].content)

    def test_workflow_counts_each_parallel_read_member_as_a_tool_call(self) -> None:
        client = FakeLLMClient(
            [
                iterative_decision(
                    "parallel_read",
                    {
                        "actions": [
                            {"kind": "read_file", "arguments": {"path": "main.py"}},
                            {"kind": "read_file", "arguments": {"path": "test_main.py"}},
                        ]
                    },
                    "Read independent implementation and test files.",
                    "Both file bodies.",
                    focus="Compare implementation and tests.",
                ),
                iterative_decision(
                    "finish",
                    {"selected_paths": ["main.py", "test_main.py"]},
                    "Both files are understood.",
                    "Evidence-backed completion.",
                    focus="Summarize the repository.",
                    finish_reason="Implementation and tests were inspected.",
                    plan_updates=[
                        {
                            "step_id": "investigate_repository",
                            "title": "Investigate repository evidence",
                            "detail": "Read implementation and tests.",
                            "status": "completed",
                            "evidence_action_ids": ["explore-1"],
                        }
                    ],
                    acceptance_updates=[
                        {
                            "criterion_id": "analysis_complete",
                            "kind": "analysis",
                            "description": "Repository evidence addresses the task.",
                            "required": True,
                            "evidence_action_ids": ["explore-1"],
                            "evidence_summary": "Both files were read.",
                        }
                    ],
                ),
                '{"steps":[{"title":"Review behavior","detail":"Use both inspected files."}]}',
                '{"objective":"Explain behavior","files":[],"risks":[],'
                '"validation_suggestions":[],"ready_for_patch":false,"file_edits":[]}',
                '{"summary":"No patch required.","risk_level":"low","concerns":[],'
                '"suggested_tests":[],"approved_for_apply":false}',
            ],
            model="fake-parallel-workflow",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("value = 1\n", encoding="utf-8")
            (root / "test_main.py").write_text("def test_value(): pass\n", encoding="utf-8")
            report = run_workflow(
                root,
                "explain implementation and tests",
                use_llm=True,
                llm_client=client,
                iterative_agent=True,
                agent_max_steps=2,
            )

        self.assertEqual(report.execution_budget["usage"]["agent_steps"], 2)
        self.assertEqual(report.execution_budget["usage"]["tool_calls"], 3)
        self.assertEqual(report.agent_steps[0].tool_call_count, 2)
        self.assertEqual(
            report.agent_trajectory["metrics"]["action_sequence"],
            ["parallel_read", "finish"],
        )
        self.assertEqual(report.agent_trajectory["metrics"]["tool_calls"], 3)
        self.assertEqual(len(report.agent_trajectory["fingerprint"]), 64)
        self.assertEqual(
            report.agent_state["selected_paths"],
            ["main.py", "test_main.py"],
        )

    def test_workflow_resume_counts_only_new_runtime_tool_calls(self) -> None:
        store = InMemoryRuntimeStore()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text(
                "def parse(value):\n    return value\n",
                encoding="utf-8",
            )
            runtime = AgentRuntime(
                root,
                "inspect parser",
                run_id="workflow-resume-budget",
                store=store,
            )
            runtime.record_working_state(create_agent_working_state("inspect parser"))
            interrupted = RuntimeAction(
                kind="read_file",
                arguments={"path": "main.py"},
                action_id="explore-1",
                idempotency_key="explore-step-1",
            )
            runtime.record_decision(
                interrupted,
                json.loads(
                    iterative_decision(
                        "read_file",
                        {"path": "main.py"},
                        "Read the parser.",
                        "Current parser source.",
                        focus="Inspect parser behavior.",
                    )
                ),
            )
            store.reserve(runtime.run_id, interrupted)
            store.append_event(
                runtime.run_id,
                "action_started",
                action=interrupted,
                payload={"action": interrupted.to_dict()},
            )
            client = FakeLLMClient(
                [
                    iterative_decision(
                        "finish",
                        {"selected_paths": ["main.py"]},
                        "The recovered read is sufficient.",
                        "A completion observation.",
                        focus="Finish parser inspection.",
                        finish_reason="Parser source was recovered.",
                        plan_updates=[
                            {
                                "step_id": "investigate_repository",
                                "title": "Investigate repository evidence",
                                "detail": "Read the parser.",
                                "status": "completed",
                                "evidence_action_ids": ["explore-1"],
                            }
                        ],
                        acceptance_updates=[
                            {
                                "criterion_id": "analysis_complete",
                                "kind": "analysis",
                                "description": "Repository evidence addresses the parser task.",
                                "required": True,
                                "evidence_action_ids": ["explore-1"],
                                "evidence_summary": "main.py was recovered.",
                            }
                        ],
                    ),
                    '{"steps":[{"title":"Review parser","detail":"Use the recovered main.py evidence."}]}',
                    '{"objective":"No repository change is required","files":[],"risks":[],'
                    '"validation_suggestions":[],"ready_for_patch":false,"file_edits":[]}',
                    '{"summary":"No patch to review.","risk_level":"low","concerns":[],'
                    '"suggested_tests":[],"approved_for_apply":false}',
                ],
                model="fake-resume-budget",
            )

            report = run_workflow(
                root,
                "inspect parser",
                use_llm=True,
                llm_client=client,
                iterative_agent=True,
                agent_max_steps=1,
                agent_run_id=runtime.run_id,
                agent_event_store=store,
                resume_agent_runtime=True,
            )

        usage = report.execution_budget["usage"]
        self.assertEqual(usage["agent_steps"], 1)
        self.assertEqual(usage["tool_calls"], 2)
        self.assertEqual(report.agent_state["iteration"], 2)
        self.assertEqual(report.agent_runtime_recovery["next_step"], "stopped")

    def test_create_plan_with_llm_response(self) -> None:
        traces = []
        client = FakeLLMClient(
            '{"steps":[{"title":"Inspect parser","detail":"Review parser.py and identify failing branch."},'
            '{"title":"Add regression test","detail":"Capture the broken input before changing code."}]}'
        )
        client.last_usage = {
            "input_tokens": 120,
            "output_tokens": 30,
            "total_tokens": 150,
        }
        hits = [
            SearchHit(
                path="src/parser.py",
                score=12,
                reasons=["path matches 'parser'"],
                preview="def parse(value):",
            )
        ]

        plan, metadata = create_plan_with_optional_llm(
            "fix parser failure",
            hits,
            client,
            traces=traces,
            repository_map_context="src/parser.py [Python]\n  function parse(value) (line 1)",
        )

        self.assertEqual(metadata.source, "llm")
        self.assertEqual(metadata.model, "fake-planner")
        self.assertEqual(plan[0].title, "Inspect parser")
        self.assertIn("fix parser failure", client.messages[1].content)
        self.assertEqual(traces[0].name, "planner")
        self.assertTrue(traces[0].parsed)
        self.assertIn("Context budget summary", client.messages[1].content)
        self.assertIn("Task-relevant repository map", client.messages[1].content)
        self.assertIn("function parse(value)", client.messages[1].content)
        self.assertIn("src/parser.py", traces[0].context_summary)
        self.assertEqual(traces[0].input_tokens, 120)
        self.assertEqual(traces[0].output_tokens, 30)
        self.assertEqual(traces[0].total_tokens, 150)

    def test_create_plan_with_llm_includes_related_memory(self) -> None:
        client = FakeLLMClient(
            '{"steps":[{"title":"Reuse parser lesson","detail":"Check prior validation before editing."}]}'
        )
        memory = [
            MemoryContextItem(
                run_id="run-1",
                task="fix parser validation failure",
                summary="Previous parser fix used a focused parser test.",
                mode="run",
                created_at="2026-01-01T00:00:00+00:00",
                applied=True,
                score=8,
                reasons=["task overlap: parser, failure"],
                validation=["python -m unittest tests.test_parser: exit 0"],
            )
        ]

        plan, metadata = create_plan_with_optional_llm(
            "fix parser failure",
            [],
            client,
            memory_context=memory,
        )

        self.assertEqual(metadata.source, "llm")
        self.assertEqual(plan[0].title, "Reuse parser lesson")
        self.assertIn("Related memory:", client.messages[1].content)
        self.assertIn("fix parser validation failure", client.messages[1].content)
        self.assertIn("python -m unittest tests.test_parser: exit 0", client.messages[1].content)

    def test_create_plan_with_llm_separates_pinned_memory(self) -> None:
        client = FakeLLMClient(
            '{"steps":[{"title":"Use pinned lesson","detail":"Check pinned memory before changing code."}]}'
        )
        memory = [
            MemoryContextItem(
                run_id="run-1",
                task="document release checklist",
                summary="Pinned release workflow lesson.",
                mode="run",
                created_at="2026-01-01T00:00:00+00:00",
                applied=False,
                score=100,
                reasons=["pinned memory"],
                pinned=True,
            ),
            MemoryContextItem(
                run_id="run-2",
                task="fix parser validation failure",
                summary="Related parser lesson.",
                mode="run",
                created_at="2026-01-02T00:00:00+00:00",
                applied=True,
                score=8,
                reasons=["task overlap: parser"],
            ),
        ]

        create_plan_with_optional_llm("fix parser failure", [], client, memory_context=memory)

        prompt = client.messages[1].content
        self.assertIn("Pinned memory:", prompt)
        self.assertIn("document release checklist", prompt)
        self.assertIn("Related memory:", prompt)
        self.assertIn("fix parser validation failure", prompt)

    def test_invalid_llm_json_falls_back_to_rules(self) -> None:
        client = FakeLLMClient("not json")

        plan, metadata = create_plan_with_optional_llm("fix parser failure", [], client)

        self.assertEqual(metadata.source, "rules")
        self.assertTrue(metadata.fallback_used)
        self.assertIsNotNone(metadata.error)
        self.assertGreaterEqual(len(plan), 5)

    def test_workflow_falls_back_when_api_key_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("def run():\n    return True\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                report = run_workflow(root, "fix run behavior", use_llm=True, llm_model="fake-model")

        self.assertEqual(report.plan_metadata.source, "rules")
        self.assertTrue(report.plan_metadata.fallback_used)
        self.assertEqual(report.plan_metadata.model, "fake-model")
        self.assertIn("OPENAI_API_KEY", report.plan_metadata.error or "")
        self.assertEqual(report.patch_proposal_metadata.source, "rules")
        self.assertTrue(report.patch_proposal_metadata.fallback_used)
        self.assertIn("OPENAI_API_KEY", report.patch_proposal_metadata.error or "")

    def test_create_patch_proposal_with_llm_response(self) -> None:
        client = FakeLLMClient(
            '{"objective":"Fix parser failure safely","files":[{"path":"src/parser.py","change_type":"bugfix",'
            '"rationale":"Parser is the matched implementation point.","suggested_actions":["Guard empty input"],'
            '"confidence":"high"}],"risks":[{"level":"medium","message":"Parser behavior may affect callers.",'
            '"mitigation":"Run parser regression tests."}],"validation_suggestions":["python -m unittest discover -s tests"],'
            '"ready_for_patch":true}'
        )
        hits = [
            SearchHit(
                path="src/parser.py",
                score=12,
                reasons=["path matches 'parser'"],
                preview="def parse(value):",
            )
        ]
        plan, _ = create_plan_with_optional_llm("fix parser failure", hits, None)

        proposal, metadata = propose_patch_with_optional_llm("fix parser failure", hits, plan, client)

        self.assertEqual(metadata.source, "llm")
        self.assertEqual(proposal.objective, "Fix parser failure safely")
        self.assertEqual(proposal.files[0].path, "src/parser.py")
        self.assertEqual(proposal.files[0].confidence, "high")
        self.assertTrue(proposal.ready_for_patch)

    def test_patch_proposal_with_llm_file_edits_includes_diff(self) -> None:
        traces = []
        client = FakeLLMClient(
            '{"objective":"Fix parser failure safely","files":[{"path":"src/parser.py","change_type":"bugfix",'
            '"rationale":"Parser is the matched implementation point.","suggested_actions":["Guard empty input"],'
            '"confidence":"high"}],"risks":[],"validation_suggestions":["python -m unittest discover -s tests"],'
            '"ready_for_patch":true,"file_edits":[{"path":"src/parser.py",'
            '"new_content":"def parse(value):\\n    return value or \\"\\"\\n",'
            '"rationale":"Guard empty input."}]}'
        )
        hits = [
            SearchHit(
                path="src/parser.py",
                score=12,
                reasons=["path matches 'parser'"],
                preview="def parse(value):",
            )
        ]
        plan, _ = create_plan_with_optional_llm("fix parser failure", hits, None)

        proposal, metadata = propose_patch_with_optional_llm(
            "fix parser failure",
            hits,
            plan,
            client,
            file_contents={"src/parser.py": "def parse(value):\n    return value\n"},
            traces=traces,
            repository_map_context="src/parser.py [Python]\n  function parse(value) (line 1)",
        )

        self.assertEqual(metadata.source, "llm")
        self.assertTrue(proposal.apply_ready)
        self.assertEqual(proposal.file_edits[0].path, "src/parser.py")
        self.assertIn("--- a/src/parser.py", proposal.proposed_diff)
        self.assertIn("+    return value or \"\"", proposal.proposed_diff)
        self.assertEqual(traces[0].name, "patch_proposal")
        self.assertTrue(traces[0].parsed)
        self.assertIn("Files eligible for direct file_edits", client.messages[1].content)
        self.assertIn("Task-relevant repository map", client.messages[1].content)
        self.assertIn("function parse(value)", client.messages[1].content)
        self.assertIn("edit allowed", traces[0].context_summary)

    def test_patch_proposal_blocks_file_edits_when_context_is_truncated(self) -> None:
        traces = []
        client = FakeLLMClient(
            '{"objective":"Fix parser failure safely","files":[{"path":"src/parser.py","change_type":"bugfix",'
            '"rationale":"Parser is the matched implementation point.","suggested_actions":["Guard empty input"],'
            '"confidence":"high"}],"risks":[],"validation_suggestions":["python -m unittest discover -s tests"],'
            '"ready_for_patch":true,"file_edits":[{"path":"src/parser.py",'
            '"new_content":"def parse(value):\\n    return value or \\"\\"\\n",'
            '"rationale":"Guard empty input."}]}'
        )
        hits = [
            SearchHit(
                path="src/parser.py",
                score=12,
                reasons=["path matches 'parser'"],
                preview="def parse(value):",
            )
        ]
        plan, _ = create_plan_with_optional_llm("fix parser failure", hits, None)
        large_content = "\n".join(f"# filler {index}" for index in range(5_000))

        proposal, metadata = propose_patch_with_optional_llm(
            "fix parser failure",
            hits,
            plan,
            client,
            file_contents={"src/parser.py": large_content},
            traces=traces,
        )

        self.assertEqual(metadata.source, "llm")
        self.assertFalse(proposal.apply_ready)
        self.assertEqual(proposal.file_edits, [])
        self.assertEqual(proposal.proposed_diff, "")
        self.assertTrue(any("full file context" in risk.message for risk in proposal.risks))
        self.assertIn("none", client.messages[1].content)
        self.assertIn("truncated", traces[0].context_summary)

    def test_invalid_patch_proposal_json_falls_back_to_rules(self) -> None:
        client = FakeLLMClient("not json")

        proposal, metadata = propose_patch_with_optional_llm("fix parser failure", [], [], client)

        self.assertEqual(metadata.source, "rules")
        self.assertTrue(metadata.fallback_used)
        self.assertIsNotNone(metadata.error)
        self.assertFalse(proposal.ready_for_patch)

    def test_invalid_patch_proposal_fields_fall_back_to_rules(self) -> None:
        client = FakeLLMClient(
            '{"objective":"Fix parser","files":[{"path":"src/parser.py","change_type":"dangerous",'
            '"rationale":"Bad enum.","suggested_actions":["Do it"],"confidence":"high"}],'
            '"risks":[],"validation_suggestions":[],"ready_for_patch":true}'
        )
        hits = [
            SearchHit(
                path="src/parser.py",
                score=12,
                reasons=["path matches 'parser'"],
                preview="def parse(value):",
            )
        ]

        proposal, metadata = propose_patch_with_optional_llm("fix parser failure", hits, [], client)

        self.assertEqual(metadata.source, "rules")
        self.assertTrue(metadata.fallback_used)
        self.assertIn("Invalid change_type", metadata.error or "")
        self.assertTrue(proposal.ready_for_patch)

    def test_workflow_uses_llm_for_plan_and_patch_proposal(self) -> None:
        client = FakeLLMClient(
            [
                '{"steps":[{"title":"Inspect parser","detail":"Review parser behavior."}]}',
                '{"objective":"Fix parser failure safely","files":[{"path":"main.py","change_type":"bugfix",'
                '"rationale":"main.py contains the matched behavior.","suggested_actions":["Guard invalid input"],'
                '"confidence":"medium"}],"risks":[],"validation_suggestions":["python -m unittest discover -s tests"],'
                '"ready_for_patch":true,"file_edits":[{"path":"main.py","new_content":"def parse(value):\\n    return value or \\"\\"\\n",'
                '"rationale":"Guard invalid input."}]}',
                '{"summary":"The diff is focused.","risk_level":"low","concerns":[],'
                '"suggested_tests":["python -m unittest discover -s tests"],"approved_for_apply":true}',
            ],
            model="fake-combined",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("def parse(value):\n    return value\n", encoding="utf-8")
            report = run_workflow(root, "fix parse failure", use_llm=True, llm_client=client)

        self.assertEqual(report.plan_metadata.source, "llm")
        self.assertEqual(report.patch_proposal_metadata.source, "llm")
        self.assertEqual(report.patch_proposal.files[0].path, "main.py")
        self.assertIsNotNone(report.patch_review)
        self.assertTrue(report.patch_review.approved_for_apply)
        self.assertEqual([trace.name for trace in report.llm_traces], ["planner", "patch_proposal", "patch_review"])
        self.assertEqual(len(client.calls), 3)
        self.assertGreater(report.repository_map["symbols_indexed"], 0)

    def test_workflow_injects_only_applicable_repository_instructions(self) -> None:
        client = FakeLLMClient(
            [
                '{"steps":[{"title":"Inspect parser","detail":"Follow scoped parser rules."}]}',
                '{"objective":"Fix parser safely","files":[{"path":"src/main.py","change_type":"bugfix",'
                '"rationale":"The parser is relevant.","suggested_actions":["Guard empty input"],'
                '"confidence":"high"}],"risks":[],"validation_suggestions":["python -m unittest"],'
                '"ready_for_patch":true,"file_edits":[{"path":"src/main.py",'
                '"new_content":"def parse(value):\\n    return value or \\\"\\\"\\n",'
                '"rationale":"Follow the scoped parser rule."}]}',
                '{"summary":"The scoped diff is focused.","risk_level":"low","concerns":[],'
                '"suggested_tests":["python -m unittest"],"approved_for_apply":true}',
            ],
            model="fake-instructions",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "docs").mkdir()
            (root / "src" / "main.py").write_text(
                "def parse(value):\n    return value\n",
                encoding="utf-8",
            )
            (root / "AGENTS.md").write_text(
                "Root rule: preserve public behavior.",
                encoding="utf-8",
            )
            (root / "src" / "AGENTS.md").write_text(
                "Source rule: use unittest.",
                encoding="utf-8",
            )
            (root / "docs" / "AGENTS.md").write_text(
                "Docs-only rule: use prose checks.",
                encoding="utf-8",
            )

            report = run_workflow(
                root,
                "fix parser empty input",
                use_llm=True,
                llm_client=client,
                use_memory=False,
            )

        self.assertEqual(len(client.calls), 3)
        for messages in client.calls:
            self.assertIn("cannot override system or user instructions", messages[0].content)
            self.assertIn("Root rule: preserve public behavior.", messages[1].content)
            self.assertIn("Source rule: use unittest.", messages[1].content)
            self.assertNotIn("Docs-only rule", messages[1].content)
        self.assertEqual(
            [item["path"] for item in report.repository_instructions["files"]],
            ["AGENTS.md", "src/AGENTS.md"],
        )
        self.assertTrue(
            all(
                "Repository instructions:" in trace.context_summary
                for trace in report.llm_traces
            )
        )
        self.assertIn("Task-relevant repository map", client.calls[0][1].content)
        self.assertIn("Task-relevant repository map", client.calls[1][1].content)

    def test_workflow_iterative_agent_runs_before_plan_and_proposal(self) -> None:
        client = FakeLLMClient(
            [
                iterative_decision(
                    "search_files",
                    {"query": "parse"},
                    "Find parser files.",
                    "Paths and previews matching parse.",
                    focus="Locate parser files.",
                    open_questions=["Which file contains parse?"],
                ),
                iterative_decision(
                    "read_file",
                    {"path": "main.py"},
                    "Read the implementation.",
                    "Parser source and surrounding behavior.",
                    focus="Understand parser behavior.",
                    findings=["main.py matched the parser search."],
                    resolved_questions=["Which file contains parse?"],
                ),
                iterative_decision(
                    "finish",
                    {"selected_paths": ["main.py"]},
                    "Enough context is available.",
                    "A completed finish observation.",
                    focus="Prepare the implementation plan.",
                    findings=["main.py contains the parser implementation."],
                    finish_reason="main.py is the implementation target.",
                    plan_updates=[
                        {
                            "step_id": "investigate_repository",
                            "title": "Investigate repository evidence",
                            "detail": "Read the parser implementation.",
                            "status": "completed",
                            "evidence_action_ids": ["explore-2"],
                        }
                    ],
                    acceptance_updates=[
                        {
                            "criterion_id": "analysis_complete",
                            "kind": "analysis",
                            "description": "Repository evidence addresses the parser task.",
                            "required": True,
                            "evidence_action_ids": ["explore-2"],
                            "evidence_summary": "main.py was read successfully.",
                        }
                    ],
                ),
                '{"steps":[{"title":"Inspect parser","detail":"Review main.py parser behavior."}]}',
                '{"objective":"Fix parser failure safely","files":[{"path":"main.py","change_type":"bugfix",'
                '"rationale":"main.py contains the selected parser behavior.","suggested_actions":["Guard invalid input"],'
                '"confidence":"high"}],"risks":[],"validation_suggestions":["python -m unittest discover -s tests"],'
                '"ready_for_patch":true,"file_edits":[]}',
            ],
            model="fake-iterative",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("def parse(value):\n    return value\n", encoding="utf-8")
            (root / "README.md").write_text("General project docs\n", encoding="utf-8")
            memory = [
                MemoryContextItem(
                    run_id="pinned-agent-context",
                    task="prior parser investigation",
                    summary="Inspect parser callers before changing behavior.",
                    mode="run",
                    created_at="2026-01-01T00:00:00+00:00",
                    applied=True,
                    score=100,
                    reasons=["pinned memory"],
                    pinned=True,
                )
            ]
            with patch(
                "repopilot_agent.workflow.get_git_diff",
                side_effect=["diff --git a/main.py b/main.py\n+parser change", ""],
            ):
                report = run_workflow(
                    root,
                    "fix parse failure",
                    use_llm=True,
                    llm_client=client,
                    memory_context=memory,
                    iterative_agent=True,
                    agent_max_steps=3,
                )

        self.assertEqual([step.action for step in report.agent_steps], ["search_files", "read_file", "finish"])
        self.assertTrue(report.agent_run_id)
        self.assertEqual(report.agent_events[0].event_type, "run_started")
        self.assertEqual(report.agent_events[-1].event_type, "run_stopped")
        self.assertEqual(report.agent_state["status"], "completed")
        self.assertEqual(report.agent_stop_reason, "finished")
        self.assertEqual(report.agent_runtime_recovery["next_step"], "stopped")
        self.assertEqual(report.agent_runtime_recovery["working_state_iteration"], 3)
        self.assertEqual(report.agent_pending_question, "")
        self.assertTrue(report.agent_completion_ready)
        self.assertEqual(report.agent_completion_blockers, [])
        self.assertEqual(
            report.agent_state["version"],
            AGENT_WORKING_STATE_VERSION,
        )
        self.assertEqual(report.agent_state["proposed_edits"], [])
        self.assertEqual(report.agent_proposed_edits, [])
        self.assertEqual(report.agent_proposed_diff, "")
        self.assertEqual(report.agent_state["plan"][0]["status"], "completed")
        self.assertEqual(
            report.agent_state["acceptance_criteria"][0]["status"],
            "passed",
        )
        self.assertEqual(report.agent_state["iteration"], 3)
        self.assertEqual(report.agent_state["selected_paths"], ["main.py"])
        self.assertEqual(report.agent_state["focus"], "Prepare the implementation plan.")
        self.assertEqual(
            report.agent_state["findings"],
            [
                "main.py matched the parser search.",
                "main.py contains the parser implementation.",
            ],
        )
        self.assertEqual(report.agent_steps[0].expected_evidence, "Paths and previews matching parse.")
        self.assertEqual(report.relevant_files[0].path, "main.py")
        self.assertEqual(report.plan_metadata.source, "llm")
        self.assertEqual(report.patch_proposal_metadata.source, "llm")
        self.assertEqual(
            [trace.name for trace in report.llm_traces],
            ["agent_step_1", "agent_step_2", "agent_step_3", "planner", "patch_proposal"],
        )
        self.assertEqual(len(client.calls), 5)
        self.assertIn("Managed context packet:", client.calls[0][1].content)
        self.assertIn("prior parser investigation", client.calls[0][1].content)
        self.assertIn("diff --git a/main.py b/main.py", client.calls[0][1].content)
        self.assertIn("analysis_complete", client.calls[0][1].content)
        self.assertIn("function parse", client.calls[0][1].content)
        self.assertIn("- inspect_diff:", client.calls[0][1].content)
        self.assertNotIn("- ask_user:", client.calls[0][1].content)
        self.assertIn("Implementation plan:", client.calls[0][1].content)
        self.assertIn("Acceptance state:", client.calls[0][1].content)
        self.assertIn("explore-1", client.calls[1][1].content)
        self.assertIn("Completion ready: no", client.calls[1][1].content)
        self.assertIn("Agent plan and acceptance handoff:", client.calls[3][1].content)
        self.assertIn("Completion ready: yes", client.calls[3][1].content)
        self.assertIn("evidence: explore-2", client.calls[3][1].content.lower())
        self.assertIn("Agent context:", report.llm_traces[0].context_summary)
        self.assertIn("remaining_budget", report.llm_traces[0].context_summary)
        self.assertEqual(report.execution_budget["limits"]["max_agent_steps"], 3)
        self.assertEqual(report.execution_budget["usage"]["agent_steps"], 3)
        self.assertFalse(report.execution_budget["exhausted"])
        self.assertEqual(report.acceptance_criteria[0]["criterion_id"], "analysis_complete")
        self.assertEqual(report.completion_evidence["status"], "passed")


if __name__ == "__main__":
    unittest.main()
