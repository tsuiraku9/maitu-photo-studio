"""Configuration models for the MaiTu photo plugin.

The module deliberately keeps all user-editable LLM text in the configuration
model.  MaiBot's runner turns these models into the WebUI schema and persists
the active values in the plugin-local ``config.toml``.
"""

from __future__ import annotations

from typing import Any, Literal

try:  # pragma: no cover - exercised in the real MaiBot runner
    from maibot_sdk import Field, PluginConfigBase
    from pydantic import model_validator
except ImportError:  # lightweight fallback used by local unit tests
    from pydantic import BaseModel, model_validator
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
        parts.append(f"运行时会替换这些占位符：{placeholders.strip()}。")
    else:
        parts.append("这段文本没有运行时占位符，请按字面编写。")
    rows = extra.pop("rows", 6)
    extra.setdefault("x-widget", "textarea")
    return _ui(label, " ".join(parts), rows=rows, **extra)


def _tool_text_ui(label: str, purpose: str, *, rows: int = 4) -> dict[str, Any]:
    """Build textarea metadata for Planner-facing tool text."""

    return _ui(
        label,
        f"{purpose.strip()} 这段文本没有运行时占位符；修改后需要重载插件。",
        **{"x-widget": "textarea", "rows": rows},
    )


class PluginSection(PluginConfigBase):
    """插件开关、管理员命令与权限配置。"""

    __ui_label__ = "插件与权限"
    __ui_icon__ = "photo_camera"
    config_version: str = Field(
        default="1.0.0",
        description="插件配置版本",
        json_schema_extra=_ui("配置版本", "用于配置迁移，请勿手动修改。"),
    )
    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra=_ui("启用插件", "关闭后插件不会接受新的生图或图库管理任务。"),
    )
    command_prefix: str = Field(
        default="/maitu",
        description="管理员命令前缀",
        json_schema_extra=_ui("管理员命令前缀", "必须以 / 开头；修改后需要重载插件。", placeholder="/maitu"),
    )
    admin_user_ids: list[str] = Field(
        default_factory=list,
        description="允许管理图库的用户 ID",
        json_schema_extra=_ui("管理员用户 ID", "填写可使用图库管理和全部 /maitu 命令的用户 ID；每项填写一个 ID。"),
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
            "OpenAI 接口地址", "OpenAI 兼容服务的 Base URL，可带或不带 /v1。", placeholder="https://api.example.com/v1"
        ),
    )
    api_key: str = Field(
        default="",
        description="API Key（诊断与日志不会输出）",
        json_schema_extra=_ui(
            "API 密钥", "用于调用生图服务；诊断和日志不会输出此值。", **{"x-widget": "password"}, placeholder="sk-..."
        ),
    )
    generation_model: str = Field(
        default="gpt-image-2",
        description="默认生图模型",
        json_schema_extra=_ui(
            "默认生图模型", "含人物写真与无人物环境照片默认使用的模型 ID。", placeholder="gpt-image-2"
        ),
    )
    reference_model: str = Field(
        default="gpt-image-2",
        description="参考图提取模型",
        json_schema_extra=_ui(
            "参考图处理模型", "从上传图或生成结果提取多角度参考板时使用的模型 ID。", placeholder="gpt-image-2"
        ),
    )
    generation_mode: Literal["images_api", "chat_completions"] = Field(
        default="images_api",
        description="照片生图模式",
        json_schema_extra=_ui("照片生图接口模式", "images_api 使用 Images API；chat_completions 使用多模态聊天接口。"),
    )
    reference_mode: Literal["images_api", "chat_completions"] = Field(
        default="images_api",
        description="参考图提取模式",
        json_schema_extra=_ui("参考图处理接口模式", "多参考图编辑和参考板提取所使用的接口模式。"),
    )
    request_timeout_seconds: float = Field(
        default=180.0,
        description="单次 HTTP 请求超时（秒）",
        json_schema_extra=_ui("请求超时（秒）", "等待单次模型请求完成的最长时间。"),
    )
    connect_timeout_seconds: float = Field(
        default=15.0,
        description="HTTP 建连超时（秒）",
        json_schema_extra=_ui("连接超时（秒）", "连接 OpenAI 兼容服务的最长等待时间。"),
    )
    max_response_bytes: int = Field(
        default=32 * 1024 * 1024,
        description="允许下载的单张结果上限",
        json_schema_extra=_ui("响应图片上限（字节）", "拒绝下载超过此大小的单张模型结果，防止异常响应占用过多内存。"),
    )


