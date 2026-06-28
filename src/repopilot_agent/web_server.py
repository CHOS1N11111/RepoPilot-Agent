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
from .github_tools import inspect_github_repository
from .llm.base import LLMError
from .llm.openai_compatible import OpenAICompatibleClient
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
        self._serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/run":
            self._handle_run()
            return
        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _handle_run(self) -> None:
        payload = self._read_json()
        repo = str(payload.get("repo") or ".")
        task = str(payload.get("task") or "").strip()
        if not task:
            self._send_json({"error": "Task is required."}, status=HTTPStatus.BAD_REQUEST)
            return

        validation = payload.get("validation") or []
        if not isinstance(validation, list) or not all(isinstance(item, str) for item in validation):
            self._send_json({"error": "validation must be a list of strings."}, status=HTTPStatus.BAD_REQUEST)
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
                repo,
                task,
                validation_commands=validation,
                use_llm=use_llm,
                llm_client=llm_client,
                llm_model=str(payload.get("model") or "") or None,
                allow_llm_fallback=not bool(payload.get("no_llm_fallback")),
            )
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json(report.to_dict())

    def _handle_git_status(self, query: str) -> None:
        params = parse_qs(query)
        repo = _first(params, "repo", ".")
        try:
            self._send_json(asdict(inspect_repository(repo)))
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_git_diff(self, query: str) -> None:
        params = parse_qs(query)
        repo = _first(params, "repo", ".")
        staged = _first(params, "staged", "false").lower() == "true"
        try:
            self._send_json({"repo": repo, "staged": staged, "diff": get_git_diff(repo, staged=staged)})
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_github_status(self, query: str) -> None:
        params = parse_qs(query)
        repo = _first(params, "repo", ".")
        try:
            limit = int(_first(params, "limit", "5"))
        except ValueError:
            limit = 5
        snapshot = inspect_github_repository(repo, limit=limit)
        self._send_json(snapshot.to_dict())

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


def _first(params: dict[str, list[str]], name: str, default: str) -> str:
    values = params.get(name)
    return values[0] if values else default


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
