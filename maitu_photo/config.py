"""麦麦写真插件的配置模型。

The module deliberately keeps all user-editable LLM text in the configuration
model.  MaiBot's runner turns these models into the WebUI schema and persists
the active values in the plugin-local ``config.toml``.
"""

from __future__ import annotations

import json
import re
import string
from typing import Any, Literal

try:  # pragma: no cover - exercised in the real MaiBot runner
    from maibot_sdk import Field, PluginConfigBase
    from pydantic import field_validator, model_validator
except ImportError:  # lightweight fallback used by local unit tests
    from pydantic import BaseModel, field_validator, model_validator
    from pydantic import Field as _PydanticField

    class PluginConfigBase(BaseModel):
        """Small compatibility base matching the SDK model surface."""

        model_config = {"extra": "ignore"}

    def Field(default: Any = ..., **kwargs: Any) -> Any:
        return _PydanticField(default, **kwargs)


def _ui(label: str, hint: str, **extra: Any) -> dict[str, Any]:
    """Build the WebUI metadata shared by every user-editable field."""

    return {"label": label, "hint": hint, **extra}


def _prompt_ui(label: str, purpose: str, placeholders: str = "", **extra: Any) -> dict[str, Any]:
    """Build textarea metadata that names the runtime placeholders for this field."""

    parts = [purpose.strip()]
    if placeholders.strip():
        parts.append(f"占位符：{placeholders.strip()}")
    rows = extra.pop("rows", 6)
    extra.setdefault("x-widget", "textarea")
    return _ui(label, " ".join(parts), rows=rows, **extra)


def _tool_text_ui(label: str, purpose: str, *, rows: int = 4) -> dict[str, Any]:
    """Build textarea metadata for Planner-facing tool text."""

    return _ui(label, purpose.strip(), **{"x-widget": "textarea", "rows": rows})


RequestMode = Literal["images_api", "images_json", "chat_completions"]
ImageQuality = Literal["", "auto", "low", "medium", "high", "xhigh", "max", "standard", "hd"]
ImageOutputFormat = Literal["", "png", "jpeg", "webp"]
ImageModeration = Literal["", "auto", "low"]
_REQUEST_MODE_HINT = (
    "按服务商选择，失败后不会改用其他方式。"
    "images_api：OpenAI Images，参考图用 multipart。"
    "images_json：参考图用 JSON（Grok Imagine 等）。"
    "chat_completions：多模态聊天生图（Gemini 等）。"
)

_IMAGE_SIZE_RE = re.compile(r"^[1-9]\d*x[1-9]\d*$")
_RESERVED_IMAGE_EXTRA_PARAMS = frozenset(
    {
        "image",
        "images",
        "messages",
        "model",
        "moderation",
        "n",
        "negative_prompt",
        "output_format",
        "prompt",
        "quality",
        "response_format",
        "size",
    }
)


_LEGACY_BROKEN_TAG_SCENE_PROMPT = (
    "请分析场景参考板并只输出符合 Schema 的 JSON："
    '{{"room_type":"","scene_signature":"","confidence":0}}。'
    "时间和光线由每次生图任务自行判断；confidence 必须是 0 到 1 之间的小数。"
)
_DEFAULT_TAG_SCENE_PROMPT = (
    "请分析场景参考板并只输出符合 Schema 的 JSON："
    '{{"room_type":"","privacy_eligible":false,"scene_signature":"","confidence":0}}。'
    "privacy_eligible 仅当参考板确实是卧室、浴室、客厅等室内私密小空间时为 true；"
    "confidence 必须是 0 到 1 之间的小数，禁止使用百分制。"
)


class PluginSection(PluginConfigBase):
    """插件开关、管理员命令与权限配置。"""

    __ui_label__ = "插件与权限"
    __ui_icon__ = "photo_camera"
    config_version: str = Field(
        default="1.6.0",
        description="插件配置版本",
        json_schema_extra=_ui("配置版本", "用于配置迁移，请勿手动修改。"),
    )
    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra=_ui("启用插件", "关闭后不再接受新的生图或图库任务。"),
    )
    command_prefix: str = Field(
        default="/maitu",
        description="管理员命令前缀",
        json_schema_extra=_ui("管理员命令前缀", "必须以 / 开头；修改后需要重载插件。", placeholder="/maitu"),
    )
    admin_user_ids: list[str] = Field(
        default_factory=list,
        description="允许管理图库的用户 ID",
        json_schema_extra=_ui(
            "管理员用户 ID",
            "每项填写 platform:user_id，例如 qq:123456。",
        ),
    )

    def model_post_init(self, __context: Any) -> None:
        if not self.command_prefix.strip().startswith("/"):
            raise ValueError("管理员命令前缀必须以 / 开头")


