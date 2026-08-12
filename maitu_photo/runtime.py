"""MaiBot invocation context, permissions, and uploaded-image extraction."""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .continuity import make_scope_key


class InvocationError(ValueError):
    """Raised when the host invocation does not contain required context."""


class ImageInputError(InvocationError):
    """Raised when an administrator upload is missing or ambiguous."""


@dataclass(frozen=True, slots=True)
class InvocationContext:
    stream_id: str
    scope_key: str
    user_id: str
    group_id: str | None
    message_id: str
    message: Mapping[str, Any]

    def is_admin(self, configured_ids: Sequence[str]) -> bool:
        allowed = {str(item).strip() for item in configured_ids if str(item).strip()}
        return bool(self.user_id and self.user_id in allowed)


def invocation_context(kwargs: Mapping[str, Any]) -> InvocationContext:
    message_value = kwargs.get("message")
    message = message_value if isinstance(message_value, Mapping) else {}
    message_info = _mapping(message.get("message_info"))
    user_info = _mapping(message_info.get("user_info"))
    group_info = _mapping(message_info.get("group_info"))

    stream_id = _first_text(
        kwargs.get("stream_id"),
        message.get("stream_id"),
        message.get("session_id"),
    )
    if not stream_id:
        raise InvocationError("当前调用缺少 stream_id")
    user_id = _first_text(
        kwargs.get("user_id"),
        user_info.get("user_id"),
        message.get("user_id"),
    )
    group_id = _first_text(
        kwargs.get("group_id"),
        group_info.get("group_id"),
        message.get("group_id"),
    )
    message_id = _first_text(
        kwargs.get("message_id"),
        message.get("message_id"),
        message.get("id"),
    )
    return InvocationContext(
        stream_id=stream_id,
        scope_key=make_scope_key(group_id=group_id or None, stream_id=stream_id),
        user_id=user_id,
        group_id=group_id or None,
        message_id=message_id,
        message=message,
    )


async def resolve_single_image(
    ctx: Any,
    invocation: InvocationContext,
    *,
    source_message_id: str = "",
) -> bytes:
    """Load exactly one uploaded image from an explicit/current/replied message."""

    explicit_id = str(source_message_id or "").strip()
    current_images = extract_message_images(invocation.message)
    if explicit_id:
        message = await ctx.message.get_by_id(
            explicit_id,
            stream_id=invocation.stream_id,
            include_binary_data=True,
        )
        return _require_single_image(message, explicit_id)
    if current_images:
        if len(current_images) != 1:
            raise ImageInputError("每次只能上传一张图片")
        return current_images[0]

    reply_id = _reply_message_id(invocation.message)
    target_id = reply_id or invocation.message_id
    if not target_id:
        raise ImageInputError("请在当前消息上传一张图片，或引用一条含单张图片的消息")
    message = await ctx.message.get_by_id(
        target_id,
        stream_id=invocation.stream_id,
        include_binary_data=True,
    )
    return _require_single_image(message, target_id)


def extract_message_images(message: Any) -> list[bytes]:
    if not isinstance(message, Mapping):
        return []
    segments: list[Any] = []
    for key in ("raw_message", "segments", "message_segments", "content"):
        value = message.get(key)
        if isinstance(value, list):
            segments.extend(value)
    images: list[bytes] = []
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        segment_type = str(segment.get("type") or segment.get("content_type") or "").casefold()
        if segment_type not in {"image", "picture", "photo"}:
            continue
        encoded = _first_text(
            segment.get("binary_data_base64"),
            segment.get("base64"),
            segment.get("data_base64"),
        )
        if not encoded:
            data = segment.get("data")
            encoded = data if isinstance(data, str) and data.startswith("data:image/") else ""
        if encoded:
            images.append(_decode_image_base64(encoded))
    return images


def _require_single_image(message: Any, message_id: str) -> bytes:
    if message is None:
        raise ImageInputError(f"找不到消息: {message_id}")
    images = extract_message_images(message)
    if not images:
        raise ImageInputError("指定消息中没有可读取的图片")
    if len(images) != 1:
        raise ImageInputError("每次只能上传一张图片")
    return images[0]


def _decode_image_base64(value: str) -> bytes:
    encoded = value.strip()
    if encoded.startswith("data:"):
        match = re.match(r"^data:image/[^;,]+;base64,(.*)$", encoded, flags=re.I | re.S)
        if match is None:
            raise ImageInputError("图片 Data URL 格式无效")
        encoded = match.group(1)
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ImageInputError("图片 Base64 数据无效") from exc
    if not data:
        raise ImageInputError("图片数据为空")
    return data


def _reply_message_id(message: Mapping[str, Any]) -> str:
    direct = _first_text(message.get("reply_to"), message.get("quoted_message_id"))
    if direct:
        return direct
    for key in ("raw_message", "segments", "message_segments"):
        segments = message.get(key)
        if not isinstance(segments, list):
            continue
        for segment in segments:
            if not isinstance(segment, Mapping) or str(segment.get("type") or "").casefold() != "reply":
                continue
            data = segment.get("data")
            if isinstance(data, Mapping):
                # MaiBot 1.1.x serializes ReplyComponent with
                # ``target_message_id``; older adapters used message_id/id.
                reply_id = _first_text(
                    data.get("target_message_id"),
                    data.get("message_id"),
                    data.get("id"),
                )
            else:
                reply_id = _first_text(data)
            if reply_id:
                return reply_id
    return ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


__all__ = [
    "ImageInputError",
    "InvocationContext",
    "InvocationError",
    "extract_message_images",
    "invocation_context",
    "resolve_single_image",
]
