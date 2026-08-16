"""Local web UI server for RepoPilot Agent."""

from __future__ import annotations

import json
import mimetypes
import threading
import time
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlparse

from .agent_validation import (
    AgentValidationError,
    AgentValidationResult,
    continue_agent_after_validation,
    execute_pending_agent_validation,
    reject_pending_agent_validation,
    request_agent_validation,
)
from .agent_repair import (
    AgentRepairTransition,
    blocked_agent_repair_fingerprints,
    observe_agent_repair_proposal,
    observe_agent_validation,
    observe_agent_write,
    render_agent_repair_context,
    stop_agent_repair,
)
from .agent_write import (
    AgentWriteError,
    execute_pending_agent_write,
    reject_pending_agent_write,
)
from .execution import (
    ExecutionBudget,
    ExecutionUsage,
    build_acceptance_criteria,
    completion_from_record,
    criteria_from_records,
    evaluate_completion,
    execution_budget_state,
)
from .execution_profile import TaskRunExecutionProfile, create_execution_profile
from .git_tools import get_git_diff, inspect_repository
from .git_summary import build_git_workflow_summary, build_pull_request_readiness
from .github_tools import create_github_pull_request, inspect_github_repository
from .llm.base import LLMError, LLMMessage
from .llm.openai_compatible import OpenAICompatibleClient
from .memory import MemoryStore, default_memory_path, ensure_local_state_ignored
from .models import FileEditProposal, ValidationResult
from .patch_apply import ApplyResult, apply_file_edits, capture_file_snapshots, revert_file_snapshots
from .repo_source import resolve_repository_reference, sync_repository_reference
from .repair_loop import (
    STOP_EXECUTION_BUDGET,
    STOP_NO_PROPOSAL,
    STOP_NO_REPOSITORY_CHANGE,
    STOP_REPAIR_BUDGET,
    STOP_REPEATED_PROPOSAL,
    RepairAttemptRecord,
    RepairLoopStopped,
    latest_failure_fingerprint,
    mark_repair_attempt_stopped,
    proposal_changes_repository,
    record_repair_proposal,
    record_validation_outcome,
    validation_feedback_fingerprint,
)
from .recovery import TaskRunRecoveryReadiness, inspect_task_run_recovery
from .runtime import (
    SQLiteRuntimeStore,
    agent_completion_blockers,
    agent_completion_ready,
    agent_working_state_from_record,
    stop_agent_working_state,
)
from .safety import SafetyCheckError
from .task_runs import (
    ACTIVE_TASK_RUN_STATUSES,
    TaskRun,
    TaskRunError,
    checkpoint_task_run,
    create_task_run,
    create_task_run_branch,
    get_task_run,
    mark_task_run_interrupted,
    prepare_task_run_resume,
    record_task_run_checkpoint,
    request_task_run_cancel,
    request_task_run_pause,
    task_run_from_record,
    update_task_run,
    validate_task_run_resume_request,
)
from .validation_feedback import build_validation_feedback
from .validator import run_validation
from .structured_patch import apply_structured_patch, current_file_sha256
from .web_sessions import (
    DEFAULT_MAX_REPAIR_ATTEMPTS,
    ProposalSession,
    TimelineEvent,
    append_timeline,
    build_report_timeline,
    create_proposal_session,
    get_proposal_session,
    proposal_session_from_record,
    proposal_session_to_record,
)
from .worktree_sandbox import (
    DirtyWorktreeError,
    WorktreeSandboxError,
    create_worktree_sandbox,
    list_worktree_sandboxes,
    remove_worktree_sandbox,
)
from .workflow import run_workflow

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
MAX_REPAIR_ATTEMPTS_LIMIT = 5
MAX_AGENT_WRITE_HISTORY = 20

_SESSION_PUBLIC_KEYS = (
    "parent_proposal_id",
    "repair_attempt",
    "max_repair_attempts",
    "repair_budget_remaining",
    "next_repair_attempt",
    "repair_budget_exhausted",
    "structured_patches",
    "acceptance_criteria",
    "execution_budget",
    "completion_evidence",
    "root_task",
    "repair_history",
    "repair_stop_reason",
    "repair_stop_message",
    "auto_repair_enabled",
)


def recover_interrupted_task_runs(source_repo: str | Path) -> list[TaskRun]:
    source = Path(source_repo).expanduser().resolve()
    memory_path = default_memory_path(source)
    if not memory_path.is_file():
        return []
    store = MemoryStore(memory_path)
    recovered: list[TaskRun] = []
    for record in store.list_task_runs_by_status(ACTIVE_TASK_RUN_STATUSES):
        run_id = str(record.get("run_id") or "").strip()
        if not run_id or get_task_run(run_id) is not None:
            continue
        task_run = task_run_from_record(record)
        mark_task_run_interrupted(task_run)
        store.save_task_run(task_run.to_record())
        recovered.append(task_run)
    return recovered


def run_web_server(host: str = "127.0.0.1", port: int = 8765, repo: str | Path = ".") -> None:
    server = ThreadingHTTPServer((host, port), RepoPilotRequestHandler)
    try:
        interrupted = recover_interrupted_task_runs(repo)
    except Exception as exc:
        interrupted = []
        print(f"Warning: unfinished task-run recovery scan failed: {exc}")
    print(f"RepoPilot web UI running at http://{host}:{port}")
    if interrupted:
        print(f"Marked {len(interrupted)} unfinished task run(s) as interrupted.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping RepoPilot web UI.")
    finally:
        server.server_close()


