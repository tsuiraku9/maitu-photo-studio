#!/usr/bin/env python3
"""只用标准库离线拼装一个包含图库和任务子页面的 HTML 入口，不访问服务或导入插件后端。"""

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parents[1]
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024


def build_documents(root: Path = ROOT) -> Dict[str, str]:
    """按 entry 返回确定性产物；验证相对路径、占位符和大小后才允许写入。"""
    declaration_path = root / "webui.json"
    raw = declaration_path.read_bytes()
    if len(raw) > 131072:
        raise ValueError("webui.json 超过 128 KiB")
    declaration = json.loads(raw)
    if declaration.get("schema_version") != 2:
        raise ValueError("仅支持官方 schema_version 2")
    source = root / "webui" / "src"
    css = (source / "common.css").read_text(encoding="utf-8")
    common = (source / "common.js").read_text(encoding="utf-8")
    if "</style" in css.lower() or "</script" in common.lower():
        raise ValueError("源码包含会提前结束内联标签的文本")
    if len(declaration["pages"]) != 1:
        raise ValueError("只注册一个麦麦写真侧栏入口")
    fragments = {}
    scripts = []
    for view in ("gallery", "tasks"):
        template = (source / f"{view}.html").read_text(encoding="utf-8")
        body = template.split('<main class="app">', 1)[1].split("</main>", 1)[0]
        marker = '<section class="panel"' if view == "gallery" else '<section class="overview-strip"'
        fragments[f"@@{view.upper()}_BODY@@"] = body[body.index(marker) : body.index('<details id="receipts-panel"')]
        dialogs = template.split("</main>", 1)[1].split("<script>", 1)[0]
        fragments[f"@@{view.upper()}_DIALOGS@@"] = dialogs.split('<dialog id="confirm-dialog"', 1)[0]
        scripts.append((source / f"{view}.js").read_text(encoding="utf-8"))
    fragments["@@RECEIPTS@@"] = body[body.index('<details id="receipts-panel"') :]
    scripts.append("window.PhotoUI.startStudio().catch(window.PhotoUI.fatal);")
    documents: Dict[str, str] = {}
    for page in declaration["pages"]:
        if page.get("mode") != "custom" or page["id"] != "gallery":
            raise ValueError("只允许一个 gallery custom 入口")
        entry = Path(page["entry"])
        if entry != Path("webui") / f"{page['id']}.html":
            raise ValueError("入口必须为 webui/gallery.html")
        template = (source / "studio.html").read_text(encoding="utf-8")
        script = "\n".join(scripts)
        if "</script" in script.lower():
            raise ValueError("页面脚本包含会提前结束内联标签的文本")
        for token, value in (
            ("@@STYLE@@", css),
            ("@@COMMON_JS@@", common),
            ("@@PAGE_JS@@", script),
            *fragments.items(),
        ):
            if template.count(token) != 1:
                raise ValueError(f"模板必须恰好包含一个 {token}")
            template = template.replace(token, value)
        if "@@" in template:
            raise ValueError("模板存在未处理占位符")
        document = template.rstrip() + "\n"
        if len(document.encode("utf-8")) >= MAX_PAGE_BYTES:
            raise ValueError("单页产物必须小于 2 MiB")
        if page["entry"] in documents:
            raise ValueError("页面入口重复")
        documents[page["entry"]] = document
    if len(documents) != 1 or sum(len(item.encode("utf-8")) for item in documents.values()) > MAX_TOTAL_BYTES:
        raise ValueError("产物必须为一个页面且不超过 8 MiB")
    return documents


def main() -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="仅检查现有产物是否与源码一致，不写文件")
    parser.add_argument("--output-dir", type=Path, help="输出到指定根目录（供离线构建测试使用）")
    options = parser.parse_args()
    destination = options.output_dir.resolve() if options.output_dir else ROOT
    try:
        documents = build_documents()
        for entry, document in documents.items():
            target = destination / entry
            if options.check:
                if not target.is_file() or target.read_text(encoding="utf-8") != document:
                    raise ValueError(f"产物需要重新构建：{entry}")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(document, encoding="utf-8", newline="\n")
            print(f"{'已核验' if options.check else '已构建'} {entry}（{len(document.encode('utf-8'))} 字节）")
    except (OSError, ValueError, KeyError) as error:
        print(f"构建失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
