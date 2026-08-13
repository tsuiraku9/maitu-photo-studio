"""MaiBot entry point for the MaiTu photo plugin."""

from __future__ import annotations

import base64
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any, Mapping

if __package__:
    from .maitu_photo.commands import (
        AdminCommand,
        CommandParseError,
        help_text,
        parse_admin_command,
        parse_tags,
    )
    from .maitu_photo.config import PhotoPluginConfig, ToolDescriptionSection
    from .maitu_photo.models import ReferenceAsset, ReferenceCategory, TaskStatus
    from .maitu_photo.reference_service import validate_reference_tags
    from .maitu_photo.runtime import (
        InvocationContext,
        InvocationError,
        invocation_context,
        resolve_single_image,
    )
    from .maitu_photo.sdk_compat import (
        CONFIG_RELOAD_SCOPE_SELF,
        Command,
        MaiBotPlugin,
        Tool,
        ToolParameterInfo,
        ToolParamType,
    )
    from .maitu_photo.service import PhotoStudioService
else:  # direct local import used by unit tests
    from maitu_photo.commands import (
        AdminCommand,
        CommandParseError,
        help_text,
        parse_admin_command,
        parse_tags,
    )
    from maitu_photo.config import PhotoPluginConfig, ToolDescriptionSection
    from maitu_photo.models import ReferenceAsset, ReferenceCategory, TaskStatus
    from maitu_photo.reference_service import validate_reference_tags
    from maitu_photo.runtime import (
        InvocationContext,
        InvocationError,
        invocation_context,
        resolve_single_image,
    )
    from maitu_photo.sdk_compat import (
        CONFIG_RELOAD_SCOPE_SELF,
        Command,
        MaiBotPlugin,
        Tool,
        ToolParameterInfo,
        ToolParamType,
    )
    from maitu_photo.service import PhotoStudioService


PLUGIN_ID = "maitu.photo-studio"


