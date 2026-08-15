from __future__ import annotations

import asyncio
import base64
import io
import json
import logging

import httpx
import pytest
from PIL import Image

import maitu_photo.provider as provider_module
from maitu_photo.provider import (
    OpenAICompatibleProvider,
    ProviderConfigError,
    ProviderHTTPError,
    ProviderImageDecodeError,
    ProviderNetworkError,
    ProviderResponseError,
    normalize_base_url,
)


def _png(colour: str = "red") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (4, 3), colour).save(output, "PNG")
    return output.getvalue()


def _run(coro):
    return asyncio.run(coro)


class _NetworkStream:
    def __init__(self, address: str, port: int = 443) -> None:
        self.address = address
        self.port = port

    def get_extra_info(self, name: str):
        return (self.address, self.port) if name == "server_addr" else None


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.yielded = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk


class _NoStreamClient(httpx.AsyncClient):
    async def stream(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("image provider requests must use AsyncClient.post")


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


def test_base_url_rejects_userinfo_credentials() -> None:
    with pytest.raises(ProviderConfigError):
        normalize_base_url("https://alice:supersecret@example.test")


def test_provider_errors_redact_url_userinfo() -> None:
    error = ProviderNetworkError("request failed for https://alice:supersecret@example.test/api")

    rendered = str(error)
    assert "alice" not in rendered
    assert "supersecret" not in rendered
    assert "https://[REDACTED]@example.test/api" in rendered


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


def test_gpt_image_generation_requests_base64_output() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(_png()).decode()}]},
            request=request,
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                "https://api.example.test",
                "test-key-value",
                generation_model="gpt-image-2",
                client=client,
            )
            return await provider.generate("a portrait")

    _run(scenario())

    payload = seen["json"]
    assert isinstance(payload, dict)
    assert payload == {
        "model": "gpt-image-2",
        "prompt": "a portrait",
        "n": 1,
        "response_format": "b64_json",
    }


def test_provider_response_stops_when_stream_exceeds_size_limit() -> None:
    stream = _ChunkStream([b'{"data":[', b"x" * 256])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=stream,
            headers={"content-type": "application/json"},
            request=request,
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                "https://api.example.test",
                "test-key-value",
                generation_model="image-model",
                max_response_bytes=128,
                client=client,
            )
            return await provider.generate("photo")

    with pytest.raises(ProviderResponseError, match="size limit"):
        _run(scenario())

    assert stream.yielded == 2


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
                generation_model="gpt-image-2",
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
    assert b'name="response_format"' in body
    assert b"reference-0.jpeg" in body
    assert b"reference-1.jpeg" in body


def test_images_edit_uses_eager_post_for_compatible_gateways() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(_png("green")).decode()}]},
            request=request,
        )

    async def scenario():
        async with _NoStreamClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                "https://api.example.test",
                "test-key-value",
                generation_model="gpt-image-2",
                client=client,
            )
            return await provider.edit_image("keep identity", [b"REFERENCE"])

    result = _run(scenario())
    assert result.media_type == "image/png"


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


def test_remote_url_result_is_downloaded_without_forwarding_provider_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _png()
    seen_authorization: list[str | None] = []

    async def resolve_public_host(hostname: str, port: int) -> set[str]:
        assert (hostname, port) == ("cdn.example.test", 443)
        return {"93.184.216.34"}

    monkeypatch.setattr(provider_module, "_resolve_host_addresses", resolve_public_host)

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
            extensions={"network_stream": _NetworkStream("93.184.216.34")},
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


def test_same_origin_url_result_keeps_provider_authorization() -> None:
    image = _png()
    seen_authorization: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"data": [{"url": "https://api.example.test/v1/generated.png"}]},
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
    assert seen_authorization == ["Bearer test-key-value"]


@pytest.mark.parametrize(
    "image_url",
    [
        "http://127.0.0.1:8080/internal.png",
        "http://10.0.0.8/internal.png",
        "http://169.254.169.254/latest/meta-data",
        "http://224.0.0.1/multicast.png",
        "http://[ff02::1]/multicast.png",
        "http://metadata.google.internal/computeMetadata/v1/",
        "https://alice:supersecret@8.8.8.8/internal.png",
    ],
)
def test_remote_url_result_rejects_non_public_targets(image_url: str) -> None:
    request_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(200, json={"data": [{"url": image_url}]}, request=request)
        return httpx.Response(200, content=_png(), headers={"content-type": "image/png"}, request=request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                "https://api.example.test",
                "test-key-value",
                generation_model="image-model",
                client=client,
            )
            return await provider.generate("photo")

    with pytest.raises(ProviderImageDecodeError):
        _run(scenario())

    assert request_methods == ["POST"]


def test_remote_url_result_allows_same_configured_private_origin() -> None:
    image = _png("green")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"data": [{"url": "http://127.0.0.1:8000/generated.png"}]},
                request=request,
            )
        return httpx.Response(200, content=image, headers={"content-type": "image/png"}, request=request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                "http://127.0.0.1:8000",
                "test-key-value",
                generation_model="image-model",
                client=client,
            )
            return await provider.generate("photo")

    result = _run(scenario())

    assert result.data == image


def test_remote_url_result_rejects_hostname_resolving_to_private_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_methods: list[str] = []

    async def resolve_private_host(hostname: str, port: int) -> set[str]:
        assert (hostname, port) == ("cdn.example.test", 443)
        return {"10.0.0.8"}

    monkeypatch.setattr(provider_module, "_resolve_host_addresses", resolve_private_host)

    def handler(request: httpx.Request) -> httpx.Response:
        request_methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"data": [{"url": "https://cdn.example.test/generated.png"}]},
                request=request,
            )
        return httpx.Response(200, content=_png(), headers={"content-type": "image/png"}, request=request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                "https://api.example.test",
                "test-key-value",
                generation_model="image-model",
                client=client,
            )
            return await provider.generate("photo")

    with pytest.raises(ProviderImageDecodeError):
        _run(scenario())

    assert request_methods == ["POST"]


