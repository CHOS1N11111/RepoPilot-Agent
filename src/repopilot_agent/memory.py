"""SQLite-backed local memory for web workflow runs."""

from __future__ import annotations

import json
import re
import sqlite3
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


class MemoryStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
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
                    plan_source, proposal_source, review_source, applied, timeline_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    _json(timeline or []),
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

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, repo_path, task, mode, created_at, summary, proposal_id,
                       plan_source, proposal_source, review_source, applied, timeline_json
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
                       plan_source, proposal_source, review_source, applied, timeline_json
                FROM runs
                WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
            if run_row is None:
                return None
            proposal = conn.execute("SELECT * FROM proposals WHERE run_id = ?", (run_id,)).fetchone()
            traces = conn.execute("SELECT * FROM llm_traces WHERE run_id = ? ORDER BY rowid", (run_id,)).fetchall()
            validation = conn.execute(
                "SELECT * FROM validation_results WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            ).fetchall()
        data = _row_to_run(run_row)
        data["proposal"] = _row_to_proposal(proposal) if proposal else None
        data["llm_traces"] = [_row_to_trace(row) for row in traces]
        data["validation"] = [_row_to_validation(row) for row in validation]
        return data

    def delete_run(self, run_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM validation_results WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM llm_traces WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM proposals WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        return True

    def clear_runs(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM runs").fetchone()
            count = int(row["count"] if row else 0)
            conn.execute("DELETE FROM validation_results")
            conn.execute("DELETE FROM llm_traces")
            conn.execute("DELETE FROM proposals")
            conn.execute("DELETE FROM runs")
        return count

    def find_related_runs(
        self,
        task: str,
        limit: int = 3,
        candidate_limit: int = 50,
        exclude_run_id: str | None = None,
    ) -> list[MemoryContextItem]:
        query_tokens = _tokens(task)
        if not query_tokens:
            return []

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, task, mode, created_at, summary, applied
                FROM runs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (candidate_limit,),
            ).fetchall()
            validation_by_run = {
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

        candidates: list[MemoryContextItem] = []
        for row in rows:
            if exclude_run_id and row["id"] == exclude_run_id:
                continue
            scored = _score_memory_row(row, query_tokens)
            if scored is None:
                continue
            score, reasons = scored
            candidates.append(
                MemoryContextItem(
                    run_id=row["id"],
                    task=row["task"],
                    summary=row["summary"],
                    mode=row["mode"],
                    created_at=row["created_at"],
                    applied=bool(row["applied"]),
                    score=score,
                    reasons=reasons,
                    validation=validation_by_run.get(row["id"], []),
                )
            )

        candidates.sort(key=lambda item: (item.score, item.created_at), reverse=True)
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
                    timeline_json TEXT NOT NULL
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
                """
            )
            _ensure_column(conn, "llm_traces", "context_summary", "TEXT NOT NULL DEFAULT ''")

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


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


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
