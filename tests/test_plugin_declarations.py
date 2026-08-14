from __future__ import annotations

import asyncio
from pathlib import Path

from maitu_photo.config import PhotoPluginConfig
from maitu_photo.models import ReferenceAsset, ReferenceCategory
from maitu_photo.runtime import invocation_context
from plugin import MaiTuPhotoPlugin


def _inject_config(plugin: MaiTuPhotoPlugin, config: PhotoPluginConfig) -> None:
    if hasattr(plugin, "set_plugin_config"):
        plugin.set_plugin_config(config.model_dump(mode="python"))  # type: ignore[attr-defined]
    else:
        plugin._config = config  # type: ignore[attr-defined]


def test_component_metadata_uses_configured_descriptions_and_prefix() -> None:
    config = PhotoPluginConfig()
    config.plugin.command_prefix = "/photo"
    config.prompts.generate_scene_photo_brief = "custom scene photo tool"
    config.prompts.generate_scene_photo_description = "custom description field"
    plugin = MaiTuPhotoPlugin()
    _inject_config(plugin, config)

    components = {item["name"]: item for item in plugin.get_components()}

    scene_tool = components["generate_scene_photo"]["metadata"]
    assert scene_tool["description"] == "custom scene photo tool"
    parameters = {item["name"]: item for item in scene_tool["parameters"]}
    assert parameters["description"]["description"] == "custom description field"
    assert components["maitu_admin"]["metadata"]["command_pattern"] == r"^/photo(?:\s+.*)?$"


def test_components_can_be_discovered_before_config_injection() -> None:
    plugin = MaiTuPhotoPlugin()

    components = plugin.get_components()

    assert {item["name"] for item in components} == {
        "maitu_admin",
        "generate_scene_photo",
        "generate_photo",
        "get_image_task_status",
    }


def test_gallery_tool_is_registered_when_planner_management_is_enabled() -> None:
    config = PhotoPluginConfig()
    config.references.planner_gallery_management_enabled = True
    plugin = MaiTuPhotoPlugin()
    _inject_config(plugin, config)

    assert "manage_reference_gallery" in {item["name"] for item in plugin.get_components()}


def test_gallery_tool_rejects_calls_when_planner_management_is_disabled() -> None:
    config = PhotoPluginConfig()
    plugin = MaiTuPhotoPlugin()
    _inject_config(plugin, config)
    message = {
        "message_id": "m1",
        "session_id": "s1",
        "message_info": {"user_info": {"user_id": "ordinary"}},
    }

    result = asyncio.run(
        plugin.handle_manage_reference_gallery(
            operation="list",
            stream_id="s1",
            message=message,
        )
    )

    assert result["success"] is False
    assert "关闭 Planner" in result["error"]


def test_gallery_tool_rejects_non_admin_when_planner_management_is_enabled() -> None:
    config = PhotoPluginConfig()
    config.plugin.admin_user_ids = ["admin"]
    config.references.planner_gallery_management_enabled = True
    plugin = MaiTuPhotoPlugin()
    _inject_config(plugin, config)
    message = {
        "message_id": "m1",
        "session_id": "s1",
        "message_info": {"user_info": {"user_id": "ordinary"}},
    }

    result = asyncio.run(
        plugin.handle_manage_reference_gallery(
            operation="list",
            stream_id="s1",
            message=message,
        )
    )

    assert result["success"] is False
    assert "管理员" in result["error"]


def test_gallery_tool_allows_admin_when_planner_management_is_enabled() -> None:
    config = PhotoPluginConfig()
    config.plugin.admin_user_ids = ["admin"]
    config.references.planner_gallery_management_enabled = True
    plugin = MaiTuPhotoPlugin()
    _inject_config(plugin, config)
    message = {
        "message_id": "m1",
        "session_id": "s1",
        "message_info": {"user_info": {"user_id": "admin"}},
    }

    async def fake_manage_reference(*args: object, **kwargs: object) -> dict[str, object]:
        return {"success": True, "operation": kwargs["operation"]}

    plugin._manage_reference = fake_manage_reference  # type: ignore[method-assign]
    result = asyncio.run(
        plugin.handle_manage_reference_gallery(
            operation="list",
            stream_id="s1",
            message=message,
        )
    )

    assert result == {"success": True, "operation": "list"}


def test_invocation_falls_back_to_private_stream_scope() -> None:
    invocation = invocation_context(
        {
            "stream_id": "private-stream",
            "message": {
                "message_info": {"user_info": {"user_id": "u1"}},
            },
        }
    )
    assert invocation.scope_key == "stream:private-stream"


def test_reference_asset_formatter_uses_chinese_labels_and_readable_tag_values() -> None:
    asset = ReferenceAsset(
        category=ReferenceCategory.OUTFIT,
        name="自动补库-服装-12345678",
        source_path=Path("source.jpg"),
        reference_path=Path("reference.jpg"),
        sha256="a" * 64,
        tags={
            "type": "cozy knit set",
            "wearing_scenes": ["home", "loungewear"],
            "seasons": ["spring", "summer"],
            "styles": ["cute", "minimalist"],
            "confidence": 0.85,
        },
    )

    text = MaiTuPhotoPlugin._format_asset(asset, compact=True)

    assert "服装参考图" in text
    assert "状态：已启用，可用于生图" in text
    assert "ID：" + asset.id in text
    assert "服装类型：舒适针织套装" in text
    assert "适用场景：居家、家居休闲" in text
    assert "适用季节：春季、夏季" in text
    assert "风格：甜美、极简" in text
    assert "标签置信度：85%" in text
    assert "tags=" not in text


def test_reference_asset_formatter_makes_scene_signature_readable() -> None:
    asset = ReferenceAsset(
        category=ReferenceCategory.SCENE,
        name="卧室窗边",
        source_path=Path("scene-source.jpg"),
        reference_path=Path("scene-reference.jpg"),
        sha256="b" * 64,
        tags={
            "room_type": "bedroom",
            "privacy_eligible": True,
            "scene_signature": "minimalist_bedroom_window",
            "confidence": 0.92,
        },
    )

    text = MaiTuPhotoPlugin._format_asset(asset, compact=True)

    assert "房间类型：卧室" in text
    assert "私密空间资格：是" in text
    assert "场景指纹：minimalist bedroom window" in text

    legacy = ReferenceAsset(
        category=ReferenceCategory.SCENE,
        name="旧场景",
        source_path=Path("legacy-source.jpg"),
        reference_path=Path("legacy-reference.jpg"),
        sha256="c" * 64,
        tags={"room_type": "bedroom", "scene_signature": "legacy-bedroom", "confidence": 0.8},
    )

    assert legacy.is_selectable is False
    assert "状态：待审核，尚未确认私密空间资格" in MaiTuPhotoPlugin._format_asset(legacy)
