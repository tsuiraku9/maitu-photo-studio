"""OpenAI-compatible image generation provider.

Only one transport mode is attempted for a request.  ``images_api`` uses the
standard ``/images/generations`` and ``/images/edits`` endpoints;
``chat_completions`` sends a multimodal prompt to ``/chat/completions``.  A
caller can choose either mode per request, but this module deliberately never
falls back to another mode after an error (which could otherwise double-charge
an image request).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Sequence
from urllib.parse import unquote_to_bytes, urlsplit

import httpx

DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_RESPONSE_BYTES = 25 * 1024 * 1024
SUPPORTED_MODES = frozenset({"images_api", "chat_completions"})


class ProviderError(RuntimeError):
    """Base class for safe, user-presentable provider failures."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_error",
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(_redact(message))

    def __str__(self) -> str:
        return _redact(super().__str__())


class ProviderConfigError(ProviderError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="provider_config")


class ProviderNetworkError(ProviderError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message, code="provider_network", retryable=retryable)


class ProviderHTTPError(ProviderError):
    def __init__(self, message: str, *, status_code: int, retryable: bool = False) -> None:
        super().__init__(
            message,
            code="provider_http",
            status_code=status_code,
            retryable=retryable,
        )


class ProviderResponseError(ProviderError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="provider_response")


class ProviderImageDecodeError(ProviderResponseError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = "provider_image_decode"


def _redact(value: object) -> str:
    """Remove common API-key/token shapes from error text and diagnostics."""

    text = str(value)
    # OpenAI-style keys, generic sk-* keys, and bearer values.
    text = re.sub(r"(?i)(?:sk|key|token)-[A-Za-z0-9._~-]{8,}", "[REDACTED]", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}", r"\1[REDACTED]", text)
    text = re.sub(
        r"(?i)(api[_-]?key\s*[=:]\s*)[^\s,;\"']+",
        r"\1[REDACTED]",
        text,
    )
    # Do not expose query-string credentials in a provider URL.
    text = re.sub(r"(?i)([?&](?:api[_-]?key|token|key)=)[^&\s]+", r"\1[REDACTED]", text)
    return text[:4000]


def normalize_base_url(base_url: str) -> str:
    """Return a canonical API root ending in exactly one ``/v1`` segment."""

    if not isinstance(base_url, str) or not base_url.strip():
        raise ProviderConfigError("base_url is required")
    raw = base_url.strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ProviderConfigError("base_url must be an absolute http(s) URL")
    if parsed.query or parsed.fragment:
        raise ProviderConfigError("base_url must not contain query or fragment credentials")
    path = parsed.path.rstrip("/")
    if not path or path == "/":
        path = "/v1"
    elif not path.lower().endswith("/v1"):
        path = f"{path}/v1"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


@dataclass(frozen=True)
class ProviderConfig:
    """Connection defaults for :class:`OpenAICompatibleProvider`."""

    base_url: str
    api_key: str = field(repr=False)
    mode: str = "images_api"
    generation_model: str = ""
    extraction_model: str = ""
    timeout: float = DEFAULT_TIMEOUT
    connect_timeout: float = 15.0
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", normalize_base_url(self.base_url))
        if self.mode not in SUPPORTED_MODES:
            raise ProviderConfigError(f"unsupported provider mode: {self.mode}")
        if self.timeout <= 0:
            raise ProviderConfigError("timeout must be positive")
        if self.connect_timeout <= 0:
            raise ProviderConfigError("connect_timeout must be positive")
        if self.max_response_bytes <= 0:
            raise ProviderConfigError("max_response_bytes must be positive")


@dataclass(frozen=True)
class GeneratedImage:
    """Decoded image returned by a provider request."""

    data: bytes
    media_type: str = "image/jpeg"
    source_url: str | None = None
    index: int = 0
    raw: Mapping[str, Any] | None = None

    @property
    def bytes(self) -> bytes:
        return self.data

    @property
    def content(self) -> bytes:
        return self.data

    @property
    def content_type(self) -> str:
        return self.media_type

    @property
    def size(self) -> int:
        return len(self.data)


# Naming aliases used by integrations and older prototypes.
ImageResult = GeneratedImage
ImageGenerationResult = GeneratedImage


ImageInput = bytes | bytearray | memoryview | BinaryIO | str | Path | GeneratedImage


_DATA_URL_RE = re.compile(
    r"^data:(?P<media>[^;,\s]+)?(?P<params>(?:;[^;,\s]*)*?),(?P<body>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)(?:\s+['\"][^)]*['\"])?\)")
_DIRECT_DATA_RE = re.compile(r"data:image/[^\s)\"']+", re.IGNORECASE)


def _media_type_for_bytes(data: bytes, fallback: str = "image/jpeg") -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"BM"):
        return "image/bmp"
    return fallback


