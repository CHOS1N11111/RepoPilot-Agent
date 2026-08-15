from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.runtime import AgentRuntime, RuntimeAction


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def proposal_action(
    path: str,
    expected_sha256: str,
    old_text: str,
    new_text: str,
    action_id: str,
) -> RuntimeAction:
    return RuntimeAction(
        kind="propose_patch",
        arguments={
            "path": path,
            "expected_sha256": expected_sha256,
            "hunks": [
                {
                    "old_text": old_text,
                    "new_text": new_text,
                    "expected_occurrences": 1,
                }
            ],
        },
        rationale="Prepare a virtual edit.",
        action_id=action_id,
    )


class VirtualPatchRuntimeTests(unittest.TestCase):
    def test_proposal_revision_and_inspection_never_write_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "module.py")
            original = "def value():\n    return 1\n"
            first_virtual = "def value():\n    return 2\n"
            latest_virtual = "def value():\n    return 3\n"
            target.write_text(original, encoding="utf-8")
            runtime = AgentRuntime(tmp, "update value")

            first = runtime.execute(
                proposal_action(
                    "module.py",
                    sha256(original),
                    "return 1",
                    "return 2",
                    "proposal-1",
                )
            )
            second = runtime.execute(
                proposal_action(
                    "module.py",
                    first.data["resulting_sha256"],
                    "return 2",
                    "return 3",
                    "proposal-2",
                )
            )
            inspected = runtime.execute(
                RuntimeAction(kind="inspect_proposed_diff", action_id="inspect-1")
            )

            self.assertEqual(first.status, "completed")
            self.assertEqual(first.data["revision"], 1)
            self.assertEqual(first.data["resulting_sha256"], sha256(first_virtual))
            self.assertFalse(first.data["repository_written"])
            self.assertEqual(second.status, "completed")
            self.assertEqual(second.data["revision"], 2)
            self.assertEqual(second.data["resulting_sha256"], sha256(latest_virtual))
            self.assertIn("+    return 3", second.data["diff"])
            self.assertNotIn("+    return 2", second.data["diff"])
            self.assertEqual(inspected.status, "completed")
            self.assertEqual(inspected.data["proposal_status"], "inspected")
            self.assertTrue(runtime.proposed_edits[0]["inspected"])
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_revision_rejects_stale_virtual_hash_without_losing_current_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "notes.txt")
            original = "before\n"
            target.write_text(original, encoding="utf-8")
            runtime = AgentRuntime(tmp, "update notes")
            runtime.execute(
                proposal_action(
                    "notes.txt",
                    sha256(original),
                    "before",
                    "after",
                    "proposal-1",
                )
            )

            conflict = runtime.execute(
                proposal_action(
                    "notes.txt",
                    sha256(original),
                    "after",
                    "latest",
                    "proposal-stale",
                )
            )

            self.assertEqual(conflict.status, "conflict")
            self.assertEqual(
                conflict.data["conflicts"][0]["kind"],
                "stale_virtual_revision",
            )
            self.assertIn("+after", runtime.proposed_diff)
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_external_change_conflicts_then_fresh_hash_resets_virtual_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "notes.txt")
            original = "before\n"
            external = "external\n"
            target.write_text(original, encoding="utf-8")
            runtime = AgentRuntime(tmp, "update notes")
            runtime.execute(
                proposal_action(
                    "notes.txt",
                    sha256(original),
                    "before",
                    "after",
                    "proposal-1",
                )
            )
            target.write_text(external, encoding="utf-8")

            inspection = runtime.execute(
                RuntimeAction(kind="inspect_proposed_diff", action_id="inspect-stale")
            )
            stale_revision = runtime.execute(
                proposal_action(
                    "notes.txt",
                    sha256(original),
                    "before",
                    "wrong",
                    "proposal-stale",
                )
            )
            recovered = runtime.execute(
                proposal_action(
                    "notes.txt",
                    sha256(external),
                    "external",
                    "recovered",
                    "proposal-recovered",
                )
            )

            self.assertEqual(inspection.status, "conflict")
            self.assertEqual(
                inspection.data["conflicts"][0]["kind"],
                "stale_repository",
            )
            self.assertEqual(stale_revision.status, "conflict")
            self.assertTrue(recovered.data["baseline_reset"])
            self.assertEqual(recovered.data["base_sha256"], sha256(external))
            self.assertIn("+recovered", recovered.data["diff"])
            self.assertEqual(target.read_text(encoding="utf-8"), external)

    def test_ambiguous_hunk_and_invalid_syntax_are_not_stored_or_written(self) -> None:
        fixtures = [
            ("notes.txt", "same\nsame\n", "same", "changed", "conflict"),
            ("module.py", "value = 1\n", "value = 1", "def broken(", "rejected"),
        ]
        for path, original, old_text, new_text, expected_status in fixtures:
            with self.subTest(path=path), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp, path)
                target.write_text(original, encoding="utf-8")
                runtime = AgentRuntime(tmp, "prepare edit")

                result = runtime.execute(
                    proposal_action(
                        path,
                        sha256(original),
                        old_text,
                        new_text,
                        "proposal-invalid",
                    )
                )

                self.assertEqual(result.status, expected_status)
                self.assertEqual(runtime.proposed_edits, [])
                self.assertEqual(runtime.proposed_diff, "")
                self.assertEqual(target.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
