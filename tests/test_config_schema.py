from __future__ import annotations

import re

import pytest
from maibot_sdk.config import generate_plugin_config_schema

from maitu_photo.config import (
    ContinuitySection,
    LoggingSection,
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
from maitu_photo.prompts import PromptService, PromptTemplateError

_CHINESE_TEXT = re.compile(r"[\u4e00-\u9fff]")


def test_every_webui_field_has_chinese_label_description_and_hint() -> None:
    config = PhotoPluginConfig()
    assert config.plugin.config_version == "1.4.0"
    assert config.references.require_person_reference is True
    schema = generate_plugin_config_schema(PhotoPluginConfig)
    fields = [field for section in schema["sections"].values() for field in section["fields"].values()]

    assert len(fields) == 101
    for field in fields:
        assert _CHINESE_TEXT.search(field["label"]), field["name"]
        assert _CHINESE_TEXT.search(field["description"]), field["name"]
        assert _CHINESE_TEXT.search(field["hint"]), field["name"]
    prompt_fields = schema["sections"]["prompts"]["fields"]
    assert "{description}" in prompt_fields["scene_photo_user"]["hint"]
    assert "{person_prompt}" in prompt_fields["photo_user"]["hint"]
    assert "generate_scene_photo" in prompt_fields["generate_scene_photo_brief"]["hint"]
    assert prompt_fields["generate_scene_photo_brief"]["ui_type"] == "textarea"


def test_default_scene_tag_prompt_requests_privacy_eligibility() -> None:
    assert '"privacy_eligible":false' in PhotoPluginConfig().prompts.tag_scene


def test_generation_templates_do_not_expose_reference_labels() -> None:
    config = PhotoPluginConfig()
    schema = generate_plugin_config_schema(PhotoPluginConfig)
    prompt_fields = schema["sections"]["prompts"]["fields"]

    assert "reference_labels" not in config.prompts.photo_user
    assert "reference_labels" not in config.prompts.scene_photo_user
    assert "reference_labels" not in prompt_fields["photo_user"]["hint"]
    assert "reference_labels" not in prompt_fields["scene_photo_user"]["hint"]


@pytest.mark.parametrize("field_name", ["photo_user", "scene_photo_user"])
def test_generation_templates_reject_removed_reference_labels_placeholder(field_name: str) -> None:
    with pytest.raises(ValueError, match="reference_labels 已不再支持"):
        PhotoPluginConfig.model_validate({"prompts": {field_name: "参考图说明：{reference_labels}"}})


def test_prompt_renderer_defensively_rejects_removed_reference_labels_placeholder() -> None:
    config = PhotoPluginConfig()
    object.__setattr__(config.prompts, "photo_user", "参考图说明：{reference_labels}")

    with pytest.raises(PromptTemplateError, match="reference_labels"):
        PromptService(config.prompts).render(
            "photo_user",
            description="拍摄需求",
            person_prompt="人物约束",
            outfit_prompt="服装约束",
            scene_prompt="场景约束",
            negative_prompt="负面词",
        )


def test_generation_retry_settings_have_safe_defaults_and_bounds() -> None:
    config = PhotoPluginConfig()

    assert config.openai.generation_max_retries == 0
    assert config.openai.generation_retry_backoff_seconds == 1.0

    with pytest.raises(ValueError, match="generation_max_retries"):
        PhotoPluginConfig.model_validate({"openai": {"generation_max_retries": 6}})
    with pytest.raises(ValueError, match="generation_retry_backoff_seconds"):
        PhotoPluginConfig.model_validate({"openai": {"generation_retry_backoff_seconds": -0.1}})
    with pytest.raises(ValueError, match="不能使用布尔值"):
        PhotoPluginConfig.model_validate({"openai": {"generation_max_retries": True}})
    with pytest.raises(ValueError, match="不能使用布尔值"):
        PhotoPluginConfig.model_validate({"openai": {"generation_retry_backoff_seconds": False}})


def test_legacy_broken_scene_tag_prompt_is_migrated() -> None:
    legacy_prompt = (
        "请分析场景参考板并只输出符合 Schema 的 JSON："
        '{{"room_type":"","scene_signature":"","confidence":0}}。'
        "时间和光线由每次生图任务自行判断；confidence 必须是 0 到 1 之间的小数。"
    )

    config = PhotoPluginConfig.model_validate({"prompts": {"tag_scene": legacy_prompt}})

    assert '"privacy_eligible":false' in config.prompts.tag_scene


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
        "logging",
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
        LoggingSection,
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