class MaiTuPhotoPlugin(MaiBotPlugin):
    """真实照片生成、参考图库和连续性管理。"""

    config_model = PhotoPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._service: PhotoStudioService | None = None
        self._confirmations: dict[str, tuple[float, str, str, str]] = {}

    async def on_load(self) -> None:
        data_dir = Path(self.ctx.paths.data_dir)
        self._service = PhotoStudioService(self.ctx, self.config, data_dir)
        await self._service.start()
        self.ctx.logger.info("写真插件已加载，数据目录=%s", data_dir)

    async def on_unload(self) -> None:
        if self._service is not None:
            await self._service.close()
            self._service = None

    async def on_config_update(
        self,
        scope: str,
        config_data: dict[str, object],
        version: str,
    ) -> None:
        del config_data
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        if self._service is not None:
            await self._service.close()
        self._service = PhotoStudioService(self.ctx, self.config, self.ctx.paths.data_dir)
        await self._service.start()
        self.ctx.logger.info(
            "写真插件运行配置已热更新(version=%s)；工具描述或命令前缀变更需重载插件",
            version,
        )

    def get_components(self) -> list[dict[str, Any]]:
        """Inject configurable Tool text and command prefix at registration."""

        components = super().get_components()
        # Some SDK discovery paths inspect components before the runner has
        # injected the persisted config.  Use validated defaults for that
        # metadata-only pass; normal runtime calls still use ``self.config``.
        try:
            config = self.config
        except RuntimeError:
            config = PhotoPluginConfig()
        sections = {
            "generate_scene_photo": config.prompts.generate_scene_photo_tool,
            "generate_photo": config.prompts.generate_photo_tool,
            "manage_reference_gallery": config.prompts.gallery_tool,
            "get_image_task_status": config.prompts.status_tool,
        }
        for component in components:
            metadata = component.get("metadata")
            if not isinstance(metadata, dict):
                continue
            name = str(component.get("name") or "")
            section = sections.get(name)
            if section is not None:
                self._patch_tool_metadata(metadata, section)
            if name == "maitu_admin":
                prefix = config.plugin.command_prefix.strip().rstrip("/") or "/maitu"
                metadata["command_pattern"] = rf"^{re.escape(prefix)}(?:\s+.*)?$"
                metadata["description"] = f"{prefix} 写真插件管理员命令"
        return components

    @staticmethod
    def _patch_tool_metadata(metadata: dict[str, Any], section: ToolDescriptionSection) -> None:
        if section.brief.strip():
            metadata["description"] = section.brief.strip()
            metadata["brief_description"] = section.brief.strip()
        if section.detailed.strip():
            metadata["detailed_description"] = section.detailed.strip()
        parameters = metadata.get("parameters")
        if isinstance(parameters, list):
            for parameter in parameters:
                if not isinstance(parameter, dict):
                    continue
                name = str(parameter.get("name") or "")
                if name in section.parameters:
                    parameter["description"] = section.parameters[name]
        raw = metadata.get("parameters_raw")
        if isinstance(raw, dict):
            properties = raw.get("properties") if isinstance(raw.get("properties"), dict) else raw
            for name, description in section.parameters.items():
                schema = properties.get(name) if isinstance(properties, dict) else None
                if isinstance(schema, dict):
                    schema["description"] = description

    @Tool(
        "generate_scene_photo",
        brief_description="生成不含 bot 本人的手机真实环境照片",
        detailed_description=(
            "当需要发送不含 bot 本人的真实手机照片时使用。可按需使用场景参考，不使用人物和服装参考。"
            "请提供完整拍摄需求；工具立即返回任务 ID。"
        ),
        parameters=[
            ToolParameterInfo(
                "description",
                ToolParamType.STRING,
                "完整拍摄需求，画面不得出现 bot 本人",
                required=True,
            ),
            ToolParameterInfo("scene_hint", ToolParamType.STRING, "场景/地点提示", required=False, default=""),
            ToolParameterInfo("scene_id", ToolParamType.STRING, "场景参考 ID", required=False, default=""),
            ToolParameterInfo("use_scene_reference", ToolParamType.BOOLEAN, "是否使用场景参考", required=False),
            ToolParameterInfo(
                "force_new_scene", ToolParamType.BOOLEAN, "强制重新选择场景", required=False, default=False
            ),
            ToolParameterInfo("size", ToolParamType.STRING, "图片尺寸", required=False, default=""),
            ToolParameterInfo("model_id", ToolParamType.STRING, "模型覆盖", required=False, default=""),
        ],
    )
    async def handle_generate_scene_photo(
        self,
        description: str,
        scene_hint: str = "",
        scene_id: str = "",
        use_scene_reference: bool | None = None,
        force_new_scene: bool = False,
        size: str = "",
        model_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            self._require_enabled()
            invocation = invocation_context(kwargs)
            task = self.service.submit_scene_photo(
                invocation,
                description=description,
                scene_hint=scene_hint,
                scene_id=scene_id,
                use_scene_reference=use_scene_reference,
                force_new_scene=force_new_scene,
                size=size,
                model_id=model_id,
            )
            return {"success": True, "task_id": task.id, "status": task.status.value}
        except Exception as exc:
            return self._tool_error(exc)

    @Tool(
        "generate_photo",
        brief_description="生成 bot 本人出镜的手机真实生活照片",
        detailed_description=(
            "当需要发送 bot 本人出现在画面中的真实手机照片时使用。"
            "人物参考配置开启时必须使用全局人物参考板；关闭时改用文字人物描述。"
            "请提供完整拍摄需求；工具立即返回任务 ID。"
        ),
        parameters=[
            ToolParameterInfo("description", ToolParamType.STRING, "完整拍摄需求", required=True),
            ToolParameterInfo("outfit_hint", ToolParamType.STRING, "服装提示", required=False, default=""),
            ToolParameterInfo("scene_hint", ToolParamType.STRING, "场景提示", required=False, default=""),
            ToolParameterInfo("accessory_hint", ToolParamType.STRING, "配饰提示", required=False, default=""),
            ToolParameterInfo("outfit_id", ToolParamType.STRING, "服装参考 ID", required=False, default=""),
            ToolParameterInfo("scene_id", ToolParamType.STRING, "场景参考 ID", required=False, default=""),
            ToolParameterInfo(
                "use_person_reference",
                ToolParamType.BOOLEAN,
                "是否使用人物参考；配置开启时传 false 会拒绝",
                required=False,
            ),
            ToolParameterInfo("use_outfit_reference", ToolParamType.BOOLEAN, "使用服装参考", required=False),
            ToolParameterInfo("use_scene_reference", ToolParamType.BOOLEAN, "使用场景参考", required=False),
            ToolParameterInfo("force_new_outfit", ToolParamType.BOOLEAN, "强制新服装", required=False, default=False),
            ToolParameterInfo("force_new_scene", ToolParamType.BOOLEAN, "强制新场景", required=False, default=False),
            ToolParameterInfo("size", ToolParamType.STRING, "图片尺寸", required=False, default=""),
            ToolParameterInfo("model_id", ToolParamType.STRING, "模型覆盖", required=False, default=""),
        ],
    )
    async def handle_generate_photo(
        self,
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
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            self._require_enabled()
            invocation = invocation_context(kwargs)
            task = self.service.submit_photo(
                invocation,
                description=description,
                outfit_hint=outfit_hint,
                scene_hint=scene_hint,
                accessory_hint=accessory_hint,
                outfit_id=outfit_id,
                scene_id=scene_id,
                use_person_reference=use_person_reference,
                use_outfit_reference=use_outfit_reference,
                use_scene_reference=use_scene_reference,
                force_new_outfit=force_new_outfit,
                force_new_scene=force_new_scene,
                size=size,
                model_id=model_id,
            )
            return {"success": True, "task_id": task.id, "status": task.status.value}
        except Exception as exc:
            return self._tool_error(exc)

    @Tool(
        "manage_reference_gallery",
        brief_description="管理员维护参考图库",
        detailed_description="仅管理员可用；耗时操作进入后台队列。",
        parameters=[
            ToolParameterInfo(
                "operation",
                ToolParamType.STRING,
                "操作",
                required=True,
                enum_values=[
                    "list",
                    "show",
                    "extract",
                    "import",
                    "edit",
                    "retag",
                    "regenerate",
                    "replace",
                    "enable",
                    "disable",
                    "delete",
                ],
            ),
            ToolParameterInfo("category", ToolParamType.STRING, "分类", required=False, default=""),
            ToolParameterInfo("asset_id", ToolParamType.STRING, "参考图 ID", required=False, default=""),
            ToolParameterInfo("name", ToolParamType.STRING, "名称", required=False, default=""),
            ToolParameterInfo(
                "tags",
                ToolParamType.OBJECT,
                "人工标签",
                required=False,
                properties={},
                additional_properties=True,
            ),
            ToolParameterInfo(
                "source_message_id",
                ToolParamType.STRING,
                "通常留空；优先使用当前消息、引用消息或本聊天最近一张单图",
                required=False,
                default="",
            ),
            ToolParameterInfo("confirm_token", ToolParamType.STRING, "确认令牌", required=False, default=""),
        ],
    )
    async def handle_manage_reference_gallery(
        self,
        operation: str,
        category: str = "",
        asset_id: str = "",
        name: str = "",
        tags: dict[str, Any] | None = None,
        source_message_id: str = "",
        confirm_token: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            invocation = invocation_context(kwargs)
            self._require_admin(invocation)
            return await self._manage_reference(
                invocation,
                operation=operation,
                category=category,
                asset_id=asset_id,
                name=name,
                tags=tags or {},
                source_message_id=source_message_id,
                confirm_token=confirm_token,
            )
        except Exception as exc:
            return self._tool_error(exc)

    @Tool(
        "get_image_task_status",
        brief_description="查询当前聊天的图片任务状态",
        detailed_description="默认返回当前聊天最近任务，可选附带生成图片。",
        parameters=[
            ToolParameterInfo("task_id", ToolParamType.STRING, "任务 ID", required=False, default=""),
            ToolParameterInfo("include_image", ToolParamType.BOOLEAN, "附带图片", required=False, default=False),
        ],
    )
    async def handle_get_image_task_status(
        self,
        task_id: str = "",
        include_image: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            invocation = invocation_context(kwargs)
            return self.service.task_result(
                invocation,
                task_id,
                include_image=bool(include_image),
                is_admin=invocation.is_admin(self.config.plugin.admin_user_ids),
            )
        except Exception as exc:
            return self._tool_error(exc)

    @Command(
        "maitu_admin",
        description="写真插件管理员命令",
        pattern=r"^/maitu(?:\s+.*)?$",
    )
    async def handle_admin_command(self, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = str(kwargs.get("stream_id") or "")
        try:
            invocation = invocation_context(kwargs)
            self._require_admin(invocation)
            command = parse_admin_command(self._raw_command(kwargs), self.config.plugin.command_prefix)
            text = await self._dispatch_admin(command, invocation)
            if text:
                await self.ctx.send.text(text, invocation.stream_id)
            return True, text or "操作完成", 3
        except Exception as exc:
            text = self._error_text(exc)
            if stream_id:
                await self.ctx.send.text(text, stream_id)
            return False, text, 3

    async def _manage_reference(
        self,
        invocation: InvocationContext,
        *,
        operation: str,
        category: str = "",
        asset_id: str = "",
        name: str = "",
        tags: Mapping[str, Any] | None = None,
        source_message_id: str = "",
        confirm_token: str = "",
    ) -> dict[str, Any]:
        op = operation.strip().casefold()
        category_value = ReferenceCategory(category) if category.strip() else None
        if op == "list":
            assets = self.service.gallery.list_assets(category=category_value, limit=100)
            return {"success": True, "count": len(assets), "assets": [self._asset_data(item) for item in assets]}
        if op == "show":
            asset = self._resolve_asset(asset_id, category_value)
            data = asset.reference_path.read_bytes()
            return {
                "success": True,
                "asset": self._asset_data(asset),
                "content": f"参考图 {asset.id}",
                "content_items": [
                    {
                        "type": "image",
                        "data": base64.b64encode(data).decode("ascii"),
                        "mime_type": "image/jpeg",
                        "name": f"{asset.category.value}-{asset.id}.jpg",
                        "description": asset.name,
                    }
                ],
            }
        if op in {"extract", "import"}:
            if category_value is None:
                raise ValueError("extract/import 必须指定 category")
            image = await resolve_single_image(self.ctx, invocation, source_message_id=source_message_id)
            task = self.service.submit_reference_job(
                invocation,
                operation=op,
                category=category_value,
                name=name or f"{category_value.value}-{int(time.time())}",
                image=image,
                manual_tags=tags,
            )
            return {"success": True, "task_id": task.id, "status": task.status.value}
        if op == "edit":
            asset = self.service.gallery.edit(
                asset_id,
                name=name or None,
                manual_tags=tags,
            )
            return {"success": True, "asset": self._asset_data(asset)}
        if op in {"retag", "regenerate"}:
            asset = self.service.gallery.require(asset_id)
            task = self.service.submit_reference_job(
                invocation,
                operation=op,
                category=asset.category,
                asset_id=asset.id,
                name=asset.name,
            )
            return {"success": True, "task_id": task.id, "status": task.status.value}
        if op == "replace":
            asset = self.service.gallery.require(asset_id)
            image = await resolve_single_image(self.ctx, invocation, source_message_id=source_message_id)
            task = self.service.submit_reference_job(
                invocation,
                operation="replace",
                category=asset.category,
                asset_id=asset.id,
                name=name or asset.name,
                image=image,
                manual_tags=tags or asset.manual_tags,
            )
            return {"success": True, "task_id": task.id, "status": task.status.value}
        if op in {"enable", "disable"}:
            if op == "enable":
                asset = self.service.gallery.require(asset_id)
                validation = validate_reference_tags(asset.category, asset.effective_tags)
                if not validation.selectable:
                    raise ValueError("标签未通过 Schema/场景资格校验，不能启用")
                asset = self.service.gallery.enable(asset.id)
            else:
                asset = self.service.gallery.disable(asset_id)
            return {"success": True, "asset": self._asset_data(asset)}
        if op == "delete":
            asset = self.service.gallery.require(asset_id)
            action_key = f"delete:{asset.id}"
            if not self._consume_confirmation(confirm_token, action_key, invocation.user_id):
                token = self._issue_confirmation(action_key, invocation.user_id)
                return {
                    "success": False,
                    "requires_confirmation": True,
                    "confirm_token": token,
                    "expires_in_seconds": 300,
                    "content": "再次调用 delete 并传入 confirm_token 完成删除。",
                }
            deleted = self.service.gallery.soft_delete(asset.id)
            self._unlink_asset_files(deleted)
            return {"success": True, "deleted_asset_id": deleted.id}
        raise ValueError(f"不支持的图库操作: {op}")

    async def _dispatch_admin(self, command: AdminCommand, invocation: InvocationContext) -> str:
        if command.domain == "help":
            return help_text(self.config.plugin.command_prefix)
        if command.domain == "doctor":
            return self._doctor_text()
        if command.domain == "person":
            return await self._person_command(command, invocation)
        if command.domain == "ref":
            return await self._reference_command(command, invocation)
        if command.domain == "continuity":
            return self._continuity_command(command, invocation)
        if command.domain == "task":
            return self._task_command(command, invocation)
        raise CommandParseError("未知管理员命令")

    async def _person_command(self, command: AdminCommand, invocation: InvocationContext) -> str:
        person = self.service.gallery.get_person()
        if command.action in {"extract", "import"}:
            image = await resolve_single_image(self.ctx, invocation)
            task = self.service.submit_reference_job(
                invocation,
                operation=command.action,
                category=ReferenceCategory.PERSON,
                name=command.options.get("name", "人物参考"),
                image=image,
            )
            return f"人物参考任务已排队：{task.id}"
        if command.action == "generate":
            if person is not None:
                raise ValueError("已有人物参考图，如需重做请先执行「人物 清空」")
            nickname, personality = await self.service.load_host_identity()
            if not personality:
                raise ValueError("未能读取 MaiBot 人格设定，无法生成人物参考图")
            appearance_hint = command.options.get("appearance_hint", "").strip()
            task = self.service.submit_reference_job(
                invocation,
                operation="generate_person",
                category=ReferenceCategory.PERSON,
                name=command.options.get("name", "人物参考"),
                personality=personality,
                nickname=nickname,
                appearance_hint=appearance_hint,
            )
            return f"已按人格设定排队生成人物参考：{task.id}"
        if command.action == "show":
            if person is None:
                return "尚未设置人物参考图。可执行「人物 提取」「人物 导入」或「人物 生成」。"
            await self._send_asset(person, invocation.stream_id)
            return self._format_asset(person)
        if command.action == "regenerate":
            if person is None:
                raise ValueError("尚未设置人物参考图")
            task = self.service.submit_reference_job(
                invocation,
                operation="regenerate",
                category=ReferenceCategory.PERSON,
                asset_id=person.id,
                name=person.name,
            )
            return f"人物参考重生成任务已排队：{task.id}"
        if command.action == "clear":
            if person is None:
                return "尚未设置人物参考图。"
            token = command.options.get("confirm_token", command.args[0] if command.args else "")
            action_key = f"clear-person:{person.id}"
            if not self._consume_confirmation(token, action_key, invocation.user_id):
                issued = self._issue_confirmation(action_key, invocation.user_id)
                return (
                    f"危险操作，请在 5 分钟内再次执行：{self.config.plugin.command_prefix} 人物 清空 确认令牌={issued}"
                )
            deleted = self.service.gallery.soft_delete(person.id)
            self._unlink_asset_files(deleted)
            return "人物参考图已清空。"
        raise CommandParseError("人物 支持 提取/导入/生成/查看/重生成/清空")

    async def _reference_command(self, command: AdminCommand, invocation: InvocationContext) -> str:
        if command.action in {"extract", "import"}:
            if not command.args:
                raise CommandParseError("请指定 服装 或 场景")
            category = ReferenceCategory(command.args[0])
            if category == ReferenceCategory.PERSON:
                raise CommandParseError("人物参考请使用「人物」命令")
            image = await resolve_single_image(self.ctx, invocation)
            task = self.service.submit_reference_job(
                invocation,
                operation=command.action,
                category=category,
                name=command.options.get("name", f"{category.value}-{int(time.time())}"),
                image=image,
                manual_tags=parse_tags(command.options.get("tags")),
            )
            return f"参考图任务已排队：{task.id}"
        if command.action == "list":
            category = ReferenceCategory(command.args[0]) if command.args else None
            assets = self.service.gallery.list_assets(category=category, limit=100)
            if not assets:
                return "参考图库为空。"
            return "\n".join(self._format_asset(item) for item in assets)
        if not command.args:
            raise CommandParseError(f"参考 {command.action} 需要参考图 ID")
        asset = self.service.gallery.require(command.args[0])
        if command.action == "show":
            await self._send_asset(asset, invocation.stream_id)
            return self._format_asset(asset)
        if command.action == "edit":
            edited = self.service.gallery.edit(
                asset.id,
                name=command.options.get("name") or None,
                manual_tags=parse_tags(command.options.get("tags")),
            )
            return f"已更新：{self._format_asset(edited)}"
        if command.action in {"retag", "regenerate"}:
            task = self.service.submit_reference_job(
                invocation,
                operation=command.action,
                category=asset.category,
                asset_id=asset.id,
                name=asset.name,
            )
            return f"任务已排队：{task.id}"
        if command.action == "replace":
            image = await resolve_single_image(self.ctx, invocation)
            task = self.service.submit_reference_job(
                invocation,
                operation="replace",
                category=asset.category,
                asset_id=asset.id,
                name=command.options.get("name", asset.name),
                image=image,
                manual_tags=parse_tags(command.options.get("tags")) or asset.manual_tags,
            )
            return f"替换任务已排队：{task.id}"
        if command.action == "enable":
            validation = validate_reference_tags(asset.category, asset.effective_tags)
            if not validation.selectable:
                raise ValueError("标签未通过 Schema/场景资格校验，不能启用")
            return f"已启用：{self._format_asset(self.service.gallery.enable(asset.id))}"
        if command.action == "disable":
            return f"已停用：{self._format_asset(self.service.gallery.disable(asset.id))}"
        if command.action == "delete":
            token = command.options.get("confirm_token", command.args[1] if len(command.args) > 1 else "")
            key = f"delete:{asset.id}"
            if not self._consume_confirmation(token, key, invocation.user_id):
                issued = self._issue_confirmation(key, invocation.user_id)
                return (
                    "危险操作，请在 5 分钟内再次执行："
                    f"{self.config.plugin.command_prefix} 参考 删除 {asset.id} "
                    f"确认令牌={issued}"
                )
            deleted = self.service.gallery.soft_delete(asset.id)
            self._unlink_asset_files(deleted)
            return f"已删除参考图：{asset.id}"
        raise CommandParseError("未知参考操作")

    def _continuity_command(self, command: AdminCommand, invocation: InvocationContext) -> str:
        manager = self.service.continuity
        if command.action == "show":
            state = manager.get(invocation.scope_key)
            if state is None:
                return "当前聊天没有连续性记录。"
            return json.dumps(
                {
                    "scope_key": state.scope_key,
                    "local_date": state.local_date,
                    "scene_signature": state.scene_signature,
                    "outfit_id": state.outfit_id,
                    "scene_id": state.scene_id,
                    "pinned_outfit_id": state.pinned_outfit_id,
                    "pinned_scene_id": state.pinned_scene_id,
                    "last_photo_at": state.last_photo_at.isoformat(),
                },
                ensure_ascii=False,
            )
        if command.action == "reset":
            removed = manager.reset(invocation.scope_key, preserve_pins=False)
            return "连续性记录已重置。" if removed else "当前聊天没有连续性记录。"
        if command.action == "pin":
            if len(command.args) < 2:
                raise CommandParseError("用法：连续 固定 服装|场景 <参考图ID>")
            manager.pin(invocation.scope_key, command.args[0], command.args[1])
            return f"已固定 {command.args[0]} 参考图 {command.args[1]}。"
        if command.action == "unpin":
            category = command.args[0] if command.args else None
            manager.unpin(invocation.scope_key, category)
            return "已取消固定参考图。"
        raise CommandParseError("连续 支持 查看/重置/固定/取消固定")

    def _task_command(self, command: AdminCommand, invocation: InvocationContext) -> str:
        if command.action == "list":
            all_scopes = command.options.get("all", "false").casefold() in {"1", "true", "yes"}
            tasks = self.service.storage.list_tasks(
                scope_key=None if all_scopes else invocation.scope_key,
                limit=50,
            )
            if not tasks:
                return "没有任务记录。"
            return "\n".join(
                f"{item.id} {item.kind} {item.status.value} {item.created_at.isoformat()}" for item in tasks
            )
        task_id = command.args[0] if command.args else ""
        if command.action == "show":
            result = self.service.task_result(invocation, task_id, is_admin=True)
            return json.dumps(
                {key: value for key, value in result.items() if key != "content_items"}, ensure_ascii=False
            )
        if not task_id:
            raise CommandParseError(f"任务 {command.action} 需要任务 ID")
        if command.action == "retry":
            task = self.service.retry_task(task_id)
            return f"任务已重新排队：{task.id}"
        if command.action == "cancel":
            task = self.service.cancel_task(task_id)
            return f"任务已取消：{task.id}"
        raise CommandParseError("任务 支持 列表/查看/重试/取消")

    def _doctor_text(self) -> str:
        refs = self.service.gallery.list_assets(limit=10_000)
        queued = self.service.storage.list_tasks(statuses=(TaskStatus.QUEUED,), limit=10_000)
        return "\n".join(
            [
                "写真插件诊断：",
                f"插件启用：{self.config.plugin.enabled}",
                f"管理员数量：{len(self.config.plugin.admin_user_ids)}",
                f"Provider Base URL 已配置：{bool(self.config.openai.base_url.strip())}",
                f"API Key 已配置：{bool(self.config.openai.api_key.strip())}",
                f"生成模型：{self.config.openai.generation_model or '未配置'}",
                f"参考图目标上限：{self.config.references.max_bytes} bytes",
                f"参考图数量：{len(refs)}",
                f"排队任务数量：{len(queued)}",
                "SQLite journal_mode：WAL",
            ]
        )

    @property
    def service(self) -> PhotoStudioService:
        if self._service is None:
            raise RuntimeError("插件服务尚未加载")
        return self._service

    def _require_enabled(self) -> None:
        if not self.config.plugin.enabled:
            raise ValueError("写真插件当前已停用")

    def _require_admin(self, invocation: InvocationContext) -> None:
        if not invocation.is_admin(self.config.plugin.admin_user_ids):
            raise PermissionError("只有写真插件管理员可以执行此操作")

    def _resolve_asset(
        self,
        asset_id: str,
        category: ReferenceCategory | None,
    ) -> ReferenceAsset:
        if asset_id.strip():
            asset = self.service.gallery.require(asset_id.strip())
        elif category == ReferenceCategory.PERSON:
            asset = self.service.gallery.get_person()
            if asset is None:
                raise ValueError("尚未设置人物参考图")
        else:
            raise ValueError("show 必须指定 asset_id")
        if category is not None and asset.category != category:
            raise ValueError("参考图分类与请求不一致")
        return asset

    def _issue_confirmation(self, action: str, user_id: str) -> str:
        self._prune_confirmations()
        token = secrets.token_urlsafe(18)
        self._confirmations[token] = (time.monotonic() + 300.0, action, user_id, secrets.token_hex(4))
        return token

    def _consume_confirmation(self, token: str, action: str, user_id: str) -> bool:
        self._prune_confirmations()
        record = self._confirmations.pop(str(token or "").strip(), None)
        return bool(record and record[0] >= time.monotonic() and record[1] == action and record[2] == user_id)

    def _prune_confirmations(self) -> None:
        now = time.monotonic()
        self._confirmations = {token: value for token, value in self._confirmations.items() if value[0] >= now}

    @staticmethod
    def _asset_data(asset: ReferenceAsset) -> dict[str, Any]:
        return {
            "id": asset.id,
            "category": asset.category.value,
            "name": asset.name,
            "status": asset.status.value,
            "tags": asset.tags,
            "manual_tags": asset.manual_tags,
            "effective_tags": asset.effective_tags,
            "sha256": asset.sha256,
            "use_count": asset.use_count,
            "last_used_at": asset.last_used_at.isoformat() if asset.last_used_at else None,
            "created_at": asset.created_at.isoformat(),
            "source_task_id": asset.source_task_id,
        }

    @staticmethod
    def _format_asset(asset: ReferenceAsset) -> str:
        tags = json.dumps(asset.effective_tags, ensure_ascii=False, separators=(",", ":"))
        return f"{asset.id} [{asset.category.value}/{asset.status.value}] {asset.name} tags={tags}"

    async def _send_asset(self, asset: ReferenceAsset, stream_id: str) -> None:
        data = base64.b64encode(asset.reference_path.read_bytes()).decode("ascii")
        sent = await self.ctx.send.image(data, stream_id)
        if not sent:
            raise RuntimeError("参考图发送失败")

    @staticmethod
    def _unlink_asset_files(asset: ReferenceAsset) -> None:
        for path in {asset.source_path, asset.reference_path}:
            path.unlink(missing_ok=True)

    @staticmethod
    def _raw_command(kwargs: Mapping[str, Any]) -> str:
        for key in ("raw_message", "text", "plain_text"):
            value = kwargs.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        message = kwargs.get("message")
        if isinstance(message, Mapping):
            for key in ("processed_plain_text", "plain_text"):
                value = message.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        raise InvocationError("无法读取命令文本")

    @staticmethod
    def _error_text(exc: BaseException) -> str:
        text = str(exc).strip() or type(exc).__name__
        text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}", r"\1[REDACTED]", text)
        text = re.sub(r"(?i)(?:sk|key|token)-[A-Za-z0-9._~-]{8,}", "[REDACTED]", text)
        return text[:1000]

    @classmethod
    def _tool_error(cls, exc: BaseException) -> dict[str, Any]:
        return {"success": False, "error": cls._error_text(exc), "content": cls._error_text(exc)}


def create_plugin() -> MaiTuPhotoPlugin:
    return MaiTuPhotoPlugin()


__all__ = ["MaiTuPhotoPlugin", "create_plugin"]
