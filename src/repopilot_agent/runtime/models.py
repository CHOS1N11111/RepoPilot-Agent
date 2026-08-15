"""Data contracts for the RepoPilot action-observation runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any
from uuid import uuid4


SUPPORTED_ACTIONS = frozenset(
    {
        "search_files",
        "read_file",
        "inspect_repository_map",
        "inspect_git_status",
        "inspect_diff",
        "propose_patch",
        "inspect_proposed_diff",
        "edit_file",
        "apply_patch",
        "run_command",
        "validate",
        "ask_user",
        "finish",
    }
)
READ_ONLY_ACTIONS = frozenset(
    {
        "search_files",
        "read_file",
        "inspect_repository_map",
        "inspect_git_status",
        "inspect_diff",
        "propose_patch",
        "inspect_proposed_diff",
        "ask_user",
        "finish",
    }
)
SIDE_EFFECT_ACTIONS = frozenset({"edit_file", "apply_patch", "run_command", "validate"})
STOPPING_OBSERVATION_STATUSES = frozenset(
    {
        "approval_required",
        "input_required",
        "recovery_required",
        "policy_denied",
        "verification_failed",
        "failed",
    }
)


@dataclass(frozen=True)
class RuntimeAction:
    kind: str
    arguments: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    action_id: str = field(default_factory=lambda: uuid4().hex)
    idempotency_key: str = ""

    def __post_init__(self) -> None:
        if self.kind not in SUPPORTED_ACTIONS:
            raise ValueError(f"Unsupported runtime action: {self.kind}")
        if not self.action_id.strip():
            raise ValueError("Runtime action_id must not be empty.")
        if not isinstance(self.arguments, dict):
            raise ValueError("Runtime action arguments must be an object.")

    @property
    def effective_idempotency_key(self) -> str:
        return self.idempotency_key.strip() or self.action_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeAction":
        return cls(
            kind=str(data.get("kind") or ""),
            arguments=dict(data.get("arguments") or {}),
            rationale=str(data.get("rationale") or ""),
            action_id=str(data.get("action_id") or uuid4().hex),
            idempotency_key=str(data.get("idempotency_key") or ""),
        )


@dataclass(frozen=True)
class RuntimeObservation:
    action_id: str
    action_kind: str
    status: str
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def as_replayed(self) -> "RuntimeObservation":
        return replace(self, replayed=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeObservation":
        return cls(
            action_id=str(data.get("action_id") or ""),
            action_kind=str(data.get("action_kind") or ""),
            status=str(data.get("status") or "failed"),
            summary=str(data.get("summary") or ""),
            data=dict(data.get("data") or {}),
            error=str(data["error"]) if data.get("error") is not None else None,
            replayed=bool(data.get("replayed")),
        )


@dataclass(frozen=True)
class RuntimeEvent:
    event_id: str
    run_id: str
    sequence: int
    event_type: str
    created_at: str
    action_id: str | None = None
    idempotency_key: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeEvent":
        return cls(
            event_id=str(data.get("event_id") or ""),
            run_id=str(data.get("run_id") or ""),
            sequence=int(data.get("sequence") or 0),
            event_type=str(data.get("event_type") or "unknown"),
            created_at=str(data.get("created_at") or ""),
            action_id=str(data["action_id"]) if data.get("action_id") is not None else None,
            idempotency_key=(
                str(data["idempotency_key"]) if data.get("idempotency_key") is not None else None
            ),
            payload=dict(data.get("payload") or {}),
        )


@dataclass(frozen=True)
class RuntimePolicy:
    allowed_actions: frozenset[str] = READ_ONLY_ACTIONS
    approval_required_actions: frozenset[str] = SIDE_EFFECT_ACTIONS
    allowed_edit_paths: tuple[str, ...] = ()
    allowed_commands: tuple[str, ...] = ()
    requires_managed_worktree: bool = False
    worktree_root: str = ""

    @classmethod
    def read_only(cls) -> "RuntimePolicy":
        return cls()

    @classmethod
    def sandboxed(
        cls,
        *,
        allowed_edit_paths: list[str] | tuple[str, ...] = (),
        allowed_commands: list[str] | tuple[str, ...] = (),
    ) -> "RuntimePolicy":
        return cls(
            allowed_actions=SUPPORTED_ACTIONS,
            allowed_edit_paths=tuple(path.replace("\\", "/") for path in allowed_edit_paths),
            allowed_commands=tuple(command.strip() for command in allowed_commands),
        )

    @classmethod
    def managed_worktree(
        cls,
        *,
        allowed_edit_paths: list[str] | tuple[str, ...],
        worktree_root: str = "",
    ) -> "RuntimePolicy":
        normalized_paths = tuple(
            dict.fromkeys(
                path.strip().replace("\\", "/")
                for path in allowed_edit_paths
                if path.strip()
            )
        )
        if not normalized_paths:
            raise ValueError(
                "Managed-worktree policy requires at least one exact editable path."
            )
        return cls(
            allowed_actions=READ_ONLY_ACTIONS | {"edit_file", "apply_patch"},
            allowed_edit_paths=normalized_paths,
            allowed_commands=(),
            requires_managed_worktree=True,
            worktree_root=str(worktree_root or "").strip(),
        )

    def evaluate(
        self,
        action: RuntimeAction,
        *,
        approval_granted: bool = False,
    ) -> tuple[str, str]:
        if action.kind not in self.allowed_actions:
            return "deny", f"Action {action.kind} is disabled by the current runtime policy."
        if action.kind in {"edit_file", "apply_patch"}:
            path = str(action.arguments.get("path") or "").replace("\\", "/")
            if self.allowed_edit_paths and path not in self.allowed_edit_paths:
                return "deny", f"File edit path is outside the approved runtime boundary: {path or '(empty)'}"
        if action.kind in {"run_command", "validate"}:
            command = str(action.arguments.get("command") or "").strip()
            if not self.allowed_commands or command not in self.allowed_commands:
                return "deny", f"Command is outside the approved runtime command set: {command or '(empty)'}"
        if action.kind in self.approval_required_actions and not approval_granted:
            return "approval", f"Action {action.action_id} requires explicit approval before execution."
        return "allow", "Action is allowed."


@dataclass(frozen=True)
class RuntimeRunResult:
    run_id: str
    status: str
    stop_reason: str
    observations: list[RuntimeObservation]
    events: list[RuntimeEvent]
    selected_paths: list[str]
    summary: str = ""
    pending_approval: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
