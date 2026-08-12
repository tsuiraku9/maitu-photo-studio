from __future__ import annotations

import asyncio
import base64

import pytest

from maitu_photo.commands import CommandParseError, parse_admin_command, parse_tags
from maitu_photo.runtime import (
    ImageInputError,
    extract_message_images,
    invocation_context,
    resolve_single_image,
)


def _message(image_values: list[bytes], *, message_id: str = "m1") -> dict:
    return {
        "message_id": message_id,
        "session_id": "stream-1",
        "message_info": {
            "user_info": {"user_id": "admin"},
            "group_info": {"group_id": "group-1"},
        },
        "raw_message": [
            {
                "type": "image",
                "binary_data_base64": base64.b64encode(value).decode("ascii"),
            }
            for value in image_values
        ],
    }


class _Messages:
    def __init__(self, messages: dict[str, dict]) -> None:
        self.messages = messages
        self.calls: list[tuple[str, bool]] = []

    async def get_by_id(
        self,
        message_id: str,
        *,
        stream_id: str,
        include_binary_data: bool,
    ) -> dict | None:
        del stream_id
        self.calls.append((message_id, include_binary_data))
        return self.messages.get(message_id)


class _Context:
    def __init__(self, messages: dict[str, dict]) -> None:
        self.message = _Messages(messages)


def test_invocation_context_prefers_group_scope_and_checks_admin() -> None:
    invocation = invocation_context({"stream_id": "stream-1", "message": _message([])})
    assert invocation.scope_key == "group:group-1"
    assert invocation.user_id == "admin"
    assert invocation.is_admin(["someone", "admin"]) is True


def test_current_and_replied_message_image_resolution() -> None:
    current = _message([b"current"])
    invocation = invocation_context({"stream_id": "stream-1", "message": current})
    assert asyncio.run(resolve_single_image(_Context({}), invocation)) == b"current"

    quoted = _message([], message_id="m2")
    quoted["reply_to"] = "source"
    invocation = invocation_context({"stream_id": "stream-1", "message": quoted})
    context = _Context({"source": _message([b"quoted"], message_id="source")})
    assert asyncio.run(resolve_single_image(context, invocation)) == b"quoted"
    assert context.message.calls == [("source", True)]


def test_reply_component_target_message_id_is_supported() -> None:
    quoted = _message([], message_id="m2")
    quoted["segments"] = [{"type": "reply", "data": {"target_message_id": "source"}}]
    invocation = invocation_context({"stream_id": "stream-1", "message": quoted})
    context = _Context({"source": _message([b"quoted"], message_id="source")})

    assert asyncio.run(resolve_single_image(context, invocation)) == b"quoted"
    assert context.message.calls == [("source", True)]


def test_multiple_images_are_rejected() -> None:
    invocation = invocation_context({"stream_id": "stream-1", "message": _message([b"one", b"two"])})
    with pytest.raises(ImageInputError, match="一张"):
        asyncio.run(resolve_single_image(_Context({}), invocation))


def test_extract_message_images_accepts_data_url() -> None:
    data = base64.b64encode(b"image").decode("ascii")
    message = {"segments": [{"type": "image", "data": f"data:image/png;base64,{data}"}]}
    assert extract_message_images(message) == [b"image"]


def test_command_parser_supports_quotes_options_and_json_tags() -> None:
    parsed = parse_admin_command("/maitu ref edit abc name='summer dress' tags='{\"styles\":[\"casual\"]}'")
    assert parsed.domain == "ref"
    assert parsed.action == "edit"
    assert parsed.args == ("abc",)
    assert parsed.options["name"] == "summer dress"
    assert parse_tags(parsed.options["tags"]) == {"styles": ["casual"]}

    with pytest.raises(CommandParseError):
        parse_admin_command("/other ref list")
