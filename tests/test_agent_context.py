from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.agent_context import (
    AGENT_CONTEXT_BUDGET,
    AgentContextBudget,
    build_agent_context_packet,
    redact_context_secrets,
)
from repopilot_agent.execution import AcceptanceCriterion
from repopilot_agent.models import AgentStep, MemoryContextItem, SearchHit
from repopilot_agent.runtime import create_agent_working_state


def agent_step(order: int, observation: str) -> AgentStep:
    return AgentStep(
        order=order,
        action="read_file",
        thought=f"Inspect evidence {order}.",
        tool_input=f"src/file_{order}.py",
        observation=observation,
        expected_evidence=f"Implementation evidence {order}.",
    )


class AgentContextTests(unittest.TestCase):
    def test_packet_includes_all_sources_and_summarizes_older_evidence(self) -> None:
        state = create_agent_working_state("fix parser")
        steps = [
            agent_step(1, "Old parser evidence."),
            agent_step(2, "Second parser observation."),
            agent_step(3, "Third parser observation."),
            agent_step(4, "Newest parser observation."),
        ]
        memory = [
            MemoryContextItem(
                run_id="pinned",
                task="previous parser fix",
                summary="Reuse the focused parser test.",
                mode="run",
                created_at="2026-01-01T00:00:00+00:00",
                applied=True,
                score=100,
                reasons=["pinned memory"],
                pinned=True,
                validation=["python -m unittest tests.test_parser"],
            ),
            MemoryContextItem(
                run_id="related",
                task="ordinary related run",
                summary="This is not pinned.",
                mode="run",
                created_at="2026-01-02T00:00:00+00:00",
                applied=False,
                score=5,
                reasons=["task overlap"],
            ),
        ]
        packet = build_agent_context_packet(
            state,
            [SearchHit("src/parser.py", 10, ["parser match"], "def parse(value):")],
            steps,
            repository_map_context="src/parser.py [Python]\n  function parse(value)",
            memory_context=memory,
            current_diff="+OPENAI_API_KEY=sk-live-secret\n+def parse(value): pass",
            acceptance_criteria=[
                AcceptanceCriterion(
                    "analysis_complete",
                    "analysis",
                    "Explain the parser behavior.",
                )
            ],
            remaining_budget={
                "agent_steps": 2,
                "tool_calls": 2,
                "validation_commands": 4,
                "elapsed_ms": 10_000,
            },
        )

        self.assertLessEqual(len(packet.text), AGENT_CONTEXT_BUDGET.max_chars)
        self.assertEqual(
            [section.name for section in packet.sections],
            [
                "working_state",
                "remaining_budget",
                "acceptance_criteria",
                "pinned_memory",
                "repository_map",
                "current_diff",
                "recent_observations",
                "older_evidence",
                "initial_context",
            ],
        )
        self.assertIn("previous parser fix", packet.text)
        self.assertNotIn("ordinary related run", packet.text)
        self.assertIn("Step 1 read_file", packet.text)
        self.assertIn("Newest parser observation", packet.text)
        self.assertIn("[REDACTED]", packet.text)
        self.assertNotIn("sk-live-secret", packet.text)
        self.assertIn("current_diff", packet.summary)

    def test_total_budget_preserves_priority_and_omits_lower_sections(self) -> None:
        budget = AgentContextBudget(
            max_chars=180,
            working_state_chars=80,
            remaining_budget_chars=80,
            acceptance_criteria_chars=80,
            pinned_memory_chars=80,
            repository_map_chars=80,
            current_diff_chars=80,
            recent_observations_chars=80,
            older_evidence_chars=80,
            initial_context_chars=80,
            recent_observation_count=1,
        )

        packet = build_agent_context_packet(
            create_agent_working_state("inspect repository"),
            [],
            [],
            budget=budget,
        )

        self.assertLessEqual(len(packet.text), 180)
        self.assertFalse(packet.sections[0].omitted)
        self.assertTrue(packet.sections[-1].omitted)
        self.assertIn("initial_context", packet.omitted_sections)
        self.assertEqual(
            [section.priority for section in packet.sections],
            sorted(section.priority for section in packet.sections),
        )

    def test_section_limit_marks_repository_map_as_truncated(self) -> None:
        packet = build_agent_context_packet(
            create_agent_working_state("inspect map"),
            [],
            [],
            repository_map_context="symbol\n" * 1_000,
        )
        map_section = next(
            section for section in packet.sections if section.name == "repository_map"
        )

        self.assertTrue(map_section.truncated)
        self.assertEqual(map_section.included_chars, map_section.limit_chars)
        self.assertIn("[...section truncated...]", packet.text)

    def test_secret_redaction_handles_assignments_and_private_keys(self) -> None:
        content = (
            "Authorization: Bearer real-token\n"
            '"client_secret": "real-secret"\n'
            'value = "sk-abcdefgh12345678"\n'
            "if password == expected: allow()\n"
            "safe_value = visible\n"
            "-----BEGIN PRIVATE KEY-----\nprivate-data\n-----END PRIVATE KEY-----"
        )

        redacted = redact_context_secrets(content)

        self.assertNotIn("real-token", redacted)
        self.assertNotIn("real-secret", redacted)
        self.assertNotIn("sk-abcdefgh12345678", redacted)
        self.assertNotIn("private-data", redacted)
        self.assertIn("if password == expected: allow()", redacted)
        self.assertIn("safe_value = visible", redacted)
        self.assertIn("[REDACTED PRIVATE KEY]", redacted)

    def test_budget_rejects_nonpositive_values(self) -> None:
        with self.assertRaises(ValueError):
            AgentContextBudget(max_chars=0)


if __name__ == "__main__":
    unittest.main()
