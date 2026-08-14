"""Reference ingestion, extraction, tagging, and regeneration services.

The service is deliberately independent from MaiBot component classes.  The
plugin layer injects the configured image provider, MaiBot LLM adapter, and
gallery, while this module enforces the filesystem and metadata invariants for
every reference ingestion path.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from .compression import (
    CompressionConfig,
    CompressionResult,
    ImageSource,
    compress_image,
)
from .gallery import DuplicateReferenceError, ReferenceGallery
from .llm_adapter import MaiBotLLMAdapter
from .models import AssetStatus, ReferenceAsset, ReferenceCategory
from .provider import GeneratedImage, OpenAICompatibleProvider

MAX_REFERENCE_TARGET_BYTES = 480_000


class ReferenceServiceError(RuntimeError):
    """Base error for reference operations."""


class ReferenceTagValidationError(ReferenceServiceError):
    """Raised by the public validation helper for an invalid tag object."""

    def __init__(self, errors: tuple[str, ...]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


class ReferenceGenerationError(ReferenceServiceError):
    """Raised without upstream response text when reference generation fails."""


@dataclass(frozen=True, slots=True)
class ReferencePrompts:
    """Configurable extraction and tagging prompts.

    Defaults state the required contact-sheet layouts explicitly.  Deployments
    normally replace these values with ``PhotoPluginConfig.prompts`` values.
    """

    extract_person: str = (
        "Create a clean 3x2 face-identity reference sheet from the input image. "
        "The left column is one front-facing head-and-shoulders portrait spanning both rows. "
        "The four cells on the right show face front, left profile, right profile, "
        "and back-of-head / hairline detail. Preserve identity, facial structure, "
        "hairstyle, skin tone, and apparent age. Do not copy clothing or accessories "
        "from the source. Use a plain fitted base layer only. Do not add labels, text, "
        "or watermarks."
    )
    generate_person_from_personality: str = (
        "Create a clean 3x2 face-identity reference sheet from the bot personality "
        "description only. The left column is one front-facing head-and-shoulders "
        "portrait spanning both rows. The four cells on the right show face front, "
        "left profile, right profile, and back-of-head / hairline detail. Infer only "
        "face, hairstyle, skin tone, and apparent age. Do not invent a full outfit or "
        "accessories. Use a plain fitted base layer only. Do not add labels, text, or "
        "watermarks.\nNickname: {nickname}\nPersonality: {personality}\n"
        "Appearance hint: {appearance_hint}"
    )
    extract_outfit: str = (
        "Extract the outfit from the input image and create a clean 2x2 outfit "
        "reference sheet showing front, side, back, and material/detail views. "
        "Keep garment colour, material, cut, and accessories consistent. Do not "
        "add labels, text, or watermarks."
    )
    extract_scene: str = (
        "Extract only the private indoor small-space scene from the input and "
        "create a clean 2x2 scene reference sheet: wide view, left view, right "
        "view, and a floor plan. Remove all people and text. Bedrooms, bathrooms, "
        "and living rooms are eligible; cafes and other public places are not."
    )
    tag_person: str = (
        'Return only JSON with exactly this schema: {"appearance_summary":"","confidence":0}. '
        "appearance_summary must describe only face, hairstyle, skin tone, and apparent age; "
        "never clothing or accessories."
    )
    tag_outfit: str = (
        "Return only JSON with exactly this schema: "
        '{"type":"","wearing_scenes":[],"seasons":[],"styles":[],'
        '"confidence":0}'
    )
    tag_scene: str = (
        "Return only JSON with exactly this schema: "
        '{"room_type":"","privacy_eligible":false,"scene_signature":"","confidence":0}'
    )

    def __post_init__(self) -> None:
        for field_name in (
            "extract_person",
            "generate_person_from_personality",
            "extract_outfit",
            "extract_scene",
            "tag_person",
            "tag_outfit",
            "tag_scene",
        ):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} prompt must not be empty")

    def extraction_for(self, category: ReferenceCategory) -> str:
        return {
            ReferenceCategory.PERSON: self.extract_person,
            ReferenceCategory.OUTFIT: self.extract_outfit,
            ReferenceCategory.SCENE: self.extract_scene,
        }[category]

    def tagging_for(self, category: ReferenceCategory) -> str:
        return {
            ReferenceCategory.PERSON: self.tag_person,
            ReferenceCategory.OUTFIT: self.tag_outfit,
            ReferenceCategory.SCENE: self.tag_scene,
        }[category]


@dataclass(frozen=True, slots=True)
class ReferenceServiceConfig:
    compression: CompressionConfig = field(default_factory=CompressionConfig)
    prompts: ReferencePrompts = field(default_factory=ReferencePrompts)
    auto_enable_generated_references: bool = True
    tagging_task_name: str = "vlm"
    tagging_temperature: float | None = 0.1
    tagging_max_tokens: int | None = 2048
    reference_model: str = ""
    reference_mode: str = ""
    extraction_size: str = ""
    prompt_version: str = ""

    def __post_init__(self) -> None:
        if self.compression.target_bytes > MAX_REFERENCE_TARGET_BYTES:
            raise ValueError(f"reference compression target must not exceed {MAX_REFERENCE_TARGET_BYTES} bytes")
        if not self.tagging_task_name.strip():
            raise ValueError("tagging_task_name must not be empty")
        if self.tagging_max_tokens is not None and self.tagging_max_tokens <= 0:
            raise ValueError("tagging_max_tokens must be positive")


@dataclass(frozen=True, slots=True)
class TagValidationResult:
    tags: dict[str, Any]
    schema_valid: bool
    selectable: bool
    error_code: str | None = None
    errors: tuple[str, ...] = ()


_TAG_FIELDS: dict[ReferenceCategory, tuple[str, ...]] = {
    ReferenceCategory.PERSON: (
        "appearance_summary",
        "confidence",
    ),
    ReferenceCategory.OUTFIT: (
        "type",
        "wearing_scenes",
        "seasons",
        "styles",
        "confidence",
    ),
    ReferenceCategory.SCENE: (
        "room_type",
        "privacy_eligible",
        "scene_signature",
        "confidence",
    ),
}

_LIST_FIELDS: dict[ReferenceCategory, frozenset[str]] = {
    ReferenceCategory.PERSON: frozenset(),
    ReferenceCategory.OUTFIT: frozenset({"wearing_scenes", "seasons", "styles"}),
    ReferenceCategory.SCENE: frozenset(),
}

_TEXT_FIELDS: dict[ReferenceCategory, frozenset[str]] = {
    ReferenceCategory.PERSON: frozenset({"appearance_summary"}),
    ReferenceCategory.OUTFIT: frozenset({"type"}),
    ReferenceCategory.SCENE: frozenset({"room_type", "scene_signature"}),
}


def _normalize_legacy_tags(
    category: ReferenceCategory,
    value: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Drop retired fields that do not belong in reference metadata."""

    retired_fields = {
        ReferenceCategory.PERSON: frozenset({"accessories"}),
        ReferenceCategory.SCENE: frozenset({"light", "lighting", "time_of_day"}),
    }.get(category, frozenset())
    if not retired_fields or not any(key in value for key in retired_fields):
        return value
    return {key: item for key, item in value.items() if key not in retired_fields}


