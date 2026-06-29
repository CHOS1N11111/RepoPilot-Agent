"""Repository scanning utilities."""

from __future__ import annotations

from pathlib import Path

from .models import RepoFile

DEFAULT_IGNORED_DIRS = {
    ".git",
    ".repopilot",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "venv",
}

DEFAULT_IGNORED_FILES = {
    "log.md",
}

LANGUAGE_BY_EXTENSION = {
    ".css": "CSS",
    ".go": "Go",
    ".html": "HTML",
    ".java": "Java",
    ".js": "JavaScript",
    ".json": "JSON",
    ".jsx": "JavaScript",
    ".md": "Markdown",
    ".py": "Python",
    ".rs": "Rust",
    ".sh": "Shell",
    ".toml": "TOML",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".txt": "Text",
    ".yaml": "YAML",
    ".yml": "YAML",
}

TEXT_EXTENSIONS = set(LANGUAGE_BY_EXTENSION)
MAX_FILE_BYTES = 250_000


def scan_repository(repo_path: str | Path) -> list[RepoFile]:
    root = Path(repo_path).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"Repository path does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Repository path is not a directory: {root}")

    files: list[RepoFile] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or _is_ignored(path, root):
            continue
        if path.name in DEFAULT_IGNORED_FILES:
            continue
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            continue
        content = _read_text(path)
        if content is None:
            continue
        relative_path = path.relative_to(root).as_posix()
        files.append(
            RepoFile(
                path=path,
                relative_path=relative_path,
                size_bytes=size,
                language=LANGUAGE_BY_EXTENSION.get(path.suffix.lower(), "Text"),
                content=content,
            )
        )
    return files


def _is_ignored(path: Path, root: Path) -> bool:
    relative_parts = path.relative_to(root).parts
    return any(part in DEFAULT_IGNORED_DIRS for part in relative_parts)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            return None
