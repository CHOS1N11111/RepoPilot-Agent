"""Reproducible evaluation suites for RepoPilot workflows."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from time import perf_counter
from typing import Any, Callable

from .llm.base import LLMClient
from .models import WorkflowReport
from .workflow import run_workflow


class EvalConfigurationError(ValueError):
    """Raised when an evaluation suite is missing or has an invalid schema."""


@dataclass(frozen=True)
class EvalExpectations:
    relevant_files: list[str] = field(default_factory=list)
    top_relevant_file: str | None = None
    proposal_files: list[str] = field(default_factory=list)
    proposal_ready: bool | None = None
    min_plan_steps: int | None = None
    validation_passed: bool | None = None
    max_llm_failures: int | None = None
    max_fallbacks: int | None = None
    min_agent_steps: int | None = None


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    description: str
    task: str
    repo_path: Path
    validation_commands: list[str]
    expectations: EvalExpectations
    source_path: Path


@dataclass(frozen=True)
class EvalCriterionResult:
    name: str
    passed: bool
    expected: Any
    actual: Any
    detail: str = ""


@dataclass(frozen=True)
class EvalCaseResult:
    case_id: str
    description: str
    task: str
    repo_path: str
    passed: bool
    score: float
    duration_ms: int
    criteria: list[EvalCriterionResult]
    files_scanned: int = 0
    relevant_files: list[str] = field(default_factory=list)
    proposal_files: list[str] = field(default_factory=list)
    plan_steps: int = 0
    validation_commands: list[str] = field(default_factory=list)
    validation_passed: bool | None = None
    agent_steps: int = 0
    llm_calls: int = 0
    llm_failures: int = 0
    fallback_count: int = 0
    fallback_stages: list[str] = field(default_factory=list)
    llm_latency_ms: int = 0
    relevant_file_recall: float | None = None
    proposal_file_recall: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvalSuiteResult:
    suite_path: str
    mode: str
    model: str | None
    started_at: str
    duration_ms: int
    total_cases: int
    passed_cases: int
    failed_cases: int
    pass_rate: float
    average_score: float
    average_relevant_file_recall: float | None
    average_proposal_file_recall: float | None
    total_llm_calls: int
    total_llm_failures: int
    total_fallbacks: int
    total_llm_latency_ms: int
    cases: list[EvalCaseResult]

    @property
    def passed(self) -> bool:
        return self.failed_cases == 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


WorkflowRunner = Callable[..., WorkflowReport]

SUITE_KEYS = {"suite", "description", "cases"}
CASE_KEYS = {"id", "description", "repo", "task", "validation_commands", "expect"}
EXPECTATION_KEYS = {
    "relevant_files",
    "top_relevant_file",
    "proposal_files",
    "proposal_ready",
    "min_plan_steps",
    "validation_passed",
    "max_llm_failures",
    "max_fallbacks",
    "min_agent_steps",
}


def load_eval_cases(suite_path: str | Path) -> list[EvalCase]:
    """Load and validate all cases from a JSON file or directory."""

    root = Path(suite_path).expanduser().resolve()
    if not root.exists():
        raise EvalConfigurationError(f"Evaluation suite does not exist: {root}")
    if root.is_file():
        files = [root]
    else:
        files = sorted(path for path in root.rglob("*.json") if path.is_file())
    if not files:
        raise EvalConfigurationError(f"No evaluation JSON files found under: {root}")

    cases: list[EvalCase] = []
    seen_ids: set[str] = set()
    for source_path in files:
        payload = _read_suite_file(source_path)
        unknown_suite_keys = set(payload).difference(SUITE_KEYS)
        if unknown_suite_keys:
            raise EvalConfigurationError(
                f"{source_path}: unknown suite field(s): {', '.join(sorted(unknown_suite_keys))}"
            )
        raw_cases = payload.get("cases")
        if not isinstance(raw_cases, list) or not raw_cases:
            raise EvalConfigurationError(f"{source_path}: 'cases' must be a non-empty list.")
        for index, raw_case in enumerate(raw_cases, start=1):
            case = _parse_case(raw_case, source_path, index)
            if case.case_id in seen_ids:
                raise EvalConfigurationError(f"Duplicate evaluation case id: {case.case_id}")
            seen_ids.add(case.case_id)
            cases.append(case)
    return cases


def run_eval_suite(
    suite_path: str | Path,
    *,
    use_llm: bool = False,
    llm_client: LLMClient | None = None,
    llm_model: str | None = None,
    allow_llm_fallback: bool = True,
    llm_json_mode: bool | None = None,
    llm_timeout_seconds: int | None = None,
    iterative_agent: bool = False,
    agent_max_steps: int = 6,
    search_limit: int = 8,
    workflow_runner: WorkflowRunner = run_workflow,
) -> EvalSuiteResult:
    """Run every case and continue collecting results after individual failures."""

    if iterative_agent and not use_llm:
        raise EvalConfigurationError("Iterative agent evaluation requires --use-llm.")
    if agent_max_steps < 1:
        raise EvalConfigurationError("Agent max steps must be at least 1.")
    if search_limit < 1:
        raise EvalConfigurationError("Search limit must be at least 1.")

    resolved_suite = Path(suite_path).expanduser().resolve()
    cases = load_eval_cases(resolved_suite)
    started_at = datetime.now(timezone.utc).isoformat()
    suite_started = perf_counter()
    case_results: list[EvalCaseResult] = []
    observed_models: set[str] = set()

    for case in cases:
        case_started = perf_counter()
        try:
            report = workflow_runner(
                case.repo_path,
                case.task,
                case.validation_commands,
                search_limit=search_limit,
                use_llm=use_llm,
                llm_client=llm_client,
                llm_model=llm_model,
                allow_llm_fallback=allow_llm_fallback,
                llm_json_mode=llm_json_mode,
                llm_timeout_seconds=llm_timeout_seconds,
                use_memory=False,
                iterative_agent=iterative_agent,
                agent_max_steps=agent_max_steps,
            )
        except Exception as exc:  # A broken case must not hide the remaining suite results.
            case_results.append(_error_case_result(case, _elapsed_ms(case_started), exc))
            continue
        observed_models.update(_report_models(report))
        case_results.append(evaluate_report(case, report, _elapsed_ms(case_started)))

    passed_cases = sum(1 for result in case_results if result.passed)
    total_cases = len(case_results)
    model = None
    if use_llm:
        model = getattr(llm_client, "model", None) or llm_model or _join_models(observed_models)
    return EvalSuiteResult(
        suite_path=str(resolved_suite),
        mode=_eval_mode(use_llm, iterative_agent),
        model=model,
        started_at=started_at,
        duration_ms=_elapsed_ms(suite_started),
        total_cases=total_cases,
        passed_cases=passed_cases,
        failed_cases=total_cases - passed_cases,
        pass_rate=_percentage(passed_cases, total_cases),
        average_score=_average([result.score for result in case_results]),
        average_relevant_file_recall=_average_optional(
            [result.relevant_file_recall for result in case_results]
        ),
        average_proposal_file_recall=_average_optional(
            [result.proposal_file_recall for result in case_results]
        ),
        total_llm_calls=sum(result.llm_calls for result in case_results),
        total_llm_failures=sum(result.llm_failures for result in case_results),
        total_fallbacks=sum(result.fallback_count for result in case_results),
        total_llm_latency_ms=sum(result.llm_latency_ms for result in case_results),
        cases=case_results,
    )


def evaluate_report(case: EvalCase, report: WorkflowReport, duration_ms: int) -> EvalCaseResult:
    """Score a workflow report against one case's explicit expectations."""

    relevant_paths = [hit.path for hit in report.relevant_files]
    proposal_paths = [item.path for item in report.patch_proposal.files] if report.patch_proposal else []
    validation_passed = _validation_passed(report)
    call_traces = [trace for trace in report.llm_traces if _is_provider_call(trace)]
    llm_failures = sum(1 for trace in call_traces if trace.error or not trace.parsed)
    fallback_stages = _fallback_stages(report)
    criteria: list[EvalCriterionResult] = []
    expectations = case.expectations

    for expected_path in expectations.relevant_files:
        found = expected_path in relevant_paths
        rank = relevant_paths.index(expected_path) + 1 if found else None
        criteria.append(
            EvalCriterionResult(
                name=f"relevant_file:{expected_path}",
                passed=found,
                expected=True,
                actual=found,
                detail=f"rank={rank}" if rank else f"ranked={relevant_paths}",
            )
        )

    if expectations.top_relevant_file is not None:
        actual_top = relevant_paths[0] if relevant_paths else None
        criteria.append(
            EvalCriterionResult(
                name="top_relevant_file",
                passed=actual_top == expectations.top_relevant_file,
                expected=expectations.top_relevant_file,
                actual=actual_top,
            )
        )

    for expected_path in expectations.proposal_files:
        found = expected_path in proposal_paths
        criteria.append(
            EvalCriterionResult(
                name=f"proposal_file:{expected_path}",
                passed=found,
                expected=True,
                actual=found,
                detail=f"proposed={proposal_paths}",
            )
        )

    if expectations.proposal_ready is not None:
        actual_ready = bool(report.patch_proposal and report.patch_proposal.ready_for_patch)
        criteria.append(
            EvalCriterionResult(
                name="proposal_ready",
                passed=actual_ready == expectations.proposal_ready,
                expected=expectations.proposal_ready,
                actual=actual_ready,
            )
        )

    if expectations.min_plan_steps is not None:
        criteria.append(
            EvalCriterionResult(
                name="min_plan_steps",
                passed=len(report.plan) >= expectations.min_plan_steps,
                expected=f">={expectations.min_plan_steps}",
                actual=len(report.plan),
            )
        )

    if expectations.validation_passed is not None:
        criteria.append(
            EvalCriterionResult(
                name="validation_passed",
                passed=validation_passed == expectations.validation_passed,
                expected=expectations.validation_passed,
                actual=validation_passed,
            )
        )

    if expectations.max_llm_failures is not None:
        criteria.append(
            EvalCriterionResult(
                name="max_llm_failures",
                passed=llm_failures <= expectations.max_llm_failures,
                expected=f"<={expectations.max_llm_failures}",
                actual=llm_failures,
            )
        )

    if expectations.max_fallbacks is not None:
        criteria.append(
            EvalCriterionResult(
                name="max_fallbacks",
                passed=len(fallback_stages) <= expectations.max_fallbacks,
                expected=f"<={expectations.max_fallbacks}",
                actual=len(fallback_stages),
                detail=f"stages={fallback_stages}",
            )
        )

    if expectations.min_agent_steps is not None:
        criteria.append(
            EvalCriterionResult(
                name="min_agent_steps",
                passed=len(report.agent_steps) >= expectations.min_agent_steps,
                expected=f">={expectations.min_agent_steps}",
                actual=len(report.agent_steps),
            )
        )

    passed_count = sum(1 for criterion in criteria if criterion.passed)
    return EvalCaseResult(
        case_id=case.case_id,
        description=case.description,
        task=case.task,
        repo_path=str(case.repo_path),
        passed=passed_count == len(criteria),
        score=_percentage(passed_count, len(criteria)),
        duration_ms=duration_ms,
        criteria=criteria,
        files_scanned=report.files_scanned,
        relevant_files=relevant_paths,
        proposal_files=proposal_paths,
        plan_steps=len(report.plan),
        validation_commands=[result.command for result in report.validation],
        validation_passed=validation_passed,
        agent_steps=len(report.agent_steps),
        llm_calls=len(call_traces),
        llm_failures=llm_failures,
        fallback_count=len(fallback_stages),
        fallback_stages=fallback_stages,
        llm_latency_ms=sum(trace.latency_ms or 0 for trace in call_traces),
        relevant_file_recall=_recall(expectations.relevant_files, relevant_paths),
        proposal_file_recall=_recall(expectations.proposal_files, proposal_paths),
    )


