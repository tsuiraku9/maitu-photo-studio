from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from maitu_photo.continuity import (
    ContinuityError,
    ContinuityManager,
    make_scope_key,
    normalize_scene_signature,
)
from maitu_photo.gallery import ReferenceGallery
from maitu_photo.storage import SQLiteStorage


def _setup(tmp_path: Path):
    storage = SQLiteStorage(tmp_path / "continuity.sqlite3")
    gallery = ReferenceGallery(storage)
    outfit = gallery.add_reference(
        category="outfit",
        name="blue dress",
        source_path="source/outfit.jpg",
        reference_path="refs/outfit.jpg",
        sha256="a" * 64,
    )
    scene = gallery.add_reference(
        category="scene",
        name="bedroom",
        source_path="source/scene.jpg",
        reference_path="refs/scene.jpg",
        sha256="b" * 64,
        tags={"privacy_eligible": True},
    )
    return storage, gallery, outfit, scene


def test_scope_key_prefers_group_and_private_stream_fallback() -> None:
    assert make_scope_key(group_id=123, stream_id=456) == "group:123"
    assert make_scope_key(stream_id=" dm-1 ") == "stream:dm-1"
    with pytest.raises(ValueError):
        make_scope_key()
    assert normalize_scene_signature("  My   BEDROOM ") == "my bedroom"


def test_same_scene_same_day_within_ttl_reuses_references(tmp_path: Path) -> None:
    storage, _, outfit, scene = _setup(tmp_path)
    manager = ContinuityManager(storage, ttl_hours=12, timezone_name="Asia/Hong_Kong")
    recorded_at = datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)
    manager.record_photo(
        "group:1",
        "Warm Bedroom",
        outfit_id=outfit.id,
        scene_id=scene.id,
        now=recorded_at,
    )

    decision = manager.decide("group:1", " warm   bedroom ", now=recorded_at + timedelta(hours=11))
    assert decision.outfit_id == outfit.id
    assert decision.scene_id == scene.id
    assert decision.reuse_outfit is True
    assert decision.outfit_reason == "same_scene_same_day_within_ttl"
    assert manager.decide("group:2", "warm bedroom", now=recorded_at).outfit_id is None
    storage.close()


def test_automatic_backfill_binds_only_to_the_current_same_scene_photo(tmp_path: Path) -> None:
    storage, _, outfit, scene = _setup(tmp_path)
    manager = ContinuityManager(storage, ttl_hours=12, timezone_name="Asia/Hong_Kong")
    recorded_at = datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)
    manager.record_photo(
        "group:1",
        "bedroom",
        outfit_id=None,
        scene_id=None,
        metadata={"task_id": "photo-1"},
        now=recorded_at,
    )

    assert manager.attach_backfilled_reference(
        scope_key="group:1",
        parent_task_id="photo-1",
        scene_signature="bedroom",
        category="outfit",
        asset_id=outfit.id,
        backfill_task_id="backfill-1",
    )
    assert manager.attach_backfilled_reference(
        scope_key="group:1",
        parent_task_id="photo-1",
        scene_signature="bedroom",
        category="scene",
        asset_id=scene.id,
        backfill_task_id="backfill-2",
    )
    decision = manager.decide("group:1", "bedroom", now=recorded_at + timedelta(minutes=1))
    assert decision.outfit_id == outfit.id
    assert decision.scene_id == scene.id

    manager.record_photo(
        "group:1",
        "bedroom",
        outfit_id=None,
        scene_id=None,
        metadata={"task_id": "photo-2"},
        now=recorded_at + timedelta(minutes=2),
    )
    assert not manager.attach_backfilled_reference(
        scope_key="group:1",
        parent_task_id="photo-1",
        scene_signature="bedroom",
        category="outfit",
        asset_id=outfit.id,
        backfill_task_id="late-backfill",
    )
    storage.close()


