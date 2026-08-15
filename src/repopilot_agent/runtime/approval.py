"""Durable, exact-action approval contracts for runtime side effects."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .models import RuntimeAction


APPROVAL_PROTOCOL_VERSION = 1
DEFAULT_APPROVAL_TTL_SECONDS = 15 * 60
MAX_APPROVAL_TTL_SECONDS = 24 * 60 * 60
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class RuntimeApprovalRequest:
    checkpoint: str
    run_id: str
    action_id: str
    action_kind: str
    payload_hash: str
    action: dict[str, Any]
    file_scope: tuple[str, ...] = ()
    command_allowlist: tuple[str, ...] = ()
    baseline_hashes: dict[str, str] = field(default_factory=dict)
    baseline_exists: dict[str, bool] = field(default_factory=dict)
    diff: str = ""
    diff_truncated: bool = False
    requested_at: str = ""
    version: int = APPROVAL_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.version != APPROVAL_PROTOCOL_VERSION:
            raise ValueError(f"Unsupported approval request version: {self.version}")
        if not self.checkpoint.strip() or not self.run_id.strip() or not self.action_id.strip():
            raise ValueError("Approval request checkpoint, run id, and action id are required.")
        if not self.action_kind.strip():
            raise ValueError("Approval request action kind is required.")
        if not _SHA256_PATTERN.fullmatch(self.payload_hash):
            raise ValueError("Approval request payload_hash must be a SHA-256 hex digest.")
        _parse_timestamp(self.requested_at, "requested_at")
        if not isinstance(self.action, dict):
            raise ValueError("Approval request action must be an object.")
        if str(self.action.get("action_id") or "") != self.action_id:
            raise ValueError("Approval request action id does not match its action payload.")
        if str(self.action.get("kind") or "") != self.action_kind:
            raise ValueError("Approval request action kind does not match its action payload.")
        _validate_scope(self.file_scope, "file_scope")
        _validate_scope(self.command_allowlist, "command_allowlist")
        for path, digest in self.baseline_hashes.items():
            if path not in self.file_scope or not _SHA256_PATTERN.fullmatch(digest):
                raise ValueError("Approval request baseline hashes must match its file scope.")
        if set(self.baseline_exists) != set(self.baseline_hashes):
            raise ValueError("Approval request baseline existence must match its baseline hashes.")
        if any(not isinstance(exists, bool) for exists in self.baseline_exists.values()):
            raise ValueError("Approval request baseline existence values must be booleans.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "checkpoint": self.checkpoint,
            "run_id": self.run_id,
            "action_id": self.action_id,
            "action_kind": self.action_kind,
            "payload_hash": self.payload_hash,
            "action": dict(self.action),
            "file_scope": list(self.file_scope),
            "command_allowlist": list(self.command_allowlist),
            "baseline_hashes": dict(self.baseline_hashes),
            "baseline_exists": dict(self.baseline_exists),
            "diff": self.diff,
            "diff_truncated": self.diff_truncated,
            "requested_at": self.requested_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeApprovalRequest":
        if not isinstance(data, dict):
            raise ValueError("Approval request record must be an object.")
        return cls(
            version=_strict_int(data.get("version", APPROVAL_PROTOCOL_VERSION), "version"),
            checkpoint=str(data.get("checkpoint") or ""),
            run_id=str(data.get("run_id") or ""),
            action_id=str(data.get("action_id") or ""),
            action_kind=str(data.get("action_kind") or ""),
            payload_hash=str(data.get("payload_hash") or "").lower(),
            action=dict(data.get("action") or {}),
            file_scope=_string_tuple(data.get("file_scope"), "file_scope"),
            command_allowlist=_string_tuple(
                data.get("command_allowlist"),
                "command_allowlist",
            ),
            baseline_hashes=_hash_mapping(data.get("baseline_hashes")),
            baseline_exists=_bool_mapping(data.get("baseline_exists")),
            diff=str(data.get("diff") or ""),
            diff_truncated=bool(data.get("diff_truncated")),
            requested_at=str(data.get("requested_at") or ""),
        )


@dataclass(frozen=True)
class RuntimeApprovalGrant:
    grant_id: str
    checkpoint: str
    run_id: str
    action_id: str
    action_kind: str
    payload_hash: str
    file_scope: tuple[str, ...]
    command_allowlist: tuple[str, ...]
    granted_at: str
    expires_at: str
    version: int = APPROVAL_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.version != APPROVAL_PROTOCOL_VERSION:
            raise ValueError(f"Unsupported approval grant version: {self.version}")
        if not all(
            value.strip()
            for value in (
                self.grant_id,
                self.checkpoint,
                self.run_id,
                self.action_id,
                self.action_kind,
            )
        ):
            raise ValueError("Approval grant identity fields are required.")
        if not _SHA256_PATTERN.fullmatch(self.payload_hash):
            raise ValueError("Approval grant payload_hash must be a SHA-256 hex digest.")
        _validate_scope(self.file_scope, "file_scope")
        _validate_scope(self.command_allowlist, "command_allowlist")
        granted_at = _parse_timestamp(self.granted_at, "granted_at")
        expires_at = _parse_timestamp(self.expires_at, "expires_at")
        if expires_at <= granted_at:
            raise ValueError("Approval grant expiration must be after its grant time.")

    def is_expired(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("Approval expiration checks require a timezone-aware timestamp.")
        return current.astimezone(timezone.utc) >= _parse_timestamp(
            self.expires_at,
            "expires_at",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "grant_id": self.grant_id,
            "checkpoint": self.checkpoint,
            "run_id": self.run_id,
            "action_id": self.action_id,
            "action_kind": self.action_kind,
            "payload_hash": self.payload_hash,
            "file_scope": list(self.file_scope),
            "command_allowlist": list(self.command_allowlist),
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeApprovalGrant":
        if not isinstance(data, dict):
            raise ValueError("Approval grant record must be an object.")
        return cls(
            version=_strict_int(data.get("version", APPROVAL_PROTOCOL_VERSION), "version"),
            grant_id=str(data.get("grant_id") or ""),
            checkpoint=str(data.get("checkpoint") or ""),
            run_id=str(data.get("run_id") or ""),
            action_id=str(data.get("action_id") or ""),
            action_kind=str(data.get("action_kind") or ""),
            payload_hash=str(data.get("payload_hash") or "").lower(),
            file_scope=_string_tuple(data.get("file_scope"), "file_scope"),
            command_allowlist=_string_tuple(
                data.get("command_allowlist"),
                "command_allowlist",
            ),
            granted_at=str(data.get("granted_at") or ""),
            expires_at=str(data.get("expires_at") or ""),
        )


def approval_payload_hash(
    action: RuntimeAction,
    *,
    file_scope: tuple[str, ...],
    command_allowlist: tuple[str, ...],
    baseline_hashes: dict[str, str],
    baseline_exists: dict[str, bool],
    diff: str,
) -> str:
    canonical = json.dumps(
        {
            "version": APPROVAL_PROTOCOL_VERSION,
            "action": action.to_dict(),
            "file_scope": list(file_scope),
            "command_allowlist": list(command_allowlist),
            "baseline_hashes": baseline_hashes,
            "baseline_exists": baseline_exists,
            "diff": diff,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_file_scope(paths: object) -> tuple[str, ...]:
    values = _string_tuple(paths, "file_scope")
    return tuple(path.replace("\\", "/") for path in values)


def normalize_command_allowlist(commands: object) -> tuple[str, ...]:
    values = _string_tuple(commands, "command_allowlist")
    return tuple(command.strip() for command in values)


def utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Approval timestamps must be timezone-aware.")
    return value.astimezone(timezone.utc).isoformat()


def _parse_timestamp(value: str, field_name: str) -> datetime:
    if not value.strip():
        raise ValueError(f"Approval {field_name} is required.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Approval {field_name} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Approval {field_name} must include a timezone.")
    return parsed.astimezone(timezone.utc)


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"Approval {field_name} must be a list of strings.")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"Approval {field_name} must contain non-empty strings.")
        text = item.strip()
        if text in normalized:
            raise ValueError(f"Approval {field_name} must not contain duplicates.")
        normalized.append(text)
    return tuple(normalized)


def _hash_mapping(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Approval baseline_hashes must be an object.")
    return {str(path): str(digest).lower() for path, digest in value.items()}


def _bool_mapping(value: object) -> dict[str, bool]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Approval baseline_exists must be an object.")
    if any(not isinstance(exists, bool) for exists in value.values()):
        raise ValueError("Approval baseline_exists values must be booleans.")
    return {str(path): exists for path, exists in value.items()}


def _validate_scope(values: tuple[str, ...], field_name: str) -> None:
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(f"Approval {field_name} must contain non-empty strings.")
    if len(set(values)) != len(values):
        raise ValueError(f"Approval {field_name} must not contain duplicates.")


def _strict_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"Approval {field_name} must be an integer.")
    return value
