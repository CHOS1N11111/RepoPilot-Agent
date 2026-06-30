"""LLM context packet construction with explicit budgets."""

from __future__ import annotations

from dataclasses import dataclass

from .models import SearchHit


@dataclass(frozen=True)
class ContextBudget:
    max_chars: int
    max_files: int
    max_preview_chars: int
    max_content_chars: int
    min_content_chars: int = 400


@dataclass(frozen=True)
class ContextFile:
    path: str
    score: int
    reasons: list[str]
    preview_chars: int
    content_chars: int
    original_content_chars: int
    truncated: bool
    direct_edit_allowed: bool


@dataclass(frozen=True)
class ContextPacket:
    text: str
    summary: str
    files: list[ContextFile]
    omitted_paths: list[str]
    editable_paths: list[str]


PLANNER_CONTEXT_BUDGET = ContextBudget(
    max_chars=9_000,
    max_files=8,
    max_preview_chars=1_500,
    max_content_chars=0,
)

PATCH_CONTEXT_BUDGET = ContextBudget(
    max_chars=36_000,
    max_files=8,
    max_preview_chars=1_200,
    max_content_chars=12_000,
)

TRUNCATED_BEFORE = "[...truncated before...]"
TRUNCATED_AFTER = "[...truncated after...]"


def build_context_packet(
    hits: list[SearchHit],
    file_contents: dict[str, str] | None = None,
    budget: ContextBudget = PATCH_CONTEXT_BUDGET,
) -> ContextPacket:
    """Build a bounded, traceable context packet for an LLM call."""

    selected_hits = hits[: budget.max_files]
    if not selected_hits:
        return ContextPacket(
            text="No relevant files were selected.",
            summary=f"Budget: {budget.max_chars} chars. Included 0 file(s).",
            files=[],
            omitted_paths=[],
            editable_paths=[],
        )

    blocks: list[str] = []
    files: list[ContextFile] = []
    omitted_paths: list[str] = [hit.path for hit in hits[budget.max_files :]]
    used_chars = 0

    for hit in selected_hits:
        separator_chars = 5 if blocks else 0
        remaining = budget.max_chars - used_chars - separator_chars
        if remaining <= 120:
            omitted_paths.append(hit.path)
            continue

        block, context_file = _build_file_block(hit, file_contents or {}, budget, remaining)
        if not block:
            omitted_paths.append(hit.path)
            continue

        if blocks:
            blocks.append("---")
            used_chars += separator_chars
        blocks.append(block)
        used_chars += len(block)
        files.append(context_file)

    text = "\n\n".join(blocks) if blocks else "No relevant files fit within the context budget."
    if len(text) > budget.max_chars:
        text = text[: budget.max_chars]
    editable_paths = [file.path for file in files if file.direct_edit_allowed]
    return ContextPacket(
        text=text,
        summary=_build_summary(budget, files, omitted_paths),
        files=files,
        omitted_paths=omitted_paths,
        editable_paths=editable_paths,
    )


