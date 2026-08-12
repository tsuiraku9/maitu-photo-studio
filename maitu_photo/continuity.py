"""Per-group reference continuity with same-day and TTL constraints."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (
    ContinuityDecision,
    GroupContinuity,
    ReferenceCategory,
    utc_now,
)
from .storage import SQLiteStorage


class ContinuityError(RuntimeError):
    pass


def make_scope_key(*, group_id: str | int | None = None, stream_id: str | int | None = None) -> str:
    """Build an isolation key, preferring a group over a private stream."""

    if group_id is not None and str(group_id).strip():
        return f"group:{str(group_id).strip()}"
    if stream_id is not None and str(stream_id).strip():
        return f"stream:{str(stream_id).strip()}"
    raise ValueError("group_id or stream_id is required")


def normalize_scene_signature(value: str) -> str:
    return " ".join(value.casefold().split())


class ContinuityManager:
    def __init__(
        self,
        storage: SQLiteStorage,
        *,
        ttl: timedelta | None = None,
        ttl_hours: float = 12.0,
        timezone_name: str = "Asia/Hong_Kong",
        same_local_day: bool = True,
        now_factory: Callable[[], datetime] = utc_now,
    ) -> None:
        if ttl is None:
            ttl = timedelta(hours=ttl_hours)
        if ttl.total_seconds() <= 0:
            raise ValueError("continuity TTL must be positive")
        zone = _load_timezone(timezone_name)
        self.storage = storage
        self.ttl = ttl
        self.timezone_name = timezone_name
        self.timezone = zone
        self.same_local_day = bool(same_local_day)
        self._now_factory = now_factory

    @staticmethod
    def scope_key(*, group_id: str | int | None = None, stream_id: str | int | None = None) -> str:
        return make_scope_key(group_id=group_id, stream_id=stream_id)

    def get(self, scope_key: str) -> GroupContinuity | None:
        return self.storage.get_continuity(scope_key)

    def get_reusable(
        self,
        scope_key: str,
        *,
        scene_signature: str | None = None,
        now: datetime | None = None,
    ) -> GroupContinuity | None:
        """Return a current state for callers that resolve references in stages.

        Passing ``scene_signature`` is preferred because outfit continuity is
        scene-sensitive.  When omitted, only the same-day TTL is checked and a
        later selection stage must still validate the resulting scene.
        """

        state = self.storage.get_continuity(scope_key)
        if state is None:
            return None
        now = self._aware_utc(now or self._now_factory())
        local_date = now.astimezone(self.timezone).date().isoformat()
        elapsed = now - self._aware_utc(state.last_photo_at)
        if (self.same_local_day and state.local_date != local_date) or not timedelta(0) <= elapsed <= self.ttl:
            return None
        if scene_signature is not None and normalize_scene_signature(
            state.scene_signature
        ) != normalize_scene_signature(scene_signature):
            return None
        return state

    def decide(
        self,
        scope_key: str,
        scene_signature: str,
        *,
        force_new_outfit: bool = False,
        force_new_scene: bool = False,
        now: datetime | None = None,
    ) -> ContinuityDecision:
        """Return reusable IDs; explicit tool parameters are handled upstream."""

        state = self.storage.get_continuity(scope_key)
        if state is None:
            return ContinuityDecision(
                outfit_id=None,
                scene_id=None,
                reuse_outfit=False,
                reuse_scene=False,
                outfit_reason="no_continuity",
                scene_reason="no_continuity",
            )

        now = self._aware_utc(now or self._now_factory())
        signature = normalize_scene_signature(scene_signature)
        current = self._is_current(state, signature, now)

        pinned_outfit = self._active_reference(state.pinned_outfit_id, ReferenceCategory.OUTFIT)
        if pinned_outfit:
            outfit_id = pinned_outfit
            reuse_outfit = True
            outfit_reason = "pinned"
        elif force_new_outfit:
            outfit_id = None
            reuse_outfit = False
            outfit_reason = "forced_new"
        elif current and self._active_reference(state.outfit_id, ReferenceCategory.OUTFIT):
            outfit_id = state.outfit_id
            reuse_outfit = True
            outfit_reason = self._reuse_reason()
        else:
            outfit_id = None
            reuse_outfit = False
            outfit_reason = self._miss_reason(state, signature, now, state.outfit_id)

        pinned_scene = self._active_reference(state.pinned_scene_id, ReferenceCategory.SCENE)
        if pinned_scene:
            scene_id = pinned_scene
            reuse_scene = True
            scene_reason = "pinned"
        elif force_new_scene:
            scene_id = None
            reuse_scene = False
            scene_reason = "forced_new"
        elif current and self._active_reference(state.scene_id, ReferenceCategory.SCENE):
            scene_id = state.scene_id
            reuse_scene = True
            scene_reason = self._reuse_reason()
        else:
            scene_id = None
            reuse_scene = False
            scene_reason = self._miss_reason(state, signature, now, state.scene_id)

        return ContinuityDecision(
            outfit_id=outfit_id,
            scene_id=scene_id,
            reuse_outfit=reuse_outfit,
            reuse_scene=reuse_scene,
            outfit_reason=outfit_reason,
            scene_reason=scene_reason,
        )

    # Semantic alias used by photo selection services.
    resolve = decide

    def record_photo(
        self,
        scope_key: str,
        scene_signature: str,
        *,
        outfit_id: str | None,
        scene_id: str | None,
        metadata: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> GroupContinuity:
        now = self._aware_utc(now or self._now_factory())
        previous = self.storage.get_continuity(scope_key)
        combined_metadata = dict(previous.metadata if previous else {})
        combined_metadata.update(dict(metadata or {}))
        state = GroupContinuity(
            scope_key=scope_key,
            local_date=now.astimezone(self.timezone).date().isoformat(),
            scene_signature=normalize_scene_signature(scene_signature),
            last_photo_at=now,
            outfit_id=outfit_id,
            scene_id=scene_id,
            pinned_outfit_id=previous.pinned_outfit_id if previous else None,
            pinned_scene_id=previous.pinned_scene_id if previous else None,
            metadata=combined_metadata,
        )
        return self.storage.upsert_continuity(state)

    def pin(
        self,
        scope_key: str,
        category: ReferenceCategory | str,
        asset_id: str,
        *,
        now: datetime | None = None,
    ) -> GroupContinuity:
        category = ReferenceCategory(category)
        if category not in (ReferenceCategory.OUTFIT, ReferenceCategory.SCENE):
            raise ValueError("only outfit and scene references can be pinned")
        if not self._active_reference(asset_id, category):
            raise ContinuityError(f"cannot pin inactive or mismatched {category.value} reference: {asset_id}")
        state = self._state_for_update(scope_key, now)
        if category == ReferenceCategory.OUTFIT:
            state.pinned_outfit_id = asset_id
        else:
            state.pinned_scene_id = asset_id
        return self.storage.upsert_continuity(state)

    def unpin(self, scope_key: str, category: ReferenceCategory | str | None = None) -> GroupContinuity | None:
        state = self.storage.get_continuity(scope_key)
        if state is None:
            return None
        if category is None:
            state.pinned_outfit_id = None
            state.pinned_scene_id = None
        else:
            category = ReferenceCategory(category)
            if category == ReferenceCategory.OUTFIT:
                state.pinned_outfit_id = None
            elif category == ReferenceCategory.SCENE:
                state.pinned_scene_id = None
            else:
                raise ValueError("only outfit and scene references can be unpinned")
        return self.storage.upsert_continuity(state)

    def reset(
        self,
        scope_key: str,
        *,
        preserve_pins: bool = True,
        now: datetime | None = None,
    ) -> bool:
        state = self.storage.get_continuity(scope_key)
        if state is None:
            return False
        if not preserve_pins:
            return self.storage.delete_continuity(scope_key)
        now = self._aware_utc(now or self._now_factory())
        state.local_date = now.astimezone(self.timezone).date().isoformat()
        state.scene_signature = ""
        state.last_photo_at = now
        state.outfit_id = None
        state.scene_id = None
        state.metadata = {}
        self.storage.upsert_continuity(state)
        return True

    clear = reset

    def _state_for_update(self, scope_key: str, now: datetime | None) -> GroupContinuity:
        state = self.storage.get_continuity(scope_key)
        if state is not None:
            return state
        current = self._aware_utc(now or self._now_factory())
        return GroupContinuity(
            scope_key=scope_key,
            local_date=current.astimezone(self.timezone).date().isoformat(),
            scene_signature="",
            last_photo_at=current,
        )

    def _active_reference(self, asset_id: str | None, category: ReferenceCategory) -> str | None:
        if not asset_id:
            return None
        asset = self.storage.get_reference_asset(asset_id)
        if asset is None or asset.category != category or not asset.is_selectable:
            return None
        return asset.id

    def _is_current(self, state: GroupContinuity, signature: str, now: datetime) -> bool:
        local_date = now.astimezone(self.timezone).date().isoformat()
        elapsed = now - self._aware_utc(state.last_photo_at)
        return (
            (not self.same_local_day or state.local_date == local_date)
            and normalize_scene_signature(state.scene_signature) == signature
            and timedelta(0) <= elapsed <= self.ttl
        )

    def _miss_reason(
        self,
        state: GroupContinuity,
        signature: str,
        now: datetime,
        asset_id: str | None,
    ) -> str:
        if not asset_id:
            return "no_previous_reference"
        if self.same_local_day and state.local_date != now.astimezone(self.timezone).date().isoformat():
            return "different_day"
        if normalize_scene_signature(state.scene_signature) != signature:
            return "scene_changed"
        elapsed = now - self._aware_utc(state.last_photo_at)
        if elapsed < timedelta(0):
            return "clock_moved_backwards"
        if elapsed > self.ttl:
            return "ttl_expired"
        return "reference_unavailable"

    def _reuse_reason(self) -> str:
        return "same_scene_same_day_within_ttl" if self.same_local_day else "same_scene_within_ttl"

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


_FIXED_ZONE_FALLBACKS = {
    # Windows Python installations commonly omit the optional IANA tzdata
    # package.  These zones have fixed modern offsets and keep the plugin's
    # defaults operational; Docker/Linux continues to use full ZoneInfo data.
    "Asia/Hong_Kong": 8,
    "Asia/Shanghai": 8,
    "Asia/Taipei": 8,
    "Asia/Singapore": 8,
    "Asia/Tokyo": 9,
    "UTC": 0,
    "Etc/UTC": 0,
}


def _load_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        if name in _FIXED_ZONE_FALLBACKS:
            return timezone(timedelta(hours=_FIXED_ZONE_FALLBACKS[name]), name)
        match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", name)
        if match:
            sign = 1 if match.group(1) == "+" else -1
            hours = int(match.group(2))
            minutes = int(match.group(3))
            if hours <= 23 and minutes <= 59:
                return timezone(sign * timedelta(hours=hours, minutes=minutes), name)
        raise ValueError(f"unknown timezone: {name}; install tzdata or use an offset such as +08:00") from exc
