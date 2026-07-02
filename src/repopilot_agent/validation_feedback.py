"""Validation failure analysis and repair task generation."""

from __future__ import annotations

import re
from pathlib import Path

from .models import ValidationFailureDetail, ValidationFeedback, ValidationResult

MAX_FAILURE_EXCERPT_CHARS = 2400
MAX_REPAIR_TASK_CHARS = 5000

_PATH_PATTERN = re.compile(r"(?P<path>(?:[\w.-]+[\\/])*[\w.-]+\.(?:py|js|jsx|ts|tsx|json|md|toml|yaml|yml))")
_TEST_PATTERN = re.compile(r"\b(test_[A-Za-z0-9_]+|[A-Za-z0-9_]+Tests?\.[A-Za-z0-9_]+)\b")


def build_validation_feedback(
    validation: list[ValidationResult],
    task: str = "",
    repo_path: str | Path | None = None,
) -> ValidationFeedback | None:
    failures = [_build_failure_detail(result, repo_path) for result in validation if _is_failure(result)]
    if not failures:
        return None

    suspected_files = _dedupe(path for failure in failures for path in failure.suspected_files)
    failed_commands = [failure.command for failure in failures]
    repair_steps = _build_repair_steps(failures, suspected_files)
    summary = _build_summary(failures, suspected_files)
    repair_task = _build_repair_task(task, summary, failed_commands, suspected_files, repair_steps, failures)
    return ValidationFeedback(
        summary=summary,
        failures=failures,
        suspected_files=suspected_files,
        repair_steps=repair_steps,
        repair_task=repair_task,
    )


def _is_failure(result: ValidationResult) -> bool:
    return (not result.allowed) or result.exit_code not in (0, None)


def _build_failure_detail(result: ValidationResult, repo_path: str | Path | None) -> ValidationFailureDetail:
    combined_output = "\n".join(part for part in [result.stdout, result.stderr] if part).strip()
    if not result.allowed and not combined_output:
        combined_output = "Command was rejected by the validation allowlist."
    excerpt = _clip_middle(combined_output, MAX_FAILURE_EXCERPT_CHARS)
    signals = _extract_signals(result, combined_output)
    suspected_files = _extract_suspected_files(result.command, combined_output, repo_path)
    return ValidationFailureDetail(
        command=result.command,
        exit_code=result.exit_code,
        output_excerpt=excerpt,
        suspected_files=suspected_files,
        signals=signals,
    )


def _extract_signals(result: ValidationResult, output: str) -> list[str]:
    signals: list[str] = []
    if not result.allowed:
        signals.append("validation command rejected")
    for pattern, label in [
        ("AssertionError", "assertion failure"),
        ("ModuleNotFoundError", "missing module"),
        ("ImportError", "import failure"),
        ("SyntaxError", "syntax error"),
        ("Traceback", "python traceback"),
        ("FAILED", "test failure"),
        ("ERROR", "test error"),
        ("npm ERR!", "npm error"),
    ]:
        if pattern.lower() in output.lower():
            signals.append(label)
    tests = _dedupe(match.group(1) for match in _TEST_PATTERN.finditer(output))
    signals.extend(f"test: {name}" for name in tests[:5])
    return _dedupe(signals)


def _extract_suspected_files(command: str, output: str, repo_path: str | Path | None) -> list[str]:
    paths = _dedupe(_normalize_path(match.group("path")) for match in _PATH_PATTERN.finditer(output))
    command_path = _path_from_unittest_command(command, repo_path)
    if command_path:
        paths.insert(0, command_path)
    return _dedupe(path for path in paths if path)


def _path_from_unittest_command(command: str, repo_path: str | Path | None) -> str:
    parts = command.split()
    if len(parts) < 4 or parts[:3] != ["python", "-m", "unittest"]:
        return ""
    target = parts[3]
    if target == "discover":
        return ""
    if target.endswith(".py"):
        return _normalize_path(target)
    candidate = target.replace(".", "/") + ".py"
    if repo_path and (Path(repo_path) / candidate).exists():
        return candidate
    return candidate


def _build_repair_steps(failures: list[ValidationFailureDetail], suspected_files: list[str]) -> list[str]:
    steps = ["Inspect the failed validation output and reproduce the failing command locally."]
    if suspected_files:
        steps.append(f"Start with suspected file(s): {', '.join(suspected_files[:5])}.")
    all_signals = _dedupe(signal for failure in failures for signal in failure.signals)
    if any(signal in all_signals for signal in ("syntax error", "import failure", "missing module")):
        steps.append("Fix syntax/import issues before changing behavior.")
    if any(signal in all_signals for signal in ("assertion failure", "test failure", "test error")):
        steps.append("Compare expected and actual behavior in the failing test before editing implementation.")
    steps.append("Prepare a narrow repair patch and rerun the failed validation command first.")
    return steps


def _build_summary(failures: list[ValidationFailureDetail], suspected_files: list[str]) -> str:
    command_count = len(failures)
    file_text = ", ".join(suspected_files[:3]) if suspected_files else "no specific file"
    return f"{command_count} validation command(s) failed. Suspected focus: {file_text}."


def _build_repair_task(
    task: str,
    summary: str,
    failed_commands: list[str],
    suspected_files: list[str],
    repair_steps: list[str],
    failures: list[ValidationFailureDetail],
) -> str:
    lines = [
        "Repair the repository after validation failure.",
        "",
        f"Original task: {task or '(not provided)'}",
        f"Failure summary: {summary}",
        "",
        "Failed validation commands:",
        *[f"- {command}" for command in failed_commands],
        "",
        "Suspected files:",
        *([f"- {path}" for path in suspected_files[:8]] or ["- No specific file extracted."]),
        "",
        "Suggested repair steps:",
        *[f"- {step}" for step in repair_steps],
        "",
        "Validation excerpts:",
    ]
    for failure in failures:
        lines.extend([f"--- {failure.command} ---", failure.output_excerpt or "(no output)"])
    return _clip_end("\n".join(lines), MAX_REPAIR_TASK_CHARS)


def _normalize_path(path: str) -> str:
    return path.strip().strip("\"'`.,:;()[]{}").replace("\\", "/")


def _dedupe(items) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _clip_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head - 35
    return text[:head].rstrip() + "\n... output truncated ...\n" + text[-tail:].lstrip()


def _clip_end(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 27].rstrip() + "\n... repair task truncated ..."