def _decode_data_url(value: str) -> tuple[bytes, str] | None:
    match = _DATA_URL_RE.match(value.strip())
    if not match:
        return None
    media = (match.group("media") or "application/octet-stream").lower()
    params = match.group("params") or ""
    body = match.group("body")
    try:
        if "base64" in params.lower().split(";"):
            decoded = base64.b64decode(body.encode("ascii"), validate=True)
        else:
            decoded = unquote_to_bytes(body)
    except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
        raise ProviderImageDecodeError("provider returned an invalid data URL") from exc
    if not decoded:
        raise ProviderImageDecodeError("provider returned an empty image")
    return decoded, media


def _decode_b64(value: str) -> bytes:
    try:
        # Some compatible servers omit padding.  Add only the required amount;
        # validate=True still rejects non-base64 characters.
        padded = value.encode("ascii") + b"=" * (-len(value) % 4)
        return base64.b64decode(padded, validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise ProviderImageDecodeError("provider returned invalid base64 image data") from exc


def _coerce_input_bytes(value: ImageInput) -> tuple[bytes, str, str | None]:
    """Read a local image input without performing network I/O."""

    if isinstance(value, GeneratedImage):
        return value.data, value.media_type, value.source_url
    if isinstance(value, bytes):
        return value, _media_type_for_bytes(value), None
    if isinstance(value, (bytearray, memoryview)):
        data = bytes(value)
        return data, _media_type_for_bytes(data), None
    if isinstance(value, (str, Path)):
        text = str(value)
        parsed = _decode_data_url(text)
        if parsed:
            data, media = parsed
            return data, media, None
        try:
            data = Path(value).read_bytes()
        except OSError as exc:
            raise ProviderImageDecodeError("unable to read reference image") from exc
        return data, _media_type_for_bytes(data), None
    if hasattr(value, "read"):
        try:
            data = value.read()
        except (OSError, ValueError) as exc:
            raise ProviderImageDecodeError("unable to read reference image stream") from exc
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise ProviderImageDecodeError("reference image stream did not return bytes")
        data = bytes(data)
        return data, _media_type_for_bytes(data), None
    raise ProviderImageDecodeError("unsupported reference image input")


def _json_safe_excerpt(response: httpx.Response) -> str:
    try:
        body = response.text
    except Exception:  # pragma: no cover - defensive for custom transports
        body = ""
    return _redact(body[:2000])


class OpenAICompatibleProvider:
    """Async OpenAI-compatible image provider.

    Parameters are intentionally plain values so the class can be configured
    from MaiBot's plugin schema.  Tests may inject an ``httpx.AsyncClient``
    (typically with ``MockTransport``); ownership remains with the caller when
    a client is injected.
    """

    def __init__(
        self,
        base_url: str | ProviderConfig,
        api_key: str | None = None,
        *,
        mode: str = "images_api",
        generation_model: str = "",
        extraction_model: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        connect_timeout: float = 15.0,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        client: httpx.AsyncClient | None = None,
        http_client: httpx.AsyncClient | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if isinstance(base_url, ProviderConfig):
            config = base_url
            if api_key is not None:
                raise ProviderConfigError("api_key must not be supplied with ProviderConfig")
        else:
            if api_key is None:
                raise ProviderConfigError("api_key is required")
            config = ProviderConfig(
                base_url=base_url,
                api_key=api_key,
                mode=mode,
                generation_model=generation_model,
                extraction_model=extraction_model,
                timeout=timeout,
                connect_timeout=connect_timeout,
                max_response_bytes=max_response_bytes,
            )
        self.config = config
        self.base_url = config.base_url
        self.api_key = config.api_key
        self.mode = config.mode
        self.generation_model = config.generation_model
        self.extraction_model = config.extraction_model
        self.timeout = config.timeout
        self.connect_timeout = config.connect_timeout
        self.max_response_bytes = config.max_response_bytes
        self._owns_client = client is None and http_client is None
        self.client = (
            client
            or http_client
            or httpx.AsyncClient(timeout=httpx.Timeout(config.timeout, connect=config.connect_timeout))
        )
        self._extra_headers = dict(headers or {})

    async def __aenter__(self) -> "OpenAICompatibleProvider":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    close = aclose

    def endpoint(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _headers(self, *, json_request: bool = True, include_auth: bool = True) -> dict[str, str]:
        result = {"Accept": "application/json", **self._extra_headers}
        if include_auth and self.api_key:
            result["Authorization"] = f"Bearer {self.api_key}"
        if json_request:
            result.setdefault("Content-Type", "application/json")
        return result

    def _safe_text(self, value: object) -> str:
        text = _redact(value)
        if self.api_key:
            text = text.replace(self.api_key, "[REDACTED]")
        return text

    @staticmethod
    def _select_mode(mode: str | None, default: str) -> str:
        selected = (mode or default or "").strip().lower()
        aliases = {
            "images": "images_api",
            "image_api": "images_api",
            "image": "images_api",
            "chat": "chat_completions",
            "chat_completion": "chat_completions",
            "chat-completion": "chat_completions",
        }
        selected = aliases.get(selected, selected)
        if selected not in SUPPORTED_MODES:
            raise ProviderConfigError(f"unsupported provider mode: {selected}")
        return selected

    def _model(self, model: str | None, *, extraction: bool = False) -> str:
        selected = model or (self.extraction_model if extraction else self.generation_model)
        if not selected:
            raise ProviderConfigError("image model is not configured")
        return selected

    async def _post_json(self, endpoint: str, payload: Mapping[str, Any]) -> httpx.Response:
        try:
            response = await self.client.post(
                endpoint,
                headers=self._headers(json_request=True),
                json=dict(payload),
                timeout=self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderNetworkError("image provider request timed out") from exc
        except httpx.RequestError as exc:
            raise ProviderNetworkError(f"image provider request failed: {self._safe_text(exc)}") from exc
        return self._check_response(response)

    async def _post_multipart(
        self,
        endpoint: str,
        *,
        data: Mapping[str, Any],
        files: Sequence[tuple[str, tuple[str, bytes, str]]],
    ) -> httpx.Response:
        try:
            response = await self.client.post(
                endpoint,
                headers=self._headers(json_request=False),
                data=dict(data),
                files=list(files),
                timeout=self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderNetworkError("image provider request timed out") from exc
        except httpx.RequestError as exc:
            raise ProviderNetworkError(f"image provider request failed: {self._safe_text(exc)}") from exc
        return self._check_response(response)

    def _check_response(self, response: httpx.Response) -> httpx.Response:
        content_length = response.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self.max_response_bytes:
                    raise ProviderResponseError("provider response exceeds configured size limit")
            except ValueError:
                pass
        if response.status_code >= 400:
            retryable = response.status_code == 429 or response.status_code >= 500
            raise ProviderHTTPError(
                f"provider returned HTTP {response.status_code}: {self._safe_text(_json_safe_excerpt(response))}",
                status_code=response.status_code,
                retryable=retryable,
            )
        try:
            if len(response.content) > self.max_response_bytes:
                raise ProviderResponseError("provider response exceeds configured size limit")
        except httpx.ResponseNotRead:
            # Custom streaming transports may defer content; parsers will read
            # and apply the same check below.
            pass
        return response

    async def _download_image(self, url: str) -> GeneratedImage:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProviderImageDecodeError("provider returned an invalid image URL")
        try:
            # Do not forward the provider's Authorization header to a third
            # party image host.  An explicit empty value overrides client-level
            # defaults while preserving MockTransport compatibility in tests.
            response = await self.client.get(
                url,
                headers={"Accept": "image/*", "Authorization": ""},
                timeout=self.timeout,
                follow_redirects=True,
            )
        except httpx.TimeoutException as exc:
            raise ProviderNetworkError("image download timed out") from exc
        except httpx.RequestError as exc:
            raise ProviderNetworkError("image download failed") from exc
        if response.status_code >= 400:
            raise ProviderHTTPError(
                f"image download returned HTTP {response.status_code}",
                status_code=response.status_code,
                retryable=response.status_code >= 500,
            )
        data = response.content
        if len(data) > self.max_response_bytes:
            raise ProviderResponseError("downloaded image exceeds configured size limit")
        if not data:
            raise ProviderImageDecodeError("provider returned an empty image")
        media = response.headers.get("content-type", "").split(";", 1)[0].strip()
        return GeneratedImage(data=data, media_type=media or _media_type_for_bytes(data), source_url=url)

    async def _parse_response(self, response: httpx.Response) -> list[GeneratedImage]:
        content_type = response.headers.get("content-type", "").lower()
        try:
            body = response.content
        except httpx.ResponseNotRead:
            body = await response.aread()
        if len(body) > self.max_response_bytes:
            raise ProviderResponseError("provider response exceeds configured size limit")
        if content_type.startswith("image/"):
            if not body:
                raise ProviderImageDecodeError("provider returned an empty image")
            return [GeneratedImage(data=body, media_type=content_type.split(";", 1)[0])]

        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            # A few compatible servers return a data URL or markdown directly.
            text = body.decode("utf-8", errors="replace").strip()
            parsed = await self._extract_from_string(text)
            if parsed:
                return parsed
            raise ProviderResponseError("provider returned invalid JSON/image data") from exc
        results = await self._extract_candidates(payload)
        if not results:
            raise ProviderResponseError("provider response did not contain an image")
        return results

    async def _extract_from_string(self, value: str) -> list[GeneratedImage]:
        value = value.strip()
        data_url = _decode_data_url(value)
        if data_url:
            data, media = data_url
            return [GeneratedImage(data=data, media_type=media)]
        matches = _MARKDOWN_IMAGE_RE.findall(value)
        if not matches:
            matches = _DIRECT_DATA_RE.findall(value)
        results: list[GeneratedImage] = []
        for match in matches:
            parsed = _decode_data_url(match)
            if parsed:
                data, media = parsed
                results.append(GeneratedImage(data=data, media_type=media, index=len(results)))
            elif match.startswith(("http://", "https://")):
                result = await self._download_image(match)
                results.append(
                    GeneratedImage(
                        data=result.data,
                        media_type=result.media_type,
                        source_url=result.source_url,
                        index=len(results),
                    )
                )
        return results

    async def _extract_candidates(self, payload: Any) -> list[GeneratedImage]:
        results: list[GeneratedImage] = []

        async def visit(value: Any, *, allow_direct_url: bool = False, raw: Mapping[str, Any] | None = None) -> None:
            if isinstance(value, str):
                parsed = _decode_data_url(value)
                if parsed:
                    data, media = parsed
                    results.append(GeneratedImage(data=data, media_type=media, index=len(results), raw=raw))
                    return
                # Markdown may contain either data URLs or remote URLs.
                extracted = await self._extract_from_string(value)
                if extracted:
                    for item in extracted:
                        results.append(
                            GeneratedImage(
                                data=item.data,
                                media_type=item.media_type,
                                source_url=item.source_url,
                                index=len(results),
                                raw=raw,
                            )
                        )
                    return
                if allow_direct_url and value.startswith(("http://", "https://")):
                    item = await self._download_image(value)
                    results.append(
                        GeneratedImage(
                            data=item.data,
                            media_type=item.media_type,
                            source_url=item.source_url,
                            index=len(results),
                            raw=raw,
                        )
                    )
                return
            if isinstance(value, Mapping):
                # b64_json is the canonical Images API field.
                for key in ("b64_json", "base64", "base64_json"):
                    encoded = value.get(key)
                    if isinstance(encoded, str):
                        data = _decode_b64(encoded)
                        results.append(
                            GeneratedImage(
                                data=data,
                                media_type=_media_type_for_bytes(data),
                                index=len(results),
                                raw=value,
                            )
                        )
                        return
                # Explicit URL-bearing fields may contain a direct remote URL.
                for key in ("url", "image_url", "image", "image_data"):
                    if key in value:
                        child = value[key]
                        if isinstance(child, Mapping):
                            await visit(child, allow_direct_url=True, raw=value)
                        else:
                            await visit(child, allow_direct_url=True, raw=value)
                        # Keep traversing siblings; some responses contain more
                        # than one image object.
                # Chat content and nested data/images/choices are recursively
                # searched.  Avoid treating arbitrary metadata strings as URLs.
                for key, child in value.items():
                    if key in {"b64_json", "base64", "base64_json", "url", "image_url", "image", "image_data"}:
                        continue
                    await visit(
                        child,
                        allow_direct_url=key in {"content", "output", "result", "data"},
                        raw=value if isinstance(value, Mapping) else raw,
                    )
                return
            if isinstance(value, (list, tuple)):
                for child in value:
                    await visit(child, allow_direct_url=allow_direct_url, raw=raw)

        await visit(payload)
        # Deduplicate only exact repeated references while preserving response
        # order.  Providers sometimes mirror an image in both ``data`` and
        # ``images`` fields.
        unique: list[GeneratedImage] = []
        seen: set[tuple[str, bytes]] = set()
        for item in results:
            key = (item.media_type, item.data)
            if key in seen:
                continue
            seen.add(key)
            unique.append(
                GeneratedImage(
                    data=item.data,
                    media_type=item.media_type,
                    source_url=item.source_url,
                    index=len(unique),
                    raw=item.raw,
                )
            )
        return unique

    async def generate_many(
        self,
        prompt: str,
        *,
        model: str | None = None,
        size: str | None = None,
        images: Sequence[ImageInput] | None = None,
        references: Sequence[ImageInput] | None = None,
        image_refs: Sequence[ImageInput] | None = None,
        negative_prompt: str | None = None,
        mode: str | None = None,
        n: int = 1,
        response_format: str | None = "b64_json",
        extra: Mapping[str, Any] | None = None,
        extraction: bool = False,
    ) -> list[GeneratedImage]:
        """Generate one or more images using exactly the selected mode."""

        if not isinstance(prompt, str) or not prompt.strip():
            raise ProviderConfigError("prompt is required")
        if not isinstance(n, int) or n < 1 or n > 10:
            raise ProviderConfigError("n must be between 1 and 10")
        selected_mode = self._select_mode(mode, self.mode)
        selected_model = self._model(model, extraction=extraction)
        refs: Sequence[ImageInput] = images or references or image_refs or ()
        if images is not None and (references is not None or image_refs is not None):
            raise ProviderConfigError("provide only one of images, references, or image_refs")
        if references is not None and image_refs is not None:
            raise ProviderConfigError("provide only one of references or image_refs")

        if selected_mode == "images_api":
            if refs:
                response = await self._images_edit(
                    prompt,
                    refs,
                    model=selected_model,
                    size=size,
                    negative_prompt=negative_prompt,
                    n=n,
                    response_format=response_format,
                    extra=extra,
                )
            else:
                response = await self._images_generate(
                    prompt,
                    model=selected_model,
                    size=size,
                    negative_prompt=negative_prompt,
                    n=n,
                    response_format=response_format,
                    extra=extra,
                )
        else:
            response = await self._chat_generate(
                prompt,
                refs,
                model=selected_model,
                size=size,
                negative_prompt=negative_prompt,
                n=n,
                extra=extra,
            )
        return await self._parse_response(response)

    async def generate(self, prompt: str, **kwargs: Any) -> GeneratedImage:
        results = await self.generate_many(prompt, **kwargs)
        return results[0]

    async def generate_image(self, prompt: str, **kwargs: Any) -> GeneratedImage:
        return await self.generate(prompt, **kwargs)

    async def create_image(self, prompt: str, **kwargs: Any) -> GeneratedImage:
        return await self.generate(prompt, **kwargs)

    async def edit_image(
        self,
        prompt: str,
        images: Sequence[ImageInput],
        **kwargs: Any,
    ) -> GeneratedImage:
        kwargs["images"] = images
        return await self.generate(prompt, **kwargs)

    async def _images_generate(
        self,
        prompt: str,
        *,
        model: str,
        size: str | None,
        negative_prompt: str | None,
        n: int,
        response_format: str | None,
        extra: Mapping[str, Any] | None,
    ) -> httpx.Response:
        payload: dict[str, Any] = {"model": model, "prompt": prompt, "n": n}
        if size:
            payload["size"] = size
        if negative_prompt:
            # ``negative_prompt`` is accepted by a number of compatible
            # gateways; preserving it as a separate field lets those gateways
            # apply their own semantics instead of silently dropping it.
            payload["negative_prompt"] = negative_prompt
        if response_format:
            payload["response_format"] = response_format
        if extra:
            payload.update(dict(extra))
        return await self._post_json(self.endpoint("images/generations"), payload)

    async def _images_edit(
        self,
        prompt: str,
        images: Sequence[ImageInput],
        *,
        model: str,
        size: str | None,
        negative_prompt: str | None,
        n: int,
        response_format: str | None,
        extra: Mapping[str, Any] | None,
    ) -> httpx.Response:
        if not images:
            raise ProviderConfigError("at least one reference image is required for edits")
        files: list[tuple[str, tuple[str, bytes, str]]] = []
        for index, image in enumerate(images):
            data, media, _ = _coerce_input_bytes(image)
            extension = media.split("/", 1)[-1] if "/" in media else "jpeg"
            if extension == "jpg":
                extension = "jpeg"
            files.append(("image", (f"reference-{index}.{extension}", data, media)))
        fields: dict[str, Any] = {"model": model, "prompt": prompt, "n": str(n)}
        if size:
            fields["size"] = size
        if negative_prompt:
            fields["negative_prompt"] = negative_prompt
        if response_format:
            fields["response_format"] = response_format
        if extra:
            fields.update(dict(extra))
        return await self._post_multipart(self.endpoint("images/edits"), data=fields, files=files)

    async def _chat_generate(
        self,
        prompt: str,
        images: Sequence[ImageInput],
        *,
        model: str,
        size: str | None,
        negative_prompt: str | None,
        n: int,
        extra: Mapping[str, Any] | None,
    ) -> httpx.Response:
        text = prompt
        if negative_prompt:
            text = f"{text}\n\nNegative prompt: {negative_prompt}"
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for image in images:
            data, media, _ = _coerce_input_bytes(image)
            encoded = base64.b64encode(data).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media};base64,{encoded}"},
                }
            )
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "n": n,
        }
        if size:
            payload["size"] = size
        if extra:
            payload.update(dict(extra))
        return await self._post_json(self.endpoint("chat/completions"), payload)


# Public aliases keep the integration layer independent of the chosen class
# name and are harmless for users importing the provider directly.
OpenAIImageProvider = OpenAICompatibleProvider
ImageProvider = OpenAICompatibleProvider


__all__ = [
    "GeneratedImage",
    "ImageGenerationResult",
    "ImageInput",
    "ImageProvider",
    "ImageResult",
    "OpenAICompatibleProvider",
    "OpenAIImageProvider",
    "ProviderConfig",
    "ProviderConfigError",
    "ProviderError",
    "ProviderHTTPError",
    "ProviderImageDecodeError",
    "ProviderNetworkError",
    "ProviderResponseError",
    "normalize_base_url",
]
