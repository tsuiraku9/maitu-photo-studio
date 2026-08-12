from __future__ import annotations

import hashlib
import io

import pytest
from PIL import Image

from maitu_photo.compression import (
    HARD_MAX_TARGET_BYTES,
    ImageTooLargeError,
    InvalidImageError,
    compress_image,
)


def _encode(image: Image.Image, format: str, **kwargs: object) -> bytes:
    output = io.BytesIO()
    image.save(output, format=format, **kwargs)
    return output.getvalue()


def test_transparency_is_flattened_on_white_and_metadata_is_removed() -> None:
    image = Image.new("RGBA", (32, 24), (255, 0, 0, 128))
    image.info["comment"] = b"must-not-survive"

    result = compress_image(_encode(image, "PNG"), target_bytes=20_000)

    assert result.data.startswith(b"\xff\xd8\xff")
    assert result.sha256 == hashlib.sha256(result.data).hexdigest()
    assert result.size <= 20_000
    with Image.open(io.BytesIO(result.data)) as decoded:
        decoded.load()
        assert decoded.mode == "RGB"
        red, green, blue = decoded.getpixel((10, 10))
        assert red >= 245
        assert 115 <= green <= 140
        assert 115 <= blue <= 140
        assert "exif" not in decoded.info
        assert "icc_profile" not in decoded.info
        assert "comment" not in decoded.info


def test_exif_orientation_is_applied_before_encoding() -> None:
    image = Image.new("RGB", (40, 20), "white")
    exif = image.getexif()
    exif[274] = 6  # rotate 90 degrees clockwise for display
    source = _encode(image, "JPEG", quality=90, exif=exif)

    result = compress_image(source)

    assert (result.original_width, result.original_height) == (40, 20)
    assert (result.width, result.height) == (20, 40)
    with Image.open(io.BytesIO(result.data)) as decoded:
        assert decoded.getexif().get(274) is None


def test_animated_input_uses_first_frame() -> None:
    first = Image.new("RGB", (24, 24), "red")
    second = Image.new("RGB", (24, 24), "blue")
    source = io.BytesIO()
    first.save(source, "GIF", save_all=True, append_images=[second], loop=0, duration=100)

    result = compress_image(source.getvalue())

    with Image.open(io.BytesIO(result.data)) as decoded:
        red, green, blue = decoded.getpixel((12, 12))
        assert red > 220 and green < 30 and blue < 30


def test_noisy_large_image_is_resized_to_edge_and_byte_limit() -> None:
    # effect_noise creates a realistic high-entropy source that cannot satisfy
    # the target merely because it is a single flat colour.
    noise = Image.effect_noise((1400, 900), 100).convert("RGB")
    source = _encode(noise, "PNG")

    result = compress_image(source, target_bytes=18_000, max_edge=500)

    assert result.size <= 18_000
    assert max(result.width, result.height) <= 500
    assert 55 <= result.quality <= 90
    assert result.iterations >= 1
    with Image.open(io.BytesIO(result.data)) as decoded:
        decoded.verify()


def test_hard_ceiling_cannot_be_disabled_by_configuration() -> None:
    source = _encode(Image.new("RGB", (8, 8), "white"), "PNG")

    with pytest.raises(ImageTooLargeError, match=str(HARD_MAX_TARGET_BYTES)):
        compress_image(source, target_bytes=HARD_MAX_TARGET_BYTES + 1)


@pytest.mark.parametrize("source", [b"", b"not an image", b"\x89PNG\r\n\x1a\ntruncated"])
def test_invalid_input_is_rejected(source: bytes) -> None:
    with pytest.raises(InvalidImageError):
        compress_image(source)


def test_pillow_decompression_bomb_warning_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _encode(Image.new("RGB", (20, 20), "white"), "PNG")
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)

    with pytest.raises(InvalidImageError, match="decompression"):
        compress_image(source)
