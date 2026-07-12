from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.git_tools import get_git_diff
from repopilot_agent.llm.base import LLMClient, LLMMessage
from repopilot_agent.memory import MemoryStore, default_memory_path
from repopilot_agent.models import FileEditProposal, PlanMetadata, ValidationFeedback, WorkflowReport
from repopilot_agent.repo_source import RepositorySource
from repopilot_agent.web_server import (
    RepoPilotRequestHandler,
    STATIC_DIR,
    _payload_approved_paths,
    _payload_max_repair_attempts,
)
from repopilot_agent.web_sessions import clear_proposal_sessions, create_proposal_session, proposal_session_to_record


class FakeLLMClient(LLMClient):
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.model = "fake-web"

    def complete(self, messages: list[LLMMessage]) -> str:
        return self.responses.pop(0)


def prepare_ready_pr_repository(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "tester@example.local"], cwd=root, check=True)
    (root / "README.md").write_text("# Demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "switch", "-c", "feature/pr-ready"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/example/project.git"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=root, check=True)
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "update-ref", "refs/remotes/origin/feature/pr-ready", "HEAD"], cwd=root, check=True)
    subprocess.run(
        ["git", "branch", "--set-upstream-to", "origin/feature/pr-ready"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


class ApprovedPathsPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.edits = [
            FileEditProposal(path="notes.txt", new_content="new notes\n", rationale="Update notes."),
            FileEditProposal(path="src/main.py", new_content="print('ok')\n", rationale="Update main."),
        ]

    def test_payload_approved_paths_defaults_to_all_file_edits(self) -> None:
        self.assertEqual(_payload_approved_paths({}, self.edits), ["notes.txt", "src/main.py"])

    def test_payload_approved_paths_preserves_proposal_order_and_deduplicates(self) -> None:
        result = _payload_approved_paths(
            {"approved_paths": ["src\\main.py", "notes.txt", "notes.txt"]},
            self.edits,
        )

        self.assertEqual(result, ["notes.txt", "src/main.py"])

    def test_payload_approved_paths_rejects_empty_or_malformed_values(self) -> None:
        cases = [
            ({"approved_paths": []}, "select at least one"),
            ({"approved_paths": [""]}, "select at least one"),
            ({"approved_paths": "notes.txt"}, "list of strings"),
            ({"approved_paths": [123]}, "list of strings"),
        ]

        for payload, expected in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError) as raised:
                    _payload_approved_paths(payload, self.edits)
                self.assertIn(expected, str(raised.exception))

    def test_payload_approved_paths_rejects_unknown_or_unsafe_paths(self) -> None:
        cases = [
            ({"approved_paths": ["missing.txt"]}, "not in this proposal"),
            ({"approved_paths": ["../secret.txt"]}, "Unsafe approved path"),
            ({"approved_paths": ["/absolute.txt"]}, "Unsafe approved path"),
        ]

        for payload, expected in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError) as raised:
                    _payload_approved_paths(payload, self.edits)
                self.assertIn(expected, str(raised.exception))

    def test_payload_approved_paths_rejects_empty_proposals(self) -> None:
        with self.assertRaises(ValueError) as raised:
            _payload_approved_paths({}, [])

        self.assertIn("No proposal file edits", str(raised.exception))

    def test_payload_max_repair_attempts_defaults_clamps_and_validates(self) -> None:
        self.assertEqual(_payload_max_repair_attempts({}), 2)
        self.assertEqual(_payload_max_repair_attempts({"max_repair_attempts": ""}), 2)
        self.assertEqual(_payload_max_repair_attempts({"max_repair_attempts": "0"}), 0)
        self.assertEqual(_payload_max_repair_attempts({"max_repair_attempts": "9"}), 5)

        for payload, expected in [
            ({"max_repair_attempts": "-1"}, "cannot be negative"),
            ({"max_repair_attempts": "many"}, "must be an integer"),
        ]:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError) as raised:
                    _payload_max_repair_attempts(payload)
                self.assertIn(expected, str(raised.exception))


