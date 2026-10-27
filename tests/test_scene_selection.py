"""场景长句匹配回归测试；可用标准库 unittest 和已有运行依赖执行。"""

from __future__ import annotations

import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from maitu_photo.config import PhotoPluginConfig
from maitu_photo.continuity import ContinuityManager
from maitu_photo.gallery import ReferenceGallery
from maitu_photo.models import AssetStatus, ReferenceAsset, ReferenceCategory, TaskStatus
from maitu_photo.prompts import PromptService
from maitu_photo.provider import GeneratedImage
from maitu_photo.runtime import InvocationContext
from maitu_photo.selection import ReferenceSelector
from maitu_photo.service import PhotoStudioService
from maitu_photo.storage import SQLiteStorage


class _LLM:
    async def generate_json(self, prompt: str, **kwargs: object) -> dict:
        return {"outfit_id": None, "scene_id": None}

    async def generate(self, prompt: str, **kwargs: object) -> dict:
        if "eligible" in prompt:
            reply = {"eligible": True, "scene_signature": "bedroom-window"}
        elif "outfit_id" in prompt:
            reply = {"outfit_id": None, "scene_id": None}
        else:
            reply = {"scene_signature": "bedroom-window", "changed": False}
        return {"success": True, "response": json.dumps(reply)}


class _Provider:
    def __init__(self, image: bytes) -> None:
        self.image = image
        self.calls: list[dict] = []

    async def generate(self, prompt: str, **kwargs: object) -> GeneratedImage:
        self.calls.append(kwargs)
        return GeneratedImage(self.image, media_type="image/png")

    async def aclose(self) -> None:
        pass


class SceneSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = SQLiteStorage(self.root / "selection.sqlite3")
        self.addCleanup(self.storage.close)
        self.gallery = ReferenceGallery(self.storage)
        self.continuity = ContinuityManager(self.storage)
        self.config = PhotoPluginConfig()
        self.asset_count = 0
        self.selector = ReferenceSelector(
            self.gallery, self.continuity, _LLM(), PromptService(self.config.prompts), self.config
        )

    def add_scene(self, room: str = "bedroom", *, name: str = "参考板", **kwargs: object) -> ReferenceAsset:
        self.asset_count += 1
        return self.gallery.add_reference(
            category="scene",
            name=name,
            source_path=self.root / "source.jpg",
            reference_path=self.root / "reference.jpg",
            sha256=f"{room}-{self.asset_count}",
            tags={"room_type": room, "scene_signature": room + "-window", "privacy_eligible": True},
            **kwargs,
        )

    async def select(self, hint: str, **kwargs: object):
        return await self.selector.select(
            scope_key="stream:test", description=hint, scene_hint=hint,
            scene_signature="bedroom-window", scene_eligible=True, allow_outfit=False, **kwargs
        )

    async def test_natural_sentences_and_room_aliases_reuse_existing_scene(self) -> None:
        for room, hint in (
            ("bedroom", "卧室窗边"),
            ("bedroom", "在卧室窗边喝茶，温暖的午后阳光"),
            ("卧室", "a photo beside the window in the bedroom"),
            ("living_room", "在客厅沙发上看书"),
            ("bathroom", "浴室镜子前的自拍"),
        ):
            with self.subTest(room=room, hint=hint):
                scene = self.add_scene(room)
                result = await self.select(hint)
                self.assertEqual(result.scene.id, scene.id)
                self.assertEqual(result.reasons["scene"], "score")
                self.gallery.disable(scene.id)

    async def test_continuity_is_not_lost_for_long_scene_description(self) -> None:
        scene = self.add_scene()
        self.continuity.record_photo("stream:test", "bedroom-window", outfit_id=None, scene_id=scene.id)
        result = await self.select("仍在卧室窗边，换个姿势拍一张")
        self.assertEqual(result.scene.id, scene.id)
        self.assertEqual(result.reasons["scene"], "same_scene_same_day_within_ttl")

    async def test_room_conflict_is_not_overridden_by_matching_style(self) -> None:
        self.add_scene("bathroom", name="温暖浴室")
        result = await self.select("卧室窗边", scene_style="温暖")
        self.assertIsNone(result.scene)

    async def test_style_must_match_independently_of_room(self) -> None:
        self.add_scene(name="明亮卧室")
        result = await self.select("卧室窗边", scene_style="暗黑风格卧室")
        self.assertIsNone(result.scene)

    async def test_matching_style_can_be_embedded_in_sentence(self) -> None:
        scene = self.add_scene(name="温馨卧室")
        result = await self.select("卧室窗边", scene_style="温馨的卧室")
        self.assertEqual(result.scene.id, scene.id)

    async def test_disabled_or_nonprivate_scenes_remain_unselectable(self) -> None:
        scene = self.add_scene(status=AssetStatus.DISABLED)
        self.assertIsNone((await self.select("卧室窗边")).scene)
        scene.status = AssetStatus.ACTIVE
        scene.tags["privacy_eligible"] = False
        self.storage.update_reference_asset(scene)
        self.assertIsNone((await self.select("卧室窗边")).scene)

    async def test_force_new_scene_still_excludes_previous_scene(self) -> None:
        scene = self.add_scene()
        self.continuity.record_photo("stream:test", "bedroom-window", outfit_id=None, scene_id=scene.id)
        self.assertIsNone((await self.select("卧室窗边", force_new_scene=True)).scene)

    async def test_explicit_scene_id_still_takes_priority(self) -> None:
        scene = self.add_scene()
        result = await self.select("卧室窗边", explicit_scene_id=scene.id)
        self.assertEqual(result.scene.id, scene.id)
        self.assertEqual(result.reasons["scene"], "explicit")

    async def test_generic_english_word_does_not_match_room_name_substring(self) -> None:
        self.add_scene()
        self.assertIsNone((await self.select("a room with a loom")).scene)

    async def test_metadata_is_not_used_as_scene_content(self) -> None:
        self.add_scene(selection_metadata={"source_kind": "loft"})
        self.assertIsNone((await self.select("loft")).scene)

    async def test_empty_hint_keeps_existing_candidates(self) -> None:
        scene = self.add_scene()
        self.assertEqual((await self.select("")).scene.id, scene.id)

    async def test_photo_uses_scene_image_and_does_not_enqueue_scene_backfill(self) -> None:
        async def send_image(*args: object, **kwargs: object) -> dict:
            return {"sent": True, "message_id": "synthetic-message"}

        config = PhotoPluginConfig()
        config.openai.base_url = "https://provider.example"
        config.openai.api_key = "synthetic-test-key"
        config.openai.generation_model = "synthetic-image-model"
        config.references.person_reference_enabled = False
        config.references.outfit_reference_enabled = False
        config.output.notify_planner = False
        config.tasks.poll_interval_seconds = 0.01
        ctx = SimpleNamespace(
            logger=logging.getLogger("test.scene-selection"), llm=_LLM(), send=SimpleNamespace(image=send_image)
        )
        service = PhotoStudioService(ctx, config, self.root / "service")
        self.addAsyncCleanup(service.close)
        image = io.BytesIO()
        Image.new("RGB", (16, 12), "blue").save(image, "PNG")
        data = image.getvalue()
        reference = self.root / "scene.png"
        reference.write_bytes(data)
        scene = service.gallery.add_reference(
            category="scene", name="参考板", source_path=reference, reference_path=reference,
            tags={"room_type": "bedroom", "scene_signature": "bedroom-window", "privacy_eligible": True},
        )
        provider = _Provider(data)
        service._provider = provider
        await service.start()
        invocation = InvocationContext(
            stream_id="stream:test", scope_key="stream:test", user_id="test",
            group_id=None, message_id="synthetic-message", message={},
        )
        task = service.submit_photo(invocation, description="在卧室窗边拍照", scene_hint="卧室窗边")
        self.assertTrue(await service.tasks.drain(timeout=3))
        saved = service.storage.get_task(task.id)
        self.assertEqual(saved.status, TaskStatus.SENT)
        self.assertEqual(saved.result_metadata["scene_id"], scene.id)
        self.assertEqual(provider.calls[0]["images"], [data])
        children = service.storage.connection.execute(
            "SELECT COUNT(*) FROM image_tasks WHERE parent_task_id = ?", (task.id,)
        ).fetchone()[0]
        self.assertEqual(children, 0)


if __name__ == "__main__":
    unittest.main()
