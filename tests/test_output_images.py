"""合成图片和假投递验证压缩、配置迁移、恢复与计费边界。"""

from __future__ import annotations

import asyncio
import base64
import io
import random
import stat
import threading
import tomllib
from datetime import timedelta
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin
from test_service import _config, _Context, _invocation, _png, _Provider, _scene_photo_llm_replies

from maitu_photo.compression import ImageTooLargeError, InvalidImageError, compress_image
from maitu_photo.config import PhotoPluginConfig
from maitu_photo.delivery import delivery_policy, prepare_delivery_image
from maitu_photo.models import ImageTask, TaskStatus, utc_now
from maitu_photo.service import PhotoStudioService
from maitu_photo.sizes import kb_to_bytes
from scripts.migrate_config_v040 import migrate_file, migrate_text


def _noise() -> bytes:
    image = Image.frombytes("RGB", (1024, 1024), random.Random(42).randbytes(1024 * 1024 * 3))
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


@pytest.mark.parametrize("value", [True, False, 0, -1, float("nan"), float("inf"), "1024", 0.0001])
def test_kb_fields_reject_invalid_values(value: object) -> None:
    for section, field in (("references", "max_kb"), ("openai", "max_response_kb"), ("output", "image_max_kb")):
        with pytest.raises(ValueError):
            PhotoPluginConfig.model_validate({section: {field: value}})


def test_kb_defaults_preserve_byte_limits_and_single_delivery_setting() -> None:
    config = PhotoPluginConfig()
    assert kb_to_bytes(config.references.max_kb) == 480_000
    assert kb_to_bytes(config.openai.max_response_kb) == 32 * 1024 * 1024
    assert kb_to_bytes(PhotoPluginConfig.model_validate({"references": {"max_kb": 500}}).references.max_kb) == 512_000
    with pytest.raises(ValueError):
        PhotoPluginConfig.model_validate({"references": {"max_kb": 500.0001}})
    assert config.output.image_max_kb == 1024
    assert delivery_policy(config.output) == {"max_kb": 1024.0}
    assert not any(
        hasattr(config.output, key)
        for key in ("image_compression_enabled", "image_max_edge", "image_min_quality", "image_max_quality")
    )


def test_migration_preserves_other_values_comments_and_is_idempotent(tmp_path: Path) -> None:
    original = (
        '# 保留注释\n[plugin]\nconfig_version = "1.8.0"\n[openai]\napi_key = "fake-only"\n'
        "max_response_bytes = 33554432 # 下载\n[references]\nmax_bytes = 123457 # 图片\n"
    )
    path = tmp_path / "config.toml"
    path.write_text(original)
    assert len(migrate_file(path)) == 2
    assert path.read_text() == original
    assert len(migrate_file(path, apply=True)) == 2
    updated = tomllib.loads(path.read_text())
    assert updated["openai"]["max_response_kb"] == 32768
    assert updated["references"]["max_kb"] * 1024 == 123457
    assert updated["openai"]["api_key"] == "fake-only"
    assert "# 保留注释" in path.read_text() and "# 图片" in path.read_text()
    backups = list((tmp_path / "config_back").iterdir())
    assert len(backups) == 1 and backups[0].read_text() == original
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert migrate_file(path, apply=True) == []
    assert len(list((tmp_path / "config_back").iterdir())) == 1


def test_migration_existing_kb_wins_and_corrupt_file_is_not_written(tmp_path: Path) -> None:
    text = "[references]\nmax_bytes = 480000\nmax_kb = 100.5\n"
    result, changes = migrate_text(text)
    assert len(changes) == 1
    assert tomllib.loads(result)["references"] == {"max_kb": 100.5}
    path = tmp_path / "config.toml"
    path.write_text('[references]\nmax_bytes = "broken"\n')
    with pytest.raises(ValueError):
        migrate_file(path, apply=True)
    assert not (tmp_path / "config_back").exists()


def test_static_passthrough_and_independent_delivery_budget() -> None:
    config = PhotoPluginConfig()
    policy = delivery_policy(config.output)
    source = _png("green")
    image = prepare_delivery_image(source, "image/png", policy)
    assert image.data == source and image.media_type == "image/png"
    assert not image.metadata["compressed"]
    noise = _noise()
    with pytest.raises(ImageTooLargeError):
        compress_image(noise, target_bytes=900_000)
    image = prepare_delivery_image(noise, "image/png", {**policy, "max_kb": 900_000 / 1024})
    assert 512_000 < len(image.data) <= 900_000
    assert image.metadata["compressed"] and image.media_type == "image/jpeg"


def test_impossible_target_and_animation_do_not_fall_back() -> None:
    config = PhotoPluginConfig()
    policy = {**delivery_policy(config.output), "enabled": True, "max_kb": 1 / 1024}
    with pytest.raises(ImageTooLargeError):
        prepare_delivery_image(_png("red"), "image/png", policy)
    output = io.BytesIO()
    first = Image.new("RGB", (12, 12), "red")
    first.save(output, "GIF", save_all=True, append_images=[Image.new("RGB", (12, 12), "blue")])
    with pytest.raises(InvalidImageError):
        prepare_delivery_image(output.getvalue(), "image/gif", policy)
    image = prepare_delivery_image(output.getvalue(), "image/gif", {"enabled": False})
    assert image.data == output.getvalue()