class ModelTaskSection(PluginConfigBase):
    """MaiBot 辅助模型任务及生成参数。"""

    __ui_label__ = "MaiBot 辅助模型"
    __ui_icon__ = "psychology"
    tagging_task_name: str = Field(
        default="vlm",
        description="自动标签使用的 MaiBot 模型任务名",
        json_schema_extra=_ui("自动标签模型任务", "MaiBot 模型配置中用于识图和生成结构化标签的任务名。"),
    )
    selection_task_name: str = Field(
        default="utils",
        description="图库选择使用的 MaiBot 模型任务名",
        json_schema_extra=_ui("图库选择模型任务", "MaiBot 模型配置中用于场景判断和候选参考图选择的任务名。"),
    )
    max_tokens: int = Field(
        default=2048,
        description="辅助模型最大输出 token 数",
        json_schema_extra=_ui("最大输出 Token 数", "自动标签、场景判断和图库选择请求允许的最大输出长度。"),
    )
    temperature: float = Field(
        default=0.1,
        description="辅助模型温度",
        json_schema_extra=_ui("模型温度", "辅助模型采样温度；较低值可提高结构化 JSON 的稳定性。"),
    )


class ReferenceSection(PluginConfigBase):
    """人物、服装和场景参考图库及入库压缩配置。"""

    __ui_label__ = "参考图库"
    __ui_icon__ = "collections"
    person_reference_enabled: bool = Field(
        default=True,
        description="含人物写真任务是否默认并强制使用全局人物参考图",
        json_schema_extra=_ui(
            "启用写真人物参考",
            "开启后，含人物的写真任务必须使用已启用的全局人物参考板，并始终作为第一张参考图；"
            "关闭后，含人物写真改为文字描述人物，不再要求人物参考板。"
            "不含人物的环境照片工具不受此项影响。",
        ),
    )
    outfit_reference_enabled: bool = Field(
        default=True,
        description="默认使用服装参考图",
        json_schema_extra=_ui("使用服装参考图", "写真任务默认从服装图库选择并传入参考板。"),
    )
    scene_reference_enabled: bool = Field(
        default=True,
        description="默认使用场景参考图",
        json_schema_extra=_ui("使用场景参考图", "写真任务仅在合格的室内私密小空间中选择场景参考板。"),
    )
    auto_extract_missing: bool = Field(
        default=True,
        description="缺少参考图时是否自动从结果提取",
        json_schema_extra=_ui("自动补充缺失参考图", "照片使用文字回退后，从成功结果异步提取缺少的服装或场景参考板。"),
    )
    auto_enable_generated_references: bool = Field(
        default=True,
        description="生成成功后是否立即启用新参考图",
        json_schema_extra=_ui("自动启用新参考图", "自动提取或管理员提取成功且标签有效时，立即允许新条目参与选择。"),
    )
    max_bytes: int = Field(
        default=480_000,
        description="参考图硬上限（字节，不得超过 500000）",
        json_schema_extra=_ui("参考图大小上限（字节）", "所有入库图片压缩后的硬上限；配置值不得超过 500000。"),
    )
    max_edge: int = Field(
        default=2048,
        description="参考图最长边上限",
        json_schema_extra=_ui("参考图最长边（像素）", "入库 JPEG 的宽和高中较长一边的最大像素数。"),
    )
    max_pixels: int = Field(
        default=40_000_000,
        description="解码时允许的最大像素数",
        json_schema_extra=_ui("解码像素上限", "拒绝解码总像素数超过此值的上传图片，用于限制内存占用。"),
    )

    def model_post_init(self, __context: Any) -> None:
        if not 1 <= self.max_bytes <= 500_000:
            raise ValueError("参考图大小上限必须位于 1..500000 字节")
        if self.max_edge < 1:
            raise ValueError("参考图最长边必须大于 0")


class ContinuitySection(PluginConfigBase):
    """按群聊或私聊流隔离的写真服装连续性配置。"""

    __ui_label__ = "群聊连续性"
    __ui_icon__ = "history"
    enabled: bool = Field(
        default=True,
        description="是否启用连续性选择",
        json_schema_extra=_ui("启用服装连续性", "按群聊记录最近写真，在场景未变化时优先复用同一套服装。"),
    )
    ttl_hours: float = Field(
        default=12.0,
        description="场景未变化时复用服装的有效时长",
        json_schema_extra=_ui("服装复用时长（小时）", "距本群上一张同场景照片不超过此时长时，优先复用原服装。"),
    )
    same_local_day: bool = Field(
        default=True,
        description="是否要求处于同一自然日",
        json_schema_extra=_ui("限制同一自然日", "启用后，即使未超过复用时长，跨自然日也会重新选择服装。"),
    )
    timezone: str = Field(
        default="Asia/Hong_Kong",
        description="连续性日期时区",
        json_schema_extra=_ui("连续性时区", "判断自然日边界使用的 IANA 时区名称。", placeholder="Asia/Hong_Kong"),
    )


