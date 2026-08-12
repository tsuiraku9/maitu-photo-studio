from __future__ import annotations

import re

from maibot_sdk.config import generate_plugin_config_schema

from maitu_photo.config import (
    ContinuitySection,
    ModelTaskSection,
    OpenAISection,
    OutputSection,
    PhotoPluginConfig,
    PluginSection,
    PromptSection,
    ReferenceSection,
    TaskSection,
    ToolDescriptionSection,
)

_CHINESE_TEXT = re.compile(r"[\u4e00-\u9fff]")


def test_every_webui_field_has_chinese_label_description_and_hint() -> None:
    schema = generate_plugin_config_schema(PhotoPluginConfig)
    fields = [field for section in schema["sections"].values() for field in section["fields"].values()]

    assert len(fields) == 94
    for field in fields:
        assert _CHINESE_TEXT.search(field["label"]), field["name"]
        assert _CHINESE_TEXT.search(field["description"]), field["name"]
        assert _CHINESE_TEXT.search(field["hint"]), field["name"]
    prompt_fields = schema["sections"]["prompts"]["fields"]
    assert "{description}" in prompt_fields["scene_photo_user"]["hint"]
    assert "{person_prompt}" in prompt_fields["photo_user"]["hint"]
    assert "generate_scene_photo" in prompt_fields["generate_scene_photo_brief"]["hint"]
    assert prompt_fields["generate_scene_photo_brief"]["ui_type"] == "textarea"


def test_every_config_section_has_chinese_title_and_description() -> None:
    schema = generate_plugin_config_schema(PhotoPluginConfig)

    assert set(schema["sections"]) == {
        "plugin",
        "openai",
        "model_tasks",
        "references",
        "continuity",
        "tasks",
        "output",
        "prompts",
    }
    for section in schema["sections"].values():
        assert _CHINESE_TEXT.search(section["title"]), section["name"]
        assert _CHINESE_TEXT.search(section["description"]), section["name"]


def test_nested_tool_description_fields_keep_chinese_webui_metadata() -> None:
    config_models = (
        PluginSection,
        OpenAISection,
        ModelTaskSection,
        ReferenceSection,
        ContinuitySection,
        TaskSection,
        OutputSection,
        ToolDescriptionSection,
        PromptSection,
    )

    for config_model in config_models:
        for field_name, field in config_model.model_fields.items():
            metadata = field.json_schema_extra
            assert isinstance(metadata, dict), f"{config_model.__name__}.{field_name}"
            assert _CHINESE_TEXT.search(str(metadata.get("label", ""))), field_name
            assert _CHINESE_TEXT.search(str(metadata.get("hint", ""))), field_name


def test_legacy_nested_tool_sections_flatten_into_webui_fields() -> None:
    config = PhotoPluginConfig.model_validate(
        {
            "prompts": {
                "generate_scene_photo_tool": {
                    "brief": "legacy scene brief",
                    "detailed": "legacy scene detailed",
                    "parameters": {"description": "legacy scene description"},
                }
            }
        }
    )

    assert config.prompts.generate_scene_photo_brief == "legacy scene brief"
    assert config.prompts.generate_scene_photo_detailed == "legacy scene detailed"
    assert config.prompts.generate_scene_photo_description == "legacy scene description"
    assert config.prompts.generate_scene_photo_tool.brief == "legacy scene brief"
