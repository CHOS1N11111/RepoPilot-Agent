"""Local web UI server for RepoPilot Agent."""

from __future__ import annotations

import json
import mimetypes
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .git_tools import get_git_diff, inspect_repository
from .git_summary import build_git_workflow_summary
from .github_tools import inspect_github_repository
from .llm.base import LLMError
from .llm.openai_compatible import OpenAICompatibleClient
from .memory import MemoryStore, default_memory_path
from .patch_apply import apply_file_edits
from .repo_source import resolve_repository_reference, sync_repository_reference
from .safety import SafetyCheckError
from .validator import run_validation
from .web_sessions import append_timeline, build_report_timeline, create_proposal_session, get_proposal_session
from .workflow import run_workflow

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


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
        if parsed.path == "/api/git/summary":
            self._handle_git_summary()
            return
        if parsed.path == "/api/repository/sync":
            self._handle_repository_sync()
            return
        if parsed.path == "/api/history/delete":
            self._handle_history_delete()
            return
        if parsed.path == "/api/history/clear":
            self._handle_history_clear()
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
        session = get_proposal_session(proposal_id)
        if session is None:
            self._send_json({"error": "Unknown proposal_id."}, status=HTTPStatus.BAD_REQUEST)
            return
        if session.applied:
            self._send_json({"error": "Proposal has already been applied."}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            result = apply_file_edits(
                session.repo_path,
                session.file_edits,
                task=session.task,
                allowed_paths=session.allowed_paths,
            )
            session.applied = True
            append_timeline(session, "apply", "done", result.message)
            if session.validation_commands:
                validation = run_validation(session.repo_path, session.validation_commands)
                session.validation = validation
                failed = [item for item in validation if item.exit_code not in (0, None)]
                rejected = [item for item in validation if not item.allowed]
                if failed or rejected:
                    append_timeline(
                        session,
                        "validation",
                        "warning",
                        f"Validation completed with {len(failed)} failed and {len(rejected)} rejected command(s).",
                    )
                else:
                    append_timeline(session, "validation", "done", f"Ran {len(validation)} validation command(s).")
            else:
                append_timeline(session, "validation", "skipped", "No validation command was configured.")
        except SafetyCheckError as exc:
            append_timeline(session, "safety", "blocked", "Pre-apply safety check blocked this proposal.")
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
        data["timeline"] = session.to_public_dict()["timeline"]
        try:
            self._memory(session.repo_path).mark_proposal_applied(
                proposal_id,
                session.validation,
                data["timeline"],
            )
        except Exception as exc:
            data["memory_error"] = str(exc)
        self._send_json(data)

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
            )
            proposal_id = session.proposal_id
            append_timeline(session, "approval", "pending", f"Waiting for approval on proposal {proposal_id}.")
            timeline = session.timeline
        data = report.to_dict()
        data["repository_source"] = repo_source.to_dict()
        data["proposal_id"] = proposal_id
        data["timeline"] = [asdict(event) for event in timeline]
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
    value = payload.get("use_memory", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
