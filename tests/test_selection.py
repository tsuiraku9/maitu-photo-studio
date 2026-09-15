from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from maitu_photo.config import PhotoPluginConfig
from maitu_photo.models import ReferenceAsset, ReferenceCategory
from maitu_photo.selection import ReferenceSelector


class _Gallery:
    def __init__(self, assets: list[ReferenceAsset]) -> None:
        self.assets = assets

    def get(self, asset_id: str) -> ReferenceAsset | None:
        return next((asset for asset in self.assets if asset.id == asset_id), None)

    def candidates(self, category: str, *, include_disabled: bool = False) -> list[ReferenceAsset]:
        assert include_disabled is False
        return [asset for asset in self.assets if asset.category == ReferenceCategory(category)]


class _Continuity:
    def get(self, scope_key: str) -> None:
        del scope_key
        return None


class _LLM:
    async def generate_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        del prompt, kwargs
        return {"outfit_id": None, "scene_id": None}


class _Prompts:
    def __init__(self) -> None:
        self.candidate_json = ""

    def render(self, name: str, **kwargs: Any) -> str:
        assert name == "select_references"
        self.candidate_json = str(kwargs["candidate_json"])
        return "select references"


def _asset(category: ReferenceCategory, index: int) -> ReferenceAsset:
    tags: dict[str, Any] = {"styles": ["casual"]}
    if category == ReferenceCategory.SCENE:
        tags = {"privacy_eligible": True, "scene_signature": f"room-{index}"}
    return ReferenceAsset(
        category=category,
        name=f"{category.value}-{index:02d}",
        source_path=Path(f"source-{category.value}-{index}.jpg"),
        reference_path=Path(f"reference-{category.value}-{index}.jpg"),
        sha256=f"{category.value}-{index}",
        id=f"{category.value}-{index:02d}",
        tags=tags,
    )


@pytest.mark.parametrize(("configured_limit", "expected_per_category"), [(2, 2), (12, 12)])
def test_selection_candidate_limit_is_applied_independently_per_category(
    configured_limit: int, expected_per_category: int
) -> None:
    assets = [
        *[_asset(ReferenceCategory.OUTFIT, index) for index in range(14)],
        *[_asset(ReferenceCategory.SCENE, index) for index in range(14)],
    ]
    config = PhotoPluginConfig()
    config.continuity.enabled = False
    config.model_tasks.selection_candidate_limit_per_category = configured_limit
    prompts = _Prompts()
    selector = ReferenceSelector(_Gallery(assets), _Continuity(), _LLM(), prompts, config)

    asyncio.run(
        selector.select(
            scope_key="stream:test",
            description="bedroom photo",
            scene_signature="bedroom",
            scene_eligible=True,
        )
    )

    candidates = json.loads(prompts.candidate_json)
    outfit_ids = [item["id"] for item in candidates if item["category"] == "outfit"]
    scene_ids = [item["id"] for item in candidates if item["category"] == "scene"]
    assert outfit_ids == [f"outfit-{index:02d}" for index in range(expected_per_category)]
    assert scene_ids == [f"scene-{index:02d}" for index in range(expected_per_category)]
    assert [item["category"] for item in candidates] == [
        *(["outfit"] * expected_per_category),
        *(["scene"] * expected_per_category),
    ]