def test_scene_change_ttl_and_local_midnight_break_continuity(tmp_path: Path) -> None:
    storage, _, outfit, scene = _setup(tmp_path)
    manager = ContinuityManager(storage, ttl_hours=12, timezone_name="Asia/Hong_Kong")
    recorded_at = datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)
    manager.record_photo("group:1", "bedroom", outfit_id=outfit.id, scene_id=scene.id, now=recorded_at)
    changed = manager.decide("group:1", "living room", now=recorded_at + timedelta(hours=1))
    assert changed.outfit_id is None
    assert changed.outfit_reason == "scene_changed"

    expired = manager.decide("group:1", "bedroom", now=recorded_at + timedelta(hours=13))
    assert expired.outfit_id is None
    assert expired.outfit_reason == "ttl_expired"

    before_midnight = datetime(2026, 8, 11, 15, 30, tzinfo=timezone.utc)  # 23:30 HKT
    manager.record_photo(
        "group:1",
        "bedroom",
        outfit_id=outfit.id,
        scene_id=scene.id,
        now=before_midnight,
    )
    after_midnight = manager.decide("group:1", "bedroom", now=before_midnight + timedelta(hours=1))
    assert after_midnight.outfit_id is None
    assert after_midnight.outfit_reason == "different_day"
    storage.close()


def test_same_local_day_requirement_can_be_disabled(tmp_path: Path) -> None:
    storage, _, outfit, scene = _setup(tmp_path)
    manager = ContinuityManager(
        storage,
        ttl_hours=12,
        timezone_name="Asia/Hong_Kong",
        same_local_day=False,
    )
    before_midnight = datetime(2026, 8, 11, 15, 30, tzinfo=timezone.utc)
    manager.record_photo(
        "group:1",
        "bedroom",
        outfit_id=outfit.id,
        scene_id=scene.id,
        now=before_midnight,
    )

    decision = manager.decide("group:1", "bedroom", now=before_midnight + timedelta(hours=1))

    assert decision.outfit_id == outfit.id
    assert decision.scene_id == scene.id
    assert decision.outfit_reason == "same_scene_within_ttl"
    storage.close()


def test_force_new_and_admin_pins(tmp_path: Path) -> None:
    storage, gallery, outfit, scene = _setup(tmp_path)
    now = datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)
    manager = ContinuityManager(storage, ttl_hours=12)
    manager.record_photo("group:1", "bedroom", outfit_id=outfit.id, scene_id=scene.id, now=now)
    forced = manager.decide(
        "group:1",
        "bedroom",
        force_new_outfit=True,
        force_new_scene=True,
        now=now + timedelta(minutes=5),
    )
    assert forced.outfit_id is None and forced.outfit_reason == "forced_new"
    assert forced.scene_id is None and forced.scene_reason == "forced_new"

    manager.pin("group:1", "outfit", outfit.id, now=now)
    manager.pin("group:1", "scene", scene.id, now=now)
    pinned = manager.decide(
        "group:1",
        "different place",
        force_new_outfit=True,
        force_new_scene=True,
        now=now + timedelta(days=3),
    )
    assert pinned.outfit_id == outfit.id and pinned.outfit_reason == "pinned"
    assert pinned.scene_id == scene.id and pinned.scene_reason == "pinned"

    gallery.disable(outfit.id)
    unavailable = manager.decide("group:1", "different place", now=now + timedelta(days=3))
    assert unavailable.outfit_id is None
    with pytest.raises(ContinuityError):
        manager.pin("group:2", "outfit", outfit.id)
    storage.close()


def test_reset_preserves_pins_until_unpinned(tmp_path: Path) -> None:
    storage, _, outfit, scene = _setup(tmp_path)
    now = datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)
    manager = ContinuityManager(storage)
    manager.record_photo("group:1", "bedroom", outfit_id=outfit.id, scene_id=scene.id, now=now)
    manager.pin("group:1", "outfit", outfit.id, now=now)
    assert manager.reset("group:1", preserve_pins=True, now=now)
    state = manager.get("group:1")
    assert state is not None
    assert state.outfit_id is None
    assert state.pinned_outfit_id == outfit.id
    manager.unpin("group:1", "outfit")
    assert manager.get("group:1").pinned_outfit_id is None  # type: ignore[union-attr]
    assert manager.reset("group:1", preserve_pins=False)
    assert manager.get("group:1") is None
    storage.close()
