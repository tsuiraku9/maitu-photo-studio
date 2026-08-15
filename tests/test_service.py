from __future__ import annotations

import asyncio
import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from maitu_photo.config import PhotoPluginConfig
from maitu_photo.models import AssetStatus, ImageTask, ReferenceCategory, TaskStatus
from maitu_photo.provider import GeneratedImage
from maitu_photo.runtime import InvocationContext
from maitu_photo.service import PhotoStudioError, PhotoStudioService, TaskAccessError


def _png(colour: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (16, 12), colour).save(output, "PNG")
    return output.getvalue()


class _Provider:
    def __init__(self, outputs: list[bytes]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[str, dict]] = []

    async def generate(self, prompt: str, **kwargs) -> GeneratedImage:
        self.calls.append((prompt, kwargs))
        output = self.outputs[min(len(self.calls) - 1, len(self.outputs) - 1)]
        return GeneratedImage(output, media_type="image/png")

    async def aclose(self) -> None:
        return None


class _BlockingProvider(_Provider):
    def __init__(self, output: bytes) -> None:
        super().__init__([output])
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, prompt: str, **kwargs) -> GeneratedImage:
        self.started.set()
        await self.release.wait()
        return await super().generate(prompt, **kwargs)


class _Send:
    def __init__(self, image_success: bool = True) -> None:
        self.image_success = image_success
        self.images: list[tuple[str, str]] = []
        self.texts: list[tuple[str, str]] = []

    async def image(self, data: str, stream_id: str, **kwargs):
        del kwargs
        self.images.append((data, stream_id))
        return {"sent": self.image_success, "message_id": "platform-message"}

    async def text(self, text: str, stream_id: str):
        self.texts.append((text, stream_id))
        return True


class _LLM:
    def __init__(self, replies: list[dict] | None = None) -> None:
        self.replies = list(replies or [])
        self.calls: list[object] = []

    async def generate(self, prompt, **kwargs):
        del kwargs
        self.calls.append(prompt)
        reply = self.replies.pop(0)
        return {"success": True, "response": json.dumps(reply, ensure_ascii=False)}


class _ContextLog:
    def __init__(self, *, success: bool = True) -> None:
        self.success = success
        self.append_calls: list[dict] = []
        self.trigger_calls: list[dict] = []

    async def append(self, **kwargs):
        self.append_calls.append(kwargs)
        return {"success": self.success}

    async def trigger(self, **kwargs):
        self.trigger_calls.append(kwargs)
        return {"success": self.success}


class _Context:
    def __init__(
        self,
        llm_replies: list[dict] | None = None,
        *,
        image_success: bool = True,
        proactive_success: bool = True,
    ) -> None:
        self.logger = logging.getLogger("test.maitu")
        self.send = _Send(image_success=image_success)
        self.llm = _LLM(llm_replies)
        context = _ContextLog()
        proactive = _ContextLog(success=proactive_success)
        self.maisaka = SimpleNamespace(context=context, proactive=proactive)


class _HostConfig:
    def __init__(self, nickname: str, personality: str) -> None:
        self.values = {
            "bot.nickname": nickname,
            "personality.personality": personality,
        }

    async def get(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)


def _config() -> PhotoPluginConfig:
    config = PhotoPluginConfig()
    config.openai.base_url = "https://provider.example"
    config.openai.api_key = "test-secret"
    config.openai.generation_model = "image-model"
    config.tasks.poll_interval_seconds = 0.01
    return config


def _invocation(*, stream_id: str = "stream-1", group_id: str = "group-1") -> InvocationContext:
    return InvocationContext(
        stream_id=stream_id,
        scope_key=f"stream:{stream_id}",
        user_id="user-1",
        group_id=group_id,
        message_id="message-1",
        message={},
    )


def _scene_photo_llm_replies(signature: str = "generic-scene") -> list[dict]:
    return [
        {"eligible": False, "scene_signature": signature, "reason": "not private"},
        {"scene_signature": signature, "changed": False},
    ]


