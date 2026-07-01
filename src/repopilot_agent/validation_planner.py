"""Validation command recommendations based on changed files."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from .models import ValidationPlan

PYTHON_SUFFIX = ".py"
JS_SUFFIXES = {".js", ".jsx", ".ts", ".tsx"}
DOC_SUFFIXES = {".md", ".rst", ".txt"}


def build_validation_plan(repo_path: str | Path, changed_paths: list[str]) -> ValidationPlan:
    root = Path(repo_path).expanduser().resolve()
    paths = _unique_paths(changed_paths)
    commands: list[str] = []
    notes: list[str] = []

    python_paths = [path for path in paths if path.endswith(PYTHON_SUFFIX)]
    js_paths = [path for path in paths if PurePosixPath(path).suffix.lower() in JS_SUFFIXES]
    doc_paths = [path for path in paths if _is_documentation_path(path)]

    if python_paths:
        python_commands, python_notes = _python_validation(root, python_paths)
        commands.extend(python_commands)
        notes.extend(python_notes)

    if js_paths:
        if (root / "package.json").is_file():
            commands.append("npm test")
        else:
            notes.append("JavaScript or TypeScript files changed, but no package.json was detected.")

    if doc_paths and not commands:
        notes.append("Documentation-only changes should be reviewed in rendered form.")
    elif doc_paths:
        notes.append("Review rendered documentation if user-facing docs changed.")

    if not commands and not notes:
        notes.append("No validation command could be inferred from the changed files.")

    return ValidationPlan(commands=_dedupe(commands), notes=_dedupe(notes))


def _python_validation(root: Path, python_paths: list[str]) -> tuple[list[str], list[str]]:
    commands: list[str] = []
    notes: list[str] = []

    for path in python_paths:
        if _is_test_path(path):
            commands.append(_unittest_command(path))
            continue
        paired_test = _find_paired_test(root, path)
        if paired_test:
            commands.append(_unittest_command(paired_test))

    if not commands:
        if (root / "tests").is_dir():
            commands.append("python -m unittest discover -s tests")
        else:
            notes.append("Python files changed, but no tests directory was detected.")
    return commands, notes


def _find_paired_test(root: Path, source_path: str) -> str | None:
    pure_path = PurePosixPath(source_path)
    stem = pure_path.stem
    suffix = pure_path.suffix
    candidates = [
        PurePosixPath("tests") / f"test_{stem}{suffix}",
        PurePosixPath("test") / f"test_{stem}{suffix}",
        PurePosixPath("tests") / f"{stem}_test{suffix}",
        pure_path.parent / f"test_{pure_path.name}",
    ]
    for candidate in candidates:
        if (root / Path(*candidate.parts)).is_file():
            return candidate.as_posix()
    return None


def _unittest_command(path: str) -> str:
    pure_path = PurePosixPath(path)
    if pure_path.suffix == ".py":
        pure_path = pure_path.with_suffix("")
    module = ".".join(part for part in pure_path.parts if part)
    return f"python -m unittest {module}"


def _is_test_path(path: str) -> bool:
    lower = path.lower()
    name = PurePosixPath(lower).name
    return lower.startswith(("tests/", "test/")) or "/tests/" in lower or name.startswith("test_") or name.endswith("_test.py")


def _is_documentation_path(path: str) -> bool:
    lower = path.lower()
    suffix = PurePosixPath(lower).suffix
    return suffix in DOC_SUFFIXES or lower.startswith(("docs/", "documentation/")) or PurePosixPath(lower).name == "readme.md"


def _unique_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        normalized = PurePosixPath(path.replace("\\", "/")).as_posix()
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