def test_delivery_normalizes_orientation_and_transparency_and_rejects_corruption() -> None:
    policy = {"max_kb": 1}
    output = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text("padding", "large metadata" * 1000)
    Image.new("RGBA", (80, 40), (0, 0, 0, 0)).save(output, "PNG", pnginfo=info)
    result = prepare_delivery_image(output.getvalue(), "image/png", policy)
    with Image.open(io.BytesIO(result.data)) as image:
        assert image.size == (80, 40) and image.getpixel((0, 0)) == (255, 255, 255)
    output = io.BytesIO()
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "padding" * 1000
    Image.new("RGB", (80, 40), "red").save(output, "JPEG", exif=exif)
    result = prepare_delivery_image(output.getvalue(), "image/jpeg", policy)
    with Image.open(io.BytesIO(result.data)) as image:
        assert image.size == (40, 80) and not image.getexif()
    with pytest.raises(InvalidImageError):
        prepare_delivery_image(b"corrupt", "image/png", {"max_kb": 1 / 1024})


def test_at_limit_and_large_dimensions_under_limit_are_unchanged() -> None:
    output = io.BytesIO()
    Image.new("RGB", (4096, 8), "red").save(output, "PNG")
    data = output.getvalue()
    result = prepare_delivery_image(data, "image/png", {"max_kb": len(data) / 1024})
    assert result.data == data and not result.metadata["compressed"]
    result = prepare_delivery_image(data, "image/png", {"max_kb": 1024})
    assert result.data == data and not result.metadata["compressed"]


@pytest.mark.parametrize("value", [0.0001, 500.0001, float("nan"), float("inf"), True])
def test_migration_rejects_invalid_existing_reference_kb(value: object) -> None:
    literal = str(value).lower()
    with pytest.raises(ValueError):
        migrate_text(f"[references]\nmax_bytes = 480000\nmax_kb = {literal}\n")


@pytest.mark.asyncio
async def test_shutdown_waits_for_image_thread_before_closing_storage(tmp_path: Path) -> None:
    service = PhotoStudioService(_Context(), _config(), tmp_path)
    started = threading.Event()
    release = threading.Event()

    def read_after_shutdown_begins() -> int:
        started.set()
        assert release.wait(5)
        return service.storage.connection.execute("SELECT 1").fetchone()[0]

    worker = asyncio.create_task(service.run_image_work(read_after_shutdown_begins))
    closer = None
    try:
        assert await asyncio.to_thread(started.wait, 5)
        closer = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        assert not closer.done()
    finally:
        release.set()
        assert await worker == 1
        if closer is not None:
            await closer


@pytest.mark.asyncio
async def test_send_retry_and_reopen_keep_policy_original_and_single_model_call(tmp_path: Path) -> None:
    config = _config()
    config.output.image_max_kb = 20
    ctx = _Context(_scene_photo_llm_replies(), image_success=False)
    service = PhotoStudioService(ctx, config, tmp_path)
    original = _noise()
    provider = _Provider([original])
    service._provider = provider
    task = service.submit_scene_photo(_invocation(), description="test scene", use_scene_reference=False)
    config.output.image_max_kb = 5000
    await service._handle_task(service.storage.claim_next_task())
    failed = service.storage.get_task(task.id)
    assert failed.status == TaskStatus.FAILED
    assert failed.result_path.read_bytes() == original
    sent = base64.b64decode(ctx.send.images[0][0])
    assert len(sent) <= 20 * 1024
    assert len(provider.calls) == 1
    derivative = service._delivery_path(task.id)
    assert derivative.read_bytes() == sent
    await service.close()

    ctx.send.image_success = True
    restored = PhotoStudioService(ctx, config, tmp_path)
    restored._provider = provider
    restored.retry_task(task.id)
    await restored._handle_task(restored.storage.claim_next_task())
    saved = restored.storage.get_task(task.id)
    assert saved.status == TaskStatus.SENT
    assert len(provider.calls) == 1 and len(ctx.send.images) == 2
    assert base64.b64decode(ctx.send.images[1][0]) == sent
    details = restored.task_result(_invocation(), task.id, include_image=True)
    assert base64.b64decode(details["content_items"][0]["data"]) == sent
    assert saved.result_metadata["output_image"]["enabled"] is True
    restored.cleanup_expired(now=utc_now() + timedelta(days=2))
    assert not derivative.exists()
    assert restored.storage.get_task(task.id).result_path is None
    await restored.close()


@pytest.mark.asyncio
async def test_compression_failure_retains_generated_result_without_sending_or_regenerating(tmp_path: Path) -> None:
    config = _config()
    config.output.image_max_kb = 1 / 1024
    ctx = _Context(_scene_photo_llm_replies())
    service = PhotoStudioService(ctx, config, tmp_path)
    provider = _Provider([_png("red")])
    service._provider = provider
    task = service.submit_scene_photo(_invocation(), description="test", use_scene_reference=False)
    await service._handle_task(service.storage.claim_next_task())
    assert service.storage.get_task(task.id).status == TaskStatus.FAILED
    assert service.storage.get_task(task.id).result_path.is_file()
    service.retry_task(task.id)
    await service._handle_task(service.storage.claim_next_task())
    assert len(provider.calls) == 1 and not ctx.send.images
    await service.close()


def test_legacy_task_uses_original_when_compression_is_now_enabled(tmp_path: Path) -> None:
    config = _config()
    config.output.image_max_kb = 1 / 1024
    service = PhotoStudioService(_Context(), config, tmp_path)
    source = tmp_path / "results" / "legacy.png"
    source.write_bytes(_png("red"))
    task = service.storage.create_task(ImageTask(kind="image", scope_key="legacy", result_path=source))
    assert service.prepare_task_image(task).data == source.read_bytes()
    asyncio.run(service.close())
