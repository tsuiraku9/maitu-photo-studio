"""Small adapter around MaiBot's LLM capability for JSON metadata tasks."""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Awaitable, Callable


class LLMAdapterError(RuntimeError):
    """Raised when an auxiliary LLM call cannot produce valid structured data."""


def _strip_json_fence(text: str) -> str:
    value = (text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def parse_json_response(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict) or not result.get("success", True):
        reason = result.get("reason") if isinstance(result, dict) else "无返回结果"
        raise LLMAdapterError(str(reason or "辅助模型调用失败"))
    text = str(result.get("response") or result.get("content") or "").strip()
    if not text:
        raise LLMAdapterError("辅助模型没有返回文本")
    try:
        value = json.loads(_strip_json_fence(text))
    except json.JSONDecodeError as exc:
        raise LLMAdapterError(f"辅助模型返回不是合法 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise LLMAdapterError("辅助模型 JSON 顶层必须是对象")
    return value


class MaiBotLLMAdapter:
    """Adapter that works with the SDK proxy and a test-friendly callable."""

    def __init__(self, ctx: Any = None, generate: Callable[..., Awaitable[Any]] | None = None) -> None:
        self.ctx = ctx
        self._generate = generate

    async def generate_text(
        self,
        prompt: str | list[dict[str, Any]],
        *,
        task_name: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        if self._generate is not None:
            return await self._generate(
                prompt=prompt,
                model=task_name,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        if self.ctx is None:
            raise LLMAdapterError("MaiBot LLM 上下文尚未注入")
        return await self.ctx.llm.generate(
            prompt=prompt,
            model=task_name,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    async def generate_json(
        self,
        prompt: str,
        *,
        task_name: str = "",
        image_bytes: bytes | None = None,
        mime_type: str = "image/jpeg",
        temperature: float | None = 0.1,
        max_tokens: int | None = 2048,
    ) -> dict[str, Any]:
        if image_bytes:
            data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
            messages: list[dict[str, Any]] = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ]
            result = await self.generate_text(
                messages,
                task_name=task_name,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        else:
            result = await self.generate_text(
                prompt,
                task_name=task_name,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        return parse_json_response(result)
