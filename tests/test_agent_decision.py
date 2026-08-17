from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.llm.base import LLMError
from repopilot_agent.llm.schema import (
    AGENT_WRITE_ACTIONS,
    ALLOWED_AGENT_DECISION_ACTIONS,
    parse_agent_action_json,
    parse_agent_decision_json,
)
from repopilot_agent.models import AGENT_DECISION_VERSION, AgentDecision


def decision_payload(kind: str = "search_files", arguments: dict | None = None) -> dict:
    return {
        "version": AGENT_DECISION_VERSION,
        "rationale": "Inspect the most relevant repository evidence.",
        "action": {
            "kind": kind,
            "arguments": {"query": "parser"} if arguments is None else arguments,
        },
        "expected_evidence": "Relevant paths and bounded previews.",
        "state_update": {
            "focus": "Locate parser behavior.",
            "add_findings": [],
            "add_open_questions": ["Where is parsing implemented?"],
            "resolve_open_questions": [],
        },
        "finish_reason": "",
        "user_question": "",
    }


class AgentDecisionSchemaTests(unittest.TestCase):
    def test_valid_decision_and_compatibility_entry_point(self) -> None:
        raw = json.dumps(decision_payload())

        parsed = parse_agent_decision_json(raw)
        compatible = parse_agent_action_json(raw)

        self.assertIsInstance(parsed, AgentDecision)
        self.assertEqual(parsed.version, 2)
        self.assertEqual(parsed.action_kind, "search_files")
        self.assertEqual(parsed.action_arguments, {"query": "parser"})
        self.assertEqual(parsed.query, "parser")
        self.assertEqual(compatible, parsed)

    def test_finish_normalizes_and_deduplicates_selected_paths(self) -> None:
        payload = decision_payload(
            "finish",
            {"selected_paths": ["src\\main.py", "src/main.py"]},
        )
        payload["finish_reason"] = "The implementation target is known."

        parsed = parse_agent_decision_json(json.dumps(payload))

        self.assertEqual(parsed.selected_paths, ["src/main.py"])
        self.assertEqual(parsed.finish_reason, "The implementation target is known.")

    def test_repository_map_and_git_arguments_are_typed(self) -> None:
        repository_map = parse_agent_decision_json(
            json.dumps(decision_payload("inspect_repository_map", {"query": "parse", "limit": 8}))
        )
        git_status = parse_agent_decision_json(
            json.dumps(decision_payload("inspect_git_status", {}))
        )

        self.assertEqual(repository_map.action_arguments, {"query": "parse", "limit": 8})
        self.assertEqual(git_status.action_arguments, {})

    def test_diff_and_user_question_arguments_are_typed(self) -> None:
        unstaged_diff = parse_agent_decision_json(
            json.dumps(decision_payload("inspect_diff", {}))
        )
        staged_diff = parse_agent_decision_json(
            json.dumps(decision_payload("inspect_diff", {"staged": True}))
        )
        ask_payload = decision_payload("ask_user", {})
        ask_payload["user_question"] = "Which behavior should remain compatible?"
        ask_payload["state_update"]["add_open_questions"] = [
            "Which behavior should remain compatible?"
        ]
        ask_user = parse_agent_decision_json(json.dumps(ask_payload))

        self.assertEqual(unstaged_diff.action_arguments, {"staged": False})
        self.assertEqual(staged_diff.action_arguments, {"staged": True})
        self.assertEqual(ask_user.action_arguments, {})
        self.assertEqual(
            ask_user.user_question,
            "Which behavior should remain compatible?",
        )

    def test_parallel_read_is_strict_bounded_and_budget_aware(self) -> None:
        payload = decision_payload(
            "parallel_read",
            {
                "actions": [
                    {"kind": "read_file", "arguments": {"path": "src\\main.py"}},
                    {"kind": "inspect_diff", "arguments": {}},
                ]
            },
        )

        parsed = parse_agent_decision_json(
            json.dumps(payload),
            max_parallel_read_actions=2,
        )

        self.assertEqual(parsed.action_arguments["actions"][0]["arguments"]["path"], "src/main.py")
        self.assertEqual(parsed.action_arguments["actions"][1]["arguments"], {"staged": False})
        with self.assertRaisesRegex(LLMError, "Invalid Agent decision action"):
            parse_agent_decision_json(
                json.dumps(payload),
                max_parallel_read_actions=1,
            )
        payload["action"]["arguments"]["actions"].append(
            {"kind": "search_files", "arguments": {"query": "parser"}}
        )
        with self.assertRaisesRegex(LLMError, "2 to 2"):
            parse_agent_decision_json(
                json.dumps(payload),
                max_parallel_read_actions=2,
            )
        payload["action"]["arguments"]["actions"] = [
            {"kind": "read_file", "arguments": {"path": "main.py"}},
            {"kind": "validate", "arguments": {"command": "python bad.py"}},
        ]
        with self.assertRaisesRegex(LLMError, "unsupported action"):
            parse_agent_decision_json(json.dumps(payload))

    def test_virtual_patch_actions_are_strict_and_bounded(self) -> None:
        proposed = parse_agent_decision_json(
            json.dumps(
                decision_payload(
                    "propose_patch",
                    {
                        "path": "src\\main.py",
                        "expected_sha256": "A" * 64,
                        "hunks": [
                            {
                                "old_text": "return 1",
                                "new_text": "return 2",
                            }
                        ],
                    },
                )
            )
        )
        inspected = parse_agent_decision_json(
            json.dumps(decision_payload("inspect_proposed_diff", {}))
        )

        self.assertEqual(proposed.action_arguments["path"], "src/main.py")
        self.assertEqual(proposed.action_arguments["expected_sha256"], "a" * 64)
        self.assertEqual(
            proposed.action_arguments["hunks"][0]["expected_occurrences"],
            1,
        )
        self.assertEqual(inspected.action_arguments, {})

        invalid_payloads = [
            decision_payload(
                "propose_patch",
                {
                    "path": "../main.py",
                    "expected_sha256": "a" * 64,
                    "hunks": [{"old_text": "before", "new_text": "after"}],
                },
            ),
            decision_payload(
                "propose_patch",
                {
                    "path": "main.py",
                    "expected_sha256": "bad",
                    "hunks": [{"old_text": "before", "new_text": "after"}],
                },
            ),
            decision_payload(
                "propose_patch",
                {
                    "path": "main.py",
                    "expected_sha256": "a" * 64,
                    "hunks": [{"old_text": "x" * 12_001, "new_text": "after"}],
                },
            ),
            decision_payload("inspect_proposed_diff", {"path": "main.py"}),
        ]
        for payload in invalid_payloads:
            with self.subTest(kind=payload["action"]["kind"]), self.assertRaises(LLMError):
                parse_agent_decision_json(json.dumps(payload))

    def test_write_actions_require_an_explicit_parser_capability(self) -> None:
        payload = decision_payload(
            "apply_patch",
            {
                "path": "main.py",
                "expected_sha256": "a" * 64,
                "hunks": [{"old_text": "before", "new_text": "after"}],
            },
        )

        with self.assertRaisesRegex(LLMError, "Invalid Agent decision action"):
            parse_agent_decision_json(json.dumps(payload))

        parsed = parse_agent_decision_json(
            json.dumps(payload),
            allowed_actions=ALLOWED_AGENT_DECISION_ACTIONS | AGENT_WRITE_ACTIONS,
        )
        edit = parse_agent_decision_json(
            json.dumps(
                decision_payload(
                    "edit_file",
                    {"path": "notes.txt", "new_content": "updated\n"},
                )
            ),
            allowed_actions=ALLOWED_AGENT_DECISION_ACTIONS | AGENT_WRITE_ACTIONS,
        )

        self.assertEqual(parsed.action_kind, "apply_patch")
        self.assertEqual(parsed.action_arguments["hunks"][0]["expected_occurrences"], 1)
        self.assertEqual(edit.action_arguments["new_content"], "updated\n")

    def test_plan_and_acceptance_state_updates_are_typed(self) -> None:
        payload = decision_payload()
        payload["state_update"]["plan_updates"] = [
            {
                "step_id": "inspect_parser",
                "title": "Inspect parser",
                "detail": "Read the parser implementation.",
                "status": "completed",
                "evidence_action_ids": ["agent-step-1"],
            }
        ]
        payload["state_update"]["acceptance_updates"] = [
            {
                "criterion_id": "analysis_complete",
                "kind": "analysis",
                "description": "Parser behavior is supported by repository evidence.",
                "required": True,
                "evidence_action_ids": ["agent-step-1"],
                "evidence_summary": "The parser file was read.",
            }
        ]

        parsed = parse_agent_decision_json(json.dumps(payload))

        self.assertEqual(parsed.state_update.plan_updates[0].step_id, "inspect_parser")
        self.assertEqual(parsed.state_update.plan_updates[0].status, "completed")
        self.assertEqual(
            parsed.state_update.acceptance_updates[0].evidence_action_ids,
            ["agent-step-1"],
        )

    def test_rejects_unknown_missing_and_invalid_contract_fields(self) -> None:
        cases: list[tuple[str, dict]] = []

        unknown = decision_payload()
        unknown["unexpected"] = True
        cases.append(("unknown top-level field", unknown))

        wrong_version = decision_payload()
        wrong_version["version"] = 1
        cases.append(("wrong version", wrong_version))

        missing_evidence = decision_payload()
        missing_evidence["expected_evidence"] = ""
        cases.append(("missing expected evidence", missing_evidence))

        unknown_argument = decision_payload()
        unknown_argument["action"]["arguments"]["path"] = "main.py"
        cases.append(("unknown action argument", unknown_argument))

        unsafe_path = decision_payload("read_file", {"path": "../secret.txt"})
        cases.append(("unsafe path", unsafe_path))

        windows_absolute_path = decision_payload(
            "read_file",
            {"path": "C:\\Users\\secret.txt"},
        )
        cases.append(("Windows absolute path", windows_absolute_path))

        invalid_limit = decision_payload("inspect_repository_map", {"limit": 0})
        cases.append(("invalid map limit", invalid_limit))

        invalid_staged = decision_payload("inspect_diff", {"staged": "yes"})
        cases.append(("invalid staged flag", invalid_staged))

        unknown_diff_argument = decision_payload("inspect_diff", {"path": "main.py"})
        cases.append(("unknown diff argument", unknown_diff_argument))

        for label, payload in cases:
            with self.subTest(label=label), self.assertRaises(LLMError):
                parse_agent_decision_json(json.dumps(payload))

    def test_rejects_invalid_finish_and_state_update_semantics(self) -> None:
        missing_finish_reason = decision_payload("finish", {"selected_paths": []})

        unexpected_finish_reason = decision_payload()
        unexpected_finish_reason["finish_reason"] = "Not finishing."

        question_conflict = decision_payload()
        question_conflict["state_update"]["resolve_open_questions"] = [
            " where IS parsing implemented? "
        ]

        excessive_findings = decision_payload()
        excessive_findings["state_update"]["add_findings"] = [
            f"Finding {index}" for index in range(13)
        ]

        missing_user_question = decision_payload("ask_user", {})

        untracked_user_question = decision_payload("ask_user", {})
        untracked_user_question["user_question"] = "Which behavior is expected?"

        unexpected_user_question = decision_payload()
        unexpected_user_question["user_question"] = "Should I continue?"

        completed_plan_without_evidence = decision_payload()
        completed_plan_without_evidence["state_update"]["plan_updates"] = [
            {
                "step_id": "inspect_parser",
                "title": "Inspect parser",
                "detail": "Read the parser implementation.",
                "status": "completed",
                "evidence_action_ids": [],
            }
        ]

        acceptance_summary_without_evidence = decision_payload()
        acceptance_summary_without_evidence["state_update"]["acceptance_updates"] = [
            {
                "criterion_id": "analysis_complete",
                "kind": "analysis",
                "description": "Parser behavior is understood.",
                "required": True,
                "evidence_action_ids": [],
                "evidence_summary": "Unsupported model claim.",
            }
        ]

        pending_plan_with_evidence = decision_payload()
        pending_plan_with_evidence["state_update"]["plan_updates"] = [
            {
                "step_id": "inspect_parser",
                "title": "Inspect parser",
                "detail": "Read the parser implementation.",
                "status": "pending",
                "evidence_action_ids": ["explore-1"],
            }
        ]

        duplicate_plan_ids = decision_payload()
        duplicate_plan_ids["state_update"]["plan_updates"] = [
            {
                "step_id": "inspect_parser",
                "title": f"Inspect parser {index}",
                "detail": "Read the parser implementation.",
                "status": "pending",
                "evidence_action_ids": [],
            }
            for index in range(2)
        ]

        excessive_plan_updates = decision_payload()
        excessive_plan_updates["state_update"]["plan_updates"] = [
            {
                "step_id": f"step_{index}",
                "title": f"Step {index}",
                "detail": "Inspect repository evidence.",
                "status": "pending",
                "evidence_action_ids": [],
            }
            for index in range(13)
        ]

        invalid_acceptance_required = decision_payload()
        invalid_acceptance_required["state_update"]["acceptance_updates"] = [
            {
                "criterion_id": "analysis_complete",
                "kind": "analysis",
                "description": "Parser behavior is understood.",
                "required": "yes",
                "evidence_action_ids": [],
                "evidence_summary": "",
            }
        ]

        acceptance_evidence_without_summary = decision_payload()
        acceptance_evidence_without_summary["state_update"]["acceptance_updates"] = [
            {
                "criterion_id": "analysis_complete",
                "kind": "analysis",
                "description": "Parser behavior is understood.",
                "required": True,
                "evidence_action_ids": ["explore-1"],
                "evidence_summary": "",
            }
        ]

        for payload in [
            missing_finish_reason,
            unexpected_finish_reason,
            question_conflict,
            excessive_findings,
            missing_user_question,
            untracked_user_question,
            unexpected_user_question,
            completed_plan_without_evidence,
            acceptance_summary_without_evidence,
            pending_plan_with_evidence,
            duplicate_plan_ids,
            excessive_plan_updates,
            invalid_acceptance_required,
            acceptance_evidence_without_summary,
        ]:
            with self.assertRaises(LLMError):
                parse_agent_decision_json(json.dumps(payload))

    def test_input_payload_is_not_mutated(self) -> None:
        payload = decision_payload()
        original = copy.deepcopy(payload)

        parse_agent_decision_json(json.dumps(payload))

        self.assertEqual(payload, original)


if __name__ == "__main__":
    unittest.main()