def validate_reference_tags(
    category: ReferenceCategory | str,
    value: Mapping[str, Any] | object,
) -> TagValidationResult:
    """Validate and normalize model tags against the category's exact schema."""

    category = ReferenceCategory(category)
    if not isinstance(value, Mapping):
        return TagValidationResult(
            tags={},
            schema_valid=False,
            selectable=False,
            error_code="tag_schema_invalid",
            errors=("tags must be a JSON object",),
        )

    value = _normalize_legacy_tags(category, value)
    expected = set(_TAG_FIELDS[category])
    actual = {key for key in value if isinstance(key, str)}
    errors: list[str] = []
    if len(actual) != len(value):
        errors.append("all tag field names must be strings")
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        errors.append("missing fields: " + ", ".join(missing))
    if extra:
        errors.append("unexpected fields: " + ", ".join(extra))

    normalized: dict[str, Any] = {}
    for key in _TAG_FIELDS[category]:
        if key not in value:
            continue
        item = value[key]
        if key in _LIST_FIELDS[category]:
            if not isinstance(item, list) or any(not isinstance(entry, str) or not entry.strip() for entry in item):
                errors.append(f"{key} must be an array of non-empty strings")
            else:
                normalized[key] = list(dict.fromkeys(entry.strip() for entry in item))
        elif key in _TEXT_FIELDS[category]:
            if not isinstance(item, str) or not item.strip():
                errors.append(f"{key} must be a non-empty string")
            else:
                normalized[key] = item.strip()
        elif key == "privacy_eligible":
            if not isinstance(item, bool):
                errors.append("privacy_eligible must be a boolean")
            else:
                normalized[key] = item
        elif key == "confidence":
            if isinstance(item, bool) or not isinstance(item, (int, float)) or not 0 <= float(item) <= 1:
                errors.append("confidence must be a number between 0 and 1")
            else:
                normalized[key] = float(item)

    if errors:
        return TagValidationResult(
            # Invalid model output may contain non-JSON values or non-string
            # keys.  Keep only the validation diagnostics in metadata so a
            # malformed response can never prevent creation of the review item.
            tags={},
            schema_valid=False,
            selectable=False,
            error_code="tag_schema_invalid",
            errors=tuple(errors),
        )

    tags = {key: normalized[key] for key in _TAG_FIELDS[category]}
    if category == ReferenceCategory.SCENE and not tags["privacy_eligible"]:
        return TagValidationResult(
            tags=tags,
            schema_valid=True,
            selectable=False,
            error_code="scene_not_private",
            errors=("scene is not eligible for private-space reference use",),
        )
    return TagValidationResult(tags=tags, schema_valid=True, selectable=True)


