from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.structured_patch import (
    PatchHunk,
    StructuredPatch,
    apply_structured_patch,
    file_sha256,
    parse_structured_patch,
)


class StructuredPatchTests(unittest.TestCase):
    def test_exact_hunks_apply_with_hash_readback_and_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "module.py")
            original = "def value():\n    return 1\n\nFLAG = False\n"
            target.write_text(original, encoding="utf-8")
            patch = StructuredPatch(
                path="module.py",
                expected_sha256=file_sha256(original),
                hunks=[
                    PatchHunk("    return 1", "    return 2"),
                    PatchHunk("FLAG = False", "FLAG = True"),
                ],
                rationale="Update value and flag.",
            )

            result = apply_structured_patch(tmp, patch, task="update return value and flag")

            self.assertEqual(result.status, "applied")
            self.assertTrue(result.applied)
            self.assertEqual(result.hunks_applied, 2)
            self.assertEqual(result.syntax_check.status, "passed")
            self.assertEqual(result.resulting_sha256, file_sha256(target.read_text(encoding="utf-8")))
            self.assertIn("+    return 2", result.diff)
            self.assertIn("FLAG = True", target.read_text(encoding="utf-8"))

    def test_stale_hash_returns_conflict_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "notes.txt")
            target.write_text("current\n", encoding="utf-8")
            patch = StructuredPatch("notes.txt", file_sha256("old\n"), [PatchHunk("current", "new")])

            result = apply_structured_patch(tmp, patch)

            self.assertEqual(result.status, "conflict")
            self.assertEqual(result.conflicts[0]["kind"], "stale_file")
            self.assertEqual(target.read_text(encoding="utf-8"), "current\n")

    def test_ambiguous_or_missing_hunk_returns_structured_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "notes.txt")
            original = "same\nsame\n"
            target.write_text(original, encoding="utf-8")
            patch = StructuredPatch("notes.txt", file_sha256(original), [PatchHunk("same", "changed")])

            result = apply_structured_patch(tmp, patch)

            self.assertEqual(result.status, "conflict")
            self.assertEqual(result.conflicts[0]["kind"], "hunk_match")
            self.assertEqual(result.conflicts[0]["actual_occurrences"], 2)
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_invalid_python_and_json_are_rejected_before_write(self) -> None:
        fixtures = [
            ("module.py", "value = 1\n", "value = 1", "def broken("),
            ("config.json", '{"enabled": true}\n', "true", "not-json"),
        ]
        for path, original, old_text, new_text in fixtures:
            with self.subTest(path=path), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp, path)
                target.write_text(original, encoding="utf-8")
                patch = StructuredPatch(path, file_sha256(original), [PatchHunk(old_text, new_text)])

                result = apply_structured_patch(tmp, patch)

                self.assertEqual(result.status, "rejected")
                self.assertEqual(result.syntax_check.status, "failed")
                self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_parser_requires_hash_and_nonempty_exact_hunks(self) -> None:
        parsed = parse_structured_patch(
            {
                "path": "notes.txt",
                "expected_sha256": "a" * 64,
                "hunks": [{"old_text": "before", "new_text": "after"}],
            }
        )
        self.assertEqual(parsed.hunks[0].expected_occurrences, 1)
        with self.assertRaisesRegex(ValueError, "expected_sha256"):
            parse_structured_patch({"path": "notes.txt", "expected_sha256": "bad", "hunks": []})
        with self.assertRaisesRegex(ValueError, "non-empty old_text"):
            parse_structured_patch(
                {
                    "path": "notes.txt",
                    "expected_sha256": "a" * 64,
                    "hunks": [{"old_text": "", "new_text": "after"}],
                }
            )


if __name__ == "__main__":
    unittest.main()
