from __future__ import annotations

import asyncio
import base64
import io
import json

import httpx
import pytest
from PIL import Image

from maitu_photo.provider import (
    OpenAICompatibleProvider,
    ProviderConfigError,
    ProviderHTTPError,
    normalize_base_url,
)


def _png(colour: str = "red") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (4, 3), colour).save(output, "PNG")
    return output.getvalue()


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://example.test", "https://example.test/v1"),
        ("https://example.test/", "https://example.test/v1"),
        ("https://example.test/v1", "https://example.test/v1"),
        ("https://example.test/api/v1/", "https://example.test/api/v1"),
        ("https://example.test/openai", "https://example.test/openai/v1"),
    ],
)
def test_normalize_base_url(value: str, expected: str) -> None:
    assert normalize_base_url(value) == expected


def test_base_url_rejects_credentials_in_query() -> None:
    with pytest.raises(ProviderConfigError):
        normalize_base_url("https://example.test?api_key=secret")


def test_images_generation_uses_expected_endpoint_and_parses_b64_json() -> None:
    image = _png()
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(image).decode()}]},
            request=request,
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                "https://api.example.test",
                "test-key-value",
                generation_model="image-model",
                client=client,
            )
            return await provider.generate("a portrait", size="1024x1024")

    result = _run(scenario())

    assert result.data == image
    assert result.media_type == "image/png"
    assert seen["url"] == "https://api.example.test/v1/images/generations"
    assert seen["auth"] == "Bearer test-key-value"
    assert seen["json"] == {
        "model": "image-model",
        "prompt": "a portrait",
        "n": 1,
        "size": "1024x1024",
        "response_format": "b64_json",
    }


def test_images_edit_preserves_reference_order_in_multipart_body() -> None:
    first = b"FIRST_REFERENCE_BYTES"
    second = b"SECOND_REFERENCE_BYTES"
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content
        seen["url"] = str(request.url)
        seen["content_type"] = request.headers["content-type"]
        seen["body"] = body
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(_png("green")).decode()}]},
            request=request,
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                "https://api.example.test/v1/",
                "test-key-value",
                generation_model="edit-model",
                client=client,
            )
            return await provider.edit_image("keep identity", [first, second])

    _run(scenario())

    body = seen["body"]
    assert isinstance(body, bytes)
    assert seen["url"] == "https://api.example.test/v1/images/edits"
    assert str(seen["content_type"]).startswith("multipart/form-data; boundary=")
    assert body.find(first) < body.find(second)
    assert body.count(b'name="image"') == 2
    assert b"reference-0.jpeg" in body
    assert b"reference-1.jpeg" in body


def test_chat_completions_sends_ordered_multimodal_content_and_parses_markdown() -> None:
    references = [_png("red"), _png("blue")]
    output = _png("green")
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen["url"] = str(request.url)
        seen["payload"] = payload
        data_url = f"data:image/png;base64,{base64.b64encode(output).decode()}"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": f"Done ![image]({data_url})"}}]},
            request=request,
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                "https://api.example.test",
                "test-key-value",
                mode="chat_completions",
                generation_model="chat-image-model",
                client=client,
            )
            return await provider.generate("photo", images=references)

    result = _run(scenario())

    assert result.data == output
    assert seen["url"] == "https://api.example.test/v1/chat/completions"
    payload = seen["payload"]
    assert isinstance(payload, dict)
    content = payload["messages"][0]["content"]
    assert [item["type"] for item in content] == ["text", "image_url", "image_url"]
    assert base64.b64decode(content[1]["image_url"]["url"].split(",", 1)[1]) == references[0]
    assert base64.b64decode(content[2]["image_url"]["url"].split(",", 1)[1]) == references[1]


def test_remote_url_result_is_downloaded_without_forwarding_provider_secret() -> None:
    image = _png()
    seen_authorization: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"data": [{"url": "https://cdn.example.test/generated.png"}]},
                request=request,
            )
        seen_authorization.append(request.headers.get("authorization"))
        return httpx.Response(
            200,
            content=image,
            headers={"content-type": "image/png"},
            request=request,
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                "https://api.example.test",
                "test-key-value",
                generation_model="image-model",
                client=client,
            )
            return await provider.generate("photo")

    result = _run(scenario())

    assert result.data == image
    assert result.source_url == "https://cdn.example.test/generated.png"
    assert seen_authorization == [""]


def test_http_error_redacts_secrets_and_marks_retryability() -> None:
    secret = "sk-this-is-a-sensitive-provider-key"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            text=f"upstream rejected Bearer {secret} api_key={secret}",
            request=request,
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                "https://api.example.test",
                secret,
                generation_model="image-model",
                client=client,
            )
            return await provider.generate("photo")

    with pytest.raises(ProviderHTTPError) as caught:
        _run(scenario())

    assert secret not in str(caught.value)
    assert "[REDACTED]" in str(caught.value)
    assert caught.value.status_code == 429
    assert caught.value.retryable is True


def test_provider_never_cross_mode_retries_after_failure() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(500, text="failed", request=request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                "https://api.example.test",
                "test-key-value",
                mode="chat_completions",
                generation_model="image-model",
                client=client,
            )
            return await provider.generate("photo")

    with pytest.raises(ProviderHTTPError):
        _run(scenario())

    assert paths == ["/v1/chat/completions"]
