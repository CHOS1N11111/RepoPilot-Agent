"""SQLite-backed local memory for web workflow runs."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import MemoryContextItem


_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "into",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "with",
}


def default_memory_path(repo_path: str | Path) -> Path:
    root = Path(repo_path).expanduser().resolve()
    return root / ".repopilot" / "memory.sqlite3"


def ensure_local_state_ignored(repo_path: str | Path) -> None:
    """Keep RepoPilot state local without editing the repository's tracked ignore files."""
    root = Path(repo_path).expanduser().resolve()
    if not root.is_dir():
        return
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-path", "info/exclude"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return
    if result.returncode != 0 or not result.stdout.strip():
        return
    exclude_path = Path(result.stdout.strip())
    if not exclude_path.is_absolute():
        exclude_path = (root / exclude_path).resolve()
    try:
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        patterns = {line.strip() for line in existing.splitlines()}
        if ".repopilot/" in patterns:
            return
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        separator = "" if not existing or existing.endswith("\n") else "\n"
        exclude_path.write_text(f"{existing}{separator}.repopilot/\n", encoding="utf-8")
    except OSError:
        return


class MemoryStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        if self.db_path.parent.name == ".repopilot":
            ensure_local_state_ignored(self.db_path.parent.parent)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create_run(
        self,
        repo_path: str,
        task: str,
        mode: str,
        report: Any,
        proposal_id: str | None = None,
        timeline: list[dict[str, Any]] | None = None,
    ) -> str:
        run_id = uuid4().hex
        proposal = report.patch_proposal
        review = report.patch_review
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    id, repo_path, task, mode, created_at, summary, proposal_id,
                    plan_source, proposal_source, review_source, applied, pinned, timeline_json,
                    agent_runtime_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    repo_path,
                    task,
                    mode,
                    _now(),
                    report.summary,
                    proposal_id,
                    report.plan_metadata.source,
                    report.patch_proposal_metadata.source,
                    review.source if review else None,
                    0,
                    0,
                    _json(timeline or []),
                    getattr(report, "agent_run_id", None),
                ),
            )
            if proposal:
                conn.execute(
                    """
                    INSERT INTO proposals (
                        id, run_id, objective, proposed_diff, apply_ready, file_edits_json,
                        metadata_json, review_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        proposal_id or uuid4().hex,
                        run_id,
                        proposal.objective,
                        proposal.proposed_diff,
                        int(proposal.apply_ready),
                        _json([asdict(edit) for edit in proposal.file_edits]),
                        _json(asdict(report.patch_proposal_metadata)),
                        _json(asdict(review) if review else None),
                    ),
                )
            for trace in report.llm_traces:
                conn.execute(
                    """
                    INSERT INTO llm_traces (
                        id, run_id, name, model, prompt_preview, raw_output,
                        parsed, fallback_used, error, latency_ms, context_summary
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uuid4().hex,
                        run_id,
                        trace.name,
                        trace.model,
                        trace.prompt_preview,
                        trace.raw_output,
                        int(trace.parsed),
                        int(trace.fallback_used),
                        trace.error,
                        trace.latency_ms,
                        trace.context_summary,
                    ),
                )
            for result in report.validation:
                conn.execute(
                    """
                    INSERT INTO validation_results (
                        id, run_id, command, allowed, exit_code, stdout, stderr
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uuid4().hex,
                        run_id,
                        result.command,
                        int(result.allowed),
                        result.exit_code,
                        result.stdout,
                        result.stderr,
                    ),
                )
        return run_id

    def mark_proposal_applied(
        self,
        proposal_id: str,
        validation: list[Any],
        timeline: list[dict[str, Any]],
    ) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT run_id FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
            if row is None:
                return
            run_id = str(row["run_id"])
            conn.execute("UPDATE runs SET applied = 1, timeline_json = ? WHERE id = ?", (_json(timeline), run_id))
            for result in validation:
                conn.execute(
                    """
                    INSERT INTO validation_results (
                        id, run_id, command, allowed, exit_code, stdout, stderr
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uuid4().hex,
                        run_id,
                        result.command,
                        int(result.allowed),
                        result.exit_code,
                        result.stdout,
                        result.stderr,
                    ),
                )

    def mark_proposal_reverted(
        self,
        proposal_id: str,
        timeline: list[dict[str, Any]],
    ) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT run_id FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
            if row is None:
                return
            run_id = str(row["run_id"])
            conn.execute("UPDATE runs SET applied = 0, timeline_json = ? WHERE id = ?", (_json(timeline), run_id))

    def save_proposal_session(self, session: dict[str, Any]) -> None:
        proposal_id = str(session.get("proposal_id") or "").strip()
        repo_path = str(session.get("repo_path") or "").strip()
        if not proposal_id:
            raise ValueError("proposal_id is required to save a proposal session.")
        if not repo_path:
            raise ValueError("repo_path is required to save a proposal session.")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO proposal_sessions (id, repo_path, updated_at, data_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    repo_path = excluded.repo_path,
                    updated_at = excluded.updated_at,
                    data_json = excluded.data_json
                """,
                (proposal_id, repo_path, _now(), _json(session)),
            )

    def get_proposal_session(self, proposal_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data_json FROM proposal_sessions WHERE id = ?",
                (proposal_id,),
            ).fetchone()
        if row is None:
            return None
        return _loads(row["data_json"], None)

    def save_task_run(self, task_run: dict[str, Any]) -> None:
        run_id = str(task_run.get("run_id") or "").strip()
        source_repo = str(task_run.get("source_repo") or "").strip()
        status = str(task_run.get("status") or "").strip()
        if not run_id:
            raise ValueError("run_id is required to save a task run.")
        if not source_repo:
            raise ValueError("source_repo is required to save a task run.")
        if not status:
            raise ValueError("status is required to save a task run.")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_runs (id, source_repo, status, updated_at, data_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source_repo = excluded.source_repo,
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    data_json = excluded.data_json
                """,
                (run_id, source_repo, status, _now(), _json(task_run)),
            )

    def get_task_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data_json FROM task_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return _loads(row["data_json"], None)

    def list_task_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT data_json
                FROM task_runs
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(min(limit, 100), 1),),
            ).fetchall()
        return [_loads(row["data_json"], {}) for row in rows]

    def reserve_agent_runtime_action(
        self,
        runtime_run_id: str,
        idempotency_key: str,
        action: dict[str, Any],
    ) -> dict[str, Any]:
        if not runtime_run_id.strip() or not idempotency_key.strip():
            raise ValueError("Runtime run id and idempotency key are required.")
        signature = _runtime_action_signature(action)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT state, action_json, observation_json
                FROM agent_runtime_actions
                WHERE runtime_run_id = ? AND idempotency_key = ?
                """,
                (runtime_run_id, idempotency_key),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO agent_runtime_actions (
                        runtime_run_id, idempotency_key, action_id, state,
                        action_json, observation_json, updated_at
                    ) VALUES (?, ?, ?, 'in_progress', ?, NULL, ?)
                    """,
                    (
                        runtime_run_id,
                        idempotency_key,
                        str(action.get("action_id") or ""),
                        _json(action),
                        _now(),
                    ),
                )
                return {"status": "new", "observation": None}
            if _runtime_action_signature(_loads(row["action_json"], {})) != signature:
                return {"status": "conflict", "observation": None}
            observation = _loads(row["observation_json"], None)
            if row["state"] == "completed" and isinstance(observation, dict):
                return {"status": "completed", "observation": observation}
            return {"status": "in_progress", "observation": None}

    def complete_agent_runtime_action(
        self,
        runtime_run_id: str,
        idempotency_key: str,
        action: dict[str, Any],
        observation: dict[str, Any],
    ) -> dict[str, Any]:
        signature = _runtime_action_signature(action)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT state, action_json, observation_json
                FROM agent_runtime_actions
                WHERE runtime_run_id = ? AND idempotency_key = ?
                """,
                (runtime_run_id, idempotency_key),
            ).fetchone()
            if row is not None and _runtime_action_signature(_loads(row["action_json"], {})) != signature:
                raise ValueError("Idempotency key is already reserved for a different action.")
            existing = _loads(row["observation_json"], None) if row is not None else None
            if row is not None and row["state"] == "completed" and isinstance(existing, dict):
                return existing
            if row is None:
                conn.execute(
                    """
                    INSERT INTO agent_runtime_actions (
                        runtime_run_id, idempotency_key, action_id, state,
                        action_json, observation_json, updated_at
                    ) VALUES (?, ?, ?, 'completed', ?, ?, ?)
                    """,
                    (
                        runtime_run_id,
                        idempotency_key,
                        str(action.get("action_id") or ""),
                        _json(action),
                        _json(observation),
                        _now(),
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE agent_runtime_actions
                    SET state = 'completed', observation_json = ?, updated_at = ?
                    WHERE runtime_run_id = ? AND idempotency_key = ?
                    """,
                    (_json(observation), _now(), runtime_run_id, idempotency_key),
                )
        return observation

    def append_agent_runtime_event(
        self,
        runtime_run_id: str,
        event_type: str,
        *,
        action_id: str | None = None,
        idempotency_key: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event_id = uuid4().hex
        created_at = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM agent_runtime_events WHERE runtime_run_id = ?",
                (runtime_run_id,),
            ).fetchone()
            sequence = int(row["sequence"] if row else 0) + 1
            conn.execute(
                """
                INSERT INTO agent_runtime_events (
                    id, runtime_run_id, sequence, event_type, action_id,
                    idempotency_key, created_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    runtime_run_id,
                    sequence,
                    event_type,
                    action_id,
                    idempotency_key,
                    created_at,
                    _json(payload or {}),
                ),
            )
        return {
            "event_id": event_id,
            "run_id": runtime_run_id,
            "sequence": sequence,
            "event_type": event_type,
            "created_at": created_at,
            "action_id": action_id,
            "idempotency_key": idempotency_key,
            "payload": payload or {},
        }

    def list_agent_runtime_events(self, runtime_run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, runtime_run_id, sequence, event_type, action_id,
                       idempotency_key, created_at, payload_json
                FROM agent_runtime_events
                WHERE runtime_run_id = ?
                ORDER BY sequence
                """,
                (runtime_run_id,),
            ).fetchall()
        return [
            {
                "event_id": row["id"],
                "run_id": row["runtime_run_id"],
                "sequence": row["sequence"],
                "event_type": row["event_type"],
                "created_at": row["created_at"],
                "action_id": row["action_id"],
                "idempotency_key": row["idempotency_key"],
                "payload": _loads(row["payload_json"], {}),
            }
            for row in rows
        ]

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, repo_path, task, mode, created_at, summary, proposal_id,
                       plan_source, proposal_source, review_source, applied, pinned, timeline_json,
                       agent_runtime_run_id
                FROM runs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_row_to_run(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            run_row = conn.execute(
                """
                SELECT id, repo_path, task, mode, created_at, summary, proposal_id,
                       plan_source, proposal_source, review_source, applied, pinned, timeline_json,
                       agent_runtime_run_id
                FROM runs
                WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
            if run_row is None:
                return None
            proposal = conn.execute("SELECT * FROM proposals WHERE run_id = ?", (run_id,)).fetchone()
            proposal_session = None
            if proposal:
                proposal_session = conn.execute(
                    "SELECT data_json FROM proposal_sessions WHERE id = ?",
                    (proposal["id"],),
                ).fetchone()
            traces = conn.execute("SELECT * FROM llm_traces WHERE run_id = ? ORDER BY rowid", (run_id,)).fetchall()
            validation = conn.execute(
                "SELECT * FROM validation_results WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            ).fetchall()
            runtime_events = []
            if run_row["agent_runtime_run_id"]:
                runtime_events = conn.execute(
                    """
                    SELECT id, runtime_run_id, sequence, event_type, action_id,
                           idempotency_key, created_at, payload_json
                    FROM agent_runtime_events
                    WHERE runtime_run_id = ?
                    ORDER BY sequence
                    """,
                    (run_row["agent_runtime_run_id"],),
                ).fetchall()
        data = _row_to_run(run_row)
        data["proposal"] = _row_to_proposal(proposal) if proposal else None
        data["proposal_session"] = _loads(proposal_session["data_json"], None) if proposal_session else None
        data["llm_traces"] = [_row_to_trace(row) for row in traces]
        data["validation"] = [_row_to_validation(row) for row in validation]
        data["agent_events"] = [_row_to_agent_runtime_event(row) for row in runtime_events]
        return data

    def delete_run(self, run_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, agent_runtime_run_id FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return False
            runtime_run_id = row["agent_runtime_run_id"]
            conn.execute("DELETE FROM validation_results WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM llm_traces WHERE run_id = ?", (run_id,))
            proposal_rows = conn.execute("SELECT id FROM proposals WHERE run_id = ?", (run_id,)).fetchall()
            for proposal_row in proposal_rows:
                conn.execute("DELETE FROM proposal_sessions WHERE id = ?", (proposal_row["id"],))
            conn.execute("DELETE FROM proposals WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
            if runtime_run_id:
                still_referenced = conn.execute(
                    """
                    SELECT 1 FROM runs WHERE agent_runtime_run_id = ?
                    UNION ALL
                    SELECT 1 FROM task_runs WHERE id = ?
                    LIMIT 1
                    """,
                    (runtime_run_id, runtime_run_id),
                ).fetchone()
                if still_referenced is None:
                    conn.execute("DELETE FROM agent_runtime_events WHERE runtime_run_id = ?", (runtime_run_id,))
                    conn.execute("DELETE FROM agent_runtime_actions WHERE runtime_run_id = ?", (runtime_run_id,))
        return True

    def set_run_pinned(self, run_id: str, pinned: bool) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return False
            conn.execute("UPDATE runs SET pinned = ? WHERE id = ?", (int(pinned), run_id))
        return True

    def clear_runs(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM runs").fetchone()
            count = int(row["count"] if row else 0)
            conn.execute("DELETE FROM validation_results")
            conn.execute("DELETE FROM llm_traces")
            conn.execute("DELETE FROM proposal_sessions")
            conn.execute("DELETE FROM proposals")
            conn.execute("DELETE FROM runs")
            conn.execute(
                "DELETE FROM agent_runtime_events WHERE runtime_run_id NOT IN (SELECT id FROM task_runs)"
            )
            conn.execute(
                "DELETE FROM agent_runtime_actions WHERE runtime_run_id NOT IN (SELECT id FROM task_runs)"
            )
        return count

    def list_pinned_runs(self, limit: int = 5, exclude_run_id: str | None = None) -> list[MemoryContextItem]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, task, mode, created_at, summary, applied, pinned
                FROM runs
                WHERE pinned = 1
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            validation_by_run = _validation_by_run(conn, rows)

        items: list[MemoryContextItem] = []
        for row in rows:
            if exclude_run_id and row["id"] == exclude_run_id:
                continue
            items.append(_row_to_memory_context(row, 100, ["pinned memory"], validation_by_run))
        return items

    def find_related_runs(
        self,
        task: str,
        limit: int = 3,
        candidate_limit: int = 50,
        pinned_limit: int = 3,
        exclude_run_id: str | None = None,
    ) -> list[MemoryContextItem]:
        query_tokens = _tokens(task)
        if not query_tokens:
            return []

        with self._connect() as conn:
            pinned_rows = conn.execute(
                """
                SELECT id, task, mode, created_at, summary, applied, pinned
                FROM runs
                WHERE pinned = 1
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (pinned_limit,),
            ).fetchall()
            recent_rows = conn.execute(
                """
                SELECT id, task, mode, created_at, summary, applied, pinned
                FROM runs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (candidate_limit,),
            ).fetchall()
            rows = _dedupe_rows([*pinned_rows, *recent_rows])
            validation_by_run = _validation_by_run(conn, rows)

        candidates: list[MemoryContextItem] = []
        for row in rows:
            if exclude_run_id and row["id"] == exclude_run_id:
                continue
            scored = _score_memory_row(row, query_tokens)
            if scored is None and not bool(row["pinned"]):
                continue
            score, reasons = scored or (0, [])
            if bool(row["pinned"]):
                score += 100
                reasons = ["pinned memory", *reasons]
            candidates.append(_row_to_memory_context(row, score, reasons, validation_by_run))

        candidates.sort(key=lambda item: (item.pinned, item.score, item.created_at), reverse=True)
        return candidates[:limit]

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    repo_path TEXT NOT NULL,
                    task TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    proposal_id TEXT,
                    plan_source TEXT,
                    proposal_source TEXT,
                    review_source TEXT,
                    applied INTEGER NOT NULL DEFAULT 0,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    timeline_json TEXT NOT NULL,
                    agent_runtime_run_id TEXT
                );

                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    proposed_diff TEXT NOT NULL,
                    apply_ready INTEGER NOT NULL,
                    file_edits_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    review_json TEXT,
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS llm_traces (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_preview TEXT NOT NULL,
                    raw_output TEXT NOT NULL,
                    parsed INTEGER NOT NULL,
                    fallback_used INTEGER NOT NULL,
                    error TEXT,
                    latency_ms INTEGER,
                    context_summary TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS validation_results (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    command TEXT NOT NULL,
                    allowed INTEGER NOT NULL,
                    exit_code INTEGER,
                    stdout TEXT NOT NULL,
                    stderr TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS proposal_sessions (
                    id TEXT PRIMARY KEY,
                    repo_path TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    data_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_runs (
                    id TEXT PRIMARY KEY,
                    source_repo TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    data_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_runtime_actions (
                    runtime_run_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    action_json TEXT NOT NULL,
                    observation_json TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (runtime_run_id, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS agent_runtime_events (
                    id TEXT PRIMARY KEY,
                    runtime_run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    action_id TEXT,
                    idempotency_key TEXT,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE (runtime_run_id, sequence)
                );

                CREATE INDEX IF NOT EXISTS idx_agent_runtime_events_run
                ON agent_runtime_events (runtime_run_id, sequence);
                """
            )
            _ensure_column(conn, "llm_traces", "context_summary", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(conn, "runs", "pinned", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "runs", "agent_runtime_run_id", "TEXT")

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def _row_to_run(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "repo_path": row["repo_path"],
        "task": row["task"],
        "mode": row["mode"],
        "created_at": row["created_at"],
        "summary": row["summary"],
        "proposal_id": row["proposal_id"],
        "plan_source": row["plan_source"],
        "proposal_source": row["proposal_source"],
        "review_source": row["review_source"],
        "applied": bool(row["applied"]),
        "pinned": bool(row["pinned"]),
        "agent_runtime_run_id": row["agent_runtime_run_id"],
        "timeline": _loads(row["timeline_json"], []),
    }


def _row_to_proposal(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "objective": row["objective"],
        "proposed_diff": row["proposed_diff"],
        "apply_ready": bool(row["apply_ready"]),
        "file_edits": _loads(row["file_edits_json"], []),
        "metadata": _loads(row["metadata_json"], {}),
        "review": _loads(row["review_json"], None),
    }


def _row_to_trace(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "model": row["model"],
        "prompt_preview": row["prompt_preview"],
        "raw_output": row["raw_output"],
        "parsed": bool(row["parsed"]),
        "fallback_used": bool(row["fallback_used"]),
        "error": row["error"],
        "latency_ms": row["latency_ms"],
        "context_summary": row["context_summary"],
    }


def _row_to_validation(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "command": row["command"],
        "allowed": bool(row["allowed"]),
        "exit_code": row["exit_code"],
        "stdout": row["stdout"],
        "stderr": row["stderr"],
    }


def _row_to_agent_runtime_event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": row["id"],
        "run_id": row["runtime_run_id"],
        "sequence": row["sequence"],
        "event_type": row["event_type"],
        "created_at": row["created_at"],
        "action_id": row["action_id"],
        "idempotency_key": row["idempotency_key"],
        "payload": _loads(row["payload_json"], {}),
    }


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def _runtime_action_signature(action: dict[str, Any]) -> str:
    return json.dumps(
        {"kind": action.get("kind"), "arguments": action.get("arguments") or {}},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _loads(data: str | None, default: Any) -> Any:
    if data is None:
        return default
    return json.loads(data)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if any(row["name"] == column for row in rows):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _dedupe_rows(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    seen: set[str] = set()
    unique: list[sqlite3.Row] = []
    for row in rows:
        run_id = str(row["id"])
        if run_id in seen:
            continue
        seen.add(run_id)
        unique.append(row)
    return unique


def _validation_by_run(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict[str, list[str]]:
    return {
        row["id"]: _validation_summaries(
            conn.execute(
                """
                SELECT command, allowed, exit_code
                FROM validation_results
                WHERE run_id = ?
                ORDER BY rowid
                LIMIT 5
                """,
                (row["id"],),
            ).fetchall()
        )
        for row in rows
    }


def _row_to_memory_context(
    row: sqlite3.Row,
    score: int,
    reasons: list[str],
    validation_by_run: dict[str, list[str]],
) -> MemoryContextItem:
    return MemoryContextItem(
        run_id=row["id"],
        task=row["task"],
        summary=row["summary"],
        mode=row["mode"],
        created_at=row["created_at"],
        applied=bool(row["applied"]),
        score=score,
        reasons=reasons,
        pinned=bool(row["pinned"]),
        validation=validation_by_run.get(row["id"], []),
    )


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text.lower())
    normalized: set[str] = set()
    for word in words:
        parts = word.split("_")
        for part in parts:
            if len(part) < 2 or part in _STOP_WORDS:
                continue
            normalized.add(part)
    return normalized


def _score_memory_row(row: sqlite3.Row, query_tokens: set[str]) -> tuple[int, list[str]] | None:
    task_tokens = _tokens(row["task"])
    summary_tokens = _tokens(row["summary"])
    task_matches = sorted(query_tokens & task_tokens)
    summary_matches = sorted((query_tokens & summary_tokens) - set(task_matches))
    if not task_matches and not summary_matches:
        return None

    score = len(task_matches) * 4 + len(summary_matches) * 2
    reasons = []
    if task_matches:
        reasons.append(f"task overlap: {', '.join(task_matches[:4])}")
    if summary_matches:
        reasons.append(f"summary overlap: {', '.join(summary_matches[:4])}")
    if bool(row["applied"]):
        score += 2
        reasons.append("previous proposal was applied")
    return score, reasons


def _validation_summaries(rows: list[sqlite3.Row]) -> list[str]:
    summaries: list[str] = []
    for row in rows:
        if not bool(row["allowed"]):
            summaries.append(f"{row['command']}: rejected")
            continue
        exit_code = row["exit_code"]
        summaries.append(f"{row['command']}: exit {'n/a' if exit_code is None else exit_code}")
    return summaries