class RepoPilotRequestHandler(BaseHTTPRequestHandler):
    server_version = "RepoPilotWeb/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/git/status":
            self._handle_git_status(parsed.query)
            return
        if parsed.path == "/api/git/diff":
            self._handle_git_diff(parsed.query)
            return
        if parsed.path == "/api/github/status":
            self._handle_github_status(parsed.query)
            return
        if parsed.path == "/api/history":
            self._handle_history_list(parsed.query)
            return
        if parsed.path == "/api/history/run":
            self._handle_history_detail(parsed.query)
            return
        if parsed.path == "/api/sandbox/list":
            self._handle_sandbox_list(parsed.query)
            return
        if parsed.path == "/api/task-runs":
            self._handle_task_run_list(parsed.query)
            return
        if parsed.path == "/api/task-runs/status":
            self._handle_task_run_status(parsed.query)
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/run":
            self._handle_run()
            return
        if parsed.path == "/api/propose":
            self._handle_propose()
            return
        if parsed.path == "/api/apply":
            self._handle_apply()
            return
        if parsed.path == "/api/revert":
            self._handle_revert()
            return
        if parsed.path == "/api/repair/propose":
            self._handle_repair_propose()
            return
        if parsed.path == "/api/git/summary":
            self._handle_git_summary()
            return
        if parsed.path == "/api/github/pr/readiness":
            self._handle_pr_readiness()
            return
        if parsed.path == "/api/github/pr/draft":
            self._handle_git_summary()
            return
        if parsed.path == "/api/github/pr/create":
            self._handle_pr_create()
            return
        if parsed.path == "/api/repository/sync":
            self._handle_repository_sync()
            return
        if parsed.path == "/api/sandbox/create":
            self._handle_sandbox_create()
            return
        if parsed.path == "/api/sandbox/remove":
            self._handle_sandbox_remove()
            return
        if parsed.path == "/api/task-runs/start":
            self._handle_task_run_start()
            return
        if parsed.path == "/api/task-runs/pause":
            self._handle_task_run_pause()
            return
        if parsed.path == "/api/task-runs/recovery/readiness":
            self._handle_task_run_recovery_readiness()
            return
        if parsed.path == "/api/task-runs/resume":
            self._handle_task_run_resume()
            return
        if parsed.path == "/api/task-runs/cancel":
            self._handle_task_run_cancel()
            return
        if parsed.path == "/api/task-runs/branch":
            self._handle_task_run_branch()
            return
        if parsed.path == "/api/task-runs/runtime-approval/grant":
            self._handle_runtime_approval_grant()
            return
        if parsed.path == "/api/task-runs/runtime-approval/reject":
            self._handle_runtime_approval_reject()
            return
        if parsed.path == "/api/history/delete":
            self._handle_history_delete()
            return
        if parsed.path == "/api/history/clear":
            self._handle_history_clear()
            return
        if parsed.path == "/api/history/pin":
            self._handle_history_pin()
            return
        if parsed.path == "/api/llm/test":
            self._handle_llm_test()
            return
        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _handle_run(self) -> None:
        payload = self._read_json()
        task = str(payload.get("task") or "").strip()
        if not task:
            self._send_json({"error": "Task is required."}, status=HTTPStatus.BAD_REQUEST)
            return

        validation = payload.get("validation") or []
        if not isinstance(validation, list) or not all(isinstance(item, str) for item in validation):
            self._send_json({"error": "validation must be a list of strings."}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            execution_budget = _payload_execution_budget(payload)
            _validate_validation_budget(validation, execution_budget)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        repo_source = self._resolve_payload_repository_or_error(payload)
        if repo_source is None:
            return

        use_llm = bool(payload.get("use_llm"))
        llm_client = None
        if use_llm and payload.get("api_key"):
            try:
                llm_client = OpenAICompatibleClient(
                    api_key=str(payload.get("api_key")),
                    base_url=str(payload.get("base_url") or "") or None,
                    model=str(payload.get("model") or "") or None,
                    json_mode=_payload_json_mode(payload),
                    timeout_seconds=_payload_llm_timeout_seconds(payload),
                )
            except LLMError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return

        try:
            report = run_workflow(
                repo_source.local_path,
                task,
                validation_commands=validation,
                use_llm=use_llm,
                llm_client=llm_client,
                llm_model=str(payload.get("model") or "") or None,
                allow_llm_fallback=not bool(payload.get("no_llm_fallback")),
                llm_json_mode=_payload_json_mode(payload),
                llm_timeout_seconds=_payload_llm_timeout_seconds(payload),
                iterative_agent=_payload_iterative_agent(payload),
                agent_max_steps=_payload_agent_max_steps(payload),
                use_memory=_payload_use_memory(payload),
                execution_budget=execution_budget,
            )
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        data = report.to_dict()
        data["repository_source"] = repo_source.to_dict()
        timeline = build_report_timeline(report)
        data["timeline"] = [asdict(event) for event in timeline]
        try:
            data["run_id"] = self._memory(report.repo_path).create_run(
                repo_path=report.repo_path,
                task=task,
                mode="run",
                report=report,
                timeline=[asdict(event) for event in timeline],
            )
        except Exception as exc:
            data["memory_error"] = str(exc)
        self._send_json(data)

    def _handle_apply(self) -> None:
        payload = self._read_json()
        proposal_id = str(payload.get("proposal_id") or "").strip()
        if not proposal_id:
            self._send_json({"error": "proposal_id is required."}, status=HTTPStatus.BAD_REQUEST)
            return
        session = self._get_session_or_restore(proposal_id, payload)
        if session is None:
            self._send_json({"error": "Unknown proposal_id."}, status=HTTPStatus.BAD_REQUEST)
            return
        if session.applied:
            self._send_json({"error": "Proposal has already been applied."}, status=HTTPStatus.BAD_REQUEST)
            return
        auto_repair_launch = False
        try:
            approved_paths = _payload_approved_paths(payload, session.file_edits)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        task_run = None
        task_run_id = str(payload.get("task_run_id") or "").strip()
        if task_run_id:
            task_payload = dict(payload)
            task_payload["run_id"] = task_run_id
            task_run = self._task_run_from_payload_or_error(task_payload)
            if task_run is None:
                return
            if task_run.proposal_id != proposal_id:
                self._send_json(
                    {"error": "proposal_id does not belong to this task run."},
                    status=HTTPStatus.CONFLICT,
                )
                return
            if not task_run.sandbox_path or Path(session.repo_path).resolve() != Path(task_run.sandbox_path).resolve():
                self._send_json(
                    {"error": "Proposal repository does not match the task-run sandbox."},
                    status=HTTPStatus.CONFLICT,
                )
                return
            if "auto_repair" in payload:
                task_run.auto_repair_enabled = _payload_auto_repair(payload)
                session.auto_repair_enabled = task_run.auto_repair_enabled
        active_budget = task_run.execution_budget if task_run else session.execution_budget
        active_usage = task_run.execution_usage if task_run else session.execution_usage
        current_budget_state = execution_budget_state(active_budget, active_usage)
        if current_budget_state["exhausted"]:
            if task_run:
                update_task_run(
                    task_run,
                    "awaiting_approval",
                    "Execution budget is exhausted; approved edits were not applied.",
                    error="; ".join(current_budget_state["exhausted_reasons"]),
                )
                self._persist_task_run(task_run)
            self._send_json(
                {
                    "error": "Execution budget is exhausted; approved edits were not applied.",
                    "execution_budget": current_budget_state,
                },
                status=HTTPStatus.CONFLICT,
            )
            return
        if active_usage.validation_commands + len(session.validation_commands) > active_budget.max_validation_commands:
            if task_run:
                update_task_run(
                    task_run,
                    "awaiting_approval",
                    "Validation budget is insufficient; approved edits were not applied.",
                )
                self._persist_task_run(task_run)
            self._send_json(
                {
                    "error": "Validation command budget would be exceeded before approved edits can be verified.",
                    "execution_budget": execution_budget_state(active_budget, active_usage),
                },
                status=HTTPStatus.CONFLICT,
            )
            return
        required_tool_calls = len(approved_paths) + len(session.validation_commands)
        if active_usage.tool_calls + required_tool_calls > active_budget.max_tool_calls:
            if task_run:
                update_task_run(
                    task_run,
                    "awaiting_approval",
                    "Tool-call budget is insufficient; approved edits were not applied.",
                )
                self._persist_task_run(task_run)
            self._send_json(
                {
                    "error": "Tool-call budget would be exceeded before approved edits can be applied and verified.",
                    "execution_budget": execution_budget_state(active_budget, active_usage),
                },
                status=HTTPStatus.CONFLICT,
            )
            return
        if task_run:
            update_task_run(task_run, "applying", "Applying the human-approved proposal in the task sandbox.")
            self._persist_task_run(task_run)
        automated_started = time.monotonic()
        structured_patch_results: list[dict[str, Any]] = []
        try:
            approved_path_set = set(approved_paths)
            approved_edits = [edit for edit in session.file_edits if edit.path in approved_path_set]
            session.approved_paths = approved_paths
            session.applied_paths = []
            append_timeline(
                session,
                "approval",
                "done",
                f"Approved {len(approved_edits)} of {len(session.file_edits)} proposed file edit(s).",
            )
            rollback_snapshot = capture_file_snapshots(session.repo_path, approved_edits)
            patches_by_path = {patch.path: patch for patch in session.structured_patches}
            approved_patches = [patches_by_path[path] for path in approved_paths if path in patches_by_path]
            if len(approved_patches) == len(approved_edits):
                for patch in approved_patches:
                    current_hash = current_file_sha256(session.repo_path, patch.path)
                    if current_hash != patch.expected_sha256:
                        raise ValueError(
                            f"Structured patch conflict for {patch.path}: the file changed after proposal generation."
                        )
                changed_files: list[str] = []
                diff_parts: list[str] = []
                try:
                    for structured_patch in approved_patches:
                        patch_result = apply_structured_patch(
                            session.repo_path,
                            structured_patch,
                            task=session.task,
                            allowed_paths=session.allowed_paths,
                        )
                        structured_patch_results.append(patch_result.to_dict())
                        if patch_result.status != "applied":
                            raise ValueError(
                                f"Structured patch for {structured_patch.path} did not apply: {patch_result.message}"
                            )
                        changed_files.append(structured_patch.path)
                        diff_parts.append(patch_result.diff)
                except Exception:
                    applied_snapshots = [
                        snapshot for snapshot in rollback_snapshot if snapshot.path in changed_files
                    ]
                    if applied_snapshots:
                        revert_file_snapshots(session.repo_path, applied_snapshots)
                    raise
                result = ApplyResult(
                    applied=bool(changed_files),
                    changed_files=changed_files,
                    diff="\n".join(part for part in diff_parts if part),
                    message=f"Applied {len(changed_files)} approved structured patch(es).",
                )
            else:
                result = apply_file_edits(
                    session.repo_path,
                    approved_edits,
                    task=session.task,
                    allowed_paths=session.allowed_paths,
                )
            session.applied = True
            session.reverted = False
            session.applied_paths = result.changed_files
            session.rollback_snapshot = rollback_snapshot if result.applied else []
            apply_mode = "structured hunks" if structured_patch_results else "compatible file edits"
            append_timeline(session, "apply", "done", f"{result.message} Mode: {apply_mode}.")
            if session.rollback_snapshot:
                append_timeline(
                    session,
                    "rollback",
                    "ready",
                    f"Rollback snapshot captured for {len(session.rollback_snapshot)} file(s).",
                )
            if session.validation_commands:
                if task_run:
                    update_task_run(task_run, "validating", "Running allowlisted validation commands in the sandbox.")
                    self._persist_task_run(task_run)
                validation = run_validation(session.repo_path, session.validation_commands)
                session.validation = validation
                session.validation_feedback = build_validation_feedback(
                    validation,
                    task=session.task,
                    repo_path=session.repo_path,
                )
                failed = [item for item in validation if item.exit_code not in (0, None)]
                rejected = [item for item in validation if not item.allowed]
                if failed or rejected:
                    append_timeline(
                        session,
                        "validation",
                        "warning",
                        f"Validation completed with {len(failed)} failed and {len(rejected)} rejected command(s).",
                    )
                    if session.validation_feedback:
                        if session.repair_budget_exhausted():
                            append_timeline(
                                session,
                                "repair",
                                "blocked",
                                f"Repair retry budget exhausted ({session.repair_attempt}/{session.max_repair_attempts}).",
                            )
                        else:
                            append_timeline(
                                session,
                                "repair",
                                "available",
                                (
                                    f"{session.validation_feedback.summary} "
                                    f"Next repair attempt: {session.next_repair_attempt()}/"
                                    f"{session.max_repair_attempts}."
                                ),
                            )
                else:
                    append_timeline(session, "validation", "done", f"Ran {len(validation)} validation command(s).")
            else:
                append_timeline(session, "validation", "skipped", "No validation command was configured.")
            session.execution_usage = active_usage.add(
                tool_calls=len(result.changed_files) + len(session.validation),
                validation_commands=len(session.validation),
                elapsed_ms=max(int((time.monotonic() - automated_started) * 1000), 0),
            )
            if session.validation_feedback:
                session.repair_history, repair_decision = record_validation_outcome(
                    session.repair_history,
                    attempt=session.repair_attempt,
                    validation=session.validation,
                    summary=session.validation_feedback.summary,
                )
                if not repair_decision.accepted:
                    session.repair_stop_reason = repair_decision.stop_reason
                    session.repair_stop_message = repair_decision.message
                    append_timeline(session, "repair", "stopped", repair_decision.message)
                elif session.repair_budget_exhausted():
                    session.repair_stop_reason = STOP_REPAIR_BUDGET
                    session.repair_stop_message = (
                        f"Repair retry budget exhausted ({session.repair_attempt}/"
                        f"{session.max_repair_attempts})."
                    )
                    append_timeline(session, "repair", "stopped", session.repair_stop_message)
                else:
                    session.repair_stop_reason = None
                    session.repair_stop_message = ""
            elif session.repair_attempt > 0 and session.validation_commands:
                session.repair_history, _ = record_validation_outcome(
                    session.repair_history,
                    attempt=session.repair_attempt,
                    validation=session.validation,
                    summary="Validation passed after the approved repair.",
                )
                session.repair_stop_reason = None
                session.repair_stop_message = ""
            session.completion_evidence = evaluate_completion(
                session.acceptance_criteria,
                changed_files=result.changed_files,
                approved_paths=approved_paths,
                validation=session.validation,
                diff=result.diff,
            )
            append_timeline(
                session,
                "acceptance",
                "done" if session.completion_evidence.status == "passed" else "warning",
                session.completion_evidence.summary,
            )
        except SafetyCheckError as exc:
            append_timeline(session, "safety", "blocked", "Pre-apply safety check blocked this proposal.")
            self._persist_session(session)
            if task_run:
                update_task_run(
                    task_run,
                    "awaiting_approval",
                    "Safety checks blocked apply. Review the proposal before trying again.",
                    error=str(exc),
                )
                record_task_run_checkpoint(
                    task_run,
                    "apply_blocked",
                    "Pre-apply safety checks blocked the proposal without applying it.",
                    "review_proposal",
                )
                self._persist_task_run(task_run)
            self._send_json(
                {
                    "error": str(exc),
                    "safety_check": exc.result.to_dict(),
                    "timeline": session.to_public_dict()["timeline"],
                },
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        except (FileNotFoundError, ValueError) as exc:
            if task_run:
                update_task_run(task_run, "failed", "Proposal application failed.", error=str(exc))
                record_task_run_checkpoint(
                    task_run,
                    "application_failed",
                    "Proposal application failed before completion.",
                    "inspect_failure",
                )
                self._persist_task_run(task_run)
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:
            if task_run:
                update_task_run(task_run, "failed", "Proposal application failed.", error=str(exc))
                record_task_run_checkpoint(
                    task_run,
                    "application_failed",
                    "Proposal application failed before completion.",
                    "inspect_failure",
                )
                self._persist_task_run(task_run)
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        data = result.to_dict()
        data["proposal_id"] = proposal_id
        data["validation"] = [asdict(item) for item in session.validation]
        data["validation_feedback"] = (
            asdict(session.validation_feedback) if session.validation_feedback else None
        )
        data["structured_patch_results"] = structured_patch_results
        public_session = session.to_public_dict()
        data["timeline"] = public_session["timeline"]
        data["rollback_available"] = public_session["rollback_available"]
        data["reverted"] = public_session["reverted"]
        data["approved_paths"] = public_session["approved_paths"]
        data["applied_paths"] = public_session["applied_paths"]
        _add_session_public_fields(data, session)
        self._persist_session(session)
        if task_run:
            failed = [item for item in session.validation if item.exit_code not in (0, None)]
            rejected = [item for item in session.validation if not item.allowed]
            task_result = dict(task_run.result or {})
            task_result["apply_result"] = data
            task_result["timeline"] = data["timeline"]
            task_result["validation"] = data["validation"]
            task_result["validation_feedback"] = data["validation_feedback"]
            task_result["completion_evidence"] = public_session["completion_evidence"]
            task_result["execution_budget"] = public_session["execution_budget"]
            task_run.acceptance_criteria = list(session.acceptance_criteria)
            task_run.execution_usage = session.execution_usage
            task_run.completion_evidence = session.completion_evidence
            task_run.repair_history = list(session.repair_history)
            task_run.repair_stop_reason = session.repair_stop_reason
            task_run.repair_stop_message = session.repair_stop_message
            execution_exhausted = bool(public_session["execution_budget"]["exhausted"])
            if failed or rejected:
                if session.repair_stop_reason:
                    next_status = "failed"
                    message = session.repair_stop_message or "The repair loop stopped without progress."
                elif task_run.auto_repair_enabled and bool(payload.get("use_llm")):
                    next_status = "diagnosing"
                    message = "Validation failed. The Agent is diagnosing the failure for a bounded repair."
                    auto_repair_launch = True
                else:
                    next_status = "repair_pending"
                    message = (
                        "Validation needs attention. Review feedback and generate a bounded repair proposal."
                        if not task_run.auto_repair_enabled
                        else "Validation needs attention. Enable LLM use to generate the next repair automatically."
                    )
            elif execution_exhausted:
                next_status = "failed"
                message = "Execution evidence passed, but the configured execution budget was exceeded."
            elif session.completion_evidence and session.completion_evidence.status == "passed":
                next_status = "completed"
                message = "Approved changes, validation, and required acceptance criteria completed successfully."
            else:
                next_status = "failed"
                message = "Approved edits finished, but required completion evidence is incomplete."
            task_run.result = task_result
            task_run.error = None
            if task_run.cancel_requested:
                checkpoint_task_run(task_run, next_status)
                auto_repair_launch = False
            elif task_run.pause_requested and next_status in {"repair_pending", "diagnosing"}:
                checkpoint_task_run(task_run, "repair_pending")
                auto_repair_launch = False
            else:
                task_run.pause_requested = False
                update_task_run(task_run, next_status, message)
                next_action = {
                    "completed": "review_diff",
                    "repair_pending": "generate_repair",
                    "diagnosing": "wait_for_repair",
                    "failed": "inspect_failure",
                }.get(next_status, "review_task")
                record_task_run_checkpoint(
                    task_run,
                    "validation_complete" if session.validation else "application_complete",
                    message,
                    next_action,
                )
            self._persist_task_run(task_run)
            data["task_run"] = task_run.to_public_dict()
        try:
            self._memory(session.repo_path).mark_proposal_applied(
                proposal_id,
                session.validation,
                data["timeline"],
            )
        except Exception as exc:
            data["memory_error"] = str(exc)
        self._send_json(data)
        if auto_repair_launch and task_run:
            self._launch_auto_repair_worker(task_run, session, payload)

    def _handle_revert(self) -> None:
        payload = self._read_json()
        proposal_id = str(payload.get("proposal_id") or "").strip()
        if not proposal_id:
            self._send_json({"error": "proposal_id is required."}, status=HTTPStatus.BAD_REQUEST)
            return
        session = self._get_session_or_restore(proposal_id, payload)
        if session is None:
            self._send_json({"error": "Unknown proposal_id."}, status=HTTPStatus.BAD_REQUEST)
            return
        if not session.applied:
            self._send_json({"error": "Proposal is not currently applied."}, status=HTTPStatus.BAD_REQUEST)
            return
        if session.reverted or not session.rollback_snapshot:
            self._send_json({"error": "No rollback snapshot is available."}, status=HTTPStatus.BAD_REQUEST)
            return

        task_run = None
        task_run_id = str(payload.get("task_run_id") or "").strip()
        if task_run_id:
            task_payload = dict(payload)
            task_payload["run_id"] = task_run_id
            task_run = self._task_run_from_payload_or_error(task_payload)
            if task_run is None:
                return
            if task_run.proposal_id != proposal_id:
                self._send_json(
                    {"error": "proposal_id does not belong to this task run."},
                    status=HTTPStatus.CONFLICT,
                )
                return
            if not task_run.sandbox_path or Path(session.repo_path).resolve() != Path(task_run.sandbox_path).resolve():
                self._send_json(
                    {"error": "Proposal repository does not match the task-run sandbox."},
                    status=HTTPStatus.CONFLICT,
                )
                return

        try:
            result = revert_file_snapshots(session.repo_path, session.rollback_snapshot)
            session.applied = False
            session.reverted = True
            session.validation_feedback = None
            append_timeline(session, "rollback", "done", result.message)
            self._persist_session(session)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            append_timeline(session, "rollback", "blocked", str(exc))
            self._persist_session(session)
            self._send_json(
                {
                    "error": str(exc),
                    "timeline": session.to_public_dict()["timeline"],
                    "rollback_available": session.to_public_dict()["rollback_available"],
                },
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        data = result.to_dict()
        public_session = session.to_public_dict()
        data["proposal_id"] = proposal_id
        data["timeline"] = public_session["timeline"]
        data["rollback_available"] = public_session["rollback_available"]
        data["reverted"] = public_session["reverted"]
        _add_session_public_fields(data, session)
        if task_run:
            task_result = dict(task_run.result or {})
            task_result["revert_result"] = data
            update_task_run(
                task_run,
                "cancelled",
                "Applied task changes were reverted. The sandbox was preserved.",
                result=task_result,
            )
            self._persist_task_run(task_run)
            data["task_run"] = task_run.to_public_dict()
        try:
            self._memory(session.repo_path).mark_proposal_reverted(proposal_id, data["timeline"])
        except Exception as exc:
            data["memory_error"] = str(exc)
        self._send_json(data)

    def _handle_repair_propose(self) -> None:
        payload = self._read_json()
        proposal_id = str(payload.get("proposal_id") or "").strip()
        if not proposal_id:
            self._send_json({"error": "proposal_id is required."}, status=HTTPStatus.BAD_REQUEST)
            return
        session = self._get_session_or_restore(proposal_id, payload)
        if session is None:
            self._send_json({"error": "Unknown proposal_id."}, status=HTTPStatus.BAD_REQUEST)
            return
        if session.reverted:
            self._send_json({"error": "Proposal has been reverted."}, status=HTTPStatus.BAD_REQUEST)
            return
        if session.validation_feedback is None:
            self._send_json({"error": "No validation feedback is available for this proposal."}, status=HTTPStatus.BAD_REQUEST)
            return

        task_run = None
        task_run_id = str(payload.get("task_run_id") or "").strip()
        if task_run_id:
            task_payload = dict(payload)
            task_payload["run_id"] = task_run_id
            task_run = self._task_run_from_payload_or_error(task_payload)
            if task_run is None:
                return
            if task_run.proposal_id != proposal_id:
                self._send_json(
                    {"error": "proposal_id does not belong to this task run."},
                    status=HTTPStatus.CONFLICT,
                )
                return
            if not task_run.sandbox_path or Path(session.repo_path).resolve() != Path(task_run.sandbox_path).resolve():
                self._send_json(
                    {"error": "Proposal repository does not match the task-run sandbox."},
                    status=HTTPStatus.CONFLICT,
                )
                return
        try:
            llm_client = _payload_llm_client(payload)
            data = self._generate_repair_proposal(session, payload, task_run, llm_client)
        except RepairLoopStopped as exc:
            data = session.to_public_dict()
            data["error"] = str(exc)
            if task_run:
                data["task_run"] = task_run.to_public_dict()
            status = HTTPStatus.BAD_REQUEST if exc.reason == STOP_REPAIR_BUDGET else HTTPStatus.CONFLICT
            self._send_json(data, status=status)
            return
        except LLMError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:
            if task_run and task_run.status not in {"cancelled", "paused", "failed"}:
                update_task_run(task_run, "repair_pending", "Repair proposal generation failed.", error=str(exc))
                record_task_run_checkpoint(
                    task_run,
                    "repair_generation_failed",
                    "Repair proposal generation failed before a new proposal was available.",
                    "retry_repair",
                )
                self._persist_task_run(task_run)
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json(data)

    def _generate_repair_proposal(
        self,
        session: ProposalSession,
        payload: dict[str, Any],
        task_run: TaskRun | None,
        llm_client: OpenAICompatibleClient | None,
    ) -> dict[str, Any]:
        if session.repair_stop_reason:
            raise RepairLoopStopped(
                session.repair_stop_reason,
                session.repair_stop_message or "The repair loop has already stopped.",
            )
        if session.repair_budget_exhausted():
            message = (
                f"Repair retry budget exhausted ({session.repair_attempt}/"
                f"{session.max_repair_attempts})."
            )
            self._stop_repair_loop(session, task_run, STOP_REPAIR_BUDGET, message)
            raise RepairLoopStopped(STOP_REPAIR_BUDGET, message)

        try:
            remaining_budget = _remaining_repair_execution_budget(
                session,
                iterative_agent=_payload_iterative_agent(payload),
            )
        except RepairLoopStopped as exc:
            self._stop_repair_loop(session, task_run, exc.reason, str(exc))
            raise

        next_repair_attempt = session.repair_attempt + 1
        trigger_fingerprint = latest_failure_fingerprint(session.repair_history)
        if not trigger_fingerprint:
            trigger_fingerprint = validation_feedback_fingerprint(session.validation_feedback)
        repair_task = _repair_task_with_history(
            session.validation_feedback.repair_task,
            root_task=session.root_task or session.task,
            history=session.repair_history,
            attempt=next_repair_attempt,
            max_attempts=session.max_repair_attempts,
        )
        if task_run:
            update_task_run(task_run, "diagnosing", "Agent is normalizing validation evidence for repair.")
            self._persist_task_run(task_run)
            if checkpoint_task_run(task_run, "repair_pending"):
                self._persist_task_run(task_run)
                raise TaskRunError("Repair generation stopped at the diagnosis checkpoint.")
            update_task_run(task_run, "replanning", "Agent is re-reading the sandbox and generating a repair proposal.")
            self._persist_task_run(task_run)

        report = run_workflow(
            session.repo_path,
            repair_task,
            validation_commands=[],
            use_llm=bool(payload.get("use_llm")),
            llm_client=llm_client,
            llm_model=str(payload.get("model") or "") or None,
            allow_llm_fallback=not bool(payload.get("no_llm_fallback")),
            llm_json_mode=_payload_json_mode(payload),
            llm_timeout_seconds=_payload_llm_timeout_seconds(payload),
            iterative_agent=_payload_iterative_agent(payload),
            agent_max_steps=min(_payload_agent_max_steps(payload), remaining_budget.max_agent_steps),
            use_memory=_payload_use_memory(payload),
            execution_budget=remaining_budget,
        )
        report_usage = ExecutionUsage.from_dict(report.execution_budget.get("usage"))
        cumulative_usage = session.execution_usage.add(
            agent_steps=report_usage.agent_steps,
            tool_calls=report_usage.tool_calls,
            validation_commands=report_usage.validation_commands,
            elapsed_ms=report_usage.elapsed_ms,
        )
        cumulative_budget_state = execution_budget_state(session.execution_budget, cumulative_usage)
        if task_run and checkpoint_task_run(task_run, "repair_pending"):
            self._persist_task_run(task_run)
            raise TaskRunError("Repair generation stopped after the replanning checkpoint.")
        if cumulative_budget_state["exhausted"]:
            message = "Repair analysis exceeded the execution budget: " + "; ".join(
                cumulative_budget_state["exhausted_reasons"]
            )
            self._stop_repair_loop(session, task_run, STOP_EXECUTION_BUDGET, message)
            self._persist_stopped_repair_report(
                report,
                session,
                task_run,
                repair_task,
                next_repair_attempt,
                STOP_EXECUTION_BUDGET,
                message,
            )
            raise RepairLoopStopped(STOP_EXECUTION_BUDGET, message)

        proposal = report.patch_proposal
        if not proposal or not proposal.file_edits or not proposal.apply_ready:
            message = "Repair analysis did not produce an apply-ready proposal."
            session.repair_history = mark_repair_attempt_stopped(
                session.repair_history,
                attempt=next_repair_attempt,
                summary=message,
            )
            self._stop_repair_loop(session, task_run, STOP_NO_PROPOSAL, message)
            self._persist_stopped_repair_report(
                report,
                session,
                task_run,
                repair_task,
                next_repair_attempt,
                STOP_NO_PROPOSAL,
                message,
            )
            raise RepairLoopStopped(STOP_NO_PROPOSAL, message)

        session.repair_history, proposal_decision = record_repair_proposal(
            session.repair_history,
            attempt=next_repair_attempt,
            trigger_failure_fingerprint=trigger_fingerprint,
            edits=proposal.file_edits,
            summary=proposal.objective,
        )
        if not proposal_decision.accepted:
            self._stop_repair_loop(
                session,
                task_run,
                proposal_decision.stop_reason or STOP_NO_PROPOSAL,
                proposal_decision.message,
            )
            self._persist_stopped_repair_report(
                report,
                session,
                task_run,
                repair_task,
                next_repair_attempt,
                proposal_decision.stop_reason or STOP_NO_PROPOSAL,
                proposal_decision.message,
            )
            raise RepairLoopStopped(
                proposal_decision.stop_reason or STOP_NO_PROPOSAL,
                proposal_decision.message,
            )
        if not proposal_changes_repository(report.repo_path, proposal.file_edits):
            message = "The repair proposal would not change the current repository state."
            session.repair_history = mark_repair_attempt_stopped(
                session.repair_history,
                attempt=next_repair_attempt,
                summary=message,
            )
            self._stop_repair_loop(session, task_run, STOP_NO_REPOSITORY_CHANGE, message)
            self._persist_stopped_repair_report(
                report,
                session,
                task_run,
                repair_task,
                next_repair_attempt,
                STOP_NO_REPOSITORY_CHANGE,
                message,
            )
            raise RepairLoopStopped(STOP_NO_REPOSITORY_CHANGE, message)

        validation_commands = session.validation_commands or (
            proposal.validation_plan.commands if proposal.validation_plan else []
        )
        repair_session = create_proposal_session(
            repo_path=report.repo_path,
            task=repair_task,
            file_edits=proposal.file_edits,
            validation_commands=validation_commands,
            timeline=build_report_timeline(report),
            allowed_paths=[file.path for file in proposal.files],
            parent_proposal_id=session.proposal_id,
            repair_attempt=next_repair_attempt,
            max_repair_attempts=session.max_repair_attempts,
            acceptance_criteria=session.acceptance_criteria,
            execution_budget=session.execution_budget,
            execution_usage=cumulative_usage,
            root_task=session.root_task or session.task,
            repair_history=session.repair_history,
            auto_repair_enabled=session.auto_repair_enabled,
        )
        append_timeline(
            repair_session,
            "approval",
            "pending",
            f"Waiting for approval on repair proposal {repair_session.proposal_id}.",
        )
        self._persist_session(repair_session)
        append_timeline(
            session,
            "repair",
            "done",
            (
                f"Generated repair attempt {next_repair_attempt}/"
                f"{session.max_repair_attempts}: {repair_session.proposal_id}."
            ),
        )
        self._persist_session(session)

        data = report.to_dict()
        data["proposal_id"] = repair_session.proposal_id
        data["parent_proposal_id"] = session.proposal_id
        data["repair_task"] = repair_task
        data["timeline"] = [asdict(event) for event in repair_session.timeline]
        _add_session_public_fields(data, repair_session)
        try:
            data["run_id"] = self._memory(report.repo_path).create_run(
                repo_path=report.repo_path,
                task=repair_task,
                mode="repair",
                report=report,
                proposal_id=repair_session.proposal_id,
                timeline=data["timeline"],
            )
        except Exception as exc:
            data["memory_error"] = str(exc)

        if task_run:
            self._memory(task_run.source_repo).save_proposal_session(
                proposal_session_to_record(repair_session)
            )
            task_result = dict(task_run.result or {})
            task_result["repair_report"] = data
            task_run.result = task_result
            task_run.proposal_id = repair_session.proposal_id
            task_run.error = None
            task_run.execution_usage = cumulative_usage
            task_run.repair_history = list(repair_session.repair_history)
            task_run.repair_stop_reason = None
            task_run.repair_stop_message = ""
            if task_run.cancel_requested:
                checkpoint_task_run(task_run, "awaiting_approval")
            elif task_run.pause_requested:
                checkpoint_task_run(task_run, "awaiting_approval")
            else:
                update_task_run(
                    task_run,
                    "awaiting_approval",
                    f"Repair proposal {next_repair_attempt}/{session.max_repair_attempts} is ready for approval.",
                )
                record_task_run_checkpoint(
                    task_run,
                    "repair_ready",
                    f"Repair proposal {next_repair_attempt} is ready for human review.",
                    "review_repair_proposal",
                )
            self._persist_task_run(task_run)
            data["task_run"] = task_run.to_public_dict()
        return data

    def _stop_repair_loop(
        self,
        session: ProposalSession,
        task_run: TaskRun | None,
        reason: str,
        message: str,
    ) -> None:
        session.repair_stop_reason = reason
        session.repair_stop_message = message
        append_timeline(session, "repair", "stopped", message)
        self._persist_session(session)
        if task_run:
            task_run.repair_history = list(session.repair_history)
            task_run.repair_stop_reason = reason
            task_run.repair_stop_message = message
            task_result = dict(task_run.result or {})
            task_result["repair_stop"] = {"reason": reason, "message": message}
            update_task_run(
                task_run,
                "failed",
                message,
                result=task_result,
                error=f"Repair loop stopped: {reason}",
            )
            record_task_run_checkpoint(
                task_run,
                "repair_stopped",
                message,
                "inspect_failure",
            )
            self._persist_task_run(task_run)

    def _persist_stopped_repair_report(
        self,
        report: Any,
        session: ProposalSession,
        task_run: TaskRun | None,
        repair_task: str,
        attempt: int,
        reason: str,
        message: str,
    ) -> None:
        timeline = build_report_timeline(report)
        timeline.append(TimelineEvent("repair", "stopped", message))
        data = report.to_dict()
        data.update(
            {
                "proposal_id": None,
                "parent_proposal_id": session.proposal_id,
                "repair_task": repair_task,
                "repair_attempt": attempt,
                "max_repair_attempts": session.max_repair_attempts,
                "repair_history": [item.to_dict() for item in session.repair_history],
                "repair_stop_reason": reason,
                "repair_stop_message": message,
                "timeline": [asdict(event) for event in timeline],
            }
        )
        try:
            data["run_id"] = self._memory(report.repo_path).create_run(
                repo_path=report.repo_path,
                task=repair_task,
                mode="repair",
                report=report,
                proposal_id=None,
                timeline=data["timeline"],
            )
        except Exception as exc:
            data["memory_error"] = str(exc)
        if task_run:
            task_result = dict(task_run.result or {})
            task_result["repair_report"] = data
            task_run.result = task_result
            self._persist_task_run(task_run)

    def _launch_auto_repair_worker(
        self,
        task_run: TaskRun,
        session: ProposalSession,
        payload: dict[str, Any],
    ) -> None:
        worker = threading.Thread(
            target=self._execute_auto_repair,
            args=(task_run, session, dict(payload)),
            name=f"repopilot-repair-{task_run.run_id[:8]}-{session.repair_attempt + 1}",
            daemon=True,
        )
        worker.start()

    def _execute_auto_repair(
        self,
        task_run: TaskRun,
        session: ProposalSession,
        payload: dict[str, Any],
    ) -> None:
        try:
            llm_client = _payload_llm_client(payload)
            self._generate_repair_proposal(session, payload, task_run, llm_client)
        except RepairLoopStopped:
            return
        except Exception as exc:
            if task_run.status not in {"cancelled", "failed"}:
                update_task_run(
                    task_run,
                    "repair_pending",
                    "Automatic repair generation failed; manual retry remains available.",
                    error=str(exc),
                )
                record_task_run_checkpoint(
                    task_run,
                    "repair_generation_failed",
                    "Automatic repair generation failed before a new proposal was available.",
                    "retry_repair",
                )
                self._persist_task_run(task_run)

    def _handle_llm_test(self) -> None:
        payload = self._read_json()
        try:
            client = OpenAICompatibleClient(
                api_key=str(payload.get("api_key") or "") or None,
                base_url=str(payload.get("base_url") or "") or None,
                model=str(payload.get("model") or "") or None,
                json_mode=_payload_json_mode(payload),
                timeout_seconds=_payload_llm_timeout_seconds(payload),
            )
            response = client.complete(
                [
                    LLMMessage(
                        role="system",
                        content='Return only JSON with this shape: {"ok": true, "message": "ready"}.',
                    ),
                    LLMMessage(role="user", content="Test the RepoPilot LLM connection."),
                ]
            )
        except LLMError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json(
            {
                "ok": True,
                "model": getattr(client, "model", ""),
                "base_url": getattr(client, "base_url", ""),
                "response_preview": _text_preview(response),
            }
        )

    def _handle_git_summary(self) -> None:
        payload = self._read_json()
        repo_source = self._resolve_payload_repository_or_error(payload)
        if repo_source is None:
            return
        validation_notes = payload.get("validation_notes") or []
        if not isinstance(validation_notes, list) or not all(isinstance(item, str) for item in validation_notes):
            self._send_json({"error": "validation_notes must be a list of strings."}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            summary = build_git_workflow_summary(repo_source.local_path, validation_notes=validation_notes)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        data = summary.to_dict()
        data["repository_source"] = repo_source.to_dict()
        self._send_json(data)

    def _handle_pr_readiness(self) -> None:
        payload = self._read_json()
        repo_source = self._resolve_payload_repository_or_error(payload, clone_if_missing=False)
        if repo_source is None:
            return
        base_branch = str(payload.get("base_branch") or "").strip() or None
        pull_request_title = str(payload.get("title") or "").strip() or None
        try:
            readiness = build_pull_request_readiness(
                repo_source.local_path,
                base_branch=base_branch,
                pull_request_title=pull_request_title,
            )
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json(
            {
                "pr_readiness": asdict(readiness),
                "repository_source": repo_source.to_dict(),
            }
        )

    def _handle_pr_create(self) -> None:
        payload = self._read_json()
        if not bool(payload.get("confirm_create")):
            self._send_json(
                {"error": "confirm_create must be true before creating a pull request."},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        title = str(payload.get("title") or "").strip()
        body = str(payload.get("body") or "").strip()
        if not title or not body:
            self._send_json({"error": "title and body are required."}, status=HTTPStatus.BAD_REQUEST)
            return
        repo_source = self._resolve_payload_repository_or_error(payload, clone_if_missing=False)
        if repo_source is None:
            return
        base_branch = str(payload.get("base_branch") or "").strip() or None
        try:
            readiness = build_pull_request_readiness(
                repo_source.local_path,
                base_branch=base_branch,
                pull_request_title=title,
            )
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if not readiness.ready:
            self._send_json(
                {
                    "error": "Pull request is not ready to create.",
                    "pr_readiness": asdict(readiness),
                    "repository_source": repo_source.to_dict(),
                },
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            created = create_github_pull_request(
                repo_source.local_path,
                title=title,
                body=body,
                base_branch=readiness.base_branch,
                head_branch=readiness.head_branch,
            )
        except Exception as exc:
            self._send_json({"error": str(exc), "pr_readiness": asdict(readiness)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json(
            {
                "created": True,
                "pull_request": created,
                "pr_readiness": asdict(readiness),
                "repository_source": repo_source.to_dict(),
            }
        )

    def _handle_repository_sync(self) -> None:
        payload = self._read_json()
        try:
            source = sync_repository_reference(
                repo=payload.get("repo") or ".",
                repo_source=str(payload.get("repo_source") or "auto"),
                github_url=str(payload.get("github_url") or ""),
                branch=str(payload.get("branch") or ""),
            )
        except (ValueError, FileNotFoundError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json({"repository_source": source.to_dict()})

    def _handle_sandbox_create(self) -> None:
        payload = self._read_json()
        repo_source = self._resolve_payload_repository_or_error(payload)
        if repo_source is None:
            return
        try:
            sandbox = create_worktree_sandbox(
                repo_source.local_path,
                base_ref=str(payload.get("ref") or "HEAD"),
                name=str(payload.get("name") or "").strip() or None,
            )
            sandboxes = list_worktree_sandboxes(sandbox.source_repo)
        except DirtyWorktreeError as exc:
            self._send_json(
                {"error": str(exc), "dirty": True, "repository_source": repo_source.to_dict()},
                status=HTTPStatus.CONFLICT,
            )
            return
        except WorktreeSandboxError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json(
            {
                "sandbox": sandbox.to_dict(),
                "sandboxes": [item.to_dict() for item in sandboxes],
                "repository_source": repo_source.to_dict(),
            }
        )

    def _handle_sandbox_list(self, query: str) -> None:
        params = parse_qs(query)
        try:
            repo_source = self._resolve_query_repository(params, clone_if_missing=False)
            sandboxes = list_worktree_sandboxes(repo_source.local_path)
        except (ValueError, FileNotFoundError, WorktreeSandboxError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json(
            {
                "sandboxes": [item.to_dict() for item in sandboxes],
                "repository_source": repo_source.to_dict(),
            }
        )

    def _handle_sandbox_remove(self) -> None:
        payload = self._read_json()
        if not _payload_bool(payload.get("confirm_remove"), default=False):
            self._send_json(
                {"error": "Explicit sandbox removal confirmation is required."},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        target = str(payload.get("path") or "").strip()
        if not target:
            self._send_json({"error": "Sandbox path is required."}, status=HTTPStatus.BAD_REQUEST)
            return
        source_repo = str(payload.get("source_repo") or payload.get("repo") or ".").strip() or "."
        force = _payload_bool(payload.get("force"), default=False)
        try:
            removal = remove_worktree_sandbox(source_repo, target, force=force)
            sandboxes = list_worktree_sandboxes(removal.source_repo)
        except DirtyWorktreeError as exc:
            self._send_json(
                {"error": str(exc), "dirty": True, "path": target},
                status=HTTPStatus.CONFLICT,
            )
            return
        except WorktreeSandboxError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json(
            {
                "removed": removal.to_dict(),
                "sandboxes": [item.to_dict() for item in sandboxes],
            }
        )

    def _handle_task_run_start(self) -> None:
        payload = self._read_json()
        task = str(payload.get("task") or "").strip()
        if not task:
            self._send_json({"error": "Task is required."}, status=HTTPStatus.BAD_REQUEST)
            return
        validation = payload.get("validation") or []
        if not isinstance(validation, list) or not all(isinstance(item, str) for item in validation):
            self._send_json({"error": "validation must be a list of strings."}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            max_repair_attempts = _payload_max_repair_attempts(payload)
            execution_budget = _payload_execution_budget(payload)
            _validate_validation_budget(validation, execution_budget)
            llm_client = _payload_llm_client(payload)
            auto_repair_enabled = _payload_auto_repair(payload)
            execution_profile = _payload_execution_profile(
                payload,
                execution_budget,
                llm_client,
                max_repair_attempts,
                auto_repair_enabled,
            )
        except (ValueError, LLMError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        repo_source = self._resolve_payload_repository_or_error(payload)
        if repo_source is None:
            return
        task_run = create_task_run(
            repo_source.local_path,
            task,
            validation,
            execution_budget=execution_budget,
            execution_profile=execution_profile,
            auto_repair_enabled=auto_repair_enabled,
        )
        try:
            self._persist_task_run(task_run)
            self._launch_task_run_worker(task_run, payload, llm_client, reuse_sandbox=False)
        except Exception as exc:
            update_task_run(task_run, "failed", "Task run could not be started.", error=str(exc))
            record_task_run_checkpoint(
                task_run,
                "task_failed",
                "Task worker could not be started after the run was accepted.",
                "inspect_failure",
            )
            self._persist_task_run(task_run)
            self._send_json({"error": str(exc), "task_run": task_run.to_public_dict()}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json({"task_run": task_run.to_public_dict()}, status=HTTPStatus.ACCEPTED)

    def _handle_task_run_list(self, query: str) -> None:
        params = parse_qs(query)
        try:
            limit = int(_first(params, "limit", "20"))
        except ValueError:
            limit = 20
        try:
            source_repo = self._task_run_source_from_query(params)
            recover_interrupted_task_runs(source_repo)
            records = self._memory(source_repo).list_task_runs(limit=limit)
            task_runs = []
            for record in records:
                run_id = str(record.get("run_id") or "")
                task_run = get_task_run(run_id)
                if task_run is None:
                    task_run = task_run_from_record(record, mark_interrupted=True)
                    self._persist_task_run(task_run)
                task_runs.append(task_run.to_public_dict())
        except (ValueError, FileNotFoundError, TaskRunError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json({"task_runs": task_runs, "source_repo": source_repo})

    def _handle_task_run_status(self, query: str) -> None:
        params = parse_qs(query)
        run_id = _first(params, "run_id", "").strip()
        if not run_id:
            self._send_json({"error": "run_id is required."}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            task_run = self._get_task_run_or_restore(run_id, self._task_run_source_from_query(params))
        except (ValueError, FileNotFoundError, TaskRunError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if task_run is None:
            self._send_json({"error": "Task run not found."}, status=HTTPStatus.NOT_FOUND)
            return
        self._send_json({"task_run": task_run.to_public_dict()})

    def _handle_runtime_approval_grant(self) -> None:
        payload = self._read_json()
        task_run = self._task_run_from_payload_or_error(payload)
        if task_run is None:
            return
        request = _pending_runtime_approval(task_run)
        if not request:
            self._send_json(
                {"error": "This task run has no pending runtime approval."},
                status=HTTPStatus.CONFLICT,
            )
            return
        if not task_run.sandbox_path:
            self._send_json(
                {"error": "The task run has no managed worktree sandbox."},
                status=HTTPStatus.CONFLICT,
            )
            return
        action_kind = str(request.get("action_kind") or "")
        if action_kind == "validate":
            self._handle_runtime_validation_grant(task_run, payload, request)
            return
        if action_kind not in {"apply_patch", "edit_file"}:
            self._send_json(
                {"error": f"Unsupported pending runtime action: {action_kind or '(empty)'}."},
                status=HTTPStatus.CONFLICT,
            )
            return
        current_budget = execution_budget_state(
            task_run.execution_budget,
            task_run.execution_usage,
        )
        remaining = current_budget["remaining"]
        if (
            current_budget["exhausted"]
            or remaining["tool_calls"] < 2
            or remaining["elapsed_ms"] <= 0
        ):
            update_task_run(
                task_run,
                "awaiting_approval",
                "Execution budget is insufficient for the approved write and resulting diff inspection.",
            )
            self._persist_task_run(task_run)
            self._send_json(
                {
                    "error": "Execution budget cannot cover the write and resulting diff inspection.",
                    "execution_budget": current_budget,
                    "task_run": task_run.to_public_dict(),
                },
                status=HTTPStatus.CONFLICT,
            )
            return
        task_result = dict(task_run.result or {})
        repair_attempt = _pending_agent_repair_attempt(task_result)
        started_at = time.monotonic()
        try:
            file_scope = _payload_string_list(payload, "file_scope")
            command_allowlist = _payload_string_list(payload, "command_allowlist")
            result = execute_pending_agent_write(
                task_run.source_repo,
                task_run.sandbox_path,
                task_run.task,
                task_run.run_id,
                SQLiteRuntimeStore(self._memory(task_run.source_repo)),
                checkpoint=str(payload.get("checkpoint") or ""),
                payload_hash=str(payload.get("payload_hash") or ""),
                file_scope=file_scope,
                command_allowlist=command_allowlist,
                validation_commands=task_run.validation_commands,
            )
        except (AgentWriteError, ValueError, WorktreeSandboxError) as exc:
            self._send_json(
                {"error": str(exc), "task_run": task_run.to_public_dict()},
                status=HTTPStatus.CONFLICT,
            )
            return

        store = SQLiteRuntimeStore(self._memory(task_run.source_repo))
        task_result["agent_events"] = _public_runtime_events(
            store.list_events(task_run.run_id)
        )
        task_result["agent_state"] = result.working_state
        serialized_write = result.to_dict()
        task_result["agent_write_result"] = serialized_write
        task_result["agent_resulting_diff"] = serialized_write["resulting_diff"]
        if result.status != "approval_required":
            task_result["agent_write_history"] = _append_agent_write_history(
                task_result.get("agent_write_history"),
                serialized_write,
            )
        task_run.execution_usage = task_run.execution_usage.add(
            tool_calls=2 if result.diff_observation else 1,
            elapsed_ms=max(int((time.monotonic() - started_at) * 1000), 0),
        )
        task_result["execution_budget"] = execution_budget_state(
            task_run.execution_budget,
            task_run.execution_usage,
        )
        if result.status != "approval_required":
            write_transition = observe_agent_write(
                task_run.repair_history,
                attempt=repair_attempt,
                max_attempts=_task_run_max_repair_attempts(task_run),
                changed_paths=list(
                    result.write_observation.data.get("changed_files") or []
                ),
            )
            if repair_attempt > 0 or write_transition.stop_reason:
                _apply_agent_repair_transition(
                    task_run,
                    task_result,
                    write_transition,
                    store,
                )
            if write_transition.stop_reason:
                task_result["agent_pending_approval"] = {}
                task_result["agent_stop_reason"] = write_transition.stop_reason
                task_result.pop("agent_repair_pending_attempt", None)
                update_task_run(
                    task_run,
                    "failed",
                    write_transition.message,
                    result=task_result,
                    error=None,
                )
                record_task_run_checkpoint(
                    task_run,
                    "runtime_repair_stopped",
                    write_transition.message,
                    "inspect_failure",
                )
                self._persist_task_run(task_run)
                self._send_json(
                    {
                        "write_result": result.to_dict(),
                        "task_run": task_run.to_public_dict(),
                    }
                )
                return
        if result.status == "approval_required":
            fresh_request = result.write_observation.data.get("approval_request") or {}
            task_result["agent_pending_approval"] = fresh_request
            task_result["agent_stop_reason"] = "approval_required"
            update_task_run(
                task_run,
                "awaiting_approval",
                "The managed-worktree baseline changed; review the fresh exact approval request.",
                result=task_result,
            )
            record_task_run_checkpoint(
                task_run,
                "runtime_approval_refreshed",
                "The previous grant became stale before execution and a fresh diff is waiting.",
                "review_runtime_action",
            )
        else:
            task_run.acceptance_criteria = criteria_from_records(
                result.working_state.get("acceptance_criteria")
            )
            validation_commands = _dedupe_commands(task_run.validation_commands)
            task_result.pop("agent_repair_pending_attempt", None)
            task_result["agent_validation_cycle"] = {
                "cycle_id": result.action_id,
                "commands": validation_commands,
                "next_index": 0,
                "results": [],
                "repair_attempt": repair_attempt,
            }
            if validation_commands:
                try:
                    validation_request = request_agent_validation(
                        task_run.source_repo,
                        task_run.sandbox_path,
                        task_run.task,
                        task_run.run_id,
                        store,
                        cycle_id=result.action_id,
                        command_index=0,
                        command_count=len(validation_commands),
                        command=validation_commands[0],
                    )
                except (AgentValidationError, ValueError, WorktreeSandboxError) as exc:
                    task_result["agent_pending_approval"] = {}
                    task_result["agent_stop_reason"] = "validation_setup_failed"
                    task_result["agent_validation_error"] = str(exc)
                    update_task_run(
                        task_run,
                        "review_pending",
                        "The approved write completed, but its validation approval "
                        "could not be prepared.",
                        result=task_result,
                        error=None,
                    )
                    record_task_run_checkpoint(
                        task_run,
                        "runtime_validation_setup_failed",
                        str(exc),
                        "inspect_failure",
                    )
                else:
                    task_result["agent_pending_approval"] = (
                        validation_request.pending_approval
                    )
                    task_result["agent_state"] = validation_request.working_state
                    task_result["agent_stop_reason"] = "approval_required"
                    task_result["agent_events"] = _public_runtime_events(
                        store.list_events(task_run.run_id)
                    )
                    update_task_run(
                        task_run,
                        "awaiting_approval",
                        "The approved write is ready for exact validation approval (1 of "
                        f"{len(validation_commands)}).",
                        result=task_result,
                        error=None,
                    )
                    record_task_run_checkpoint(
                        task_run,
                        "runtime_validation_ready",
                        "The first configured validation command is waiting for exact approval.",
                        "review_runtime_action",
                    )
            else:
                task_result["agent_pending_approval"] = {}
                task_result["agent_stop_reason"] = "validation_unavailable"
                update_task_run(
                    task_run,
                    "review_pending",
                    "Approved Agent write completed; no automated validation command is configured.",
                    result=task_result,
                    error=None,
                )
                record_task_run_checkpoint(
                    task_run,
                    "runtime_write_complete",
                    "The exact write completed, but automated validation is unavailable.",
                    "review_diff",
                )
        self._persist_task_run(task_run)
        self._send_json(
            {
                "write_result": result.to_dict(),
                "task_run": task_run.to_public_dict(),
            }
        )

    def _handle_runtime_validation_grant(
        self,
        task_run: TaskRun,
        payload: dict[str, Any],
        request: dict[str, Any],
    ) -> None:
        cycle = _task_validation_cycle(task_run)
        if not cycle:
            self._send_json(
                {"error": "The task run has no active validation cycle."},
                status=HTTPStatus.CONFLICT,
            )
            return
        commands = cycle["commands"]
        command_index = cycle["next_index"]
        if command_index >= len(commands):
            self._send_json(
                {"error": "The validation cycle has no remaining command."},
                status=HTTPStatus.CONFLICT,
            )
            return
        command = commands[command_index]
        pending_command = str(
            (request.get("action") or {}).get("arguments", {}).get("command") or ""
        ).strip()
        if pending_command != command:
            self._send_json(
                {"error": "The pending validation command does not match the task cycle."},
                status=HTTPStatus.CONFLICT,
            )
            return
        current_budget = execution_budget_state(
            task_run.execution_budget,
            task_run.execution_usage,
        )
        remaining = current_budget["remaining"]
        if (
            current_budget["exhausted"]
            or remaining["tool_calls"] < 1
            or remaining["validation_commands"] < 1
            or remaining["elapsed_ms"] <= 0
        ):
            update_task_run(
                task_run,
                "awaiting_approval",
                "Execution budget is insufficient for the approved validation command.",
            )
            self._persist_task_run(task_run)
            self._send_json(
                {
                    "error": "Execution budget cannot cover the pending validation command.",
                    "execution_budget": current_budget,
                    "task_run": task_run.to_public_dict(),
                },
                status=HTTPStatus.CONFLICT,
            )
            return

        started_at = time.monotonic()
        store = SQLiteRuntimeStore(self._memory(task_run.source_repo))
        try:
            result = execute_pending_agent_validation(
                task_run.source_repo,
                task_run.sandbox_path or "",
                task_run.task,
                task_run.run_id,
                store,
                cycle_id=cycle["cycle_id"],
                command_index=command_index,
                command_count=len(commands),
                expected_command=command,
                checkpoint=str(payload.get("checkpoint") or ""),
                payload_hash=str(payload.get("payload_hash") or ""),
                file_scope=_payload_string_list(payload, "file_scope"),
                command_allowlist=_payload_string_list(
                    payload,
                    "command_allowlist",
                ),
            )
        except (AgentValidationError, ValueError, WorktreeSandboxError) as exc:
            self._send_json(
                {"error": str(exc), "task_run": task_run.to_public_dict()},
                status=HTTPStatus.CONFLICT,
            )
            return

        task_run.execution_usage = task_run.execution_usage.add(
            tool_calls=1,
            validation_commands=1,
            elapsed_ms=max(int((time.monotonic() - started_at) * 1000), 0),
        )
        task_result = dict(task_run.result or {})
        result_records = [
            item
            for item in cycle.get("results", [])
            if isinstance(item, dict)
        ]
        result_records.append(result.to_dict())
        cycle["results"] = result_records
        cycle["next_index"] = command_index + 1
        task_result["agent_validation_cycle"] = cycle
        task_result["agent_validation_results"] = result_records
        task_result["agent_state"] = result.working_state
        task_result["agent_pending_approval"] = {}
        task_result["agent_events"] = _public_runtime_events(
            store.list_events(task_run.run_id)
        )
        validation = _validation_results_from_cycle(cycle)
        task_result["validation"] = [asdict(item) for item in validation]
        feedback = build_validation_feedback(
            validation,
            task=task_run.task,
            repo_path=task_run.sandbox_path,
        )
        task_result["validation_feedback"] = asdict(feedback) if feedback else None
        task_result["execution_budget"] = execution_budget_state(
            task_run.execution_budget,
            task_run.execution_usage,
        )
        task_run.acceptance_criteria = criteria_from_records(
            result.working_state.get("acceptance_criteria")
        )

        if result.status == "passed" and cycle["next_index"] < len(commands):
            next_index = cycle["next_index"]
            try:
                next_request = request_agent_validation(
                    task_run.source_repo,
                    task_run.sandbox_path or "",
                    task_run.task,
                    task_run.run_id,
                    store,
                    cycle_id=cycle["cycle_id"],
                    command_index=next_index,
                    command_count=len(commands),
                    command=commands[next_index],
                )
            except (AgentValidationError, ValueError, WorktreeSandboxError) as exc:
                task_result["agent_stop_reason"] = "validation_setup_failed"
                task_result["agent_validation_error"] = str(exc)
                update_task_run(
                    task_run,
                    "review_pending",
                    "Validation evidence was saved, but the next exact command "
                    "could not be prepared.",
                    result=task_result,
                    error=None,
                )
                record_task_run_checkpoint(
                    task_run,
                    "runtime_validation_setup_failed",
                    str(exc),
                    "inspect_failure",
                )
            else:
                task_result["agent_pending_approval"] = next_request.pending_approval
                task_result["agent_state"] = next_request.working_state
                task_result["agent_stop_reason"] = "approval_required"
                task_result["agent_events"] = _public_runtime_events(
                    store.list_events(task_run.run_id)
                )
                update_task_run(
                    task_run,
                    "awaiting_approval",
                    f"Validation {command_index + 1} passed; command {next_index + 1} "
                    f"of {len(commands)} is waiting for exact approval.",
                    result=task_result,
                    error=None,
                )
                record_task_run_checkpoint(
                    task_run,
                    "runtime_validation_ready",
                    f"Configured validation command {next_index + 1} is waiting for approval.",
                    "review_runtime_action",
                )
        else:
            self._continue_task_after_validation(
                task_run,
                task_result,
                cycle,
                payload,
                store,
                validation_passed=result.status == "passed",
            )
        self._persist_task_run(task_run)
        self._send_json(
            {
                "validation_result": result.to_dict(),
                "task_run": task_run.to_public_dict(),
            }
        )

    def _continue_task_after_validation(
        self,
        task_run: TaskRun,
        task_result: dict[str, Any],
        cycle: dict[str, Any],
        payload: dict[str, Any],
        store: SQLiteRuntimeStore,
        *,
        validation_passed: bool,
    ) -> None:
        validation = _validation_results_from_cycle(cycle)
        repair_transition = observe_agent_validation(
            task_run.repair_history,
            attempt=cycle["repair_attempt"],
            max_attempts=_task_run_max_repair_attempts(task_run),
            validation=validation,
            summary=(
                "All approved validation commands passed."
                if validation_passed
                else "Approved validation failed; bounded evidence was returned to the Agent."
            ),
        )
        _apply_agent_repair_transition(
            task_run,
            task_result,
            repair_transition,
            store,
        )
        if repair_transition.stop_reason:
            _finish_stopped_agent_repair(
                task_run,
                task_result,
                repair_transition,
            )
            return

        continuation_config_error = ""
        try:
            remaining_budget = _remaining_agent_continuation_budget(task_run, payload)
        except ValueError as exc:
            remaining_budget = None
            continuation_config_error = str(exc)
            task_result["agent_continuation_error"] = continuation_config_error
        llm_client = None
        if remaining_budget is not None:
            try:
                llm_client = _runtime_continuation_llm_client(payload, task_run)
            except (LLMError, ValueError) as exc:
                continuation_config_error = str(exc)
                task_result["agent_continuation_error"] = continuation_config_error
        if llm_client is None or remaining_budget is None:
            if (
                repair_transition.repair_required
                and remaining_budget is None
                and not continuation_config_error
            ):
                stopped = stop_agent_repair(
                    repair_transition.history,
                    attempt=repair_transition.next_attempt or repair_transition.attempt,
                    max_attempts=repair_transition.max_attempts,
                    reason=STOP_EXECUTION_BUDGET,
                    message=(
                        "Validation failed, but the remaining execution budget cannot cover "
                        "another Agent repair decision."
                    ),
                    trigger_failure_fingerprint=(
                        repair_transition.trigger_failure_fingerprint
                    ),
                )
                _apply_agent_repair_transition(task_run, task_result, stopped, store)
                _finish_stopped_agent_repair(task_run, task_result, stopped)
                return
            if continuation_config_error:
                reason = (
                    "Agent continuation configuration is invalid: "
                    f"{continuation_config_error}"
                )
            elif llm_client is None:
                reason = "No request-scoped LLM client is available for the next decision."
            else:
                reason = "The remaining execution budget cannot cover another Agent decision."
            task_result["agent_stop_reason"] = (
                "validation_passed" if validation_passed else "validation_failed"
            )
            update_task_run(
                task_run,
                "review_pending",
                f"Validation {'passed' if validation_passed else 'failed'}; {reason}",
                result=task_result,
                error=None,
            )
            record_task_run_checkpoint(
                task_run,
                "runtime_validation_observed",
                reason,
                "review_validation",
            )
            return

        traces = []
        before_tool_calls = sum(
            1
            for event in store.list_events(task_run.run_id)
            if event.event_type == "action_started"
        )
        continued_at = time.monotonic()
        try:
            validation_results = [
                AgentValidationResult.from_dict(item)
                for item in cycle.get("results", [])
                if isinstance(item, dict)
            ]
            memory_context = None
            if _payload_use_memory(payload):
                memory_context = self._memory(task_run.source_repo).find_related_runs(
                    task_run.task
                )
            continued = continue_agent_after_validation(
                task_run.source_repo,
                task_run.sandbox_path or "",
                task_run.task,
                task_run.run_id,
                store,
                llm_client,
                validation_results,
                max_steps=remaining_budget.max_agent_steps,
                execution_budget=remaining_budget,
                memory_context=memory_context,
                traces=traces,
                repair_context=(
                    render_agent_repair_context(repair_transition)
                    if repair_transition.repair_required
                    else ""
                ),
                blocked_repair_proposal_fingerprints=(
                    blocked_agent_repair_fingerprints(repair_transition.history)
                    if repair_transition.repair_required
                    else None
                ),
            )
        except (LLMError, AgentValidationError, ValueError, WorktreeSandboxError) as exc:
            task_result["agent_continuation_error"] = str(exc)
            task_result["agent_stop_reason"] = "continuation_failed"
            update_task_run(
                task_run,
                "review_pending",
                "Validation evidence was saved, but the next Agent decision failed.",
                result=task_result,
                error=None,
            )
            record_task_run_checkpoint(
                task_run,
                "runtime_continuation_failed",
                str(exc),
                "inspect_failure",
            )
            return

        after_tool_calls = sum(
            1
            for event in store.list_events(task_run.run_id)
            if event.event_type == "action_started"
        )
        task_run.execution_usage = task_run.execution_usage.add(
            agent_steps=len(continued.steps),
            tool_calls=max(after_tool_calls - before_tool_calls, 0),
            elapsed_ms=max(int((time.monotonic() - continued_at) * 1000), 0),
        )
        task_result["agent_steps"] = [
            *list(task_result.get("agent_steps") or []),
            *[asdict(step) for step in continued.steps],
        ]
        task_result["llm_traces"] = [
            *list(task_result.get("llm_traces") or []),
            *[asdict(trace) for trace in traces],
        ]
        task_result["agent_events"] = _public_runtime_events(
            store.list_events(task_run.run_id)
        )
        task_result["agent_state"] = continued.working_state.to_dict()
        task_result["agent_stop_reason"] = continued.stop_reason
        task_result["agent_pending_question"] = continued.pending_question
        task_result["agent_pending_approval"] = continued.pending_approval
        task_result["agent_completion_ready"] = agent_completion_ready(
            continued.working_state
        )
        task_result["agent_completion_blockers"] = agent_completion_blockers(
            continued.working_state
        )
        task_result["agent_proposed_edits"] = continued.proposed_edits
        task_result["agent_proposed_diff"] = continued.proposed_diff
        task_result["execution_budget"] = execution_budget_state(
            task_run.execution_budget,
            task_run.execution_usage,
        )
        task_run.acceptance_criteria = criteria_from_records(
            continued.working_state.to_dict().get("acceptance_criteria")
        )

        if repair_transition.repair_required:
            pending_kind = str(continued.pending_approval.get("action_kind") or "")
            repeated_proposal = continued.stop_reason == STOP_REPEATED_PROPOSAL
            if pending_kind in {"apply_patch", "edit_file"} or repeated_proposal:
                next_attempt = repair_transition.next_attempt
                if next_attempt is None or not continued.repair_write_fingerprint:
                    proposal_transition = stop_agent_repair(
                        repair_transition.history,
                        attempt=next_attempt or repair_transition.attempt,
                        max_attempts=repair_transition.max_attempts,
                        reason=STOP_NO_PROPOSAL,
                        message=(
                            "The Agent reached a repair write decision without a valid material "
                            "proposal fingerprint."
                        ),
                        trigger_failure_fingerprint=(
                            repair_transition.trigger_failure_fingerprint
                        ),
                    )
                else:
                    proposal_transition = observe_agent_repair_proposal(
                        repair_transition.history,
                        attempt=next_attempt,
                        max_attempts=repair_transition.max_attempts,
                        trigger_failure_fingerprint=(
                            repair_transition.trigger_failure_fingerprint
                        ),
                        proposal_fingerprint=continued.repair_write_fingerprint,
                        proposal_paths=continued.repair_write_paths,
                        summary=continued.summary or "Agent repair proposal prepared.",
                    )
                _apply_agent_repair_transition(
                    task_run,
                    task_result,
                    proposal_transition,
                    store,
                )
                if proposal_transition.stop_reason:
                    task_result["agent_pending_approval"] = {}
                    task_result["agent_stop_reason"] = proposal_transition.stop_reason
                    _finish_stopped_agent_repair(
                        task_run,
                        task_result,
                        proposal_transition,
                    )
                    return
                task_result["agent_repair_pending_attempt"] = proposal_transition.attempt
                _sync_agent_repair_result(task_run, task_result)
            elif not continued.pending_question:
                budget_state = execution_budget_state(
                    task_run.execution_budget,
                    task_run.execution_usage,
                )
                exhausted = budget_state["exhausted"] or (
                    budget_state["remaining"]["agent_steps"] <= 0
                    or budget_state["remaining"]["tool_calls"] <= 0
                )
                stopped = stop_agent_repair(
                    repair_transition.history,
                    attempt=repair_transition.next_attempt or repair_transition.attempt,
                    max_attempts=repair_transition.max_attempts,
                    reason=STOP_EXECUTION_BUDGET if exhausted else STOP_NO_PROPOSAL,
                    message=(
                        "The execution budget ended before the Agent produced a new repair patch."
                        if exhausted
                        else "The Agent continuation produced no apply-ready repair action."
                    ),
                    trigger_failure_fingerprint=(
                        repair_transition.trigger_failure_fingerprint
                    ),
                )
                _apply_agent_repair_transition(task_run, task_result, stopped, store)
                _finish_stopped_agent_repair(task_run, task_result, stopped)
                return

        if continued.pending_approval:
            update_task_run(
                task_run,
                "awaiting_approval",
                "The Agent used validation evidence to prepare another exact managed action.",
                result=task_result,
                error=None,
            )
            record_task_run_checkpoint(
                task_run,
                "runtime_approval_ready",
                "A post-validation Agent action is waiting for exact approval.",
                "review_runtime_action",
            )
            return
        if continued.stop_reason == "finished" and agent_completion_ready(
            continued.working_state
        ):
            validation = _validation_results_from_cycle(cycle)
            approved_paths = _approved_agent_write_paths(task_result)
            try:
                changed_files = [
                    change.path
                    for change in inspect_repository(task_run.sandbox_path or "").changes
                ]
            except (FileNotFoundError, RuntimeError):
                changed_files = list(approved_paths)
            completion = evaluate_completion(
                task_run.acceptance_criteria,
                changed_files=changed_files,
                approved_paths=approved_paths,
                validation=validation,
                diff=str(task_result.get("agent_resulting_diff") or ""),
            )
            task_run.completion_evidence = completion
            task_result["completion_evidence"] = completion.to_dict()
            if completion.status == "passed":
                update_task_run(
                    task_run,
                    "completed",
                    "The Agent finished with passing validation and complete acceptance evidence.",
                    result=task_result,
                    error=None,
                )
                record_task_run_checkpoint(
                    task_run,
                    "runtime_task_complete",
                    completion.summary,
                    "review_report",
                )
            else:
                task_result["agent_stop_reason"] = "completion_evidence_incomplete"
                update_task_run(
                    task_run,
                    "review_pending",
                    "The Agent requested completion, but repository evidence did not pass.",
                    result=task_result,
                    error=None,
                )
                record_task_run_checkpoint(
                    task_run,
                    "runtime_completion_blocked",
                    completion.summary,
                    "review_validation",
                )
            return

        detail = (
            "The Agent requested user input; answer continuation is not available until the user-interaction milestone."
            if continued.pending_question
            else "Review the validation evidence and current Agent state before continuing."
        )
        update_task_run(
            task_run,
            "review_pending",
            detail,
            result=task_result,
            error=None,
        )
        record_task_run_checkpoint(
            task_run,
            "runtime_validation_observed",
            detail,
            "review_validation",
        )

    def _handle_runtime_approval_reject(self) -> None:
        payload = self._read_json()
        task_run = self._task_run_from_payload_or_error(payload)
        if task_run is None:
            return
        request = _pending_runtime_approval(task_run)
        if not request:
            self._send_json(
                {"error": "This task run has no pending runtime approval."},
                status=HTTPStatus.CONFLICT,
            )
            return
        if not task_run.sandbox_path:
            self._send_json(
                {"error": "The task run has no managed worktree sandbox."},
                status=HTTPStatus.CONFLICT,
            )
            return
        action_kind = str(request.get("action_kind") or "")
        if action_kind == "validate":
            self._handle_runtime_validation_reject(task_run, payload, request)
            return
        if action_kind not in {"apply_patch", "edit_file"}:
            self._send_json(
                {"error": f"Unsupported pending runtime action: {action_kind or '(empty)'}."},
                status=HTTPStatus.CONFLICT,
            )
            return
        reason = str(payload.get("reason") or "Rejected by the user.").strip()
        try:
            rejected = reject_pending_agent_write(
                task_run.source_repo,
                task_run.sandbox_path,
                task_run.task,
                task_run.run_id,
                SQLiteRuntimeStore(self._memory(task_run.source_repo)),
                checkpoint=str(payload.get("checkpoint") or ""),
                file_scope=list(request.get("file_scope") or []),
                reason=reason,
            )
        except (AgentWriteError, ValueError, WorktreeSandboxError) as exc:
            self._send_json(
                {"error": str(exc), "task_run": task_run.to_public_dict()},
                status=HTTPStatus.CONFLICT,
            )
            return
        task_result = dict(task_run.result or {})
        store = SQLiteRuntimeStore(self._memory(task_run.source_repo))
        task_result["agent_events"] = _public_runtime_events(
            store.list_events(task_run.run_id)
        )
        task_result["agent_state"] = rejected["working_state"]
        task_result["agent_pending_approval"] = {}
        task_result["agent_stop_reason"] = "approval_rejected"
        task_result["agent_write_result"] = rejected
        repair_attempt = _pending_agent_repair_attempt(task_result)
        task_result.pop("agent_repair_pending_attempt", None)
        if repair_attempt > 0:
            rejected_transition = stop_agent_repair(
                task_run.repair_history,
                attempt=repair_attempt,
                max_attempts=_task_run_max_repair_attempts(task_run),
                reason="approval_rejected",
                message="The user rejected the exact pending Agent repair write.",
                trigger_failure_fingerprint=latest_failure_fingerprint(
                    task_run.repair_history
                ),
            )
            _apply_agent_repair_transition(
                task_run,
                task_result,
                rejected_transition,
                store,
            )
        update_task_run(
            task_run,
            "cancelled",
            "The pending Agent write was rejected; the managed worktree was not modified.",
            result=task_result,
            error=None,
        )
        record_task_run_checkpoint(
            task_run,
            "runtime_approval_rejected",
            "The user rejected the exact pending Runtime write action.",
            "review_report",
        )
        self._persist_task_run(task_run)
        self._send_json(
            {"rejection": rejected, "task_run": task_run.to_public_dict()}
        )

    def _handle_runtime_validation_reject(
        self,
        task_run: TaskRun,
        payload: dict[str, Any],
        request: dict[str, Any],
    ) -> None:
        cycle = _task_validation_cycle(task_run)
        if not cycle or cycle["next_index"] >= len(cycle["commands"]):
            self._send_json(
                {"error": "The task run has no active validation command."},
                status=HTTPStatus.CONFLICT,
            )
            return
        expected_command = cycle["commands"][cycle["next_index"]]
        pending_command = str(
            (request.get("action") or {}).get("arguments", {}).get("command") or ""
        ).strip()
        if pending_command != expected_command:
            self._send_json(
                {"error": "The pending validation command does not match the task cycle."},
                status=HTTPStatus.CONFLICT,
            )
            return
        reason = str(payload.get("reason") or "Rejected by the user.").strip()
        store = SQLiteRuntimeStore(self._memory(task_run.source_repo))
        try:
            rejected = reject_pending_agent_validation(
                task_run.source_repo,
                task_run.sandbox_path or "",
                task_run.task,
                task_run.run_id,
                store,
                checkpoint=str(payload.get("checkpoint") or ""),
                expected_command=expected_command,
                reason=reason,
            )
        except (AgentValidationError, ValueError, WorktreeSandboxError) as exc:
            self._send_json(
                {"error": str(exc), "task_run": task_run.to_public_dict()},
                status=HTTPStatus.CONFLICT,
            )
            return
        task_result = dict(task_run.result or {})
        task_result["agent_events"] = _public_runtime_events(
            store.list_events(task_run.run_id)
        )
        task_result["agent_state"] = rejected["working_state"]
        task_result["agent_pending_approval"] = {}
        task_result["agent_stop_reason"] = "approval_rejected"
        task_result["agent_validation_rejection"] = rejected
        rejected_transition = stop_agent_repair(
            task_run.repair_history,
            attempt=cycle["repair_attempt"],
            max_attempts=_task_run_max_repair_attempts(task_run),
            reason="approval_rejected",
            message="The user rejected the exact pending Agent validation command.",
            trigger_failure_fingerprint=latest_failure_fingerprint(
                task_run.repair_history
            ),
        )
        _apply_agent_repair_transition(
            task_run,
            task_result,
            rejected_transition,
            store,
        )
        update_task_run(
            task_run,
            "cancelled",
            "The pending validation command was rejected; the managed worktree was preserved.",
            result=task_result,
            error=None,
        )
        record_task_run_checkpoint(
            task_run,
            "runtime_validation_rejected",
            "The user rejected the exact pending validation command.",
            "inspect_sandbox",
        )
        self._persist_task_run(task_run)
        self._send_json(
            {"rejection": rejected, "task_run": task_run.to_public_dict()}
        )

    def _handle_task_run_pause(self) -> None:
        payload = self._read_json()
        task_run = self._task_run_from_payload_or_error(payload)
        if task_run is None:
            return
        try:
            request_task_run_pause(task_run)
            self._persist_task_run(task_run)
        except TaskRunError as exc:
            self._send_json({"error": str(exc), "task_run": task_run.to_public_dict()}, status=HTTPStatus.CONFLICT)
            return
        self._send_json({"task_run": task_run.to_public_dict()})

    def _handle_task_run_recovery_readiness(self) -> None:
        payload = self._read_json()
        task_run = self._task_run_from_payload_or_error(payload)
        if task_run is None:
            return
        try:
            current_profile = _current_execution_profile_from_payload(payload)
            readiness = self._inspect_task_run_recovery(task_run, current_profile)
        except (ValueError, LLMError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json(
            {
                "task_run": task_run.to_public_dict(),
                "recovery_readiness": readiness.to_dict(),
            }
        )

    def _handle_task_run_resume(self) -> None:
        payload = self._read_json()
        task_run = self._task_run_from_payload_or_error(payload)
        if task_run is None:
            return
        readiness: TaskRunRecoveryReadiness | None = None
        try:
            requested_checkpoint = str(payload.get("resume_checkpoint") or "").strip()
            plan = validate_task_run_resume_request(
                task_run,
                requested_checkpoint,
                _payload_bool(payload.get("confirm_resume"), default=False),
            )
            llm_client = (
                _payload_llm_client(payload) if plan.target_status == "queued" else None
            )
            current_profile = _current_execution_profile_from_payload(
                payload,
                llm_client,
                resolve_llm_client=False,
            )
            readiness = self._inspect_task_run_recovery(task_run, current_profile)
            if not readiness.ready:
                raise TaskRunError(readiness.summary)
            prepare_task_run_resume(task_run, requested_checkpoint, confirmed=True)
            self._persist_task_run(task_run)
            if task_run.status not in {"awaiting_approval", "repair_pending"}:
                self._launch_task_run_worker(
                    task_run,
                    payload,
                    llm_client,
                    reuse_sandbox=plan.reuse_sandbox,
                )
        except (TaskRunError, LLMError, ValueError, FileNotFoundError, RuntimeError) as exc:
            self._persist_task_run(task_run)
            data = {"error": str(exc), "task_run": task_run.to_public_dict()}
            if readiness is not None:
                data["recovery_readiness"] = readiness.to_dict()
            self._send_json(data, status=HTTPStatus.CONFLICT)
            return
        self._send_json(
            {
                "task_run": task_run.to_public_dict(),
                "recovery_readiness": readiness.to_dict() if readiness else None,
            },
            status=HTTPStatus.ACCEPTED,
        )

    def _handle_task_run_cancel(self) -> None:
        payload = self._read_json()
        task_run = self._task_run_from_payload_or_error(payload)
        if task_run is None:
            return
        try:
            request_task_run_cancel(task_run)
            self._persist_task_run(task_run)
        except TaskRunError as exc:
            self._send_json({"error": str(exc), "task_run": task_run.to_public_dict()}, status=HTTPStatus.CONFLICT)
            return
        self._send_json({"task_run": task_run.to_public_dict()})

    def _handle_task_run_branch(self) -> None:
        payload = self._read_json()
        task_run = self._task_run_from_payload_or_error(payload)
        if task_run is None:
            return
        try:
            branch = create_task_run_branch(
                task_run,
                str(payload.get("branch_name") or ""),
                confirmed=bool(payload.get("confirm_create")),
            )
            self._persist_task_run(task_run)
        except TaskRunError as exc:
            self._send_json({"error": str(exc), "task_run": task_run.to_public_dict()}, status=HTTPStatus.CONFLICT)
            return
        self._send_json({"created": True, "branch": branch, "task_run": task_run.to_public_dict()})

    def _launch_task_run_worker(
        self,
        task_run: TaskRun,
        payload: dict[str, Any],
        llm_client: OpenAICompatibleClient | None,
        *,
        reuse_sandbox: bool,
    ) -> None:
        worker = threading.Thread(
            target=self._execute_task_run,
            args=(task_run, dict(payload), llm_client, reuse_sandbox),
            name=f"repopilot-task-{task_run.run_id[:8]}",
            daemon=True,
        )
        worker.start()

    def _execute_task_run(
        self,
        task_run: TaskRun,
        payload: dict[str, Any],
        llm_client: OpenAICompatibleClient | None,
        reuse_sandbox: bool,
    ) -> None:
        try:
            sandbox = None
            if reuse_sandbox:
                if not task_run.sandbox_path or not Path(task_run.sandbox_path).is_dir():
                    raise TaskRunError("The task sandbox no longer exists.")
                sandbox_path = task_run.sandbox_path
            else:
                update_task_run(task_run, "creating_sandbox", "Creating an isolated Git worktree sandbox.")
                self._persist_task_run(task_run)
                sandbox = create_worktree_sandbox(task_run.source_repo)
                sandbox_path = sandbox.path
                update_task_run(
                    task_run,
                    "creating_sandbox",
                    "Created the isolated task sandbox.",
                    sandbox_path=sandbox.path,
                    sandbox_head=sandbox.head,
                )
                self._persist_task_run(task_run)
            record_task_run_checkpoint(
                task_run,
                "sandbox_ready",
                "Existing task sandbox passed resume preflight."
                if reuse_sandbox
                else "Managed Git worktree sandbox created successfully.",
                "explore_repository",
            )
            self._persist_task_run(task_run)
            if checkpoint_task_run(task_run, "exploring"):
                self._persist_task_run(task_run)
                return

            update_task_run(task_run, "exploring", "Agent is exploring the sandbox and preparing a proposal.")
            self._persist_task_run(task_run)
            memory_context = None
            if _payload_use_memory(payload):
                memory_context = self._memory(task_run.source_repo).find_related_runs(task_run.task)
            runtime_store = SQLiteRuntimeStore(self._memory(task_run.source_repo))
            report = run_workflow(
                sandbox_path,
                task_run.task,
                validation_commands=[],
                use_llm=bool(payload.get("use_llm")),
                llm_client=llm_client,
                llm_model=str(payload.get("model") or "") or None,
                allow_llm_fallback=not bool(payload.get("no_llm_fallback")),
                llm_json_mode=_payload_json_mode(payload),
                llm_timeout_seconds=_payload_llm_timeout_seconds(payload),
                iterative_agent=_payload_iterative_agent(payload),
                agent_max_steps=_payload_agent_max_steps(payload),
                use_memory=_payload_use_memory(payload),
                memory_context=memory_context,
                agent_run_id=task_run.run_id,
                agent_event_store=runtime_store,
                execution_budget=task_run.execution_budget,
                allow_agent_writes=True,
            )
            task_run.acceptance_criteria = criteria_from_records(report.acceptance_criteria)
            task_run.execution_usage = ExecutionUsage.from_dict(
                report.execution_budget.get("usage")
            )
            timeline = build_report_timeline(report)
            proposal = report.patch_proposal
            proposal_id = None
            proposal_session = None
            budget_exhausted = bool(report.execution_budget.get("exhausted"))
            runtime_approval = (
                report.agent_pending_approval
                if report.agent_pending_approval.get("action_kind")
                in {"apply_patch", "edit_file"}
                else {}
            )
            if (
                not runtime_approval
                and proposal
                and proposal.file_edits
                and proposal.apply_ready
                and not budget_exhausted
            ):
                validation_commands = task_run.validation_commands or (
                    proposal.validation_plan.commands if proposal.validation_plan else []
                )
                _validate_validation_budget(validation_commands, task_run.execution_budget)
                proposal_session = create_proposal_session(
                    repo_path=report.repo_path,
                    task=task_run.task,
                    file_edits=proposal.file_edits,
                    validation_commands=validation_commands,
                    timeline=timeline,
                    allowed_paths=[file.path for file in proposal.files],
                    max_repair_attempts=_payload_max_repair_attempts(payload),
                    acceptance_criteria=build_acceptance_criteria(
                        task_run.task,
                        [edit.path for edit in proposal.file_edits],
                        validation_commands,
                    ),
                    execution_budget=task_run.execution_budget,
                    execution_usage=task_run.execution_usage,
                    root_task=task_run.task,
                    repair_history=task_run.repair_history,
                    auto_repair_enabled=task_run.auto_repair_enabled,
                )
                task_run.acceptance_criteria = list(proposal_session.acceptance_criteria)
                proposal_id = proposal_session.proposal_id
                append_timeline(
                    proposal_session,
                    "approval",
                    "pending",
                    f"Waiting for approval on proposal {proposal_id}.",
                )
                self._persist_session(proposal_session)
                self._memory(task_run.source_repo).save_proposal_session(
                    proposal_session_to_record(proposal_session)
                )
                timeline = proposal_session.timeline

            data = report.to_dict()
            data["repository_source"] = {
                "source": "local",
                "input": sandbox_path,
                "local_path": sandbox_path,
                "branch": None,
                "latest_commit": task_run.sandbox_head,
                "cached": False,
                "dirty": False,
                "synced": False,
                "message": "Running inside a managed RepoPilot worktree sandbox.",
            }
            data["sandbox"] = sandbox.to_dict() if sandbox else {
                "source_repo": task_run.source_repo,
                "path": sandbox_path,
                "head": task_run.sandbox_head,
            }
            data["proposal_id"] = proposal_id
            data["task_run_id"] = task_run.run_id
            data["timeline"] = [asdict(event) for event in timeline]
            if proposal_session:
                _add_session_public_fields(data, proposal_session)
            elif runtime_approval:
                _sync_agent_repair_result(task_run, data)
            history_run_id = self._memory(task_run.source_repo).create_run(
                repo_path=report.repo_path,
                task=task_run.task,
                mode="task_run",
                report=report,
                proposal_id=proposal_id,
                timeline=data["timeline"],
            )
            data["run_id"] = history_run_id
            task_run.result = data
            task_run.proposal_id = proposal_id
            task_run.history_run_id = history_run_id
            task_run.completion_evidence = (
                proposal_session.completion_evidence
                if proposal_session
                else completion_from_record(report.completion_evidence)
            )
            if task_run.cancel_requested:
                checkpoint_task_run(
                    task_run,
                    "awaiting_approval"
                    if proposal_id or runtime_approval
                    else "completed",
                )
            elif task_run.pause_requested and (proposal_id or runtime_approval):
                checkpoint_task_run(task_run, "awaiting_approval")
            elif budget_exhausted:
                update_task_run(
                    task_run,
                    "failed",
                    "Execution budget was exhausted before the approval checkpoint.",
                    error="; ".join(report.execution_budget.get("exhausted_reasons", [])),
                )
                record_task_run_checkpoint(
                    task_run,
                    "analysis_failed",
                    "Analysis stopped after exhausting the execution budget.",
                    "inspect_failure",
                )
            elif runtime_approval:
                update_task_run(
                    task_run,
                    "awaiting_approval",
                    "The Agent requested an exact managed-worktree write. Waiting for human approval.",
                )
                record_task_run_checkpoint(
                    task_run,
                    "runtime_approval_ready",
                    "An exact Runtime write action and diff are waiting for human approval.",
                    "review_runtime_action",
                )
            elif proposal_id:
                update_task_run(task_run, "awaiting_approval", "Proposal ready. Waiting for human approval.")
                record_task_run_checkpoint(
                    task_run,
                    "approval_ready",
                    "An apply-ready proposal is waiting for human approval.",
                    "review_proposal",
                )
            else:
                task_run.pause_requested = False
                update_task_run(task_run, "completed", "Task analysis completed without apply-ready file edits.")
                record_task_run_checkpoint(
                    task_run,
                    "analysis_complete",
                    "Repository analysis completed without apply-ready file edits.",
                    "review_report",
                )
            self._persist_task_run(task_run)
        except Exception as exc:
            update_task_run(task_run, "failed", "Task run failed. Its sandbox was preserved.", error=str(exc))
            record_task_run_checkpoint(
                task_run,
                "task_failed",
                "Task execution failed after preserving the current sandbox state.",
                "inspect_failure",
            )
            self._persist_task_run(task_run)

    def _task_run_source_from_query(self, params: dict[str, list[str]]) -> str:
        source_repo = _first(params, "source_repo", "").strip()
        if source_repo:
            path = Path(source_repo).expanduser().resolve()
            if not path.is_dir():
                raise FileNotFoundError(f"Task-run source repository does not exist: {path}")
            return str(path)
        return self._resolve_query_repository(params, clone_if_missing=False).local_path

    def _task_run_from_payload_or_error(self, payload: dict[str, Any]) -> TaskRun | None:
        run_id = str(payload.get("run_id") or "").strip()
        if not run_id:
            self._send_json({"error": "run_id is required."}, status=HTTPStatus.BAD_REQUEST)
            return None
        source_repo = str(payload.get("source_repo") or "").strip()
        if not source_repo:
            repo_source = self._resolve_payload_repository_or_error(payload, clone_if_missing=False)
            if repo_source is None:
                return None
            source_repo = repo_source.local_path
        try:
            task_run = self._get_task_run_or_restore(run_id, source_repo)
        except TaskRunError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            return None
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return None
        if task_run is None:
            self._send_json({"error": "Task run not found."}, status=HTTPStatus.NOT_FOUND)
            return None
        return task_run

    def _get_task_run_or_restore(self, run_id: str, source_repo: str | Path) -> TaskRun | None:
        requested_source = Path(source_repo).expanduser().resolve()
        task_run = get_task_run(run_id)
        if task_run is not None:
            if Path(task_run.source_repo).resolve() != requested_source:
                raise TaskRunError("Task run does not belong to the requested source repository.")
            return task_run
        record = self._memory(requested_source).get_task_run(run_id)
        if not record:
            return None
        task_run = task_run_from_record(record, mark_interrupted=True)
        self._persist_task_run(task_run)
        return task_run

    def _persist_task_run(self, task_run: TaskRun) -> None:
        self._memory(task_run.source_repo).save_task_run(task_run.to_record())

    def _inspect_task_run_recovery(
        self,
        task_run: TaskRun,
        current_execution_profile: TaskRunExecutionProfile | None = None,
    ) -> TaskRunRecoveryReadiness:
        proposal_record = None
        if task_run.proposal_id:
            session = get_proposal_session(task_run.proposal_id)
            if session is not None:
                proposal_record = proposal_session_to_record(session)
            else:
                proposal_record = self._memory(task_run.source_repo).get_proposal_session(
                    task_run.proposal_id
                )
        return inspect_task_run_recovery(
            task_run,
            proposal_record,
            current_execution_profile,
        )

    def _handle_propose(self) -> None:
        payload = self._read_json()
        task = str(payload.get("task") or "").strip()
        if not task:
            self._send_json({"error": "Task is required."}, status=HTTPStatus.BAD_REQUEST)
            return
        validation = payload.get("validation") or []
        if not isinstance(validation, list) or not all(isinstance(item, str) for item in validation):
            self._send_json({"error": "validation must be a list of strings."}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            max_repair_attempts = _payload_max_repair_attempts(payload)
            execution_budget = _payload_execution_budget(payload)
            _validate_validation_budget(validation, execution_budget)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        repo_source = self._resolve_payload_repository_or_error(payload)
        if repo_source is None:
            return

        use_llm = bool(payload.get("use_llm"))
        llm_client = None
        if use_llm and payload.get("api_key"):
            try:
                llm_client = OpenAICompatibleClient(
                    api_key=str(payload.get("api_key")),
                    base_url=str(payload.get("base_url") or "") or None,
                    model=str(payload.get("model") or "") or None,
                    json_mode=_payload_json_mode(payload),
                    timeout_seconds=_payload_llm_timeout_seconds(payload),
                )
            except LLMError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return

        try:
            report = run_workflow(
                repo_source.local_path,
                task,
                validation_commands=[],
                use_llm=use_llm,
                llm_client=llm_client,
                llm_model=str(payload.get("model") or "") or None,
                allow_llm_fallback=not bool(payload.get("no_llm_fallback")),
                llm_json_mode=_payload_json_mode(payload),
                llm_timeout_seconds=_payload_llm_timeout_seconds(payload),
                iterative_agent=_payload_iterative_agent(payload),
                agent_max_steps=_payload_agent_max_steps(payload),
                use_memory=_payload_use_memory(payload),
                execution_budget=execution_budget,
            )
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        proposal_id = None
        timeline = build_report_timeline(report)
        proposal = report.patch_proposal
        if proposal and proposal.file_edits and proposal.apply_ready:
            validation_commands = validation or (proposal.validation_plan.commands if proposal.validation_plan else [])
            _validate_validation_budget(validation_commands, execution_budget)
            session = create_proposal_session(
                repo_path=report.repo_path,
                task=task,
                file_edits=proposal.file_edits,
                validation_commands=validation_commands,
                timeline=timeline,
                allowed_paths=[file.path for file in proposal.files],
                max_repair_attempts=max_repair_attempts,
                acceptance_criteria=build_acceptance_criteria(
                    task,
                    [edit.path for edit in proposal.file_edits],
                    validation_commands,
                ),
                execution_budget=execution_budget,
                execution_usage=ExecutionUsage.from_dict(report.execution_budget.get("usage")),
            )
            proposal_id = session.proposal_id
            append_timeline(session, "approval", "pending", f"Waiting for approval on proposal {proposal_id}.")
            self._persist_session(session)
            timeline = session.timeline
        data = report.to_dict()
        data["repository_source"] = repo_source.to_dict()
        data["proposal_id"] = proposal_id
        data["timeline"] = [asdict(event) for event in timeline]
        if proposal_id and session:
            _add_session_public_fields(data, session)
        try:
            data["run_id"] = self._memory(report.repo_path).create_run(
                repo_path=report.repo_path,
                task=task,
                mode="propose",
                report=report,
                proposal_id=proposal_id,
                timeline=data["timeline"],
            )
        except Exception as exc:
            data["memory_error"] = str(exc)
        self._send_json(data)

    def _handle_git_status(self, query: str) -> None:
        params = parse_qs(query)
        try:
            repo_source = self._resolve_query_repository(params)
            data = asdict(inspect_repository(repo_source.local_path))
            data["repository_source"] = repo_source.to_dict()
            self._send_json(data)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_git_diff(self, query: str) -> None:
        params = parse_qs(query)
        staged = _first(params, "staged", "false").lower() == "true"
        try:
            repo_source = self._resolve_query_repository(params)
            self._send_json(
                {
                    "repo": repo_source.local_path,
                    "staged": staged,
                    "diff": get_git_diff(repo_source.local_path, staged=staged),
                    "repository_source": repo_source.to_dict(),
                }
            )
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_github_status(self, query: str) -> None:
        params = parse_qs(query)
        try:
            limit = int(_first(params, "limit", "5"))
        except ValueError:
            limit = 5
        try:
            repo_source = self._resolve_query_repository(params)
            snapshot = inspect_github_repository(repo_source.local_path, limit=limit)
            data = snapshot.to_dict()
            data["repository_source"] = repo_source.to_dict()
            self._send_json(data)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_history_list(self, query: str) -> None:
        params = parse_qs(query)
        try:
            limit = int(_first(params, "limit", "20"))
        except ValueError:
            limit = 20
        try:
            repo_source = self._resolve_query_repository(params, clone_if_missing=False)
            runs = self._memory(repo_source.local_path).list_runs(limit=limit)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json({"runs": runs, "repository_source": repo_source.to_dict()})

    def _handle_history_detail(self, query: str) -> None:
        params = parse_qs(query)
        run_id = _first(params, "id", "").strip()
        if not run_id:
            self._send_json({"error": "id is required."}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            repo_source = self._resolve_query_repository(params, clone_if_missing=False)
            run = self._memory(repo_source.local_path).get_run(run_id)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if run is None:
            self._send_json({"error": "Run not found."}, status=HTTPStatus.NOT_FOUND)
            return
        self._send_json(run)

    def _handle_history_delete(self) -> None:
        payload = self._read_json()
        run_id = str(payload.get("id") or "").strip()
        if not run_id:
            self._send_json({"error": "id is required."}, status=HTTPStatus.BAD_REQUEST)
            return
        repo_source = self._resolve_payload_repository_or_error(payload, clone_if_missing=False)
        if repo_source is None:
            return
        try:
            deleted = self._memory(repo_source.local_path).delete_run(run_id)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if not deleted:
            self._send_json({"error": "Run not found."}, status=HTTPStatus.NOT_FOUND)
            return
        self._send_json({"deleted": True, "id": run_id, "repository_source": repo_source.to_dict()})

    def _handle_history_clear(self) -> None:
        payload = self._read_json()
        repo_source = self._resolve_payload_repository_or_error(payload, clone_if_missing=False)
        if repo_source is None:
            return
        try:
            deleted_count = self._memory(repo_source.local_path).clear_runs()
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json({"deleted": deleted_count, "repository_source": repo_source.to_dict()})

    def _handle_history_pin(self) -> None:
        payload = self._read_json()
        run_id = str(payload.get("id") or "").strip()
        if not run_id:
            self._send_json({"error": "id is required."}, status=HTTPStatus.BAD_REQUEST)
            return
        pinned = _payload_bool(payload.get("pinned"), default=True)
        repo_source = self._resolve_payload_repository_or_error(payload, clone_if_missing=False)
        if repo_source is None:
            return
        try:
            updated = self._memory(repo_source.local_path).set_run_pinned(run_id, pinned)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if not updated:
            self._send_json({"error": "Run not found."}, status=HTTPStatus.NOT_FOUND)
            return
        self._send_json({"id": run_id, "pinned": pinned, "repository_source": repo_source.to_dict()})

    def _serve_static(self, path: str) -> None:
        target = "index.html" if path in {"", "/"} else path.lstrip("/")
        file_path = (STATIC_DIR / target).resolve()
        if not _is_relative_to(file_path, STATIC_DIR) or not file_path.is_file():
            self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        body = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _send_json(self, data: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _memory(self, repo: str | Path) -> MemoryStore:
        ensure_local_state_ignored(repo)
        return MemoryStore(default_memory_path(repo))

    def _get_session_or_restore(self, proposal_id: str, payload: dict[str, Any]) -> ProposalSession | None:
        session = get_proposal_session(proposal_id)
        if session is not None:
            return session
        try:
            repo_source = self._resolve_payload_repository(payload, clone_if_missing=False)
            record = self._memory(repo_source.local_path).get_proposal_session(proposal_id)
        except Exception:
            return None
        if not record:
            return None
        return proposal_session_from_record(record)

    def _persist_session(self, session: ProposalSession) -> None:
        self._memory(session.repo_path).save_proposal_session(proposal_session_to_record(session))

    def _resolve_payload_repository_or_error(
        self,
        payload: dict[str, Any],
        clone_if_missing: bool = True,
    ) -> Any | None:
        try:
            return self._resolve_payload_repository(payload, clone_if_missing=clone_if_missing)
        except (ValueError, FileNotFoundError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
        return None

    def _resolve_payload_repository(self, payload: dict[str, Any], clone_if_missing: bool = True) -> Any:
        return resolve_repository_reference(
            repo=payload.get("repo") or ".",
            repo_source=str(payload.get("repo_source") or "auto"),
            github_url=str(payload.get("github_url") or ""),
            clone_if_missing=clone_if_missing,
        )

    def _resolve_query_repository(self, params: dict[str, list[str]], clone_if_missing: bool = True) -> Any:
        return resolve_repository_reference(
            repo=_first(params, "repo", "."),
            repo_source=_first(params, "repo_source", "auto"),
            github_url=_first(params, "github_url", ""),
            clone_if_missing=clone_if_missing,
        )


def _first(params: dict[str, list[str]], name: str, default: str) -> str:
    values = params.get(name)
    return values[0] if values else default


def _payload_use_memory(payload: dict[str, Any]) -> bool:
    return _payload_bool(payload.get("use_memory"), default=True)


def _payload_auto_repair(payload: dict[str, Any]) -> bool:
    return _payload_bool(payload.get("auto_repair"), default=True)


def _payload_execution_profile(
    payload: dict[str, Any],
    execution_budget: ExecutionBudget,
    llm_client: OpenAICompatibleClient | None,
    max_repair_attempts: int,
    auto_repair_enabled: bool,
) -> TaskRunExecutionProfile:
    raw_timeout = payload.get("timeout_seconds")
    try:
        requested_timeout = int(raw_timeout) if raw_timeout is not None and raw_timeout != "" else None
    except (TypeError, ValueError):
        requested_timeout = None
    return create_execution_profile(
        use_llm=bool(payload.get("use_llm")),
        model=llm_client.model if llm_client else str(payload.get("model") or ""),
        endpoint_url=(
            llm_client.base_url if llm_client else str(payload.get("base_url") or "")
        ),
        json_mode=llm_client.json_mode if llm_client else _payload_json_mode(payload),
        allow_llm_fallback=not _payload_bool(payload.get("no_llm_fallback"), default=False),
        use_memory=_payload_use_memory(payload),
        iterative_agent=_payload_iterative_agent(payload),
        llm_timeout_seconds=(
            llm_client.timeout_seconds if llm_client else requested_timeout
        ),
        max_repair_attempts=max_repair_attempts,
        auto_repair_enabled=auto_repair_enabled,
        execution_budget=execution_budget,
    )


def _current_execution_profile_from_payload(
    payload: dict[str, Any],
    llm_client: OpenAICompatibleClient | None = None,
    *,
    resolve_llm_client: bool = True,
) -> TaskRunExecutionProfile:
    client = llm_client
    if client is None and resolve_llm_client:
        client = _payload_llm_client(payload)
    return _payload_execution_profile(
        payload,
        _payload_execution_budget(payload),
        client,
        _payload_max_repair_attempts(payload),
        _payload_auto_repair(payload),
    )


def _payload_llm_client(payload: dict[str, Any]) -> OpenAICompatibleClient | None:
    if not bool(payload.get("use_llm")) or not payload.get("api_key"):
        return None
    return OpenAICompatibleClient(
        api_key=str(payload.get("api_key")),
        base_url=str(payload.get("base_url") or "") or None,
        model=str(payload.get("model") or "") or None,
        json_mode=_payload_json_mode(payload),
        timeout_seconds=_payload_llm_timeout_seconds(payload),
    )


def _payload_json_mode(payload: dict[str, Any]) -> bool | None:
    if payload.get("json_mode") is None:
        return None
    return _payload_bool(payload.get("json_mode"), default=True)


def _payload_llm_timeout_seconds(payload: dict[str, Any]) -> int | None:
    raw = payload.get("timeout_seconds")
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise LLMError("LLM timeout must be an integer number of seconds.") from exc
    if value <= 0:
        raise LLMError("LLM timeout must be greater than 0 seconds.")
    return value


def _payload_iterative_agent(payload: dict[str, Any]) -> bool:
    return _payload_bool(payload.get("iterative_agent"), default=False)


def _payload_agent_max_steps(payload: dict[str, Any]) -> int:
    raw = payload.get("agent_max_steps")
    if raw is None or raw == "":
        return 6
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise LLMError("Agent max steps must be an integer.") from exc
    if value <= 0:
        raise LLMError("Agent max steps must be greater than 0.")
    return min(value, 12)


def _payload_execution_budget(payload: dict[str, Any]) -> ExecutionBudget:
    return ExecutionBudget(
        max_agent_steps=_payload_agent_max_steps(payload),
        max_tool_calls=_payload_positive_int(
            payload,
            "agent_max_tool_calls",
            default=12,
            maximum=24,
            label="Agent max tool calls",
        ),
        max_validation_commands=_payload_positive_int(
            payload,
            "max_validation_commands",
            default=4,
            maximum=16,
            label="Validation command limit",
        ),
        max_elapsed_seconds=_payload_positive_int(
            payload,
            "execution_timeout_seconds",
            default=600,
            maximum=3600,
            label="Execution timeout",
        ),
    )


def _payload_positive_int(
    payload: dict[str, Any],
    key: str,
    *,
    default: int,
    maximum: int,
    label: str,
) -> int:
    raw = payload.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer.") from exc
    if value <= 0:
        raise ValueError(f"{label} must be greater than 0.")
    return min(value, maximum)


def _validate_validation_budget(commands: list[str], budget: ExecutionBudget) -> None:
    if len(commands) > budget.max_validation_commands:
        raise ValueError(
            f"Validation command count {len(commands)} exceeds the configured limit "
            f"of {budget.max_validation_commands}."
        )


def _payload_max_repair_attempts(payload: dict[str, Any]) -> int:
    raw = payload.get("max_repair_attempts")
    if raw is None or raw == "":
        return DEFAULT_MAX_REPAIR_ATTEMPTS
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Repair max attempts must be an integer.") from exc
    if value < 0:
        raise ValueError("Repair max attempts cannot be negative.")
    return min(value, MAX_REPAIR_ATTEMPTS_LIMIT)


def _repair_task_with_budget(task: str, attempt: int, max_attempts: int) -> str:
    return "\n\n".join(
        [
            task.strip(),
            (
                f"Repair attempt: {attempt}/{max_attempts}. "
                "Use the latest validation failure context and avoid repeating ineffective edits."
            ),
        ]
    ).strip()


def _repair_task_with_history(
    task: str,
    *,
    root_task: str,
    history: list[RepairAttemptRecord],
    attempt: int,
    max_attempts: int,
) -> str:
    base = _repair_task_with_budget(task, attempt, max_attempts)
    prior = [
        (
            f"- Attempt {item.attempt}: {item.status}; "
            f"failure={_short_fingerprint(item.result_failure_fingerprint or item.trigger_failure_fingerprint)}; "
            f"proposal={_short_fingerprint(item.proposal_fingerprint)}; {item.summary}"
        )
        for item in history[-5:]
    ]
    sections = [
        base,
        f"Root objective: {root_task.strip() or '(not provided)'}",
        "Prior repair outcomes:\n" + ("\n".join(prior) if prior else "- No previous repair attempt."),
        "Generate a materially different, narrow proposal. Do not repeat a prior proposal fingerprint.",
    ]
    combined = "\n\n".join(sections).strip()
    return combined if len(combined) <= 7500 else combined[:7470].rstrip() + "\n... repair history truncated ..."


def _remaining_repair_execution_budget(
    session: ProposalSession,
    *,
    iterative_agent: bool,
) -> ExecutionBudget:
    state = execution_budget_state(session.execution_budget, session.execution_usage)
    if state["exhausted"]:
        raise RepairLoopStopped(
            STOP_EXECUTION_BUDGET,
            "Execution budget is already exhausted: " + "; ".join(state["exhausted_reasons"]),
        )
    remaining = state["remaining"]
    required_validation = len(session.validation_commands)
    reserved_tools = 1 + required_validation
    reasons: list[str] = []
    if remaining["validation_commands"] < required_validation:
        reasons.append("insufficient validation-command capacity for the next repair")
    if remaining["tool_calls"] < reserved_tools:
        reasons.append("insufficient tool-call capacity to apply and validate the next repair")
    if remaining["elapsed_ms"] <= 0:
        reasons.append("no elapsed-time capacity remains")
    available_agent_steps = remaining["agent_steps"]
    available_agent_tools = remaining["tool_calls"] - reserved_tools
    if iterative_agent and available_agent_steps <= 0:
        reasons.append("no Agent-step capacity remains for iterative repair exploration")
    if iterative_agent and available_agent_tools <= 0:
        reasons.append("no tool-call capacity remains for iterative repair exploration")
    if reasons:
        raise RepairLoopStopped(STOP_EXECUTION_BUDGET, "; ".join(reasons) + ".")
    return ExecutionBudget(
        max_agent_steps=max(available_agent_steps, 1),
        max_tool_calls=max(available_agent_tools, 1),
        max_validation_commands=max(remaining["validation_commands"], 1),
        max_elapsed_seconds=max((remaining["elapsed_ms"] + 999) // 1000, 1),
    )


def _short_fingerprint(value: str) -> str:
    return value[:12] if value else "none"


def _add_session_public_fields(data: dict[str, Any], session: ProposalSession) -> None:
    public = session.to_public_dict()
    for key in _SESSION_PUBLIC_KEYS:
        data[key] = public[key]


def _payload_approved_paths(payload: dict[str, Any], file_edits: list[FileEditProposal]) -> list[str]:
    available_paths = [edit.path for edit in file_edits]
    if not available_paths:
        raise ValueError("No proposal file edits are available to apply.")
    raw_paths = payload.get("approved_paths")
    if raw_paths is None:
        return available_paths
    if not isinstance(raw_paths, list) or not all(isinstance(path, str) for path in raw_paths):
        raise ValueError("approved_paths must be a list of strings.")

    requested: set[str] = set()
    for raw_path in raw_paths:
        path = _normalize_approved_path(raw_path)
        if path:
            requested.add(path)
    if not requested:
        raise ValueError("approved_paths must select at least one proposal file.")

    available = set(available_paths)
    unknown = sorted(requested - available)
    if unknown:
        raise ValueError(
            "approved_paths contains file(s) that are not in this proposal: "
            + ", ".join(unknown)
        )
    return [path for path in available_paths if path in requested]


def _payload_string_list(payload: dict[str, Any], name: str) -> list[str]:
    value = payload.get(name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a list of strings.")
    return list(value)


def _public_runtime_events(events: list[Any]) -> list[dict[str, Any]]:
    public_events: list[dict[str, Any]] = []
    for event in events:
        data = event.to_dict()
        if data.get("event_type") == "rollback_snapshot_recorded":
            payload = dict(data.get("payload") or {})
            snapshots = payload.get("snapshots")
            if isinstance(snapshots, list):
                payload["snapshots"] = [
                    {
                        "path": snapshot.get("path"),
                        "existed": snapshot.get("existed"),
                    }
                    for snapshot in snapshots
                    if isinstance(snapshot, dict)
                ]
            else:
                payload["snapshots"] = []
            data["payload"] = payload
        public_events.append(data)
    return public_events


def _dedupe_commands(commands: list[str] | tuple[str, ...]) -> list[str]:
    normalized: list[str] = []
    for raw_command in commands:
        command = str(raw_command).strip()
        if command and command not in normalized:
            normalized.append(command)
    return normalized


def _append_agent_write_history(
    existing: object,
    write_result: dict[str, Any],
) -> list[dict[str, Any]]:
    history = [dict(item) for item in existing or [] if isinstance(item, dict)]
    observation = write_result.get("write_observation")
    data = observation.get("data") if isinstance(observation, dict) else None
    write_data = data if isinstance(data, dict) else {}
    changed_files = _safe_record_paths(write_data.get("changed_files"))
    evidence = write_data.get("write_evidence")
    if not changed_files and isinstance(evidence, list):
        changed_files = _safe_record_paths(
            [
                item.get("path")
                for item in evidence
                if isinstance(item, dict)
                and (
                    item.get("before_exists") != item.get("after_exists")
                    or item.get("before_sha256") != item.get("after_sha256")
                )
            ]
        )
    approved_paths = _safe_record_paths(
        [item.get("path") for item in evidence if isinstance(item, dict)]
        if isinstance(evidence, list)
        else changed_files
    )
    record = {
        "action_id": str(write_result.get("action_id") or ""),
        "status": str(write_result.get("status") or "completed"),
        "changed_files": changed_files,
        "approved_paths": approved_paths,
    }
    history = [
        item
        for item in history
        if item.get("action_id") != record["action_id"]
    ]
    return [*history, record][-MAX_AGENT_WRITE_HISTORY:]


def _task_run_max_repair_attempts(task_run: TaskRun) -> int:
    if task_run.execution_profile is None:
        return DEFAULT_MAX_REPAIR_ATTEMPTS
    return min(
        max(task_run.execution_profile.max_repair_attempts, 0),
        MAX_REPAIR_ATTEMPTS_LIMIT,
    )


def _pending_agent_repair_attempt(task_result: dict[str, Any]) -> int:
    value = task_result.get("agent_repair_pending_attempt", 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return 0
    return value


def _apply_agent_repair_transition(
    task_run: TaskRun,
    task_result: dict[str, Any],
    transition: AgentRepairTransition,
    store: SQLiteRuntimeStore,
) -> None:
    task_run.repair_history = list(transition.history)
    task_run.repair_stop_reason = transition.stop_reason
    task_run.repair_stop_message = transition.message if transition.stop_reason else ""
    task_result["agent_repair"] = transition.to_dict()
    if transition.stop_reason:
        state = agent_working_state_from_record(
            task_result.get("agent_state"),
            default_objective=task_run.task,
        )
        if state is not None and state.stop_reason != transition.stop_reason:
            state = stop_agent_working_state(state, transition.stop_reason)
            task_result["agent_state"] = state.to_dict()
            store.append_event(
                task_run.run_id,
                "working_state_updated",
                payload={"working_state": state.to_dict()},
            )
    store.append_event(
        task_run.run_id,
        "repair_progress_updated",
        payload={"repair": transition.to_dict()},
    )
    _sync_agent_repair_result(task_run, task_result)
    task_result["agent_events"] = _public_runtime_events(
        store.list_events(task_run.run_id)
    )


def _finish_stopped_agent_repair(
    task_run: TaskRun,
    task_result: dict[str, Any],
    transition: AgentRepairTransition,
) -> None:
    task_result["agent_pending_approval"] = {}
    task_result["agent_stop_reason"] = transition.stop_reason
    task_result.pop("agent_repair_pending_attempt", None)
    _sync_agent_repair_result(task_run, task_result)
    update_task_run(
        task_run,
        "failed",
        transition.message,
        result=task_result,
        error=None,
    )
    record_task_run_checkpoint(
        task_run,
        "runtime_repair_stopped",
        transition.message,
        "inspect_failure",
    )


def _sync_agent_repair_result(
    task_run: TaskRun,
    task_result: dict[str, Any],
) -> None:
    maximum = _task_run_max_repair_attempts(task_run)
    pending = _pending_agent_repair_attempt(task_result)
    recorded_attempts = [item.attempt for item in task_run.repair_history]
    current = max([pending, *recorded_attempts], default=0)
    remaining = max(maximum - current, 0)
    latest = max(task_run.repair_history, key=lambda item: item.attempt, default=None)
    repair_needed = bool(latest and latest.status == "validation_failed")
    task_result.update(
        {
            "repair_attempt": current,
            "max_repair_attempts": maximum,
            "repair_budget_remaining": remaining,
            "next_repair_attempt": (
                current + 1
                if repair_needed
                and not task_run.repair_stop_reason
                and remaining > 0
                and pending == 0
                else None
            ),
            "repair_budget_exhausted": bool(
                task_run.repair_stop_reason == STOP_REPAIR_BUDGET
                or (repair_needed and remaining <= 0)
            ),
            "repair_history": [
                item.to_dict() for item in task_run.repair_history
            ],
            "repair_stop_reason": task_run.repair_stop_reason,
            "repair_stop_message": task_run.repair_stop_message,
            "auto_repair_enabled": task_run.auto_repair_enabled,
            "agent_repair_mode": "unified_controller",
        }
    )


def _approved_agent_write_paths(task_result: dict[str, Any]) -> list[str]:
    history = task_result.get("agent_write_history")
    paths: list[str] = []
    if isinstance(history, list):
        for item in history:
            if isinstance(item, dict):
                paths.extend(_safe_record_paths(item.get("approved_paths")))
    if not paths:
        latest = task_result.get("agent_write_result")
        if isinstance(latest, dict):
            observation = latest.get("write_observation")
            data = observation.get("data") if isinstance(observation, dict) else None
            if isinstance(data, dict):
                paths.extend(_safe_record_paths(data.get("changed_files")))
    return list(dict.fromkeys(paths))


def _safe_record_paths(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    paths: list[str] = []
    for raw_path in value:
        if not isinstance(raw_path, str):
            continue
        try:
            path = _normalize_approved_path(raw_path)
        except ValueError:
            continue
        if path and path not in paths:
            paths.append(path)
    return paths


def _task_validation_cycle(task_run: TaskRun) -> dict[str, Any]:
    if not isinstance(task_run.result, dict):
        return {}
    raw_cycle = task_run.result.get("agent_validation_cycle")
    if not isinstance(raw_cycle, dict):
        return {}
    cycle_id = str(raw_cycle.get("cycle_id") or "").strip()
    raw_commands = raw_cycle.get("commands")
    raw_results = raw_cycle.get("results")
    next_index = raw_cycle.get("next_index")
    repair_attempt = raw_cycle.get("repair_attempt", 0)
    if (
        not cycle_id
        or not isinstance(raw_commands, list)
        or not all(isinstance(command, str) for command in raw_commands)
        or not isinstance(raw_results, list)
        or not all(isinstance(result, dict) for result in raw_results)
        or not isinstance(next_index, int)
        or isinstance(next_index, bool)
        or not isinstance(repair_attempt, int)
        or isinstance(repair_attempt, bool)
        or repair_attempt < 0
    ):
        return {}
    commands = _dedupe_commands(raw_commands)
    if (
        not commands
        or commands != raw_commands
        or not 0 <= next_index <= len(commands)
        or len(raw_results) != next_index
    ):
        return {}
    for index, record in enumerate(raw_results):
        try:
            result = AgentValidationResult.from_dict(record)
        except (TypeError, ValueError):
            return {}
        if (
            result.cycle_id != cycle_id
            or result.command_index != index
            or result.command_count != len(commands)
            or result.command != commands[index]
            or result.status not in {"passed", "failed"}
        ):
            return {}
    return {
        "cycle_id": cycle_id,
        "commands": commands,
        "next_index": next_index,
        "results": [dict(result) for result in raw_results],
        "repair_attempt": repair_attempt,
    }


def _validation_results_from_cycle(
    cycle: dict[str, Any],
) -> list[ValidationResult]:
    results: list[ValidationResult] = []
    for record in cycle.get("results", []):
        if not isinstance(record, dict):
            continue
        observation = record.get("observation")
        data = observation.get("data") if isinstance(observation, dict) else None
        if not isinstance(data, dict):
            continue
        exit_code = data.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            exit_code = None
        results.append(
            ValidationResult(
                command=str(record.get("command") or data.get("command") or ""),
                allowed=data.get("allowed") is True,
                exit_code=exit_code,
                stdout=str(data.get("stdout") or ""),
                stderr=str(data.get("stderr") or ""),
            )
        )
    return results


def _remaining_agent_continuation_budget(
    task_run: TaskRun,
    payload: dict[str, Any],
) -> ExecutionBudget | None:
    budget_state = execution_budget_state(
        task_run.execution_budget,
        task_run.execution_usage,
    )
    remaining = budget_state["remaining"]
    if (
        budget_state["exhausted"]
        or remaining["agent_steps"] <= 0
        or remaining["tool_calls"] <= 0
        or remaining["elapsed_ms"] <= 0
    ):
        return None
    requested_steps = _payload_agent_max_steps(payload)
    return ExecutionBudget(
        max_agent_steps=min(requested_steps, remaining["agent_steps"]),
        max_tool_calls=remaining["tool_calls"],
        max_validation_commands=max(remaining["validation_commands"], 1),
        max_elapsed_seconds=max((remaining["elapsed_ms"] + 999) // 1000, 1),
    )


def _runtime_continuation_llm_client(
    payload: dict[str, Any],
    task_run: TaskRun,
) -> OpenAICompatibleClient | None:
    profile = task_run.execution_profile
    use_llm = _payload_bool(
        payload.get("use_llm"),
        default=profile.use_llm if profile else False,
    )
    if not use_llm:
        return None
    json_mode = (
        _payload_json_mode(payload)
        if "json_mode" in payload
        else profile.json_mode if profile else None
    )
    timeout_seconds = (
        _payload_llm_timeout_seconds(payload)
        if payload.get("timeout_seconds") not in {None, ""}
        else profile.llm_timeout_seconds if profile else None
    )
    return OpenAICompatibleClient(
        api_key=str(payload.get("api_key") or "") or None,
        base_url=str(payload.get("base_url") or "") or None,
        model=(
            str(payload.get("model") or "").strip()
            or (profile.model if profile else "")
            or None
        ),
        json_mode=json_mode,
        timeout_seconds=timeout_seconds,
    )


def _pending_runtime_approval(task_run: TaskRun) -> dict[str, Any]:
    if task_run.status != "awaiting_approval" or not isinstance(task_run.result, dict):
        return {}
    request = task_run.result.get("agent_pending_approval")
    if not isinstance(request, dict) or not request.get("checkpoint"):
        return {}
    return request


def _normalize_approved_path(path: str) -> str:
    stripped = path.strip()
    if not stripped:
        return ""
    normalized = PurePosixPath(stripped.replace("\\", "/"))
    parts = normalized.parts
    if normalized.is_absolute() or ".." in parts or any(part in {"", "."} for part in parts):
        raise ValueError(f"Unsafe approved path: {path}")
    return normalized.as_posix()


def _payload_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _text_preview(value: str, limit: int = 600) -> str:
    text = value.strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
