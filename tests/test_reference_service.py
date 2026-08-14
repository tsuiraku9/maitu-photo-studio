from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from maitu_photo.compression import CompressionConfig
from maitu_photo.gallery import DuplicateReferenceError, ReferenceGallery
from maitu_photo.models import AssetStatus, ReferenceCategory
from maitu_photo.provider import GeneratedImage, ProviderImageDecodeError
from maitu_photo.reference_service import (
    ReferenceGenerationError,
    ReferencePrompts,
    ReferenceService,
    ReferenceServiceConfig,
    ReferenceTagValidationError,
    require_valid_reference_tags,
    validate_reference_tags,
)
from maitu_photo.storage import SQLiteStorage


def _image(
    colour: tuple[int, int, int, int] = (180, 80, 40, 180),
    *,
    size: tuple[int, int] = (1400, 900),
) -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", size, colour).save(
        output,
        "PNG",
        pnginfo=None,
    )
    return output.getvalue()


def _tags(category: ReferenceCategory) -> dict[str, Any]:
    if category == ReferenceCategory.PERSON:
        return {
            "appearance_summary": "dark shoulder-length hair",
            "confidence": 0.92,
        }
    if category == ReferenceCategory.OUTFIT:
        return {
            "type": "dress",
            "wearing_scenes": ["home", "casual"],
            "seasons": ["spring", "summer"],
            "styles": ["minimal"],
            "confidence": 0.88,
        }
    return {
        "room_type": "bedroom",
        "privacy_eligible": True,
        "scene_signature": "bedroom-window-left",
        "confidence": 0.9,
    }


class FakeProvider:
    def __init__(self, outputs: list[bytes] | None = None) -> None:
        self.outputs = list(outputs or [_image((20, 150, 90, 255))])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def generate(self, prompt: str, **kwargs: Any) -> GeneratedImage:
        self.calls.append((prompt, kwargs))
        data = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        return GeneratedImage(data=data, media_type="image/png")


class FakeLLM:
    def __init__(self, replies: list[dict[str, Any] | Exception]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def generate_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"prompt": prompt, **kwargs})
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


class FailingProvider:
    async def generate(self, prompt: str, **kwargs: Any) -> GeneratedImage:
        raise RuntimeError("Bearer sk-sensitive-compatible-provider-key")


class ProviderDecodeFailingProvider:
    async def generate(self, prompt: str, **kwargs: Any) -> GeneratedImage:
        raise ProviderImageDecodeError("provider returned a protected image URL")


def _run(coro):
    return asyncio.run(coro)


def _service(
    tmp_path: Path,
    *,
    provider: FakeProvider,
    llm: FakeLLM,
    auto_enable: bool = True,
) -> tuple[SQLiteStorage, ReferenceGallery, ReferenceService]:
    storage = SQLiteStorage(tmp_path / "db" / "maitu.sqlite3")
    gallery = ReferenceGallery(storage)
    config = ReferenceServiceConfig(
        compression=CompressionConfig(target_bytes=120_000, max_edge=1024),
        auto_enable_generated_references=auto_enable,
        prompts=ReferencePrompts(),
        reference_model="reference-model",
        reference_mode="images_api",
        prompt_version="tests-v1",
    )
    service = ReferenceService(
        gallery=gallery,
        provider=provider,  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        data_dir=tmp_path / "plugin-data",
        config=config,
    )
    return storage, gallery, service


