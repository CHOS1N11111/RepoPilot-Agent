from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.agent_loop import run_agent_loop, select_agent_hits
from repopilot_agent.execution import AcceptanceCriterion, ExecutionBudget
from repopilot_agent.llm.base import LLMMessage
from repopilot_agent.models import AgentStep, MemoryContextItem, RepoFile, SearchHit
from repopilot_agent.runtime import (
    AgentRuntime,
    InMemoryRuntimeStore,
    RuntimeAction,
    RuntimeObservation,
    RuntimePolicy,
    STOPPING_OBSERVATION_STATUSES,
    create_agent_working_state,
    runtime_started_tool_call_count,
)


def decision(
    kind: str,
    arguments: dict,
    rationale: str,
    expected_evidence: str,
    *,
    focus: str = "",
    findings: list[str] | None = None,
    open_questions: list[str] | None = None,
    resolved_questions: list[str] | None = None,
    finish_reason: str = "",
    user_question: str = "",
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
            "user_question": user_question,
        }
    )


class FakeLLMClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.model = "fake-agent"
        self.calls: list[list[LLMMessage]] = []

    def complete(self, messages: list[LLMMessage]) -> str:
        self.calls.append(messages)
        return self.responses.pop(0)


class AgentLoopTests(unittest.TestCase):
    def test_parallel_read_uses_one_decision_and_charges_each_member(self) -> None:
        contents = {
            "src/main.py": "def parse(value):\n    return value\n",
            "tests/test_main.py": "def test_parse():\n    assert True\n",
        }
        files = [
            RepoFile(
                path=Path(path),
                relative_path=path,
                size_bytes=len(content.encode("utf-8")),
                language="python",
                content=content,
            )
            for path, content in contents.items()
        ]
        client = FakeLLMClient(
            [
                decision(
                    "parallel_read",
                    {
                        "actions": [
                            {"kind": "read_file", "arguments": {"path": "src/main.py"}},
                            {"kind": "read_file", "arguments": {"path": "tests/test_main.py"}},
                        ]
                    },
                    "Read independent implementation and test files together.",
                    "Both complete file bodies and hashes.",
                    focus="Compare implementation and tests.",
                ),
                decision(
                    "finish",
                    {"selected_paths": ["src/main.py", "tests/test_main.py"]},
                    "The relevant behavior and tests are understood.",
                    "Evidence-backed completion.",
                    focus="Summarize parser behavior.",
                    finish_reason="The parser and its tests were inspected.",
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
                            "description": "Repository evidence addresses the parser task.",
                            "required": True,
                            "evidence_action_ids": ["explore-1"],
                            "evidence_summary": "Implementation and tests were read.",
                        }
                    ],
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for path, content in contents.items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            result = run_agent_loop(
                "explain parser behavior",
                root,
                files,
                [],
                client,
                max_steps=2,
                execution_budget=ExecutionBudget(
                    max_agent_steps=2,
                    max_tool_calls=3,
                ),
            )

        self.assertEqual([step.action for step in result.steps], ["parallel_read", "finish"])
        self.assertEqual(result.steps[0].tool_call_count, 2)
        self.assertIn("[1] read_file [completed]", result.steps[0].observation)
        self.assertIn("[2] read_file [completed]", result.steps[0].observation)
        self.assertEqual(result.selected_paths, ["src/main.py", "tests/test_main.py"])
        self.assertEqual(result.stop_reason, "finished")
        self.assertEqual(runtime_started_tool_call_count(result.events), 3)

    def test_parallel_read_consumes_remaining_tools_before_another_llm_call(self) -> None:
        content = "value = 1\n"
        files = [
            RepoFile(Path("one.py"), "one.py", len(content), "python", content),
            RepoFile(Path("two.py"), "two.py", len(content), "python", content),
        ]
        client = FakeLLMClient(
            [
                decision(
                    "parallel_read",
                    {
                        "actions": [
                            {"kind": "read_file", "arguments": {"path": "one.py"}},
                            {"kind": "read_file", "arguments": {"path": "two.py"}},
                        ]
                    },
                    "Read both independent files.",
                    "Both file bodies.",
                ),
                decision(
                    "finish",
                    {"selected_paths": ["one.py", "two.py"]},
                    "This response must not be consumed.",
                    "A finish observation.",
                    finish_reason="This response must not be consumed.",
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "one.py").write_text(content, encoding="utf-8")
            (root / "two.py").write_text(content, encoding="utf-8")
            result = run_agent_loop(
                "inspect both files",
                root,
                files,
                [],
                client,
                max_steps=3,
                execution_budget=ExecutionBudget(
                    max_agent_steps=3,
                    max_tool_calls=2,
                ),
            )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result.stop_reason, "step_limit")
        self.assertIn("remaining tool-call budget", result.summary)
        self.assertEqual(runtime_started_tool_call_count(result.events), 2)

    def test_parallel_read_is_hidden_when_only_one_tool_call_remains(self) -> None:
        content = "value = 1\n"
        files = [RepoFile(Path("one.py"), "one.py", len(content), "python", content)]
        client = FakeLLMClient(
            [
                decision(
                    "read_file",
                    {"path": "one.py"},
                    "Read the only relevant file.",
                    "The file body.",
                )
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "one.py").write_text(content, encoding="utf-8")
            run_agent_loop(
                "inspect one file",
                root,
                files,
                [],
                client,
                max_steps=2,
                execution_budget=ExecutionBudget(
                    max_agent_steps=2,
                    max_tool_calls=1,
                ),
            )

        prompt = client.calls[0][1].content
        self.assertNotIn("- parallel_read:", prompt)

    def test_resume_retries_exact_read_only_action_then_calls_next_decision(self) -> None:
        content = "def parse(value):\n    return value\n"
        files = [
            RepoFile(
                path=Path("main.py"),
                relative_path="main.py",
                size_bytes=len(content.encode("utf-8")),
                language="python",
                content=content,
            )
        ]
        store = InMemoryRuntimeStore()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text(content, encoding="utf-8")
            runtime = AgentRuntime(
                root,
                "inspect parser",
                run_id="resume-exact-read",
                store=store,
            )
            runtime.record_working_state(create_agent_working_state("inspect parser"))
            action = RuntimeAction(
                kind="read_file",
                arguments={"path": "main.py"},
                action_id="explore-1",
                idempotency_key="explore-step-1",
            )
            runtime.record_decision(
                action,
                json.loads(
                    decision(
                        "read_file",
                        {"path": "main.py"},
                        "Read the parser implementation.",
                        "Current parser source.",
                    )
                ),
            )
            store.reserve(runtime.run_id, action)
            store.append_event(
                runtime.run_id,
                "action_started",
                action=action,
                payload={"action": action.to_dict()},
            )
            client = FakeLLMClient(
                [
                    decision(
                        "finish",
                        {"selected_paths": ["main.py"]},
                        "The recovered file observation is sufficient.",
                        "A completion observation.",
                        finish_reason="Parser source was recovered without restarting exploration.",
                        plan_updates=[
                            {
                                "step_id": "investigate_repository",
                                "title": "Investigate repository evidence",
                                "detail": "Read the parser implementation.",
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
                                "evidence_summary": "Recovered main.py content identifies parse.",
                            }
                        ],
                    )
                ]
            )

            result = run_agent_loop(
                "inspect parser",
                root,
                files,
                [],
                client,
                max_steps=1,
                runtime_run_id=runtime.run_id,
                runtime_store=store,
                resume_existing_state=True,
            )

        self.assertEqual(result.stop_reason, "finished")
        self.assertEqual([step.order for step in result.steps], [2])
        self.assertEqual(len(client.calls), 1)
        self.assertIn("def parse", client.calls[0][1].content)
        decision_ids = [
            event.action_id
            for event in store.list_events("resume-exact-read")
            if event.event_type == "decision_recorded"
        ]
        self.assertEqual(decision_ids, ["explore-1", "explore-2"])
        self.assertIn(
            "action_recovered",
            [event.event_type for event in store.list_events("resume-exact-read")],
        )

    def test_recovered_read_consumes_tool_budget_before_next_llm_decision(self) -> None:
        content = "def parse(value):\n    return value\n"
        files = [
            RepoFile(
                path=Path("main.py"),
                relative_path="main.py",
                size_bytes=len(content.encode("utf-8")),
                language="python",
                content=content,
            )
        ]
        store = InMemoryRuntimeStore()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text(content, encoding="utf-8")
            runtime = AgentRuntime(
                root,
                "inspect parser",
                run_id="resume-read-budget",
                store=store,
            )
            runtime.record_working_state(create_agent_working_state("inspect parser"))
            action = RuntimeAction(
                kind="read_file",
                arguments={"path": "main.py"},
                action_id="explore-1",
                idempotency_key="explore-step-1",
            )
            runtime.record_decision(
                action,
                json.loads(
                    decision(
                        "read_file",
                        {"path": "main.py"},
                        "Read the parser implementation.",
                        "Current parser source.",
                    )
                ),
            )
            store.reserve(runtime.run_id, action)
            store.append_event(
                runtime.run_id,
                "action_started",
                action=action,
                payload={"action": action.to_dict()},
            )
            client = FakeLLMClient([])

            result = run_agent_loop(
                "inspect parser",
                root,
                files,
                [],
                client,
                max_steps=2,
                runtime_run_id=runtime.run_id,
                runtime_store=store,
                execution_budget=ExecutionBudget(
                    max_agent_steps=2,
                    max_tool_calls=1,
                    max_validation_commands=1,
                    max_elapsed_seconds=60,
                ),
                resume_existing_state=True,
            )

        self.assertEqual(client.calls, [])
        self.assertEqual(result.stop_reason, "step_limit")
        self.assertIn("remaining tool-call budget", result.summary)
        self.assertEqual(result.working_state.iteration, 1)
        self.assertEqual(
            [
                event.action_id
                for event in store.list_events("resume-read-budget")
                if event.event_type == "decision_recorded"
            ],
            ["explore-1"],
        )

    def test_resume_ambiguous_write_stops_before_llm_or_replay(self) -> None:
        content = "before\n"
        files = [
            RepoFile(
                path=Path("notes.txt"),
                relative_path="notes.txt",
                size_bytes=len(content.encode("utf-8")),
                language="text",
                content=content,
            )
        ]
        store = InMemoryRuntimeStore()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "notes.txt"
            target.write_text(content, encoding="utf-8")
            runtime = AgentRuntime(
                root,
                "edit notes",
                run_id="resume-ambiguous-write",
                store=store,
                policy=RuntimePolicy.sandboxed(allowed_edit_paths=["notes.txt"]),
            )
            runtime.record_working_state(create_agent_working_state("edit notes"))
            action = RuntimeAction(
                kind="edit_file",
                arguments={"path": "notes.txt", "new_content": "after\n"},
                action_id="explore-1",
                idempotency_key="explore-step-1",
            )
            store.reserve(runtime.run_id, action)
            store.append_event(
                runtime.run_id,
                "action_started",
                action=action,
                payload={"action": action.to_dict()},
            )
            client = FakeLLMClient([])

            result = run_agent_loop(
                "edit notes",
                root,
                files,
                [],
                client,
                max_steps=1,
                runtime_run_id=runtime.run_id,
                runtime_store=store,
                resume_existing_state=True,
            )

            self.assertEqual(result.stop_reason, "recovery_confirmation_required")
            self.assertEqual(client.calls, [])
            self.assertTrue(result.runtime_recovery["requires_confirmation"])
            self.assertEqual(target.read_text(encoding="utf-8"), content)

    def test_agent_loop_continues_existing_state_with_unique_action_ids(self) -> None:
        content = "def parse(value):\n    return value\n"
        files = [
            RepoFile(
                path=Path("main.py"),
                relative_path="main.py",
                size_bytes=len(content.encode("utf-8")),
                language="python",
                content=content,
            )
        ]
        store = InMemoryRuntimeStore()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text(content, encoding="utf-8")
            first = run_agent_loop(
                "inspect parser",
                root,
                files,
                [],
                FakeLLMClient(
                    [
                        decision(
                            "search_files",
                            {"query": "parse"},
                            "Locate parser candidates.",
                            "Candidate parser paths.",
                        )
                    ]
                ),
                max_steps=1,
                runtime_run_id="continued-agent",
                runtime_store=store,
            )
            continuation_client = FakeLLMClient(
                [
                    decision(
                        "read_file",
                        {"path": "main.py"},
                        "Read the parser after validation.",
                        "Current parser content.",
                    ),
                    decision(
                        "finish",
                        {"selected_paths": ["main.py"]},
                        "The validated parser is understood.",
                        "A completion observation.",
                        finish_reason="Parser inspection is complete.",
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
                                "evidence_summary": "main.py was read after validation.",
                            }
                        ],
                    ),
                ]
            )
            continued = run_agent_loop(
                "inspect parser",
                root,
                files,
                [],
                continuation_client,
                max_steps=2,
                runtime_run_id="continued-agent",
                runtime_store=store,
                resume_existing_state=True,
                prior_steps=[
                    AgentStep(
                        order=1,
                        action="validate",
                        thought="Run the approved validation.",
                        tool_input="python -m unittest",
                        observation="VALIDATION-EVIDENCE: all parser tests passed.",
                    )
                ],
            )

        self.assertEqual(first.working_state.iteration, 1)
        self.assertEqual([step.order for step in continued.steps], [2, 3])
        self.assertEqual(continued.stop_reason, "finished")
        self.assertEqual(continued.working_state.iteration, 3)
        self.assertIn(
            "VALIDATION-EVIDENCE",
            continuation_client.calls[0][1].content,
        )
        decision_ids = [
            event.action_id
            for event in store.list_events("continued-agent")
            if event.event_type == "decision_recorded"
        ]
        self.assertEqual(decision_ids, ["explore-1", "explore-2", "explore-3"])

    def test_empty_repository_keeps_opt_in_write_loop_read_only(self) -> None:
        client = FakeLLMClient(
            [
                decision(
                    "search_files",
                    {"query": "bootstrap"},
                    "Check whether the empty repository has any candidates.",
                    "An empty structured search result.",
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = run_agent_loop(
                "bootstrap the repository",
                Path(tmp),
                [],
                [],
                client,
                max_steps=1,
                allow_write_actions=True,
            )

        self.assertEqual(result.stop_reason, "step_limit")
        self.assertEqual(result.pending_approval, {})
        self.assertNotIn("apply_patch {", client.calls[0][0].content)

    def test_agent_loop_searches_reads_and_finishes(self) -> None:
        files = [
            RepoFile(
                path=Path("main.py"),
                relative_path="main.py",
                size_bytes=32,
                language="python",
                content="def parse(value):\n    return value\n",
            ),
            RepoFile(
                path=Path("README.md"),
                relative_path="README.md",
                size_bytes=20,
                language="markdown",
                content="Project documentation\n",
            ),
        ]
        initial_hits = [
            SearchHit(path="README.md", score=5, reasons=["important project file"], preview="Project documentation")
        ]
        client = FakeLLMClient(
            [
                decision(
                    "search_files",
                    {"query": "parse"},
                    "Find parser code.",
                    "Paths and previews that mention parse.",
                    focus="Locate the parser implementation.",
                    open_questions=["Which file implements parse?"],
                ),
                decision(
                    "read_file",
                    {"path": "main.py"},
                    "Read the parser implementation.",
                    "The implementation and surrounding behavior in main.py.",
                    focus="Understand parser behavior.",
                    findings=["main.py matched the parse search."],
                    resolved_questions=["Which file implements parse?"],
                    open_questions=["How does parse handle values?"],
                ),
                decision(
                    "finish",
                    {"selected_paths": ["main.py"]},
                    "Enough context is available.",
                    "A completed finish observation with main.py selected.",
                    focus="Summarize the parser target.",
                    findings=["main.py defines parse and returns the provided value."],
                    resolved_questions=["How does parse handle values?"],
                    finish_reason="main.py contains the parser behavior.",
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
            ]
        )
        traces = []
        memory = [
            MemoryContextItem(
                run_id="pinned",
                task="prior parser task",
                summary="Use a parser regression test.",
                mode="run",
                created_at="2026-01-01T00:00:00+00:00",
                applied=True,
                score=100,
                reasons=["pinned memory"],
                pinned=True,
            )
        ]

        with tempfile.TemporaryDirectory() as tmp:
            result = run_agent_loop(
                "fix parse behavior",
                tmp,
                files,
                initial_hits,
                client,
                traces=traces,
                max_steps=3,
                memory_context=memory,
                current_diff="+OPENAI_API_KEY=must-not-reach-the-model",
                acceptance_criteria=[
                    AcceptanceCriterion(
                        "analysis_complete",
                        "analysis",
                        "Understand parser behavior.",
                    )
                ],
            )

        self.assertEqual([step.action for step in result.steps], ["search_files", "read_file", "finish"])
        self.assertEqual(result.steps[0].selected_paths, [])
        self.assertEqual(result.steps[1].selected_paths, ["main.py"])
        self.assertEqual(result.selected_paths, ["main.py"])
        self.assertIn("parser behavior", result.summary)
        self.assertEqual(len(client.calls), 3)
        self.assertTrue(result.runtime_run_id)
        self.assertEqual(result.events[0].event_type, "run_started")
        self.assertEqual(result.events[-1].event_type, "run_stopped")
        self.assertIsNotNone(result.working_state)
        self.assertEqual(result.working_state.status, "completed")
        self.assertEqual(result.working_state.iteration, 3)
        self.assertEqual(result.working_state.selected_paths, ["main.py"])
        self.assertEqual(result.working_state.focus, "Summarize the parser target.")
        self.assertEqual(
            result.working_state.findings,
            [
                "main.py matched the parse search.",
                "main.py defines parse and returns the provided value.",
            ],
        )
        self.assertEqual(result.working_state.open_questions, [])
        self.assertIn("finish observation", result.working_state.expected_evidence)
        self.assertIn("Paths and previews", result.steps[0].expected_evidence)
        self.assertEqual(
            result.steps[1].state_update["add_findings"],
            ["main.py matched the parse search."],
        )
        self.assertEqual(result.steps[2].finish_reason, result.summary)
        self.assertEqual(result.stop_reason, "finished")
        self.assertEqual(result.pending_question, "")
        event_types = [event.event_type for event in result.events]
        self.assertEqual(event_types.count("decision_recorded"), 3)
        self.assertEqual(event_types.count("action_authorized"), 3)
        state_events = [
            event for event in result.events if event.event_type == "working_state_updated"
        ]
        self.assertEqual(len(state_events), 4)
        self.assertNotIn("def parse", json.dumps([event.payload for event in state_events]))
        self.assertIn("Managed context packet:", client.calls[0][1].content)
        self.assertIn("## Agent Working State", client.calls[0][1].content)
        self.assertIn("## Pinned Memory", client.calls[0][1].content)
        self.assertIn("prior parser task", client.calls[0][1].content)
        self.assertIn("Understand parser behavior", client.calls[0][1].content)
        self.assertIn("Agent steps: 3 remaining", client.calls[0][1].content)
        self.assertIn("Agent steps: 2 remaining", client.calls[1][1].content)
        self.assertIn("[REDACTED]", client.calls[0][1].content)
        self.assertNotIn("must-not-reach-the-model", client.calls[0][1].content)
        self.assertIn("Iteration: 0", client.calls[0][1].content)
        self.assertIn("Iteration: 1", client.calls[1][1].content)
        self.assertIn('"version":2', client.calls[0][0].content)
        self.assertIn('"plan_updates"', client.calls[0][0].content)
        self.assertIn('"acceptance_updates"', client.calls[0][0].content)
        self.assertIn("Expected evidence:", client.calls[1][1].content)
        self.assertEqual(len(traces), 3)
        self.assertIn("Agent context:", traces[0].context_summary)
        self.assertIn("working_state", traces[0].context_summary)

    def test_select_agent_hits_prioritizes_selected_paths(self) -> None:
        files = [
            RepoFile(Path("main.py"), "main.py", 10, "python", "def parse(value):\n    return value\n"),
            RepoFile(Path("README.md"), "README.md", 10, "markdown", "docs\n"),
        ]
        hits = [SearchHit(path="README.md", score=10, reasons=["important project file"], preview="docs")]

        selected = select_agent_hits(hits, files, ["main.py"], limit=2)

        self.assertEqual(selected[0].path, "main.py")
        self.assertEqual(selected[0].reasons, ["selected by iterative agent"])
        self.assertEqual(selected[1].path, "README.md")

    def test_step_limit_state_keeps_fallback_selected_paths(self) -> None:
        files = [
            RepoFile(Path("README.md"), "README.md", 10, "markdown", "docs\n"),
        ]
        hits = [
            SearchHit(
                path="README.md",
                score=10,
                reasons=["important project file"],
                preview="docs",
            )
        ]
        client = FakeLLMClient(
            [
                decision(
                    "search_files",
                    {"query": "missing"},
                    "Search once.",
                    "Any repository file matching missing.",
                    focus="Search documentation.",
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = run_agent_loop(
                "inspect docs",
                tmp,
                files,
                hits,
                client,
                max_steps=3,
                execution_budget=ExecutionBudget(
                    max_agent_steps=1,
                    max_tool_calls=1,
                ),
            )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result.selected_paths, ["README.md"])
        self.assertEqual(result.working_state.selected_paths, ["README.md"])
        self.assertEqual(result.working_state.status, "stopped")
        self.assertEqual(result.working_state.stop_reason, "step_limit")
        self.assertEqual(result.stop_reason, "step_limit")

    def test_agent_loop_inspects_diff_through_the_unified_runtime_cycle(self) -> None:
        client = FakeLLMClient(
            [
                decision(
                    "inspect_diff",
                    {"staged": True},
                    "Inspect the staged implementation change.",
                    "A bounded staged diff.",
                    focus="Review staged changes.",
                    findings=["A staged change needs inspection."],
                ),
                decision(
                    "finish",
                    {"selected_paths": ["main.py"]},
                    "The staged change is understood.",
                    "A completed finish observation.",
                    focus="Summarize the staged change.",
                    finish_reason="The staged change updates main.py.",
                    plan_updates=[
                        {
                            "step_id": "investigate_repository",
                            "title": "Investigate repository evidence",
                            "detail": "Inspect the staged diff.",
                            "status": "completed",
                            "evidence_action_ids": ["explore-1"],
                        }
                    ],
                    acceptance_updates=[
                        {
                            "criterion_id": "analysis_complete",
                            "kind": "analysis",
                            "description": "Repository evidence addresses the staged change.",
                            "required": True,
                            "evidence_action_ids": ["explore-1"],
                            "evidence_summary": "The staged diff was inspected.",
                        }
                    ],
                ),
            ]
        )
        files = [RepoFile(Path("main.py"), "main.py", 12, "python", "value = 2\n")]

        with tempfile.TemporaryDirectory() as tmp, patch(
            "repopilot_agent.runtime.tools.get_git_diff",
            return_value="diff --git a/main.py b/main.py\n+value = 2",
        ):
            result = run_agent_loop(
                "review staged change",
                tmp,
                files,
                [],
                client,
                max_steps=2,
            )

        self.assertEqual([step.action for step in result.steps], ["inspect_diff", "finish"])
        self.assertIn("Staged diff", result.steps[0].observation)
        self.assertIn("+value = 2", result.steps[0].observation)
        self.assertEqual(result.stop_reason, "finished")
        event_types = [event.event_type for event in result.events]
        self.assertEqual(event_types.count("decision_recorded"), 2)
        self.assertEqual(event_types.count("action_authorized"), 2)
        self.assertEqual(event_types.count("action_started"), 2)
        self.assertEqual(event_types.count("action_completed"), 2)

    def test_agent_loop_stops_and_exposes_a_pending_user_question(self) -> None:
        question = "Which parser behavior should remain backward compatible?"
        client = FakeLLMClient(
            [
                decision(
                    "ask_user",
                    {},
                    "The repository does not define the compatibility requirement.",
                    "A user-provided compatibility requirement.",
                    focus="Clarify parser compatibility.",
                    open_questions=[question],
                    user_question=question,
                )
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = run_agent_loop(
                "change parser behavior",
                tmp,
                [],
                [],
                client,
                max_steps=3,
                allow_user_questions=True,
            )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result.stop_reason, "input_required")
        self.assertEqual(result.pending_question, question)
        self.assertEqual(result.steps[0].user_question, question)
        self.assertEqual(result.working_state.status, "waiting")
        self.assertEqual(result.working_state.phase, "input")
        self.assertEqual(result.working_state.stop_reason, "input_required")
        self.assertEqual(result.working_state.open_questions, [question])
        self.assertEqual(
            [event.event_type for event in result.events],
            [
                "run_started",
                "working_state_updated",
                "decision_recorded",
                "action_authorized",
                "input_required",
                "working_state_updated",
                "run_stopped",
            ],
        )

    def test_agent_loop_stops_after_a_failed_tool_observation(self) -> None:
        client = FakeLLMClient(
            [
                decision(
                    "inspect_diff",
                    {},
                    "Inspect the current working-tree changes.",
                    "A bounded working-tree diff.",
                ),
                decision(
                    "finish",
                    {"selected_paths": []},
                    "This response must not be consumed.",
                    "A finish observation.",
                    finish_reason="This response must not be consumed.",
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp, patch(
            "repopilot_agent.runtime.tools.get_git_diff",
            side_effect=RuntimeError("diff unavailable"),
        ):
            result = run_agent_loop(
                "inspect changes",
                tmp,
                [],
                [],
                client,
                max_steps=2,
            )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result.stop_reason, "failed")
        self.assertIn("diff unavailable", result.summary)
        self.assertEqual(result.working_state.status, "failed")

    def test_finish_is_blocked_until_plan_and_acceptance_have_observation_evidence(self) -> None:
        files = [RepoFile(Path("README.md"), "README.md", 8, "markdown", "# Docs\n")]
        client = FakeLLMClient(
            [
                decision(
                    "finish",
                    {"selected_paths": ["README.md"]},
                    "Attempt to finish before collecting evidence.",
                    "A finish decision guarded by acceptance state.",
                    finish_reason="The repository is understood.",
                ),
                decision(
                    "read_file",
                    {"path": "README.md"},
                    "Collect evidence for the task.",
                    "The repository documentation.",
                ),
                decision(
                    "finish",
                    {"selected_paths": ["README.md"]},
                    "Finish with cited repository evidence.",
                    "A completed evidence-backed finish observation.",
                    finish_reason="README.md documents the repository.",
                    plan_updates=[
                        {
                            "step_id": "investigate_repository",
                            "title": "Investigate repository evidence",
                            "detail": "Read the repository documentation.",
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
                            "evidence_summary": "README.md was read successfully.",
                        }
                    ],
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = run_agent_loop(
                "explain repository",
                tmp,
                files,
                [],
                client,
                max_steps=3,
            )

        self.assertEqual([step.action for step in result.steps], ["finish", "read_file", "finish"])
        self.assertEqual(result.steps[0].observation.split(":", 1)[0], "Finish blocked until plan and acceptance evidence are complete")
        self.assertEqual(result.stop_reason, "finished")
        self.assertEqual(result.working_state.plan[0].status, "completed")
        self.assertEqual(result.working_state.acceptance_criteria[0].status, "passed")
        event_types = [event.event_type for event in result.events]
        self.assertEqual(event_types.count("finish_blocked"), 1)
        self.assertEqual(event_types.count("action_authorized"), 2)
        authorized_action_ids = [
            event.action_id
            for event in result.events
            if event.event_type == "action_authorized"
        ]
        self.assertNotIn("explore-1", authorized_action_ids)
        self.assertEqual(authorized_action_ids, ["explore-2", "explore-3"])

    def test_agent_proposes_inspects_and_finishes_without_writing_repository(self) -> None:
        original = "def value():\n    return 1\n"
        expected_sha256 = hashlib.sha256(original.encode("utf-8")).hexdigest()
        client = FakeLLMClient(
            [
                decision(
                    "read_file",
                    {"path": "main.py"},
                    "Read the edit target and capture its baseline hash.",
                    "Current source content and SHA-256.",
                ),
                decision(
                    "propose_patch",
                    {
                        "path": "main.py",
                        "expected_sha256": expected_sha256,
                        "hunks": [
                            {
                                "old_text": "return 1",
                                "new_text": "return 2",
                            }
                        ],
                    },
                    "Prepare the requested edit in the virtual overlay.",
                    "A syntax-checked virtual revision and cumulative diff.",
                    plan_updates=[
                        {
                            "step_id": "investigate_repository",
                            "title": "Investigate repository evidence",
                            "detail": "Read the implementation target.",
                            "status": "completed",
                            "evidence_action_ids": ["explore-1"],
                        }
                    ],
                    acceptance_updates=[
                        {
                            "criterion_id": "analysis_complete",
                            "kind": "analysis",
                            "description": "Repository evidence supports the requested edit.",
                            "required": True,
                            "evidence_action_ids": ["explore-1"],
                            "evidence_summary": "main.py was read successfully.",
                        }
                    ],
                ),
                decision(
                    "finish",
                    {"selected_paths": ["main.py"]},
                    "Attempt completion before reviewing the virtual diff.",
                    "The proposal review gate should block completion.",
                    finish_reason="The virtual edit is prepared.",
                ),
                decision(
                    "inspect_proposed_diff",
                    {},
                    "Review the latest cumulative virtual diff.",
                    "An inspected diff with a fresh real baseline.",
                ),
                decision(
                    "finish",
                    {"selected_paths": ["main.py"]},
                    "Complete after evidence and proposal review are satisfied.",
                    "A completed finish observation.",
                    finish_reason="main.py has a reviewed virtual edit.",
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "main.py")
            target.write_text(original, encoding="utf-8")
            files = [
                RepoFile(
                    target,
                    "main.py",
                    len(original.encode("utf-8")),
                    "python",
                    original,
                )
            ]

            result = run_agent_loop(
                "change value to 2",
                tmp,
                files,
                [],
                client,
                max_steps=5,
            )

            self.assertEqual(target.read_text(encoding="utf-8"), original)

        self.assertEqual(
            [step.action for step in result.steps],
            ["read_file", "propose_patch", "finish", "inspect_proposed_diff", "finish"],
        )
        self.assertIn("proposed-edit review", result.steps[2].observation)
        self.assertEqual(result.stop_reason, "finished")
        self.assertTrue(result.working_state.proposed_edits[0].inspected)
        self.assertEqual(result.proposed_edits[0]["status"], "inspected")
        self.assertIn("+    return 2", result.proposed_diff)
        self.assertIn(expected_sha256, client.calls[1][1].content)
        self.assertIn("Virtual proposed edits:", client.calls[4][1].content)
        self.assertEqual(
            [event.event_type for event in result.events].count("finish_blocked"),
            1,
        )

    def test_agent_loop_stops_on_every_runtime_stopping_status(self) -> None:
        for status in sorted(STOPPING_OBSERVATION_STATUSES):
            with self.subTest(status=status):
                client = FakeLLMClient(
                    [
                        decision(
                            "search_files",
                            {"query": "parser"},
                            "Search for parser evidence.",
                            "Repository paths related to parser.",
                        ),
                        decision(
                            "finish",
                            {"selected_paths": []},
                            "This response must not be consumed.",
                            "A finish observation.",
                            finish_reason="This response must not be consumed.",
                        ),
                    ]
                )
                observation = RuntimeObservation(
                    action_id="agent-step-1",
                    action_kind="search_files",
                    status=status,
                    summary=f"Stopped with {status}.",
                    data={"question": "What behavior is required?"}
                    if status == "input_required"
                    else {},
                    error=f"Stopped with {status}."
                    if status != "input_required"
                    else None,
                )

                with tempfile.TemporaryDirectory() as tmp, patch(
                    "repopilot_agent.agent_loop.AgentRuntime.execute",
                    return_value=observation,
                ):
                    result = run_agent_loop(
                        "inspect parser",
                        tmp,
                        [],
                        [],
                        client,
                        max_steps=2,
                    )

                self.assertEqual(len(client.calls), 1)
                self.assertEqual(result.stop_reason, status)
                self.assertEqual(result.working_state.stop_reason, status)


if __name__ == "__main__":
    unittest.main()
