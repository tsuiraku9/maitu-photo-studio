"""Exercise the WebUI adapter with temporary files and local fake capabilities.

No host, startup scanner, background workers, or real model client is started.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import random
import sqlite3
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from PIL import Image

from maitu_photo.config import PhotoPluginConfig
from maitu_photo.models import AssetStatus, ImageTask, ReferenceAsset, TaskReference, TaskStatus, utc_now
from maitu_photo.provider import GeneratedImage
from maitu_photo.service import PhotoStudioService
from maitu_photo.storage import StorageError
from maitu_photo.webui_api import PhotoWebUI

pytestmark = pytest.mark.asyncio

SECRET = "fake-webui-api-secret"


def _id() -> str:
    return uuid4().hex


def _png(colour: str | tuple[int, int, int] = "green") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (24, 16), colour).save(output, "PNG")
    return output.getvalue()


def _tags(category: str = "outfit") -> dict[str, Any]:
    return {
        "person": {"appearance_summary": "short dark hair", "confidence": 0.9},
        "outfit": {
            "type": "dress",
            "wearing_scenes": ["home"],
            "seasons": ["summer"],
            "styles": ["minimal"],
            "confidence": 0.9,
        },
        "scene": {
            "room_type": "bedroom",
            "privacy_eligible": True,
            "scene_signature": "window-left",
            "confidence": 0.9,
        },
    }[category]


class _Uploads:
    def __init__(self) -> None:
        self.envelopes: dict[str, Any] = {}
        self.calls: list[str] = []

    async def claim_upload(self, upload_id: str) -> Any:
        self.calls.append(upload_id)
        if upload_id not in self.envelopes:
            raise ValueError("upload was already claimed")
        return self.envelopes.pop(upload_id)


class _LLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.replies: list[dict[str, Any]] = []
        self.before: Callable[[], None] | None = None

    async def generate(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.before is not None:
            self.before()
        if not self.replies:
            raise AssertionError("unexpected fake tagging-model call")
        return {"success": True, "response": json.dumps(self.replies.pop(0))}


class _Provider:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.outputs: list[bytes] = []

    async def generate(self, prompt: str, **kwargs: Any) -> GeneratedImage:
        self.calls.append({"prompt": prompt, **kwargs})
        if not self.outputs:
            raise AssertionError("unexpected fake image-model call")
        return GeneratedImage(self.outputs.pop(0), media_type="image/png")

    async def aclose(self) -> None:
        return None


class _NoDelivery:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def call(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))
        raise AssertionError("WebUI jobs must not send chat or Planner notifications")


class _HostConfig:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get(self, key: str, default: str = "") -> str:
        self.calls.append(key)
        return {"bot.nickname": "fake bot", "personality.personality": "quiet and curious"}.get(key, default)


class _Environment:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.data_dir = tmp_path / "plugin-data"
        self.uploads = _Uploads()
        self.llm = _LLM()
        self.provider = _Provider()
        self.delivery = _NoDelivery()
        self.host_config = _HostConfig()
        self.ctx = SimpleNamespace(
            logger=logging.getLogger("test.maitu.webui"),
            paths=SimpleNamespace(runtime_dir=tmp_path / "runtime"),
            webui=self.uploads,
            llm=self.llm,
            config=self.host_config,
            send=SimpleNamespace(image=self.delivery.call, text=self.delivery.call),
            maisaka=SimpleNamespace(
                context=SimpleNamespace(append=self.delivery.call),
                proactive=SimpleNamespace(trigger=self.delivery.call),
            ),
        )
        self.config = PhotoPluginConfig()
        self.config.openai.base_url = "https://models.invalid/v1"
        self.config.openai.api_key = SECRET
        self.config.openai.generation_model = "fake-only"
        self.config.openai.reference_model = "fake-reference-only"
        self._asset_count = 0
        self._open()

    def _open(self) -> None:
        self.service = PhotoStudioService(self.ctx, self.config, self.data_dir)
        # Never allow _provider_instance to instantiate a real network client.
        self.service._provider = self.provider  # type: ignore[assignment]
        self.api = PhotoWebUI(self.service, self.ctx)

    def reopen(self) -> None:
        self.service.storage.close()
        self._open()

    def upload(self, data: bytes | None = None) -> tuple[str, Path]:
        data = _png() if data is None else data
        upload_id = _id()
        path = self.service.payloads.upload_root / f"{upload_id}.claimed"
        path.write_bytes(data)
        self.uploads.envelopes[upload_id] = {
            "success": True,
            "upload": {
                "path": str(path),
                "sha256": hashlib.sha256(data).hexdigest(),
                "name": "untrusted-original.png",
                "size": len(data),
            },
        }
        return upload_id, path

    def asset(self, category: str = "outfit", **kwargs: Any) -> ReferenceAsset:
        self._asset_count += 1
        asset_id = _id()
        data = _png((self._asset_count % 255, 50, 100))
        source = self.data_dir / "sources" / category / f"{asset_id}.jpg"
        reference = self.data_dir / "references" / category / f"{asset_id}.jpg"
        for path in (source, reference):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return self.service.gallery.add_reference(
            category=category,
            name=kwargs.pop("name", f"reference {self._asset_count}"),
            source_path=source,
            reference_path=reference,
            sha256=hashlib.sha256(data).hexdigest(),
            tags=kwargs.pop("tags", _tags(category)),
            **kwargs,
        )

    def assert_no_models_or_delivery(self) -> None:
        assert self.llm.calls == []
        assert self.provider.calls == []
        assert self.delivery.calls == []


@pytest.fixture
def env(tmp_path: Path) -> Iterator[_Environment]:
    environment = _Environment(tmp_path)
    try:
        yield environment
    finally:
        # Workers and startup scanning were never started.
        environment.service.storage.close()


async def _stage(env: _Environment, data: bytes | None = None) -> tuple[str, Path]:
    upload_id, source = env.upload(data)
    response = await env.api.stage_upload(upload_id, _id())
    return response["stage_id"], source


async def _confirm(api: PhotoWebUI, action: str, params: dict[str, Any], request_id: str) -> str:
    response = await api.action("confirm", _id(), {"action": action, "params": params, "target_request_id": request_id})
    return response["confirmation"]["token"]


def _versions(*assets: ReferenceAsset) -> list[dict[str, str]]:
    return [{"asset_id": asset.id, "updated_at": asset.updated_at.isoformat()} for asset in assets]


def _cache_snapshot(api: PhotoWebUI) -> list[tuple[str, int, bytes | None]]:
    return [
        (str(path.relative_to(api.images.root)), path.stat().st_mtime_ns, path.read_bytes() if path.is_file() else None)
        for path in sorted(api.images.root.rglob("*"))
    ]


def _assert_private(response: Any, env: _Environment) -> None:
    serialized = json.dumps(response, ensure_ascii=False)
    for forbidden in (
        SECRET,
        str(env.root),
        "models.invalid",
        "source_path",
        "reference_path",
        "result_path",
        "api_key",
    ):
        assert forbidden not in serialized


async def test_wrapped_claim_is_hash_checked_normalized_and_cleaned_without_models(env: _Environment) -> None:
    upload_id, source = env.upload()
    request_id = _id()
    response = await env.api.stage_upload(upload_id, request_id)
    assert response["stage_id"] == request_id
    assert response["expires_at"] > time.time()
    assert env.uploads.calls == [upload_id]
    assert not source.exists()
    staged_image = env.api.stage_root / f"{request_id}.jpg"
    staged_metadata = env.api.stage_root / f"{request_id}.json"
    with Image.open(staged_image) as image:
        assert image.format == "JPEG" and getattr(image, "n_frames", 1) == 1
    metadata = json.loads(staged_metadata.read_text())
    assert hashlib.sha256(staged_image.read_bytes()).hexdigest() == metadata["compression"]["sha256"]
    assert staged_image.stat().st_size <= env.config.references.max_kb * 1024
    receipt = env.service.storage.get_request(request_id)
    assert receipt["state"] == "completed" and receipt["task_id"] is None
    assert receipt["response"] == response
    assert env.service.storage.list_tasks() == []
    _assert_private(response, env)
    env.assert_no_models_or_delivery()


async def test_reference_500_kb_survives_upload_stage_and_task_source_validation(env: _Environment) -> None:
    env.config.references.max_kb = 500
    image = Image.frombytes("RGB", (1024, 1024), random.Random(42).randbytes(1024 * 1024 * 3))
    source = io.BytesIO()
    image.save(source, "PNG")
    upload_id, _ = env.upload(source.getvalue())
    request_id = _id()
    await env.api.stage_upload(upload_id, request_id)
    data, metadata = env.api._stage(request_id)
    # 覆盖旧的 500000 B 限制与新的 500 KB 上限之间的有效图片。
    assert 500_000 < len(data) <= 500 * 1024
    task = ImageTask(kind="reference_import", scope_key="webui", request={"origin": "webui"})
    prepared = env.service._prepared_reference_source(task, {"source_compression": metadata}, data)
    assert prepared is not None and prepared.data == data
    env.assert_no_models_or_delivery()


@pytest.mark.parametrize("digest", ["0" * 64, None, "A" * 64])
async def test_claim_hash_failure_cleans_original_and_is_not_reclaimed(env: _Environment, digest: Any) -> None:
    upload_id, source = env.upload()
    env.uploads.envelopes[upload_id]["upload"]["sha256"] = digest
    request_id = _id()
    with pytest.raises(ValueError, match="校验失败"):
        await env.api.stage_upload(upload_id, request_id)
    assert not source.exists()
    assert list(env.api.stage_root.iterdir()) == []
    receipt = env.service.storage.get_request(request_id)
    assert receipt["state"] == "failed"
    _assert_private(receipt["response"], env)
    assert await env.api.stage_upload(upload_id, request_id) == receipt["response"]
    assert env.uploads.calls == [upload_id]
    env.assert_no_models_or_delivery()


@pytest.mark.parametrize("format_name", ["PNG", "WEBP", "GIF"])
async def test_animated_upload_is_rejected_and_cleaned(env: _Environment, format_name: str) -> None:
    output = io.BytesIO()
    Image.new("RGB", (16, 12), "red").save(
        output,
        format_name,
        save_all=True,
        append_images=[Image.new("RGB", (16, 12), "blue")],
        duration=100,
        loop=0,
    )
    upload_id, source = env.upload(output.getvalue())
    request_id = _id()
    with pytest.raises(ValueError, match="仅支持静态"):
        await env.api.stage_upload(upload_id, request_id)
    assert not source.exists()
    assert not (env.api.stage_root / f"{request_id}.jpg").exists()
    assert env.service.storage.get_request(request_id)["state"] == "failed"
    env.assert_no_models_or_delivery()


async def test_corrupt_claimed_image_is_cleaned_and_does_not_stage(env: _Environment) -> None:
    upload_id, source = env.upload(b"not an image")
    request_id = _id()
    with pytest.raises(OSError):
        await env.api.stage_upload(upload_id, request_id)
    assert not source.exists()
    assert list(env.api.stage_root.iterdir()) == []
    assert env.service.storage.get_request(request_id)["state"] == "failed"
    env.assert_no_models_or_delivery()


@pytest.mark.parametrize("escape", ["outside", "symlink", "parent-traversal", "nested-directory"])
async def test_claimed_path_escape_is_rejected_without_touching_external_files(env: _Environment, escape: str) -> None:
    upload_id = _id()
    outside = env.root / "external.png"
    outside.write_bytes(_png())
    if escape == "outside":
        claimed = outside
    elif escape == "symlink":
        claimed = env.service.payloads.upload_root / "link.png"
        claimed.symlink_to(outside)
    elif escape == "parent-traversal":
        claimed = env.service.payloads.upload_root / ".." / ".." / "external.png"
    else:
        claimed = env.service.payloads.upload_root / "nested" / "upload.png"
        claimed.parent.mkdir()
        claimed.write_bytes(_png())
    env.uploads.envelopes[upload_id] = {
        "success": True,
        "upload": {"path": str(claimed), "sha256": hashlib.sha256(_png()).hexdigest()},
    }
    request_id = _id()
    with pytest.raises(ValueError, match="路径无效"):
        await env.api.stage_upload(upload_id, request_id)
    assert outside.read_bytes() == _png()
    assert claimed.exists()
    assert env.service.storage.get_request(request_id)["state"] == "failed"
    assert list(env.api.stage_root.iterdir()) == []
    env.assert_no_models_or_delivery()


async def test_symlinked_upload_root_is_not_trusted_or_allowed_to_delete_external_file(env: _Environment) -> None:
    outside_root = env.root / "external-uploads"
    outside_root.mkdir()
    source = outside_root / "claimed.png"
    source.write_bytes(_png())
    upload_root = env.data_dir / "uploads"
    upload_root.rmdir()
    upload_root.symlink_to(outside_root, target_is_directory=True)
    upload_id = _id()
    env.uploads.envelopes[upload_id] = {
        "success": True,
        "upload": {"path": str(source), "sha256": hashlib.sha256(_png()).hexdigest()},
    }
    with pytest.raises(ValueError, match="路径"):
        await env.api.stage_upload(upload_id, _id())
    assert source.read_bytes() == _png()
    assert list(env.api.stage_root.iterdir()) == []
    env.assert_no_models_or_delivery()


async def test_stage_receipt_survives_adapter_and_sqlite_reopen(env: _Environment) -> None:
    upload_id, source = env.upload()
    request_id = _id()
    first = await env.api.stage_upload(upload_id, request_id)
    env.reopen()
    assert await env.api.stage_upload(upload_id, request_id) == first
    assert env.uploads.calls == [upload_id]
    assert not source.exists()
    assert (env.api.stage_root / f"{request_id}.jpg").is_file()
    second_upload_id, second_source = env.upload(_png("blue"))
    with pytest.raises(StorageError):
        await env.api.stage_upload(second_upload_id, request_id)
    assert second_source.exists()
    assert env.uploads.calls == [upload_id]
    env.assert_no_models_or_delivery()


async def test_double_submit_queues_once_and_replays_across_adapter_reopen(env: _Environment) -> None:
    stage_id, _ = await _stage(env)
    request_id = _id()
    args = {"operation": "import", "category": "outfit", "name": "summer", "stage_id": stage_id}
    first, second = await asyncio.gather(
        env.api.action("submit", request_id, args), env.api.action("submit", request_id, args)
    )
    assert first == second
    assert len(env.service.storage.list_tasks()) == 1
    task = env.service.storage.get_task(first["task_id"])
    assert task.kind == "reference_import" and task.status == TaskStatus.QUEUED
    assert task.request["origin"] == "webui" and task.request["delivery"] == "none"
    assert not task.stream_id and not task.group_id and not task.user_id
    assert env.service.storage.get_request(request_id)["state"] == "accepted"
    assert env.service.storage.get_request(stage_id)["task_id"] == task.id
    assert not (env.api.stage_root / f"{stage_id}.jpg").exists()
    assert len(env.uploads.calls) == 1
    env.reopen()
    assert await env.api.action("submit", request_id, args) == first
    with pytest.raises(StorageError):
        await env.api.action("submit", request_id, {**args, "name": "different parameters"})
    assert len(env.service.storage.list_tasks()) == 1
    receipt = await env.api.task_query("receipt", {"request_id": request_id})
    assert receipt["receipt"] == {"request_id": request_id, "state": "accepted", "task_id": task.id, "response": first}
    _assert_private(first, env)
    _assert_private(receipt, env)
    env.assert_no_models_or_delivery()


async def test_stage_is_single_use_even_after_adapter_reopen(env: _Environment) -> None:
    stage_id, _ = await _stage(env)
    args = {"operation": "import", "category": "outfit", "stage_id": stage_id}
    first = await env.api.action("submit", _id(), args)
    env.reopen()
    second_request_id = _id()
    with pytest.raises(ValueError, match="已使用"):
        await env.api.action("submit", second_request_id, args)
    assert len(env.service.storage.list_tasks()) == 1
    assert env.service.storage.get_request(stage_id)["task_id"] == first["task_id"]
    assert env.service.storage.get_request(second_request_id)["state"] == "failed"
    env.assert_no_models_or_delivery()


@pytest.mark.parametrize("tamper", ["expired", "bytes", "symlink"])
async def test_expired_or_tampered_stage_never_queues(env: _Environment, tamper: str) -> None:
    stage_id, _ = await _stage(env)
    metadata_path = env.api.stage_root / f"{stage_id}.json"
    image_path = env.api.stage_root / f"{stage_id}.jpg"
    if tamper == "expired":
        metadata = json.loads(metadata_path.read_text())
        metadata["expires_at"] = time.time() - 10
        metadata_path.write_text(json.dumps(metadata))
    elif tamper == "bytes":
        image_path.write_bytes(_png("red"))
    else:
        outside = env.root / "stage-external.jpg"
        outside.write_bytes(image_path.read_bytes())
        image_path.unlink()
        image_path.symlink_to(outside)
    request_id = _id()
    with pytest.raises(ValueError):
        await env.api.action("submit", request_id, {"operation": "import", "category": "outfit", "stage_id": stage_id})
    assert env.service.storage.list_tasks() == []
    assert env.service.storage.get_request(stage_id)["task_id"] is None
    assert env.service.storage.get_request(request_id)["state"] == "failed"
    env.assert_no_models_or_delivery()


async def test_stale_target_version_rejects_replace_without_spending_stage(env: _Environment) -> None:
    asset = env.asset()
    stage_id, _ = await _stage(env)
    args = {
        "operation": "replace",
        "asset_id": asset.id,
        "expected_version": asset.updated_at.isoformat(),
        "stage_id": stage_id,
    }
    changed = env.service.gallery.edit(asset.id, name="new name")
    assert changed.updated_at != asset.updated_at
    request_id = _id()
    with pytest.raises(ValueError, match="已变化"):
        await env.api.action("submit", request_id, args)
    assert env.service.storage.list_tasks() == []
    assert env.service.storage.get_request(stage_id)["task_id"] is None
    assert (env.api.stage_root / f"{stage_id}.jpg").is_file()
    good = await env.api.action("submit", _id(), {**args, "expected_version": changed.updated_at.isoformat()})
    assert env.service.storage.get_task(good["task_id"]).request["expected_version"] == changed.updated_at.isoformat()
    env.assert_no_models_or_delivery()


async def test_person_version_and_confirmation_are_both_required(env: _Environment) -> None:
    person = env.asset("person")
    stage_id, _ = await _stage(env)
    args = {"operation": "import", "category": "person", "stage_id": stage_id, "person_version": "none"}
    with pytest.raises(ValueError, match="人物参考已变化"):
        await env.api.action("submit", _id(), args)
    args["person_version"] = f"{person.id}:{person.updated_at.isoformat()}"
    with pytest.raises(ValueError, match="确认"):
        await env.api.action("submit", _id(), args)
    request_id = _id()
    token = await _confirm(env.api, "submit", args, request_id)
    response = await env.api.action("submit", request_id, args, token)
    assert response["success"]
    assert len(env.service.storage.list_tasks()) == 1
    assert env.service.storage.get_task(response["task_id"]).request["person_version"] == args["person_version"]
    env.assert_no_models_or_delivery()


@pytest.mark.parametrize("change", ["request_id", "name", "stage_id", "batch_id"])
async def test_confirmation_token_binds_exact_submission_id_and_parameters(env: _Environment, change: str) -> None:
    stage_id, _ = await _stage(env)
    args = {"operation": "extract", "category": "outfit", "name": "original", "stage_id": stage_id, "batch_id": _id()}
    request_id = _id()
    token = await _confirm(env.api, "submit", args, request_id)
    submitted_id = _id() if change == "request_id" else request_id
    changed = dict(args)
    if change != "request_id":
        changed[change] = "changed" if change == "name" else _id()
    with pytest.raises(ValueError, match="确认"):
        await env.api.action("submit", submitted_id, changed, token)
    assert env.service.storage.list_tasks() == []
    assert env.service.storage.get_request(stage_id)["task_id"] is None
    env.assert_no_models_or_delivery()


async def test_confirmation_expires_and_is_not_restored_by_adapter_reopen(env: _Environment) -> None:
    stage_id, _ = await _stage(env)
    args = {"operation": "extract", "category": "outfit", "stage_id": stage_id}
    request_id = _id()
    token = await _confirm(env.api, "submit", args, request_id)
    env.api._confirmations[token]["expires_at"] = time.time() - 1
    with pytest.raises(ValueError, match="确认"):
        await env.api.action("submit", request_id, args, token)
    second_id = _id()
    second_token = await _confirm(env.api, "submit", args, second_id)
    env.reopen()
    with pytest.raises(ValueError, match="确认"):
        await env.api.action("submit", second_id, args, second_token)
    assert env.service.storage.list_tasks() == []
    env.assert_no_models_or_delivery()


async def test_batch_confirmation_allows_each_exact_item_once(env: _Environment) -> None:
    batch_id = _id()
    items = []
    for colour in ("red", "blue"):
        stage_id, _ = await _stage(env, _png(colour))
        items.append(
            {
                "request_id": _id(),
                "operation": "extract",
                "category": "outfit",
                "stage_id": stage_id,
                "batch_id": batch_id,
            }
        )
    token = await _confirm(env.api, "batch_submit", {"items": items}, batch_id)
    for item in items:
        params = {key: value for key, value in item.items() if key != "request_id"}
        response = await env.api.action("submit", item["request_id"], params, token)
        assert await env.api.action("submit", item["request_id"], params, token) == response
    assert len(env.service.storage.list_tasks()) == 2
    assert token not in env.api._confirmations
    assert {task.request["batch_id"] for task in env.service.storage.list_tasks()} == {batch_id}
    assert len(env.uploads.calls) == 2
    env.assert_no_models_or_delivery()


@pytest.mark.parametrize("change", ["request_id", "name", "batch_id"])
async def test_batch_confirmation_rejects_extra_or_changed_item(env: _Environment, change: str) -> None:
    stage_id, _ = await _stage(env)
    item = {"request_id": _id(), "operation": "extract", "category": "outfit", "stage_id": stage_id, "batch_id": _id()}
    token = await _confirm(env.api, "batch_submit", {"items": [item]}, _id())
    params = {key: value for key, value in item.items() if key != "request_id"}
    request_id = _id() if change == "request_id" else item["request_id"]
    if change != "request_id":
        params[change] = "unconfirmed name" if change == "name" else _id()
    with pytest.raises(ValueError, match="确认"):
        await env.api.action("submit", request_id, params, token)
    assert env.service.storage.list_tasks() == []
    env.assert_no_models_or_delivery()


@pytest.mark.parametrize("invalid", ["duplicate_ids", "twenty_one_items", "person_batch"])
async def test_invalid_batch_cannot_mint_confirmation(env: _Environment, invalid: str) -> None:
    items = [{"request_id": _id(), "operation": "extract", "category": "outfit"}]
    if invalid == "duplicate_ids":
        items *= 2
    elif invalid == "twenty_one_items":
        items = [{"request_id": _id(), "operation": "extract", "category": "outfit"} for _ in range(21)]
    else:
        items = [
            {"request_id": _id(), "operation": "extract", "category": "person", "person_version": "none"},
            {"request_id": _id(), "operation": "extract", "category": "outfit"},
        ]
    with pytest.raises(ValueError):
        await _confirm(env.api, "batch_submit", {"items": items}, _id())
    assert env.api._confirmations == {}
    assert env.service.storage.list_tasks() == []
    env.assert_no_models_or_delivery()


async def test_partial_delete_keeps_busy_asset_and_replays_stable_result(env: _Environment) -> None:
    idle = env.asset(name="idle")
    busy = env.asset(name="busy")
    env.service.storage.create_task(ImageTask(kind="reference_retag", scope_key="test", request={"asset_id": busy.id}))
    args = {"ids": [idle.id, busy.id], "versions": _versions(idle, busy)}
    request_id = _id()
    token = await _confirm(env.api, "delete", args, request_id)
    response = await env.api.action("delete", request_id, args, token)
    assert response["success"] is False
    assert [(item["asset_id"], item["success"]) for item in response["items"]] == [(idle.id, True), (busy.id, False)]
    assert not idle.source_path.exists() and not idle.reference_path.exists()
    assert env.service.gallery.get(idle.id, include_deleted=True).status == AssetStatus.DELETED
    assert env.service.gallery.get(busy.id).status == AssetStatus.ACTIVE
    assert busy.source_path.exists() and busy.reference_path.exists()
    assert env.service.storage.get_request(request_id)["state"] == "completed"
    assert await env.api.action("delete", request_id, args, token) == response
    _assert_private(response, env)
    env.assert_no_models_or_delivery()


async def test_delete_cleanup_failure_is_partial_and_does_not_undo_tombstone(
    env: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = env.asset(name="broken cleanup")
    good = env.asset(name="good cleanup")
    unlink = Path.unlink

    def denied(path: Path, *args: Any, **kwargs: Any) -> None:
        if path in (broken.source_path, broken.reference_path):
            raise PermissionError(f"{SECRET}: cannot unlink {path}")
        unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)
    args = {"ids": [broken.id, good.id], "versions": _versions(broken, good)}
    request_id = _id()
    token = await _confirm(env.api, "delete", args, request_id)
    response = await env.api.action("delete", request_id, args, token)
    assert response["success"] is False
    assert [item["success"] for item in response["items"]] == [False, True]
    tombstone = env.service.gallery.require(broken.id, include_deleted=True)
    assert tombstone.status == AssetStatus.DELETED and tombstone.deleted_at is not None
    assert tombstone.selection_metadata["cleanup_error"]
    assert broken.source_path.exists() and broken.reference_path.exists()
    assert not good.source_path.exists() and not good.reference_path.exists()
    _assert_private(response, env)
    env.assert_no_models_or_delivery()


@pytest.mark.parametrize("operation", ["cancel", "retry"])
@pytest.mark.parametrize(
    ("kind", "origin", "delivery"),
    [
        ("reference_import", "qq", "qq"),
        ("reference_import", "automatic", "none"),
        ("reference_import", "webui", "qq"),
        ("photo", "webui", "none"),
    ],
)
async def test_non_webui_or_chat_delivery_tasks_cannot_be_cancelled_or_retried(
    env: _Environment, operation: str, kind: str, origin: str, delivery: str
) -> None:
    task = env.service.storage.create_task(
        ImageTask(
            kind=kind,
            scope_key="other-entry",
            status="failed" if operation == "retry" else "queued",
            stream_id="private-stream",
            request={"origin": origin, "delivery": delivery},
        )
    )
    env.service.payloads.put(task.id, {"operation": "import", "category": "outfit"})
    request_id = _id()
    with pytest.raises(ValueError):
        await env.api.action(operation, request_id, {"task_id": task.id}, "cannot-authorize-another-entry")
    assert env.service.storage.get_task(task.id).status == task.status
    assert env.service.storage.get_request(request_id)["state"] == "failed"
    if kind.startswith("reference_"):
        dto = (await env.api.task_query("detail", {"task_id": task.id}))["task"]
        assert not dto["can_cancel"] and not dto["can_retry"]
        _assert_private(dto, env)
    env.assert_no_models_or_delivery()


async def test_gallery_and_task_queries_are_model_free_cache_read_only_and_private(env: _Environment) -> None:
    person = env.asset("person")
    outfit = env.asset(name="summer dress", selection_metadata={"api_key": SECRET, "source_path": str(env.root)})
    scene = env.asset("scene", status="disabled")
    deleted = env.asset(name="removed")
    env.service.gallery.soft_delete(deleted.id)
    task = env.service.storage.create_task(
        ImageTask(
            kind="reference_import",
            scope_key="webui:gallery",
            status="sent",
            request={"operation": "import", "origin": "webui", "delivery": "none", "api_key": SECRET},
            result_path=env.root / "private-output.png",
            error_message=f"{SECRET} at {env.root}",
            result_metadata={"asset_id": outfit.id, "asset_status": "active", "api_key": SECRET},
        )
    )
    env.service.storage.create_task(ImageTask(kind="photo", scope_key="qq"))
    reference = env.service.storage.create_task(
        ImageTask(kind="reference_retag", scope_key="qq", stream_id="private-stream", status="queued")
    )
    before_cache = _cache_snapshot(env.api)
    before_changes = env.service.storage.connection.total_changes
    settings = await env.api.gallery_query("settings")
    assert settings["person"]["asset_id"] == person.id
    assert settings["page_size"] == 12
    gallery = await env.api.gallery_query("list", {"category": "outfit", "search": "summer", "limit": 1})
    assert gallery["total"] == 1 and gallery["items"][0]["id"] == outfit.id
    assert (await env.api.gallery_query("list", {"offset": 1, "limit": 1}))["total"] == 3
    detail = await env.api.gallery_query("detail", {"asset_id": scene.id})
    assert detail["asset"]["status"] == "disabled"
    images = await env.api.gallery_query("images", {"asset_ids": [outfit.id]})
    assert images == {"images": [{"asset_id": outfit.id, "variant": "thumbnail", "ready": False}]}
    tasks = await env.api.task_query("list", {"origin": "webui", "status": "completed", "limit": 1})
    assert tasks["total"] == 1 and tasks["items"][0]["task_id"] == task.id
    assert tasks["items"][0]["status"] == "completed"
    task_detail = await env.api.task_query("detail", {"task_id": reference.id})
    assert task_detail["task"]["origin"] == "qq"
    missing_receipt = await env.api.task_query("receipt", {"request_id": _id()})
    assert missing_receipt == {"receipt": None}
    for response in (settings, gallery, detail, images, tasks, task_detail):
        _assert_private(response, env)
    assert _cache_snapshot(env.api) == before_cache
    assert env.service.storage.connection.total_changes == before_changes
    env.assert_no_models_or_delivery()


async def test_only_explicit_prepare_writes_preview_cache_not_image_query(env: _Environment) -> None:
    asset = env.asset()
    await env.api.action("prepare_preview", _id(), {"asset_ids": [asset.id]})
    before = _cache_snapshot(env.api)
    assert len(before) == 1
    response = await env.api.gallery_query("images", {"asset_ids": [asset.id]})
    assert response["images"][0]["ready"]
    assert response["images"][0]["data_url"].startswith("data:image/jpeg;base64,")
    assert _cache_snapshot(env.api) == before
    _assert_private(response, env)
    env.assert_no_models_or_delivery()


@pytest.mark.parametrize(
    "field", ["stream_id", "user_id", "group_id", "origin", "delivery", "api_key", "path", "personality"]
)
async def test_submit_never_accepts_client_identity_paths_or_model_credentials(env: _Environment, field: str) -> None:
    request_id = _id()
    args = {"operation": "import", "category": "outfit", field: "untrusted"}
    with pytest.raises(ValueError, match="不允许的参数"):
        await env.api.action("submit", request_id, args)
    assert env.service.storage.list_tasks() == []
    assert env.uploads.calls == []
    env.assert_no_models_or_delivery()


async def test_edit_checks_version_and_in_use_task_references_without_models(env: _Environment) -> None:
    asset = env.asset(manual_tags={"styles": ["old"]})
    args = {"asset_id": asset.id, "expected_version": asset.updated_at.isoformat(), "manual_tags": {"styles": ["new"]}}
    edited = await env.api.action("edit", _id(), args)
    assert edited["asset"]["manual_tags"] == {"styles": ["new"]}
    with pytest.raises(RuntimeError, match="已变更"):
        await env.api.action("edit", _id(), args)
    task = env.service.storage.create_task(ImageTask(kind="photo", scope_key="qq"))
    env.service.storage.record_task_reference(TaskReference(task.id, "outfit", asset_id=asset.id))
    with pytest.raises(ValueError, match="生图任务使用"):
        await env.api.action("edit", _id(), {**args, "expected_version": edited["asset"]["updated_at"]})
    _assert_private(edited, env)
    env.assert_no_models_or_delivery()


async def test_submit_receipt_failure_rolls_back_task_and_cleans_uncommitted_queue_files(
    env: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_id, _ = await _stage(env)
    request_id = _id()
    original_update = env.service.storage.update_request

    def fail_acceptance(receipt_id: str, **kwargs: Any) -> dict[str, Any]:
        if receipt_id == request_id and kwargs["state"] == "accepted":
            raise StorageError(f"{SECRET}: failed receipt write at {env.root}")
        return original_update(receipt_id, **kwargs)

    monkeypatch.setattr(env.service.storage, "update_request", fail_acceptance)
    with pytest.raises(StorageError, match="failed receipt write"):
        await env.api.action("submit", request_id, {"operation": "import", "category": "outfit", "stage_id": stage_id})
    assert env.service.storage.list_tasks() == []
    assert env.service.storage.get_request(request_id)["state"] == "failed"
    assert env.service.storage.get_request(stage_id)["task_id"] is None
    assert (env.api.stage_root / f"{stage_id}.jpg").is_file()
    assert list(env.service.payloads.root.glob("*.json")) == []
    assert list(env.service.payloads.upload_root.glob("*.bin")) == []
    _assert_private(env.service.storage.get_request(request_id)["response"], env)
    env.assert_no_models_or_delivery()


async def test_fake_webui_submit_to_completion_commits_asset_task_and_receipt_atomically(
    env: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.llm.replies = [_tags()]
    stage_id, _ = await _stage(env)
    request_id = _id()
    args = {"operation": "import", "category": "outfit", "stage_id": stage_id, "name": "offline imported board"}
    accepted = await env.api.action("submit", request_id, args)
    task_id = accepted["task_id"]
    original_complete = env.service.storage.complete_requests_for_task
    observations = []
    with sqlite3.connect(env.data_dir / "maitu.sqlite3", isolation_level=None) as observer:

        def check_atomic_completion(completed_task_id: str, response: dict[str, Any]) -> None:
            assert env.service.storage.connection.in_transaction
            observations.append(
                {
                    "task_id": completed_task_id,
                    "success": response.get("success"),
                    "external_assets": observer.execute("SELECT COUNT(*) FROM reference_assets").fetchone()[0],
                    "external_receipt": observer.execute(
                        "SELECT state FROM webui_requests WHERE request_id = ?", (request_id,)
                    ).fetchone()[0],
                }
            )
            original_complete(completed_task_id, response)

        monkeypatch.setattr(env.service.storage, "complete_requests_for_task", check_atomic_completion)
        claimed = env.service.storage.claim_next_task(kinds=["reference_import"])
        assert claimed is not None and claimed.id == task_id
        await env.service._handle_task(claimed)
    assert observations
    assert observations[0] == {
        "task_id": task_id,
        "success": True,
        "external_assets": 0,
        "external_receipt": "accepted",
    }
    task = env.service.storage.get_task(task_id)
    receipt = env.service.storage.get_request(request_id)
    assert task.status == TaskStatus.SENT
    assert receipt["state"] == "completed" and receipt["response"]["success"] is True
    assert receipt["response"]["asset_id"] == task.result_metadata["asset_id"]
    assert env.service.gallery.require(receipt["response"]["asset_id"]).status == AssetStatus.ACTIVE
    assert await env.api.action("submit", request_id, args) == receipt["response"]
    assert len(env.llm.calls) == 1 and env.provider.calls == [] and env.delivery.calls == []
    assert not (env.service.payloads.root / f"{task_id}.json").exists()
    assert not (env.service.payloads.upload_root / f"{task_id}.bin").exists()
    response = await env.api.task_query("receipt", {"request_id": request_id})
    _assert_private(response, env)


async def test_completion_receipt_failure_rolls_back_new_asset_and_records_failed_receipt(
    env: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.llm.replies = [_tags()]
    stage_id, _ = await _stage(env)
    request_id = _id()
    accepted = await env.api.action(
        "submit", request_id, {"operation": "import", "category": "outfit", "stage_id": stage_id}
    )
    original_complete = env.service.storage.complete_requests_for_task

    def fail_success(task_id: str, response: dict[str, Any]) -> None:
        assert env.service.storage.connection.in_transaction
        if response.get("success") is True:
            raise StorageError(f"{SECRET} failed completion write {env.root}")
        original_complete(task_id, response)

    monkeypatch.setattr(env.service.storage, "complete_requests_for_task", fail_success)
    claimed = env.service.storage.claim_next_task(kinds=["reference_import"])
    assert claimed is not None
    await env.service._handle_task(claimed)
    assert env.service.storage.get_task(accepted["task_id"]).status == TaskStatus.FAILED
    receipt = env.service.storage.get_request(request_id)
    assert receipt["state"] == "failed" and receipt["response"]["success"] is False
    assert env.service.gallery.list_assets(include_deleted=True) == []
    assert list((env.data_dir / "sources").rglob("*.jpg")) == []
    assert list((env.data_dir / "references").rglob("*.jpg")) == []
    _assert_private(receipt["response"], env)
    assert len(env.llm.calls) == 1 and env.provider.calls == [] and env.delivery.calls == []


async def test_cancel_webui_task_finalizes_original_receipt_and_does_not_requeue(env: _Environment) -> None:
    stage_id, _ = await _stage(env)
    submit_id = _id()
    accepted = await env.api.action(
        "submit", submit_id, {"operation": "import", "category": "outfit", "stage_id": stage_id}
    )
    task_id = accepted["task_id"]
    cancel_id = _id()
    response = await env.api.action("cancel", cancel_id, {"task_id": task_id})
    assert response == {"success": True, "task_id": task_id, "status": "cancelled"}
    assert env.service.storage.get_task(task_id).status == TaskStatus.CANCELLED
    original_receipt = env.service.storage.get_request(submit_id)
    assert original_receipt["state"] == "failed"
    assert original_receipt["response"]["status"] == "cancelled"
    assert env.service.storage.get_request(cancel_id)["state"] == "completed"
    assert await env.api.action("cancel", cancel_id, {"task_id": task_id}) == response
    assert env.service.storage.claim_next_task() is None
    assert not (env.service.payloads.root / f"{task_id}.json").exists()
    assert not (env.service.payloads.upload_root / f"{task_id}.bin").exists()
    _assert_private(original_receipt["response"], env)
    env.assert_no_models_or_delivery()


async def test_cancel_action_receipt_failure_cannot_resurrect_task_after_payload_cleanup(
    env: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_id, _ = await _stage(env)
    submit_id = _id()
    accepted = await env.api.action(
        "submit", submit_id, {"operation": "import", "category": "outfit", "stage_id": stage_id}
    )
    task_id = accepted["task_id"]
    cancel_id = _id()
    original_update = env.service.storage.update_request

    def fail_cancel_receipt(request_id: str, **kwargs: Any) -> dict[str, Any]:
        if request_id == cancel_id and kwargs["state"] == "completed":
            raise StorageError("fake cancellation receipt failure")
        return original_update(request_id, **kwargs)

    monkeypatch.setattr(env.service.storage, "update_request", fail_cancel_receipt)
    with pytest.raises(StorageError, match="cancellation receipt failure"):
        await env.api.action("cancel", cancel_id, {"task_id": task_id})
    assert env.service.storage.get_task(task_id).status == TaskStatus.CANCELLED
    assert env.service.storage.get_request(submit_id)["state"] == "failed"
    assert env.service.storage.get_request(cancel_id)["state"] == "failed"
    assert env.service.storage.claim_next_task() is None
    assert not (env.service.payloads.root / f"{task_id}.json").exists()
    assert not (env.service.payloads.upload_root / f"{task_id}.bin").exists()
    env.assert_no_models_or_delivery()


async def test_missing_upload_disables_retry_without_invoking_models(env: _Environment) -> None:
    stage_id, _ = await _stage(env)
    accepted = await env.api.action(
        "submit", _id(), {"operation": "import", "category": "outfit", "stage_id": stage_id}
    )
    task_id = accepted["task_id"]
    env.service.storage.set_task_status(task_id, TaskStatus.FAILED)
    env.service.payloads.delete_upload(task_id)
    dto = (await env.api.task_query("detail", {"task_id": task_id}))["task"]
    assert dto["can_retry"] is False
    env.assert_no_models_or_delivery()


async def test_confirmed_delete_removes_cached_previews_without_touching_other_assets(env: _Environment) -> None:
    asset = env.asset()
    other = env.asset(name="other")
    for variant in ("thumbnail", "source", "reference"):
        env.api.images.prepare(asset.id, variant)
    env.api.images.prepare(other.id, "thumbnail")
    args = {"ids": [asset.id], "versions": _versions(asset)}
    request_id = _id()
    token = await _confirm(env.api, "delete", args, request_id)
    assert (await env.api.action("delete", request_id, args, token))["success"] is True
    assert list(env.api.images.root.glob(f"{asset.id}-*.jpg")) == []
    assert env.api.images.query([other.id])["images"][0]["ready"] is True
    env.assert_no_models_or_delivery()


async def test_confirmed_retry_reuses_webui_task_once_and_completes_new_receipt(env: _Environment) -> None:
    stage_id, _ = await _stage(env)
    submit_id = _id()
    args = {"operation": "import", "category": "outfit", "stage_id": stage_id}
    accepted = await env.api.action("submit", submit_id, args)
    task_id = accepted["task_id"]
    with env.service.storage.transaction():
        env.service.storage.set_task_status(task_id, TaskStatus.FAILED, error_message="fake initial failure")
        env.service.storage.complete_requests_for_task(task_id, {"success": False, "task_id": task_id})
    original_failed_receipt = env.service.storage.get_request(submit_id)
    dto = (await env.api.task_query("detail", {"task_id": task_id}))["task"]
    assert dto["can_retry"] is True and dto["can_cancel"] is False
    with pytest.raises(ValueError, match="确认"):
        await env.api.action("retry", _id(), {"task_id": task_id})
    retry_id = _id()
    token = await _confirm(env.api, "retry", {"task_id": task_id}, retry_id)
    response = await env.api.action("retry", retry_id, {"task_id": task_id}, token)
    assert response == {"success": True, "task_id": task_id, "status": "queued"}
    assert await env.api.action("retry", retry_id, {"task_id": task_id}, token) == response
    assert len(env.service.storage.list_tasks()) == 1
    assert env.service.storage.get_request(retry_id)["state"] == "accepted"
    assert env.service.storage.get_request(submit_id) == original_failed_receipt
    env.assert_no_models_or_delivery()

    env.llm.replies = [_tags()]
    claimed = env.service.storage.claim_next_task(kinds=["reference_import"])
    assert claimed is not None and claimed.id == task_id
    await env.service._handle_task(claimed)
    assert env.service.storage.get_task(task_id).status == TaskStatus.SENT
    retry_receipt = env.service.storage.get_request(retry_id)
    assert retry_receipt["state"] == "completed"
    assert retry_receipt["response"]["asset_id"]
    assert env.service.storage.get_request(submit_id) == original_failed_receipt
    assert len(env.llm.calls) == 1 and env.provider.calls == [] and env.delivery.calls == []
    _assert_private(retry_receipt["response"], env)


async def test_retry_receipt_write_failure_rolls_back_requeue_and_keeps_retry_payload(
    env: _Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_id, _ = await _stage(env)
    accepted = await env.api.action(
        "submit", _id(), {"operation": "import", "category": "outfit", "stage_id": stage_id}
    )
    task_id = accepted["task_id"]
    env.service.storage.set_task_status(task_id, TaskStatus.FAILED)
    retry_id = _id()
    token = await _confirm(env.api, "retry", {"task_id": task_id}, retry_id)
    original_update = env.service.storage.update_request

    def fail_retry_acceptance(request_id: str, **kwargs: Any) -> dict[str, Any]:
        if request_id == retry_id and kwargs["state"] == "accepted":
            raise StorageError("fake retry receipt failure")
        return original_update(request_id, **kwargs)

    monkeypatch.setattr(env.service.storage, "update_request", fail_retry_acceptance)
    with pytest.raises(StorageError, match="retry receipt failure"):
        await env.api.action("retry", retry_id, {"task_id": task_id}, token)
    assert env.service.storage.get_task(task_id).status == TaskStatus.FAILED
    assert env.service.storage.get_request(retry_id)["state"] == "failed"
    assert env.service.storage.claim_next_task() is None
    assert (env.service.payloads.root / f"{task_id}.json").is_file()
    assert (env.service.payloads.upload_root / f"{task_id}.bin").is_file()
    env.assert_no_models_or_delivery()


async def test_worker_rechecks_target_version_before_any_model_call(env: _Environment) -> None:
    asset = env.asset()
    request_id = _id()
    accepted = await env.api.action(
        "submit",
        request_id,
        {"operation": "retag", "asset_id": asset.id, "expected_version": asset.updated_at.isoformat()},
    )
    changed = env.service.gallery.edit(asset.id, name="changed after queue")
    claimed = env.service.storage.claim_next_task(kinds=["reference_retag"])
    assert claimed is not None
    await env.service._handle_task(claimed)
    assert env.service.storage.get_task(accepted["task_id"]).status == TaskStatus.FAILED
    assert env.service.storage.get_request(request_id)["state"] == "failed"
    assert env.service.gallery.require(asset.id).updated_at == changed.updated_at
    env.assert_no_models_or_delivery()


async def test_worker_rechecks_target_after_fake_model_before_mutation(env: _Environment) -> None:
    asset = env.asset()
    env.llm.replies = [_tags()]
    env.llm.before = lambda: env.service.gallery.edit(asset.id, name="concurrent administrator edit")
    request_id = _id()
    accepted = await env.api.action(
        "submit",
        request_id,
        {"operation": "retag", "asset_id": asset.id, "expected_version": asset.updated_at.isoformat()},
    )
    claimed = env.service.storage.claim_next_task(kinds=["reference_retag"])
    assert claimed is not None
    await env.service._handle_task(claimed)
    assert env.service.storage.get_task(accepted["task_id"]).status == TaskStatus.FAILED
    assert env.service.storage.get_request(request_id)["state"] == "failed"
    current = env.service.gallery.require(asset.id)
    assert current.name == "concurrent administrator edit" and current.tags == asset.tags
    assert len(env.llm.calls) == 1 and env.provider.calls == [] and env.delivery.calls == []


@pytest.mark.parametrize(
    "args", [{"limit": 0}, {"limit": 25}, {"limit": True}, {"offset": -1}, {"offset": 1_000_001}, {"path": "/tmp"}]
)
@pytest.mark.parametrize("query", ["gallery_query", "task_query"])
async def test_query_rejects_invalid_pagination_and_extra_fields(
    env: _Environment, query: str, args: dict[str, Any]
) -> None:
    before = _cache_snapshot(env.api)
    with pytest.raises(ValueError):
        await getattr(env.api, query)("list", args)
    assert _cache_snapshot(env.api) == before
    env.assert_no_models_or_delivery()


async def test_person_generation_uses_host_identity_only_and_defers_fake_image_model_to_worker(
    env: _Environment,
) -> None:
    args = {
        "operation": "generate_person",
        "category": "person",
        "person_version": "none",
        "appearance_hint": "short hair",
    }
    request_id = _id()
    token = await _confirm(env.api, "submit", args, request_id)
    response = await env.api.action("submit", request_id, args, token)
    env.assert_no_models_or_delivery()
    assert env.host_config.calls == ["bot.nickname", "personality.personality"]
    task = env.service.storage.get_task(response["task_id"])
    payload = env.service.payloads.get(task.id)
    assert payload["nickname"] == "fake bot" and payload["personality"] == "quiet and curious"
    assert task.request["origin"] == "webui" and task.request["delivery"] == "none"
    _assert_private(response, env)


async def test_confirmation_does_not_mutate_receipts_and_unknown_preparing_is_not_resubmitted(
    env: _Environment,
) -> None:
    asset = env.asset()
    request_id = _id()
    args = {"operation": "regenerate", "asset_id": asset.id, "expected_version": asset.updated_at.isoformat()}
    before = env.service.storage.connection.total_changes
    await _confirm(env.api, "submit", args, request_id)
    assert env.service.storage.get_request(request_id) is None
    assert env.service.storage.connection.total_changes == before
    signature = hashlib.sha256(
        json.dumps(
            {"operation": "submit", "args": args}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    env.service.storage.begin_request(request_id, "submit", signature)
    with pytest.raises(ValueError, match="尚未确定"):
        await env.api.action("submit", request_id, args)
    assert env.service.storage.list_tasks() == []
    assert env.service.storage.get_request(request_id)["state"] == "preparing"
    env.assert_no_models_or_delivery()


async def test_all_task_types_filters_and_delivery_states_are_visible_read_only(env: _Environment) -> None:
    now = utc_now()
    records = []
    for kind in ("photo", "scene_photo", "image", "reference_import", "notification_retry"):
        records.append(
            env.service.storage.create_task(
                ImageTask(
                    kind=kind,
                    scope_key="private-scope",
                    stream_id="private-stream",
                    status="sent",
                    created_at=now,
                    request={"api_key": SECRET},
                    result_metadata={
                        "image_sent": kind == "photo",
                        "notification_error": SECRET,
                        "private_path": str(env.root),
                    },
                )
            )
        )
    before = env.service.storage.connection.total_changes
    result = await env.api.task_query("list", {"limit": 2})
    assert result["total"] == 5 and len(result["items"]) == 2
    for record in records:
        detail = (await env.api.task_query("detail", {"task_id": record.id}))["task"]
        assert not detail["can_cancel"] and not detail["can_retry"]
        assert detail["status"] == ("sent" if record.kind in {"photo", "scene_photo", "image"} else "completed")
        assert detail["delivery_status"] == ("sent" if record.kind in {"photo", "scene_photo", "image"} else "none")
        _assert_private(detail, env)
    assert (await env.api.task_query("list", {"kind": "scene_photo"}))["total"] == 2
    assert (await env.api.task_query("list", {"status": "sent"}))["total"] == 3
    assert (await env.api.task_query("list", {"status": "completed"}))["total"] == 2
    assert (await env.api.task_query("list", {"task_id": records[0].id[:12]}))["total"] == 1
    assert (
        await env.api.task_query(
            "list",
            {
                "created_after": (now - timedelta(seconds=1)).isoformat(),
                "created_before": (now + timedelta(seconds=1)).isoformat(),
            },
        )
    )["total"] == 5
    assert (await env.api.task_query("list", {"created_after": (now + timedelta(seconds=1)).isoformat()}))["total"] == 0
    with pytest.raises(ValueError):
        await env.api.task_query("list", {"created_after": "invalid"})
    assert env.service.storage.connection.total_changes == before
    env.assert_no_models_or_delivery()


async def test_task_result_preview_has_budget_and_expires_with_original(env: _Environment) -> None:
    source = env.service.results_dir / "synthetic.png"
    source.write_bytes(_png())
    task = env.service.storage.create_task(ImageTask(kind="photo", scope_key="qq", result_path=source))
    assert await env.api.task_query("image", {"task_id": task.id}) == {"ready": False, "available": True}
    await env.api.action("prepare_task_preview", _id(), {"task_id": task.id})
    before = env.service.storage.connection.total_changes
    result = await env.api.task_query("image", {"task_id": task.id})
    assert result["ready"] and result["data_url"].startswith("data:image/jpeg;base64,")
    assert len(json.dumps(result).encode()) < 480_000
    assert env.service.storage.connection.total_changes == before
    source.unlink()
    assert await env.api.task_query("image", {"task_id": task.id}) == {"ready": False, "available": False}
    env.assert_no_models_or_delivery()


async def test_task_preview_rejects_outside_files_and_symlinks(env: _Environment) -> None:
    outside = env.root / "outside.png"
    outside.write_bytes(_png())
    for source in (outside, env.service.results_dir / "symlink.png"):
        if source != outside:
            source.symlink_to(outside)
        task = env.service.storage.create_task(ImageTask(kind="photo", scope_key="qq", result_path=source))
        with pytest.raises(ValueError):
            await env.api.action("prepare_task_preview", _id(), {"task_id": task.id})
    env.assert_no_models_or_delivery()