def test_task_lookup_rejects_same_group_on_another_canonical_stream(tmp_path: Path) -> None:
    service = PhotoStudioService(_Context([]), _config(), tmp_path)
    first = _invocation(stream_id="qq-stream", group_id="same-group")
    second = _invocation(stream_id="discord-stream", group_id="same-group")
    task = service.storage.create_task(ImageTask(kind="scene_photo", scope_key=first.scope_key))

    assert service.get_task_for(first, task.id) == task
    with pytest.raises(TaskAccessError, match="其他聊天"):
        service.get_task_for(second, task.id)

    asyncio.run(service.close())


def test_scene_photo_generation_persists_sends_and_notifies_planner(tmp_path: Path) -> None:
    async def scenario() -> None:
        ctx = _Context(_scene_photo_llm_replies("desk"))
        service = PhotoStudioService(ctx, _config(), tmp_path)
        provider = _Provider([_png("red")])
        service._provider = provider  # type: ignore[assignment]
        await service.start()
        task = service.submit_scene_photo(_invocation(), description="桌上的咖啡特写")
        assert await service.tasks.drain(timeout=3)

        saved = service.storage.get_task(task.id)
        assert saved is not None
        assert saved.kind == "scene_photo"
        assert saved.status == TaskStatus.SENT
        assert saved.result_path is not None and saved.result_path.is_file()
        assert saved.planner_notified_at is not None
        assert saved.result_metadata["generation"] == "scene_photo"
        assert not (service.payloads.root / f"{task.id}.json").exists()
        assert len(ctx.send.images) == 1
        assert len(ctx.maisaka.context.append_calls) == 1
        assert len(ctx.maisaka.proactive.trigger_calls) == 1
        assert provider.calls[0][1].get("images") in (None, [])
        assert "test-secret" not in json.dumps(saved.result_metadata)
        await service.close()

    asyncio.run(scenario())


