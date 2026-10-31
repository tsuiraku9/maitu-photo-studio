"""发送图片准备，不改写供应商返回的原图。"""

from __future__ import annotations

import io
import warnings
from dataclasses import dataclass
from typing import Any, Mapping

from PIL import Image, UnidentifiedImageError

from .compression import (
    DEFAULT_MAX_INPUT_BYTES,
    DEFAULT_MAX_PIXELS,
    ImageEncodingConfig,
    InvalidImageError,
    encode_image,
)
from .config import OutputSection
from .sizes import kb_to_bytes


def delivery_policy(settings: OutputSection) -> dict[str, Any]:
    """只快照压缩参数，避免持久化密钥及无关配置。"""

    return {"max_kb": settings.image_max_kb}


@dataclass(frozen=True)
class DeliveryImage:
    data: bytes
    media_type: str
    metadata: dict[str, Any]


def prepare_delivery_image(data: bytes, media_type: str, policy: Mapping[str, Any]) -> DeliveryImage:
    """在工作线程中调用；已达标的静态图直接复用，压缩失败不回退原图。"""

    metadata: dict[str, Any] = {
        "enabled": policy.get("enabled", True) is not False,
        "compressed": False,
        "original_kb": len(data) / 1024,
        "size_kb": len(data) / 1024,
    }
    if not metadata["enabled"]:
        return DeliveryImage(data, media_type, metadata)
    target_bytes = kb_to_bytes(policy["max_kb"])
    if not data or len(data) > DEFAULT_MAX_INPUT_BYTES:
        raise InvalidImageError("发送图片为空或超过解码大小限制")
    if len(data) <= target_bytes:
        return DeliveryImage(data, media_type, metadata)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            # PNG 的 verify 必须紧接 open；读取 EXIF 或帧信息会移动解析位置。
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"JPEG", "PNG", "WEBP"} or getattr(image, "n_frames", 1) != 1:
                    raise InvalidImageError("发送压缩仅支持静态 JPEG、PNG 和 WebP 图片")
                if image.width * image.height > DEFAULT_MAX_PIXELS:
                    raise InvalidImageError("发送图片超过解码像素限制")
                metadata.update(width=image.width, height=image.height, format=image.format)
                image.load()
    except (
        UnidentifiedImageError,
        OSError,
        EOFError,
        SyntaxError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise InvalidImageError("发送图片无效、格式不支持或超过解码限制") from exc
    # 大小是唯一公开限制；从原尺寸开始编码，仅在最低质量仍超限时缩小。
    cfg = ImageEncodingConfig(target_bytes=target_bytes, max_edge=max(metadata["width"], metadata["height"]))
    result = encode_image(data, config=cfg)
    metadata.update(
        compressed=True,
        size_kb=result.size / 1024,
        width=result.width,
        height=result.height,
        format="JPEG",
        quality=result.quality,
    )
    return DeliveryImage(result.data, result.content_type, metadata)
