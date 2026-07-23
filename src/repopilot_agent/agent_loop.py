"""LLM repository exploration backed by the unified agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .llm.base import LLMClient, LLMError, LLMMessage
from .llm.prompts import AGENT_SYSTEM_PROMPT, build_agent_prompt
from .llm.schema import parse_agent_action_json
from .llm.tracing import traced_llm_json_call
from .models import AgentAction, AgentStep, LLMCallTrace, RepoFile, SearchHit
from .runtime import AgentRuntime, RuntimeAction, RuntimeEvent, RuntimeEventStore, RuntimePolicy

DEFAULT_AGENT_MAX_STEPS = 6
MAX_INITIAL_CONTEXT_CHARS = 4_000
MAX_OBSERVATIONS_CHARS = 10_000
MAX_FILE_OBSERVATION_CHARS = 6_000
MAX_SEARCH_RESULTS = 5


@dataclass(frozen=True)
class AgentLoopResult:
    steps: list[AgentStep]
    selected_paths: list[str]
    summary: str
    runtime_run_id: str = ""
    events: list[RuntimeEvent] = field(default_factory=list)


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
) -> AgentLoopResult:
    if max_steps <= 0:
        raise LLMError("Agent max steps must be greater than 0.")

    root = Path(repo_path)
    by_path = {repo_file.relative_path: repo_file for repo_file in files}
    steps: list[AgentStep] = []
    selected_paths: list[str] = []
    summary = ""
    finished = False
    runtime = AgentRuntime(
        root,
        task,
        run_id=runtime_run_id,
        policy=RuntimePolicy.read_only(),
        store=runtime_store,
        files=files,
    )

    try:
        for step_number in range(1, max_steps + 1):
            action = _choose_next_action(task, initial_hits, steps, step_number, max_steps, llm_client, traces)
            runtime_action = _to_runtime_action(action, step_number)
            runtime_observation = runtime.execute(runtime_action)
            if runtime_observation.status in {"approval_required", "policy_denied", "recovery_required"}:
                raise LLMError(runtime_observation.error or runtime_observation.summary)
            observation = _format_runtime_observation(runtime_observation)
            tool_input = _runtime_tool_input(runtime_action)
            selected_paths = _merge_paths(selected_paths, runtime.selected_paths, by_path)
            agent_step = AgentStep(
                order=step_number,
                action=action.action,
                thought=action.thought,
                tool_input=tool_input,
                observation=observation,
                selected_paths=list(selected_paths),
            )
            steps.append(agent_step)
            if action.action == "finish" and runtime_observation.status == "completed":
                summary = str(runtime_observation.data.get("summary") or action.summary or observation)
                finished = True
                break
    except Exception as exc:
        runtime.stop("failed", str(exc))
        raise

    if not selected_paths:
        selected_paths = _merge_paths([], [step.tool_input for step in steps if step.action == "read_file"], by_path)
    if not selected_paths:
        selected_paths = [hit.path for hit in initial_hits[:MAX_SEARCH_RESULTS] if hit.path in by_path]
    if not summary:
        summary = "Agent exploration reached the step limit; selected the best observed files."
    runtime.stop("finished" if finished else "step_limit", summary)
    return AgentLoopResult(
        steps=steps,
        selected_paths=selected_paths,
        summary=summary,
        runtime_run_id=runtime.run_id,
        events=runtime.events,
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


def _choose_next_action(
    task: str,
    initial_hits: list[SearchHit],
    steps: list[AgentStep],
    step_number: int,
    max_steps: int,
    llm_client: LLMClient,
    traces: list[LLMCallTrace] | None,
) -> AgentAction:
    return traced_llm_json_call(
        f"agent_step_{step_number}",
        llm_client,
        [
            LLMMessage(role="system", content=AGENT_SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content=build_agent_prompt(
                    task,
                    _format_initial_context(initial_hits),
                    _format_observations(steps),
                    step_number,
                    max_steps,
                ),
            ),
        ],
        parse_agent_action_json,
        traces,
        context_summary=f"Agent step {step_number}/{max_steps}; previous observations: {len(steps)}.",
    )


def _to_runtime_action(action: AgentAction, step_number: int) -> RuntimeAction:
    arguments: dict = {}
    if action.action == "search_files":
        arguments["query"] = action.query
    elif action.action == "read_file":
        arguments["path"] = action.path
    elif action.action == "finish":
        arguments["summary"] = action.summary
        arguments["selected_paths"] = action.selected_paths
    return RuntimeAction(
        kind=action.action,
        arguments=arguments,
        rationale=action.thought,
        action_id=f"explore-{step_number}",
        idempotency_key=f"explore-step-{step_number}",
    )


def _format_runtime_observation(observation) -> str:
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
        return _clip(str(observation.data.get("content") or ""), MAX_FILE_OBSERVATION_CHARS)
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
    return observation.summary


def _runtime_tool_input(action: RuntimeAction) -> str:
    if action.kind == "search_files":
        return str(action.arguments.get("query") or "")
    if action.kind == "read_file":
        return str(action.arguments.get("path") or "")
    if action.kind == "inspect_git_status":
        return "git status"
    return action.kind


def _format_initial_context(hits: list[SearchHit]) -> str:
    lines = []
    for hit in hits[:MAX_SEARCH_RESULTS]:
        lines.append(
            f"Path: {hit.path}\n"
            f"Score: {hit.score}\n"
            f"Reasons: {', '.join(hit.reasons) or 'none'}\n"
            f"Preview:\n{_clip(hit.preview, 700)}"
        )
    return _clip("\n\n---\n\n".join(lines), MAX_INITIAL_CONTEXT_CHARS)


def _format_observations(steps: list[AgentStep]) -> str:
    blocks = []
    for step in steps:
        blocks.append(
            f"Step {step.order}: {step.action}\n"
            f"Thought: {step.thought}\n"
            f"Input: {step.tool_input}\n"
            f"Observation:\n{_clip(step.observation, 1_500)}"
        )
    return _clip("\n\n---\n\n".join(blocks), MAX_OBSERVATIONS_CHARS)


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
