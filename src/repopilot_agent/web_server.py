"""Local web UI server for RepoPilot Agent."""

from __future__ import annotations

import json
import mimetypes
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlparse

from .git_tools import get_git_diff, inspect_repository
from .git_summary import build_git_workflow_summary, build_pull_request_readiness
from .github_tools import create_github_pull_request, inspect_github_repository
from .llm.base import LLMError, LLMMessage
from .llm.openai_compatible import OpenAICompatibleClient
from .memory import MemoryStore, default_memory_path
from .models import FileEditProposal
from .patch_apply import apply_file_edits, capture_file_snapshots, revert_file_snapshots
from .repo_source import resolve_repository_reference, sync_repository_reference
from .safety import SafetyCheckError
from .validation_feedback import build_validation_feedback
from .validator import run_validation
from .web_sessions import (
    DEFAULT_MAX_REPAIR_ATTEMPTS,
    ProposalSession,
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

_SESSION_PUBLIC_KEYS = (
    "parent_proposal_id",
    "repair_attempt",
    "max_repair_attempts",
    "repair_budget_remaining",
    "next_repair_attempt",
    "repair_budget_exhausted",
)


def run_web_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), RepoPilotRequestHandler)
    print(f"RepoPilot web UI running at http://{host}:{port}")
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
        try:
            approved_paths = _payload_approved_paths(payload, session.file_edits)
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
            append_timeline(session, "apply", "done", result.message)
            if session.rollback_snapshot:
                append_timeline(
                    session,
                    "rollback",
                    "ready",
                    f"Rollback snapshot captured for {len(session.rollback_snapshot)} file(s).",
                )
            if session.validation_commands:
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
        except SafetyCheckError as exc:
            append_timeline(session, "safety", "blocked", "Pre-apply safety check blocked this proposal.")
            self._persist_session(session)
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
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        data = result.to_dict()
        data["proposal_id"] = proposal_id
        data["validation"] = [asdict(item) for item in session.validation]
        data["validation_feedback"] = (
            asdict(session.validation_feedback) if session.validation_feedback else None
        )
        public_session = session.to_public_dict()
        data["timeline"] = public_session["timeline"]
        data["rollback_available"] = public_session["rollback_available"]
        data["reverted"] = public_session["reverted"]
        data["approved_paths"] = public_session["approved_paths"]
        data["applied_paths"] = public_session["applied_paths"]
        _add_session_public_fields(data, session)
        self._persist_session(session)
        try:
            self._memory(session.repo_path).mark_proposal_applied(
                proposal_id,
                session.validation,
                data["timeline"],
            )
        except Exception as exc:
            data["memory_error"] = str(exc)
        self._send_json(data)

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
        if session.repair_budget_exhausted():
            append_timeline(
                session,
                "repair",
                "blocked",
                f"Repair retry budget exhausted ({session.repair_attempt}/{session.max_repair_attempts}).",
            )
            self._persist_session(session)
            data = session.to_public_dict()
            data["error"] = "Repair retry budget exhausted for this proposal."
            self._send_json(data, status=HTTPStatus.BAD_REQUEST)
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

        next_repair_attempt = session.repair_attempt + 1
        repair_task = _repair_task_with_budget(
            session.validation_feedback.repair_task,
            next_repair_attempt,
            session.max_repair_attempts,
        )
        try:
            report = run_workflow(
                session.repo_path,
                repair_task,
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
            )
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        repair_proposal_id = None
        timeline = build_report_timeline(report)
        proposal = report.patch_proposal
        if proposal and proposal.file_edits and proposal.apply_ready:
            validation_commands = session.validation_commands or (
                proposal.validation_plan.commands if proposal.validation_plan else []
            )
            repair_session = create_proposal_session(
                repo_path=report.repo_path,
                task=repair_task,
                file_edits=proposal.file_edits,
                validation_commands=validation_commands,
                timeline=timeline,
                allowed_paths=[file.path for file in proposal.files],
                parent_proposal_id=proposal_id,
                repair_attempt=next_repair_attempt,
                max_repair_attempts=session.max_repair_attempts,
            )
            repair_proposal_id = repair_session.proposal_id
            append_timeline(
                repair_session,
                "approval",
                "pending",
                f"Waiting for approval on repair proposal {repair_proposal_id}.",
            )
            self._persist_session(repair_session)
            append_timeline(
                session,
                "repair",
                "done",
                (
                    f"Generated repair attempt {next_repair_attempt}/"
                    f"{session.max_repair_attempts}: {repair_proposal_id}."
                ),
            )
            self._persist_session(session)
            timeline = repair_session.timeline

        data = report.to_dict()
        data["proposal_id"] = repair_proposal_id
        data["parent_proposal_id"] = proposal_id
        data["repair_task"] = repair_task
        data["repair_attempt"] = next_repair_attempt
        data["max_repair_attempts"] = session.max_repair_attempts
        data["repair_budget_remaining"] = max(session.max_repair_attempts - next_repair_attempt, 0)
        data["next_repair_attempt"] = (
            next_repair_attempt + 1 if next_repair_attempt < session.max_repair_attempts else None
        )
        data["repair_budget_exhausted"] = False
        data["timeline"] = [asdict(event) for event in timeline]
        try:
            data["run_id"] = self._memory(report.repo_path).create_run(
                repo_path=report.repo_path,
                task=repair_task,
                mode="repair",
                report=report,
                proposal_id=repair_proposal_id,
                timeline=data["timeline"],
            )
        except Exception as exc:
            data["memory_error"] = str(exc)
        self._send_json(data)

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
            )
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        proposal_id = None
        timeline = build_report_timeline(report)
        proposal = report.patch_proposal
        if proposal and proposal.file_edits and proposal.apply_ready:
            validation_commands = validation or (proposal.validation_plan.commands if proposal.validation_plan else [])
            session = create_proposal_session(
                repo_path=report.repo_path,
                task=task,
                file_edits=proposal.file_edits,
                validation_commands=validation_commands,
                timeline=timeline,
                allowed_paths=[file.path for file in proposal.files],
                max_repair_attempts=max_repair_attempts,
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