class OpenAISection(PluginConfigBase):
    """OpenAI 兼容生图服务的连接与模型配置。"""

    __ui_label__ = "OpenAI 兼容接口"
    __ui_icon__ = "cloud"
    base_url: str = Field(
        default="",
        description="接口 Base URL，可带或不带 /v1",
        json_schema_extra=_ui(
            "接口地址", "OpenAI 兼容服务的 Base URL，可带或不带 /v1。", placeholder="https://api.example.com/v1"
        ),
    )
    api_key: str = Field(
        default="",
        description="API Key（诊断与日志不会输出）",
        json_schema_extra=_ui(
            "API 密钥", "调用生图服务；日志不会输出此值。", **{"x-widget": "password"}, placeholder="sk-..."
        ),
    )
    generation_model: str = Field(
        default="gpt-image-2",
        description="默认生图模型",
        json_schema_extra=_ui("生图模型", "写真和环境照使用的模型 ID。", placeholder="gpt-image-2"),
    )
    reference_model: str = Field(
        default="gpt-image-2",
        description="参考图提取模型",
        json_schema_extra=_ui("参考图模型", "提取人物、服装、场景参考板使用的模型 ID。", placeholder="gpt-image-2"),
    )
    generation_mode: RequestMode = Field(
        default="images_api",
        description="照片生图请求方式",
        json_schema_extra=_ui("生图请求方式", _REQUEST_MODE_HINT),
    )
    reference_mode: RequestMode = Field(
        default="images_api",
        description="参考图提取请求方式",
        json_schema_extra=_ui("参考图请求方式", _REQUEST_MODE_HINT),
    )
    generation_size: str = Field(
        default="",
        description="写真和环境照的默认输出分辨率",
        json_schema_extra=_ui(
            "生图默认分辨率",
            "写真和环境照使用；留空由服务商决定，工具 size 参数优先。支持 auto 或 WIDTHxHEIGHT。",
            placeholder="1024x1024",
        ),
    )
    generation_quality: ImageQuality = Field(
        default="",
        description="写真和环境照的默认生成质量",
        json_schema_extra=_ui("生图默认质量", "留空由服务商决定；不同模型支持的质量档位可能不同。"),
    )
    generation_output_format: ImageOutputFormat = Field(
        default="",
        description="写真和环境照的默认输出格式",
        json_schema_extra=_ui("生图默认输出格式", "留空由服务商决定；OpenAI Images 支持 png、jpeg 和 webp。"),
    )
    generation_moderation: ImageModeration = Field(
        default="",
        description="写真和环境照的内容审核强度",
        json_schema_extra=_ui("生图审核强度", "留空由服务商决定；auto 为标准过滤，low 为较宽松过滤。"),
    )
    generation_extra_params: dict[str, Any] = Field(
        default_factory=dict,
        description="写真和环境照请求的额外 JSON 参数",
        json_schema_extra=_ui(
            "生图额外参数",
            "仅补充请求体字段，不能覆盖模型、提示词、分辨率、质量、输出格式等插件管理字段。",
            example={"background": "opaque"},
        ),
    )
    reference_size: str = Field(
        default="",
        description="人物、服装和场景参考板的默认输出分辨率",
        json_schema_extra=_ui(
            "参考板默认分辨率",
            "参考板生图使用；留空由服务商决定，管理员命令 size 参数优先。支持 auto 或 WIDTHxHEIGHT。",
            placeholder="1024x1024",
        ),
    )
    reference_quality: ImageQuality = Field(
        default="",
        description="人物、服装和场景参考板的默认生成质量",
        json_schema_extra=_ui("参考板默认质量", "留空由服务商决定；不同模型支持的质量档位可能不同。"),
    )
    reference_output_format: ImageOutputFormat = Field(
        default="",
        description="人物、服装和场景参考板的默认输出格式",
        json_schema_extra=_ui("参考板默认输出格式", "留空由服务商决定；OpenAI Images 支持 png、jpeg 和 webp。"),
    )
    reference_moderation: ImageModeration = Field(
        default="",
        description="人物、服装和场景参考板的内容审核强度",
        json_schema_extra=_ui("参考板审核强度", "留空由服务商决定；auto 为标准过滤，low 为较宽松过滤。"),
    )
    reference_extra_params: dict[str, Any] = Field(
        default_factory=dict,
        description="人物、服装和场景参考板请求的额外 JSON 参数",
        json_schema_extra=_ui(
            "参考板额外参数",
            "仅补充请求体字段，不能覆盖模型、提示词、分辨率、质量、输出格式等插件管理字段。",
            example={"background": "opaque"},
        ),
    )
    request_timeout_seconds: float = Field(
        default=180.0,
        description="单次 HTTP 请求超时（秒）",
        json_schema_extra=_ui("请求超时（秒）", "等待单次模型请求完成的最长时间。"),
    )
    connect_timeout_seconds: float = Field(
        default=15.0,
        description="HTTP 建连超时（秒）",
        json_schema_extra=_ui("连接超时（秒）", "连接生图服务的最长等待时间。"),
    )
    max_response_bytes: int = Field(
        default=32 * 1024 * 1024,
        description="允许下载的单张结果上限",
        json_schema_extra=_ui("响应图片上限（字节）", "超过此大小的单张结果会被拒绝下载。"),
    )

    generation_max_retries: int = Field(
        default=0,
        description="生图失败后的最大重试次数",
        json_schema_extra=_ui("生图重试次数", "仅网络错误、429、5xx 会重试；默认 0，避免重复计费。"),
    )
    generation_retry_backoff_seconds: float = Field(
        default=1.0,
        description="生图重试之间的基础等待时间（秒）",
        json_schema_extra=_ui("重试等待（秒）", "指数退避的基础等待时间，上限 60 秒。"),
    )

    @model_validator(mode="after")
    def _validate_generation_retry_settings(self) -> "OpenAISection":
        if not 0 <= self.generation_max_retries <= 5:
            raise ValueError("generation_max_retries 必须介于 0 和 5 之间")
        if not 0 <= self.generation_retry_backoff_seconds <= 60:
            raise ValueError("generation_retry_backoff_seconds 必须介于 0 和 60 秒之间")
        return self

    @field_validator("generation_size", "reference_size")
    @classmethod
    def _validate_image_size(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized and normalized != "auto" and not _IMAGE_SIZE_RE.fullmatch(normalized):
            raise ValueError("图片分辨率必须留空、使用 auto 或填写 WIDTHxHEIGHT")
        return normalized

    @field_validator("generation_extra_params", "reference_extra_params")
    @classmethod
    def _validate_image_extra_params(cls, value: dict[str, Any]) -> dict[str, Any]:
        conflicts = sorted(key for key in value if key.strip().casefold() in _RESERVED_IMAGE_EXTRA_PARAMS)
        if conflicts:
            raise ValueError(f"额外参数不能覆盖插件管理字段：{', '.join(conflicts)}")
        try:
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("额外参数必须是可序列化的 JSON 对象") from exc
        return dict(value)

    @field_validator("generation_max_retries", "generation_retry_backoff_seconds", mode="before")
    @classmethod
    def _reject_boolean_retry_settings(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("生图重试设置不能使用布尔值")
        return value


class ModelTaskSection(PluginConfigBase):
    """MaiBot 辅助模型任务及生成参数。"""

    __ui_label__ = "MaiBot 辅助模型"
    __ui_icon__ = "psychology"
    tagging_task_name: str = Field(
        default="vlm",
        description="自动标签使用的 MaiBot 模型任务名",
        json_schema_extra=_ui("自动标签模型", "MaiBot 里用于识图打标的任务名。"),
    )
    selection_task_name: str = Field(
        default="utils",
        description="图库选择使用的 MaiBot 模型任务名",
        json_schema_extra=_ui("图库选择模型", "MaiBot 里用于场景判断和挑选参考图的任务名。"),
    )
    max_tokens: int = Field(
        default=6400,
        description="辅助模型最大输出 token 数",
        json_schema_extra=_ui("最大输出 Token", "标签、场景判断和图库选择共用的输出上限。"),
    )
    temperature: float = Field(
        default=0.1,
        description="辅助模型温度",
        json_schema_extra=_ui("模型温度", "较低值更利于稳定输出 JSON。"),
    )


class ReferenceSection(PluginConfigBase):
    """人物、服装和场景参考图库及入库压缩配置。"""

    __ui_label__ = "参考图库"
    __ui_icon__ = "collections"
    person_reference_enabled: bool = Field(
        default=True,
        description="含人物写真任务是否优先使用全局人物参考图",
        json_schema_extra=_ui("使用人物参考图", "开启后，有人物板时作为第一张参考图。"),
    )
    require_person_reference: bool = Field(
        default=True,
        description="是否禁止人物参考缺失时的人格文字回退",
        json_schema_extra=_ui("强制人物参考图", "开启后没有人物板会拒绝任务；关闭则改用人格文字描述。"),
    )
    outfit_reference_enabled: bool = Field(
        default=True,
        description="默认使用服装参考图",
        json_schema_extra=_ui("使用服装参考图", "写真默认从服装图库挑选参考板。"),
    )
    scene_reference_enabled: bool = Field(
        default=True,
        description="默认使用场景参考图",
        json_schema_extra=_ui("使用场景参考图", "仅在合格的室内私密小空间使用场景参考板。"),
    )
    auto_extract_missing: bool = Field(
        default=True,
        description="缺少参考图时是否自动从结果提取",
        json_schema_extra=_ui("自动补齐参考图", "写真成功后，从结果图异步提取本次缺少的服装或场景参考。"),
    )
    auto_enable_generated_references: bool = Field(
        default=True,
        description="生成成功后是否立即启用新参考图",
        json_schema_extra=_ui("自动启用新参考图", "提取成功且标签有效时，立即允许新条目参与选择。"),
    )
    max_bytes: int = Field(
        default=480_000,
        description="参考图硬上限（字节，不得超过 500000）",
        json_schema_extra=_ui("参考图大小上限（字节）", "入库压缩后的硬上限，不得超过 500000。"),
    )
    max_edge: int = Field(
        default=2048,
        description="参考图最长边上限",
        json_schema_extra=_ui("参考图最长边（像素）", "入库图片较长一边的最大像素数。"),
    )
    max_pixels: int = Field(
        default=40_000_000,
        description="解码时允许的最大像素数",
        json_schema_extra=_ui("解码像素上限", "超过此总像素的上传图会拒绝解码。"),
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_planner_gallery_flag(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        migrated = dict(data)
        migrated.pop("planner_gallery_management_enabled", None)
        return migrated

    def model_post_init(self, __context: Any) -> None:
        if not 1 <= self.max_bytes <= 500_000:
            raise ValueError("参考图大小上限必须位于 1..500000 字节")
        if self.max_edge < 1:
            raise ValueError("参考图最长边必须大于 0")


class ContinuitySection(PluginConfigBase):
    """按群聊或私聊流隔离的服装与场景参考连续性配置。"""

    __ui_label__ = "参考图连续性"
    __ui_icon__ = "history"
    enabled: bool = Field(
        default=True,
        description="是否启用连续性选择",
        json_schema_extra=_ui("启用连续性", "同一聊天、同一场景时优先复用上次的服装和场景参考。"),
    )
    ttl_hours: float = Field(
        default=12.0,
        description="场景未变化时复用参考图的有效时长",
        json_schema_extra=_ui("复用时长（小时）", "同场景照片在此时长内优先复用上次参考图。"),
    )
    same_local_day: bool = Field(
        default=True,
        description="是否要求处于同一自然日",
        json_schema_extra=_ui("限制同一自然日", "跨自然日会重新选择服装和场景参考。"),
    )
    timezone: str = Field(
        default="Asia/Hong_Kong",
        description="连续性日期时区",
        json_schema_extra=_ui("时区", "判断自然日边界使用的 IANA 时区。", placeholder="Asia/Hong_Kong"),
    )


class TaskSection(PluginConfigBase):
    """持久化后台任务队列与结果清理配置。"""

    __ui_label__ = "任务队列"
    __ui_icon__ = "queue"
    worker_count: int = Field(
        default=1,
        description="后台任务 worker 数量",
        json_schema_extra=_ui("后台工作进程数", "并行处理生图和图库任务的数量；提高可能增加并发计费。"),
    )
    poll_interval_seconds: float = Field(
        default=0.5,
        description="队列轮询间隔",
        json_schema_extra=_ui("队列轮询间隔（秒）", "后台检查待处理任务的间隔。"),
    )
    result_retention_hours: int = Field(
        default=24,
        description="生图结果文件保留时长",
        json_schema_extra=_ui("结果保留（小时）", "投递完成后，非图库结果文件的保留时间。"),
    )
    metadata_retention_days: int = Field(
        default=30,
        description="任务元数据保留时长",
        json_schema_extra=_ui("任务记录保留（天）", "已结束任务元数据在数据库中的保留天数。"),
    )
    max_queue_size: int = Field(
        default=100,
        description="最大排队任务数",
        json_schema_extra=_ui("最大排队数", "待处理任务达到此数量后拒绝新提交。"),
    )


class OutputSection(PluginConfigBase):
    """图片投递、状态返回和 Planner 唤醒配置。"""

    __ui_label__ = "投递与 Planner"
    __ui_icon__ = "send"
    notify_planner: bool = Field(
        default=True,
        description="图片投递后是否唤起 Planner",
        json_schema_extra=_ui("通知 Planner", "发送成功或失败后追加上下文并唤醒 Planner。"),
    )
    include_image_in_status: bool = Field(
        default=True,
        description="状态工具是否允许返回图片内容",
        json_schema_extra=_ui("状态可返回图片", "允许状态查询附带已生成图片供 Planner 观察。"),
    )
    notification_priority: str = Field(
        default="normal",
        description="Planner 主动任务优先级",
        json_schema_extra=_ui("通知优先级", "传给主动触发能力的优先级。", placeholder="normal"),
    )


class LoggingSection(PluginConfigBase):
    """叠加在 MaiBot 宿主日志上的插件生命周期日志控制。"""

    __ui_label__ = "日志与诊断"
    __ui_icon__ = "receipt_long"
    enabled: bool = Field(
        default=True,
        description="是否输出插件生命周期日志",
        json_schema_extra=_ui("启用插件日志", "输出入队、生成、投递和失败等事件；不含密钥、完整提示词或图片。"),
    )
    minimum_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="插件日志最低输出级别",
        json_schema_extra=_ui("日志最低级别", "日常用 INFO；DEBUG 更细，WARNING/ERROR 只留异常。"),
    )


class ToolDescriptionSection(PluginConfigBase):
    """暴露给 Planner 的单个工具描述。

    Kept as a nested model so persisted ``config.toml`` and runtime access stay
    unchanged.  WebUI cannot render nested objects, so PromptSection also
    exposes the same values as flattened textarea/key fields.
    """

    brief: str = Field(
        default="",
        description="工具简短描述",
        json_schema_extra=_tool_text_ui("工具简介", "Planner 筛选工具时看到的一句话。", rows=2),
    )
    detailed: str = Field(
        default="",
        description="工具详细描述",
        json_schema_extra=_tool_text_ui("工具详细说明", "Planner 决定如何调用时看到的完整说明。", rows=6),
    )
    parameters: dict[str, str] = Field(
        default_factory=dict,
        description="参数描述映射",
        json_schema_extra=_ui("工具参数说明", "按参数名写给 Planner 看的说明，例如 description、scene_hint。"),
    )


class PromptSection(PluginConfigBase):
    """生图、参考图处理、辅助模型和 Planner 使用的全部提示词。"""

    __ui_label__ = "LLM 提示词"
    __ui_icon__ = "edit_note"

    scene_photo_system: str = Field(
        default=(
            "你负责生成像真人用手机随手拍摄的真实照片，画面中不得出现 bot 本人或任何可识别的固定主角。"
            "优先还原手机摄影的自然感：轻微手持抖动感、真实景深、普通镜头透视、生活化构图与光线，"
            "避免棚拍感、海报构图、过度美颜和明显 AI 痕迹。不要添加文字或水印。"
        ),
        description="无人物环境照片系统提示词",
        json_schema_extra=_prompt_ui(
            "环境照 · 系统提示词",
            "不含 bot 本人的环境、景物或物品照发给生图模型的系统指令。",
        ),
    )
    scene_photo_user: str = Field(
        default=(
            "拍摄需求：{description}\n场景提示：{scene_prompt}\n"
            "负面要求：{negative_prompt}\n"
            "要求：这是手机拍摄的真实生活照片，不要出现 bot 本人，不要生成插画或海报。"
        ),
        description="无人物环境照片用户提示词",
        json_schema_extra=_prompt_ui(
            "环境照 · 用户提示词",
            "拼在系统提示词后发给生图模型。",
            "{description}、{scene_prompt}、{negative_prompt}",
        ),
    )
    photo_system: str = Field(
        default=(
            "你负责生成像真人用手机拍摄并发送的真实生活照片。"
            "严格保持参考人物身份、服装与合格场景的一致性；"
            "优先自然手持构图、真实皮肤质感、日常光线与轻微生活瑕疵，"
            "避免棚拍打光、商业海报、过度磨皮和明显 AI 痕迹。不要添加参考图中不存在的文字或水印。"
        ),
        description="含人物写真系统提示词",
        json_schema_extra=_prompt_ui(
            "写真 · 系统提示词",
            "bot 本人出镜的写真发给生图模型的系统指令。",
        ),
    )
    photo_user: str = Field(
        default=(
            "拍摄需求：{description}\n人物提示：{person_prompt}\n服装提示：{outfit_prompt}\n"
            "场景提示：{scene_prompt}\n负面要求：{negative_prompt}\n"
            "要求：这是手机拍摄的真实生活照片，优先自然、随意、可直接发到聊天里的观感。"
        ),
        description="含人物写真用户提示词",
        json_schema_extra=_prompt_ui(
            "写真 · 用户提示词",
            "拼在系统提示词后发给生图模型。",
            "{description}、{person_prompt}、{outfit_prompt}、{scene_prompt}、{negative_prompt}",
        ),
    )

    @field_validator("scene_photo_user", "photo_user")
    @classmethod
    def _reject_reference_labels_placeholder(cls, value: str) -> str:
        for _, field_name, _, _ in string.Formatter().parse(value):
            if field_name and field_name.split(".", 1)[0].split("[", 1)[0] == "reference_labels":
                raise ValueError("reference_labels 已不再支持；请删除该占位符，仅保留参考图约束")
        return value

    negative_prompt: str = Field(
        default=(
            "明显的 AI 痕迹、塑料皮肤、过度磨皮、错误手指、重复人物、文字、水印、畸形肢体、"
            "商业棚拍光、海报排版、插画风、二次元、超广角畸变夸张"
        ),
        description="默认负面提示词",
        json_schema_extra=_prompt_ui(
            "默认负面提示词",
            "追加到所有照片任务，填入用户提示词的 {negative_prompt}。",
        ),
    )
    person_prompt: str = Field(
        default=(
            "保持同一位成年人物的稳定身份特征，五官、发型、体型和肤色自然一致，像真实手机照片里的同一个人"
            # Reference images and outfit prompts own clothing, not this fallback identity text.
        ),
        description="无人物参考图时的基础人物描述",
        json_schema_extra=_prompt_ui(
            "无人物参考 · 基础外貌",
            "没有人物板时的基础外貌约束，不要写服装。",
        ),
    )
    person_fallback_prompt: str = Field(
        default=(
            "本次没有人物参考图。必须让出镜人物符合 MaiBot 的固定身份与人格，不要随意更换人物。\\n"
            "昵称：{nickname}\\n人格设定：{personality}\\n基础外貌约束：{person_prompt}"
        ),
        description="无人物参考图时的人格人物提示词模板",
        json_schema_extra=_prompt_ui(
            "无人物参考 · 回退模板",
            "没有人物板时渲染，结果写入 {person_prompt}。",
            "{nickname}、{personality}、{person_prompt}",
            rows=5,
        ),
    )
    person_reference_prompt: str = Field(
        default="严格保持人物参考图的面部与身份一致，不要根据人物参考图推断或复制服装。",
        description="有人物参考图时写入 {person_prompt} 的约束",
        json_schema_extra=_prompt_ui(
            "有人物参考时的约束",
            "有人物板时写入写真用户提示词的 {person_prompt}。",
            rows=3,
        ),
    )
    clothing_style_prompt: str = Field(
        default="自然合身的日常服装，符合本次地点、季节、活动与人格气质；材质、剪裁和褶皱真实",
        description="无服装参考图时的服装风格提示词",
        json_schema_extra=_prompt_ui(
            "无服装参考 · 文字回退",
            "没有服装板时写入 {outfit_prompt}。",
        ),
    )
    outfit_reference_prompt: str = Field(
        default="服装完全由服装参考图控制，不要使用人物参考图中的衣服。",
        description="有服装参考图时写入 {outfit_prompt} 的约束",
        json_schema_extra=_prompt_ui(
            "有服装参考时的约束",
            "有服装板时写入写真用户提示词的 {outfit_prompt}。",
            rows=3,
        ),
    )
    scene_fallback_prompt: str = Field(
        default="按本次拍摄需求还原地点、时间、光线、天气、固定布局与生活氛围：{scene_hint}",
        description="无场景参考图时的场景提示词",
        json_schema_extra=_prompt_ui(
            "无场景参考 · 回退模板",
            "没有场景板时渲染，结果写入 {scene_prompt}。",
            "{scene_hint}",
            rows=3,
        ),
    )
    scene_reference_prompt: str = Field(
        default=("场景参考图仅用于还原空间结构、固定布局与关键建模细节；时间和光线应根据本次拍摄需求自行判断补充。"),
        description="有场景参考图时写入 {scene_prompt} 的约束",
        json_schema_extra=_prompt_ui(
            "有场景参考时的约束",
            "有场景板时写入用户提示词的 {scene_prompt}。",
            rows=3,
        ),
    )

    extract_person: str = Field(
        default=(
            "将输入人物图整理为 3×2 人物参考板：左列为纵向占两格的正面全身特写，"
            "右侧四格依次为无配饰的脸部正面、侧面和背面特写和可选配饰示意图。"
            "只保留同一人物的五官、发型、肤色、年龄感和面部轮廓；"
            "穿着无特点的灰色分体贴身衣，不要根据原图复制上衣、裙子、外套或配饰。"
            "不加入文字水印。"
        ),
        description="人物参考提取提示词",
        json_schema_extra=_prompt_ui(
            "人物参考提取提示词",
            "把上传的人物图整理成身份参考板，不要保留服装。",
        ),
    )
    generate_person_from_personality: str = Field(
        default=(
            "根据以下 bot 人格与身份设定，生成一张 3×2 面部身份参考板："
            "左列为纵向占两格的正面全身特写，右侧四格依次为无配饰的脸部正面、侧面和背面特写和可选配饰示意图。"
            "只保留同一人物的五官、发型、肤色、年龄感和面部轮廓；"
            "穿着无特点的灰色分体贴身衣，不要根据原图复制上衣、裙子、外套或配饰。不加入文字水印。\n"
            "昵称：{nickname}\n人格设定：{personality}\n外貌补充：{appearance_hint}"
        ),
        description="按人格设定生成人物参考板提示词",
        json_schema_extra=_prompt_ui(
            "按人格生成人物参考",
            "没有人物图时，按 MaiBot 人格生成身份参考板。",
            "{nickname}、{personality}、{appearance_hint}",
        ),
    )
    extract_outfit: str = Field(
        default=(
            "从输入照片中提取同一套服装参考图，生成 2×2 参考板：正面、侧面、背面和服装细节。"
            "保持颜色、材质、版型和配件一致，不出现人物，使用干净背景，不生成文字水印。"
        ),
        description="服装参考提取提示词",
        json_schema_extra=_prompt_ui(
            "服装参考提取提示词",
            "从原图提取同一套服装，生成多角度参考板。",
        ),
    )
    extract_scene: str = Field(
        default="从输入照片中提取空场景参考图，生成 2×2 参考板：广角、多视角和平面图。",
        description="场景参考提取提示词",
        json_schema_extra=_prompt_ui(
            "场景参考提取提示词",
            "从原图移除人物，生成私密场景参考板。",
        ),
    )
    tag_person: str = Field(
        default="".join(
            (
                "请分析人物参考板并只输出符合 Schema 的 JSON：",
                '{{"appearance_summary":"","confidence":0}}。',
                "appearance_summary 只写五官、发型、肤色、年龄感和面部轮廓，禁止出现服装或配饰。"
                "confidence 必须是 0 到 1 之间的小数。",
            )
        ),
        description="人物标签提示词",
        json_schema_extra=_prompt_ui(
            "人物自动标签",
            "分析人物参考板；只描述面部与身份，仅返回约定 JSON。",
        ),
    )
    tag_outfit: str = Field(
        default=(
            "请分析服装参考板并只输出符合 Schema 的 JSON："
            '{{"type":"","wearing_scenes":[],"seasons":[],"styles":[],"confidence":0}}。'
            "confidence 必须是 0 到 1 之间的小数，禁止使用百分制。"
        ),
        description="服装标签提示词",
        json_schema_extra=_prompt_ui(
            "服装自动标签",
            "从服装参考板提取类型、场景、季节和风格。",
        ),
    )
    tag_scene: str = Field(
        default=_DEFAULT_TAG_SCENE_PROMPT,
        description="场景标签提示词",
        json_schema_extra=_prompt_ui(
            "场景自动标签",
            "提取房间类型、私密资格和场景指纹；时间与光线由每次生图自行判断。",
        ),
    )
    scene_eligibility: str = Field(
        default=(
            "判断目标场景是否属于适合保存参考图的室内私密小空间。卧室、浴室、客厅合格；"
            "咖啡店、商场、街道、办公室等公共或开放场所不合格。只输出 JSON："
            '{{"eligible":false,"scene_signature":"","reason":""}}\n'
            "场景描述：{scene_hint}\n完整需求：{description}"
        ),
        description="目标场景文字资格与场景指纹判断提示词",
        json_schema_extra=_prompt_ui(
            "场景资格判断",
            "根据文字判断该地点能否使用或入库场景参考。",
            "{scene_hint}、{description}",
        ),
    )
    select_references: str = Field(
        default=(
            "根据需求和候选参考图元数据选择最合适的 ID。若没有明确冲突，优先复用已有且最近使用的服装参考图；"
            "如果没有明确匹配当前需求的服装候选，必须将 outfit_id 设为 null，禁止为了填充字段选择不合适的服装；"
            "服装为 null 时表示回退到文字服装提示；"
            "只输出符合 Schema 的 JSON："
            '{{"outfit_id":null,"scene_id":null,"reason":""}}\n'
            "需求：{description}\n候选：{candidate_json}"
        ),
        description="参考图选择提示词",
        json_schema_extra=_prompt_ui(
            "图库选择提示词",
            "从候选元数据里挑选服装和场景。",
            "{description}、{candidate_json}",
        ),
    )
    scene_signature: str = Field(
        default=(
            "只根据物理空间本身将场景描述归一化为稳定、简短的场景指纹，只输出 JSON："
            '{{"scene_signature":"","changed":false}}\n'
            "忽略人物身份、服装、配饰、动作、姿势、手持物、拍摄角度和构图；"
            "仅保留房间类型、固定布局、主要家具和不可移动的空间特征。\n"
            "场景描述：{scene_hint}"
        ),
        description="场景变化判断提示词",
        json_schema_extra=_prompt_ui(
            "场景指纹提示词",
            "把场景描述归一化，供连续性判断复用。",
            "{scene_hint}",
        ),
    )
    planner_success: str = Field(
        default=(
            "图片生成任务 {task_id} 已完成，图片已成功发送到当前聊天流。"
            "请根据上下文决定是否自然回复，不要重复调用生图工具。"
        ),
        description="成功投递后的 Planner 意图",
        json_schema_extra=_prompt_ui(
            "成功投递通知",
            "图片发送成功后唤醒 Planner。",
            "{task_id}",
            rows=3,
        ),
    )
    planner_failure: str = Field(
        default="图片生成任务 {task_id} 未能成功发送：{error}。请根据上下文向用户自然说明，不要假装图片已经发送。",
        description="投递失败后的 Planner 意图",
        json_schema_extra=_prompt_ui(
            "失败投递通知",
            "生成或发送失败后唤醒 Planner。",
            "{task_id}、{error}",
            rows=3,
        ),
    )

    generate_scene_photo_brief: str = Field(
        default="生成一张不含 bot 本人的手机真实环境/景物/物品照片",
        description="无人物环境照片工具简短描述",
        json_schema_extra=_tool_text_ui(
            "环境照 · 简介",
            "Planner 筛选 generate_scene_photo 时看到的一句话。",
            rows=2,
        ),
    )
    generate_scene_photo_detailed: str = Field(
        default=(
            "当需要发送不含 bot 本人的真实手机照片时使用，例如房间一角、窗外、桌上的食物、路边风景、空镜。"
            "不会传入人物或服装参考图；可按需从图库选择合格室内场景参考。"
            "请把完整拍摄需求写进 description：主体、环境、光线、时间、构图、氛围和是否有路人/物品。"
            "工具立即返回 task_id，异步生成并发送；同一需求不要重复提交，可用状态工具查询。"
        ),
        description="无人物环境照片工具详细描述",
        json_schema_extra=_tool_text_ui(
            "环境照 · 详细说明",
            "Planner 调用 generate_scene_photo 时看到的完整说明。",
        ),
    )
    generate_scene_photo_description: str = Field(
        default="完整拍摄需求：主体、环境、光线、构图、氛围；画面不得出现 bot 本人",
        description="无人物环境照片工具 description 参数说明",
        json_schema_extra=_tool_text_ui("环境照 · description", "拍摄需求参数。", rows=2),
    )
    generate_scene_photo_scene_hint: str = Field(
        default="场景/地点提示，用于选择或文字描述场景",
        description="无人物环境照片工具 scene_hint 参数说明",
        json_schema_extra=_tool_text_ui("环境照 · scene_hint", "场景或地点提示。", rows=2),
    )
    generate_scene_photo_scene_id: str = Field(
        default="明确指定的场景参考 ID；无效或分类错误会直接失败",
        description="无人物环境照片工具 scene_id 参数说明",
        json_schema_extra=_tool_text_ui("环境照 · scene_id", "指定场景参考 ID。", rows=2),
    )
    generate_scene_photo_use_scene_reference: str = Field(
        default="是否尝试使用场景参考图；省略时使用配置默认值",
        description="无人物环境照片工具 use_scene_reference 参数说明",
        json_schema_extra=_tool_text_ui("环境照 · use_scene_reference", "是否使用场景参考图。", rows=2),
    )
    generate_scene_photo_force_new_scene: str = Field(
        default="忽略连续性缓存，重新判断/选择场景",
        description="无人物环境照片工具 force_new_scene 参数说明",
        json_schema_extra=_tool_text_ui("环境照 · force_new_scene", "忽略连续性，重新选择场景。", rows=2),
    )
    generate_scene_photo_size: str = Field(
        default="服务商支持的图片尺寸，留空用默认",
        description="无人物环境照片工具 size 参数说明",
        json_schema_extra=_tool_text_ui("环境照 · size", "图片尺寸，留空用默认。", rows=2),
    )
    generate_scene_photo_model_id: str = Field(
        default="临时覆盖生成模型，留空用插件配置",
        description="无人物环境照片工具 model_id 参数说明",
        json_schema_extra=_tool_text_ui("环境照 · model_id", "临时覆盖生图模型。", rows=2),
    )

    generate_photo_brief: str = Field(
        default="生成一张 bot 本人出镜的手机真实生活照片，并尽量保持服装与场景连续",
        description="含人物写真工具简短描述",
        json_schema_extra=_tool_text_ui(
            "写真 · 简介",
            "Planner 筛选 generate_photo 时看到的一句话。",
            rows=2,
        ),
    )
    generate_photo_detailed: str = Field(
        default=(
            "当需要发送 bot 本人出现在画面中的真实手机照片时使用，例如自拍、被拍、生活随手拍。"
            "插件目标是让你像真人一样发手机照片，而不是插画或海报。"
            "请一次性给出完整详细需求：动作姿势、表情、服装、配饰、地点场景、光线时间、构图远近和氛围。"
            "若配置启用了人物参考，将强制使用已启用的全局人物参考板作为第一张参考图；"
            "若配置关闭人物参考，则改用文字人物描述，不再要求人物参考板。"
            "会积极复用本聊天近期同场景服装，并仅在合格室内私密小空间使用场景参考。"
            "工具立即返回 task_id，异步生成并发送；同一需求不要重复提交。"
        ),
        description="含人物写真工具详细描述",
        json_schema_extra=_tool_text_ui(
            "写真 · 详细说明",
            "Planner 调用 generate_photo 时看到的完整说明。",
        ),
    )
    generate_photo_description: str = Field(
        default="完整拍摄需求：动作、表情、构图、光线、氛围和画面中要发生的事",
        description="含人物写真工具 description 参数说明",
        json_schema_extra=_tool_text_ui("写真 · description", "拍摄需求参数。", rows=2),
    )
    generate_photo_outfit_hint: str = Field(
        default="服装类型、颜色、季节、风格或穿着场合",
        description="含人物写真工具 outfit_hint 参数说明",
        json_schema_extra=_tool_text_ui("写真 · outfit_hint", "服装类型、颜色或风格提示。", rows=2),
    )
    generate_photo_scene_hint: str = Field(
        default="地点与场景；仅卧室/浴室/客厅等私密小空间才会使用场景参考",
        description="含人物写真工具 scene_hint 参数说明",
        json_schema_extra=_tool_text_ui("写真 · scene_hint", "地点与场景提示。", rows=2),
    )
    generate_photo_accessory_hint: str = Field(
        default="发饰、眼镜、包、手持物等配饰",
        description="含人物写真工具 accessory_hint 参数说明",
        json_schema_extra=_tool_text_ui("写真 · accessory_hint", "手持物或随身物件，不要写服装。", rows=2),
    )
    generate_photo_outfit_id: str = Field(
        default="明确指定的服装参考 ID；无效会直接失败",
        description="含人物写真工具 outfit_id 参数说明",
        json_schema_extra=_tool_text_ui("写真 · outfit_id", "指定服装参考 ID。", rows=2),
    )
    generate_photo_scene_id: str = Field(
        default="明确指定的场景参考 ID；无效会直接失败",
        description="含人物写真工具 scene_id 参数说明",
        json_schema_extra=_tool_text_ui("写真 · scene_id", "指定场景参考 ID。", rows=2),
    )
    generate_photo_use_person_reference: str = Field(
        default="是否使用人物参考；人物参考配置开启时只能省略或 true，传 false 会拒绝",
        description="含人物写真工具 use_person_reference 参数说明",
        json_schema_extra=_tool_text_ui("写真 · use_person_reference", "是否使用人物参考图。", rows=2),
    )
    generate_photo_use_outfit_reference: str = Field(
        default="是否使用服装参考；省略时用配置默认值",
        description="含人物写真工具 use_outfit_reference 参数说明",
        json_schema_extra=_tool_text_ui("写真 · use_outfit_reference", "是否使用服装参考图。", rows=2),
    )
    generate_photo_use_scene_reference: str = Field(
        default="是否使用场景参考；省略时用配置默认值",
        description="含人物写真工具 use_scene_reference 参数说明",
        json_schema_extra=_tool_text_ui("写真 · use_scene_reference", "是否使用场景参考图。", rows=2),
    )
    generate_photo_force_new_outfit: str = Field(
        default="忽略本聊天服装连续性，强制重选服装",
        description="含人物写真工具 force_new_outfit 参数说明",
        json_schema_extra=_tool_text_ui("写真 · force_new_outfit", "忽略连续性，强制重选服装。", rows=2),
    )
    generate_photo_force_new_scene: str = Field(
        default="忽略场景连续性，强制重选场景",
        description="含人物写真工具 force_new_scene 参数说明",
        json_schema_extra=_tool_text_ui("写真 · force_new_scene", "忽略连续性，强制重选场景。", rows=2),
    )
    generate_photo_size: str = Field(
        default="服务商支持的图片尺寸",
        description="含人物写真工具 size 参数说明",
        json_schema_extra=_tool_text_ui("写真 · size", "图片尺寸。", rows=2),
    )
    generate_photo_model_id: str = Field(
        default="临时覆盖生成模型",
        description="含人物写真工具 model_id 参数说明",
        json_schema_extra=_tool_text_ui("写真 · model_id", "临时覆盖生图模型。", rows=2),
    )

    gallery_brief: str = Field(
        default="管理员查询或维护人物、服装和场景参考图库",
        description="图库管理工具简短描述",
        json_schema_extra=_tool_text_ui("图库 · 简介", "管理员维护参考图库时的简介。", rows=2),
    )
    gallery_detailed: str = Field(
        default="仅插件管理员可用。提取、导入、重标和重生成操作会创建后台任务；删除需要五分钟有效的确认令牌。",
        description="图库管理工具详细描述",
        json_schema_extra=_tool_text_ui("图库 · 详细说明", "图库管理工具的完整说明。", rows=4),
    )
    gallery_operation: str = Field(
        default="list、show、extract、import、edit、retag、regenerate、enable、disable 或 delete",
        description="图库管理工具 operation 参数说明",
        json_schema_extra=_tool_text_ui("图库 · operation", "图库操作类型。", rows=2),
    )
    gallery_category: str = Field(
        default="person、outfit 或 scene",
        description="图库管理工具 category 参数说明",
        json_schema_extra=_tool_text_ui("图库 · category", "参考图分类。", rows=2),
    )
    gallery_asset_id: str = Field(
        default="参考图 ID",
        description="图库管理工具 asset_id 参数说明",
        json_schema_extra=_tool_text_ui("图库 · asset_id", "参考图 ID。", rows=2),
    )
    gallery_name: str = Field(
        default="参考图名称",
        description="图库管理工具 name 参数说明",
        json_schema_extra=_tool_text_ui("图库 · name", "参考图名称。", rows=2),
    )
    gallery_tags: str = Field(
        default="人工标签覆盖对象",
        description="图库管理工具 tags 参数说明",
        json_schema_extra=_tool_text_ui("图库 · tags", "人工标签覆盖。", rows=2),
    )
    gallery_source_message_id: str = Field(
        default="包含唯一一张图片的当前或引用消息 ID",
        description="图库管理工具 source_message_id 参数说明",
        json_schema_extra=_tool_text_ui("图库 · source_message_id", "来源图片消息 ID。", rows=2),
    )
    gallery_confirm_token: str = Field(
        default="危险操作的二次确认令牌",
        description="图库管理工具 confirm_token 参数说明",
        json_schema_extra=_tool_text_ui("图库 · confirm_token", "危险操作的确认令牌。", rows=2),
    )

    status_brief: str = Field(
        default="查询当前聊天的图片任务状态",
        description="任务状态工具简短描述",
        json_schema_extra=_tool_text_ui(
            "状态查询 · 简介", "Planner 筛选 get_image_task_status 时看到的一句话。", rows=2
        ),
    )
    status_detailed: str = Field(
        default=(
            "不传任务 ID 时返回当前聊天最近的任务；普通用户不能查看其他聊天的任务。可选返回已生成图片供 Planner 观察。"
        ),
        description="任务状态工具详细描述",
        json_schema_extra=_tool_text_ui(
            "状态查询 · 详细说明", "Planner 调用 get_image_task_status 时看到的完整说明。", rows=4
        ),
    )
    status_task_id: str = Field(
        default="任务 ID；留空查询当前聊天最近任务",
        description="任务状态工具 task_id 参数说明",
        json_schema_extra=_tool_text_ui("状态查询 · task_id", "任务 ID；留空查当前聊天最近任务。", rows=2),
    )
    status_include_image: str = Field(
        default="是否在 content_items 中附带已生成图片",
        description="任务状态工具 include_image 参数说明",
        json_schema_extra=_tool_text_ui("状态查询 · include_image", "是否附带已生成图片。", rows=2),
    )

    @model_validator(mode="before")
    @classmethod
    def _flatten_legacy_tool_sections(cls, data: Any) -> Any:
        """Accept previously nested ToolDescriptionSection objects from config.toml."""

        if not isinstance(data, dict):
            return data
        migrated = dict(data)
        mappings = (
            (
                "generate_scene_photo_tool",
                "generate_scene_photo",
                (
                    "description",
                    "scene_hint",
                    "scene_id",
                    "use_scene_reference",
                    "force_new_scene",
                    "size",
                    "model_id",
                ),
            ),
            (
                "generate_photo_tool",
                "generate_photo",
                (
                    "description",
                    "outfit_hint",
                    "scene_hint",
                    "accessory_hint",
                    "outfit_id",
                    "scene_id",
                    "use_person_reference",
                    "use_outfit_reference",
                    "use_scene_reference",
                    "force_new_outfit",
                    "force_new_scene",
                    "size",
                    "model_id",
                ),
            ),
            (
                "gallery_tool",
                "gallery",
                (
                    "operation",
                    "category",
                    "asset_id",
                    "name",
                    "tags",
                    "source_message_id",
                    "confirm_token",
                ),
            ),
            ("status_tool", "status", ("task_id", "include_image")),
        )
        for nested_name, prefix, parameter_names in mappings:
            section = migrated.pop(nested_name, None)
            if not isinstance(section, dict):
                continue
            brief = str(section.get("brief") or "").strip()
            detailed = str(section.get("detailed") or "").strip()
            if brief:
                migrated[f"{prefix}_brief"] = brief
            if detailed:
                migrated[f"{prefix}_detailed"] = detailed
            parameters = section.get("parameters")
            if isinstance(parameters, dict):
                for parameter_name in parameter_names:
                    value = str(parameters.get(parameter_name) or "").strip()
                    if value:
                        migrated[f"{prefix}_{parameter_name}"] = value
        if str(migrated.get("tag_scene") or "").strip() == _LEGACY_BROKEN_TAG_SCENE_PROMPT:
            migrated["tag_scene"] = _DEFAULT_TAG_SCENE_PROMPT
        return migrated

    @property
    def generate_scene_photo_tool(self) -> ToolDescriptionSection:
        return ToolDescriptionSection(
            brief=self.generate_scene_photo_brief,
            detailed=self.generate_scene_photo_detailed,
            parameters={
                "description": self.generate_scene_photo_description,
                "scene_hint": self.generate_scene_photo_scene_hint,
                "scene_id": self.generate_scene_photo_scene_id,
                "use_scene_reference": self.generate_scene_photo_use_scene_reference,
                "force_new_scene": self.generate_scene_photo_force_new_scene,
                "size": self.generate_scene_photo_size,
                "model_id": self.generate_scene_photo_model_id,
            },
        )

    @property
    def generate_photo_tool(self) -> ToolDescriptionSection:
        return ToolDescriptionSection(
            brief=self.generate_photo_brief,
            detailed=self.generate_photo_detailed,
            parameters={
                "description": self.generate_photo_description,
                "outfit_hint": self.generate_photo_outfit_hint,
                "scene_hint": self.generate_photo_scene_hint,
                "accessory_hint": self.generate_photo_accessory_hint,
                "outfit_id": self.generate_photo_outfit_id,
                "scene_id": self.generate_photo_scene_id,
                "use_person_reference": self.generate_photo_use_person_reference,
                "use_outfit_reference": self.generate_photo_use_outfit_reference,
                "use_scene_reference": self.generate_photo_use_scene_reference,
                "force_new_outfit": self.generate_photo_force_new_outfit,
                "force_new_scene": self.generate_photo_force_new_scene,
                "size": self.generate_photo_size,
                "model_id": self.generate_photo_model_id,
            },
        )

    @property
    def gallery_tool(self) -> ToolDescriptionSection:
        return ToolDescriptionSection(
            brief=self.gallery_brief,
            detailed=self.gallery_detailed,
            parameters={
                "operation": self.gallery_operation,
                "category": self.gallery_category,
                "asset_id": self.gallery_asset_id,
                "name": self.gallery_name,
                "tags": self.gallery_tags,
                "source_message_id": self.gallery_source_message_id,
                "confirm_token": self.gallery_confirm_token,
            },
        )

    @property
    def status_tool(self) -> ToolDescriptionSection:
        return ToolDescriptionSection(
            brief=self.status_brief,
            detailed=self.status_detailed,
            parameters={
                "task_id": self.status_task_id,
                "include_image": self.status_include_image,
            },
        )


class PhotoPluginConfig(PluginConfigBase):
    """麦麦写真插件的完整配置。"""

    __ui_label__ = "麦麦写真配置"
    plugin: PluginSection = Field(default_factory=PluginSection, description="插件开关、命令与管理员权限")
    openai: OpenAISection = Field(default_factory=OpenAISection, description="OpenAI 兼容生图服务")
    model_tasks: ModelTaskSection = Field(default_factory=ModelTaskSection, description="MaiBot 辅助模型任务")
    references: ReferenceSection = Field(default_factory=ReferenceSection, description="参考图库与图片压缩")
    continuity: ContinuitySection = Field(default_factory=ContinuitySection, description="按聊天隔离的写真连续性")
    tasks: TaskSection = Field(default_factory=TaskSection, description="后台任务队列与清理策略")
    output: OutputSection = Field(default_factory=OutputSection, description="图片投递和 Planner 通知")
    logging: LoggingSection = Field(default_factory=LoggingSection, description="插件安全日志与诊断级别")
    prompts: PromptSection = Field(default_factory=PromptSection, description="所有可自定义的模型提示词与工具描述")


def config_to_dict(config: Any) -> dict[str, Any]:
    """Return a plain dictionary for SDK and test fallback instances."""

    if hasattr(config, "model_dump"):
        return config.model_dump(mode="python")
    if isinstance(config, dict):
        return dict(config)
    return {}
