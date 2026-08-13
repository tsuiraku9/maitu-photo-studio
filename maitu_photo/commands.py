"""Parser and help text for the administrator command surface."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from typing import Any


class CommandParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AdminCommand:
    domain: str
    action: str = ""
    args: tuple[str, ...] = ()
    options: dict[str, str] = field(default_factory=dict)


DOMAIN_ALIASES = {
    "person": "person",
    "人物": "person",
    "ref": "ref",
    "参考": "ref",
    "图库": "ref",
    "continuity": "continuity",
    "连续": "continuity",
    "连续性": "continuity",
    "task": "task",
    "任务": "task",
    "doctor": "doctor",
    "诊断": "doctor",
    "help": "help",
    "帮助": "help",
}

ACTION_ALIASES = {
    "extract": "extract",
    "提取": "extract",
    "import": "import",
    "导入": "import",
    "show": "show",
    "查看": "show",
    "显示": "show",
    "regenerate": "regenerate",
    "重生成": "regenerate",
    "clear": "clear",
    "清空": "clear",
    "generate": "generate",
    "生成": "generate",
    "list": "list",
    "列表": "list",
    "edit": "edit",
    "编辑": "edit",
    "retag": "retag",
    "重标": "retag",
    "replace": "replace",
    "替换": "replace",
    "enable": "enable",
    "启用": "enable",
    "disable": "disable",
    "停用": "disable",
    "delete": "delete",
    "删除": "delete",
    "reset": "reset",
    "重置": "reset",
    "pin": "pin",
    "固定": "pin",
    "unpin": "unpin",
    "取消固定": "unpin",
    "retry": "retry",
    "重试": "retry",
    "cancel": "cancel",
    "取消": "cancel",
}

CATEGORY_ALIASES = {
    "outfit": "outfit",
    "服装": "outfit",
    "clothes": "outfit",
    "scene": "scene",
    "场景": "scene",
    "person": "person",
    "人物": "person",
}

OPTION_ALIASES = {
    "name": "name",
    "名称": "name",
    "tags": "tags",
    "标签": "tags",
    "confirm_token": "confirm_token",
    "确认令牌": "confirm_token",
    "appearance_hint": "appearance_hint",
    "补充": "appearance_hint",
}

DOMAINS = frozenset(DOMAIN_ALIASES.values())


def parse_admin_command(text: str, prefix: str = "/maitu") -> AdminCommand:
    normalized_prefix = prefix.strip().rstrip("/")
    raw = str(text or "").strip()
    if raw == normalized_prefix:
        return AdminCommand("help")
    if not raw.startswith(normalized_prefix + " "):
        raise CommandParseError(f"命令必须以 {normalized_prefix} 开头")
    try:
        tokens = shlex.split(raw[len(normalized_prefix) :].strip(), posix=True)
    except ValueError as exc:
        raise CommandParseError(f"命令引号不完整: {exc}") from exc
    if not tokens:
        return AdminCommand("help")
    domain = _alias(tokens.pop(0), DOMAIN_ALIASES, "未知命令分组")
    if domain in {"doctor", "help"}:
        if tokens:
            raise CommandParseError(f"{_domain_label(domain)} 不接受额外参数")
        return AdminCommand(domain)
    action_token = tokens.pop(0) if tokens else "查看"
    action = _alias(action_token, ACTION_ALIASES, f"{_domain_label(domain)} 不支持的操作")
    args: list[str] = []
    options: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--"):
            key = _option_key(token[2:])
            if not key:
                raise CommandParseError("选项名不能为空")
            if index + 1 < len(tokens) and not tokens[index + 1].startswith("--"):
                options[key] = tokens[index + 1]
                index += 2
            else:
                options[key] = "true"
                index += 1
            continue
        if "=" in token:
            raw_key, value = token.split("=", 1)
            if raw_key and _is_option_name(raw_key):
                options[_option_key(raw_key)] = value
                index += 1
                continue
        args.append(_alias_value(token, CATEGORY_ALIASES))
        index += 1
    if domain == "person" and action == "generate" and args and "appearance_hint" not in options:
        options["appearance_hint"] = " ".join(args)
        args = []
    return AdminCommand(domain=domain, action=action, args=tuple(args), options=options)


def parse_tags(value: str | dict[str, Any] | None) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise CommandParseError("标签必须是 JSON 对象") from exc
    if not isinstance(parsed, dict):
        raise CommandParseError("标签必须是 JSON 对象")
    return parsed


def help_text(prefix: str = "/maitu") -> str:
    p = prefix.strip().rstrip("/")
    return "\n".join(
        [
            "写真插件管理员命令：",
            f"{p} 帮助",
            f"{p} 诊断",
            f"{p} 人物 查看",
            f"{p} 人物 提取    # 当前消息、引用消息或本聊天最近一张单图 → 面部参考板",
            f"{p} 人物 导入    # 直接导入已处理好的面部参考板",
            f"{p} 人物 生成    # 无人物参考时，按 MaiBot 人格设定生成面部参考板",
            f"{p} 人物 生成 补充=短发圆脸    # 可追加不含服装的外貌补充",
            f"{p} 人物 重生成",
            f"{p} 人物 清空    # 首次返回确认令牌，五分钟内再执行一次",
            f"{p} 参考 提取 服装 [名称=夏天裙子]",
            f"{p} 参考 导入 场景 [名称=卧室]",
            f"{p} 参考 列表 [服装|场景]",
            f"{p} 参考 查看 <参考图ID>",
            f'{p} 参考 编辑 <参考图ID> [名称=名称] [标签=\'{{"styles":["casual"]}}\']',
            f"{p} 参考 重标|重生成|替换|启用|停用 <参考图ID>",
            f"{p} 参考 删除 <参考图ID>    # 首次返回确认令牌",
            f"{p} 连续 查看|重置",
            f"{p} 连续 固定 服装|场景 <参考图ID>",
            f"{p} 连续 取消固定 [服装|场景]",
            f"{p} 任务 列表|查看|重试|取消 [任务ID]",
            "导入图片请用下面任一方式，不要填写消息 ID：",
            "1. 命令和图片发在同一条消息里；",
            "2. 回复/引用一张只含单图的消息后再发命令；",
            "3. 先单独发一张图，再发命令，插件会使用本聊天最近的一张单图。",
            "人物参考只保留面部与身份；服装完全由服装参考图控制。",
            "删除和人物清空首次调用会返回五分钟有效的确认令牌。",
        ]
    )


def _alias(token: str, table: dict[str, str], error: str) -> str:
    key = str(token or "").strip()
    mapped = table.get(key) or table.get(key.casefold())
    if mapped is None:
        raise CommandParseError(f"{error}: {token}")
    return mapped


def _alias_value(token: str, table: dict[str, str]) -> str:
    key = str(token or "").strip()
    return table.get(key) or table.get(key.casefold()) or token


def _option_key(raw: str) -> str:
    key = str(raw or "").strip().replace("-", "_")
    return OPTION_ALIASES.get(key) or OPTION_ALIASES.get(key.casefold()) or key.casefold()


def _is_option_name(raw: str) -> bool:
    key = str(raw or "").strip().replace("-", "_")
    if key in OPTION_ALIASES or key.casefold() in OPTION_ALIASES:
        return True
    return bool(key) and key.replace("_", "").isalnum()


def _domain_label(domain: str) -> str:
    return {
        "person": "人物",
        "ref": "参考",
        "continuity": "连续",
        "task": "任务",
        "doctor": "诊断",
        "help": "帮助",
    }.get(domain, domain)


__all__ = [
    "ACTION_ALIASES",
    "AdminCommand",
    "CATEGORY_ALIASES",
    "CommandParseError",
    "DOMAIN_ALIASES",
    "help_text",
    "parse_admin_command",
    "parse_tags",
]
