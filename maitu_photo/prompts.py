"""Safe rendering and validation of all configurable LLM prompts."""

from __future__ import annotations

import string
from dataclasses import dataclass
from typing import Any, Iterable

from .config import PromptSection


class PromptTemplateError(ValueError):
    """Raised when a configured prompt contains an unknown placeholder."""


class _StrictMapping(dict[str, Any]):
    def __missing__(self, key: str) -> Any:
        raise PromptTemplateError(f"提示词缺少参数: {key}")


def template_fields(template: str) -> set[str]:
    fields: set[str] = set()
    for _, field_name, _, _ in string.Formatter().parse(template or ""):
        if field_name:
            fields.add(field_name.split(".", 1)[0].split("[", 1)[0])
    return fields


def validate_template(template: str, allowed: Iterable[str]) -> None:
    unknown = template_fields(template) - set(allowed)
    if unknown:
        raise PromptTemplateError(f"提示词包含未允许的占位符: {', '.join(sorted(unknown))}")


def render_prompt(template: str, allowed: Iterable[str], **values: Any) -> str:
    validate_template(template, allowed)
    return template.format_map(_StrictMapping(values)).strip()


@dataclass
class PromptService:
    """Central prompt renderer; swapping a config is atomic at the caller."""

    config: PromptSection

    def render(self, name: str, **values: Any) -> str:
        template = getattr(self.config, name)
        allowed = {
            "user_prompt",
            "negative_instruction",
            "description",
            "person_prompt",
            "outfit_prompt",
            "scene_prompt",
            "reference_labels",
            "negative_prompt",
            "person_style",
            "scene_hint",
            "candidate_json",
            "task_id",
            "error",
            "scene_signature",
            "selected_tags",
            "source_summary",
            "nickname",
            "personality",
            "appearance_hint",
        }
        return render_prompt(template, allowed, **values)

    def render_tool(self, name: str, fallback_brief: str, fallback_detailed: str) -> tuple[str, str]:
        section = getattr(self.config, name)
        brief = section.brief.strip() or fallback_brief
        detailed = section.detailed.strip() or fallback_detailed
        return brief, detailed
