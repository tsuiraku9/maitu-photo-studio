#!/usr/bin/env python3
"""首次加载写真 0.4.0 前迁移大小字段；默认预览，--apply 备份并原子写入。"""

from __future__ import annotations

import os
import re
import tempfile
import tomllib
from argparse import ArgumentParser
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

FIELDS = (("references", "max_bytes", "max_kb"), ("openai", "max_response_bytes", "max_response_kb"))


def migrate_text(text: str) -> tuple[str, list[str]]:
    """只替换已解析的大小字段，完整保留其它配置与注释。"""

    config = tomllib.loads(text)
    lines = text.splitlines(keepends=True)
    changes = []
    for section, old_key, new_key in FIELDS:
        values = config.get(section, {})
        if new_key in values:
            new_value = values[new_key]
            if isinstance(new_value, bool) or not isinstance(new_value, (int, float)):
                raise ValueError(f"{section}.{new_key} 必须是 KB 数值")
            amount = Decimal(str(new_value))
            if not amount.is_finite() or amount < Decimal(1) / 1024:
                raise ValueError(f"{section}.{new_key} 必须是有限正数且至少为 1 B")
            if section == "references" and amount > 500:
                raise ValueError("参考图大小配置超过图库硬上限")
        if old_key not in values:
            continue
        old_value = values[old_key]
        if isinstance(old_value, bool) or not isinstance(old_value, int) or old_value < 1:
            raise ValueError(f"{section}.{old_key} 必须是正整数")
        if section == "references" and old_value > 500 * 1024:
            raise ValueError("参考图旧大小配置超过图库硬上限")
        if new_key in values:
            replacement = None
            changes.append(f"{section}: 保留已有 {new_key}，移除旧 {old_key}")
        else:
            replacement = repr(old_value / 1024)
            changes.append(f"{section}.{old_key} → {section}.{new_key}")
        active_section = ""
        found = 0
        updated = []
        for line in lines:
            heading = re.match(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$", line.rstrip("\r\n"))
            if heading:
                active_section = heading.group(1).strip()
            match = re.match(rf"^(\s*){old_key}(\s*=\s*)[^#\r\n]*(.*)$", line.rstrip("\r\n"))
            if active_section == section and match:
                found += 1
                if replacement is not None:
                    newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                    updated.append(f"{match[1]}{new_key}{match[2]}{replacement} {match[3]}{newline}")
            else:
                updated.append(line)
        if found != 1:
            raise ValueError(f"无法安全定位 [{section}] 下的 {old_key}，请先将该项改为标准表字段")
        lines = updated
    result = "".join(lines)
    parsed = tomllib.loads(result)
    for section, old_key, new_key in FIELDS:
        if old_key in config.get(section, {}):
            expected = config[section].get(new_key, config[section][old_key] / 1024)
            if parsed[section].get(new_key) != expected or old_key in parsed[section]:
                raise ValueError("迁移结果校验失败")
    return result, changes


def migrate_file(path: Path, *, apply: bool = False) -> list[str]:
    """业务停写后使用；备份与新配置均为 0600，不输出任何配置内容。"""

    if path.is_symlink():
        raise ValueError("配置文件不能是符号链接")
    original = path.read_bytes()
    result, changes = migrate_text(original.decode("utf-8"))
    if not changes or not apply:
        return changes
    backup_dir = path.parent / "config_back"
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    backup_dir.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / f"pre-v040-{stamp}-{uuid4().hex[:8]}.toml"
    with backup.open("xb") as file:
        os.fchmod(file.fileno(), 0o600)
        file.write(original)
        file.flush()
        os.fsync(file.fileno())
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".config-v040-", delete=False) as file:
            temporary = Path(file.name)
            os.fchmod(file.fileno(), 0o600)
            file.write(result.encode("utf-8"))
            file.flush()
            os.fsync(file.fileno())
        if path.read_bytes() != original:
            raise ValueError("配置已被其它进程修改，未覆盖，请停写后重试")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return changes


def main() -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="待迁移的 config.toml 路径")
    parser.add_argument("--apply", action="store_true", help="业务停写后执行实际迁移；默认只读预览")
    args = parser.parse_args()
    try:
        changes = migrate_file(args.config, apply=args.apply)
    except (OSError, ValueError):
        # TOML 解析异常可能含原始配置行，命令行禁止回显异常文本。
        print("迁移失败：请检查文件权限、TOML 格式及大小字段；未输出配置内容。")
        return 1
    print("已应用并备份" if args.apply and changes else "迁移预览" if changes else "无需迁移")
    for change in changes:
        print(change)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