def _build_file_block(
    hit: SearchHit,
    file_contents: dict[str, str],
    budget: ContextBudget,
    remaining_chars: int,
) -> tuple[str, ContextFile | None]:
    preview = _clip_text(hit.preview, min(budget.max_preview_chars, max(0, remaining_chars // 3)))
    header_lines = [
        f"Path: {hit.path}",
        f"Score: {hit.score}",
        f"Reasons: {', '.join(hit.reasons) or 'none'}",
        "Direct edit allowed: no",
        f"Preview:\n{preview or '(empty preview)'}",
    ]
    header = "\n".join(header_lines)
    if len(header) > remaining_chars:
        short_preview = _clip_text(hit.preview, 120)
        header = "\n".join(
            [
                f"Path: {hit.path}",
                f"Score: {hit.score}",
                "Direct edit allowed: no",
                f"Preview:\n{short_preview or '(empty preview)'}",
            ]
        )
    if len(header) > remaining_chars:
        return "", None

    original_content = file_contents.get(hit.path)
    if original_content is None or budget.max_content_chars <= 0:
        return header, ContextFile(
            path=hit.path,
            score=hit.score,
            reasons=hit.reasons,
            preview_chars=len(preview),
            content_chars=0,
            original_content_chars=0,
            truncated=False,
            direct_edit_allowed=False,
        )

    content_budget = min(
        budget.max_content_chars,
        max(0, remaining_chars - len(header) - len("\nCurrent file content:\n")),
    )
    if len(original_content) == 0:
        content_excerpt = ""
        truncated = False
    elif content_budget >= len(original_content):
        content_excerpt = original_content
        truncated = False
    elif content_budget >= budget.min_content_chars:
        content_excerpt, truncated = _excerpt_content(original_content, hit.preview, content_budget)
    else:
        content_excerpt = ""
        truncated = True

    direct_edit_allowed = not truncated and content_budget >= len(original_content)
    block = "\n".join(
        [
            f"Path: {hit.path}",
            f"Score: {hit.score}",
            f"Reasons: {', '.join(hit.reasons) or 'none'}",
            f"Direct edit allowed: {'yes' if direct_edit_allowed else 'no'}",
            f"Preview:\n{preview or '(empty preview)'}",
            f"Current file content:\n{content_excerpt}",
        ]
    )
    if len(block) > remaining_chars:
        allowed = max(0, content_budget - (len(block) - remaining_chars))
        content_excerpt, truncated = _excerpt_content(original_content, hit.preview, allowed)
        direct_edit_allowed = False
        block = "\n".join(
            [
                f"Path: {hit.path}",
                f"Score: {hit.score}",
                f"Reasons: {', '.join(hit.reasons) or 'none'}",
                "Direct edit allowed: no",
                f"Preview:\n{preview or '(empty preview)'}",
                f"Current file content:\n{content_excerpt}",
            ]
        )

    return block, ContextFile(
        path=hit.path,
        score=hit.score,
        reasons=hit.reasons,
        preview_chars=len(preview),
        content_chars=len(content_excerpt),
        original_content_chars=len(original_content),
        truncated=truncated,
        direct_edit_allowed=direct_edit_allowed,
    )


def _clip_text(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = "\n[...truncated...]"
    if limit <= len(marker):
        return text[:limit]
    return text[: limit - len(marker)] + marker


def _excerpt_content(content: str, anchor: str, limit: int) -> tuple[str, bool]:
    if limit <= 0:
        return "", bool(content)
    if len(content) <= limit:
        return content, False

    clean_anchor = anchor.strip()
    anchor_index = content.find(clean_anchor) if clean_anchor else -1
    if anchor_index < 0:
        start = 0
    else:
        start = max(0, anchor_index - max(0, limit // 3))
    end = min(len(content), start + limit)
    start = max(0, end - limit)

    prefix = f"{TRUNCATED_BEFORE}\n" if start > 0 else ""
    suffix = f"\n{TRUNCATED_AFTER}" if end < len(content) else ""
    marker_chars = len(prefix) + len(suffix)
    excerpt_limit = max(0, limit - marker_chars)
    excerpt = content[start : start + excerpt_limit]
    return prefix + excerpt + suffix, True


def _build_summary(budget: ContextBudget, files: list[ContextFile], omitted_paths: list[str]) -> str:
    if not files:
        return f"Budget: {budget.max_chars} chars. Included 0 file(s)."
    file_summaries = []
    for file in files:
        state = "full" if not file.truncated else "truncated"
        edit_state = "edit allowed" if file.direct_edit_allowed else "no direct edit"
        if file.original_content_chars:
            size = f"{file.content_chars}/{file.original_content_chars} chars"
        else:
            size = f"{file.preview_chars} preview chars"
        file_summaries.append(f"{file.path} ({state}, {edit_state}, {size})")
    omitted = f" Omitted: {', '.join(omitted_paths)}." if omitted_paths else ""
    return (
        f"Budget: {budget.max_chars} chars. Included {len(files)} file(s): "
        f"{'; '.join(file_summaries)}.{omitted}"
    )