def test_remote_url_result_rejects_actual_peer_outside_validated_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve_public_host(hostname: str, port: int) -> set[str]:
        assert (hostname, port) == ("cdn.example.test", 443)
        return {"93.184.216.34"}

    monkeypatch.setattr(provider_module, "_resolve_host_addresses", resolve_public_host)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"data": [{"url": "https://cdn.example.test/generated.png"}]},
                request=request,
            )
        return httpx.Response(
            200,
            content=_png(),
            headers={"content-type": "image/png"},
            extensions={"network_stream": _NetworkStream("10.0.0.8")},
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

    with pytest.raises(ProviderImageDecodeError, match="unexpected address"):
        _run(scenario())


def test_remote_url_download_stops_when_stream_exceeds_size_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _ChunkStream([b"\x89PNG\r\n\x1a\n", b"x" * 256])

    async def resolve_public_host(hostname: str, port: int) -> set[str]:
        assert (hostname, port) == ("cdn.example.test", 443)
        return {"93.184.216.34"}

    monkeypatch.setattr(provider_module, "_resolve_host_addresses", resolve_public_host)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"data": [{"url": "https://cdn.example.test/generated.png"}]},
                request=request,
            )
        return httpx.Response(
            200,
            stream=stream,
            headers={"content-type": "image/png"},
            extensions={"network_stream": _NetworkStream("93.184.216.34")},
            request=request,
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                "https://api.example.test",
                "test-key-value",
                generation_model="image-model",
                max_response_bytes=128,
                client=client,
            )
            return await provider.generate("photo")

    with pytest.raises(ProviderResponseError, match="size limit"):
        _run(scenario())

    assert stream.yielded == 2


def test_remote_url_rejects_non_image_bytes_despite_image_content_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"data": [{"url": "https://8.8.8.8/generated.png"}]},
                request=request,
            )
        return httpx.Response(
            200,
            content=b"not an image",
            headers={"content-type": "image/png"},
            extensions={"network_stream": _NetworkStream("8.8.8.8")},
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

    with pytest.raises(ProviderImageDecodeError, match="non-image"):
        _run(scenario())


def test_remote_url_result_revalidates_redirect_targets() -> None:
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(str(request.url.host))
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"data": [{"url": "https://8.8.8.8/generated.png"}]},
                request=request,
            )
        if request.url.host == "8.8.8.8":
            return httpx.Response(
                302,
                headers={"location": "http://169.254.169.254/latest/meta-data"},
                request=request,
            )
        return httpx.Response(200, content=_png(), headers={"content-type": "image/png"}, request=request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                "https://api.example.test",
                "test-key-value",
                generation_model="image-model",
                client=client,
            )
            return await provider.generate("photo")

    with pytest.raises(ProviderImageDecodeError):
        _run(scenario())

    assert requested_hosts == ["api.example.test", "8.8.8.8"]


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


def test_provider_errors_redact_json_style_credentials() -> None:
    secret = "json-provider-secret"
    error = ProviderHTTPError(
        f'{{"api_key":"{secret}","token":"{secret}"}}',
        status_code=503,
        retryable=True,
    )

    assert secret not in str(error)
    assert '"api_key":"[REDACTED]"' in str(error)


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


def test_provider_retries_retryable_generation_failures(caplog: pytest.LogCaptureFixture) -> None:
    image = _png("green")
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, text="temporarily unavailable", request=request)
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
                max_retries=1,
                retry_backoff_seconds=0,
                client=client,
            )
            return await provider.generate("photo")

    with caplog.at_level(logging.WARNING):
        result = _run(scenario())

    assert result.data == image
    assert attempts == 2
    assert "生图请求失败，准备重试" in caplog.text
    assert "HTTP状态=503" in caplog.text


def test_provider_retry_reuses_binary_stream_references() -> None:
    result_image = _png("green")
    reference_image = _png("blue")
    request_bodies: list[bytes] = []
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        request_bodies.append(request.content)
        if attempts == 1:
            return httpx.Response(503, text="temporarily unavailable", request=request)
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(result_image).decode()}]},
            request=request,
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                "https://api.example.test",
                "test-key-value",
                generation_model="image-model",
                max_retries=1,
                retry_backoff_seconds=0,
                client=client,
            )
            return await provider.generate("photo", images=[io.BytesIO(reference_image)])

    assert _run(scenario()).data == result_image
    assert attempts == 2
    assert all(reference_image in body for body in request_bodies)


def test_provider_does_not_retry_nonretryable_generation_failures() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(400, text="invalid request", request=request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                "https://api.example.test",
                "test-key-value",
                generation_model="image-model",
                max_retries=5,
                retry_backoff_seconds=0,
                client=client,
            )
            return await provider.generate("photo")

    with pytest.raises(ProviderHTTPError) as caught:
        _run(scenario())

    assert caught.value.status_code == 400
    assert attempts == 1


@pytest.mark.parametrize("max_retries", [-1, 6, True])
def test_provider_rejects_invalid_retry_count(max_retries: int) -> None:
    with pytest.raises(ProviderConfigError, match="max_retries"):
        OpenAICompatibleProvider(
            "https://api.example.test",
            "test-key-value",
            generation_model="image-model",
            max_retries=max_retries,
        )
