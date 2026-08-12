"""MaiBot SDK imports with a small declaration-only test fallback."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Callable

try:  # pragma: no cover - exercised by MaiBot and SDK integration tests
    from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Command, MaiBotPlugin, Tool
    from maibot_sdk.types import ToolParameterInfo as _ToolParameterInfo
    from maibot_sdk.types import ToolParamType

    SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - local tests use this lightweight surface
    SDK_AVAILABLE = False
    CONFIG_RELOAD_SCOPE_SELF = "self"

    class ToolParamType(str, Enum):
        STRING = "string"
        INTEGER = "integer"
        NUMBER = "number"
        FLOAT = "number"
        BOOLEAN = "boolean"
        ARRAY = "array"
        OBJECT = "object"

    @dataclass(slots=True)
    class _ToolParameterInfo:
        name: str
        param_type: ToolParamType = ToolParamType.STRING
        description: str = ""
        required: bool = True
        enum_values: list[Any] | None = None
        items_schema: dict[str, Any] | None = None
        properties: dict[str, dict[str, Any]] | None = None
        required_properties: list[str] | None = None
        additional_properties: bool | dict[str, Any] | None = None
        default: Any = None

    def _component(kind: str, name: str, **metadata: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(function: Callable[..., Any]) -> Callable[..., Any]:
            function.__maitu_component__ = {"name": name, "type": kind, "metadata": metadata}
            return function

        return decorator

    def Tool(
        name: str,
        description: str = "",
        brief_description: str = "",
        detailed_description: str = "",
        parameters: list[Any] | dict[str, Any] | None = None,
        **metadata: Any,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        typed = [asdict(item) for item in parameters] if isinstance(parameters, list) else []
        return _component(
            "tool",
            name,
            description=brief_description or description,
            brief_description=brief_description or description,
            detailed_description=detailed_description,
            parameters=typed,
            parameters_raw=parameters if isinstance(parameters, dict) else {},
            **metadata,
        )

    def Command(
        name: str,
        description: str = "",
        pattern: str = "",
        aliases: list[str] | None = None,
        **metadata: Any,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return _component(
            "command",
            name,
            description=description,
            command_pattern=pattern,
            aliases=list(aliases or []),
            **metadata,
        )

    class MaiBotPlugin:
        config_model: type[Any] | None = None

        def __init__(self) -> None:
            self._ctx: Any = None
            self._config: Any = self.config_model() if self.config_model is not None else None

        @property
        def ctx(self) -> Any:
            if self._ctx is None:
                raise RuntimeError("PluginContext 尚未注入")
            return self._ctx

        @property
        def config(self) -> Any:
            if self._config is None:
                raise RuntimeError("插件配置尚未注入")
            return self._config

        def get_plugin_config_data(self) -> dict[str, Any]:
            if hasattr(self.config, "model_dump"):
                return self.config.model_dump(mode="python")
            return {}

        def get_components(self) -> list[dict[str, Any]]:
            components: list[dict[str, Any]] = []
            for name in dir(self):
                try:
                    value = getattr(self, name)
                except Exception:
                    continue
                declaration = getattr(value, "__maitu_component__", None)
                if declaration:
                    item = {
                        "name": declaration["name"],
                        "type": declaration["type"],
                        "metadata": dict(declaration["metadata"]),
                    }
                    item["metadata"]["handler_name"] = name
                    components.append(item)
            return components


def ToolParameterInfo(
    name: str,
    param_type: ToolParamType = ToolParamType.STRING,
    description: str = "",
    *,
    required: bool = True,
    enum_values: list[Any] | None = None,
    items_schema: dict[str, Any] | None = None,
    properties: dict[str, dict[str, Any]] | None = None,
    required_properties: list[str] | None = None,
    additional_properties: bool | dict[str, Any] | None = None,
    default: Any = None,
) -> Any:
    """Construct the SDK model with a stable positional convenience surface."""

    return _ToolParameterInfo(
        name=name,
        param_type=param_type,
        description=description,
        required=required,
        enum_values=enum_values,
        items_schema=items_schema,
        properties=properties,
        required_properties=required_properties or [],
        additional_properties=additional_properties,
        default=default,
    )


__all__ = [
    "CONFIG_RELOAD_SCOPE_SELF",
    "Command",
    "MaiBotPlugin",
    "SDK_AVAILABLE",
    "Tool",
    "ToolParameterInfo",
    "ToolParamType",
]
