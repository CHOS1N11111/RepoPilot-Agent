"""Iterative read-only LLM exploration loop."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .git_tools import inspect_repository
from .llm.base import LLMClient, LLMError, LLMMessage
from .llm.prompts import AGENT_SYSTEM_PROMPT, build_agent_prompt
from .llm.schema import parse_agent_action_json
from .llm.tracing import traced_llm_json_call
from .models import AgentAction, AgentStep, LLMCallTrace, RepoFile, SearchHit
from .search import search_files

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


def run_agent_loop(
    task: str,
    repo_path: str | Path,
    files: list[RepoFile],
    initial_hits: list[SearchHit],
    llm_client: LLMClient,
    traces: list[LLMCallTrace] | None = None,
    max_steps: int = DEFAULT_AGENT_MAX_STEPS,
) -> AgentLoopResult:
    if max_steps <= 0:
        raise LLMError("Agent max steps must be greater than 0.")

    root = Path(repo_path)
    by_path = {repo_file.relative_path: repo_file for repo_file in files}
    steps: list[AgentStep] = []
    selected_paths: list[str] = []
    summary = ""

    for step_number in range(1, max_steps + 1):
        action = _choose_next_action(task, initial_hits, steps, step_number, max_steps, llm_client, traces)
        observation, tool_input, newly_selected = _execute_action(action, root, files, by_path)
        if newly_selected:
            selected_paths = _merge_paths(selected_paths, newly_selected, by_path)
        agent_step = AgentStep(
            order=step_number,
            action=action.action,
            thought=action.thought,
            tool_input=tool_input,
            observation=observation,
            selected_paths=list(selected_paths),
        )
        steps.append(agent_step)
        if action.action == "finish":
            summary = action.summary or observation
            break

    if not selected_paths:
        selected_paths = _merge_paths([], [step.tool_input for step in steps if step.action == "read_file"], by_path)
    if not selected_paths:
        selected_paths = [hit.path for hit in initial_hits[:MAX_SEARCH_RESULTS] if hit.path in by_path]
    if not summary:
        summary = "Agent exploration reached the step limit; selected the best observed files."
    return AgentLoopResult(steps=steps, selected_paths=selected_paths, summary=summary)


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


def _execute_action(
    action: AgentAction,
    root: Path,
    files: list[RepoFile],
    by_path: dict[str, RepoFile],
) -> tuple[str, str, list[str]]:
    if action.action == "search_files":
        hits = search_files(action.query, files, limit=MAX_SEARCH_RESULTS)
        if not hits:
            return f"No files matched query: {action.query}", action.query, []
        lines = [
            f"- {hit.path} (score {hit.score}; reasons: {', '.join(hit.reasons) or 'none'})\n"
            f"  Preview: {_single_line(hit.preview)}"
            for hit in hits
        ]
        return "\n".join(lines), action.query, [hit.path for hit in hits]

    if action.action == "read_file":
        repo_file = by_path.get(action.path)
        if repo_file is None:
            return f"File was not found in scanned repository context: {action.path}", action.path, []
        return _clip(repo_file.content, MAX_FILE_OBSERVATION_CHARS), action.path, [action.path]

    if action.action == "inspect_git_status":
        try:
            state = inspect_repository(root)
        except Exception as exc:
            return f"Git status unavailable: {exc}", "git status", []
        changes = "\n".join(f"- {change.path}: {change.description}" for change in state.changes[:8])
        if not changes:
            changes = "No local file changes."
        observation = "\n".join(
            [
                f"Branch: {state.branch}",
                f"Upstream: {state.upstream or '(none)'}",
                f"Ahead/behind: {state.ahead}/{state.behind}",
                f"Latest commit: {state.latest_commit.short_hash + ' ' + state.latest_commit.subject if state.latest_commit else '(none)'}",
                f"Diff stat: {state.diff_stat or '(none)'}",
                "Changes:",
                changes,
            ]
        )
        return observation, "git status", []

    if action.action == "finish":
        valid_paths = [path for path in action.selected_paths if path in by_path]
        if valid_paths:
            observation = f"Finished exploration with selected files: {', '.join(valid_paths)}"
        else:
            observation = action.summary or "Finished exploration without explicit selected files."
        return observation, "finish", valid_paths

    raise LLMError(f"Unsupported agent action: {action.action}")


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
