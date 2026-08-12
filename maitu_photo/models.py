"""Domain models for the photo plugin persistence layer.

The module deliberately has no MaiBot imports.  Keeping persistence values as
plain dataclasses makes them usable by background workers and unit tests even
when the host application is not running.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

JsonObject = dict[str, Any]


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid4().hex


class StrEnum(str, Enum):
    """A Python 3.10-compatible string enum."""

    def __str__(self) -> str:
        return self.value


class ReferenceCategory(StrEnum):
    PERSON = "person"
    OUTFIT = "outfit"
    SCENE = "scene"


class AssetStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    NEEDS_REVIEW = "needs_review"
    DELETED = "deleted"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    GENERATED = "generated"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_TASK_STATUSES = frozenset({TaskStatus.SENT, TaskStatus.FAILED, TaskStatus.CANCELLED})


@dataclass(slots=True)
class ReferenceAsset:
    category: ReferenceCategory | str
    name: str
    source_path: Path | str
    reference_path: Path | str
    sha256: str
    id: str = field(default_factory=new_id)
    tags: JsonObject = field(default_factory=dict)
    manual_tags: JsonObject = field(default_factory=dict)
    prompt_version: str = ""
    status: AssetStatus | str = AssetStatus.ACTIVE
    source_task_id: str | None = None
    selection_metadata: JsonObject = field(default_factory=dict)
    use_count: int = 0
    last_used_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        self.category = ReferenceCategory(self.category)
        self.status = AssetStatus(self.status)
        self.source_path = Path(self.source_path)
        self.reference_path = Path(self.reference_path)
        self.sha256 = self.sha256.lower().strip()
        if not self.name.strip():
            raise ValueError("reference asset name must not be empty")
        if not self.sha256:
            raise ValueError("reference asset sha256 must not be empty")
        if self.use_count < 0:
            raise ValueError("reference asset use_count must not be negative")

    @property
    def effective_tags(self) -> JsonObject:
        """Return generated tags with administrator overrides applied."""

        result = dict(self.tags)
        result.update(self.manual_tags)
        return result

    @property
    def is_selectable(self) -> bool:
        return self.status == AssetStatus.ACTIVE and self.deleted_at is None

    def as_selection_metadata(self) -> JsonObject:
        """Return a prompt-safe metadata projection without local paths."""

        return {
            "id": self.id,
            "category": self.category.value,
            "name": self.name,
            "tags": self.effective_tags,
            "use_count": self.use_count,
            "last_used_at": (self.last_used_at.astimezone(timezone.utc).isoformat() if self.last_used_at else None),
            **self.selection_metadata,
        }


@dataclass(slots=True)
class ImageTask:
    kind: str
    scope_key: str
    id: str = field(default_factory=new_id)
    user_id: str | None = None
    stream_id: str | None = None
    group_id: str | None = None
    status: TaskStatus | str = TaskStatus.QUEUED
    prompt_summary: str = ""
    prompt_hash: str = ""
    prompt_version: str = ""
    request: JsonObject = field(default_factory=dict)
    result_path: Path | str | None = None
    result_metadata: JsonObject = field(default_factory=dict)
    error_message: str | None = None
    parent_task_id: str | None = None
    paid_request_started: bool = False
    sent_at: datetime | None = None
    planner_notified_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.status = TaskStatus(self.status)
        if self.result_path is not None:
            self.result_path = Path(self.result_path)
        if not self.kind.strip():
            raise ValueError("task kind must not be empty")
        if not self.scope_key.strip():
            raise ValueError("task scope_key must not be empty")

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_TASK_STATUSES


@dataclass(slots=True)
class TaskReference:
    task_id: str
    role: str
    asset_id: str | None = None
    selection_source: str = "fallback"
    fallback_reason: str | None = None
    selection_metadata: JsonObject = field(default_factory=dict)
    recorded_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("task reference task_id must not be empty")
        if not self.role.strip():
            raise ValueError("task reference role must not be empty")


@dataclass(slots=True)
class GroupContinuity:
    scope_key: str
    local_date: str
    scene_signature: str
    last_photo_at: datetime
    outfit_id: str | None = None
    scene_id: str | None = None
    pinned_outfit_id: str | None = None
    pinned_scene_id: str | None = None
    metadata: JsonObject = field(default_factory=dict)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.scope_key.strip():
            raise ValueError("continuity scope_key must not be empty")
        if not self.local_date.strip():
            raise ValueError("continuity local_date must not be empty")


@dataclass(frozen=True, slots=True)
class ReferenceSelection:
    """The result of gallery-side reference resolution."""

    asset: ReferenceAsset | None
    source: str
    fallback_reason: str | None = None
    candidates: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ContinuityDecision:
    outfit_id: str | None
    scene_id: str | None
    reuse_outfit: bool
    reuse_scene: bool
    outfit_reason: str
    scene_reason: str