def require_valid_reference_tags(
    category: ReferenceCategory | str,
    value: Mapping[str, Any] | object,
) -> dict[str, Any]:
    """Return normalized tags or raise for callers that need hard validation."""

    result = validate_reference_tags(category, value)
    if not result.selectable:
        raise ReferenceTagValidationError(result.errors)
    return result.tags


class ReferenceService:
    """Own the full lifecycle for compressed reference assets."""

    def __init__(
        self,
        *,
        gallery: ReferenceGallery,
        provider: OpenAICompatibleProvider | None,
        llm: MaiBotLLMAdapter,
        data_dir: Path | str,
        config: ReferenceServiceConfig | None = None,
        before_provider_request: Callable[[], None] | None = None,
    ) -> None:
        self.gallery = gallery
        self.provider = provider
        self.llm = llm
        self.data_dir = Path(data_dir).resolve()
        self.config = config or ReferenceServiceConfig()
        self._before_provider_request = before_provider_request
        self._mutation_lock = asyncio.Lock()

    async def import_reference(
        self,
        category: ReferenceCategory | str,
        source: ImageSource,
        *,
        name: str,
        replace_person: bool = False,
        manual_tags: Mapping[str, Any] | None = None,
        source_task_id: str | None = None,
        prompt_version: str = "",
    ) -> ReferenceAsset:
        """Import an already prepared reference image.

        The uploaded bytes are still normalized twice through the common
        compression pipeline: once for the retained source and once for the
        selectable reference artifact.
        """

        category = self._validate_request(category, name, replace_person)
        source_result = await self._compress(source)
        reference_result = await self._compress(source_result.data)
        return await self._persist_new(
            category=category,
            name=name,
            source_result=source_result,
            reference_result=reference_result,
            replace_person=replace_person,
            manual_tags=manual_tags,
            source_task_id=source_task_id,
            prompt_version=prompt_version or self._prompt_version("import", self.config.prompts.tagging_for(category)),
            source_kind="admin_import",
        )

    async def generate_person_from_personality(
        self,
        *,
        name: str,
        personality: str,
        nickname: str = "",
        appearance_hint: str = "",
        replace_person: bool = False,
        source_task_id: str | None = None,
        generation_prompt: str = "",
        prompt_version: str = "",
        size: str = "",
    ) -> ReferenceAsset:
        """Create a face-only person board from MaiBot personality text."""

        category = self._validate_request(ReferenceCategory.PERSON, name, replace_person)
        prompt_template = generation_prompt or self.config.prompts.generate_person_from_personality
        prompt = (
            prompt_template.replace("{nickname}", nickname.strip() or "bot")
            .replace("{personality}", personality.strip())
            .replace("{appearance_hint}", appearance_hint.strip() or "无")
        )
        generated = await self._generate_reference(
            None,
            prompt=prompt,
            size=size,
        )
        reference_result = await self._compress(generated.data)
        return await self._persist_new(
            category=category,
            name=name,
            source_result=reference_result,
            reference_result=reference_result,
            replace_person=replace_person,
            manual_tags=None,
            source_task_id=source_task_id,
            prompt_version=prompt_version or self._prompt_version("personality", prompt),
            source_kind="personality_generate",
            provider_media_type=generated.media_type,
        )

    async def extract_reference(
        self,
        category: ReferenceCategory | str,
        source: ImageSource,
        *,
        name: str,
        replace_person: bool = False,
        manual_tags: Mapping[str, Any] | None = None,
        source_task_id: str | None = None,
        extraction_prompt: str = "",
        prompt_version: str = "",
        size: str = "",
    ) -> ReferenceAsset:
        """Generate the category's multi-angle reference board from an upload."""

        category = self._validate_request(category, name, replace_person)
        source_result = await self._compress(source)
        prompt = extraction_prompt or self.config.prompts.extraction_for(category)
        generated = await self._generate_reference(
            source_result.data,
            prompt=prompt,
            size=size,
        )
        reference_result = await self._compress(generated.data)
        return await self._persist_new(
            category=category,
            name=name,
            source_result=source_result,
            reference_result=reference_result,
            replace_person=replace_person,
            manual_tags=manual_tags,
            source_task_id=source_task_id,
            prompt_version=prompt_version or self._prompt_version("extract", prompt),
            source_kind="model_extract",
            provider_media_type=generated.media_type,
        )

    async def retag_reference(
        self,
        asset_id: str,
        *,
        tagging_prompt: str = "",
    ) -> ReferenceAsset:
        """Re-run strict visual tagging for an existing reference."""

        asset = self.gallery.require(asset_id)
        reference_result = await self._compress(asset.reference_path)
        prompt = tagging_prompt or self.config.prompts.tagging_for(asset.category)
        tagging = await self._tag(asset.category, reference_result.data, prompt=prompt)
        updated = self.gallery.apply_generated_tags(
            asset.id,
            tagging.tags,
            valid=tagging.selectable,
            auto_enable=self.config.auto_enable_generated_references,
        )
        updated.selection_metadata.update(self._tagging_metadata(tagging, reference_result))
        updated.prompt_version = self._prompt_version("retag", prompt)
        return self.gallery.storage.update_reference_asset(updated)

    async def regenerate_reference(
        self,
        asset_id: str,
        *,
        name: str = "",
        extraction_prompt: str = "",
        prompt_version: str = "",
        size: str = "",
        source_task_id: str | None = None,
    ) -> ReferenceAsset:
        """Regenerate an asset from its retained source and replace its record.

        A new asset ID is created so historical task references remain an
        accurate audit trail.  The old record is soft-deleted only after the
        replacement has been created successfully.
        """

        old = self.gallery.require(asset_id)
        source_result = await self._compress(old.source_path)
        prompt = extraction_prompt or self.config.prompts.extraction_for(old.category)
        generated = await self._generate_reference(
            source_result.data,
            prompt=prompt,
            size=size,
        )
        reference_result = await self._compress(generated.data)

        if reference_result.sha256 == old.sha256:
            tagging = await self._tag(
                old.category,
                reference_result.data,
                prompt=self.config.prompts.tagging_for(old.category),
            )
            updated = self.gallery.apply_generated_tags(
                old.id,
                tagging.tags,
                valid=tagging.selectable,
                auto_enable=self.config.auto_enable_generated_references,
            )
            updated.selection_metadata.update(self._tagging_metadata(tagging, reference_result))
            updated.prompt_version = prompt_version or self._prompt_version("regenerate", prompt)
            return self.gallery.storage.update_reference_asset(updated)

        replacement = await self._persist_new(
            category=old.category,
            name=name or old.name,
            source_result=source_result,
            reference_result=reference_result,
            replace_person=old.category == ReferenceCategory.PERSON,
            manual_tags=old.manual_tags,
            source_task_id=source_task_id or old.source_task_id,
            prompt_version=prompt_version or self._prompt_version("regenerate", prompt),
            source_kind="model_regenerate",
            provider_media_type=generated.media_type,
            replaces_asset_id=old.id,
        )
        return replacement

    def _validate_request(
        self,
        category: ReferenceCategory | str,
        name: str,
        replace_person: bool,
    ) -> ReferenceCategory:
        category = ReferenceCategory(category)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("reference name must not be empty")
        if category == ReferenceCategory.PERSON:
            current = self.gallery.get_person()
            if current is not None and not replace_person:
                raise DuplicateReferenceError(
                    f"person reference already exists: {current.id}",
                    existing_id=current.id,
                )
        return category

    async def _compress(self, source: ImageSource) -> CompressionResult:
        return await asyncio.to_thread(
            compress_image,
            source,
            config=self.config.compression,
        )

    async def _generate_reference(
        self,
        source_bytes: bytes | None,
        *,
        prompt: str,
        size: str,
    ) -> GeneratedImage:
        if not prompt.strip():
            raise ValueError("reference extraction prompt must not be empty")
        if self.provider is None:
            raise ReferenceGenerationError("reference image provider is not configured")
        kwargs: dict[str, Any] = {
            "extraction": True,
        }
        if source_bytes is not None:
            kwargs["images"] = [source_bytes]
        if self.config.reference_model:
            kwargs["model"] = self.config.reference_model
        if self.config.reference_mode:
            kwargs["mode"] = self.config.reference_mode
        selected_size = size or self.config.extraction_size
        if selected_size:
            kwargs["size"] = selected_size
        if self._before_provider_request is not None:
            self._before_provider_request()
        try:
            return await self.provider.generate(prompt, **kwargs)
        except Exception:  # noqa: BLE001 - provider implementations are injected
            # Do not propagate compatible-provider response text: some gateways
            # echo request headers or credentials in non-standard errors.
            raise ReferenceGenerationError("reference image generation failed") from None

    async def _tag(
        self,
        category: ReferenceCategory,
        reference_bytes: bytes,
        *,
        prompt: str,
    ) -> TagValidationResult:
        try:
            result = await self.llm.generate_json(
                prompt,
                task_name=self.config.tagging_task_name,
                image_bytes=reference_bytes,
                mime_type="image/jpeg",
                temperature=self.config.tagging_temperature,
                max_tokens=self.config.tagging_max_tokens,
            )
        except Exception:  # noqa: BLE001 - SDK proxies may raise host exceptions
            # Provider/host exception text is intentionally not persisted or
            # logged because compatible endpoints may echo credentials.
            return TagValidationResult(
                tags={},
                schema_valid=False,
                selectable=False,
                error_code="tagging_failed",
                errors=("tagging model call failed",),
            )
        return validate_reference_tags(category, result)

    async def _persist_new(
        self,
        *,
        category: ReferenceCategory,
        name: str,
        source_result: CompressionResult,
        reference_result: CompressionResult,
        replace_person: bool,
        manual_tags: Mapping[str, Any] | None,
        source_task_id: str | None,
        prompt_version: str,
        source_kind: str,
        provider_media_type: str = "",
        replaces_asset_id: str | None = None,
    ) -> ReferenceAsset:
        tagging = await self._tag(
            category,
            reference_result.data,
            prompt=self.config.prompts.tagging_for(category),
        )
        status = self._status_for(tagging)
        token = uuid4().hex
        source_path = self._artifact_path("sources", category, token)
        reference_path = self._artifact_path("references", category, token)

        async with self._mutation_lock:
            written: list[Path] = []
            try:
                self._atomic_write(source_path, source_result.data)
                written.append(source_path)
                self._atomic_write(reference_path, reference_result.data)
                written.append(reference_path)
                with self.gallery.storage.transaction():
                    asset = self.gallery.add_reference(
                        category=category,
                        name=name.strip(),
                        source_path=source_path,
                        reference_path=reference_path,
                        sha256=reference_result.sha256,
                        tags=tagging.tags,
                        manual_tags=dict(manual_tags or {}),
                        prompt_version=prompt_version,
                        status=status,
                        source_task_id=source_task_id,
                        selection_metadata={
                            "source_kind": source_kind,
                            "layout": self._layout_name(category),
                            "source_compression": source_result.to_dict(),
                            "reference_compression": reference_result.to_dict(),
                            "provider_media_type": provider_media_type,
                            **self._tagging_metadata(tagging, reference_result),
                        },
                        replace_person=replace_person,
                    )
                    if replaces_asset_id is not None and category != ReferenceCategory.PERSON:
                        self.gallery.soft_delete(replaces_asset_id)
            except Exception:
                for path in written:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise
        return asset

    def _status_for(self, tagging: TagValidationResult) -> AssetStatus:
        if not tagging.selectable:
            return AssetStatus.NEEDS_REVIEW
        if self.config.auto_enable_generated_references:
            return AssetStatus.ACTIVE
        return AssetStatus.DISABLED

    @staticmethod
    def _tagging_metadata(
        tagging: TagValidationResult,
        reference_result: CompressionResult,
    ) -> dict[str, Any]:
        return {
            "tag_schema_valid": tagging.schema_valid,
            "tag_selectable": tagging.selectable,
            "tag_error_code": tagging.error_code,
            "tag_validation_errors": list(tagging.errors),
            "reference_sha256": reference_result.sha256,
        }

    def _artifact_path(
        self,
        kind: str,
        category: ReferenceCategory,
        token: str,
    ) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", token):
            raise ReferenceServiceError("invalid artifact token")
        path = (self.data_dir / kind / category.value / f"{token}.jpg").resolve()
        try:
            path.relative_to(self.data_dir)
        except ValueError as exc:
            raise ReferenceServiceError("artifact path escaped data directory") from exc
        return path

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _prompt_version(self, operation: str, prompt: str) -> str:
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        configured = self.config.prompt_version.strip()
        return f"{configured}:{operation}:{digest}" if configured else f"{operation}:{digest}"

    @staticmethod
    def _layout_name(category: ReferenceCategory) -> str:
        return {
            ReferenceCategory.PERSON: "3x2_person_sheet",
            ReferenceCategory.OUTFIT: "2x2_outfit_sheet",
            ReferenceCategory.SCENE: "2x2_scene_sheet_with_floor_plan",
        }[category]


__all__ = [
    "MAX_REFERENCE_TARGET_BYTES",
    "ReferenceGenerationError",
    "ReferencePrompts",
    "ReferenceService",
    "ReferenceServiceConfig",
    "ReferenceServiceError",
    "ReferenceTagValidationError",
    "TagValidationResult",
    "require_valid_reference_tags",
    "validate_reference_tags",
]
