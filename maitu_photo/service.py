"""Application service for generation jobs, delivery, and reference jobs."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from .compression import CompressionConfig
from .config import PhotoPluginConfig
from .continuity import ContinuityManager
from .gallery import DuplicateReferenceError, ReferenceGallery, file_sha256
from .llm_adapter import MaiBotLLMAdapter
from .models import (
    ImageTask,
    ReferenceAsset,
    ReferenceCategory,
    TaskReference,
    TaskStatus,
    utc_now,
)
from .prompts import PromptService
from .provider import GeneratedImage, OpenAICompatibleProvider, ProviderError
from .reference_service import (
    ReferencePrompts,
    ReferenceService,
    ReferenceServiceConfig,
)
from .runtime import InvocationContext
from .selection import ReferenceSelector, SelectionResult
from .storage import RecordNotFoundError, SQLiteStorage, StorageError
from .task_manager import TaskManager


class PhotoStudioError(RuntimeError):
    pass


class PermissionDeniedError(PhotoStudioError):
    pass


class TaskAccessError(PhotoStudioError):
    pass


class TaskPayloadStore:
    """Persist active queue payloads separately from retained task metadata."""

    def __init__(self, data_dir: Path | str) -> None:
        self.root = (Path(data_dir) / "queue").resolve()
        self.upload_root = (Path(data_dir) / "uploads").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.upload_root.mkdir(parents=True, exist_ok=True)

    def put(self, task_id: str, payload: Mapping[str, Any]) -> Path:
        path = self._path(self.root, task_id, ".json")
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._atomic_write(path, data)
        return path

    def get(self, task_id: str) -> dict[str, Any]:
        path = self._path(self.root, task_id, ".json")
        if not path.is_file():
            raise PhotoStudioError(f"任务载荷不存在，无法继续: {task_id}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PhotoStudioError(f"任务载荷损坏: {task_id}") from exc
        if not isinstance(value, dict):
            raise PhotoStudioError(f"任务载荷格式无效: {task_id}")
        return value

    def delete(self, task_id: str) -> None:
        self._path(self.root, task_id, ".json").unlink(missing_ok=True)

    def put_upload(self, task_id: str, data: bytes) -> Path:
        if not data:
            raise ValueError("上传图片为空")
        path = self._path(self.upload_root, task_id, ".bin")
        self._atomic_write(path, data)
        return path

    def get_upload(self, task_id: str) -> bytes:
        path = self._path(self.upload_root, task_id, ".bin")
        if not path.is_file():
            raise PhotoStudioError(f"任务上传图片不存在: {task_id}")
        return path.read_bytes()

    def delete_upload(self, task_id: str) -> None:
        self._path(self.upload_root, task_id, ".bin").unlink(missing_ok=True)

    @staticmethod
    def _path(root: Path, task_id: str, suffix: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", task_id):
            raise ValueError("invalid task id")
        path = (root / f"{task_id}{suffix}").resolve()
        path.relative_to(root)
        return path

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class PhotoStudioService:
    """Coordinates persistent tasks without importing MaiBot host internals."""

    def __init__(self, ctx: Any, config: PhotoPluginConfig, data_dir: Path | str) -> None:
        self.ctx = ctx
        self.config = config
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir = (self.data_dir / "results").resolve()
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.storage = SQLiteStorage(self.data_dir / "maitu.sqlite3")
        self.gallery = ReferenceGallery(self.storage)
        self.continuity = ContinuityManager(
            self.storage,
            ttl_hours=config.continuity.ttl_hours,
            timezone_name=config.continuity.timezone,
            same_local_day=config.continuity.same_local_day,
        )
        self.prompts = PromptService(config.prompts)
        self.llm = MaiBotLLMAdapter(ctx)
        self.selector = ReferenceSelector(
            self.gallery,
            self.continuity,
            self.llm,
            self.prompts,
            config,
        )
        self.payloads = TaskPayloadStore(self.data_dir)
        self._provider: OpenAICompatibleProvider | None = None
        self._reference_scan_task: asyncio.Task[dict[str, int]] | None = None
        self.tasks = TaskManager(
            self.storage,
            self._handle_task,
            worker_count=config.tasks.worker_count,
            poll_interval=config.tasks.poll_interval_seconds,
            max_queue_size=config.tasks.max_queue_size,
            logger=ctx.logger,
        )

    async def start(self) -> None:
        self.cleanup_expired()
        await self.tasks.start()
        if self._reference_scan_task is None:
            self._reference_scan_task = asyncio.create_task(
                self._run_startup_reference_scan(),
                name="maitu-reference-startup-scan",
            )

    async def close(self) -> None:
        scan_task = self._reference_scan_task
        self._reference_scan_task = None
        if scan_task is not None:
            if not scan_task.done():
                scan_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scan_task
        await self.tasks.stop()
        if self._provider is not None:
            await self._provider.aclose()
            self._provider = None
        self.storage.close()

    async def wait_for_startup_reference_scan(self) -> dict[str, int]:
        """Wait for the one-shot startup gallery scan, primarily for diagnostics."""

        task = self._reference_scan_task
        if task is None:
            return self._empty_reference_scan_result()
        return await asyncio.shield(task)

    async def scan_reference_folders(self) -> dict[str, int]:
        """Import unregistered files dropped directly into gallery folders.

        The managed UUID-named artifacts already recorded in SQLite are left
        untouched. Successful imports are copied through the normal compressed
        ingestion path and the original drop file is then removed.
        """

        result = self._empty_reference_scan_result()
        registered_paths = self._registered_reference_paths()
        reference_service = self._reference_service(require_provider=False)

        for category in ReferenceCategory:
            directory = (self.data_dir / "references" / category.value).resolve()
            directory.mkdir(parents=True, exist_ok=True)
            try:
                candidates = sorted(directory.iterdir(), key=lambda path: path.name.casefold())
            except OSError as exc:
                result["errors"] += 1
                self.ctx.logger.warning(
                    "Reference startup scan could not list %s: %s",
                    directory,
                    _safe_error(exc),
                )
                continue

            for candidate in candidates:
                if self._skip_reference_scan_candidate(candidate, registered_paths):
                    result["skipped"] += 1
                    continue
                result["scanned"] += 1

                if category == ReferenceCategory.PERSON:
                    current = self.gallery.get_person()
                    if current is not None:
                        result["person_conflicts"] += 1
                        self.ctx.logger.warning(
                            "Reference startup scan kept %s because person asset %s already exists",
                            candidate.name,
                            current.id,
                        )
                        continue

                try:
                    raw_digest = await asyncio.to_thread(file_sha256, candidate)
                    existing = self.storage.find_reference_by_hash(category, raw_digest)
                    if existing is not None:
                        candidate.unlink(missing_ok=True)
                        result["duplicates"] += 1
                        self.ctx.logger.info(
                            "Reference startup scan removed duplicate %s (asset=%s)",
                            candidate.name,
                            existing.id,
                        )
                        continue

                    asset = await reference_service.import_reference(
                        category,
                        candidate,
                        name=candidate.stem.strip() or f"startup-{category.value}",
                    )
                    if candidate.resolve() != asset.reference_path.resolve():
                        candidate.unlink(missing_ok=True)
                    registered_paths.add(asset.reference_path.resolve())
                    result["imported"] += 1
                    self.ctx.logger.info(
                        "Reference startup scan imported %s as %s/%s (%s)",
                        candidate.name,
                        category.value,
                        asset.id,
                        asset.status.value,
                    )
                except DuplicateReferenceError as exc:
                    if category == ReferenceCategory.PERSON:
                        result["person_conflicts"] += 1
                        self.ctx.logger.warning(
                            "Reference startup scan kept %s because a person reference already exists",
                            candidate.name,
                        )
                    else:
                        try:
                            candidate.unlink(missing_ok=True)
                        except OSError as unlink_exc:
                            result["errors"] += 1
                            self.ctx.logger.warning(
                                "Reference startup scan could not remove duplicate %s: %s",
                                candidate.name,
                                _safe_error(unlink_exc),
                            )
                            continue
                        result["duplicates"] += 1
                        self.ctx.logger.info(
                            "Reference startup scan removed duplicate %s (asset=%s)",
                            candidate.name,
                            exc.existing_id or "unknown",
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one bad drop must not stop startup
                    result["errors"] += 1
                    self.ctx.logger.warning(
                        "Reference startup scan could not import %s/%s: %s",
                        category.value,
                        candidate.name,
                        _safe_error(exc),
                    )

        return result

    async def _run_startup_reference_scan(self) -> dict[str, int]:
        try:
            result = await self.scan_reference_folders()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - startup must survive scanner failure
            self.ctx.logger.error("Reference startup scan failed: %s", _safe_error(exc))
            result = self._empty_reference_scan_result()
            result["errors"] = 1
        self.ctx.logger.info(
            "Reference startup scan complete: scanned=%s imported=%s duplicates=%s person_conflicts=%s errors=%s",
            result["scanned"],
            result["imported"],
            result["duplicates"],
            result["person_conflicts"],
            result["errors"],
        )
        return result

    def _registered_reference_paths(self) -> set[Path]:
        paths: set[Path] = set()
        offset = 0
        while True:
            assets = self.storage.list_reference_assets(
                include_deleted=True,
                limit=10_000,
                offset=offset,
            )
            for asset in assets:
                reference_path = Path(asset.reference_path)
                if not reference_path.is_absolute():
                    reference_path = self.data_dir / reference_path
                paths.add(reference_path.resolve())
            if len(assets) < 10_000:
                break
            offset += len(assets)
        return paths

    @staticmethod
    def _skip_reference_scan_candidate(candidate: Path, registered_paths: set[Path]) -> bool:
        try:
            name = candidate.name.casefold()
            if name.startswith(".") or name.endswith((".tmp", ".part", ".crdownload")):
                return True
            if candidate.is_symlink() or not candidate.is_file():
                return True
            return candidate.resolve() in registered_paths
        except OSError:
            return True

    @staticmethod
    def _empty_reference_scan_result() -> dict[str, int]:
        return {
            "scanned": 0,
            "imported": 0,
            "duplicates": 0,
            "person_conflicts": 0,
            "skipped": 0,
            "errors": 0,
        }

    def submit_scene_photo(
        self,
        invocation: InvocationContext,
        *,
        description: str,
        scene_hint: str = "",
        scene_id: str = "",
        use_scene_reference: bool | None = None,
        force_new_scene: bool = False,
        size: str = "",
        model_id: str = "",
    ) -> ImageTask:
        """Queue a phone-like photo that must not include the person reference."""

        if not description.strip():
            raise ValueError("description 不能为空")
        payload = {
            "description": description.strip(),
            "scene_hint": scene_hint.strip(),
            "scene_id": scene_id.strip(),
            "use_scene_reference": use_scene_reference,
            "force_new_scene": bool(force_new_scene),
            "size": size.strip(),
            "model_id": model_id.strip(),
        }
        return self._submit_generation("scene_photo", invocation, payload, description)

    # Backward-compatible alias used by older tests and local scripts.
    def submit_image(
        self,
        invocation: InvocationContext,
        *,
        prompt: str,
        negative_prompt: str = "",
        size: str = "",
        model_id: str = "",
        scene_hint: str = "",
        scene_id: str = "",
        use_scene_reference: bool | None = None,
        force_new_scene: bool = False,
    ) -> ImageTask:
        del negative_prompt  # scene photos always use the shared negative prompt config
        return self.submit_scene_photo(
            invocation,
            description=prompt,
            scene_hint=scene_hint,
            scene_id=scene_id,
            use_scene_reference=use_scene_reference,
            force_new_scene=force_new_scene,
            size=size,
            model_id=model_id,
        )

    def submit_photo(
        self,
        invocation: InvocationContext,
        *,
        description: str,
        outfit_hint: str = "",
        scene_hint: str = "",
        accessory_hint: str = "",
        outfit_id: str = "",
        scene_id: str = "",
        use_person_reference: bool | None = None,
        use_outfit_reference: bool | None = None,
        use_scene_reference: bool | None = None,
        force_new_outfit: bool = False,
        force_new_scene: bool = False,
        size: str = "",
        model_id: str = "",
    ) -> ImageTask:
        if not description.strip():
            raise ValueError("description 不能为空")
        # Validate the caller's opt-out against the current config before the
        # task is enqueued so the planner gets an actionable error immediately.
        self._validate_photo_person_config(use_person_reference)
        # The startup folder scan is intentionally asynchronous.  Defer the
        # asset-existence check until the worker if it is still in flight;
        # otherwise a just-uploaded person board could be rejected during the
        # brief plugin startup window.
        if self._person_reference_required(use_person_reference) and not self._startup_reference_scan_pending():
            self._validate_photo_person_requirement(use_person_reference)
        payload = {
            "description": description.strip(),
            "outfit_hint": outfit_hint.strip(),
            "scene_hint": scene_hint.strip(),
            "accessory_hint": accessory_hint.strip(),
            "outfit_id": outfit_id.strip(),
            "scene_id": scene_id.strip(),
            "use_person_reference": use_person_reference,
            "use_outfit_reference": use_outfit_reference,
            "use_scene_reference": use_scene_reference,
            "force_new_outfit": bool(force_new_outfit),
            "force_new_scene": bool(force_new_scene),
            "size": size.strip(),
            "model_id": model_id.strip(),
        }
        return self._submit_generation("photo", invocation, payload, description)

    def submit_reference_job(
        self,
        invocation: InvocationContext,
        *,
        operation: str,
        category: ReferenceCategory | str | None = None,
        name: str = "",
        image: bytes | None = None,
        asset_id: str = "",
        manual_tags: Mapping[str, Any] | None = None,
        parent_task_id: str | None = None,
        automatic: bool = False,
    ) -> ImageTask:
        operation = operation.strip().casefold()
        if operation not in {"extract", "import", "retag", "regenerate", "replace"}:
            raise ValueError(f"不支持的参考图任务: {operation}")
        category_value = ReferenceCategory(category).value if category is not None else ""
        task = self._new_task(
            kind=f"reference_{operation}",
            invocation=invocation,
            prompt_text=name or asset_id or operation,
            request={
                "operation": operation,
                "category": category_value,
                "automatic": bool(automatic),
            },
            parent_task_id=parent_task_id,
        )
        payload = {
            "operation": operation,
            "category": category_value,
            "name": name.strip() or f"{category_value}-{task.id[:8]}",
            "asset_id": asset_id.strip(),
            "manual_tags": dict(manual_tags or {}),
            "automatic": bool(automatic),
        }
        try:
            if image is not None:
                self.payloads.put_upload(task.id, image)
                payload["has_upload"] = True
            self.payloads.put(task.id, payload)
            return self.tasks.submit(task)
        except Exception:
            self.payloads.delete(task.id)
            self.payloads.delete_upload(task.id)
            raise

    def get_task_for(
        self,
        invocation: InvocationContext,
        task_id: str = "",
        *,
        is_admin: bool = False,
    ) -> ImageTask | None:
        task = (
            self.storage.get_task(task_id.strip())
            if task_id.strip()
            else self.storage.latest_task(invocation.scope_key)
        )
        if task is not None and task.scope_key != invocation.scope_key and not is_admin:
            raise TaskAccessError("不能查看其他聊天的任务")
        return task

    def task_result(
        self,
        invocation: InvocationContext,
        task_id: str = "",
        *,
        include_image: bool = False,
        is_admin: bool = False,
    ) -> dict[str, Any]:
        task = self.get_task_for(invocation, task_id, is_admin=is_admin)
        if task is None:
            return {"success": False, "content": "当前聊天没有图片任务"}
        references = self.storage.list_task_references(task.id)
        result: dict[str, Any] = {
            "success": True,
            "task_id": task.id,
            "kind": task.kind,
            "status": task.status.value,
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat(),
            "error": task.error_message,
            "references": [
                {
                    "role": item.role,
                    "asset_id": item.asset_id,
                    "source": item.selection_source,
                    "fallback_reason": item.fallback_reason,
                }
                for item in references
            ],
        }
        result["content"] = f"任务 {task.id} 当前状态：{task.status.value}"
        if include_image:
            if not self.config.output.include_image_in_status:
                raise TaskAccessError("配置已禁止状态工具返回图片")
            if task.result_path and task.result_path.is_file():
                data = task.result_path.read_bytes()
                result["content_items"] = [
                    {
                        "type": "image",
                        "data": base64.b64encode(data).decode("ascii"),
                        "mime_type": str(task.result_metadata.get("media_type") or "image/jpeg"),
                        "name": task.result_path.name,
                        "description": f"任务 {task.id} 的生成结果",
                    }
                ]
        return result

    def retry_task(self, task_id: str) -> ImageTask:
        task = self.storage.get_task(task_id)
        if task is None:
            raise RecordNotFoundError(f"task not found: {task_id}")
        if task.status not in {TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.SENT}:
            raise StorageError("只有失败、取消或通知未完成的任务可以重试")
        if task.status == TaskStatus.SENT:
            if task.planner_notified_at is not None:
                raise StorageError("任务已经完成且 Planner 已收到通知")
            task.result_metadata["notification_retry"] = True
        elif task.result_path is None and not (self.payloads.root / f"{task.id}.json").is_file():
            raise StorageError("任务载荷已清理，无法重新发起生成")
        task.status = TaskStatus.QUEUED
        task.error_message = None
        task.paid_request_started = False
        self.storage.update_task(task)
        self.tasks.wake()
        return task

    def cancel_task(self, task_id: str) -> ImageTask:
        task = self.storage.get_task(task_id)
        if task is None:
            raise RecordNotFoundError(f"task not found: {task_id}")
        if task.terminal:
            raise StorageError("任务已经结束")
        task.status = TaskStatus.CANCELLED
        task.error_message = "cancelled by administrator"
        self.storage.update_task(task)
        self.payloads.delete(task.id)
        self.payloads.delete_upload(task.id)
        return task

    def cleanup_expired(self, *, now: datetime | None = None) -> dict[str, int]:
        """Apply result and metadata retention without touching gallery files."""

        current = now or utc_now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        result_before = current - timedelta(hours=self.config.tasks.result_retention_hours)
        metadata_before = current - timedelta(days=self.config.tasks.metadata_retention_days)
        cleaned_results = 0
        deleted_tasks = 0
        tasks = self.storage.list_tasks(limit=10_000)
        for task in tasks:
            children = self.storage.list_tasks(parent_task_id=task.id, limit=10_000)
            children_finished = all(child.terminal for child in children)
            if task.terminal and task.updated_at <= metadata_before and children_finished:
                self.payloads.delete(task.id)
                self.payloads.delete_upload(task.id)
                if task.result_path is not None:
                    task.result_path.unlink(missing_ok=True)
                if self.storage.delete_task(task.id):
                    deleted_tasks += 1
                continue
            if (
                task.status == TaskStatus.SENT
                and task.result_path is not None
                and task.updated_at <= result_before
                and children_finished
            ):
                task.result_path.unlink(missing_ok=True)
                task.result_path = None
                task.result_metadata["result_cleaned_at"] = current.isoformat()
                self.storage.update_task(task)
                cleaned_results += 1
        return {"cleaned_results": cleaned_results, "deleted_tasks": deleted_tasks}

    def _submit_generation(
        self,
        kind: str,
        invocation: InvocationContext,
        payload: Mapping[str, Any],
        prompt_text: str,
    ) -> ImageTask:
        task = self._new_task(kind=kind, invocation=invocation, prompt_text=prompt_text)
        try:
            self.payloads.put(task.id, payload)
            return self.tasks.submit(task)
        except Exception:
            self.payloads.delete(task.id)
            raise

    def _new_task(
        self,
        *,
        kind: str,
        invocation: InvocationContext,
        prompt_text: str,
        request: Mapping[str, Any] | None = None,
        parent_task_id: str | None = None,
    ) -> ImageTask:
        prompt = str(prompt_text or "").strip()
        return ImageTask(
            kind=kind,
            scope_key=invocation.scope_key,
            user_id=invocation.user_id or None,
            stream_id=invocation.stream_id,
            group_id=invocation.group_id,
            prompt_summary=_prompt_summary(prompt),
            prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            prompt_version=self._prompt_version(kind),
            request=dict(request or {}),
            parent_task_id=parent_task_id,
        )

    async def _handle_task(self, claimed: ImageTask) -> None:
        task = self.storage.get_task(claimed.id)
        if task is None or task.status == TaskStatus.CANCELLED:
            return
        try:
            if task.kind == "notification_retry" or task.result_metadata.get("notification_retry"):
                try:
                    await self._notify_success(task)
                    self.storage.set_task_status(task.id, TaskStatus.SENT)
                except Exception as exc:
                    self._merge_task_metadata(task.id, notification_error=_safe_error(exc))
                    self.storage.set_task_status(task.id, TaskStatus.SENT)
                latest = self.storage.get_task(task.id)
                if latest is not None:
                    latest.result_metadata.pop("notification_retry", None)
                    self.storage.update_task(latest)
                return
            if task.kind in {"image", "scene_photo", "photo"}:
                if task.result_path and task.result_path.is_file():
                    await self._deliver(task)
                elif task.kind in {"image", "scene_photo"}:
                    # Legacy "image" queue rows are treated as scene photos.
                    await self._generate_scene_photo(task)
                else:
                    await self._generate_photo(task)
                return
            if task.kind.startswith("reference_"):
                await self._process_reference_task(task)
                return
            raise PhotoStudioError(f"未知任务类型: {task.kind}")
        except Exception as exc:
            latest = self.storage.get_task(task.id)
            if latest is not None and latest.status != TaskStatus.CANCELLED:
                error = _safe_error(exc)
                self.storage.set_task_status(task.id, TaskStatus.FAILED, error_message=error)
                if task.kind in {"image", "scene_photo", "photo", "notification_retry"}:
                    await self._notify_failure(task.id, error)
            raise

    async def _generate_scene_photo(self, task: ImageTask) -> None:
        """Generate a phone-like photo without person or outfit references."""

        payload = self.payloads.get(task.id)
        await self.wait_for_startup_reference_scan()
        use_scene = _optional_bool(payload.get("use_scene_reference"), self.config.references.scene_reference_enabled)
        description = str(payload.get("description") or payload.get("prompt") or "")
        scene_hint = str(payload.get("scene_hint") or "")
        selection = await self.selector.select(
            scope_key=task.scope_key,
            description=description,
            outfit_hint="",
            scene_hint=scene_hint,
            explicit_outfit_id="",
            explicit_scene_id=str(payload.get("scene_id") or ""),
            force_new_outfit=True,
            force_new_scene=bool(payload.get("force_new_scene", False)),
            allow_outfit=False,
            allow_scene=use_scene,
        )
        self._record_scene_photo_references(task.id, selection)
        references: list[bytes] = []
        if selection.scene is not None:
            references.append(selection.scene.reference_path.read_bytes())
        scene_prompt = self._scene_prompt(selection.scene, scene_hint)
        reference_labels = json.dumps(
            {
                "person": None,
                "outfit": None,
                "scene": _reference_label(selection.scene),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        user_prompt = self.prompts.render(
            "scene_photo_user",
            description=description,
            scene_prompt=scene_prompt,
            reference_labels=reference_labels,
            negative_prompt=self.config.prompts.negative_prompt,
        )
        prompt = f"{self.config.prompts.scene_photo_system.strip()}\n\n{user_prompt}".strip()
        self.storage.mark_task_request_started(task.id)
        generated = await self._provider_instance().generate(
            prompt,
            images=references or None,
            model=str(payload.get("model_id") or "") or None,
            size=str(payload.get("size") or "") or None,
            negative_prompt=self.config.prompts.negative_prompt or None,
            mode=self.config.openai.generation_mode,
        )
        if self._task_cancelled(task.id):
            self.payloads.delete(task.id)
            return
        metadata = {
            "generation": "scene_photo",
            "person_id": None,
            "outfit_id": None,
            "scene_id": selection.scene.id if selection.scene else None,
            "selection_reasons": selection.reasons,
            "scene_signature": selection.scene_signature,
            "scene_eligible": selection.scene_eligible,
        }
        result_path = self._save_result(task.id, generated)
        self.storage.set_task_status(
            task.id,
            TaskStatus.GENERATED,
            result_path=result_path,
            result_metadata={**metadata, "media_type": generated.media_type},
        )
        if selection.scene is not None:
            self.gallery.record_usage([selection.scene.id])
        if selection.scene_signature:
            self.continuity.record_photo(
                task.scope_key,
                selection.scene_signature,
                outfit_id=None,
                scene_id=selection.scene.id if selection.scene else None,
                metadata={"task_id": task.id, "generation": "scene_photo"},
            )
        self.payloads.delete(task.id)
        # Scene-only photos never backfill outfit/person references.
        await self._deliver(self.storage.get_task(task.id) or task)

    async def _generate_photo(self, task: ImageTask) -> None:
        payload = self.payloads.get(task.id)
        # Re-check after a task has waited in the queue.  An administrator may
        # disable or replace the singleton while an earlier task is queued.
        await self.wait_for_startup_reference_scan()
        use_person = self._person_reference_required(payload.get("use_person_reference"))
        if use_person:
            person = self._validate_photo_person_requirement(payload.get("use_person_reference"))
        else:
            person = None
        use_outfit = _optional_bool(
            payload.get("use_outfit_reference"), self.config.references.outfit_reference_enabled
        )
        use_scene = _optional_bool(payload.get("use_scene_reference"), self.config.references.scene_reference_enabled)
        selection = await self.selector.select(
            scope_key=task.scope_key,
            description=str(payload.get("description") or ""),
            outfit_hint=str(payload.get("outfit_hint") or ""),
            scene_hint=str(payload.get("scene_hint") or ""),
            explicit_outfit_id=str(payload.get("outfit_id") or ""),
            explicit_scene_id=str(payload.get("scene_id") or ""),
            force_new_outfit=bool(payload.get("force_new_outfit", False)),
            force_new_scene=bool(payload.get("force_new_scene", False)),
            allow_outfit=use_outfit,
            allow_scene=use_scene,
        )
        self._record_photo_references(task.id, person, selection, use_person)
        # Provider role order is fixed as [person?, outfit?, scene?].  Person
        # stays first whenever present so identity-preserving models keep a
        # stable multi-image contract.
        references: list[bytes] = []
        if person is not None:
            references.append(person.reference_path.read_bytes())
        if selection.outfit is not None:
            references.append(selection.outfit.reference_path.read_bytes())
        if selection.scene is not None:
            references.append(selection.scene.reference_path.read_bytes())
        person_prompt = self._person_prompt(person)
        outfit_prompt = self._outfit_prompt(selection.outfit, str(payload.get("outfit_hint") or ""))
        scene_prompt = self._scene_prompt(selection.scene, str(payload.get("scene_hint") or ""))
        accessory_hint = str(payload.get("accessory_hint") or "").strip()
        if accessory_hint:
            person_prompt = f"{person_prompt}\n配饰要求：{accessory_hint}"
        reference_labels = json.dumps(
            {
                "person": _reference_label(person),
                "outfit": _reference_label(selection.outfit),
                "scene": _reference_label(selection.scene),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        user_prompt = self.prompts.render(
            "photo_user",
            description=str(payload.get("description") or ""),
            person_prompt=person_prompt,
            outfit_prompt=outfit_prompt,
            scene_prompt=scene_prompt,
            reference_labels=reference_labels,
            negative_prompt=self.config.prompts.negative_prompt,
        )
        prompt = f"{self.config.prompts.photo_system.strip()}\n\n{user_prompt}".strip()
        self.storage.mark_task_request_started(task.id)
        generated = await self._provider_instance().generate(
            prompt,
            images=references or None,
            model=str(payload.get("model_id") or "") or None,
            size=str(payload.get("size") or "") or None,
            negative_prompt=self.config.prompts.negative_prompt or None,
            mode=self.config.openai.generation_mode,
        )
        if self._task_cancelled(task.id):
            self.payloads.delete(task.id)
            return
        metadata = {
            "generation": "photo",
            "person_id": person.id if person else None,
            "outfit_id": selection.outfit.id if selection.outfit else None,
            "scene_id": selection.scene.id if selection.scene else None,
            "selection_reasons": selection.reasons,
            "scene_signature": selection.scene_signature,
            "scene_eligible": selection.scene_eligible,
            "person_reference_used": person is not None,
        }
        result_path = self._save_result(task.id, generated)
        self.storage.set_task_status(
            task.id,
            TaskStatus.GENERATED,
            result_path=result_path,
            result_metadata={**metadata, "media_type": generated.media_type},
        )
        used = [asset.id for asset in (person, selection.outfit, selection.scene) if asset is not None]
        self.gallery.record_usage(used)
        self.continuity.record_photo(
            task.scope_key,
            selection.scene_signature,
            outfit_id=selection.outfit.id if selection.outfit else None,
            scene_id=selection.scene.id if selection.scene else None,
            metadata={"task_id": task.id},
        )
        self.payloads.delete(task.id)
        self._schedule_backfill_tasks(task, payload, selection, result_path, use_outfit, use_scene)
        await self._deliver(self.storage.get_task(task.id) or task)

    async def _persist_and_deliver(
        self,
        task: ImageTask,
        generated: GeneratedImage,
        metadata: Mapping[str, Any],
    ) -> None:
        result_path = self._save_result(task.id, generated)
        self.storage.set_task_status(
            task.id,
            TaskStatus.GENERATED,
            result_path=result_path,
            result_metadata={**dict(metadata), "media_type": generated.media_type},
        )
        self.payloads.delete(task.id)
        await self._deliver(self.storage.get_task(task.id) or task)

    async def _deliver(self, task: ImageTask) -> None:
        if task.result_path is None or not task.result_path.is_file():
            raise PhotoStudioError("生成结果文件不存在")
        latest = self.storage.get_task(task.id)
        if latest is not None and latest.status == TaskStatus.CANCELLED:
            return
        if latest is not None and latest.result_metadata.get("image_sent"):
            if latest.status != TaskStatus.SENT:
                latest = self.storage.set_task_status(task.id, TaskStatus.SENT)
            try:
                await self._notify_success(latest)
            except Exception as exc:
                self._merge_task_metadata(task.id, notification_error=_safe_error(exc))
            return
        image_base64 = base64.b64encode(task.result_path.read_bytes()).decode("ascii")
        response = await self.ctx.send.image(image_base64, task.stream_id or "", return_details=True)
        sent = bool(response.get("sent")) if isinstance(response, Mapping) else bool(response)
        message_id = str(response.get("message_id") or "") if isinstance(response, Mapping) else ""
        if not sent:
            self._merge_task_metadata(task.id, image_sent=False, delivery_failed=True)
            raise PhotoStudioError("图片投递失败")
        self._merge_task_metadata(
            task.id,
            image_sent=True,
            delivery_failed=False,
            platform_message_id=message_id or None,
        )
        self.storage.set_task_status(task.id, TaskStatus.SENT)
        try:
            await self._notify_success(self.storage.get_task(task.id) or task)
        except Exception as exc:
            self._merge_task_metadata(task.id, notification_error=_safe_error(exc))

    async def _notify_success(self, task: ImageTask) -> None:
        latest = self.storage.get_task(task.id) or task
        if latest.planner_notified_at is not None or not self.config.output.notify_planner:
            return
        references = self.storage.list_task_references(task.id)
        delivered_at = latest.sent_at or utc_now()
        reference_text = ",".join(f"{item.role}:{item.asset_id or 'text'}" for item in references)
        visible = (
            f"写真任务已发送：task_id={task.id}，类型={task.kind}，"
            f"参考图={reference_text}，"
            f"结果摘要={latest.prompt_summary}，投递时间={delivered_at.isoformat()}"
        )
        metadata = latest.result_metadata
        if not metadata.get("planner_context_appended"):
            append_result = await self.ctx.maisaka.context.append(
                stream_id=latest.stream_id or "",
                segments=[{"type": "text", "content": visible}],
                visible_text=visible,
                source_kind="plugin:maitu.photo-studio",
                message_id=f"maitu:{task.id}:sent",
            )
            _require_capability_success(append_result, "planner context append")
            self._merge_task_metadata(task.id, planner_context_appended=True)
        intent = self.prompts.render("planner_success", task_id=task.id)
        trigger_result = await self.ctx.maisaka.proactive.trigger(
            stream_id=latest.stream_id or "",
            intent=intent,
            reason="maitu_image_sent",
            priority=self.config.output.notification_priority,
            metadata={"plugin_id": "maitu.photo-studio", "task_id": task.id, "kind": task.kind},
        )
        _require_capability_success(trigger_result, "planner proactive trigger")
        self.storage.mark_task_planner_notified(task.id)

    async def _notify_failure(self, task_id: str, error: str) -> None:
        task = self.storage.get_task(task_id)
        if task is None or not task.stream_id or not self.config.output.notify_planner:
            return
        metadata = task.result_metadata
        try:
            visible = f"写真任务失败：task_id={task.id}，类型={task.kind}，错误={error}"
            if not metadata.get("failure_context_appended"):
                append_result = await self.ctx.maisaka.context.append(
                    stream_id=task.stream_id,
                    segments=[{"type": "text", "content": visible}],
                    visible_text=visible,
                    source_kind="plugin:maitu.photo-studio",
                    message_id=f"maitu:{task.id}:failed",
                )
                _require_capability_success(append_result, "planner failure context append")
                self._merge_task_metadata(task.id, failure_context_appended=True)
            intent = self.prompts.render("planner_failure", task_id=task.id, error=error)
            trigger_result = await self.ctx.maisaka.proactive.trigger(
                stream_id=task.stream_id,
                intent=intent,
                reason="maitu_image_failed",
                priority=self.config.output.notification_priority,
                metadata={"plugin_id": "maitu.photo-studio", "task_id": task.id, "kind": task.kind},
            )
            _require_capability_success(trigger_result, "planner failure proactive trigger")
            self.storage.mark_task_planner_notified(task.id)
        except Exception as exc:
            self._merge_task_metadata(task.id, notification_error=_safe_error(exc))

    async def _process_reference_task(self, task: ImageTask) -> None:
        payload = self.payloads.get(task.id)
        operation = str(payload.get("operation") or "")
        category_text = str(payload.get("category") or "")
        category = ReferenceCategory(category_text) if category_text else None
        # Import and retag only need the MaiBot tagging model.  Avoid forcing
        # an OpenAI image provider for already-prepared uploads, so gallery
        # maintenance remains usable before generation credentials are set.
        service = self._reference_service(require_provider=operation in {"extract", "regenerate"})
        asset: ReferenceAsset
        if operation in {"extract", "import", "replace"}:
            if category is None:
                raise ValueError("参考图任务缺少 category")
            source = self.payloads.get_upload(task.id)
            old_id = str(payload.get("asset_id") or "")
            replace_person = category == ReferenceCategory.PERSON
            if operation == "extract":
                asset = await service.extract_reference(
                    category,
                    source,
                    name=str(payload.get("name") or ""),
                    replace_person=replace_person,
                    manual_tags=_mapping(payload.get("manual_tags")),
                    source_task_id=task.id,
                )
            else:
                asset = await service.import_reference(
                    category,
                    source,
                    name=str(payload.get("name") or ""),
                    replace_person=replace_person,
                    manual_tags=_mapping(payload.get("manual_tags")),
                    source_task_id=task.id,
                )
            if operation == "replace" and old_id and category != ReferenceCategory.PERSON:
                old = self.gallery.require(old_id)
                if old.category != category:
                    raise ValueError("替换参考图分类不一致")
                self.gallery.soft_delete(old.id)
        elif operation == "retag":
            asset = await service.retag_reference(str(payload.get("asset_id") or ""))
        elif operation == "regenerate":
            asset = await service.regenerate_reference(str(payload.get("asset_id") or ""), source_task_id=task.id)
        else:
            raise ValueError(f"未知参考图任务操作: {operation}")
        self.storage.set_task_status(
            task.id,
            TaskStatus.SENT,
            result_metadata={
                "completed": True,
                "asset_id": asset.id,
                "asset_category": asset.category.value,
                "asset_status": asset.status.value,
                "delivery_required": False,
            },
        )
        self.payloads.delete(task.id)
        self.payloads.delete_upload(task.id)
        if not bool(payload.get("automatic")) and task.stream_id:
            try:
                sent = await self.ctx.send.text(
                    f"参考图任务 {task.id} 已完成：{asset.category.value}/{asset.id}，状态 {asset.status.value}",
                    task.stream_id,
                )
                self._merge_task_metadata(task.id, admin_notification_sent=bool(sent))
            except Exception as exc:
                self._merge_task_metadata(task.id, admin_notification_error=_safe_error(exc))

    def _reference_service(self, *, require_provider: bool = True) -> ReferenceService:
        compression = CompressionConfig(
            target_bytes=min(int(self.config.references.max_bytes), 480_000),
            max_edge=int(self.config.references.max_edge),
            max_pixels=int(self.config.references.max_pixels),
        )
        prompts = ReferencePrompts(
            extract_person=self.config.prompts.extract_person,
            extract_outfit=self.config.prompts.extract_outfit,
            extract_scene=self.config.prompts.extract_scene,
            tag_person=self.prompts.render("tag_person"),
            tag_outfit=self.prompts.render("tag_outfit"),
            tag_scene=self.prompts.render("tag_scene"),
        )
        return ReferenceService(
            gallery=self.gallery,
            provider=self._provider_instance() if require_provider else self._provider,
            llm=self.llm,
            data_dir=self.data_dir,
            config=ReferenceServiceConfig(
                compression=compression,
                prompts=prompts,
                auto_enable_generated_references=self.config.references.auto_enable_generated_references,
                tagging_task_name=self.config.model_tasks.tagging_task_name,
                tagging_temperature=self.config.model_tasks.temperature,
                tagging_max_tokens=self.config.model_tasks.max_tokens,
                reference_model=self.config.openai.reference_model,
                reference_mode=self.config.openai.reference_mode,
                prompt_version=self.config.plugin.config_version,
            ),
        )

    def _provider_instance(self) -> OpenAICompatibleProvider:
        if self._provider is None:
            if not self.config.openai.base_url.strip():
                raise PhotoStudioError("尚未配置 OpenAI 兼容 Base URL")
            if not self.config.openai.api_key.strip():
                raise PhotoStudioError("尚未配置 OpenAI 兼容 API Key")
            self._provider = OpenAICompatibleProvider(
                self.config.openai.base_url,
                self.config.openai.api_key,
                mode=self.config.openai.generation_mode,
                generation_model=self.config.openai.generation_model,
                extraction_model=self.config.openai.reference_model,
                timeout=self.config.openai.request_timeout_seconds,
                connect_timeout=self.config.openai.connect_timeout_seconds,
                max_response_bytes=self.config.openai.max_response_bytes,
            )
        return self._provider

    def _record_photo_references(
        self,
        task_id: str,
        person: ReferenceAsset | None,
        selection: SelectionResult,
        use_person: bool,
    ) -> None:
        records = [
            TaskReference(
                task_id=task_id,
                role="person",
                asset_id=person.id if person else None,
                selection_source="singleton" if person else ("disabled" if not use_person else "text_fallback"),
                fallback_reason=None if person else ("disabled" if not use_person else "no_active_person"),
            ),
            TaskReference(
                task_id=task_id,
                role="outfit",
                asset_id=selection.outfit.id if selection.outfit else None,
                selection_source=selection.reasons.get("outfit", "text_fallback"),
                fallback_reason=None if selection.outfit else selection.reasons.get("outfit"),
            ),
            TaskReference(
                task_id=task_id,
                role="scene",
                asset_id=selection.scene.id if selection.scene else None,
                selection_source=selection.reasons.get("scene", "text_fallback"),
                fallback_reason=None if selection.scene else selection.reasons.get("scene"),
                selection_metadata={"scene_signature": selection.scene_signature},
            ),
        ]
        with self.storage.transaction():
            for record in records:
                self.storage.record_task_reference(record)

    def _record_scene_photo_references(self, task_id: str, selection: SelectionResult) -> None:
        records = [
            TaskReference(
                task_id=task_id,
                role="person",
                asset_id=None,
                selection_source="disabled",
                fallback_reason="scene_photo_no_person",
            ),
            TaskReference(
                task_id=task_id,
                role="outfit",
                asset_id=None,
                selection_source="disabled",
                fallback_reason="scene_photo_no_outfit",
            ),
            TaskReference(
                task_id=task_id,
                role="scene",
                asset_id=selection.scene.id if selection.scene else None,
                selection_source=selection.reasons.get("scene", "text_fallback"),
                fallback_reason=None if selection.scene else selection.reasons.get("scene"),
                selection_metadata={"scene_signature": selection.scene_signature},
            ),
        ]
        with self.storage.transaction():
            for record in records:
                self.storage.record_task_reference(record)

    def _schedule_backfill_tasks(
        self,
        parent: ImageTask,
        payload: Mapping[str, Any],
        selection: SelectionResult,
        result_path: Path,
        use_outfit: bool,
        use_scene: bool,
    ) -> None:
        if not self.config.references.auto_extract_missing:
            return
        invocation = InvocationContext(
            stream_id=parent.stream_id or "",
            scope_key=parent.scope_key,
            user_id=parent.user_id or "",
            group_id=parent.group_id,
            message_id="",
            message={},
        )
        result = result_path.read_bytes()
        jobs: list[tuple[ReferenceCategory, str]] = []
        if use_outfit and selection.outfit is None:
            jobs.append((ReferenceCategory.OUTFIT, f"自动补库-服装-{parent.id[:8]}"))
        if use_scene and selection.scene is None and selection.scene_eligible:
            jobs.append((ReferenceCategory.SCENE, f"自动补库-场景-{parent.id[:8]}"))
        for category, name in jobs:
            try:
                self.submit_reference_job(
                    invocation,
                    operation="extract",
                    category=category,
                    name=name,
                    image=result,
                    parent_task_id=parent.id,
                    automatic=True,
                )
            except Exception as exc:
                self.ctx.logger.error(
                    "任务 %s 自动补充 %s 参考图失败: %s",
                    parent.id,
                    category.value,
                    _safe_error(exc),
                )

    def _save_result(self, task_id: str, generated: GeneratedImage) -> Path:
        suffix = {
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(generated.media_type.casefold(), ".jpg")
        path = (self.results_dir / f"{task_id}{suffix}").resolve()
        path.relative_to(self.results_dir)
        TaskPayloadStore._atomic_write(path, generated.data)
        return path

    def _person_prompt(self, person: ReferenceAsset | None) -> str:
        if person is not None:
            return f"严格保持人物参考图身份一致。标签：{json.dumps(person.effective_tags, ensure_ascii=False)}"
        return self.prompts.render(
            "person_fallback_prompt",
            person_prompt=self.config.prompts.person_prompt,
        )

    def _outfit_prompt(self, outfit: ReferenceAsset | None, hint: str) -> str:
        if outfit is not None:
            base = f"严格保持服装参考图一致。标签：{json.dumps(outfit.effective_tags, ensure_ascii=False)}"
        else:
            base = self.config.prompts.clothing_style_prompt
        return f"{base}\n用户服装提示：{hint}" if hint else base

    def _scene_prompt(self, scene: ReferenceAsset | None, hint: str) -> str:
        if scene is not None:
            base = (
                "严格保持场景参考图的空间结构与关键细节一致。标签："
                f"{json.dumps(scene.effective_tags, ensure_ascii=False)}"
            )
        else:
            base = self.prompts.render("scene_fallback_prompt", scene_hint=hint)
        return base or hint

    def _merge_task_metadata(self, task_id: str, **values: Any) -> ImageTask:
        task = self.storage.get_task(task_id)
        if task is None:
            raise RecordNotFoundError(f"task not found: {task_id}")
        task.result_metadata.update(values)
        return self.storage.update_task(task)

    def _task_cancelled(self, task_id: str) -> bool:
        current = self.storage.get_task(task_id)
        return current is None or current.status == TaskStatus.CANCELLED

    def _prompt_version(self, kind: str) -> str:
        selected = {
            "image": self.config.prompts.scene_photo_system + self.config.prompts.scene_photo_user,
            "scene_photo": self.config.prompts.scene_photo_system + self.config.prompts.scene_photo_user,
            "photo": self.config.prompts.photo_system + self.config.prompts.photo_user,
        }.get(kind, kind)
        digest = hashlib.sha256(selected.encode("utf-8")).hexdigest()[:16]
        return f"{self.config.plugin.config_version}:{kind}:{digest}"

    def _person_reference_required(self, requested: Any) -> bool:
        """Return whether a person-bearing photo must attach the person board.

        Config off → person reference is optional and defaults to off.
        Config on  → person reference is required unless the caller opts out,
        which is rejected by ``_validate_photo_person_config``.
        """

        if not self.config.references.person_reference_enabled:
            return bool(requested) if requested is not None else False
        return True if requested is None else bool(requested)

    def _validate_photo_person_config(self, requested: Any) -> None:
        if not self.config.references.person_reference_enabled:
            # Config closed: callers may omit or pass false.  Passing true is
            # still allowed and will require an active person board at runtime.
            return
        if requested is False:
            raise PhotoStudioError("当前配置要求含人物写真必须使用人物参考图，use_person_reference 不能关闭")

    def _validate_photo_person_requirement(self, requested: Any) -> ReferenceAsset:
        """Require the configured global person reference when enabled.

        Validation is intentionally repeated by the worker so queue delays
        cannot turn an initially valid task into a text fallback.
        """

        self._validate_photo_person_config(requested)
        if not self._person_reference_required(requested):
            raise PhotoStudioError("当前任务未启用人物参考图")
        person = self.gallery.get_person()
        if person is None:
            raise PhotoStudioError("尚未配置人物参考图；请由管理员先上传或提取人物参考图")
        if not person.is_selectable:
            raise PhotoStudioError(
                f"人物参考图当前状态为 {person.status.value}，必须先由管理员启用或修复后才能生成照片"
            )
        return person

    def _startup_reference_scan_pending(self) -> bool:
        task = self._reference_scan_task
        return task is not None and not task.done()


def _prompt_summary(value: str, limit: int = 120) -> str:
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= limit else normalized[: limit - 1] + "…"


def _optional_bool(value: Any, default: bool) -> bool:
    return default if value is None else bool(value)


def _reference_label(asset: ReferenceAsset | None) -> dict[str, Any] | None:
    if asset is None:
        return None
    return {"id": asset.id, "name": asset.name, "tags": asset.effective_tags}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _safe_error(error: BaseException) -> str:
    if isinstance(error, ProviderError):
        return str(error)[:1000]
    text = str(error)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(?:sk|key|token)-[A-Za-z0-9._~-]{8,}", "[REDACTED]", text)
    text = re.sub(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s,;\"']+", r"\1[REDACTED]", text)
    return (text or type(error).__name__)[:1000]


def _require_capability_success(result: Any, operation: str) -> None:
    """Treat SDK capability soft-fail responses like raised failures."""

    if result is False or (isinstance(result, Mapping) and result.get("success") is False):
        raise PhotoStudioError(f"{operation} failed")


__all__ = [
    "PermissionDeniedError",
    "PhotoStudioError",
    "PhotoStudioService",
    "TaskAccessError",
    "TaskPayloadStore",
]
