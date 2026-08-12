from __future__ import annotations

import asyncio

from maitu_photo.config import PhotoPluginConfig
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
        "manage_reference_gallery",
        "get_image_task_status",
    }


def test_gallery_tool_rejects_non_admin_before_service_access() -> None:
    config = PhotoPluginConfig()
    config.plugin.admin_user_ids = ["admin"]
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