def write_eval_report(result: EvalSuiteResult, output_path: str | Path) -> Path:
    """Write a secret-free JSON summary, excluding raw prompts and model outputs."""

    destination = Path(output_path).expanduser().resolve()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise EvalConfigurationError(f"Could not write evaluation report to {destination}: {exc}") from exc
    return destination


def _read_suite_file(source_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvalConfigurationError(f"{source_path}: invalid JSON: {exc}") from exc
    except OSError as exc:
        raise EvalConfigurationError(f"Could not read evaluation suite {source_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvalConfigurationError(f"{source_path}: suite root must be a JSON object.")
    return payload


def _parse_case(raw_case: Any, source_path: Path, index: int) -> EvalCase:
    location = f"{source_path}: case {index}"
    if not isinstance(raw_case, dict):
        raise EvalConfigurationError(f"{location} must be a JSON object.")
    unknown_case_keys = set(raw_case).difference(CASE_KEYS)
    if unknown_case_keys:
        raise EvalConfigurationError(
            f"{location} has unknown field(s): {', '.join(sorted(unknown_case_keys))}"
        )

    case_id = _required_string(raw_case, "id", location)
    task = _required_string(raw_case, "task", location)
    repo_value = _required_string(raw_case, "repo", location)
    description = raw_case.get("description", "")
    if not isinstance(description, str):
        raise EvalConfigurationError(f"{location}: 'description' must be a string.")
    validation_commands = _string_list(raw_case.get("validation_commands", []), "validation_commands", location)

    repo_path = Path(repo_value).expanduser()
    if not repo_path.is_absolute():
        repo_path = source_path.parent / repo_path
    repo_path = repo_path.resolve()
    if not repo_path.is_dir():
        raise EvalConfigurationError(f"{location}: repository directory does not exist: {repo_path}")

    raw_expectations = raw_case.get("expect")
    if not isinstance(raw_expectations, dict):
        raise EvalConfigurationError(f"{location}: 'expect' must be a JSON object.")
    unknown_expectation_keys = set(raw_expectations).difference(EXPECTATION_KEYS)
    if unknown_expectation_keys:
        raise EvalConfigurationError(
            f"{location} has unknown expectation(s): {', '.join(sorted(unknown_expectation_keys))}"
        )

    expectations = EvalExpectations(
        relevant_files=_path_list(raw_expectations.get("relevant_files", []), "relevant_files", location),
        top_relevant_file=_optional_path(raw_expectations, "top_relevant_file", location),
        proposal_files=_path_list(raw_expectations.get("proposal_files", []), "proposal_files", location),
        proposal_ready=_optional_bool(raw_expectations, "proposal_ready", location),
        min_plan_steps=_optional_non_negative_int(raw_expectations, "min_plan_steps", location),
        validation_passed=_optional_bool(raw_expectations, "validation_passed", location),
        max_llm_failures=_optional_non_negative_int(raw_expectations, "max_llm_failures", location),
        max_fallbacks=_optional_non_negative_int(raw_expectations, "max_fallbacks", location),
        min_agent_steps=_optional_non_negative_int(raw_expectations, "min_agent_steps", location),
    )
    if _expectation_count(expectations) == 0:
        raise EvalConfigurationError(f"{location}: at least one expectation is required.")
    if expectations.validation_passed is not None and not validation_commands:
        raise EvalConfigurationError(
            f"{location}: 'validation_passed' requires at least one validation command."
        )

    return EvalCase(
        case_id=case_id,
        description=description,
        task=task,
        repo_path=repo_path,
        validation_commands=validation_commands,
        expectations=expectations,
        source_path=source_path,
    )


def _required_string(mapping: dict[str, Any], key: str, location: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EvalConfigurationError(f"{location}: '{key}' must be a non-empty string.")
    return value.strip()


def _string_list(value: Any, key: str, location: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise EvalConfigurationError(f"{location}: '{key}' must be a list of non-empty strings.")
    return [item.strip() for item in value]


def _path_list(value: Any, key: str, location: str) -> list[str]:
    return [_normalize_path(item) for item in _string_list(value, key, location)]


def _optional_path(mapping: dict[str, Any], key: str, location: str) -> str | None:
    if key not in mapping:
        return None
    value = mapping[key]
    if not isinstance(value, str) or not value.strip():
        raise EvalConfigurationError(f"{location}: '{key}' must be a non-empty string.")
    return _normalize_path(value)


def _optional_bool(mapping: dict[str, Any], key: str, location: str) -> bool | None:
    if key not in mapping:
        return None
    value = mapping[key]
    if not isinstance(value, bool):
        raise EvalConfigurationError(f"{location}: '{key}' must be true or false.")
    return value


def _optional_non_negative_int(mapping: dict[str, Any], key: str, location: str) -> int | None:
    if key not in mapping:
        return None
    value = mapping[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvalConfigurationError(f"{location}: '{key}' must be a non-negative integer.")
    return value


def _normalize_path(value: str) -> str:
    return PurePosixPath(value.replace("\\", "/")).as_posix()


def _expectation_count(expectations: EvalExpectations) -> int:
    return (
        len(expectations.relevant_files)
        + len(expectations.proposal_files)
        + sum(
            value is not None
            for value in (
                expectations.top_relevant_file,
                expectations.proposal_ready,
                expectations.min_plan_steps,
                expectations.validation_passed,
                expectations.max_llm_failures,
                expectations.max_fallbacks,
                expectations.min_agent_steps,
            )
        )
    )


def _validation_passed(report: WorkflowReport) -> bool | None:
    if not report.validation:
        return None
    return all(result.allowed and result.exit_code == 0 for result in report.validation)


def _is_provider_call(trace) -> bool:
    return trace.latency_ms is not None or bool(trace.prompt_preview)


def _fallback_stages(report: WorkflowReport) -> list[str]:
    stages = {trace.name for trace in report.llm_traces if trace.fallback_used}
    if report.plan_metadata.fallback_used:
        stages.add("planner")
    if report.patch_proposal_metadata.fallback_used:
        stages.add("patch_proposal")
    if report.patch_review and report.patch_review.fallback_used:
        stages.add("patch_review")
    return sorted(stages)


def _report_models(report: WorkflowReport) -> set[str]:
    models = {trace.model for trace in report.llm_traces if trace.model and trace.model != "unknown"}
    if report.plan_metadata.model:
        models.add(report.plan_metadata.model)
    if report.patch_proposal_metadata.model:
        models.add(report.patch_proposal_metadata.model)
    if report.patch_review and report.patch_review.model:
        models.add(report.patch_review.model)
    return models


def _join_models(models: set[str]) -> str | None:
    return ", ".join(sorted(models)) if models else None


def _recall(expected: list[str], actual: list[str]) -> float | None:
    if not expected:
        return None
    found = sum(1 for path in expected if path in actual)
    return round(found / len(expected), 4)


def _error_case_result(case: EvalCase, duration_ms: int, exc: Exception) -> EvalCaseResult:
    error = str(exc).strip() or exc.__class__.__name__
    criterion = EvalCriterionResult(
        name="workflow_completed",
        passed=False,
        expected=True,
        actual=False,
        detail=error[:1000],
    )
    return EvalCaseResult(
        case_id=case.case_id,
        description=case.description,
        task=case.task,
        repo_path=str(case.repo_path),
        passed=False,
        score=0.0,
        duration_ms=duration_ms,
        criteria=[criterion],
        error=error[:1000],
    )


def _eval_mode(use_llm: bool, iterative_agent: bool) -> str:
    if not use_llm:
        return "deterministic"
    return "llm-iterative" if iterative_agent else "llm"


def _percentage(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator * 100 / denominator, 2)


def _average(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 2)


def _average_optional(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return round(sum(present) / len(present), 4)


def _elapsed_ms(started: float) -> int:
    return int((perf_counter() - started) * 1000)
