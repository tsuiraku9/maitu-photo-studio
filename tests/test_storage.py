from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from maitu_photo.gallery import DuplicateReferenceError, ReferenceGallery
from maitu_photo.models import (
    AssetStatus,
    ImageTask,
    TaskReference,
    TaskStatus,
)
from maitu_photo.storage import SQLiteStorage


def _gallery(tmp_path: Path) -> tuple[SQLiteStorage, ReferenceGallery]:
    storage = SQLiteStorage(tmp_path / "data" / "maitu.sqlite3")
    return storage, ReferenceGallery(storage)


def _add(
    gallery: ReferenceGallery,
    category: str,
    name: str,
    digest_char: str,
    **kwargs: object,
):
    return gallery.add_reference(
        category=category,
        name=name,
        source_path=f"source/{name}.jpg",
        reference_path=f"references/{name}.jpg",
        sha256=digest_char * 64,
        **kwargs,
    )


def test_storage_uses_wal_and_persists_assets(tmp_path: Path) -> None:
    storage, gallery = _gallery(tmp_path)
    assert storage.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    outfit = _add(
        gallery,
        "outfit",
        "linen dress",
        "a",
        tags={"type": "dress", "season": ["spring", "summer"]},
        manual_tags={"style": "quiet"},
        selection_metadata={"source": "admin_extract"},
    )
    storage.close()

    with SQLiteStorage(tmp_path / "data" / "maitu.sqlite3") as reopened:
        loaded = reopened.get_reference_asset(outfit.id)
        assert loaded is not None
        assert loaded.effective_tags == {
            "type": "dress",
            "season": ["spring", "summer"],
            "style": "quiet",
        }
        assert loaded.as_selection_metadata()["source"] == "admin_extract"


def test_gallery_enforces_person_singleton_and_category_hash_dedup(
    tmp_path: Path,
) -> None:
    storage, gallery = _gallery(tmp_path)
    first = _add(gallery, "person", "first", "1")

    with pytest.raises(DuplicateReferenceError) as conflict:
        _add(gallery, "person", "second", "2")
    assert conflict.value.existing_id == first.id

    second = _add(gallery, "person", "second", "2", replace_person=True)
    assert gallery.get_person().id == second.id  # type: ignore[union-attr]
    replaced = storage.get_reference_asset(first.id, include_deleted=True)
    assert replaced is not None
    assert replaced.status == AssetStatus.DELETED
    assert replaced.deleted_at is not None

    outfit = _add(gallery, "outfit", "dress", "3")
    with pytest.raises(DuplicateReferenceError) as duplicate:
        _add(gallery, "outfit", "same bytes", "3")
    assert duplicate.value.existing_id == outfit.id

    gallery.soft_delete(outfit.id)
    replacement = _add(gallery, "outfit", "replacement", "3")
    assert replacement.id != outfit.id
    storage.close()


def test_gallery_tags_status_and_selection_priority(tmp_path: Path) -> None:
    storage, gallery = _gallery(tmp_path)
    summer = _add(
        gallery,
        "outfit",
        "summer dress",
        "4",
        tags={"type": "dress", "season": ["summer"]},
    )
    winter = _add(
        gallery,
        "outfit",
        "winter coat",
        "5",
        tags={"type": "coat", "season": ["winter"]},
    )

    explicit = gallery.select_reference("outfit", explicit_id=winter.id)
    assert explicit.asset.id == winter.id  # type: ignore[union-attr]
    assert explicit.source == "explicit"

    continuity = gallery.select_reference("outfit", continuity_id=summer.id)
    assert continuity.asset.id == summer.id  # type: ignore[union-attr]
    assert continuity.source == "continuity"

    filtered = gallery.select_reference("outfit", tag_filters={"season": "winter"}, hints=("winter",))
    assert filtered.asset.id == winter.id  # type: ignore[union-attr]

    gallery.apply_generated_tags(winter.id, None, valid=False)
    assert gallery.get(winter.id).status == AssetStatus.NEEDS_REVIEW  # type: ignore[union-attr]
    fallback = gallery.select_reference("outfit", tag_filters={"season": "winter"})
    assert fallback.asset is None
    assert fallback.fallback_reason == "no_matching_reference"

    gallery.edit(summer.id, manual_tags={"season": "autumn"})
    assert gallery.get(summer.id).effective_tags["season"] == "autumn"  # type: ignore[union-attr]
    storage.close()


