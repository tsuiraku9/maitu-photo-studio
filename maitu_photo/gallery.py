"""Reference gallery lifecycle and deterministic selection helpers."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .models import (
    AssetStatus,
    ReferenceAsset,
    ReferenceCategory,
    ReferenceSelection,
    TaskReference,
    normalize_reference_tags,
)
from .storage import DuplicateRecordError, RecordNotFoundError, SQLiteStorage


class GalleryError(RuntimeError):
    pass


class ReferenceNotFoundError(GalleryError):
    pass


class DuplicateReferenceError(GalleryError):
    def __init__(self, message: str, *, existing_id: str | None = None) -> None:
        super().__init__(message)
        self.existing_id = existing_id


class ReferenceNotSelectableError(GalleryError):
    pass


def file_sha256(path: Path | str, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash a processed reference without loading the whole file into memory."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        while chunk := file.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


class ReferenceGallery:
    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage

    def add_reference(
        self,
        *,
        category: ReferenceCategory | str,
        name: str,
        source_path: Path | str,
        reference_path: Path | str,
        sha256: str | None = None,
        tags: Mapping[str, Any] | None = None,
        manual_tags: Mapping[str, Any] | None = None,
        prompt_version: str = "",
        status: AssetStatus | str = AssetStatus.ACTIVE,
        source_task_id: str | None = None,
        selection_metadata: Mapping[str, Any] | None = None,
        replace_person: bool = False,
    ) -> ReferenceAsset:
        """Add a compressed gallery image.

        Compression belongs to the caller's ingestion pipeline.  Hashing the
        final reference here ensures deduplication reflects bytes sent to the
        image provider, rather than the unprocessed upload.
        """

        category = ReferenceCategory(category)
        reference_path = Path(reference_path)
        source_path = Path(source_path)
        digest = (sha256 or file_sha256(reference_path)).lower().strip()
        existing = self.storage.find_reference_by_hash(category, digest)
        if existing is not None:
            raise DuplicateReferenceError(
                f"duplicate {category.value} reference: {existing.id}",
                existing_id=existing.id,
            )
        asset = ReferenceAsset(
            category=category,
            name=name.strip(),
            source_path=source_path,
            reference_path=reference_path,
            sha256=digest,
            tags=dict(tags or {}),
            manual_tags=dict(manual_tags or {}),
            prompt_version=prompt_version,
            status=status,
            source_task_id=source_task_id,
            selection_metadata=dict(selection_metadata or {}),
        )
        try:
            with self.storage.transaction():
                if category == ReferenceCategory.PERSON:
                    current = self.get_person()
                    if current is not None:
                        if not replace_person:
                            raise DuplicateReferenceError(
                                f"person reference already exists: {current.id}",
                                existing_id=current.id,
                            )
                        self.storage.soft_delete_reference_asset(current.id)
                self.storage.create_reference_asset(asset)
        except DuplicateRecordError as exc:
            duplicate = self.storage.find_reference_by_hash(category, digest)
            raise DuplicateReferenceError(
                f"reference conflicts with an existing {category.value} asset",
                existing_id=duplicate.id if duplicate else None,
            ) from exc
        return asset

    # Friendly alias used by service layers.
    create_asset = add_reference

    def get(self, asset_id: str, *, include_deleted: bool = False) -> ReferenceAsset | None:
        """Return an asset or ``None`` for selector-friendly lookups."""

        return self.storage.get_reference_asset(asset_id, include_deleted=include_deleted)

    def require(self, asset_id: str, *, include_deleted: bool = False) -> ReferenceAsset:
        asset = self.get(asset_id, include_deleted=include_deleted)
        if asset is None:
            raise ReferenceNotFoundError(f"reference asset not found: {asset_id}")
        return asset

    def get_person(self) -> ReferenceAsset | None:
        assets = self.storage.list_reference_assets(category=ReferenceCategory.PERSON, include_deleted=False, limit=1)
        return assets[0] if assets else None

    def list_assets(
        self,
        *,
        category: ReferenceCategory | str | None = None,
        statuses: Iterable[AssetStatus | str] | None = None,
        tag_filters: Mapping[str, Any] | None = None,
        include_deleted: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ReferenceAsset]:
        assets = self.storage.list_reference_assets(
            category=category,
            statuses=statuses,
            include_deleted=include_deleted,
            limit=limit,
            offset=offset,
        )
        if tag_filters:
            assets = [asset for asset in assets if _tags_match(asset, tag_filters)]
        return assets

    def list_candidates(
        self,
        category: ReferenceCategory | str,
        *,
        tag_filters: Mapping[str, Any] | None = None,
        exclude_ids: Iterable[str] = (),
        limit: int = 100,
    ) -> list[ReferenceAsset]:
        excluded = set(exclude_ids)
        return [
            asset
            for asset in self.list_assets(
                category=category,
                statuses=(AssetStatus.ACTIVE,),
                tag_filters=tag_filters,
                limit=limit,
            )
            if asset.id not in excluded and asset.is_selectable
        ]

    def candidates(
        self,
        category: ReferenceCategory | str,
        *,
        include_disabled: bool = False,
        limit: int = 100,
    ) -> list[ReferenceAsset]:
        """Compatibility facade used by the asynchronous selector."""

        statuses: tuple[AssetStatus, ...]
        if include_disabled:
            statuses = (
                AssetStatus.ACTIVE,
                AssetStatus.DISABLED,
                AssetStatus.NEEDS_REVIEW,
            )
        else:
            statuses = (AssetStatus.ACTIVE,)
        assets = self.list_assets(category=category, statuses=statuses, limit=limit)
        return assets if include_disabled else [asset for asset in assets if asset.is_selectable]

    def candidate_metadata(
        self,
        category: ReferenceCategory | str,
        *,
        tag_filters: Mapping[str, Any] | None = None,
        exclude_ids: Iterable[str] = (),
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return [
            asset.as_selection_metadata()
            for asset in self.list_candidates(
                category,
                tag_filters=tag_filters,
                exclude_ids=exclude_ids,
                limit=limit,
            )
        ]

    def edit(
        self,
        asset_id: str,
        *,
        name: str | None = None,
        manual_tags: Mapping[str, Any] | None = None,
        merge_manual_tags: bool = True,
        selection_metadata: Mapping[str, Any] | None = None,
    ) -> ReferenceAsset:
        asset = self.require(asset_id)
        if name is not None:
            if not name.strip():
                raise ValueError("reference asset name must not be empty")
            asset.name = name.strip()
        if manual_tags is not None:
            if merge_manual_tags:
                asset.manual_tags.update(dict(manual_tags))
            else:
                asset.manual_tags = dict(manual_tags)
            asset.manual_tags = normalize_reference_tags(asset.category, asset.manual_tags)
        if selection_metadata is not None:
            asset.selection_metadata.update(dict(selection_metadata))
        return self.storage.update_reference_asset(asset)

    def apply_generated_tags(
        self,
        asset_id: str,
        tags: Mapping[str, Any] | None,
        *,
        valid: bool,
        auto_enable: bool = True,
    ) -> ReferenceAsset:
        asset = self.require(asset_id)
        if valid and tags is not None:
            asset.tags = dict(tags)
            asset.status = AssetStatus.ACTIVE if auto_enable else AssetStatus.DISABLED
        else:
            asset.status = AssetStatus.NEEDS_REVIEW
        return self.storage.update_reference_asset(asset)

    def enable(self, asset_id: str) -> ReferenceAsset:
        return self._set_status(asset_id, AssetStatus.ACTIVE)

    def disable(self, asset_id: str) -> ReferenceAsset:
        return self._set_status(asset_id, AssetStatus.DISABLED)

    def mark_needs_review(self, asset_id: str) -> ReferenceAsset:
        return self._set_status(asset_id, AssetStatus.NEEDS_REVIEW)

    def _set_status(self, asset_id: str, status: AssetStatus) -> ReferenceAsset:
        try:
            return self.storage.set_reference_status(asset_id, status)
        except RecordNotFoundError as exc:
            raise ReferenceNotFoundError(str(exc)) from exc

    def soft_delete(self, asset_id: str) -> ReferenceAsset:
        try:
            return self.storage.soft_delete_reference_asset(asset_id)
        except RecordNotFoundError as exc:
            raise ReferenceNotFoundError(str(exc)) from exc

    def select_reference(
        self,
        category: ReferenceCategory | str,
        *,
        explicit_id: str | None = None,
        continuity_id: str | None = None,
        tag_filters: Mapping[str, Any] | None = None,
        hints: Sequence[str] = (),
        force_new: bool = False,
        exclude_ids: Iterable[str] = (),
        candidate_limit: int = 100,
    ) -> ReferenceSelection:
        """Resolve explicit/continuity choices, then use a stable fallback.

        The MaiBot-facing service can call :meth:`candidate_metadata`, ask its
        utility model to choose an ID, and validate it with
        :meth:`validate_candidate_choice`.  This method is the deterministic
        fallback for model failure.
        """

        category = ReferenceCategory(category)
        excluded = set(exclude_ids)
        if explicit_id:
            asset = self._require_selectable(explicit_id, category)
            if asset.id in excluded:
                raise ReferenceNotSelectableError(f"explicit reference is excluded: {explicit_id}")
            return ReferenceSelection(asset=asset, source="explicit")

        if continuity_id and not force_new and continuity_id not in excluded:
            asset = self.storage.get_reference_asset(continuity_id)
            if asset is not None and asset.category == category and asset.is_selectable:
                return ReferenceSelection(asset=asset, source="continuity")

        candidates = self.list_candidates(
            category,
            tag_filters=tag_filters,
            exclude_ids=excluded,
            limit=candidate_limit,
        )
        metadata = tuple(asset.as_selection_metadata() for asset in candidates)
        if not candidates:
            reason = "no_active_reference"
            if tag_filters:
                reason = "no_matching_reference"
            return ReferenceSelection(
                asset=None,
                source="text_fallback",
                fallback_reason=reason,
                candidates=metadata,
            )

        selected = min(candidates, key=lambda asset: _selection_key(asset, hints))
        return ReferenceSelection(asset=selected, source="deterministic", candidates=metadata)

    # Short alias for callers that already know the return type.
    select = select_reference

    def validate_candidate_choice(
        self,
        category: ReferenceCategory | str,
        asset_id: str,
        candidate_ids: Iterable[str],
    ) -> ReferenceAsset:
        allowed = set(candidate_ids)
        if asset_id not in allowed:
            raise ReferenceNotSelectableError("model selected an ID outside the provided candidate set")
        return self._require_selectable(asset_id, ReferenceCategory(category))

    def _require_selectable(self, asset_id: str, category: ReferenceCategory) -> ReferenceAsset:
        asset = self.storage.get_reference_asset(asset_id)
        if asset is None:
            raise ReferenceNotFoundError(f"reference asset not found: {asset_id}")
        if asset.category != category:
            raise ReferenceNotSelectableError(
                f"reference {asset_id} is {asset.category.value}, expected {category.value}"
            )
        if not asset.is_selectable:
            raise ReferenceNotSelectableError(f"reference {asset_id} is not active ({asset.status.value})")
        return asset

    def record_usage(self, assets: Iterable[ReferenceAsset | str], *, used_at: datetime | None = None) -> None:
        ids = [asset.id if isinstance(asset, ReferenceAsset) else asset for asset in assets]
        self.storage.increment_reference_usage(ids, used_at=used_at)

    def record_task_selection(
        self,
        *,
        task_id: str,
        role: str,
        selection: ReferenceSelection,
    ) -> TaskReference:
        reference = TaskReference(
            task_id=task_id,
            role=role,
            asset_id=selection.asset.id if selection.asset else None,
            selection_source=selection.source,
            fallback_reason=selection.fallback_reason,
            selection_metadata=(selection.asset.as_selection_metadata() if selection.asset else {}),
        )
        return self.storage.record_task_reference(reference)


def _tags_match(asset: ReferenceAsset, filters: Mapping[str, Any]) -> bool:
    tags = asset.effective_tags
    for key, expected in filters.items():
        if key not in tags:
            return False
        expected_values = _value_set(expected)
        actual_values = _value_set(tags[key])
        if expected_values.isdisjoint(actual_values):
            return False
    return True


def _value_set(value: Any) -> set[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        values = value
    else:
        values = (value,)
    return {_normalized_text(item) for item in values}


def _normalized_text(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return " ".join(str(value).casefold().split())


def _selection_key(asset: ReferenceAsset, hints: Sequence[str]) -> tuple[Any, ...]:
    searchable = _normalized_text({"name": asset.name, "tags": asset.effective_tags, **asset.selection_metadata})
    tokens: list[str] = []
    for hint in hints:
        normalized = _normalized_text(hint)
        if normalized:
            tokens.append(normalized)
            tokens.extend(re.findall(r"[\w-]+", normalized, flags=re.UNICODE))
    score = sum(1 for token in set(tokens) if token and token in searchable)
    never_used = asset.last_used_at is None
    last_used = asset.last_used_at.astimezone(timezone.utc).timestamp() if asset.last_used_at else 0.0
    # min() chooses the strongest match, then least-used/least-recent asset.
    return (-score, asset.use_count, not never_used, last_used, asset.created_at, asset.id)
