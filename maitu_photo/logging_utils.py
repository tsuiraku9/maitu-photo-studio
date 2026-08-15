"""Structured, redacted logging helpers for the photo plugin.

MaiBot owns process-wide log routing, rotation, and retention.  This module
only gives plugin events a consistent shape and makes accidental leakage of
provider credentials or large user payloads less likely.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Any

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9._:/@+=-]{1,160}$")
_DIAGNOSTIC_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "api_key",
        "authorization",
        "content",
        "description",
        "error",
        "error_message",
        "exception",
        "image",
        "image_data",
        "message",
        "password",
        "payload",
        "personality",
        "prompt",
        "raw_message",
        "reason",
        "secret",
        "scene_signature",
        "token",
    }
)


def redact_text(value: object, *, limit: int = 500) -> str:
    """Return a bounded diagnostic string without common credential shapes."""

    text = str("" if value is None else value).replace("\r", " ").replace("\n", " ")
    text = re.sub(r"(?i)(?:sk|key|token)-[A-Za-z0-9._~-]{8,}", "[REDACTED]", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}", r"\1[REDACTED]", text)
    text = re.sub(
        r"(?i)([\"']?(?:api[_-]?key|authorization|token|secret|password)[\"']?\s*[:=]\s*[\"']?)"
        r"(?!\[REDACTED\])(?:bearer\s+)?[^\s,;\"'\]\}]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)([?&](?:api[_-]?key|token|key)=)[^&\s]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(https?://)[^\s/?#]*@", r"\1[REDACTED]@", text)
    text = " ".join(text.split())
    return text[:limit]


def diagnostic_error(error: BaseException) -> str:
    """Return a stable error category without rendering exception text.

    Provider errors carry a safe machine-readable ``code`` and optional HTTP
    status.  Other exceptions are represented by their class name so host
    logs cannot accidentally include a provider response body or user input.
    """

    code = getattr(error, "code", None)
    status_code = getattr(error, "status_code", None)
    if isinstance(code, str) and _DIAGNOSTIC_CODE.fullmatch(code):
        if isinstance(status_code, int) and not isinstance(status_code, bool):
            return f"{code}/http_{status_code}"
        return code
    return type(error).__name__


def _format_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, BaseException):
        return diagnostic_error(value)
    elif isinstance(value, Mapping):
        return f"<mapping:{len(value)}>"
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return f"<sequence:{len(value)}>"

    text = redact_text(value, limit=160)
    return text if _SAFE_VALUE.fullmatch(text) else repr(text)


class PluginEventLogger:
    """Emit concise plugin lifecycle events through MaiBot's standard logger."""

    def __init__(
        self,
        logger: logging.Logger | None = None,
        *,
        enabled: bool = True,
        minimum_level: str = "INFO",
    ) -> None:
        self.logger = logger or logging.getLogger(__name__)
        self.enabled = bool(enabled)
        self.minimum_level = _LEVELS.get(str(minimum_level).upper(), logging.INFO)

    def debug(self, event: str, *args: Any, **fields: Any) -> None:
        self._emit(logging.DEBUG, self._interpolate(event, args), fields)

    def info(self, event: str, *args: Any, **fields: Any) -> None:
        self._emit(logging.INFO, self._interpolate(event, args), fields)

    def warning(self, event: str, *args: Any, **fields: Any) -> None:
        self._emit(logging.WARNING, self._interpolate(event, args), fields)

    def error(self, event: str, *args: Any, **fields: Any) -> None:
        self._emit(logging.ERROR, self._interpolate(event, args), fields)

    def exception(self, event: str, *args: Any, **fields: Any) -> None:
        active_error = sys.exception()
        if active_error is not None and "error_kind" not in fields:
            fields = {**fields, "error_kind": diagnostic_error(active_error)}
        # Avoid ``exc_info=True`` here: provider tracebacks may contain an
        # arbitrary upstream response body.  Structured error categories are
        # sufficient for host-level operations and preserve user privacy.
        self._emit(logging.ERROR, self._interpolate(event, args), fields)

    def _emit(self, level: int, event: str, fields: Mapping[str, Any]) -> None:
        if not self.enabled or level < self.minimum_level:
            return
        self.logger.log(level, self._message(event, fields))

    @staticmethod
    def _message(event: str, fields: Mapping[str, Any]) -> str:
        rendered = " ".join(
            f"{key}={'<redacted>' if key.casefold() in _SENSITIVE_FIELD_NAMES else _format_value(value)}"
            for key, value in sorted(fields.items())
        )
        return f"麦麦写真 | {redact_text(event, limit=120) or '未命名事件'}" + (f" | {rendered}" if rendered else "")

    @staticmethod
    def _interpolate(event: str, args: tuple[Any, ...]) -> str:
        if not args:
            return event
        try:
            safe_args = tuple(
                (
                    value
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                    else redact_text(value, limit=160)
                )
                for value in args
            )
            return event % safe_args
        except (TypeError, ValueError):
            return " ".join((event, *(redact_text(value, limit=160) for value in args)))


__all__ = ["PluginEventLogger", "diagnostic_error", "redact_text"]
