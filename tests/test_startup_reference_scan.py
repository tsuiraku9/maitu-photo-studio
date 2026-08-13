from __future__ import annotations

import asyncio
import io
import json
import logging
from pathlib import Path

from PIL import Image

from maitu_photo.config import PhotoPluginConfig
from maitu_photo.models import AssetStatus, ReferenceCategory
from maitu_photo.service import PhotoStudioService


def _png(colour: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (96, 72), colour).save(output, "PNG")
    return output.getvalue()


def _outfit_tags() -> dict[str, object]:
    return {
        "type": "shirt",
        "wearing_scenes": ["casual"],
        "seasons": ["summer"],
        "styles": ["minimal"],
        "confidence": 0.9,
    }


def _person_tags() -> dict[str, object]:
    return {
        "appearance_summary": "short dark hair",
        "confidence": 0.9,
    }


class _LLM:
    def __init__(self, replies: list[dict[str, object]]) -> None:
        self.replies = list(replies)
        self.calls: list[str] = []

    async def generate(self, prompt: str, **kwargs: object) -> dict[str, object]:
        del kwargs
        self.calls.append(prompt)
        reply = self.replies.pop(0)
        return {"success": True, "response": json.dumps(reply)}


class _BlockingLLM(_LLM):
    def __init__(self, reply: dict[str, object]) -> None:
        super().__init__([reply])
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, prompt: str, **kwargs: object) -> dict[str, object]:
        self.calls.append(prompt)
        self.started.set()
        await self.release.wait()
        return {"success": True, "response": json.dumps(self.replies[0])}


class _Context:
    def __init__(self, llm: _LLM) -> None:
        self.logger = logging.getLogger("test.maitu.startup-scan")
        self.llm = llm


def _config() -> PhotoPluginConfig:
    config = PhotoPluginConfig()
    config.tasks.poll_interval_seconds = 0.01
    return config


def test_startup_scan_imports_and_removes_direct_drop(tmp_path: Path) -> None:
    async def scenario() -> None:
        drop = tmp_path / "references" / "outfit" / "summer-shirt.png"
        drop.parent.mkdir(parents=True)
        drop.write_bytes(_png("green"))
        llm = _LLM([_outfit_tags()])
        service = PhotoStudioService(_Context(llm), _config(), tmp_path)

        await service.start()
        result = await service.wait_for_startup_reference_scan()

        assert result["scanned"] == 1
        assert result["imported"] == 1
        assert not drop.exists()
        assets = service.storage.list_reference_assets(category=ReferenceCategory.OUTFIT)
        assert len(assets) == 1
        assert assets[0].status == AssetStatus.ACTIVE
        assert assets[0].reference_path.suffix == ".jpg"
        assert assets[0].reference_path.stat().st_size <= 480_000
        assert len(llm.calls) == 1
        await service.close()

    asyncio.run(scenario())


def test_startup_scan_skips_registered_files_and_removes_duplicate_drop(tmp_path: Path) -> None:
    async def scenario() -> None:
        first_drop = tmp_path / "references" / "outfit" / "first.png"
        first_drop.parent.mkdir(parents=True)
        first_drop.write_bytes(_png("blue"))
        first_llm = _LLM([_outfit_tags()])
        first = PhotoStudioService(_Context(first_llm), _config(), tmp_path)
        await first.start()
        await first.wait_for_startup_reference_scan()
        asset = first.storage.list_reference_assets(category=ReferenceCategory.OUTFIT)[0]
        await first.close()

        duplicate = tmp_path / "references" / "outfit" / "copied-reference.jpg"
        duplicate.write_bytes(asset.reference_path.read_bytes())
        second_llm = _LLM([])
        second = PhotoStudioService(_Context(second_llm), _config(), tmp_path)
        await second.start()
        result = await second.wait_for_startup_reference_scan()

        assert result["imported"] == 0
        assert result["duplicates"] == 1
        assert not duplicate.exists()
        assert len(second.storage.list_reference_assets(category=ReferenceCategory.OUTFIT)) == 1
        assert second_llm.calls == []
        await second.close()

    asyncio.run(scenario())


def test_startup_scan_keeps_person_drop_when_singleton_exists(tmp_path: Path) -> None:
    async def scenario() -> None:
        person_dir = tmp_path / "references" / "person"
        person_dir.mkdir(parents=True)
        first_drop = person_dir / "first.png"
        first_drop.write_bytes(_png("red"))
        llm = _LLM([_person_tags()])
        service = PhotoStudioService(_Context(llm), _config(), tmp_path)
        await service.start()
        await service.wait_for_startup_reference_scan()

        second_drop = person_dir / "second.png"
        second_drop.write_bytes(_png("yellow"))
        result = await service.scan_reference_folders()

        assert result["person_conflicts"] == 1
        assert second_drop.exists()
        assert len(service.storage.list_reference_assets(category=ReferenceCategory.PERSON)) == 1
        assert len(llm.calls) == 1
        await service.close()

    asyncio.run(scenario())


def test_close_cancels_startup_scan_without_deleting_drop(tmp_path: Path) -> None:
    async def scenario() -> None:
        drop = tmp_path / "references" / "outfit" / "waiting.png"
        drop.parent.mkdir(parents=True)
        drop.write_bytes(_png("purple"))
        llm = _BlockingLLM(_outfit_tags())
        service = PhotoStudioService(_Context(llm), _config(), tmp_path)
        await service.start()
        await asyncio.wait_for(llm.started.wait(), timeout=3)

        await service.close()

        assert drop.exists()

    asyncio.run(scenario())