def test_prepared_reference_import_does_not_require_openai_provider(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = PhotoPluginConfig()
        ctx = _Context(
            [
                {
                    "type": "dress",
                    "wearing_scenes": ["home"],
                    "seasons": ["summer"],
                    "styles": ["casual"],
                    "confidence": 1.0,
                }
            ]
        )
        service = PhotoStudioService(ctx, config, tmp_path)

        reference_service = service._reference_service(require_provider=False)
        asset = await reference_service.import_reference(
            ReferenceCategory.OUTFIT,
            _png("green"),
            name="prepared outfit",
        )

        assert service._provider is None
        assert asset.status.value == "active"
        assert asset.reference_path.stat().st_size <= config.references.max_bytes
        await service.close()

    asyncio.run(scenario())


def test_planner_soft_failure_is_recorded_without_resending_image(tmp_path: Path) -> None:
    async def scenario() -> None:
        ctx = _Context(_scene_photo_llm_replies("window"), proactive_success=False)
        service = PhotoStudioService(ctx, _config(), tmp_path)
        service._provider = _Provider([_png("red")])  # type: ignore[assignment]
        await service.start()
        task = service.submit_scene_photo(_invocation(), description="窗边空镜")
        assert await service.tasks.drain(timeout=3)

        saved = service.storage.get_task(task.id)
        assert saved is not None
        assert saved.status == TaskStatus.SENT
        assert saved.planner_notified_at is None
        assert saved.result_metadata["planner_context_appended"] is True
        assert "notification_error" in saved.result_metadata
        assert len(ctx.send.images) == 1

        ctx.maisaka.proactive.success = True
        service.retry_task(task.id)
        assert await service.tasks.drain(timeout=3)
        retried = service.storage.get_task(task.id)
        assert retried is not None
        assert retried.kind == "scene_photo"
        assert retried.planner_notified_at is not None
        assert len(ctx.send.images) == 1
        await service.close()

    asyncio.run(scenario())


def test_recovery_does_not_resend_already_delivered_result(tmp_path: Path) -> None:
    async def scenario() -> None:
        ctx = _Context()
        service = PhotoStudioService(ctx, _config(), tmp_path)
        result_path = tmp_path / "already-sent.png"
        result_path.write_bytes(_png("red"))
        task = ImageTask(
            kind="scene_photo",
            scope_key="group:group-1",
            stream_id="stream-1",
            group_id="group-1",
            status=TaskStatus.GENERATED,
            result_path=result_path,
            result_metadata={"image_sent": True, "media_type": "image/png"},
        )
        service.storage.create_task(task)

        await service.start()
        assert await service.tasks.drain(timeout=3)

        recovered = service.storage.get_task(task.id)
        assert recovered is not None
        assert recovered.status == TaskStatus.SENT
        assert recovered.planner_notified_at is not None
        assert ctx.send.images == []
        await service.close()

    asyncio.run(scenario())


def _add_reference(
    service: PhotoStudioService,
    tmp_path: Path,
    category: ReferenceCategory,
    data: bytes,
    tags: dict,
) -> str:
    source = tmp_path / f"{category.value}-source.png"
    reference = tmp_path / f"{category.value}-reference.png"
    source.write_bytes(data)
    reference.write_bytes(data)
    asset = service.gallery.add_reference(
        category=category,
        name=category.value,
        source_path=source,
        reference_path=reference,
        tags=tags,
    )
    return asset.id


def test_photo_submission_uses_personality_fallback_without_active_person_reference(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = _config()
        config.references.auto_extract_missing = False
        config.references.require_person_reference = False
        ctx = _Context(
            [
                {"eligible": False, "scene_signature": "street", "reason": "public"},
                {"scene_signature": "street", "changed": False},
            ]
        )
        ctx.config = _HostConfig("Mai", "温柔、好奇、喜欢城市漫步的年轻女性")
        service = PhotoStudioService(ctx, config, tmp_path / "data")
        provider = _Provider([_png("white")])
        service._provider = provider  # type: ignore[assignment]
        await service.start()

        task = service.submit_photo(_invocation(), description="街边随手拍")
        assert await service.tasks.drain(timeout=3)

        saved = service.storage.get_task(task.id)
        assert saved is not None and saved.status == TaskStatus.SENT
        assert saved.result_metadata["person_reference_used"] is False
        assert saved.result_metadata["person_fallback"] == "maibot_personality"
        assert provider.calls[0][1].get("images") in (None, [])
        assert "温柔、好奇、喜欢城市漫步的年轻女性" in provider.calls[0][0]
        assert "昵称：Mai" in provider.calls[0][0]
        references = {item.role: item for item in service.storage.list_task_references(task.id)}
        assert references["person"].selection_source == "maibot_personality"
        assert references["person"].fallback_reason == "no_active_person"
        await service.close()

    asyncio.run(scenario())


def test_photo_submission_rejects_person_reference_opt_out_when_strict_mode_enabled(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = _config()
        config.references.require_person_reference = True
        service = PhotoStudioService(_Context(), config, tmp_path / "data")
        _add_reference(
            service,
            tmp_path,
            ReferenceCategory.PERSON,
            _png("red"),
            {"appearance_summary": "adult with dark hair", "confidence": 1.0},
        )

        with pytest.raises(PhotoStudioError, match="use_person_reference 不能关闭"):
            service.submit_photo(
                _invocation(),
                description="natural portrait",
                use_person_reference=False,
            )

        assert service.storage.list_tasks() == []
        await service.close()

    asyncio.run(scenario())


def test_photo_submission_requires_active_person_reference_by_default(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = _config()
        service = PhotoStudioService(_Context(), config, tmp_path / "data")

        with pytest.raises(PhotoStudioError, match="尚未配置人物参考图"):
            service.submit_photo(_invocation(), description="natural portrait")

        person_id = _add_reference(
            service,
            tmp_path,
            ReferenceCategory.PERSON,
            _png("red"),
            {"appearance_summary": "adult with dark hair", "confidence": 1.0},
        )
        service.gallery.mark_needs_review(person_id)
        with pytest.raises(PhotoStudioError, match="needs_review"):
            service.submit_photo(_invocation(), description="natural portrait")

        assert service.storage.list_tasks() == []
        await service.close()

    asyncio.run(scenario())


def test_photo_allows_text_person_when_person_reference_disabled(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = _config()
        config.references.person_reference_enabled = False
        config.references.auto_extract_missing = False
        ctx = _Context(
            [
                {"eligible": False, "scene_signature": "street", "reason": "public"},
                {"scene_signature": "street", "changed": False},
            ]
        )
        service = PhotoStudioService(ctx, config, tmp_path / "data")
        provider = _Provider([_png("white")])
        service._provider = provider  # type: ignore[assignment]
        await service.start()

        task = service.submit_photo(_invocation(), description="街边随手拍一张生活照片")
        assert await service.tasks.drain(timeout=3)

        saved = service.storage.get_task(task.id)
        assert saved is not None
        assert saved.status == TaskStatus.SENT
        assert saved.result_metadata["person_id"] is None
        assert saved.result_metadata["person_reference_used"] is False
        assert provider.calls[0][1].get("images") in (None, [])
        refs = {item.role: item for item in service.storage.list_task_references(task.id)}
        assert refs["person"].asset_id is None
        assert refs["person"].selection_source == "maibot_personality"
        assert refs["person"].fallback_reason == "reference_disabled"
        await service.close()

    asyncio.run(scenario())


def test_auto_backfill_skips_scene_extraction_when_scene_text_is_ineligible(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = _config()
        config.references.person_reference_enabled = False
        ctx = _Context(
            [
                {"eligible": False, "scene_signature": "cafe", "reason": "public text scene"},
                {"scene_signature": "cafe", "changed": False},
                {
                    "type": "dress",
                    "wearing_scenes": ["casual"],
                    "seasons": ["summer"],
                    "styles": ["minimal"],
                    "confidence": 0.9,
                },
            ]
        )
        service = PhotoStudioService(ctx, config, tmp_path / "data")
        service._provider = _Provider([_png("white"), _png("green")])  # type: ignore[assignment]
        await service.start()

        task = service.submit_photo(
            _invocation(),
            description="在卧室窗边拍一张自然照片",
            scene_hint="卧室窗边",
        )
        assert await service.tasks.drain(timeout=3)

        children = service.storage.list_tasks(parent_task_id=task.id)
        assert len(children) == 1
        assert children[0].kind == "reference_extract"
        assert children[0].result_metadata["asset_category"] == ReferenceCategory.OUTFIT.value
        assert all(asset.category != ReferenceCategory.SCENE for asset in service.gallery.list_assets())
        text_eligibility_calls = [
            call
            for call in ctx.llm.calls
            if isinstance(call, str) and "判断目标场景是否属于" in call
        ]
        assert len(text_eligibility_calls) == 1
        assert not any(
            isinstance(call, list)
            and call
            and isinstance(call[0], dict)
            and any(
                isinstance(item, dict)
                and item.get("type") == "text"
                and "判断目标场景是否属于" in str(item.get("text") or "")
                for item in call[0].get("content", [])
            )
            and any(
                isinstance(item, dict) and item.get("type") == "image_url"
                for item in call[0].get("content", [])
            )
            for call in ctx.llm.calls
        )
        await service.close()

    asyncio.run(scenario())


def test_auto_backfill_extracts_scene_when_scene_text_is_eligible(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = _config()
        config.references.person_reference_enabled = False
        ctx = _Context(
            [
                {"eligible": True, "scene_signature": "bedroom-window", "reason": "private"},
                {"scene_signature": "bedroom-window", "changed": False},
                {
                    "type": "dress",
                    "wearing_scenes": ["casual"],
                    "seasons": ["summer"],
                    "styles": ["minimal"],
                    "confidence": 0.9,
                },
                {
                    "room_type": "bedroom",
                    "privacy_eligible": True,
                    "scene_signature": "bedroom-window",
                    "confidence": 0.9,
                },
            ]
        )
        service = PhotoStudioService(ctx, config, tmp_path / "data")
        service._provider = _Provider([_png("white"), _png("green"), _png("blue")])  # type: ignore[assignment]
        await service.start()

        task = service.submit_photo(
            _invocation(),
            description="在卧室窗边拍一张自然照片",
            scene_hint="卧室窗边",
        )
        assert await service.tasks.drain(timeout=3)

        children = service.storage.list_tasks(parent_task_id=task.id)
        assert {child.result_metadata["asset_category"] for child in children} == {"outfit", "scene"}
        scenes = service.gallery.list_assets(category=ReferenceCategory.SCENE)
        assert len(scenes) == 1
        assert scenes[0].status == AssetStatus.ACTIVE
        assert scenes[0].tags == {
            "room_type": "bedroom",
            "privacy_eligible": True,
            "scene_signature": "bedroom-window",
            "confidence": 0.9,
        }
        scene_child = next(
            child for child in children if child.result_metadata["asset_category"] == ReferenceCategory.SCENE.value
        )
        assert scene_child.result_metadata["asset_status"] == AssetStatus.ACTIVE.value
        scene_tag_call = ctx.llm.calls[-1]
        assert isinstance(scene_tag_call, list)
        assert '"privacy_eligible":false' in scene_tag_call[0]["content"][0]["text"]
        assert "光线" in service._scene_prompt(scenes[0], "")
        text_eligibility_calls = [
            call
            for call in ctx.llm.calls
            if isinstance(call, str) and "判断目标场景是否属于" in call
        ]
        assert len(text_eligibility_calls) == 1
        assert not any(
            isinstance(call, list)
            and call
            and isinstance(call[0], dict)
            and any(
                isinstance(item, dict)
                and item.get("type") == "text"
                and "判断目标场景是否属于" in str(item.get("text") or "")
                for item in call[0].get("content", [])
            )
            and any(
                isinstance(item, dict) and item.get("type") == "image_url"
                for item in call[0].get("content", [])
            )
            for call in ctx.llm.calls
        )
        await service.close()

    asyncio.run(scenario())


def test_scene_photo_can_use_scene_reference_without_person(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = _config()
        config.references.auto_extract_missing = False
        ctx = _Context(
            [
                {"eligible": True, "scene_signature": "bedroom-window", "reason": "private"},
                {"scene_signature": "bedroom-window", "changed": False},
                {"outfit_id": None, "scene_id": "placeholder", "reason": "best"},
            ]
        )
        service = PhotoStudioService(ctx, config, tmp_path / "data")
        scene_bytes = _png("blue")
        scene_id = _add_reference(
            service,
            tmp_path,
            ReferenceCategory.SCENE,
            scene_bytes,
            {
                "room_type": "bedroom",
                "privacy_eligible": True,
                "scene_signature": "bedroom-window",
                "confidence": 1.0,
            },
        )
        ctx.llm.replies[2]["scene_id"] = scene_id
        provider = _Provider([_png("white")])
        service._provider = provider  # type: ignore[assignment]
        await service.start()

        task = service.submit_scene_photo(
            _invocation(),
            description="卧室窗边的空镜",
            scene_hint="卧室窗边",
        )
        assert await service.tasks.drain(timeout=3)

        saved = service.storage.get_task(task.id)
        assert saved is not None
        assert saved.status == TaskStatus.SENT
        assert provider.calls[0][1]["images"] == [scene_bytes]
        refs = {item.role: item for item in service.storage.list_task_references(task.id)}
        assert refs["person"].asset_id is None
        assert refs["outfit"].asset_id is None
        assert refs["scene"].asset_id == scene_id
        await service.close()

    asyncio.run(scenario())


def test_photo_reference_order_and_same_scene_outfit_continuity(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = _config()
        config.references.auto_extract_missing = False
        config.prompts.scene_signature = "CUSTOM SIGNATURE {scene_hint}"
        ctx = _Context(
            [
                {"eligible": True, "scene_signature": "bedroom-window", "reason": "private"},
                {"scene_signature": "bedroom-window", "changed": False},
                {"outfit_id": "placeholder", "scene_id": "placeholder", "reason": "best"},
                {"eligible": True, "scene_signature": "bedroom-window", "reason": "private"},
                {"scene_signature": "bedroom-window", "changed": False},
            ]
        )
        service = PhotoStudioService(ctx, config, tmp_path / "data")
        person_bytes = _png("red")
        outfit_bytes = _png("green")
        scene_bytes = _png("blue")
        person_id = _add_reference(
            service,
            tmp_path,
            ReferenceCategory.PERSON,
            person_bytes,
            {"appearance_summary": "adult with dark hair", "confidence": 1.0},
        )
        outfit_id = _add_reference(
            service,
            tmp_path,
            ReferenceCategory.OUTFIT,
            outfit_bytes,
            {
                "type": "dress",
                "wearing_scenes": ["home"],
                "seasons": ["summer"],
                "styles": ["casual"],
                "confidence": 1.0,
            },
        )
        scene_id = _add_reference(
            service,
            tmp_path,
            ReferenceCategory.SCENE,
            scene_bytes,
            {
                "room_type": "bedroom",
                "privacy_eligible": True,
                "scene_signature": "bedroom-window",
                "confidence": 1.0,
            },
        )
        ctx.llm.replies[2]["outfit_id"] = outfit_id
        ctx.llm.replies[2]["scene_id"] = scene_id
        provider = _Provider([_png("white"), _png("black")])
        service._provider = provider  # type: ignore[assignment]
        await service.start()

        first = service.submit_photo(
            _invocation(),
            description="在卧室窗边拍一张自然照片",
            outfit_hint="casual summer dress",
            scene_hint="卧室窗边",
        )
        assert await service.tasks.drain(timeout=3)
        assert ctx.llm.calls[1] == "CUSTOM SIGNATURE 卧室窗边"
        second = service.submit_photo(
            _invocation(),
            description="仍在卧室窗边换个姿势",
            outfit_hint="casual summer dress",
            scene_hint="卧室窗边",
        )
        assert await service.tasks.drain(timeout=3)

        assert [item for item in provider.calls[0][1]["images"]] == [
            person_bytes,
            outfit_bytes,
            scene_bytes,
        ]
        first_refs = {item.role: item for item in service.storage.list_task_references(first.id)}
        assert first_refs["person"].asset_id == person_id
        assert first_refs["person"].selection_source == "singleton"
        second_refs = {item.role: item for item in service.storage.list_task_references(second.id)}
        assert second_refs["person"].asset_id == person_id
        assert second_refs["outfit"].asset_id == outfit_id
        assert second_refs["outfit"].selection_source == "same_scene_same_day_within_ttl"
        assert second_refs["scene"].asset_id == scene_id
        assert service.storage.get_task(first.id).status == TaskStatus.SENT  # type: ignore[union-attr]
        assert service.storage.get_task(second.id).status == TaskStatus.SENT  # type: ignore[union-attr]
        await service.close()

    asyncio.run(scenario())


def test_automatic_outfit_backfill_updates_current_photo_continuity(tmp_path: Path) -> None:
    config = _config()
    service = PhotoStudioService(_Context(), config, tmp_path / "data")
    outfit_id = _add_reference(
        service,
        tmp_path,
        ReferenceCategory.OUTFIT,
        _png("green"),
        {
            "type": "dress",
            "wearing_scenes": ["home"],
            "seasons": ["summer"],
            "styles": ["casual"],
            "confidence": 1.0,
        },
    )
    parent = service.storage.create_task(
        ImageTask(
            kind="photo",
            scope_key="group:group-1",
            user_id="user-1",
            stream_id="stream-1",
            group_id="group-1",
        )
    )
    service.storage.set_task_status(
        parent.id,
        TaskStatus.SENT,
        result_metadata={"scene_signature": "bedroom-window"},
    )
    service.continuity.record_photo(
        parent.scope_key,
        "bedroom-window",
        outfit_id=None,
        scene_id=None,
        metadata={"task_id": parent.id},
    )
    backfill = ImageTask(
        kind="reference_extract",
        scope_key=parent.scope_key,
        parent_task_id=parent.id,
    )

    assert service._attach_automatic_reference_to_continuity(backfill, service.gallery.require(outfit_id))
    decision = service.continuity.decide(parent.scope_key, "bedroom-window")
    assert decision.outfit_id == outfit_id
    assert decision.outfit_reason == "same_scene_same_day_within_ttl"
    asyncio.run(service.close())


def test_photo_prefers_recently_used_outfit_when_candidates_tie(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = _config()
        config.references.auto_extract_missing = False
        ctx = _Context(
            [
                {"eligible": False, "scene_signature": "street", "reason": "public"},
                {"scene_signature": "street", "changed": False},
                {"outfit_id": None, "scene_id": None, "reason": "no preference"},
            ]
        )
        service = PhotoStudioService(ctx, config, tmp_path / "data")
        person_bytes = _png("red")
        recent_bytes = _png("green")
        unused_bytes = _png("blue")
        _add_reference(
            service,
            tmp_path,
            ReferenceCategory.PERSON,
            person_bytes,
            {"appearance_summary": "adult with dark hair", "confidence": 1.0},
        )

        recent_source = tmp_path / "recent-outfit-source.png"
        recent_reference = tmp_path / "recent-outfit-reference.png"
        recent_source.write_bytes(recent_bytes)
        recent_reference.write_bytes(recent_bytes)
        recent = service.gallery.add_reference(
            category=ReferenceCategory.OUTFIT,
            name="recent outfit",
            source_path=recent_source,
            reference_path=recent_reference,
            tags={"type": "dress", "wearing_scenes": ["daily"], "seasons": ["summer"], "styles": ["casual"]},
        )

        unused_source = tmp_path / "unused-outfit-source.png"
        unused_reference = tmp_path / "unused-outfit-reference.png"
        unused_source.write_bytes(unused_bytes)
        unused_reference.write_bytes(unused_bytes)
        service.gallery.add_reference(
            category=ReferenceCategory.OUTFIT,
            name="unused outfit",
            source_path=unused_source,
            reference_path=unused_reference,
            tags={"type": "dress", "wearing_scenes": ["daily"], "seasons": ["summer"], "styles": ["casual"]},
        )
        service.gallery.record_usage([recent.id])

        provider = _Provider([_png("white")])
        service._provider = provider  # type: ignore[assignment]
        await service.start()
        task = service.submit_photo(
            _invocation(),
            description="在街边拍一张自然照片",
            scene_hint="街道",
        )
        assert await service.tasks.drain(timeout=3)

        references = {item.role: item for item in service.storage.list_task_references(task.id)}
        assert references["outfit"].asset_id == recent.id
        assert references["outfit"].selection_source == "score"
        assert provider.calls[0][1]["images"][1] == recent_bytes
        await service.close()

    asyncio.run(scenario())


def test_cancelling_running_generation_prevents_delivery(tmp_path: Path) -> None:
    async def scenario() -> None:
        ctx = _Context(_scene_photo_llm_replies("empty-room"))
        service = PhotoStudioService(ctx, _config(), tmp_path)
        provider = _BlockingProvider(_png("red"))
        service._provider = provider  # type: ignore[assignment]
        await service.start()
        task = service.submit_scene_photo(_invocation(), description="portrait empty room")
        await asyncio.wait_for(provider.started.wait(), timeout=3)

        service.cancel_task(task.id)
        provider.release.set()
        assert await service.tasks.drain(timeout=3)

        saved = service.storage.get_task(task.id)
        assert saved is not None
        assert saved.status == TaskStatus.CANCELLED
        assert saved.result_path is None
        assert ctx.send.images == []
        await service.close()

    asyncio.run(scenario())


def test_delivery_failure_keeps_result_for_retry_and_notifies_failure(tmp_path: Path) -> None:
    async def scenario() -> None:
        ctx = _Context(_scene_photo_llm_replies("empty-room"), image_success=False)
        service = PhotoStudioService(ctx, _config(), tmp_path)
        service._provider = _Provider([_png("red")])  # type: ignore[assignment]
        await service.start()
        task = service.submit_scene_photo(_invocation(), description="portrait empty room")
        assert await service.tasks.drain(timeout=3)
        failed = service.storage.get_task(task.id)
        assert failed is not None
        assert failed.status == TaskStatus.FAILED
        assert failed.result_path is not None and failed.result_path.is_file()
        assert len(ctx.maisaka.context.append_calls) == 1
        assert ctx.maisaka.proactive.trigger_calls[0]["reason"] == "maitu_image_failed"
        await service.close()

    asyncio.run(scenario())


def test_delivery_failure_does_not_enqueue_automatic_backfill(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = _config()
        config.references.person_reference_enabled = False
        ctx = _Context(
            [
                {"eligible": False, "scene_signature": "street", "reason": "public"},
                {"scene_signature": "street", "changed": False},
            ],
            image_success=False,
        )
        service = PhotoStudioService(ctx, config, tmp_path)
        provider = _Provider([_png("red"), _png("green")])
        service._provider = provider  # type: ignore[assignment]
        await service.start()

        task = service.submit_photo(
            _invocation(),
            description="在街边拍一张自然照片",
            scene_hint="街道",
        )
        assert await service.tasks.drain(timeout=3)

        saved = service.storage.get_task(task.id)
        assert saved is not None and saved.status == TaskStatus.FAILED
        assert service.storage.list_tasks(parent_task_id=task.id) == []
        assert len(provider.calls) == 1
        await service.close()

    asyncio.run(scenario())


def test_reference_extraction_marks_paid_request_before_provider_call(tmp_path: Path) -> None:
    async def scenario() -> None:
        ctx = _Context(
            [
                {
                    "type": "dress",
                    "wearing_scenes": ["home"],
                    "seasons": ["summer"],
                    "styles": ["casual"],
                    "confidence": 1.0,
                }
            ]
        )
        service = PhotoStudioService(ctx, _config(), tmp_path)

        class InspectingProvider(_Provider):
            def __init__(self) -> None:
                super().__init__([_png("green")])
                self.task_id = ""
                self.saw_paid_request = False

            async def generate(self, prompt: str, **kwargs) -> GeneratedImage:
                task = service.storage.get_task(self.task_id)
                self.saw_paid_request = task is not None and task.paid_request_started
                return await super().generate(prompt, **kwargs)

        provider = InspectingProvider()
        service._provider = provider  # type: ignore[assignment]
        await service.start()
        task = service.submit_reference_job(
            _invocation(),
            operation="extract",
            category=ReferenceCategory.OUTFIT,
            name="summer dress",
            image=_png("blue"),
        )
        provider.task_id = task.id
        assert await service.tasks.drain(timeout=3)

        saved = service.storage.get_task(task.id)
        assert saved is not None and saved.status == TaskStatus.SENT
        assert saved.paid_request_started is True
        assert provider.saw_paid_request is True
        await service.close()

    asyncio.run(scenario())