class TaskSection(PluginConfigBase):
    """持久化后台任务队列与结果清理配置。"""

    __ui_label__ = "任务队列"
    __ui_icon__ = "queue"
    worker_count: int = Field(
        default=1,
        description="后台任务 worker 数量",
        json_schema_extra=_ui("后台工作进程数", "并行处理生成和图库任务的 Worker 数量；提高后可能增加并发计费。"),
    )
    poll_interval_seconds: float = Field(
        default=0.5,
        description="队列轮询间隔",
        json_schema_extra=_ui("队列轮询间隔（秒）", "后台 Worker 检查待处理任务的间隔。"),
    )
    result_retention_hours: int = Field(
        default=24,
        description="生图结果文件保留时长",
        json_schema_extra=_ui(
            "结果文件保留时间（小时）", "图片完成投递且衍生参考图处理结束后，非图库结果文件的保留时间。"
        ),
    )
    metadata_retention_days: int = Field(
        default=30,
        description="任务元数据保留时长",
        json_schema_extra=_ui("任务记录保留时间（天）", "已结束图片任务及其摘要元数据在数据库中的保留天数。"),
    )
    max_queue_size: int = Field(
        default=100,
        description="最大排队任务数",
        json_schema_extra=_ui("最大排队任务数", "待处理任务达到此数量后拒绝继续提交，避免队列无限增长。"),
    )