def test_task_queue_reference_audit_and_interruption_recovery(tmp_path: Path) -> None:
    storage, gallery = _gallery(tmp_path)
    outfit = _add(gallery, "outfit", "task outfit", "6")
    first = storage.create_task(
        ImageTask(
            kind="photo",
            scope_key="group:10",
            prompt_summary="portrait",
            prompt_hash="f" * 64,
            request={"size": "1024x1024"},
        )
    )
    storage.record_task_reference(
        TaskReference(
            task_id=first.id,
            role="outfit",
            asset_id=outfit.id,
            selection_source="explicit",
            selection_metadata={"id": outfit.id},
        )
    )
    storage.record_task_reference(
        TaskReference(
            task_id=first.id,
            role="scene",
            selection_source="text_fallback",
            fallback_reason="no_matching_reference",
        )
    )
    claimed = storage.claim_next_task()
    assert claimed is not None and claimed.id == first.id
    assert claimed.status == TaskStatus.RUNNING
    storage.mark_task_request_started(first.id)

    second = storage.create_task(ImageTask(kind="image", scope_key="group:10"))
    second_claimed = storage.claim_next_task()
    assert second_claimed is not None and second_claimed.id == second.id

    generated = storage.create_task(
        ImageTask(
            kind="image",
            scope_key="group:10",
            status=TaskStatus.GENERATED,
            result_path="results/generated.png",
        )
    )
    requeued, failed = storage.recover_interrupted_tasks()
    assert set(requeued) == {second.id, generated.id}
    assert failed == [first.id]
    assert storage.get_task(first.id).status == TaskStatus.FAILED  # type: ignore[union-attr]
    assert storage.get_task(second.id).status == TaskStatus.QUEUED  # type: ignore[union-attr]
    assert storage.get_task(generated.id).status == TaskStatus.QUEUED  # type: ignore[union-attr]

    refs = storage.list_task_references(first.id)
    assert [(ref.role, ref.asset_id) for ref in refs] == [
        ("outfit", outfit.id),
        ("scene", None),
    ]
    storage.close()


def test_running_paid_task_with_saved_result_resumes_delivery(tmp_path: Path) -> None:
    storage, _ = _gallery(tmp_path)
    task = storage.create_task(ImageTask(kind="image", scope_key="group:10"))
    claimed = storage.claim_next_task()
    assert claimed is not None and claimed.id == task.id
    storage.mark_task_request_started(task.id)
    storage.set_task_status(
        task.id,
        TaskStatus.RUNNING,
        result_path="results/already-generated.png",
    )

    requeued, failed = storage.recover_interrupted_tasks()

    assert requeued == [task.id]
    assert failed == []
    resumed = storage.get_task(task.id)
    assert resumed is not None
    assert resumed.status == TaskStatus.QUEUED
    assert resumed.result_path == Path("results/already-generated.png")
    storage.close()


def test_usage_updates_are_deduplicated(tmp_path: Path) -> None:
    storage, gallery = _gallery(tmp_path)
    asset = _add(gallery, "scene", "bedroom", "7")
    used_at = datetime(2026, 8, 11, 3, 0, tzinfo=timezone.utc)
    gallery.record_usage([asset.id, asset.id], used_at=used_at)
    loaded = gallery.get(asset.id)
    assert loaded is not None
    assert loaded.use_count == 1
    assert loaded.last_used_at == used_at
    storage.close()
