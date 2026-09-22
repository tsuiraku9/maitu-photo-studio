from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from maibot_sdk import PluginContext

from maitu_photo.llm_adapter import MaiBotLLMAdapter


def test_generate_json_passes_configured_rpc_timeout_to_sdk_proxy() -> None:
    calls: list[dict[str, Any]] = []

    async def rpc_call(
        method: str,
        plugin_id: str,
        payload: dict[str, Any],
        *,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        calls.append({"method": method, "plugin_id": plugin_id, "payload": payload, "timeout_ms": timeout_ms})
        return {"success": True, "response": '{"ok": true}'}

    adapter = MaiBotLLMAdapter(PluginContext("maitu.test", rpc_call=rpc_call), rpc_timeout_seconds=120.5)

    assert asyncio.run(adapter.generate_json("prompt")) == {"ok": True}
    assert calls[0]["timeout_ms"] == 120_500
    assert calls[0]["method"] == "cap.call"
    assert "timeout_ms" not in calls[0]["payload"]["args"]


def test_sdk_proxy_generate_uses_compatible_default_rpc_timeout() -> None:
    calls: list[dict[str, Any]] = []

    async def sdk_generate(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"success": True, "response": "ok"}

    adapter = MaiBotLLMAdapter(SimpleNamespace(llm=SimpleNamespace(generate=sdk_generate)))

    asyncio.run(adapter.generate_text("prompt"))
    assert calls[0]["timeout_ms"] == 30_000
    assert "task_name" not in calls[0]
    assert "model" not in calls[0]


def test_rpc_timeout_rounds_up_to_at_least_one_millisecond() -> None:
    calls: list[dict[str, Any]] = []

    async def generate(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"success": True, "response": "ok"}

    adapter = MaiBotLLMAdapter(generate=generate, rpc_timeout_seconds=0.0001)

    asyncio.run(adapter.generate_text("prompt"))
    assert calls[0]["timeout_ms"] == 1


def test_generate_text_sends_task_name_not_model() -> None:
    calls: list[dict[str, Any]] = []

    async def generate(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"success": True, "response": "ok"}

    adapter = MaiBotLLMAdapter(generate=generate)
    asyncio.run(adapter.generate_text("prompt", task_name="vlm"))

    assert calls[0]["task_name"] == "vlm"
    assert "model" not in calls[0]
    assert "model_name" not in calls[0]


def test_blank_task_name_is_omitted_so_sdk_keeps_utils_default() -> None:
    calls: list[dict[str, Any]] = []

    async def generate(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"success": True, "response": "ok"}

    adapter = MaiBotLLMAdapter(generate=generate)
    asyncio.run(adapter.generate_text("prompt", task_name="  "))

    assert "task_name" not in calls[0]
    assert "model" not in calls[0]


def test_sdk_proxy_generate_json_uses_task_name_not_model_alias() -> None:
    calls: list[dict[str, Any]] = []

    async def rpc_call(
        method: str,
        plugin_id: str,
        payload: dict[str, Any],
        *,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        del method, plugin_id, timeout_ms
        calls.append(payload)
        return {"success": True, "response": '{"eligible": true}'}

    adapter = MaiBotLLMAdapter(PluginContext("maitu.test", rpc_call=rpc_call))
    result = asyncio.run(adapter.generate_json("prompt", task_name="utils"))

    assert result == {"eligible": True}
    assert calls[0]["capability"] == "llm.generate"
    args = calls[0]["args"]
    assert args["task_name"] == "utils"
    assert args.get("model") in ("", None)
    assert not args.get("model_name")