class OutputSection(PluginConfigBase):
    """图片投递、状态返回和 Planner 唤醒配置。"""

    __ui_label__ = "投递与 Planner"
    __ui_icon__ = "send"
    notify_planner: bool = Field(
        default=True,
        description="图片投递后是否唤起 Planner",
        json_schema_extra=_ui("通知 Planner", "图片发送成功或失败后追加上下文并主动唤醒 Planner。"),
    )
    include_image_in_status: bool = Field(
        default=True,
        description="状态工具是否允许返回图片内容",
        json_schema_extra=_ui("状态查询可返回图片", "允许状态工具通过 content_items 附带已生成图片供 Planner 观察。"),
    )
    notification_priority: str = Field(
        default="normal",
        description="Planner 主动任务优先级",
        json_schema_extra=_ui("Planner 通知优先级", "传给 Maisaka 主动触发能力的优先级字符串。", placeholder="normal"),
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
        json_schema_extra=_tool_text_ui("工具简短描述", "Planner 筛选工具时看到的简短用途。", rows=2),
    )
    detailed: str = Field(
        default="",
        description="工具详细描述",
        json_schema_extra=_tool_text_ui("工具详细描述", "Planner 决定如何调用工具时看到的完整说明。", rows=6),
    )
    parameters: dict[str, str] = Field(
        default_factory=dict,
        description="参数描述映射",
        json_schema_extra=_ui(
            "工具参数描述",
            "按参数名配置给 Planner 看的说明；键必须是工具参数名，例如 description、scene_hint。修改后需要重载插件。",
        ),
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
            "无人物环境照片 · 系统提示词",
            "不含 bot 本人的环境/景物/物品照片发送给生图模型的系统指令。",
        ),
    )
    scene_photo_user: str = Field(
        default=(
            "拍摄需求：{description}\n场景提示：{scene_prompt}\n参考图说明：{reference_labels}\n"
            "负面要求：{negative_prompt}\n"
            "要求：这是手机拍摄的真实生活照片，不要出现 bot 本人，不要生成插画或海报。"
        ),
        description="无人物环境照片用户提示词",
        json_schema_extra=_prompt_ui(
            "无人物环境照片 · 用户提示词",
            "拼在系统提示词后面，发给生图模型。",
            "{description} 规划器填写的完整拍摄需求；"
            "{scene_prompt} 场景参考标签或场景文字回退；"
            "{reference_labels} 本次实际传入的参考图角色 JSON；"
            "{negative_prompt} 下方「默认负面提示词」字段。",
        ),
    )
    photo_system: str = Field(
        default=(
            "你负责生成像真人用手机拍摄并发送的真实生活照片。"
            "严格保持参考人物身份、服装与合格场景的一致性；优先自然手持构图、真实皮肤质感、日常光线与轻微生活瑕疵，"
            "避免棚拍打光、商业海报、过度磨皮和明显 AI 痕迹。不要添加参考图中不存在的文字或水印。"
        ),
        description="含人物写真系统提示词",
        json_schema_extra=_prompt_ui(
            "含人物写真 · 系统提示词",
            "bot 本人出镜的写真任务发送给生图模型的系统指令。",
        ),
    )
    photo_user: str = Field(
        default=(
            "拍摄需求：{description}\n人物提示：{person_prompt}\n服装提示：{outfit_prompt}\n"
            "场景提示：{scene_prompt}\n参考图说明：{reference_labels}\n负面要求：{negative_prompt}\n"
            "要求：这是手机拍摄的真实生活照片，优先自然、随意、可直接发到聊天里的观感。"
        ),
        description="含人物写真用户提示词",
        json_schema_extra=_prompt_ui(
            "含人物写真 · 用户提示词",
            "拼在系统提示词后面，发给生图模型。",
            "{description} 规划器填写的完整拍摄需求；"
            "{person_prompt} 人物参考标签，或「人物文字回退模板」渲染结果；"
            "{outfit_prompt} 服装参考标签或「服装文字回退提示词」；"
            "{scene_prompt} 场景参考标签或「场景文字回退模板」；"
            "{reference_labels} 本次实际传入的参考图角色 JSON；"
            "{negative_prompt} 下方「默认负面提示词」字段。",
        ),
    )
    negative_prompt: str = Field(
        default=(
            "明显的 AI 痕迹、塑料皮肤、过度磨皮、错误手指、重复人物、文字、水印、畸形肢体、"
            "商业棚拍光、海报排版、插画风、二次元、超广角畸变夸张"
        ),
        description="默认负面提示词",
        json_schema_extra=_prompt_ui(
            "默认负面提示词",
            "未另外指定时追加到所有照片任务；会填入用户提示词里的 {negative_prompt}。",
        ),
    )
    person_prompt: str = Field(
        default="保持同一位成年人物的稳定身份特征，五官、发型、体型和肤色自然一致，像真实手机照片里的同一个人",
        description="无人物参考图时使用的人物描述",
        json_schema_extra=_prompt_ui(
            "人物文字描述",
            "人物参考关闭或未使用人物参考板时的人物身份与外观描述；会填入「人物文字回退模板」的 {person_prompt}。",
        ),
    )
    person_fallback_prompt: str = Field(
        default="{person_prompt}",
        description="无人物参考图时的人物提示词模板",
        json_schema_extra=_prompt_ui(
            "人物文字回退模板",
            "未传入人物参考板时渲染，结果写入写真用户提示词的 {person_prompt}。",
            "{person_prompt} 上方「人物文字描述」字段。",
            rows=3,
        ),
    )
    clothing_style_prompt: str = Field(
        default="自然合身的日常服装，符合场景和季节，材质与褶皱真实",
        description="无服装参考图时的服装风格提示词",
        json_schema_extra=_prompt_ui(
            "服装文字回退提示词",
            "没有合适服装参考板时写入写真用户提示词的 {outfit_prompt}。",
        ),
    )
    scene_fallback_prompt: str = Field(
        default="{scene_hint}",
        description="无场景参考图时的场景提示词",
        json_schema_extra=_prompt_ui(
            "场景文字回退模板",
            "没有合适场景参考板时渲染，结果写入用户提示词的 {scene_prompt}。",
            "{scene_hint} 规划器传入的场景/地点提示。",
            rows=3,
        ),
    )

    extract_person: str = Field(
        default=(
            "将输入人物图整理为 3×2 人物参考板：左列为纵向占两格的正面全身图，"
            "右侧四格依次为脸部正面、侧面、背面特写和可选配饰示意。"
            "保持同一人物身份、发型、体型和服装，不加入文字水印。"
        ),
        description="人物参考提取提示词",
        json_schema_extra=_prompt_ui(
            "人物参考板提取提示词",
            "将管理员上传的人物图整理为 3×2 多角度人物参考板。",
        ),
    )
    extract_outfit: str = Field(
        default=(
            "从输入照片中提取同一套服装，生成 2×2 参考板：正面、侧面、背面和服装细节。"
            "保持颜色、材质、版型和配件一致，使用干净背景，不生成文字水印。"
        ),
        description="服装参考提取提示词",
        json_schema_extra=_prompt_ui(
            "服装参考板提取提示词",
            "从原图提取同一套服装并生成 2×2 多角度参考板。",
        ),
    )
    extract_scene: str = Field(
        default=(
            "从输入照片中提取空的私密小空间场景，生成 2×2 参考板："
            "广角、左视角、右视角和平面图。只允许卧室、浴室或客厅等室内私人空间；"
            "咖啡店、商场、街道和公共场所不合格。移除人物和文字。"
        ),
        description="场景参考提取提示词",
        json_schema_extra=_prompt_ui(
            "场景参考板提取提示词",
            "从原图移除人物并生成含平面图的 2×2 私密场景参考板。",
        ),
    )
    tag_person: str = Field(
        default="".join(
            (
                "请分析人物参考板并只输出符合 Schema 的 JSON：",
                '{{"accessories":[],"appearance_summary":"","confidence":0}}。',
                "confidence 必须是 0 到 1 之间的小数，禁止使用 0 到 100 的百分制。",
            )
        ),
        description="人物标签提示词",
        json_schema_extra=_prompt_ui(
            "人物自动标签提示词",
            "视觉模型分析人物参考板时使用；必须要求模型仅返回符合约定 Schema 的 JSON。",
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
            "服装自动标签提示词",
            "视觉模型提取类型、穿着场景、季节和风格标签时使用。",
        ),
    )
    tag_scene: str = Field(
        default=(
            "请分析场景参考板并只输出符合 Schema 的 JSON："
            '{{"room_type":"","lighting":[],"time_of_day":"",'
            '"privacy_eligible":false,"scene_signature":"","confidence":0}}。'
            "confidence 必须是 0 到 1 之间的小数，禁止使用百分制。"
        ),
        description="场景标签提示词",
        json_schema_extra=_prompt_ui(
            "场景自动标签提示词",
            "视觉模型提取房间类型、光线、时段、资格和场景指纹时使用。",
        ),
    )
    scene_eligibility: str = Field(
        default=(
            "判断目标场景是否属于适合保存参考图的室内私密小空间。卧室、浴室、客厅合格；"
            "咖啡店、商场、街道、办公室等公共或开放场所不合格。只输出 JSON："
            '{{"eligible":false,"scene_signature":"","reason":""}}\n'
            "场景描述：{scene_hint}\n完整需求：{description}"
        ),
        description="目标场景资格与场景指纹判断提示词",
        json_schema_extra=_prompt_ui(
            "场景资格判断提示词",
            "约束哪些地点可使用或入库场景参考图。",
            "{scene_hint} 规划器传入的场景/地点提示；{description} 规划器填写的完整拍摄需求。",
        ),
    )
    select_references: str = Field(
        default=(
            "根据需求和候选参考图元数据选择最合适的 ID。若没有明确冲突，优先复用已有且最近使用的服装参考图；"
            "只输出符合 Schema 的 JSON："
            '{{"outfit_id":null,"scene_id":null,"reason":""}}\n'
            "需求：{description}\n候选：{candidate_json}"
        ),
        description="参考图选择提示词",
        json_schema_extra=_prompt_ui(
            "图库选择提示词",
            "辅助模型从候选元数据选择服装和场景。",
            "{description} 规划器填写的完整拍摄需求；{candidate_json} 候选参考图元数据 JSON，不含图片字节。",
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
            "将场景描述归一化，供连续性判断复用。",
            "{scene_hint} 规划器传入的场景/地点提示；若为空则回退为完整拍摄需求。",
        ),
    )
    planner_success: str = Field(
        default=(
            "图片生成任务 {task_id} 已完成，图片已成功发送到当前聊天流。"
            "请根据上下文决定是否自然回复，不要重复调用生图工具。"
        ),
        description="成功投递后的 Planner 意图",
        json_schema_extra=_prompt_ui(
            "成功投递通知提示词",
            "图片确认发送后唤醒 Planner 使用。",
            "{task_id} 本次图片任务 ID。",
            rows=3,
        ),
    )
    planner_failure: str = Field(
        default="图片生成任务 {task_id} 未能成功发送：{error}。请根据上下文向用户自然说明，不要假装图片已经发送。",
        description="投递失败后的 Planner 意图",
        json_schema_extra=_prompt_ui(
            "失败投递通知提示词",
            "生成或发送失败后唤醒 Planner 使用。",
            "{task_id} 本次图片任务 ID；{error} 对用户安全的失败摘要。",
            rows=3,
        ),
    )

    generate_scene_photo_brief: str = Field(
        default="生成一张不含 bot 本人的手机真实环境/景物/物品照片",
        description="无人物环境照片工具简短描述",
        json_schema_extra=_tool_text_ui(
            "无人物环境照片工具 · 简短描述",
            "对应工具 generate_scene_photo。Planner 筛选工具时看到的一句话。",
            rows=2,
        ),
    )
    generate_scene_photo_detailed: str = Field(
        default=(
            "当需要发送不含 bot 本人的真实手机照片时使用，例如房间一角、窗外、桌上的食物、路边风景、空镜。"
            "不会传入人物或服装参考图；可按需从图库选择合格室内私密场景参考。"
            "请把完整拍摄需求写进 description：主体、环境、光线、时间、构图、氛围和是否有路人/物品。"
            "工具立即返回 task_id，异步生成并发送；同一需求不要重复提交，可用状态工具查询。"
        ),
        description="无人物环境照片工具详细描述",
        json_schema_extra=_tool_text_ui(
            "无人物环境照片工具 · 详细描述",
            "对应工具 generate_scene_photo。Planner 决定如何调用时看到的完整说明。",
        ),
    )
    generate_scene_photo_description: str = Field(
        default="完整拍摄需求：主体、环境、光线、构图、氛围；画面不得出现 bot 本人",
        description="无人物环境照片工具 description 参数说明",
        json_schema_extra=_tool_text_ui(
            "无人物环境照片工具 · 参数 description",
            "对应工具参数 description，没有运行时占位符。",
            rows=2,
        ),
    )
    generate_scene_photo_scene_hint: str = Field(
        default="场景/地点提示，用于选择或文字描述场景",
        description="无人物环境照片工具 scene_hint 参数说明",
        json_schema_extra=_tool_text_ui(
            "无人物环境照片工具 · 参数 scene_hint",
            "对应工具参数 scene_hint。",
            rows=2,
        ),
    )
    generate_scene_photo_scene_id: str = Field(
        default="明确指定的场景参考 ID；无效或分类错误会直接失败",
        description="无人物环境照片工具 scene_id 参数说明",
        json_schema_extra=_tool_text_ui(
            "无人物环境照片工具 · 参数 scene_id",
            "对应工具参数 scene_id。",
            rows=2,
        ),
    )
    generate_scene_photo_use_scene_reference: str = Field(
        default="是否尝试使用场景参考图；省略时使用配置默认值",
        description="无人物环境照片工具 use_scene_reference 参数说明",
        json_schema_extra=_tool_text_ui(
            "无人物环境照片工具 · 参数 use_scene_reference",
            "对应工具参数 use_scene_reference。",
            rows=2,
        ),
    )
    generate_scene_photo_force_new_scene: str = Field(
        default="忽略连续性缓存，重新判断/选择场景",
        description="无人物环境照片工具 force_new_scene 参数说明",
        json_schema_extra=_tool_text_ui(
            "无人物环境照片工具 · 参数 force_new_scene",
            "对应工具参数 force_new_scene。",
            rows=2,
        ),
    )
    generate_scene_photo_size: str = Field(
        default="服务商支持的图片尺寸，留空用默认",
        description="无人物环境照片工具 size 参数说明",
        json_schema_extra=_tool_text_ui(
            "无人物环境照片工具 · 参数 size",
            "对应工具参数 size。",
            rows=2,
        ),
    )
    generate_scene_photo_model_id: str = Field(
        default="临时覆盖生成模型，留空用插件配置",
        description="无人物环境照片工具 model_id 参数说明",
        json_schema_extra=_tool_text_ui(
            "无人物环境照片工具 · 参数 model_id",
            "对应工具参数 model_id。",
            rows=2,
        ),
    )

    generate_photo_brief: str = Field(
        default="生成一张 bot 本人出镜的手机真实生活照片，并尽量保持服装与场景连续",
        description="含人物写真工具简短描述",
        json_schema_extra=_tool_text_ui(
            "含人物写真工具 · 简短描述",
            "对应工具 generate_photo。Planner 筛选工具时看到的一句话。",
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
            "含人物写真工具 · 详细描述",
            "对应工具 generate_photo。Planner 决定如何调用时看到的完整说明。",
        ),
    )
    generate_photo_description: str = Field(
        default="完整拍摄需求：动作、表情、构图、光线、氛围和画面中要发生的事",
        description="含人物写真工具 description 参数说明",
        json_schema_extra=_tool_text_ui(
            "含人物写真工具 · 参数 description",
            "对应工具参数 description。",
            rows=2,
        ),
    )
    generate_photo_outfit_hint: str = Field(
        default="服装类型、颜色、季节、风格或穿着场合",
        description="含人物写真工具 outfit_hint 参数说明",
        json_schema_extra=_tool_text_ui(
            "含人物写真工具 · 参数 outfit_hint",
            "对应工具参数 outfit_hint。",
            rows=2,
        ),
    )
    generate_photo_scene_hint: str = Field(
        default="地点与场景；仅卧室/浴室/客厅等私密小空间才会使用场景参考",
        description="含人物写真工具 scene_hint 参数说明",
        json_schema_extra=_tool_text_ui(
            "含人物写真工具 · 参数 scene_hint",
            "对应工具参数 scene_hint。",
            rows=2,
        ),
    )
    generate_photo_accessory_hint: str = Field(
        default="发饰、眼镜、包、手持物等配饰",
        description="含人物写真工具 accessory_hint 参数说明",
        json_schema_extra=_tool_text_ui(
            "含人物写真工具 · 参数 accessory_hint",
            "对应工具参数 accessory_hint。",
            rows=2,
        ),
    )
    generate_photo_outfit_id: str = Field(
        default="明确指定的服装参考 ID；无效会直接失败",
        description="含人物写真工具 outfit_id 参数说明",
        json_schema_extra=_tool_text_ui(
            "含人物写真工具 · 参数 outfit_id",
            "对应工具参数 outfit_id。",
            rows=2,
        ),
    )
    generate_photo_scene_id: str = Field(
        default="明确指定的场景参考 ID；无效会直接失败",
        description="含人物写真工具 scene_id 参数说明",
        json_schema_extra=_tool_text_ui(
            "含人物写真工具 · 参数 scene_id",
            "对应工具参数 scene_id。",
            rows=2,
        ),
    )
    generate_photo_use_person_reference: str = Field(
        default="是否使用人物参考；人物参考配置开启时只能省略或 true，传 false 会拒绝",
        description="含人物写真工具 use_person_reference 参数说明",
        json_schema_extra=_tool_text_ui(
            "含人物写真工具 · 参数 use_person_reference",
            "对应工具参数 use_person_reference。",
            rows=2,
        ),
    )
    generate_photo_use_outfit_reference: str = Field(
        default="是否使用服装参考；省略时用配置默认值",
        description="含人物写真工具 use_outfit_reference 参数说明",
        json_schema_extra=_tool_text_ui(
            "含人物写真工具 · 参数 use_outfit_reference",
            "对应工具参数 use_outfit_reference。",
            rows=2,
        ),
    )
    generate_photo_use_scene_reference: str = Field(
        default="是否使用场景参考；省略时用配置默认值",
        description="含人物写真工具 use_scene_reference 参数说明",
        json_schema_extra=_tool_text_ui(
            "含人物写真工具 · 参数 use_scene_reference",
            "对应工具参数 use_scene_reference。",
            rows=2,
        ),
    )
    generate_photo_force_new_outfit: str = Field(
        default="忽略本聊天服装连续性，强制重选服装",
        description="含人物写真工具 force_new_outfit 参数说明",
        json_schema_extra=_tool_text_ui(
            "含人物写真工具 · 参数 force_new_outfit",
            "对应工具参数 force_new_outfit。",
            rows=2,
        ),
    )
    generate_photo_force_new_scene: str = Field(
        default="忽略场景连续性，强制重选场景",
        description="含人物写真工具 force_new_scene 参数说明",
        json_schema_extra=_tool_text_ui(
            "含人物写真工具 · 参数 force_new_scene",
            "对应工具参数 force_new_scene。",
            rows=2,
        ),
    )
    generate_photo_size: str = Field(
        default="服务商支持的图片尺寸",
        description="含人物写真工具 size 参数说明",
        json_schema_extra=_tool_text_ui(
            "含人物写真工具 · 参数 size",
            "对应工具参数 size。",
            rows=2,
        ),
    )
    generate_photo_model_id: str = Field(
        default="临时覆盖生成模型",
        description="含人物写真工具 model_id 参数说明",
        json_schema_extra=_tool_text_ui(
            "含人物写真工具 · 参数 model_id",
            "对应工具参数 model_id。",
            rows=2,
        ),
    )

    gallery_brief: str = Field(
        default="管理员查询或维护人物、服装和场景参考图库",
        description="图库管理工具简短描述",
        json_schema_extra=_tool_text_ui(
            "图库管理工具 · 简短描述",
            "对应工具 manage_reference_gallery。",
            rows=2,
        ),
    )
    gallery_detailed: str = Field(
        default="仅插件管理员可用。提取、导入、重标和重生成操作会创建后台任务；删除需要五分钟有效的确认令牌。",
        description="图库管理工具详细描述",
        json_schema_extra=_tool_text_ui(
            "图库管理工具 · 详细描述",
            "对应工具 manage_reference_gallery。",
            rows=4,
        ),
    )
    gallery_operation: str = Field(
        default="list、show、extract、import、edit、retag、regenerate、enable、disable 或 delete",
        description="图库管理工具 operation 参数说明",
        json_schema_extra=_tool_text_ui(
            "图库管理工具 · 参数 operation",
            "对应工具参数 operation。",
            rows=2,
        ),
    )
    gallery_category: str = Field(
        default="person、outfit 或 scene",
        description="图库管理工具 category 参数说明",
        json_schema_extra=_tool_text_ui(
            "图库管理工具 · 参数 category",
            "对应工具参数 category。",
            rows=2,
        ),
    )
    gallery_asset_id: str = Field(
        default="参考图 ID",
        description="图库管理工具 asset_id 参数说明",
        json_schema_extra=_tool_text_ui(
            "图库管理工具 · 参数 asset_id",
            "对应工具参数 asset_id。",
            rows=2,
        ),
    )
    gallery_name: str = Field(
        default="参考图名称",
        description="图库管理工具 name 参数说明",
        json_schema_extra=_tool_text_ui(
            "图库管理工具 · 参数 name",
            "对应工具参数 name。",
            rows=2,
        ),
    )
    gallery_tags: str = Field(
        default="人工标签覆盖对象",
        description="图库管理工具 tags 参数说明",
        json_schema_extra=_tool_text_ui(
            "图库管理工具 · 参数 tags",
            "对应工具参数 tags。",
            rows=2,
        ),
    )
    gallery_source_message_id: str = Field(
        default="包含唯一一张图片的当前或引用消息 ID",
        description="图库管理工具 source_message_id 参数说明",
        json_schema_extra=_tool_text_ui(
            "图库管理工具 · 参数 source_message_id",
            "对应工具参数 source_message_id。",
            rows=2,
        ),
    )
    gallery_confirm_token: str = Field(
        default="危险操作的二次确认令牌",
        description="图库管理工具 confirm_token 参数说明",
        json_schema_extra=_tool_text_ui(
            "图库管理工具 · 参数 confirm_token",
            "对应工具参数 confirm_token。",
            rows=2,
        ),
    )

    status_brief: str = Field(
        default="查询当前聊天的图片任务状态",
        description="任务状态工具简短描述",
        json_schema_extra=_tool_text_ui(
            "任务状态工具 · 简短描述",
            "对应工具 get_image_task_status。",
            rows=2,
        ),
    )
    status_detailed: str = Field(
        default=(
            "不传任务 ID 时返回当前聊天最近的任务；普通用户不能查看其他聊天的任务。可选返回已生成图片供 Planner 观察。"
        ),
        description="任务状态工具详细描述",
        json_schema_extra=_tool_text_ui(
            "任务状态工具 · 详细描述",
            "对应工具 get_image_task_status。",
            rows=4,
        ),
    )
    status_task_id: str = Field(
        default="任务 ID；留空查询当前聊天最近任务",
        description="任务状态工具 task_id 参数说明",
        json_schema_extra=_tool_text_ui(
            "任务状态工具 · 参数 task_id",
            "对应工具参数 task_id。",
            rows=2,
        ),
    )
    status_include_image: str = Field(
        default="是否在 content_items 中附带已生成图片",
        description="任务状态工具 include_image 参数说明",
        json_schema_extra=_tool_text_ui(
            "任务状态工具 · 参数 include_image",
            "对应工具参数 include_image。",
            rows=2,
        ),
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
    """MaiTu 写真插件的完整配置。"""

    __ui_label__ = "写真插件配置"
    plugin: PluginSection = Field(default_factory=PluginSection, description="插件开关、命令与管理员权限")
    openai: OpenAISection = Field(default_factory=OpenAISection, description="OpenAI 兼容生图服务")
    model_tasks: ModelTaskSection = Field(default_factory=ModelTaskSection, description="MaiBot 辅助模型任务")
    references: ReferenceSection = Field(default_factory=ReferenceSection, description="参考图库与图片压缩")
    continuity: ContinuitySection = Field(default_factory=ContinuitySection, description="按聊天隔离的写真连续性")
    tasks: TaskSection = Field(default_factory=TaskSection, description="后台任务队列与清理策略")
    output: OutputSection = Field(default_factory=OutputSection, description="图片投递和 Planner 通知")
    prompts: PromptSection = Field(default_factory=PromptSection, description="所有可自定义的模型提示词与工具描述")


def config_to_dict(config: Any) -> dict[str, Any]:
    """Return a plain dictionary for SDK and test fallback instances."""

    if hasattr(config, "model_dump"):
        return config.model_dump(mode="python")
    if isinstance(config, dict):
        return dict(config)
    return {}