class WebServerTests(unittest.TestCase):
    def test_static_assets_exist(self) -> None:
        self.assertTrue((STATIC_DIR / "index.html").is_file())
        self.assertTrue((STATIC_DIR / "app.css").is_file())
        self.assertTrue((STATIC_DIR / "app.js").is_file())

    def test_get_git_diff_returns_working_tree_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "tester@example.local"], cwd=root, check=True)
            file_path = root / "README.md"
            file_path.write_text("first\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "Initial"], cwd=root, check=True, capture_output=True, text=True)

            file_path.write_text("first\nsecond\n", encoding="utf-8")
            diff = get_git_diff(root)

            self.assertIn("+second", diff)

    def test_propose_api_returns_patch_proposal_without_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "search.py").write_text(
                "def search_login(query):\n    return query.lower()\n",
                encoding="utf-8",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                payload = json.dumps(
                    {
                        "repo": str(root),
                        "task": "fix login search behavior",
                        "validation": ["python -m unittest discover -s tests"],
                    }
                ).encode("utf-8")
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/propose",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )

                with urlopen(request, timeout=5) as response:
                    data = json.loads(response.read().decode("utf-8"))

                self.assertEqual(data["task"], "fix login search behavior")
                self.assertEqual(data["validation"], [])
                self.assertTrue(data["patch_proposal"]["ready_for_patch"])
                self.assertEqual(data["patch_proposal"]["files"][0]["path"], "search.py")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_run_api_accepts_github_repository_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("def login():\n    return True\n", encoding="utf-8")
            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                source = RepositorySource(
                    source="github",
                    input="https://github.com/example/project",
                    local_path=str(root),
                    github_url="https://github.com/example/project",
                    owner="example",
                    repo="project",
                    cached=True,
                    message="Using cached clone for https://github.com/example/project.",
                )
                with patch("repopilot_agent.web_server.resolve_repository_reference", return_value=source):
                    payload = json.dumps(
                        {
                            "repo_source": "github",
                            "github_url": "https://github.com/example/project",
                            "task": "fix login behavior",
                        }
                    ).encode("utf-8")
                    request = Request(
                        f"http://127.0.0.1:{server.server_port}/api/run",
                        data=payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )

                    with urlopen(request, timeout=5) as response:
                        data = json.loads(response.read().decode("utf-8"))

                self.assertEqual(data["repository_source"]["source"], "github")
                self.assertEqual(data["repository_source"]["github_url"], "https://github.com/example/project")
                self.assertEqual(data["repo_path"], str(root.resolve()))
                self.assertEqual(data["relevant_files"][0]["path"], "main.py")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_run_api_can_disable_memory_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("def parse(value):\n    return value\n", encoding="utf-8")
            store = MemoryStore(default_memory_path(root))
            history_report = WorkflowReport(
                task="fix parser validation failure",
                repo_path=tmp,
                files_scanned=1,
                plan_metadata=PlanMetadata(source="rules"),
                summary="RepoPilot analyzed a parser failure and recommended parser validation.",
            )
            store.create_run(tmp, "fix parser validation failure", "run", history_report)
            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                payload = json.dumps(
                    {
                        "repo": str(root),
                        "task": "fix parser failure",
                        "use_memory": False,
                    }
                ).encode("utf-8")
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/run",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )

                with urlopen(request, timeout=5) as response:
                    data = json.loads(response.read().decode("utf-8"))

                self.assertEqual(data["memory_context"], [])
                self.assertFalse(any(step["title"] == "Review related memory" for step in data["plan"]))
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_run_api_passes_iterative_agent_options_to_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("RepoPilot test project\n", encoding="utf-8")
            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch(
                    "repopilot_agent.web_server.run_workflow",
                    return_value=WorkflowReport(
                        task="inspect repository",
                        repo_path=str(root),
                        files_scanned=1,
                        plan_metadata=PlanMetadata(source="rules"),
                        summary="done",
                    ),
                ) as workflow:
                    payload = json.dumps(
                        {
                            "repo": str(root),
                            "task": "inspect repository",
                            "iterative_agent": True,
                            "agent_max_steps": 4,
                        }
                    ).encode("utf-8")
                    request = Request(
                        f"http://127.0.0.1:{server.server_port}/api/run",
                        data=payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )

                    with urlopen(request, timeout=5) as response:
                        data = json.loads(response.read().decode("utf-8"))

                self.assertEqual(data["task"], "inspect repository")
                self.assertTrue(workflow.call_args.kwargs["iterative_agent"])
                self.assertEqual(workflow.call_args.kwargs["agent_max_steps"], 4)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_run_api_passes_json_mode_toggle_to_llm_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("RepoPilot test project\n", encoding="utf-8")
            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch(
                    "repopilot_agent.web_server.OpenAICompatibleClient",
                    return_value=FakeLLMClient(
                        [
                            '{"steps":[{"title":"Inspect README","detail":"Review README.md."}]}',
                            '{"objective":"Explain project","files":[{"path":"README.md","change_type":"documentation",'
                            '"rationale":"README.md describes the project.","suggested_actions":["Summarize the project"],'
                            '"confidence":"high"}],"risks":[],"validation_suggestions":[],"ready_for_patch":true,'
                            '"file_edits":[]}',
                        ]
                    ),
                ) as client_cls:
                    payload = json.dumps(
                        {
                            "repo": str(root),
                            "task": "explain what this project does",
                            "use_llm": True,
                            "api_key": "test-key",
                            "json_mode": False,
                            "timeout_seconds": 180,
                        }
                    ).encode("utf-8")
                    request = Request(
                        f"http://127.0.0.1:{server.server_port}/api/run",
                        data=payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )

                    with urlopen(request, timeout=5) as response:
                        data = json.loads(response.read().decode("utf-8"))

                self.assertEqual(data["plan_metadata"]["source"], "llm")
                self.assertFalse(client_cls.call_args.kwargs["json_mode"])
                self.assertEqual(client_cls.call_args.kwargs["timeout_seconds"], 180)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_llm_test_api_uses_configured_client_without_exposing_key(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch(
                "repopilot_agent.web_server.OpenAICompatibleClient",
                return_value=FakeLLMClient(['{"ok": true, "message": "ready"}']),
            ) as client_cls:
                payload = json.dumps(
                    {
                        "api_key": "test-key",
                        "base_url": "https://sub2api.example/v1/chat/completions",
                        "model": "gpt-5.5",
                        "timeout_seconds": 240,
                    }
                ).encode("utf-8")
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/llm/test",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )

                with urlopen(request, timeout=5) as response:
                    data = json.loads(response.read().decode("utf-8"))

            self.assertTrue(data["ok"])
            self.assertEqual(data["model"], "fake-web")
            self.assertNotIn("api_key", data)
            self.assertNotIn("test-key", json.dumps(data))
            self.assertEqual(client_cls.call_args.kwargs["base_url"], "https://sub2api.example/v1/chat/completions")
            self.assertEqual(client_cls.call_args.kwargs["model"], "gpt-5.5")
            self.assertEqual(client_cls.call_args.kwargs["timeout_seconds"], 240)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_repository_sync_api_returns_branch_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                source = RepositorySource(
                    source="github",
                    input="https://github.com/example/project",
                    local_path=str(root),
                    github_url="https://github.com/example/project",
                    owner="example",
                    repo="project",
                    branch="feature",
                    latest_commit="abc123 Sync",
                    dirty=False,
                    cached=True,
                    synced=True,
                    message="Synced cached clone for https://github.com/example/project.",
                )
                with patch("repopilot_agent.web_server.sync_repository_reference", return_value=source) as sync_mock:
                    payload = json.dumps(
                        {
                            "repo_source": "github",
                            "github_url": "https://github.com/example/project",
                            "branch": "feature",
                        }
                    ).encode("utf-8")
                    request = Request(
                        f"http://127.0.0.1:{server.server_port}/api/repository/sync",
                        data=payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )

                    with urlopen(request, timeout=5) as response:
                        data = json.loads(response.read().decode("utf-8"))

                sync_mock.assert_called_once()
                self.assertEqual(sync_mock.call_args.kwargs["branch"], "feature")
                self.assertEqual(data["repository_source"]["branch"], "feature")
                self.assertTrue(data["repository_source"]["synced"])
                self.assertEqual(data["repository_source"]["latest_commit"], "abc123 Sync")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_apply_api_writes_session_file_edits_and_runs_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            (root / "notes.txt").write_text("old\n", encoding="utf-8")
            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch(
                    "repopilot_agent.web_server.OpenAICompatibleClient",
                    return_value=FakeLLMClient(
                        [
                            '{"steps":[{"title":"Inspect notes","detail":"Review notes.txt."}]}',
                            '{"objective":"Update notes","files":[{"path":"notes.txt","change_type":"refinement",'
                            '"rationale":"notes.txt is the target.","suggested_actions":["Replace old with new"],'
                            '"confidence":"high"}],"risks":[],"validation_suggestions":["python -m unittest discover -s tests"],'
                            '"ready_for_patch":true,"file_edits":[{"path":"notes.txt","new_content":"new\\n",'
                            '"rationale":"Approved replacement."}]}',
                            '{"summary":"The diff is focused.","risk_level":"low","concerns":[],'
                            '"suggested_tests":["python -m unittest discover -s tests"],"approved_for_apply":true}',
                        ]
                    ),
                ):
                    propose_payload = json.dumps(
                        {
                            "repo": str(root),
                            "task": "update notes",
                            "validation": ["python -m unittest discover -s tests"],
                            "use_llm": True,
                            "api_key": "test-key",
                        }
                    ).encode("utf-8")
                    propose_request = Request(
                        f"http://127.0.0.1:{server.server_port}/api/propose",
                        data=propose_payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )

                    with urlopen(propose_request, timeout=5) as response:
                        proposal = json.loads(response.read().decode("utf-8"))

                self.assertIsNotNone(proposal["proposal_id"])
                stored_session = MemoryStore(default_memory_path(root)).get_proposal_session(proposal["proposal_id"])
                self.assertIsNotNone(stored_session)
                self.assertEqual(stored_session["file_edits"][0]["path"], "notes.txt")
                apply_payload = json.dumps({"proposal_id": proposal["proposal_id"]}).encode("utf-8")
                apply_request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/apply",
                    data=apply_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )

                with urlopen(apply_request, timeout=5) as response:
                    data = json.loads(response.read().decode("utf-8"))

                self.assertTrue(data["applied"])
                self.assertEqual(data["changed_files"], ["notes.txt"])
                self.assertEqual(data["validation"][0]["command"], "python -m unittest discover -s tests")
                self.assertTrue(data["rollback_available"])
                self.assertTrue(any(event["step"] == "apply" for event in data["timeline"]))
                self.assertTrue(any(event["step"] == "rollback" and event["status"] == "ready" for event in data["timeline"]))
                self.assertEqual((root / "notes.txt").read_text(encoding="utf-8"), "new\n")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_revert_api_restores_applied_proposal_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            (root / "notes.txt").write_text("old\n", encoding="utf-8")
            session = create_proposal_session(
                repo_path=str(root),
                task="update notes",
                file_edits=[
                    FileEditProposal(path="notes.txt", new_content="new\n", rationale="Update notes.")
                ],
                validation_commands=[],
                timeline=[],
                allowed_paths=["notes.txt"],
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                apply_payload = json.dumps({"proposal_id": session.proposal_id}).encode("utf-8")
                apply_request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/apply",
                    data=apply_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(apply_request, timeout=5) as response:
                    applied = json.loads(response.read().decode("utf-8"))

                revert_payload = json.dumps({"proposal_id": session.proposal_id}).encode("utf-8")
                revert_request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/revert",
                    data=revert_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(revert_request, timeout=5) as response:
                    reverted = json.loads(response.read().decode("utf-8"))

                self.assertTrue(applied["rollback_available"])
                self.assertTrue(reverted["reverted"])
                self.assertFalse(reverted["rollback_available"])
                self.assertEqual(reverted["restored_files"], ["notes.txt"])
                self.assertEqual((root / "notes.txt").read_text(encoding="utf-8"), "old\n")
                self.assertTrue(any(event["step"] == "rollback" and event["status"] == "done" for event in reverted["timeline"]))
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_apply_and_revert_restore_persisted_session_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("old\n", encoding="utf-8")
            session = create_proposal_session(
                repo_path=str(root),
                task="update notes",
                file_edits=[
                    FileEditProposal(path="notes.txt", new_content="new\n", rationale="Update notes.")
                ],
                validation_commands=[],
                timeline=[],
                allowed_paths=["notes.txt"],
            )
            MemoryStore(default_memory_path(root)).save_proposal_session(proposal_session_to_record(session))
            clear_proposal_sessions()
            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                apply_payload = json.dumps(
                    {
                        "repo": str(root),
                        "repo_source": "local",
                        "proposal_id": session.proposal_id,
                        "approved_paths": ["notes.txt"],
                    }
                ).encode("utf-8")
                apply_request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/apply",
                    data=apply_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(apply_request, timeout=5) as response:
                    applied = json.loads(response.read().decode("utf-8"))

                self.assertTrue(applied["applied"])
                self.assertTrue(applied["rollback_available"])
                self.assertEqual((root / "notes.txt").read_text(encoding="utf-8"), "new\n")

                clear_proposal_sessions()
                revert_payload = json.dumps(
                    {
                        "repo": str(root),
                        "repo_source": "local",
                        "proposal_id": session.proposal_id,
                    }
                ).encode("utf-8")
                revert_request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/revert",
                    data=revert_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(revert_request, timeout=5) as response:
                    reverted = json.loads(response.read().decode("utf-8"))

                self.assertTrue(reverted["reverted"])
                self.assertFalse(reverted["rollback_available"])
                self.assertEqual(reverted["restored_files"], ["notes.txt"])
                self.assertEqual((root / "notes.txt").read_text(encoding="utf-8"), "old\n")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
                clear_proposal_sessions()

    def test_apply_api_writes_only_approved_file_edits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "first.txt").write_text("old first\n", encoding="utf-8")
            (root / "second.txt").write_text("old second\n", encoding="utf-8")
            session = create_proposal_session(
                repo_path=str(root),
                task="update first file",
                file_edits=[
                    FileEditProposal(path="first.txt", new_content="new first\n", rationale="Update first."),
                    FileEditProposal(path="second.txt", new_content="new second\n", rationale="Update second."),
                ],
                validation_commands=[],
                timeline=[],
                allowed_paths=["first.txt", "second.txt"],
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                apply_payload = json.dumps(
                    {"proposal_id": session.proposal_id, "approved_paths": ["first.txt"]}
                ).encode("utf-8")
                apply_request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/apply",
                    data=apply_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(apply_request, timeout=5) as response:
                    applied = json.loads(response.read().decode("utf-8"))

                self.assertTrue(applied["applied"])
                self.assertEqual(applied["changed_files"], ["first.txt"])
                self.assertEqual(applied["approved_paths"], ["first.txt"])
                self.assertEqual(applied["applied_paths"], ["first.txt"])
                self.assertEqual((root / "first.txt").read_text(encoding="utf-8"), "new first\n")
                self.assertEqual((root / "second.txt").read_text(encoding="utf-8"), "old second\n")

                revert_payload = json.dumps({"proposal_id": session.proposal_id}).encode("utf-8")
                revert_request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/revert",
                    data=revert_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(revert_request, timeout=5) as response:
                    reverted = json.loads(response.read().decode("utf-8"))

                self.assertTrue(reverted["reverted"])
                self.assertEqual(reverted["restored_files"], ["first.txt"])
                self.assertEqual((root / "first.txt").read_text(encoding="utf-8"), "old first\n")
                self.assertEqual((root / "second.txt").read_text(encoding="utf-8"), "old second\n")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_apply_api_rejects_approved_paths_outside_session_edits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("old\n", encoding="utf-8")
            session = create_proposal_session(
                repo_path=str(root),
                task="update notes",
                file_edits=[
                    FileEditProposal(path="notes.txt", new_content="new\n", rationale="Update notes.")
                ],
                validation_commands=[],
                timeline=[],
                allowed_paths=["notes.txt"],
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                apply_payload = json.dumps(
                    {"proposal_id": session.proposal_id, "approved_paths": ["missing.txt"]}
                ).encode("utf-8")
                apply_request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/apply",
                    data=apply_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )

                with self.assertRaises(HTTPError) as raised:
                    urlopen(apply_request, timeout=5)
                data = json.loads(raised.exception.read().decode("utf-8"))

                self.assertIn("not in this proposal", data["error"])
                self.assertEqual((root / "notes.txt").read_text(encoding="utf-8"), "old\n")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_apply_api_rejects_empty_approved_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("old\n", encoding="utf-8")
            session = create_proposal_session(
                repo_path=str(root),
                task="update notes",
                file_edits=[
                    FileEditProposal(path="notes.txt", new_content="new\n", rationale="Update notes.")
                ],
                validation_commands=[],
                timeline=[],
                allowed_paths=["notes.txt"],
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                apply_payload = json.dumps(
                    {"proposal_id": session.proposal_id, "approved_paths": []}
                ).encode("utf-8")
                apply_request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/apply",
                    data=apply_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )

                with self.assertRaises(HTTPError) as raised:
                    urlopen(apply_request, timeout=5)
                data = json.loads(raised.exception.read().decode("utf-8"))

                self.assertIn("select at least one", data["error"])
                self.assertEqual((root / "notes.txt").read_text(encoding="utf-8"), "old\n")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_apply_api_rejects_blocked_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch(
                    "repopilot_agent.web_server.OpenAICompatibleClient",
                    return_value=FakeLLMClient(
                        [
                            '{"steps":[{"title":"Inspect log","detail":"Review log.md."}]}',
                            '{"objective":"Update log","files":[{"path":"log.md","change_type":"documentation",'
                            '"rationale":"log.md is the target.","suggested_actions":["Replace content"],'
                            '"confidence":"high"}],"risks":[],"validation_suggestions":[],"ready_for_patch":true,'
                            '"file_edits":[{"path":"log.md","new_content":"hidden\\n","rationale":"Should be blocked."}]}',
                            '{"summary":"The diff touches a blocked file.","risk_level":"high","concerns":["log.md should not be edited"],'
                            '"suggested_tests":[],"approved_for_apply":false}',
                        ]
                    ),
                ):
                    propose_payload = json.dumps(
                        {
                            "repo": str(root),
                            "task": "update log",
                            "use_llm": True,
                            "api_key": "test-key",
                        }
                    ).encode("utf-8")
                    propose_request = Request(
                        f"http://127.0.0.1:{server.server_port}/api/propose",
                        data=propose_payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )

                    with urlopen(propose_request, timeout=5) as response:
                        proposal = json.loads(response.read().decode("utf-8"))

                apply_payload = json.dumps({"proposal_id": proposal["proposal_id"]}).encode("utf-8")
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/apply",
                    data=apply_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )

                with self.assertRaises(Exception):
                    urlopen(request, timeout=5)
                self.assertFalse((root / "log.md").exists())
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_apply_api_returns_safety_findings_for_unapproved_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "other.txt").write_text("old\n", encoding="utf-8")
            session = create_proposal_session(
                repo_path=str(root),
                task="update approved file",
                file_edits=[
                    FileEditProposal(path="other.txt", new_content="new\n", rationale="Update other.")
                ],
                validation_commands=[],
                timeline=[],
                allowed_paths=["approved.txt"],
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                apply_payload = json.dumps({"proposal_id": session.proposal_id}).encode("utf-8")
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/apply",
                    data=apply_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )

                with self.assertRaises(HTTPError) as raised:
                    urlopen(request, timeout=5)
                data = json.loads(raised.exception.read().decode("utf-8"))

                self.assertFalse(data["safety_check"]["ok"])
                self.assertTrue(
                    any(finding["code"] == "path_not_in_proposal" for finding in data["safety_check"]["findings"])
                )
                self.assertEqual((root / "other.txt").read_text(encoding="utf-8"), "old\n")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_apply_api_runs_recommended_validation_when_user_omits_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "tests").mkdir()
            (root / "src" / "auth.py").write_text("def login():\n    return False\n", encoding="utf-8")
            (root / "tests" / "test_auth.py").write_text(
                "import unittest\n\n"
                "class AuthTests(unittest.TestCase):\n"
                "    def test_placeholder(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch(
                    "repopilot_agent.web_server.OpenAICompatibleClient",
                    return_value=FakeLLMClient(
                        [
                            '{"steps":[{"title":"Inspect auth","detail":"Review src/auth.py."}]}',
                            '{"objective":"Fix login","files":[{"path":"src/auth.py","change_type":"bugfix",'
                            '"rationale":"src/auth.py contains login behavior.","suggested_actions":["Return true"],'
                            '"confidence":"high"}],"risks":[],"validation_suggestions":[],"ready_for_patch":true,'
                            '"file_edits":[{"path":"src/auth.py","new_content":"def login():\\n    return True\\n",'
                            '"rationale":"Fix login behavior."}]}',
                            '{"summary":"The diff is focused.","risk_level":"low","concerns":[],"suggested_tests":[],"approved_for_apply":true}',
                        ]
                    ),
                ):
                    propose_payload = json.dumps(
                        {
                            "repo": str(root),
                            "task": "fix auth login behavior",
                            "use_llm": True,
                            "api_key": "test-key",
                        }
                    ).encode("utf-8")
                    propose_request = Request(
                        f"http://127.0.0.1:{server.server_port}/api/propose",
                        data=propose_payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )

                    with urlopen(propose_request, timeout=5) as response:
                        proposal = json.loads(response.read().decode("utf-8"))

                self.assertEqual(
                    proposal["patch_proposal"]["validation_plan"]["commands"],
                    ["python -m unittest tests.test_auth"],
                )
                apply_payload = json.dumps({"proposal_id": proposal["proposal_id"]}).encode("utf-8")
                apply_request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/apply",
                    data=apply_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )

                with urlopen(apply_request, timeout=10) as response:
                    data = json.loads(response.read().decode("utf-8"))

                self.assertTrue(data["applied"])
                self.assertEqual(data["validation"][0]["command"], "python -m unittest tests.test_auth")
                self.assertEqual(data["validation"][0]["exit_code"], 0)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_repair_proposal_api_uses_validation_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            (root / "notes.txt").write_text("old\n", encoding="utf-8")
            (root / "test_fail.py").write_text(
                "import unittest\n\n"
                "class RepairTests(unittest.TestCase):\n"
                "    def test_failure(self):\n"
                "        self.assertTrue(False)\n",
                encoding="utf-8",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch(
                    "repopilot_agent.web_server.OpenAICompatibleClient",
                    return_value=FakeLLMClient(
                        [
                            '{"steps":[{"title":"Inspect notes","detail":"Review notes.txt."}]}',
                            '{"objective":"Update notes","files":[{"path":"notes.txt","change_type":"refinement",'
                            '"rationale":"notes.txt is the target.","suggested_actions":["Replace old with new"],'
                            '"confidence":"high"}],"risks":[],"validation_suggestions":["python -m unittest test_fail"],'
                            '"ready_for_patch":true,"file_edits":[{"path":"notes.txt","new_content":"new\\n",'
                            '"rationale":"Approved replacement."}]}',
                            '{"summary":"The diff is focused.","risk_level":"low","concerns":[],"suggested_tests":["python -m unittest test_fail"],"approved_for_apply":true}',
                            '{"steps":[{"title":"Inspect failing test","detail":"Review test_fail.py."}]}',
                            '{"objective":"Repair failing validation","files":[{"path":"test_fail.py","change_type":"test",'
                            '"rationale":"test_fail.py is the failing validation point.","suggested_actions":["Make placeholder pass"],'
                            '"confidence":"high"}],"risks":[],"validation_suggestions":["python -m unittest test_fail"],'
                            '"ready_for_patch":true,"file_edits":[{"path":"test_fail.py",'
                            '"new_content":"import unittest\\n\\nclass RepairTests(unittest.TestCase):\\n    def test_failure(self):\\n        self.assertTrue(True)\\n",'
                            '"rationale":"Repair the failing validation."}]}',
                            '{"summary":"The repair diff targets the failing validation.","risk_level":"low","concerns":[],"suggested_tests":["python -m unittest test_fail"],"approved_for_apply":true}',
                        ]
                    ),
                ):
                    propose_payload = json.dumps(
                        {
                            "repo": str(root),
                            "task": "update notes",
                            "validation": ["python -m unittest test_fail"],
                            "max_repair_attempts": "2",
                            "use_llm": True,
                            "api_key": "test-key",
                        }
                    ).encode("utf-8")
                    propose_request = Request(
                        f"http://127.0.0.1:{server.server_port}/api/propose",
                        data=propose_payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urlopen(propose_request, timeout=5) as response:
                        proposal = json.loads(response.read().decode("utf-8"))

                    apply_payload = json.dumps({"proposal_id": proposal["proposal_id"]}).encode("utf-8")
                    apply_request = Request(
                        f"http://127.0.0.1:{server.server_port}/api/apply",
                        data=apply_payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urlopen(apply_request, timeout=10) as response:
                        applied = json.loads(response.read().decode("utf-8"))

                    repair_payload = json.dumps(
                        {
                            "proposal_id": proposal["proposal_id"],
                            "use_llm": True,
                            "api_key": "test-key",
                        }
                    ).encode("utf-8")
                    repair_request = Request(
                        f"http://127.0.0.1:{server.server_port}/api/repair/propose",
                        data=repair_payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urlopen(repair_request, timeout=10) as response:
                        repair = json.loads(response.read().decode("utf-8"))

                self.assertIsNotNone(applied["validation_feedback"])
                self.assertIn("test_fail.py", applied["validation_feedback"]["suspected_files"])
                self.assertEqual(applied["repair_attempt"], 0)
                self.assertEqual(applied["max_repair_attempts"], 2)
                self.assertEqual(applied["next_repair_attempt"], 1)
                self.assertFalse(applied["repair_budget_exhausted"])
                self.assertEqual(repair["parent_proposal_id"], proposal["proposal_id"])
                self.assertIsNotNone(repair["proposal_id"])
                self.assertEqual(repair["repair_attempt"], 1)
                self.assertEqual(repair["max_repair_attempts"], 2)
                self.assertEqual(repair["repair_budget_remaining"], 1)
                self.assertIn("Repair the repository", repair["repair_task"])
                self.assertIn("Repair attempt: 1/2", repair["repair_task"])
                self.assertEqual(repair["patch_proposal"]["files"][0]["path"], "test_fail.py")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_repair_proposal_api_rejects_exhausted_retry_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = create_proposal_session(
                repo_path=str(root),
                task="repair failed tests",
                file_edits=[
                    FileEditProposal(path="notes.txt", new_content="new\n", rationale="Update notes.")
                ],
                validation_commands=["python -m unittest test_fail"],
                timeline=[],
                repair_attempt=1,
                max_repair_attempts=1,
            )
            session.validation_feedback = ValidationFeedback(
                summary="Validation failed.",
                failures=[],
                suspected_files=["test_fail.py"],
                repair_steps=["Fix the failed assertion."],
                repair_task="Repair the repository after validation failed.",
                source="rules",
            )

            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                payload = json.dumps({"proposal_id": session.proposal_id}).encode("utf-8")
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/repair/propose",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with patch("repopilot_agent.web_server.run_workflow") as workflow:
                    with self.assertRaises(HTTPError) as raised:
                        urlopen(request, timeout=5)
                    body = json.loads(raised.exception.read().decode("utf-8"))

                self.assertEqual(raised.exception.code, 400)
                self.assertIn("budget exhausted", body["error"])
                self.assertEqual(body["repair_attempt"], 1)
                self.assertEqual(body["max_repair_attempts"], 1)
                self.assertEqual(body["repair_budget_remaining"], 0)
                self.assertTrue(body["repair_budget_exhausted"])
                workflow.assert_not_called()
            finally:
                clear_proposal_sessions()
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_git_summary_api_returns_delivery_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "tester@example.local"], cwd=root, check=True)
            (root / "README.md").write_text("first\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "Initial"], cwd=root, check=True, capture_output=True, text=True)
            (root / "README.md").write_text("first\nsecond\n", encoding="utf-8")

            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                payload = json.dumps(
                    {
                        "repo": str(root),
                        "validation_notes": ["python -m unittest discover -s tests: exit 0"],
                    }
                ).encode("utf-8")
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/git/summary",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )

                with urlopen(request, timeout=5) as response:
                    data = json.loads(response.read().decode("utf-8"))

                self.assertEqual(data["suggested_commit_message"], "Update project documentation")
                self.assertIn("README.md", data["change_summary"][-1])
                self.assertIn("## What changed", data["pull_request"]["body"])
                self.assertEqual(data["validation_notes"], ["python -m unittest discover -s tests: exit 0"])
                self.assertIn("pr_readiness", data)
                self.assertFalse(data["pr_readiness"]["ready"])
                self.assertTrue(data["pr_readiness"]["needs_commit"])
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_pr_readiness_api_returns_blockers_without_creating_pr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            (root / "README.md").write_text("# Demo\n", encoding="utf-8")

            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                payload = json.dumps({"repo": str(root), "title": "Update docs"}).encode("utf-8")
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/github/pr/readiness",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )

                with urlopen(request, timeout=5) as response:
                    data = json.loads(response.read().decode("utf-8"))

                readiness = data["pr_readiness"]
                self.assertFalse(readiness["ready"])
                self.assertTrue(any("No GitHub remote" in item for item in readiness["blockers"]))
                self.assertTrue(any("uncommitted changes" in item for item in readiness["blockers"]))
                self.assertIn("repository_source", data)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_pr_create_api_requires_ready_branch_and_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare_ready_pr_repository(root)

            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                payload = json.dumps(
                    {
                        "repo": str(root),
                        "confirm_create": True,
                        "title": "Add PR readiness",
                        "body": "## What changed\n- Added PR readiness.",
                    }
                ).encode("utf-8")
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/github/pr/create",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )

                with patch(
                    "repopilot_agent.web_server.create_github_pull_request",
                    return_value={
                        "number": 12,
                        "title": "Add PR readiness",
                        "state": "open",
                        "html_url": "https://github.com/example/project/pull/12",
                        "base": "main",
                        "head": "feature/pr-ready",
                    },
                ) as create_pr:
                    with urlopen(request, timeout=5) as response:
                        data = json.loads(response.read().decode("utf-8"))

                self.assertTrue(data["created"])
                self.assertEqual(data["pull_request"]["number"], 12)
                self.assertTrue(data["pr_readiness"]["ready"])
                create_pr.assert_called_once()
                _, kwargs = create_pr.call_args
                self.assertEqual(kwargs["base_branch"], "main")
                self.assertEqual(kwargs["head_branch"], "feature/pr-ready")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_history_api_returns_saved_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            (root / "main.py").write_text("def login():\n    return True\n", encoding="utf-8")
            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                payload = json.dumps({"repo": str(root), "task": "fix login behavior"}).encode("utf-8")
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/run",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=5) as response:
                    run_data = json.loads(response.read().decode("utf-8"))

                history_url = f"http://127.0.0.1:{server.server_port}/api/history?repo={root}&limit=5"
                with urlopen(history_url, timeout=5) as response:
                    history = json.loads(response.read().decode("utf-8"))

                run_id = run_data["run_id"]
                detail_url = f"http://127.0.0.1:{server.server_port}/api/history/run?repo={root}&id={run_id}"
                with urlopen(detail_url, timeout=5) as response:
                    detail = json.loads(response.read().decode("utf-8"))

                self.assertEqual(history["runs"][0]["id"], run_id)
                self.assertEqual(detail["task"], "fix login behavior")
                self.assertTrue(detail["timeline"])
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_history_pin_api_updates_saved_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = MemoryStore(default_memory_path(root))
            report = WorkflowReport(
                task="fix parser behavior",
                repo_path=tmp,
                files_scanned=1,
                plan_metadata=PlanMetadata(source="rules"),
                summary="RepoPilot analyzed parser behavior.",
            )
            run_id = store.create_run(tmp, "fix parser behavior", "run", report)
            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                pin_payload = json.dumps({"repo": str(root), "id": run_id, "pinned": True}).encode("utf-8")
                pin_request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/history/pin",
                    data=pin_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(pin_request, timeout=5) as response:
                    pinned = json.loads(response.read().decode("utf-8"))

                detail_url = f"http://127.0.0.1:{server.server_port}/api/history/run?repo={root}&id={run_id}"
                with urlopen(detail_url, timeout=5) as response:
                    detail = json.loads(response.read().decode("utf-8"))

                unpin_payload = json.dumps({"repo": str(root), "id": run_id, "pinned": False}).encode("utf-8")
                unpin_request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/history/pin",
                    data=unpin_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(unpin_request, timeout=5) as response:
                    unpinned = json.loads(response.read().decode("utf-8"))

                self.assertTrue(pinned["pinned"])
                self.assertTrue(detail["pinned"])
                self.assertFalse(unpinned["pinned"])
                self.assertFalse(store.get_run(run_id)["pinned"])
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_history_delete_and_clear_api_manage_saved_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = MemoryStore(default_memory_path(root))
            report = WorkflowReport(
                task="fix parser behavior",
                repo_path=tmp,
                files_scanned=1,
                plan_metadata=PlanMetadata(source="rules"),
                summary="RepoPilot analyzed parser behavior.",
            )
            first_id = store.create_run(tmp, "fix parser behavior", "run", report)
            second_id = store.create_run(tmp, "fix parser validation", "run", report)
            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                delete_payload = json.dumps({"repo": str(root), "id": first_id}).encode("utf-8")
                delete_request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/history/delete",
                    data=delete_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(delete_request, timeout=5) as response:
                    deleted = json.loads(response.read().decode("utf-8"))

                history_url = f"http://127.0.0.1:{server.server_port}/api/history?repo={root}&limit=5"
                with urlopen(history_url, timeout=5) as response:
                    history = json.loads(response.read().decode("utf-8"))

                clear_payload = json.dumps({"repo": str(root)}).encode("utf-8")
                clear_request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/history/clear",
                    data=clear_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(clear_request, timeout=5) as response:
                    cleared = json.loads(response.read().decode("utf-8"))

                self.assertTrue(deleted["deleted"])
                self.assertNotIn(first_id, [run["id"] for run in history["runs"]])
                self.assertIn(second_id, [run["id"] for run in history["runs"]])
                self.assertEqual(cleared["deleted"], 1)
                self.assertEqual(store.list_runs(), [])
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
