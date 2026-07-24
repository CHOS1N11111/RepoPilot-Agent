from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.models import RepoFile
from repopilot_agent.repository_map import build_repository_map, rank_repository_map, render_repository_map


def repo_file(path: str, content: str, language: str = "Python") -> RepoFile:
    return RepoFile(
        path=Path(path),
        relative_path=path,
        size_bytes=len(content.encode("utf-8")),
        language=language,
        content=content,
    )


class RepositoryMapTests(unittest.TestCase):
    def test_python_symbols_dependencies_and_test_pairs_are_indexed(self) -> None:
        repository_map = build_repository_map(
            [
                repo_file(
                    "src/pkg/service.py",
                    "from .models import Record\n\nclass Service:\n"
                    "    async def run(self, record: Record) -> bool:\n        return True\n",
                ),
                repo_file("src/pkg/models.py", "class Record:\n    pass\n"),
                repo_file(
                    "tests/test_service.py",
                    "from src.pkg.service import Service\n\ndef test_run():\n    assert Service\n",
                ),
            ]
        )
        by_path = {entry.path: entry for entry in repository_map.entries}

        service = by_path["src/pkg/service.py"]
        self.assertEqual([symbol.qualified_name for symbol in service.symbols], ["Service", "Service.run"])
        self.assertIn("run(self, record: Record)", service.symbols[1].signature)
        self.assertIn("src/pkg/models.py", service.related_paths)
        self.assertIn("tests/test_service.py", service.related_paths)
        self.assertIn("src/pkg/service.py", by_path["tests/test_service.py"].related_paths)
        self.assertEqual(repository_map.symbol_count, 4)

    def test_javascript_symbols_and_relative_imports_are_indexed(self) -> None:
        repository_map = build_repository_map(
            [
                repo_file(
                    "src/app.js",
                    "import { helper } from './helper.js';\nexport class App {}\n"
                    "export async function start(value) { return helper(value); }\n"
                    "const stop = () => false;\n",
                    "JavaScript",
                ),
                repo_file("src/helper.js", "export function helper(value) { return value; }\n", "JavaScript"),
            ]
        )
        app = repository_map.entries[0]

        self.assertEqual([symbol.name for symbol in app.symbols], ["App", "start", "stop"])
        self.assertIn("src/helper.js", app.related_paths)

    def test_invalid_python_records_parse_error_without_stopping_map(self) -> None:
        repository_map = build_repository_map([repo_file("broken.py", "def broken(:\n")])

        self.assertEqual(repository_map.parse_error_count, 1)
        self.assertIn("line 1", repository_map.entries[0].parse_error or "")

    def test_task_ranking_and_rendering_are_bounded(self) -> None:
        repository_map = build_repository_map(
            [
                repo_file("src/auth.py", "def authenticate_user(token):\n    return token\n"),
                repo_file("src/cache.py", "def clear_cache():\n    return None\n"),
            ]
        )

        matches = rank_repository_map("fix authenticate user", repository_map, limit=1)
        rendered = render_repository_map(repository_map, "authenticate", max_chars=180)

        self.assertEqual(matches[0].path, "src/auth.py")
        self.assertLessEqual(len(rendered), 180)
        self.assertIn("src/auth.py", rendered)


if __name__ == "__main__":
    unittest.main()
