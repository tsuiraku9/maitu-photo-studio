"""官方 WebUI 私有 API 适配：短请求、明确确认、持久提交回执。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any, Mapping

from PIL import Image

from .compression import HARD_MAX_TARGET_BYTES, CompressionConfig, CompressionResult, compress_image
from .gallery import GalleryError, ReferenceNotFoundError, ReferenceNotSelectableError, ReferenceVersionConflictError
from .gallery_management import GalleryManagement
from .models import ImageTask, ReferenceAsset, ReferenceCategory, TaskStatus
from .reference_service import ReferenceTagValidationError, validate_reference_tags
from .sizes import kb_to_bytes
from .storage import StorageError
from .webui_images import WebUIImages

_ID = re.compile(r"(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\Z")
_ASSET_ID = re.compile(r"[0-9a-f]{32}\Z")
_TAG_FIELDS = {
    "person": ["appearance_summary", "confidence"],
    "outfit": ["type", "wearing_scenes", "seasons", "styles", "confidence"],
    "scene": ["room_type", "privacy_eligible", "scene_signature", "confidence"],
}
_SUBMIT_FIELDS = {
    "operation",
    "category",
    "name",
    "asset_id",
    "stage_id",
    "batch_id",
    "expected_version",
    "person_version",
    "appearance_hint",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _request_id(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("请求 ID 必须是小写 UUID")
    return value


def _text(value: Any, *, limit: int = 256) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("文本参数类型或长度无效")
    return value


def _asset_id(value: Any) -> str:
    if not isinstance(value, str) or not _ASSET_ID.fullmatch(value):
        raise ValueError("参考图 ID 无效")
    return value


def _budget(response: dict[str, Any]) -> dict[str, Any]:
    if len(_json(response).encode()) > 480_000:
        raise ValueError("响应超过预算，请减少分页大小")
    return response


def asset_data(asset: ReferenceAsset) -> dict[str, Any]:
    validation = validate_reference_tags(asset.category, asset.effective_tags)
    return {
        "id": asset.id,
        "name": asset.name,
        "category": asset.category.value,
        "status": asset.status.value,
        "tags": asset.tags,
        "manual_tags": asset.manual_tags,
        "effective_tags": asset.effective_tags,
        "updated_at": asset.updated_at.isoformat(),
        "created_at": asset.created_at.isoformat(),
        "use_count": asset.use_count,
        "selectable": asset.is_selectable and validation.selectable,
        "validation_errors": list(validation.errors),
        "cleanup_error": asset.selection_metadata.get("cleanup_error", ""),
    }


class PhotoWebUI:
    """授权在 Host 的 WebUI 网关；此层从不接受客户端身份或投递目标。"""

    def __init__(self, service: Any, ctx: Any) -> None:
        self.service = service
        self.ctx = ctx
        self.storage = service.storage
        self.management = GalleryManagement(service.gallery, service.data_dir)
        runtime = Path(ctx.paths.runtime_dir)
        self.images = WebUIImages(service.gallery, service.data_dir, runtime)
        self.stage_root = service.data_dir / "webui-staging"
        self.stage_root.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._confirmations: dict[str, dict[str, Any]] = {}
        self._cleanup_stages()

    async def gallery_query(self, operation: str = "list", args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        args = self._args(args)
        if operation == "settings":
            self._keys(args, set())
            person = self.service.gallery.get_person()
            return {
                "auto_enable": self.service.config.references.auto_enable_generated_references,
                "tag_fields": _TAG_FIELDS,
                "page_size": 12,
                "max_batch": 20,
                "person": {
                    "asset_id": person.id,
                    "version": person.updated_at.isoformat(),
                    "status": person.status.value,
                }
                if person
                else {"asset_id": "", "version": "", "status": ""},
            }
        if operation == "list":
            self._keys(args, {"category", "status", "search", "offset", "limit"})
            category = _text(args.get("category", "")) or None
            status = _text(args.get("status", "")) or None
            if status == "deleted":
                raise ValueError("本版本不提供回收站")
            items, total = self.storage.query_reference_assets(
                category=category,
                status=status,
                search=_text(args.get("search", ""), limit=4000),
                offset=self._int(args.get("offset", 0), 0, 1_000_000),
                limit=self._int(args.get("limit", 12), 1, 24),
            )
            return _budget({"items": [asset_data(item) for item in items], "total": total})
        if operation == "detail":
            self._keys(args, {"asset_id"})
            return _budget({"asset": asset_data(self.service.gallery.require(_asset_id(args.get("asset_id"))))})
        if operation == "images":
            self._keys(args, {"asset_ids", "variant"})
            ids = self._ids(args.get("asset_ids"), maximum=6)
            return self.images.query(ids, _text(args.get("variant", "thumbnail")))
        raise ValueError("未知图库查询")

    async def task_query(self, operation: str = "list", args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        args = self._args(args)
        if operation == "list":
            self._keys(
                args, {"origin", "status", "kind", "task_id", "created_after", "created_before", "offset", "limit"}
            )
            tasks, total = self.storage.query_tasks(
                origin=_text(args.get("origin", "")),
                status=_text(args.get("status", "")),
                kind=_text(args.get("kind", "")),
                task_id=_text(args.get("task_id", "")),
                created_after=_text(args.get("created_after", "")),
                created_before=_text(args.get("created_before", "")),
                offset=self._int(args.get("offset", 0), 0, 1_000_000),
                limit=self._int(args.get("limit", 12), 1, 24),
            )
            return _budget({"items": [self._task_data(task) for task in tasks], "total": total})
        if operation == "detail":
            self._keys(args, {"task_id"})
            return _budget({"task": self._task_data(self._task(_asset_id(args.get("task_id"))), detail=True)})
        if operation == "image":
            self._keys(args, {"task_id"})
            return self.images.query_task(self._task(_asset_id(args.get("task_id"))))
        if operation == "receipt":
            self._keys(args, {"request_id"})
            receipt = self.storage.get_request(_request_id(args.get("request_id")))
            if receipt is None:
                return {"receipt": None}
            return {"receipt": {key: receipt[key] for key in ("request_id", "state", "task_id", "response")}}
        raise ValueError("未知任务查询")

    async def stage_upload(self, upload_id: str, request_id: str) -> dict[str, Any]:
        """仅暂存处理后的图，不触发 AI；原始领取文件总是在 finally 清理。"""
        request_id = _request_id(request_id)
        upload_id = _text(upload_id)
        async with self._lock:
            existing = self.storage.get_request(request_id)
            signature = _digest({"upload_id": upload_id})
            if existing is not None:
                self.storage.begin_request(request_id, "stage", signature)
                return self._existing_response(existing)
            self.storage.begin_request(request_id, "stage", signature)
            source: Path | None = None
            try:
                self._cleanup_stages()
                if sum(1 for _ in self.stage_root.glob("*.json")) >= 64:
                    raise ValueError("暂存图片过多，请先提交或等待过期")
                envelope = await self.ctx.webui.claim_upload(upload_id)
                if not isinstance(envelope, dict) or envelope.get("success") is False:
                    raise ValueError("上传领取失败")
                upload = envelope.get("upload", envelope)
                if not isinstance(upload, dict) or not upload.get("path"):
                    raise ValueError("上传领取失败")
                source = self._claimed_path(upload["path"])
                result = await asyncio.to_thread(self._normalize_upload, source, upload.get("sha256"))
                response = {"stage_id": request_id, "expires_at": time.time() + 3600}
                metadata = {"expires_at": response["expires_at"], "compression": result.to_dict()}
                self.service.payloads._atomic_write(self.stage_root / f"{request_id}.jpg", result.data)
                self.service.payloads._atomic_write(self.stage_root / f"{request_id}.json", _json(metadata).encode())
                self.storage.update_request(request_id, state="completed", response=response)
                return response
            except BaseException:
                self._remove_stage(request_id)
                self.storage.update_request(
                    request_id, state="failed", response={"success": False, "error": "图片暂存失败"}
                )
                raise
            finally:
                if source is not None:
                    try:
                        source.unlink(missing_ok=True)
                    except OSError:
                        self.service.log.warning("网页上传原文件清理失败", error_kind="upload_cleanup")

    async def action(
        self,
        operation: str,
        request_id: str,
        args: Mapping[str, Any] | None = None,
        confirm_token: str = "",
    ) -> dict[str, Any]:
        args = self._args(args)
        request_id = _request_id(request_id)
        async with self._lock:
            if operation == "confirm":
                return self._confirm(args)
            if operation == "prepare_task_preview":
                self._keys(args, {"task_id"})
                task = self._task(_asset_id(args.get("task_id")))
                await asyncio.to_thread(self.images.prepare_task, task)
                return {"success": True}
            if operation == "prepare_preview":
                self._keys(args, {"asset_ids", "variant"})
                ids = self._ids(args.get("asset_ids"), maximum=6)
                variant = _text(args.get("variant", "thumbnail"))
                if variant != "thumbnail" and len(ids) != 1:
                    raise ValueError("大图一次只能准备一张")
                for asset_id in ids:
                    await asyncio.to_thread(self.images.prepare, asset_id, variant)
                return {"success": True}
            signature = _digest({"operation": operation, "args": args})
            existing = self.storage.get_request(request_id)
            if existing is not None:
                self.storage.begin_request(request_id, operation, signature)
                return self._existing_response(existing)
            self.storage.begin_request(request_id, operation, signature)
            try:
                if operation == "submit":
                    self._check_submit(args)
                    if self._needs_confirmation(args):
                        self._consume(confirm_token, operation, args, request_id)
                    response = await self._submit(args, request_id)
                elif operation == "edit":
                    self._keys(args, {"asset_id", "name", "manual_tags", "expected_version"})
                    asset_id = _asset_id(args.get("asset_id"))
                    self._ensure_idle(asset_id)
                    with self.storage.transaction():
                        asset = self.management.edit(
                            asset_id,
                            name=_text(args.get("name"), limit=200) if "name" in args else None,
                            manual_tags=self._args(args["manual_tags"]) if "manual_tags" in args else None,
                            merge_manual_tags=False,
                            expected_version=self._expected(args),
                        )
                        response = {"success": True, "asset": asset_data(asset)}
                        self.storage.update_request(request_id, state="completed", response=response)
                    return response
                elif operation in {"enable", "disable", "delete"}:
                    self._keys(args, {"ids", "versions"})
                    ids = self._ids(args.get("ids"))
                    versions = self._versions(args.get("versions"), ids)
                    if operation == "delete":
                        self._consume(confirm_token, operation, args, request_id)
                    items = []
                    # 删除必须先提交 tombstone 再移除文件，不得外包一个可回滚事务。
                    for asset_id in ids:
                        try:
                            self._ensure_idle(asset_id)
                            asset = getattr(self.management, operation)(asset_id, expected_version=versions[asset_id])
                            cleanup_error = (
                                asset.selection_metadata.get("cleanup_error") if operation == "delete" else None
                            )
                            if operation == "delete":
                                try:
                                    self.images.invalidate(asset_id)
                                except OSError:
                                    cleanup_error = "preview_cleanup"
                                    self.service.log.warning("已删除参考图的预览清理失败", error_kind=cleanup_error)
                            items.append(
                                {
                                    "asset_id": asset_id,
                                    "success": not bool(cleanup_error),
                                    "error": "图片清理失败，请检查日志" if cleanup_error else "",
                                }
                            )
                        except (ValueError, RuntimeError) as exc:
                            items.append({"asset_id": asset_id, "success": False, "error": self._error(exc)})
                    response = {"success": all(item["success"] for item in items), "items": items}
                    self.storage.update_request(request_id, state="completed", response=response)
                    return response
                elif operation in {"cancel", "retry"}:
                    self._keys(args, {"task_id"})
                    task = self._reference_task(_asset_id(args.get("task_id")))
                    if task.request.get("origin") != "webui" or task.request.get("delivery") != "none":
                        raise ValueError("此任务只能在原入口管理")
                    if operation == "retry":
                        self._consume(confirm_token, operation, args, request_id)
                    if operation == "retry":
                        with self.storage.transaction():
                            task = self.service.retry_task(task.id)
                            response = {"success": True, "task_id": task.id, "status": task.status.value}
                            self.storage.update_request(
                                request_id,
                                state="accepted",
                                response=response,
                                task_id=task.id,
                            )
                    else:
                        # cancel 会删除载荷，先独立提交取消状态，避免回滚后重新收费。
                        task = self.service.cancel_task(task.id)
                        response = {"success": True, "task_id": task.id, "status": task.status.value}
                        self.storage.update_request(
                            request_id,
                            state="completed",
                            response=response,
                            task_id=task.id,
                        )
                    return response
                else:
                    raise ValueError("未知图库操作")
                return response
            except Exception as exc:
                self.storage.update_request(
                    request_id, state="failed", response={"success": False, "error": self._error(exc)}
                )
                raise

    async def _submit(self, args: dict[str, Any], request_id: str) -> dict[str, Any]:
        operation = args["operation"]
        image: bytes | None = None
        source_metadata: dict[str, Any] | None = None
        stage_id = _text(args.get("stage_id", ""))
        if stage_id:
            image, source_metadata = self._stage(stage_id)
        if operation in {"import", "extract", "replace"} and image is None:
            raise ValueError("请先上传图片")
        identity = {"nickname": "", "personality": ""}
        if operation == "generate_person":
            if self.service.gallery.get_person() is not None:
                raise ValueError("已有人物参考图，请先清空后再按人格生成")
            identity["nickname"], identity["personality"] = await self.service.load_host_identity()
            if not identity["personality"]:
                raise ValueError("未能读取 MaiBot 人格设定")
        self._check_targets(args)
        task: ImageTask | None = None
        try:
            with self.storage.transaction():
                task = self.service.submit_webui_reference_job(
                    operation=operation,
                    category=args.get("category", ""),
                    name=args.get("name", ""),
                    image=image,
                    source_metadata=source_metadata,
                    asset_id=args.get("asset_id", ""),
                    batch_id=args.get("batch_id", ""),
                    request_id=request_id,
                    expected_version=args.get("expected_version", ""),
                    person_version=args.get("person_version", ""),
                    appearance_hint=args.get("appearance_hint", ""),
                    **identity,
                )
                response = {"success": True, "task_id": task.id, "status": task.status.value}
                self.storage.update_request(request_id, state="accepted", response=response, task_id=task.id)
                if stage_id:
                    self.storage.update_request(stage_id, state="completed", task_id=task.id)
        except BaseException:
            if task is not None:
                self.service.payloads.delete(task.id)
                self.service.payloads.delete_upload(task.id)
            raise
        if stage_id:
            try:
                self._remove_stage(stage_id)
            except OSError:
                self.service.log.warning("已提交的网页暂存清理失败", error_kind="staging_cleanup")
        return response

    def _confirm(self, args: dict[str, Any]) -> dict[str, Any]:
        self._keys(args, {"action", "params", "target_request_id"})
        action = _text(args.get("action"))
        target = _request_id(args.get("target_request_id"))
        params = self._args(args.get("params"))
        allowed: dict[str, str] = {}
        if action == "batch_submit":
            self._keys(params, {"items"})
            items = params.get("items")
            if not isinstance(items, list) or not 1 <= len(items) <= 20:
                raise ValueError("批次最多 20 张")
            for item in items:
                item = self._args(item)
                self._keys(item, _SUBMIT_FIELDS | {"request_id"})
                item_id = _request_id(item.get("request_id"))
                if item_id in allowed:
                    raise ValueError("批次请求 ID 不能重复")
                submit_args = {key: value for key, value in item.items() if key != "request_id"}
                self._check_submit(submit_args)
                if submit_args.get("category") == "person" and len(items) != 1:
                    raise ValueError("人物参考不允许批量替换")
                allowed[item_id] = _digest({"operation": "submit", "args": submit_args})
            summary = f"将提交 {len(items)} 个参考图作业；生成类可能计费，失败不会自动重试。"
        elif action == "submit":
            self._check_submit(params)
            allowed[target] = _digest({"operation": action, "args": params})
            summary = "将调用模型处理参考图，可能计费。人物待审的新图也会替换旧人物。"
        elif action == "delete":
            self._keys(params, {"ids", "versions"})
            ids = self._ids(params.get("ids"))
            self._versions(params.get("versions"), ids)
            allowed[target] = _digest({"operation": action, "args": params})
            summary = f"将永久删除 {len(ids)} 条参考图及对应图片，无法恢复。"
        elif action == "retry":
            self._keys(params, {"task_id"})
            task = self._reference_task(_asset_id(params.get("task_id")))
            if task.request.get("origin") != "webui":
                raise ValueError("此任务只能在原入口重试")
            allowed[target] = _digest({"operation": action, "args": params})
            summary = "将重新提交失败作业，可能重复计费；无法确认上游是否已经收费。"
        else:
            raise ValueError("不支持的确认操作")
        now = time.time()
        self._confirmations = {token: item for token, item in self._confirmations.items() if item["expires_at"] > now}
        if len(self._confirmations) >= 128:
            raise ValueError("确认请求过多，请稍后再试")
        token = secrets.token_urlsafe(32)
        self._confirmations[token] = {"expires_at": now + 300, "allowed": allowed}
        return {"success": True, "confirmation": {"token": token, "summary": summary, "expires_at": now + 300}}

    def _consume(self, token: str, operation: str, args: dict[str, Any], request_id: str) -> None:
        entry = self._confirmations.get(token)
        if entry is None or entry["expires_at"] < time.time():
            raise ValueError("请重新确认此操作")
        signature = _digest({"operation": operation, "args": args})
        if entry["allowed"].get(request_id) != signature:
            raise ValueError("确认内容已经变化，请重新确认")
        del entry["allowed"][request_id]
        if not entry["allowed"]:
            del self._confirmations[token]

    def _check_submit(self, args: dict[str, Any]) -> None:
        self._keys(args, _SUBMIT_FIELDS)
        operation = _text(args.get("operation"))
        if operation not in {"import", "extract", "replace", "retag", "regenerate", "generate_person"}:
            raise ValueError("不支持的参考图作业")
        for key in _SUBMIT_FIELDS:
            if key in args:
                _text(args[key], limit=4000 if key == "appearance_hint" else 200 if key == "name" else 400)
        if args.get("category"):
            ReferenceCategory(args["category"])
        if operation in {"import", "extract", "generate_person"} and not args.get("category"):
            raise ValueError("请指定分类")
        if operation == "generate_person" and args.get("category") != "person":
            raise ValueError("纯文字生成仅支持人物")
        if operation in {"replace", "retag", "regenerate"}:
            _asset_id(args.get("asset_id"))
            self._expected(args)
        if args.get("stage_id"):
            _request_id(args["stage_id"])
        if args.get("batch_id"):
            _request_id(args["batch_id"])
        self._check_targets(args)

    def _check_targets(self, args: dict[str, Any]) -> None:
        asset_id = args.get("asset_id")
        if asset_id:
            asset = self.service.gallery.require(asset_id)
            if args.get("expected_version") != asset.updated_at.isoformat():
                raise ValueError("参考图已变化，请刷新后重新提交")
            if args.get("category") and args["category"] != asset.category.value:
                raise ValueError("参考图分类不一致")
        category = args.get("category")
        if category == "person" or (
            asset_id and self.service.gallery.require(asset_id).category == ReferenceCategory.PERSON
        ):
            person = self.service.gallery.get_person()
            version = f"{person.id}:{person.updated_at.isoformat()}" if person else "none"
            if args.get("person_version") != version:
                raise ValueError("人物参考已变化，请刷新后重新确认")

    def _needs_confirmation(self, args: dict[str, Any]) -> bool:
        if args["operation"] in {"extract", "regenerate", "generate_person"}:
            return True
        if args["operation"] in {"import", "replace"}:
            asset_id = args.get("asset_id")
            return args.get("category") == "person" or bool(
                asset_id and self.service.gallery.require(asset_id).category == ReferenceCategory.PERSON
            )
        return False

    def _ensure_idle(self, asset_id: str) -> None:
        self.service.ensure_reference_idle(asset_id)

    def _task_data(self, task: ImageTask, *, detail: bool = False) -> dict[str, Any]:
        origin = task.request.get("origin") or (
            "automatic" if task.request.get("automatic") else "qq" if task.stream_id else "unknown"
        )
        reference = task.kind.startswith("reference_")
        photo = task.kind in {"photo", "scene_photo", "image"}
        webui = reference and origin == "webui" and task.request.get("delivery") == "none"
        retry_payload_available = False
        if webui and task.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
            try:
                payload = self.service.payloads.get(task.id)
                retry_payload_available = (
                    not payload.get("has_upload") or (self.service.payloads.upload_root / f"{task.id}.bin").is_file()
                )
            except (RuntimeError, OSError, ValueError):
                pass
        output = task.result_metadata.get("output_image", {})
        result_available = bool(task.result_path and task.result_path.is_file())
        image_sent = bool(task.result_metadata.get("image_sent") or (photo and task.status == TaskStatus.SENT))
        result = {
            "task_id": task.id,
            "id": task.id,
            "kind": task.kind,
            "operation": task.request.get("operation", task.kind.removeprefix("reference_")),
            "origin": origin,
            "status": "completed" if not photo and task.status == TaskStatus.SENT else task.status.value,
            "parent_task_id": task.parent_task_id or "",
            "result_available": result_available,
            "result_cleaned": bool(task.result_metadata.get("result_cleaned_at")),
            "delivery_status": "none"
            if not photo or task.request.get("delivery") == "none"
            else ("sent" if image_sent else "failed" if task.result_metadata.get("delivery_failed") else "pending"),
            "planner_notified": bool(task.planner_notified_at),
            "notification_failed": bool(task.result_metadata.get("notification_error")),
            "output_image": {
                key: output[key]
                for key in ("enabled", "compressed", "original_kb", "size_kb", "width", "height", "format")
                if key in output
            },
            "asset_id": task.result_metadata.get("asset_id", ""),
            "asset_status": task.result_metadata.get("asset_status", ""),
            "error": (
                "发送图片压缩失败，生成原图已保留；请检查压缩限制并在原入口处理"
                if task.result_metadata.get("compression_failed")
                else "任务处理失败，请检查插件日志；重新提交可能重复计费"
            )
            if task.error_message and task.status != TaskStatus.CANCELLED
            else "",
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat(),
            "batch_id": task.request.get("batch_id", ""),
            "paid_request_started": task.paid_request_started,
            "duplicate": task.result_metadata.get("duplicate", False),
            "can_cancel": webui and not task.terminal,
            "can_retry": retry_payload_available,
        }
        if detail:
            result["references"] = [
                {"role": item.role, "asset_id": item.asset_id or "", "source": item.selection_source}
                for item in self.storage.list_task_references(task.id)
            ]
        return result

    def _task(self, task_id: str) -> ImageTask:
        task = self.storage.get_task(task_id)
        if task is None:
            raise ValueError("任务不存在或已按保留策略清理")
        return task

    def _reference_task(self, task_id: str) -> ImageTask:
        task = self.storage.get_task(task_id)
        if task is None or not task.kind.startswith("reference_"):
            raise ValueError("参考作业不存在")
        return task

    def _stage(self, stage_id: str) -> tuple[bytes, dict[str, Any]]:
        stage_id = _request_id(stage_id)
        receipt = self.storage.get_request(stage_id)
        if receipt is None or receipt["operation"] != "stage" or receipt["state"] != "completed" or receipt["task_id"]:
            raise ValueError("上传暂存已使用或不存在，请重新上传")
        path = self.stage_root / f"{stage_id}.json"
        image = self.stage_root / f"{stage_id}.jpg"
        if not path.is_file() or not image.is_file() or path.is_symlink() or image.is_symlink():
            raise ValueError("上传暂存已使用或不存在，请重新上传")
        metadata = json.loads(path.read_text(encoding="utf-8"))
        if metadata["expires_at"] < time.time():
            raise ValueError("上传暂存已过期，请重新上传")
        data = image.read_bytes()
        if len(data) > HARD_MAX_TARGET_BYTES or hashlib.sha256(data).hexdigest() != metadata["compression"]["sha256"]:
            raise ValueError("暂存图片校验失败")
        return data, metadata["compression"]

    def _remove_stage(self, stage_id: str) -> None:
        stage_id = _request_id(stage_id)
        for suffix in (".jpg", ".json"):
            (self.stage_root / f"{stage_id}{suffix}").unlink(missing_ok=True)

    def _cleanup_stages(self) -> None:
        for path in self.stage_root.glob("*.json"):
            if not _ID.fullmatch(path.stem) or path.is_symlink():
                continue
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
                if metadata["expires_at"] < time.time():
                    self._remove_stage(path.stem)
            except (OSError, ValueError, KeyError):
                self.service.log.warning("网页暂存清理失败", error_kind="staging_metadata")

    def _claimed_path(self, value: Any) -> Path:
        path = Path(_text(value, limit=4096))
        root = self.service.data_dir / "uploads"
        if not path.is_absolute() or ".." in path.parts or path.parent != root:
            raise ValueError("上传领取路径无效")
        if any(component.is_symlink() for component in (*path.parents, path)) or not path.is_file():
            raise ValueError("上传领取路径无效")
        return path

    def _normalize_upload(self, path: Path, sha256: Any) -> CompressionResult:
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("上传图片超过 20 MiB")
        data = path.read_bytes()
        if not isinstance(sha256, str) or not secrets.compare_digest(hashlib.sha256(data).hexdigest(), sha256):
            raise ValueError("上传图片校验失败")
        with Image.open(path) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"} or getattr(image, "n_frames", 1) != 1:
                raise ValueError("仅支持静态 JPEG、PNG 和 WebP 图片")
            if image.width * image.height > min(self.service.config.references.max_pixels, 40_000_000):
                raise ValueError("上传图片像素超过限制")
        return compress_image(data, config=self._compression())

    def _compression(self) -> CompressionConfig:
        settings = self.service.config.references
        return CompressionConfig(
            target_bytes=kb_to_bytes(settings.max_kb),
            max_edge=settings.max_edge,
            max_pixels=min(settings.max_pixels, 40_000_000),
            max_input_bytes=20 * 1024 * 1024,
        )

    @staticmethod
    def _existing_response(receipt: dict[str, Any]) -> dict[str, Any]:
        if receipt["state"] == "preparing":
            raise ValueError("提交结果尚未确定，请查询回执，勿重复提交")
        return receipt["response"]

    @staticmethod
    def _args(value: Mapping[str, Any] | None) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
            raise ValueError("参数必须是对象")
        result = dict(value)
        if len(_json(result).encode()) > 48_000:
            raise ValueError("参数超过预算")
        return result

    @staticmethod
    def _keys(args: dict[str, Any], allowed: set[str]) -> None:
        if set(args) - allowed:
            raise ValueError("包含不允许的参数")

    @staticmethod
    def _int(value: Any, minimum: int, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError("分页参数超出范围")
        return value

    @staticmethod
    def _ids(value: Any, maximum: int = 20) -> list[str]:
        if not isinstance(value, list) or not 1 <= len(value) <= maximum or len(set(value)) != len(value):
            raise ValueError(f"请选择 1–{maximum} 个不同条目")
        return [_asset_id(item) for item in value]

    @staticmethod
    def _versions(value: Any, ids: list[str]) -> dict[str, str]:
        if not isinstance(value, list) or len(value) != len(ids):
            raise ValueError("缺少参考图版本")
        versions = {}
        for item in value:
            if not isinstance(item, dict) or set(item) != {"asset_id", "updated_at"}:
                raise ValueError("版本格式无效")
            versions[_asset_id(item["asset_id"])] = _text(item["updated_at"])
        if set(versions) != set(ids) or not all(versions.values()):
            raise ValueError("参考图版本不匹配")
        return versions

    @staticmethod
    def _expected(args: dict[str, Any]) -> str:
        version = _text(args.get("expected_version", ""))
        if not version:
            raise ValueError("缺少参考图版本，请刷新")
        return version

    @staticmethod
    def _error(exc: BaseException) -> str:
        # 不向浏览器回传上游响应、配置或本地路径；详细分类留在插件日志。
        if isinstance(exc, ReferenceVersionConflictError):
            return "参考图已变更，请刷新后重新确认"
        if isinstance(exc, ReferenceNotFoundError):
            return "参考图不存在或已删除，请刷新"
        if isinstance(exc, ReferenceNotSelectableError):
            return "参考图生效标签不合格，请先修正标签再启用"
        if isinstance(exc, ReferenceTagValidationError):
            return "人工标签不符合此分类的字段和类型要求，请修正后保存"
        if isinstance(exc, StorageError):
            return "请求 ID 冲突或存储操作失败，请先查询回执，勿重复提交"
        text = str(exc)
        if (
            isinstance(exc, (ValueError, GalleryError))
            and text
            and "/" not in text
            and "\\" not in text
            and len(text) <= 300
        ):
            return text
        return "操作失败，请检查插件日志并刷新状态；不要盲目重复提交"
