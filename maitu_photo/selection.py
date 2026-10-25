"""Reference selection and scene continuity orchestration."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Any

from .llm_adapter import LLMAdapterError, MaiBotLLMAdapter
from .models import ReferenceAsset, ReferenceCategory
from .prompts import PromptService


@dataclass(frozen=True)
class SelectionResult:
    outfit: ReferenceAsset | None
    scene: ReferenceAsset | None
    scene_signature: str
    scene_eligible: bool
    reasons: dict[str, str]


def _tag_values(asset: ReferenceAsset, key: str) -> set[str]:
    value = asset.effective_tags.get(key, [])
    if isinstance(value, str):
        return {value.strip().lower()} if value.strip() else set()
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip().lower() for item in value if str(item).strip()}
    return set()


class ReferenceSelector:
    """Select references without ever placing image bytes in the LLM prompt."""

    def __init__(
        self, gallery: Any, continuity: Any, llm: MaiBotLLMAdapter, prompts: PromptService, config: Any
    ) -> None:
        self.gallery = gallery
        self.continuity = continuity
        self.llm = llm
        self.prompts = prompts
        self.config = config

    def _config_value(self, name: str, default: Any) -> Any:
        if hasattr(self.config, name):
            return getattr(self.config, name)
        model_tasks = getattr(self.config, "model_tasks", None)
        if model_tasks is not None and hasattr(model_tasks, name):
            return getattr(model_tasks, name)
        continuity = getattr(self.config, "continuity", None)
        if name == "enabled" and continuity is not None:
            return getattr(continuity, name, default)
        return default

    @staticmethod
    def _score(asset: ReferenceAsset, hint: str) -> int:
        query = {word.lower() for word in str(hint or "").replace("，", ",").split(",") if word.strip()}
        if not query:
            return 0
        tags = set()
        for value in asset.effective_tags.values():
            if isinstance(value, str):
                tags.add(value.lower())
            elif isinstance(value, (list, tuple, set)):
                tags.update(str(item).lower() for item in value)
        return len(query & tags)

    @staticmethod
    def _matches_intent(asset: ReferenceAsset, hint: str) -> bool:
        """Require a tag/style match before a reference can enter selection.

        A free-form hint is intentionally treated as a filter only when it has
        usable tokens.  This keeps old galleries selectable while ensuring a
        configured style never silently picks an unrelated board.
        """

        tokens = _intent_tokens(hint)
        if not tokens:
            return True
        searchable = _normalized_asset_text(asset)
        return any(token in searchable for token in tokens)

    async def select(
        self,
        *,
        scope_key: str,
        description: str,
        outfit_hint: str = "",
        outfit_style: str = "",
        scene_hint: str = "",
        scene_style: str = "",
        explicit_outfit_id: str = "",
        explicit_scene_id: str = "",
        force_new_outfit: bool = False,
        force_new_scene: bool = False,
        allow_outfit: bool = True,
        allow_scene: bool = True,
        scene_signature: str = "",
        scene_eligible: bool | None = None,
        now: Any = None,
    ) -> SelectionResult:
        outfit = None
        scene = None
        reasons: dict[str, str] = {}
        outfit_intent = _combine_intent(outfit_hint, outfit_style)
        scene_intent = _combine_intent(scene_hint, scene_style)

        eligibility_signature = scene_signature.strip()
        if scene_eligible is None:
            try:
                target = await self.llm.generate_json(
                    self.prompts.render(
                        "scene_eligibility",
                        description=description,
                        scene_hint=_combine_intent(scene_hint, scene_style),
                    ),
                    task_name=self._config_value("selection_task_name", "utils"),
                    temperature=self._config_value("temperature", 0.1),
                    max_tokens=self._config_value("max_tokens", 2048),
                )
                if not isinstance(target, dict):
                    raise ValueError("scene eligibility response must be an object")
                if not isinstance(target.get("eligible"), bool):
                    raise ValueError("scene eligibility must be boolean")
                scene_eligible = bool(target["eligible"])
                eligibility_signature = str(target.get("scene_signature") or eligibility_signature).strip()
            except (LLMAdapterError, ValueError, TypeError):
                scene_eligible = _fallback_scene_eligible(scene_hint, description)
                reasons["scene_analysis"] = "llm_failed_deterministic_fallback"

        if not scene_signature.strip():
            try:
                # A dedicated scene hint is already the planner's normalized
                # location intent.  Mixing the full photo description back in
                # reintroduces clothes, poses, and props, which makes the same
                # room appear to change between consecutive photos.
                signature_source = scene_hint.strip() or scene_style.strip() or description.strip()
                signature_result = await self.llm.generate_json(
                    self.prompts.render(
                        "scene_signature",
                        scene_hint=signature_source,
                    ),
                    task_name=self._config_value("selection_task_name", "utils"),
                    temperature=self._config_value("temperature", 0.1),
                    max_tokens=self._config_value("max_tokens", 2048),
                )
                if not isinstance(signature_result, dict):
                    raise ValueError("scene signature response must be an object")
                scene_signature = str(signature_result.get("scene_signature") or "").strip()
                if not scene_signature:
                    raise ValueError("scene signature must not be empty")
            except (LLMAdapterError, ValueError, TypeError):
                scene_signature = eligibility_signature
                reasons["scene_signature"] = "llm_failed_deterministic_fallback"
        scene_signature = _normalize_signature(scene_signature or scene_hint or description)

        if not allow_outfit and explicit_outfit_id:
            raise ValueError("已指定服装参考 ID，但 use_outfit_reference=false")
        if not allow_scene and explicit_scene_id:
            raise ValueError("已指定场景参考 ID，但 use_scene_reference=false")
        if explicit_scene_id and not scene_eligible:
            raise ValueError("目标场景不属于允许使用场景参考图的私密小空间")

        if explicit_outfit_id and allow_outfit:
            outfit = self.gallery.get(explicit_outfit_id)
            if outfit is None or not outfit.is_selectable or outfit.category != ReferenceCategory.OUTFIT:
                raise ValueError(f"服装参考图不可用: {explicit_outfit_id}")
            reasons["outfit"] = "explicit"
        if explicit_scene_id and allow_scene:
            scene = self.gallery.get(explicit_scene_id)
            if scene is None or not scene.is_selectable or scene.category != ReferenceCategory.SCENE:
                raise ValueError(f"场景参考图不可用: {explicit_scene_id}")
            reasons["scene"] = "explicit"

        previous = self.continuity.get(scope_key)
        fallback_probability = float(self._config_value("outfit_text_fallback_probability", 0.0) or 0.0)
        probabilistic_outfit_fallback = (
            allow_outfit
            and not explicit_outfit_id
            and not force_new_outfit
            and fallback_probability > 0
            and random.random() < fallback_probability
        )
        if self._config_value("enabled", True):
            decision = self.continuity.decide(
                scope_key,
                scene_signature,
                force_new_outfit=force_new_outfit,
                force_new_scene=force_new_scene,
                now=now,
            )
            # Continuity wins over the exploration probability.  Once a scene
            # already has a reusable outfit, preserving the visual series is
            # more valuable than deliberately creating a text-only sample.
            if allow_outfit and outfit is None and decision.outfit_id and not probabilistic_outfit_fallback:
                candidate = self.gallery.get(decision.outfit_id)
                if candidate and candidate.is_selectable and self._matches_intent(candidate, outfit_intent):
                    outfit = candidate
                    reasons["outfit"] = decision.outfit_reason
            if allow_scene and scene_eligible and scene is None and decision.scene_id:
                candidate = self.gallery.get(decision.scene_id)
                if candidate and candidate.is_selectable and self._matches_intent(candidate, scene_intent):
                    scene = candidate
                    reasons["scene"] = decision.scene_reason

        excluded_outfits: set[str] = set()
        excluded_scenes: set[str] = set()
        if previous is not None:
            if force_new_outfit and previous.outfit_id:
                excluded_outfits.add(previous.outfit_id)
            if force_new_scene and previous.scene_id:
                excluded_scenes.add(previous.scene_id)

        outfit_candidates = (
            []
            if outfit is not None or not allow_outfit or probabilistic_outfit_fallback
            else [
                item
                for item in self.gallery.candidates("outfit", include_disabled=False)
                if item.id not in excluded_outfits and self._matches_intent(item, outfit_intent)
            ]
        )
        scene_candidates = (
            []
            if scene is not None or not allow_scene or not scene_eligible
            else [
                item
                for item in self.gallery.candidates("scene", include_disabled=False)
                if item.id not in excluded_scenes and self._matches_intent(item, scene_intent)
            ]
        )
        # Keep the original model-first strategy and deterministic ordering:
        # matching tags, then recently used references, then use count.
        outfit_candidates.sort(
            key=lambda item: (
                -self._score(item, outfit_intent),
                -_last_used_timestamp(item),
                -item.use_count,
                item.id,
            )
        )
        scene_candidates.sort(key=lambda item: (-self._score(item, scene_intent), item.use_count, item.id))

        candidate_limit = int(self._config_value("selection_candidate_limit_per_category", 12))
        candidates = outfit_candidates[:candidate_limit] + scene_candidates[:candidate_limit]
        selected_outfit_is_null = False
        if candidates and (outfit is None or scene is None):
            payload = json.dumps([item.as_selection_metadata() for item in candidates], ensure_ascii=False)
            try:
                selected = await self.llm.generate_json(
                    self.prompts.render(
                        "select_references",
                        description=description,
                        candidate_json=payload,
                    ),
                    task_name=self._config_value("selection_task_name", "utils"),
                    temperature=self._config_value("temperature", 0.1),
                    max_tokens=self._config_value("max_tokens", 2048),
                )
                if not isinstance(selected, dict):
                    raise ValueError("reference selection response must be an object")
                selected_outfit_value = selected.get("outfit_id")
                selected_outfit_is_null = "outfit_id" in selected and selected_outfit_value is None
                selected_outfit = str(selected_outfit_value or "").strip()
                selected_scene = str(selected.get("scene_id") or "").strip()
                if outfit is None and selected_outfit:
                    candidate = self.gallery.get(selected_outfit)
                    if (
                        candidate
                        and candidate.id in {item.id for item in outfit_candidates}
                        and candidate.is_selectable
                        and candidate.category == ReferenceCategory.OUTFIT
                    ):
                        outfit = candidate
                        reasons["outfit"] = "llm"
                if scene is None and selected_scene:
                    candidate = self.gallery.get(selected_scene)
                    if (
                        candidate
                        and candidate.id in {item.id for item in scene_candidates}
                        and candidate.is_selectable
                        and candidate.category == ReferenceCategory.SCENE
                    ):
                        scene = candidate
                        reasons["scene"] = "llm"
            except (LLMAdapterError, ValueError, TypeError):
                reasons["selection"] = "llm_failed_deterministic_fallback"

        if outfit is None and outfit_candidates and not selected_outfit_is_null:
            outfit = outfit_candidates[0]
            reasons["outfit"] = "score"
        if scene is None and scene_candidates:
            scene = scene_candidates[0]
            reasons["scene"] = "score"
        if outfit is None and allow_outfit:
            reasons["outfit"] = "probabilistic_text_fallback" if probabilistic_outfit_fallback else "text_fallback"
        elif not allow_outfit:
            reasons["outfit"] = "disabled"
        if scene is None and allow_scene and scene_eligible:
            reasons["scene"] = "text_fallback"
        elif not allow_scene:
            reasons["scene"] = "disabled"
        elif not scene_eligible:
            reasons["scene"] = "scene_not_private"

        return SelectionResult(
            outfit=outfit,
            scene=scene,
            scene_signature=scene_signature,
            scene_eligible=bool(scene_eligible),
            reasons=reasons,
        )


def _normalize_signature(value: str) -> str:
    return " ".join(str(value or "unspecified").casefold().split())


def _intent_tokens(value: str) -> set[str]:
    return {word.strip().casefold() for word in str(value or "").replace("，", ",").split(",") if word.strip()}


def _combine_intent(*values: str) -> str:
    return ", ".join(value.strip() for value in values if str(value or "").strip())


def _normalized_asset_text(asset: ReferenceAsset) -> str:
    values: list[str] = [asset.name]
    values.extend(str(value) for value in asset.effective_tags.values())
    values.extend(str(value) for value in asset.selection_metadata.values())
    return " ".join(values).casefold()


def _last_used_timestamp(asset: ReferenceAsset) -> float:
    """Return a sortable timestamp without assuming a naive datetime."""

    if asset.last_used_at is None:
        return 0.0
    value = asset.last_used_at
    try:
        return value.timestamp()
    except (OverflowError, OSError, ValueError):
        return 0.0


def _fallback_scene_eligible(scene_hint: str, description: str) -> bool:
    value = f"{scene_hint} {description}".casefold()
    blocked = ("咖啡店", "咖啡馆", "商场", "街道", "办公室", "cafe", "coffee shop", "mall", "street", "office")
    allowed = ("卧室", "浴室", "卫生间", "客厅", "bedroom", "bathroom", "living room")
    if any(token in value for token in blocked):
        return False
    return any(token in value for token in allowed)