def test_extract_outfit_uses_sheet_prompt_and_compresses_both_artifacts(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    llm = FakeLLM([_tags(ReferenceCategory.OUTFIT)])
    storage, _, service = _service(tmp_path, provider=provider, llm=llm)

    asset = _run(
        service.extract_reference(
            "outfit",
            _image(),
            name="summer dress",
            source_task_id="task-1",
        )
    )

    assert asset.status == AssetStatus.ACTIVE
    assert asset.tags["type"] == "dress"
    assert asset.selection_metadata["layout"] == "2x2_outfit_sheet"
    assert "2x2" in provider.calls[0][0]
    assert provider.calls[0][1]["extraction"] is True
    assert provider.calls[0][1]["model"] == "reference-model"
    assert provider.calls[0][1]["mode"] == "images_api"
    provider_input = provider.calls[0][1]["images"][0]
    assert provider_input.startswith(b"\xff\xd8\xff")
    assert len(provider_input) <= 120_000

    for path in (asset.source_path, asset.reference_path):
        assert path.resolve().is_relative_to((tmp_path / "plugin-data").resolve())
        assert path.stat().st_size <= 120_000
        with Image.open(path) as image:
            image.load()
            assert image.format == "JPEG"
            assert image.mode == "RGB"
            assert image.getexif() == {}
            assert "icc_profile" not in image.info
    assert llm.calls[0]["image_bytes"].startswith(b"\xff\xd8\xff")
    storage.close()


def test_tag_schema_is_exact_and_public_scene_references_are_not_selectable() -> None:
    valid = validate_reference_tags("outfit", _tags(ReferenceCategory.OUTFIT))
    assert valid.schema_valid is True
    assert valid.selectable is True

    with_extra = {**_tags(ReferenceCategory.OUTFIT), "unexpected": "field"}
    invalid = validate_reference_tags("outfit", with_extra)
    assert invalid.schema_valid is False
    assert invalid.error_code == "tag_schema_invalid"
    with pytest.raises(ReferenceTagValidationError):
        require_valid_reference_tags("outfit", with_extra)

    scene = validate_reference_tags(
        "scene",
        {
            **_tags(ReferenceCategory.SCENE),
            "light": "window light",
            "time_of_day": "morning",
            "privacy_eligible": False,
        },
    )
    assert scene.schema_valid is True
    assert scene.selectable is False
    assert scene.error_code == "scene_not_private"
    assert set(scene.tags) == {"room_type", "privacy_eligible", "scene_signature", "confidence"}

    person = validate_reference_tags("person", _tags(ReferenceCategory.PERSON))
    assert person.schema_valid is True
    assert "accessories" not in person.tags


def test_generate_person_from_personality_does_not_send_source_image(tmp_path: Path) -> None:
    provider = FakeProvider()
    llm = FakeLLM([_tags(ReferenceCategory.PERSON)])
    storage, gallery, service = _service(tmp_path, provider=provider, llm=llm)

    asset = _run(
        service.generate_person_from_personality(
            name="persona",
            personality="大二女生，短发，圆脸",
            nickname="bot",
            appearance_hint="自然刘海",
        )
    )

    assert asset.status.value == "active"
    assert gallery.get_person() is not None
    assert "images" not in provider.calls[0][1]
    assert "大二女生" in provider.calls[0][0]
    assert "自然刘海" in provider.calls[0][0]
    storage.close()


def test_scene_generation_attributes_are_not_persisted_and_public_scene_is_saved_for_review(
    tmp_path: Path,
) -> None:
    legacy_scene_tags = {
        **_tags(ReferenceCategory.SCENE),
        "light": "window light",
        "time_of_day": "morning",
        "privacy_eligible": False,
    }
    provider = FakeProvider()
    llm = FakeLLM([legacy_scene_tags, RuntimeError("Bearer secret-value-should-not-persist")])
    storage, gallery, service = _service(tmp_path, provider=provider, llm=llm)

    scene = _run(service.import_reference("scene", _image(), name="cafe"))
    assert scene.status == AssetStatus.NEEDS_REVIEW
    assert scene.tags == {**_tags(ReferenceCategory.SCENE), "privacy_eligible": False}
    assert "light" not in scene.effective_tags
    assert "time_of_day" not in scene.effective_tags
    assert scene.effective_tags["privacy_eligible"] is False
    assert gallery.candidates("scene") == []

    outfit = _run(service.import_reference("outfit", _image((30, 40, 50, 255)), name="unknown"))
    assert outfit.status == AssetStatus.NEEDS_REVIEW
    assert outfit.tags == {}
    metadata_json = json.dumps(outfit.selection_metadata)
    assert "secret-value" not in metadata_json
    assert outfit.selection_metadata["tag_error_code"] == "tagging_failed"
    storage.close()


def test_malformed_non_json_tags_still_create_review_asset(tmp_path: Path) -> None:
    provider = FakeProvider()
    llm = FakeLLM([{1: object()}])  # type: ignore[dict-item, list-item]
    storage, _, service = _service(tmp_path, provider=provider, llm=llm)

    asset = _run(service.import_reference("outfit", _image(), name="review me"))

    assert asset.status == AssetStatus.NEEDS_REVIEW
    assert asset.tags == {}
    assert asset.selection_metadata["tag_error_code"] == "tag_schema_invalid"
    storage.close()


def test_person_requires_explicit_replace_and_gallery_deduplicates_imports(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    llm = FakeLLM(
        [
            _tags(ReferenceCategory.PERSON),
            _tags(ReferenceCategory.PERSON),
            _tags(ReferenceCategory.OUTFIT),
            _tags(ReferenceCategory.OUTFIT),
        ]
    )
    storage, gallery, service = _service(tmp_path, provider=provider, llm=llm)
    first = _run(service.import_reference("person", _image(), name="first"))

    with pytest.raises(DuplicateReferenceError) as conflict:
        _run(service.import_reference("person", _image(), name="second"))
    assert conflict.value.existing_id == first.id

    second = _run(
        service.import_reference(
            "person",
            _image((10, 20, 30, 255)),
            name="second",
            replace_person=True,
        )
    )
    assert gallery.get_person() is not None
    assert gallery.get_person().id == second.id  # type: ignore[union-attr]
    assert storage.get_reference_asset(first.id, include_deleted=True).status == AssetStatus.DELETED  # type: ignore[union-attr]

    source = _image((70, 80, 90, 255))
    outfit = _run(service.import_reference("outfit", source, name="first outfit"))
    with pytest.raises(DuplicateReferenceError) as duplicate:
        _run(service.import_reference("outfit", source, name="duplicate outfit"))
    assert duplicate.value.existing_id == outfit.id
    assert len(list((tmp_path / "plugin-data" / "sources" / "outfit").glob("*.jpg"))) == 1
    assert len(list((tmp_path / "plugin-data" / "references" / "outfit").glob("*.jpg"))) == 1
    storage.close()


def test_retag_failure_marks_existing_asset_needs_review(tmp_path: Path) -> None:
    provider = FakeProvider()
    llm = FakeLLM([_tags(ReferenceCategory.OUTFIT), RuntimeError("upstream unavailable")])
    storage, _, service = _service(tmp_path, provider=provider, llm=llm)
    asset = _run(service.import_reference("outfit", _image(), name="dress"))
    assert asset.status == AssetStatus.ACTIVE

    retagged = _run(service.retag_reference(asset.id))
    assert retagged.status == AssetStatus.NEEDS_REVIEW
    assert retagged.selection_metadata["tag_error_code"] == "tagging_failed"
    storage.close()


def test_regenerate_creates_auditable_replacement_and_deletes_old_asset(
    tmp_path: Path,
) -> None:
    provider = FakeProvider([_image((15, 160, 210, 255))])
    llm = FakeLLM([_tags(ReferenceCategory.OUTFIT), _tags(ReferenceCategory.OUTFIT)])
    storage, gallery, service = _service(tmp_path, provider=provider, llm=llm)
    original = _run(
        service.import_reference(
            "outfit",
            _image((210, 40, 50, 255)),
            name="dress",
            manual_tags={"styles": ["admin-edited"]},
        )
    )

    replacement = _run(service.regenerate_reference(original.id))

    assert replacement.id != original.id
    assert replacement.manual_tags == {"styles": ["admin-edited"]}
    assert replacement.status == AssetStatus.ACTIVE
    assert replacement.reference_path.stat().st_size <= 120_000
    deleted = storage.get_reference_asset(original.id, include_deleted=True)
    assert deleted is not None and deleted.status == AssetStatus.DELETED
    assert gallery.get(original.id) is None
    assert provider.calls[0][1]["images"][0].startswith(b"\xff\xd8\xff")
    storage.close()


def test_valid_import_respects_auto_enable_setting(tmp_path: Path) -> None:
    provider = FakeProvider()
    llm = FakeLLM([_tags(ReferenceCategory.OUTFIT)])
    storage, _, service = _service(
        tmp_path,
        provider=provider,
        llm=llm,
        auto_enable=False,
    )
    asset = _run(service.import_reference("outfit", _image(), name="dress"))
    assert asset.status == AssetStatus.DISABLED
    storage.close()


def test_extraction_error_does_not_expose_provider_response(tmp_path: Path) -> None:
    storage, _, service = _service(
        tmp_path,
        provider=FailingProvider(),  # type: ignore[arg-type]
        llm=FakeLLM([_tags(ReferenceCategory.OUTFIT)]),
    )

    with pytest.raises(ReferenceGenerationError) as caught:
        _run(service.extract_reference("outfit", _image(), name="dress"))

    assert "sensitive-compatible-provider-key" not in str(caught.value)
    assert list((tmp_path / "plugin-data").rglob("*.jpg")) == []
    storage.close()


def test_extraction_error_preserves_safe_provider_category(tmp_path: Path) -> None:
    storage, _, service = _service(
        tmp_path,
        provider=ProviderDecodeFailingProvider(),  # type: ignore[arg-type]
        llm=FakeLLM([_tags(ReferenceCategory.OUTFIT)]),
    )

    with pytest.raises(ReferenceGenerationError, match=r"provider_image_decode"):
        _run(service.extract_reference("outfit", _image(), name="dress"))

    storage.close()


def test_compression_target_above_480000_is_rejected() -> None:
    with pytest.raises(ValueError, match="480000"):
        ReferenceServiceConfig(compression=CompressionConfig(target_bytes=480_001))
