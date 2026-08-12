"""Image normalisation and reference-image compression utilities.

The gallery stores only the bytes returned by :func:`compress_image`.  Keeping
the pipeline here (rather than in the storage layer) makes it difficult for a
new import path to accidentally bypass the size limit.

The public API intentionally accepts bytes, paths and binary file objects.  A
``CompressionResult`` contains the encoded bytes as well as the digest and
dimensions needed by the gallery database.
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Union

from PIL import Image, ImageOps, UnidentifiedImageError

# Pillow's default can be changed by applications.  We do not change the
# process-wide value, but still reject bomb warnings/errors while opening an
# untrusted image.
DEFAULT_TARGET_BYTES = 480_000
HARD_MAX_TARGET_BYTES = 500_000
DEFAULT_MAX_EDGE = 2048
DEFAULT_MAX_QUALITY = 90
DEFAULT_MIN_QUALITY = 55
DEFAULT_MAX_DOWNSCALE_ITERATIONS = 16
DEFAULT_MAX_INPUT_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_PIXELS = 40_000_000


class ImageCompressionError(ValueError):
    """Raised when an image is invalid or cannot satisfy the configured limit."""


# More explicit aliases are useful to callers that distinguish validation from
# an encoding failure, while preserving a single catchable base exception.
class InvalidImageError(ImageCompressionError):
    """The input is not a supported, decodable image."""


class ImageTooLargeError(ImageCompressionError):
    """The configured output limit is invalid or could not be met."""


@dataclass(frozen=True)
class CompressionConfig:
    """Configuration for :func:`compress_image`.

    ``target_bytes`` is deliberately checked against the hard 500 KB ceiling;
    allowing a larger value would make the gallery invariant dependent on a
    caller's configuration typo.
    """

    target_bytes: int = DEFAULT_TARGET_BYTES
    max_edge: int = DEFAULT_MAX_EDGE
    max_quality: int = DEFAULT_MAX_QUALITY
    min_quality: int = DEFAULT_MIN_QUALITY
    max_downscale_iterations: int = DEFAULT_MAX_DOWNSCALE_ITERATIONS
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES
    max_pixels: int = DEFAULT_MAX_PIXELS

    def __post_init__(self) -> None:
        if not isinstance(self.target_bytes, int) or isinstance(self.target_bytes, bool):
            raise ValueError("target_bytes must be an integer")
        if self.target_bytes <= 0 or self.target_bytes > HARD_MAX_TARGET_BYTES:
            raise ValueError(f"target_bytes must be between 1 and {HARD_MAX_TARGET_BYTES}")
        if self.max_edge <= 0:
            raise ValueError("max_edge must be positive")
        if not 1 <= self.min_quality <= self.max_quality <= 95:
            raise ValueError("quality range must satisfy 1 <= min <= max <= 95")
        if self.max_downscale_iterations < 0:
            raise ValueError("max_downscale_iterations must be non-negative")
        if self.max_input_bytes <= 0:
            raise ValueError("max_input_bytes must be positive")
        if self.max_pixels <= 0:
            raise ValueError("max_pixels must be positive")


@dataclass(frozen=True)
class CompressionResult:
    """A compressed JPEG and metadata suitable for a gallery record."""

    data: bytes
    sha256: str
    width: int
    height: int
    format: str = "JPEG"
    content_type: str = "image/jpeg"
    quality: int = DEFAULT_MIN_QUALITY
    original_width: int | None = None
    original_height: int | None = None
    original_size: int | None = None
    iterations: int = 0

    @property
    def size(self) -> int:
        """Encoded byte length (kept as a property for ergonomic callers)."""

        return len(self.data)

    @property
    def byte_size(self) -> int:
        return len(self.data)

    def to_dict(self) -> dict[str, object]:
        return {
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
            "format": self.format,
            "content_type": self.content_type,
            "quality": self.quality,
            "size": len(self.data),
            "original_width": self.original_width,
            "original_height": self.original_height,
            "original_size": self.original_size,
            "iterations": self.iterations,
        }


ImageSource = Union[bytes, bytearray, memoryview, BinaryIO, str, os.PathLike[str]]


def _read_source(source: ImageSource, max_input_bytes: int) -> bytes:
    if isinstance(source, bytes):
        data = source
    elif isinstance(source, (bytearray, memoryview)):
        data = bytes(source)
    elif isinstance(source, (str, os.PathLike, Path)):
        path = Path(source)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise InvalidImageError(f"unable to read image path: {exc}") from exc
        if size > max_input_bytes:
            raise InvalidImageError("image input exceeds configured limit")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise InvalidImageError(f"unable to read image path: {exc}") from exc
    elif hasattr(source, "read"):
        try:
            data = source.read()
        except (OSError, ValueError) as exc:
            raise InvalidImageError(f"unable to read image stream: {exc}") from exc
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise InvalidImageError("image stream did not return bytes")
        data = bytes(data)
    else:
        raise InvalidImageError("source must be image bytes, a path, or a binary stream")

    if len(data) == 0:
        raise InvalidImageError("image input is empty")
    if len(data) > max_input_bytes:
        raise InvalidImageError("image input exceeds configured limit")
    return bytes(data)


def _open_normalized(data: bytes, max_pixels: int) -> tuple[Image.Image, int, int]:
    """Decode the first frame and normalise it to metadata-free RGB pixels."""

    try:
        # Treat decompression-bomb warnings as hard errors.  The warning class
        # is available on supported Pillow versions; use getattr for old ones.
        bomb_warning = getattr(Image, "DecompressionBombWarning", RuntimeWarning)
        with warnings.catch_warnings():
            warnings.simplefilter("error", bomb_warning)
            with Image.open(io.BytesIO(data)) as opened:
                # Animated formats are intentionally reduced to their first
                # frame.  ``copy`` detaches pixels from the closed source.
                try:
                    opened.seek(0)
                except EOFError:
                    pass
                original_width, original_height = opened.size
                if original_width * original_height > max_pixels:
                    raise InvalidImageError("image exceeds configured pixel safety limit")
                frame = opened.copy()
                frame.load()
    except (Image.DecompressionBombError, bomb_warning) as exc:  # type: ignore[misc]
        raise InvalidImageError("image exceeds Pillow's decompression safety limit") from exc
    except (UnidentifiedImageError, OSError, ValueError, EOFError) as exc:
        raise InvalidImageError("input is not a valid decodable image") from exc

    if original_width <= 0 or original_height <= 0:
        raise InvalidImageError("image has invalid dimensions")

    try:
        # Apply orientation before compositing/resizing.  exif_transpose returns
        # a new image when an orientation tag is present and drops that tag.
        frame = ImageOps.exif_transpose(frame)
        # Palette images may carry transparency in ``info`` even when their
        # mode is not RGBA, so inspect both mode and metadata.
        has_alpha = frame.mode in ("RGBA", "LA") or "transparency" in frame.info
        if has_alpha:
            rgba = frame.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            frame = Image.alpha_composite(background, rgba).convert("RGB")
        else:
            frame = frame.convert("RGB")
    except (OSError, ValueError) as exc:
        raise InvalidImageError("unable to normalise image pixels") from exc

    # A converted image has no source metadata attached.  Explicitly clear the
    # info dictionary as an additional guard against ICC/EXIF propagation.
    frame.info.clear()
    return frame, original_width, original_height


def _fit_max_edge(image: Image.Image, max_edge: int) -> Image.Image:
    width, height = image.size
    longest = max(width, height)
    if longest <= max_edge:
        return image
    scale = max_edge / float(longest)
    dimensions = (max(1, round(width * scale)), max(1, round(height * scale)))
    return image.resize(dimensions, Image.Resampling.LANCZOS)


def _encode_jpeg(image: Image.Image, quality: int) -> bytes:
    output = io.BytesIO()
    try:
        # No exif/icc/comment arguments are supplied.  Since ``image`` is a
        # freshly converted RGB image, Pillow cannot copy source metadata.
        image.save(
            output,
            format="JPEG",
            quality=int(quality),
            optimize=True,
            progressive=False,
            subsampling=2,
        )
    except (OSError, ValueError) as exc:
        raise ImageCompressionError("unable to encode normalized JPEG") from exc
    return output.getvalue()


def _best_encoding(
    image: Image.Image, target_bytes: int, max_quality: int, min_quality: int
) -> tuple[bytes | None, int, int]:
    """Return the highest quality encoding in the configured quality range."""

    # JPEG size is effectively monotonic with quality for a fixed image.  A
    # descending scan is intentionally used instead of assuming strict
    # monotonicity: unusual quantisation tables can produce equal sizes.
    last_data = b""
    for quality in range(max_quality, min_quality - 1, -1):
        encoded = _encode_jpeg(image, quality)
        last_data = encoded
        if len(encoded) <= target_bytes:
            return encoded, quality, len(encoded)
    return None, min_quality, len(last_data)


def compress_image(
    source: ImageSource,
    *,
    target_bytes: int = DEFAULT_TARGET_BYTES,
    max_edge: int = DEFAULT_MAX_EDGE,
    max_quality: int = DEFAULT_MAX_QUALITY,
    min_quality: int = DEFAULT_MIN_QUALITY,
    max_downscale_iterations: int = DEFAULT_MAX_DOWNSCALE_ITERATIONS,
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
    max_pixels: int = DEFAULT_MAX_PIXELS,
    config: CompressionConfig | None = None,
) -> CompressionResult:
    """Normalize and compress ``source`` to a metadata-free JPEG.

    The output is guaranteed to be no larger than ``target_bytes`` (and never
    larger than 500,000 bytes).  If the image cannot meet the limit after the
    configured number of downscale passes, :class:`ImageTooLargeError` is
    raised instead of returning an oversized artifact.
    """

    if config is not None:
        # Explicit keyword arguments are intentionally ignored when a config
        # object is supplied; this avoids silently mixing two configurations.
        cfg = config
    else:
        try:
            cfg = CompressionConfig(
                target_bytes=target_bytes,
                max_edge=max_edge,
                max_quality=max_quality,
                min_quality=min_quality,
                max_downscale_iterations=max_downscale_iterations,
                max_input_bytes=max_input_bytes,
                max_pixels=max_pixels,
            )
        except ValueError as exc:
            raise ImageTooLargeError(str(exc)) from exc

    data = _read_source(source, cfg.max_input_bytes)
    image, original_width, original_height = _open_normalized(data, cfg.max_pixels)
    original_size = len(data)
    image = _fit_max_edge(image, cfg.max_edge)

    current = image
    iterations = 0
    while True:
        encoded, quality, encoded_size = _best_encoding(current, cfg.target_bytes, cfg.max_quality, cfg.min_quality)
        if encoded is not None:
            digest = hashlib.sha256(encoded).hexdigest()
            width, height = current.size
            return CompressionResult(
                data=encoded,
                sha256=digest,
                width=width,
                height=height,
                quality=quality,
                original_width=original_width,
                original_height=original_height,
                original_size=original_size,
                iterations=iterations,
            )

        if iterations >= cfg.max_downscale_iterations:
            raise ImageTooLargeError(
                f"unable to compress image below {cfg.target_bytes} bytes "
                f"after {cfg.max_downscale_iterations} downscale iterations"
            )

        width, height = current.size
        if width <= 1 and height <= 1:
            raise ImageTooLargeError(f"smallest JPEG is still larger than {cfg.target_bytes} bytes")

        # A square-root estimate is appropriate for photographic data because
        # byte count is roughly proportional to pixel count.  The safety factor
        # prevents oscillation caused by JPEG headers and quantisation effects.
        if encoded_size <= 0:
            scale = 0.5
        else:
            scale = math.sqrt(cfg.target_bytes / float(encoded_size)) * 0.92
        scale = min(0.90, max(0.05, scale))
        new_width = max(1, int(math.floor(width * scale)))
        new_height = max(1, int(math.floor(height * scale)))
        if new_width >= width and new_height >= height:
            new_width = max(1, width - 1)
            new_height = max(1, height - 1)
        current = current.resize((new_width, new_height), Image.Resampling.LANCZOS)
        iterations += 1


def compress_image_bytes(data: bytes, **kwargs: object) -> CompressionResult:
    """Bytes-oriented alias used by importers and provider adapters."""

    return compress_image(data, **kwargs)  # type: ignore[arg-type]


def normalize_and_compress(source: ImageSource, **kwargs: object) -> CompressionResult:
    """Compatibility alias for callers that use the longer descriptive name."""

    return compress_image(source, **kwargs)  # type: ignore[arg-type]


def compress_reference_image(source: ImageSource, **kwargs: object) -> CompressionResult:
    """Semantic alias for gallery reference imports."""

    return compress_image(source, **kwargs)  # type: ignore[arg-type]


# A couple of names used by earlier prototypes are retained as aliases so the
# storage/worker layer can evolve without forcing a migration of call sites.
compress_to_size = compress_image
compress_image_to_size = compress_image


__all__ = [
    "CompressionConfig",
    "CompressionResult",
    "ImageCompressionError",
    "ImageTooLargeError",
    "InvalidImageError",
    "DEFAULT_TARGET_BYTES",
    "HARD_MAX_TARGET_BYTES",
    "compress_image",
    "compress_image_bytes",
    "compress_reference_image",
    "compress_to_size",
    "compress_image_to_size",
    "normalize_and_compress",
]
