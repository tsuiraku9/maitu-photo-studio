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


DOMAINS = frozenset({"person", "ref", "continuity", "task", "doctor", "help"})


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
    domain = tokens.pop(0).casefold()
    if domain not in DOMAINS:
        raise CommandParseError(f"未知命令分组: {domain}")
    if domain in {"doctor", "help"}:
        if tokens:
            raise CommandParseError(f"{domain} 不接受额外参数")
        return AdminCommand(domain)
    action = tokens.pop(0).casefold() if tokens else "show"
    args: list[str] = []
    options: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--"):
            key = token[2:].replace("-", "_")
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
            key, value = token.split("=", 1)
            if key and key.replace("_", "").replace("-", "").isalnum():
                options[key.replace("-", "_")] = value
                index += 1
                continue
        args.append(token)
        index += 1
    return AdminCommand(domain=domain, action=action, args=tuple(args), options=options)


def parse_tags(value: str | dict[str, Any] | None) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise CommandParseError("tags 必须是 JSON 对象") from exc
    if not isinstance(parsed, dict):
        raise CommandParseError("tags 必须是 JSON 对象")
    return parsed


def help_text(prefix: str = "/maitu") -> str:
    p = prefix.strip().rstrip("/")
    return "\n".join(
        [
            "写真插件管理员命令：",
            f"{p} person extract|import|show|regenerate|clear",
            f"{p} ref extract|import <outfit|scene> [name=名称]",
            f"{p} ref list [outfit|scene] | show <id>",
            f"{p} ref edit <id> [name=名称] [tags='{{...}}']",
            f"{p} ref retag|regenerate|replace|enable|disable <id>",
            f"{p} ref delete <id> [confirm_token=令牌]",
            f"{p} continuity show|reset|pin|unpin [outfit|scene] [id]",
            f"{p} task list|show|retry|cancel [task_id]",
            f"{p} doctor | {p} help",
            "extract 会从原图生成多角度参考板；import 直接导入已处理参考板。",
            "删除和人物清空首次调用返回五分钟有效的确认令牌。",
        ]
    )


__all__ = [
    "AdminCommand",
    "CommandParseError",
    "help_text",
    "parse_admin_command",
    "parse_tags",
]
