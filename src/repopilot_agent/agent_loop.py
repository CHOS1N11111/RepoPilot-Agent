"""LLM repository exploration backed by the unified agent runtime."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from .agent_context import AgentContextPacket, build_agent_context_packet
from .execution import (
    AcceptanceCriterion,
    ExecutionBudget,
    ExecutionUsage,
    execution_budget_state,
)
from .llm.base import LLMClient, LLMError, LLMMessage
from .git_tools import get_git_diff
from .llm.prompts import agent_system_prompt, build_agent_prompt
from .llm.schema import (
    AGENT_WRITE_ACTIONS,
    ALLOWED_AGENT_DECISION_ACTIONS,
    parse_agent_decision_json,
)
from .llm.tracing import traced_llm_json_call
from .models import (
    AgentDecision,
    AgentStep,
    LLMCallTrace,
    MemoryContextItem,
    RepoFile,
    SearchHit,
)
from .repository_map import RepositoryMap, render_repository_map
from .runtime import (
    AgentRuntime,
    AgentWorkingState,
    RuntimeAction,
    RuntimeEvent,
    RuntimeEventStore,
    RuntimePolicy,
    STOPPING_OBSERVATION_STATUSES,
    advance_agent_working_state,
    agent_completion_blockers,
    apply_agent_state_update,
    create_agent_working_state,
    stop_agent_working_state,
)
from .structured_patch import file_sha256, parse_structured_patch, preview_structured_patch

DEFAULT_AGENT_MAX_STEPS = 6
MAX_FILE_OBSERVATION_CHARS = 6_000
MAX_PROPOSED_DIFF_CHARS = 24_000
MAX_SEARCH_RESULTS = 5


@dataclass(frozen=True)
class AgentLoopResult:
    steps: list[AgentStep]
    selected_paths: list[str]
    summary: str
    runtime_run_id: str = ""
    events: list[RuntimeEvent] = field(default_factory=list)
    working_state: AgentWorkingState | None = None
    stop_reason: str = ""
    pending_question: str = ""
    pending_approval: dict = field(default_factory=dict)
    proposed_edits: list[dict] = field(default_factory=list)
    proposed_diff: str = ""


def run_agent_loop(
    task: str,
    repo_path: str | Path,
    files: list[RepoFile],
    initial_hits: list[SearchHit],
    llm_client: LLMClient,
    traces: list[LLMCallTrace] | None = None,
    max_steps: int = DEFAULT_AGENT_MAX_STEPS,
    runtime_run_id: str | None = None,
    runtime_store: RuntimeEventStore | None = None,
    repository_map: RepositoryMap | None = None,
    memory_context: list[MemoryContextItem] | None = None,
    current_diff: str = "",
    acceptance_criteria: list[AcceptanceCriterion] | None = None,
    execution_budget: ExecutionBudget | None = None,
    allow_user_questions: bool = True,
    allow_write_actions: bool = False,
    managed_worktree_root: str | Path | None = None,
) -> AgentLoopResult:
    if max_steps <= 0:
        raise LLMError("Agent max steps must be greater than 0.")

    root = Path(repo_path)
    by_path = {repo_file.relative_path: repo_file for repo_file in files}
    write_actions_enabled = allow_write_actions and bool(by_path)
    steps: list[AgentStep] = []
    selected_paths: list[str] = []
    summary = ""
    stop_reason = ""
    pending_question = ""
    loop_started_at = time.monotonic()
    loop_budget = execution_budget or ExecutionBudget(
        max_agent_steps=max_steps,
        max_tool_calls=max_steps,
    )
    max_steps = min(
        max_steps,
        loop_budget.max_agent_steps,
        loop_budget.max_tool_calls,
    )
    runtime_policy = (
        RuntimePolicy.managed_worktree(
            allowed_edit_paths=sorted(by_path),
            worktree_root=(
                str(Path(managed_worktree_root).expanduser().resolve())
                if managed_worktree_root is not None
                else ""
            ),
        )
        if write_actions_enabled
        else RuntimePolicy.read_only()
    )
    if not allow_user_questions:
        runtime_policy = replace(
            runtime_policy,
            allowed_actions=runtime_policy.allowed_actions - {"ask_user"},
        )
    runtime = AgentRuntime(
        root,
        task,
        run_id=runtime_run_id,
        policy=runtime_policy,
        store=runtime_store,
        files=files,
        repository_map=repository_map,
    )
    working_state = create_agent_working_state(
        task,
        acceptance_criteria=acceptance_criteria,
    )
    runtime.record_working_state(working_state)

    try:
        for step_number in range(1, max_steps + 1):
            map_context = (
                render_repository_map(
                    repository_map,
                    task,
                    seed_paths=_merge_context_paths(selected_paths, initial_hits),
                )
                if repository_map is not None
                else ""
            )
            context_packet = build_agent_context_packet(
                working_state,
                initial_hits,
                steps,
                repository_map_context=map_context,
                memory_context=memory_context,
                current_diff=(
                    _load_current_diff(root)
                    if write_actions_enabled
                    else current_diff
                ),
                acceptance_criteria=acceptance_criteria,
                remaining_budget=_remaining_execution_budget(
                    loop_budget,
                    step_number,
                    max_steps,
                    runtime.events,
                    loop_started_at,
                ),
            )
            decision = _choose_next_decision(
                task,
                context_packet,
                step_number,
                max_steps,
                llm_client,
                traces,
                allow_user_questions,
                write_actions_enabled,
            )
            runtime_action = _to_runtime_action(decision, step_number)
            try:
                preview_state = apply_agent_state_update(
                    working_state,
                    decision.state_update,
                )
            except ValueError as exc:
                raise LLMError(f"Agent state update is not supported by evidence: {exc}") from exc
            runtime.record_decision(runtime_action, _decision_record(decision))
            completion_blockers = (
                agent_completion_blockers(preview_state)
                if runtime_action.kind == "finish"
                else []
            )
            write_conflict = (
                _managed_write_conflict(root, working_state, runtime_action)
                if write_actions_enabled and runtime_action.kind in AGENT_WRITE_ACTIONS
                else None
            )
            if completion_blockers:
                runtime_observation = runtime.block_finish(
                    runtime_action,
                    completion_blockers,
                )
            elif write_conflict is not None:
                reason, conflict_data = write_conflict
                runtime_observation = runtime.block_action_conflict(
                    runtime_action,
                    reason,
                    data=conflict_data,
                )
            else:
                runtime_observation = runtime.execute(runtime_action)
            selected_paths = _merge_paths(selected_paths, runtime.selected_paths, by_path)
            working_state = advance_agent_working_state(
                working_state,
                runtime_action,
                runtime_observation,
                selected_paths=selected_paths,
                state_update=decision.state_update,
                expected_evidence=decision.expected_evidence,
            )
            runtime.record_working_state(working_state)
            observation = _format_runtime_observation(runtime_observation)
            tool_input = _runtime_tool_input(runtime_action)
            agent_step = AgentStep(
                order=step_number,
                action=decision.action_kind,
                thought=decision.rationale,
                tool_input=tool_input,
                observation=observation,
                selected_paths=list(selected_paths),
                expected_evidence=decision.expected_evidence,
                state_update=asdict(decision.state_update),
                finish_reason=decision.finish_reason,
                user_question=decision.user_question,
            )
            steps.append(agent_step)
            if runtime_observation.status in STOPPING_OBSERVATION_STATUSES:
                stop_reason = runtime_observation.status
                summary = runtime_observation.error or runtime_observation.summary
                if stop_reason == "input_required":
                    pending_question = str(
                        runtime_observation.data.get("question")
                        or decision.user_question
                        or runtime_observation.summary
                    )
                break
            if decision.action_kind == "finish" and runtime_observation.status == "completed":
                summary = str(
                    runtime_observation.data.get("summary")
                    or decision.finish_reason
                    or observation
                )
                stop_reason = "finished"
                break
    except Exception as exc:
        if working_state.stop_reason is None:
            working_state = stop_agent_working_state(working_state, "failed")
            runtime.record_working_state(working_state)
        runtime.stop("failed", str(exc))
        raise

    if not selected_paths:
        selected_paths = _merge_paths([], [step.tool_input for step in steps if step.action == "read_file"], by_path)
    if not selected_paths:
        selected_paths = [hit.path for hit in initial_hits[:MAX_SEARCH_RESULTS] if hit.path in by_path]
    if not stop_reason:
        stop_reason = "step_limit"
    if not summary:
        summary = "Agent exploration reached the step limit; selected the best observed files."
    if working_state.stop_reason != stop_reason or working_state.selected_paths != selected_paths:
        working_state = stop_agent_working_state(
            working_state,
            stop_reason,
            selected_paths=selected_paths,
        )
        runtime.record_working_state(working_state)
    runtime.stop(stop_reason, summary)
    return AgentLoopResult(
        steps=steps,
        selected_paths=selected_paths,
        summary=summary,
        runtime_run_id=runtime.run_id,
        events=runtime.events,
        working_state=working_state,
        stop_reason=stop_reason,
        pending_question=pending_question,
        pending_approval=runtime.pending_approval,
        proposed_edits=runtime.proposed_edits,
        proposed_diff=_clip(runtime.proposed_diff, MAX_PROPOSED_DIFF_CHARS),
    )


def select_agent_hits(
    initial_hits: list[SearchHit],
    files: list[RepoFile],
    selected_paths: list[str],
    limit: int,
) -> list[SearchHit]:
    by_hit = {hit.path: hit for hit in initial_hits}
    by_file = {repo_file.relative_path: repo_file for repo_file in files}
    ordered: list[SearchHit] = []
    seen: set[str] = set()

    for path in selected_paths:
        if path in seen or path not in by_file:
            continue
        if path in by_hit:
            ordered.append(by_hit[path])
        else:
            repo_file = by_file[path]
            ordered.append(
                SearchHit(
                    path=path,
                    score=max(1, min(10, len(selected_paths) - len(ordered))),
                    reasons=["selected by iterative agent"],
                    preview=_clip(repo_file.content, 900),
                )
            )
        seen.add(path)

    for hit in initial_hits:
        if len(ordered) >= limit:
            break
        if hit.path not in seen:
            ordered.append(hit)
            seen.add(hit.path)
    return ordered[:limit]


def _managed_write_conflict(
    root: Path,
    working_state: AgentWorkingState,
    action: RuntimeAction,
) -> tuple[str, dict] | None:
    path = str(action.arguments.get("path") or "").replace("\\", "/")
    proposal = next(
        (item for item in working_state.proposed_edits if item.path == path),
        None,
    )
    if proposal is None or proposal.status != "inspected" or not proposal.inspected:
        return (
            "Write actions require a latest inspected virtual proposal for the same path.",
            {"kind": "proposal_not_inspected", "path": path},
        )
    target = (root / Path(*path.split("/"))).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return (
            "Write action path escapes the managed worktree.",
            {"kind": "unsafe_path", "path": path},
        )
    if not target.is_file():
        return (
            "The inspected virtual proposal target no longer exists.",
            {"kind": "missing_target", "path": path},
        )
    try:
        current_content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return (
            "The inspected virtual proposal target is no longer UTF-8 text.",
            {"kind": "non_utf8_target", "path": path},
        )
    current_hash = file_sha256(current_content)
    if current_hash != proposal.base_sha256:
        return (
            "The managed worktree changed after the virtual proposal was inspected.",
            {
                "kind": "stale_repository",
                "path": path,
                "expected": proposal.base_sha256,
                "actual": current_hash,
            },
        )
    try:
        if action.kind == "edit_file":
            updated_content = action.arguments.get("new_content")
            if not isinstance(updated_content, str):
                raise ValueError("edit_file requires new_content as a string.")
        else:
            patch = parse_structured_patch(action.arguments)
            preview, updated_content = preview_structured_patch(
                patch,
                current_content,
            )
            if preview.status != "ready" or updated_content is None:
                return (
                    preview.message,
                    {
                        "kind": "write_preview_conflict",
                        "path": path,
                        "conflicts": preview.conflicts,
                    },
                )
    except (TypeError, ValueError) as exc:
        return (
            f"The managed write action is invalid: {exc}",
            {"kind": "invalid_write_action", "path": path},
        )
    resulting_hash = file_sha256(updated_content)
    if resulting_hash != proposal.current_sha256:
        return (
            "The write action does not reproduce the latest inspected virtual proposal.",
            {
                "kind": "virtual_diff_mismatch",
                "path": path,
                "expected": proposal.current_sha256,
                "actual": resulting_hash,
            },
        )
    return None


def _load_current_diff(root: Path) -> str:
    try:
        return get_git_diff(root)
    except Exception:
        return ""


def _choose_next_decision(
    task: str,
    context_packet: AgentContextPacket,
    step_number: int,
    max_steps: int,
    llm_client: LLMClient,
    traces: list[LLMCallTrace] | None,
    allow_user_questions: bool,
    allow_write_actions: bool,
) -> AgentDecision:
    allowed_actions = set(ALLOWED_AGENT_DECISION_ACTIONS)
    if allow_write_actions:
        allowed_actions.update(AGENT_WRITE_ACTIONS)
    return traced_llm_json_call(
        f"agent_step_{step_number}",
        llm_client,
        [
            LLMMessage(
                role="system",
                content=agent_system_prompt(allow_write_actions),
            ),
            LLMMessage(
                role="user",
                content=build_agent_prompt(
                    task,
                    context_packet.text,
                    step_number,
                    max_steps,
                    allow_user_questions=allow_user_questions,
                    allow_write_actions=allow_write_actions,
                ),
            ),
        ],
        lambda response: parse_agent_decision_json(
            response,
            allowed_actions=allowed_actions,
        ),
        traces,
        context_summary=f"Step {step_number}/{max_steps}. {context_packet.summary}",
    )


def _to_runtime_action(decision: AgentDecision, step_number: int) -> RuntimeAction:
    arguments = dict(decision.action_arguments)
    if decision.action_kind == "finish":
        arguments["summary"] = decision.finish_reason
    elif decision.action_kind == "ask_user":
        arguments["question"] = decision.user_question
    return RuntimeAction(
        kind=decision.action_kind,
        arguments=arguments,
        rationale=decision.rationale,
        action_id=f"explore-{step_number}",
        idempotency_key=f"explore-step-{step_number}",
    )


def _decision_record(decision: AgentDecision) -> dict:
    return {
        "version": decision.version,
        "rationale": decision.rationale,
        "action": {
            "kind": decision.action_kind,
            "arguments": dict(decision.action_arguments),
        },
        "expected_evidence": decision.expected_evidence,
        "state_update": asdict(decision.state_update),
        "finish_reason": decision.finish_reason,
        "user_question": decision.user_question,
    }


def _format_runtime_observation(observation) -> str:
    if observation.action_kind == "propose_patch":
        conflicts = observation.data.get("conflicts") or []
        conflict_lines = "\n".join(
            f"- {item.get('kind', 'conflict')}: expected {item.get('expected', '')}; "
            f"actual {item.get('actual', '')}"
            for item in conflicts[:5]
            if isinstance(item, dict)
        )
        if observation.status != "completed":
            return _clip(
                "\n".join(
                    part
                    for part in [
                        observation.error or observation.summary,
                        conflict_lines,
                    ]
                    if part
                ),
                MAX_FILE_OBSERVATION_CHARS,
            )
        diff = str(observation.data.get("diff") or "").strip()
        if observation.data.get("removed"):
            return observation.summary
        return _clip(
            "\n".join(
                [
                    observation.summary,
                    f"Base SHA-256: {observation.data.get('base_sha256', '')}",
                    f"Previous virtual SHA-256: {observation.data.get('previous_sha256', '')}",
                    f"Resulting virtual SHA-256: {observation.data.get('resulting_sha256', '')}",
                    f"Revision: {observation.data.get('revision', 0)}",
                    "Cumulative virtual diff:",
                    diff or "(empty)",
                ]
            ),
            MAX_FILE_OBSERVATION_CHARS,
        )
    if observation.action_kind == "inspect_proposed_diff":
        if observation.status != "completed":
            conflicts = observation.data.get("conflicts") or []
            details = "\n".join(
                f"- {item.get('path', '')}: expected {item.get('expected', '')}; "
                f"actual {item.get('actual', '')}"
                for item in conflicts[:5]
                if isinstance(item, dict)
            )
            return _clip(
                "\n".join(
                    part for part in [observation.error or observation.summary, details] if part
                ),
                MAX_FILE_OBSERVATION_CHARS,
            )
        diff = str(observation.data.get("diff") or "").strip()
        return _clip(
            f"{observation.summary}\nCumulative virtual diff:\n{diff or '(empty)'}",
            MAX_FILE_OBSERVATION_CHARS,
        )
    if observation.action_kind in {"apply_patch", "edit_file"}:
        if observation.status == "approval_required":
            request = observation.data.get("approval_request") or {}
            diff = str(request.get("diff") or observation.data.get("diff") or "")
            return _clip(
                "\n".join(
                    [
                        observation.summary,
                        f"Approval checkpoint: {request.get('checkpoint', '')}",
                        f"Payload SHA-256: {request.get('payload_hash', '')}",
                        "Exact proposed write diff:",
                        diff or "(empty)",
                    ]
                ),
                MAX_FILE_OBSERVATION_CHARS,
            )
        if observation.status in {"completed", "applied", "no_change"}:
            evidence_lines = [
                f"- {item.get('path', '')}: before {item.get('before_sha256') or '(missing)'}; "
                f"after {item.get('after_sha256') or '(missing)'}"
                for item in observation.data.get("write_evidence") or []
                if isinstance(item, dict)
            ]
            diff = str(
                observation.data.get("resulting_diff")
                or observation.data.get("diff")
                or ""
            )
            return _clip(
                "\n".join(
                    [
                        observation.summary,
                        "Write hashes:",
                        *(evidence_lines or ["- none"]),
                        "Resulting managed-worktree diff:",
                        diff or "(empty)",
                    ]
                ),
                MAX_FILE_OBSERVATION_CHARS,
            )
    if observation.status != "completed":
        return observation.error or observation.summary
    if observation.action_kind == "search_files":
        hits = observation.data.get("hits", [])
        if not hits:
            return f"No files matched query: {observation.data.get('query', '')}"
        lines = []
        for hit in hits:
            reasons = ", ".join(hit.get("reasons", [])) or "none"
            lines.append(
                f"- {hit.get('path', '')} (score {hit.get('score', 0)}; reasons: {reasons})\n"
                f"  Preview: {_single_line(str(hit.get('preview', '')))}"
            )
        return "\n".join(lines)
    if observation.action_kind == "read_file":
        return _clip(
            "\n".join(
                [
                    f"Path: {observation.data.get('path', '')}",
                    f"Complete-file SHA-256: {observation.data.get('sha256', '')}",
                    f"Displayed content truncated: {'yes' if observation.data.get('truncated') else 'no'}",
                    "Content:",
                    str(observation.data.get("content") or ""),
                ]
            ),
            MAX_FILE_OBSERVATION_CHARS,
        )
    if observation.action_kind == "inspect_repository_map":
        matches = observation.data.get("matches", [])
        if not matches:
            return observation.summary
        lines = [observation.summary]
        for match in matches:
            symbols = ", ".join(match.get("symbols", [])[:8]) or "no indexed symbols"
            related = ", ".join(match.get("related_paths", [])[:5]) or "none"
            lines.append(f"- {match.get('path', '')}: {symbols}; related: {related}")
        return _clip("\n".join(lines), MAX_FILE_OBSERVATION_CHARS)
    if observation.action_kind == "inspect_git_status":
        latest = observation.data.get("latest_commit") or {}
        changes = observation.data.get("changes") or []
        change_lines = "\n".join(
            f"- {change.get('path', '')}: {change.get('description', '')}" for change in changes[:8]
        ) or "No local file changes."
        latest_text = f"{latest.get('short_hash', '')} {latest.get('subject', '')}".strip() or "(none)"
        return "\n".join(
            [
                f"Branch: {observation.data.get('branch', 'unknown')}",
                f"Upstream: {observation.data.get('upstream') or '(none)'}",
                f"Ahead/behind: {observation.data.get('ahead', 0)}/{observation.data.get('behind', 0)}",
                f"Latest commit: {latest_text}",
                f"Diff stat: {observation.data.get('diff_stat') or '(none)'}",
                "Changes:",
                change_lines,
            ]
        )
    if observation.action_kind == "inspect_diff":
        diff = str(observation.data.get("diff") or "").strip()
        label = "Staged diff" if observation.data.get("staged") else "Working-tree diff"
        return f"{label}:\n{_clip(diff, MAX_FILE_OBSERVATION_CHARS) if diff else '(empty)'}"
    return observation.summary


def _runtime_tool_input(action: RuntimeAction) -> str:
    if action.kind == "search_files":
        return str(action.arguments.get("query") or "")
    if action.kind == "read_file":
        return str(action.arguments.get("path") or "")
    if action.kind == "inspect_repository_map":
        return str(action.arguments.get("query") or "current task")
    if action.kind == "inspect_git_status":
        return "git status"
    if action.kind == "inspect_diff":
        return "staged diff" if action.arguments.get("staged") else "working-tree diff"
    if action.kind == "propose_patch":
        return str(action.arguments.get("path") or "")
    if action.kind == "inspect_proposed_diff":
        return "virtual proposed diff"
    if action.kind in {"apply_patch", "edit_file"}:
        return str(action.arguments.get("path") or "")
    if action.kind == "ask_user":
        return str(action.arguments.get("question") or "")
    return action.kind


def _remaining_execution_budget(
    budget: ExecutionBudget,
    step_number: int,
    max_steps: int,
    events: list[RuntimeEvent],
    started_at: float,
) -> dict[str, int]:
    completed_steps = max(step_number - 1, 0)
    tool_calls = sum(1 for event in events if event.event_type == "action_started")
    usage = ExecutionUsage(
        agent_steps=completed_steps,
        tool_calls=tool_calls,
        elapsed_ms=max(int((time.monotonic() - started_at) * 1_000), 0),
    )
    remaining = dict(execution_budget_state(budget, usage)["remaining"])
    loop_steps_remaining = max(max_steps - completed_steps, 0)
    remaining["agent_steps"] = min(remaining["agent_steps"], loop_steps_remaining)
    remaining["tool_calls"] = min(remaining["tool_calls"], loop_steps_remaining)
    return remaining


def _merge_context_paths(
    selected_paths: list[str],
    initial_hits: list[SearchHit],
) -> list[str]:
    paths = list(selected_paths)
    seen = set(paths)
    for hit in initial_hits:
        if hit.path not in seen:
            paths.append(hit.path)
            seen.add(hit.path)
    return paths


def _merge_paths(existing: list[str], paths: list[str], by_path: dict[str, RepoFile]) -> list[str]:
    merged = list(existing)
    seen = set(existing)
    for path in paths:
        if path in by_path and path not in seen:
            merged.append(path)
            seen.add(path)
    return merged


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n[...truncated...]"
    if limit <= len(marker):
        return text[:limit]
    return text[: limit - len(marker)] + marker


def _single_line(text: str, limit: int = 240) -> str:
    return " ".join(_clip(text, limit).split())
