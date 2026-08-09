from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.execution import ExecutionBudget
from repopilot_agent.execution_profile import (
    compare_execution_profiles,
    create_execution_profile,
    execution_profile_from_record,
    fingerprint_endpoint,
)
from repopilot_agent.task_runs import (
    clear_task_runs,
    create_task_run,
    task_run_from_record,
)


class ExecutionProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_task_runs()

    def tearDown(self) -> None:
        clear_task_runs()

    def test_endpoint_fingerprint_is_stable_and_does_not_expose_url(self) -> None:
        endpoint = "https://gateway.example/v1/chat/completions"

        fingerprint = fingerprint_endpoint(endpoint)

        self.assertEqual(fingerprint, fingerprint_endpoint(f"  {endpoint}/  "))
        self.assertEqual(len(fingerprint or ""), 64)
        self.assertNotIn("gateway", fingerprint or "")

    def test_profile_captures_non_sensitive_execution_configuration(self) -> None:
        endpoint = "https://gateway.example/v1/chat/completions"
        profile = create_execution_profile(
            use_llm=True,
            model="gpt-5.5",
            endpoint_url=endpoint,
            json_mode=True,
            allow_llm_fallback=False,
            use_memory=False,
            iterative_agent=True,
            llm_timeout_seconds=75,
            max_repair_attempts=2,
            auto_repair_enabled=True,
            execution_budget=ExecutionBudget(
                max_agent_steps=4,
                max_tool_calls=8,
                max_validation_commands=3,
                max_elapsed_seconds=120,
            ),
        )
        serialized = json.dumps(profile.to_dict())

        self.assertTrue(profile.use_llm)
        self.assertEqual(profile.model, "gpt-5.5")
        self.assertTrue(profile.endpoint_configured)
        self.assertNotIn(endpoint, serialized)
        self.assertEqual(profile.execution_budget["max_tool_calls"], 8)

    def test_profile_round_trips_with_task_run_and_legacy_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = create_execution_profile(
                use_llm=False,
                model="",
                endpoint_url=None,
                json_mode=None,
                allow_llm_fallback=True,
                use_memory=True,
                iterative_agent=False,
                llm_timeout_seconds=None,
                max_repair_attempts=1,
                auto_repair_enabled=False,
                execution_budget=ExecutionBudget(),
            )
            task_run = create_task_run(tmp, "inspect auth", [], execution_profile=profile)

            restored = task_run_from_record(task_run.to_record())
            legacy_record = task_run.to_record()
            legacy_record.pop("execution_profile")
            legacy = task_run_from_record(legacy_record)

            self.assertEqual(restored.execution_profile, profile)
            self.assertEqual(restored.to_public_dict()["execution_profile"], profile.to_dict())
            self.assertIsNone(legacy.execution_profile)
            self.assertIsNone(legacy.to_public_dict()["execution_profile"])

    def test_malformed_profile_fields_are_normalized_without_credentials(self) -> None:
        restored = execution_profile_from_record(
            {
                "version": -1,
                "captured_at": "now",
                "use_llm": "false",
                "model": "custom",
                "endpoint_configured": "true",
                "endpoint_fingerprint": "not-a-hash",
                "json_mode": "true",
                "allow_llm_fallback": "false",
                "use_memory": "false",
                "iterative_agent": "true",
                "llm_timeout_seconds": -3,
                "max_repair_attempts": -2,
                "auto_repair_enabled": "true",
                "execution_budget": {},
                "api_key": "must-be-ignored",
                "base_url": "https://must-not-be-loaded.example",
            }
        )

        self.assertIsNotNone(restored)
        self.assertFalse(restored.use_llm)
        self.assertTrue(restored.endpoint_configured)
        self.assertIsNone(restored.endpoint_fingerprint)
        self.assertIsNone(restored.json_mode)
        self.assertFalse(restored.allow_llm_fallback)
        self.assertFalse(restored.use_memory)
        self.assertTrue(restored.iterative_agent)
        self.assertIsNone(restored.llm_timeout_seconds)
        self.assertEqual(restored.max_repair_attempts, 0)
        self.assertNotIn("api_key", restored.to_dict())
        self.assertNotIn("base_url", restored.to_dict())

    def test_matching_profiles_ignore_snapshot_metadata(self) -> None:
        saved = _profile(model="gpt-5.5")
        current = _profile(model="gpt-5.5")

        comparison = compare_execution_profiles(saved, current)

        self.assertEqual(comparison.status, "matched")
        self.assertTrue(comparison.matched)
        self.assertEqual(comparison.differences, [])

    def test_changed_profile_reports_only_sanitized_execution_fields(self) -> None:
        saved = _profile(model="gpt-5.4", endpoint="https://saved.example/v1")
        current = create_execution_profile(
            use_llm=True,
            model="gpt-5.5",
            endpoint_url="https://current.example/v1",
            json_mode=True,
            allow_llm_fallback=False,
            use_memory=True,
            iterative_agent=True,
            llm_timeout_seconds=60,
            max_repair_attempts=2,
            auto_repair_enabled=True,
            execution_budget=ExecutionBudget(max_tool_calls=13),
        )

        comparison = compare_execution_profiles(saved, current)
        serialized = json.dumps(comparison.to_dict())
        fields = {item.field for item in comparison.differences}

        self.assertEqual(comparison.status, "changed")
        self.assertFalse(comparison.matched)
        self.assertIn("model", fields)
        self.assertIn("endpoint_fingerprint", fields)
        self.assertIn("execution_budget", fields)
        self.assertNotIn("https://", serialized)
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("base_url", serialized)

    def test_legacy_profile_comparison_is_explicit(self) -> None:
        comparison = compare_execution_profiles(None, _profile())

        self.assertEqual(comparison.status, "legacy")
        self.assertIsNone(comparison.matched)
        self.assertEqual(comparison.differences, [])
        self.assertIsNone(comparison.saved_profile_version)


def _profile(
    *,
    model: str = "gpt-5.5",
    endpoint: str = "https://gateway.example/v1",
):
    return create_execution_profile(
        use_llm=True,
        model=model,
        endpoint_url=endpoint,
        json_mode=True,
        allow_llm_fallback=False,
        use_memory=True,
        iterative_agent=True,
        llm_timeout_seconds=60,
        max_repair_attempts=2,
        auto_repair_enabled=True,
        execution_budget=ExecutionBudget(),
    )


if __name__ == "__main__":
    unittest.main()
