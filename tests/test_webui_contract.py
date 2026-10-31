"""WebUI 静态、构建及假宿主协议测试；不导入插件，不访问服务，不触发业务写入。"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys

# ruff: noqa: E501 -- 此文件内嵌原生 Node 假宿主，用于执行自包含产物的协议测试。
from html.parser import HTMLParser
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
DECLARATION = json.loads((ROOT / "webui.json").read_text(encoding="utf-8"))


def _builder() -> Any:
    spec = spec_from_file_location("photo_webui_builder", ROOT / "scripts" / "build_webui.py")
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DocumentContract(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.controls: list[dict[str, Any]] = []
        self.resources: list[tuple[str, str]] = []
        self.forbidden: list[str] = []
        self.current_select: dict[str, Any] | None = None
        self.inline_scripts = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        properties = dict(attrs)
        if "id" in properties:
            self.ids.append(str(properties["id"]))
            control = {"tag": tag, "attributes": properties, "options": []}
            self.controls.append(control)
            if tag == "select":
                self.current_select = control
        if tag == "option" and self.current_select is not None:
            self.current_select["options"].append(properties.get("value", ""))
        if tag in {"iframe", "object", "embed", "form", "base"}:
            self.forbidden.append(tag)
        if tag == "script" and "src" not in properties:
            self.inline_scripts += 1
        for name in ("src", "href", "action"):
            if name in properties and properties[name]:
                self.resources.append((tag, str(properties[name])))
        if any(name.startswith("on") for name in properties):
            self.forbidden.append("内联事件属性")

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self.current_select = None


def _accepts(parameter: dict[str, Any], value: Any) -> bool:
    """与已读取的官方 Parameter.accepts 一致的离线边界检查，不调用后端。"""
    kind = parameter["type"]
    if kind == "object":
        fields = parameter.get("properties", {})
        return (
            isinstance(value, dict)
            and not value.keys() - fields.keys()
            and all(
                (name in value and _accepts(child, value[name]))
                or (name not in value and not child.get("required", False))
                for name, child in fields.items()
            )
        )
    if kind == "array":
        return (
            isinstance(value, list)
            and len(value) <= parameter.get("max_items", 1000)
            and all(_accepts(parameter["items"], item) for item in value)
        )
    expected = {
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": type(value) in (int, float),
        "boolean": type(value) is bool,
    }[kind]
    if not expected or (parameter.get("choices") and value not in parameter["choices"]):
        return False
    if isinstance(value, str):
        return len(value) <= parameter.get("max_length", 4000)
    if type(value) in (int, float):
        return parameter.get("minimum", float("-inf")) <= value <= parameter.get("maximum", float("inf"))
    return True


def test_schema2_custom_sidebar_and_private_api_bindings() -> None:
    assert DECLARATION["schema_version"] == 2
    assert DECLARATION["workspace_title"] == "麦麦写真"
    assert set(DECLARATION["required_capabilities"]) == {"custom_pages", "image_upload", "structured_arguments"}
    assert [(page["id"], page["title"]) for page in DECLARATION["pages"]] == [
        ("gallery", "麦麦写真"),
    ]
    for page in DECLARATION["pages"]:
        assert page["mode"] == "custom" and page["placement"] == "sidebar"
        assert page["layout"] == "document"
        assert page["entry"] == f"webui/{page['id']}.html"
        assert "document" not in page and "content" not in page
        for binding in [*page["queries"].values(), *page["actions"].values()]:
            assert binding["version"] == "1"
            assert "public" not in binding  # public=False 是后端 @API 元数据，不是 schema2 字段。
            assert "confirmation" not in binding  # 业务签名确认由前端与后端协议完成，不重复宿主弹窗。
    gallery = DECLARATION["pages"][0]
    assert gallery["queries"]["gallery"]["api"] == "webui_gallery_query"
    assert gallery["queries"]["tasks"]["api"] == "webui_task_query"
    assert gallery["actions"]["manage"]["api"] == "webui_gallery_action"
    assert gallery["actions"]["stage"]["api"] == "webui_stage_upload"
    assert gallery["uploads"] == {"library": {"action": "stage", "image_max_edge": 2048}}
    assert gallery["actions"]["stage"]["parameters"]["upload_id"] == {
        "type": "string",
        "required": True,
        "max_length": 128,
    }
    assert len(DECLARATION["pages"]) == 1
    assert "prepare_task_preview" in gallery["actions"]["manage"]["parameters"]["operation"]["choices"]


def test_structured_union_and_canonical_confirmation_contract() -> None:
    gallery = DECLARATION["pages"][0]
    args = gallery["actions"]["manage"]["parameters"]["args"]
    versions = [{"asset_id": "a1", "updated_at": "2026-10-12T12:00:00+00:00"}]
    delete = {"ids": ["a1"], "versions": versions}
    assert _accepts(args, delete)
    assert _accepts(args, {"action": "delete", "params": delete, "target_request_id": "stable-delete"})
    signed = {
        "request_id": "stable-submit",
        "operation": "extract",
        "category": "outfit",
        "name": "参考图",
        "stage_id": "stage-1",
        "batch_id": "batch-1",
        "person_version": "none",
    }
    assert _accepts(
        args, {"action": "batch_submit", "params": {"items": [signed] * 20}, "target_request_id": "batch-parent"}
    )
    assert not _accepts(
        args, {"action": "batch_submit", "params": {"items": [signed] * 21}, "target_request_id": "batch-parent"}
    )
    assert not _accepts(args, {"ids": ["a1"], "versions": {"a1": "timestamp"}})
    assert not _accepts(args, {"action": "submit", "params": {"params": {}}})
    assert not _accepts(args, {"path": "/secret", "user_id": "u", "stream_id": "s", "origin": "webui"})
    labels = {
        "appearance_summary",
        "confidence",
        "type",
        "wearing_scenes",
        "seasons",
        "styles",
        "room_type",
        "privacy_eligible",
        "scene_signature",
    }
    assert set(args["properties"]["manual_tags"]["properties"]) == labels
    query = gallery["queries"]["gallery"]["parameters"]["args"]
    assert _accepts(query, {"asset_ids": ["a1"] * 6, "variant": "thumbnail"})
    assert not _accepts(query, {"asset_ids": ["a1"] * 7, "variant": "thumbnail"})
    assert _accepts(query, {"limit": 24, "offset": 0})
    assert not _accepts(query, {"limit": 25, "offset": 0})


def test_standard_library_build_is_deterministic_and_current(tmp_path: Path) -> None:
    module = _builder()
    first = module.build_documents()
    assert first == module.build_documents()
    assert set(first) == {"webui/gallery.html"}
    for entry, document in first.items():
        assert document == (ROOT / entry).read_text(encoding="utf-8")
        assert len(document.encode("utf-8")) < 2 * 1024 * 1024
        assert "@@" not in document
    command = [sys.executable, "-B", str(ROOT / "scripts" / "build_webui.py")]
    subprocess.run([*command, "--output-dir", str(tmp_path)], check=True, capture_output=True, text=True)
    subprocess.run([*command, "--output-dir", str(tmp_path), "--check"], check=True, capture_output=True, text=True)
    for entry, document in first.items():
        assert (tmp_path / entry).read_text(encoding="utf-8") == document


@pytest.mark.parametrize("page_id", ["gallery"])
def test_self_contained_chinese_accessible_document_and_script_syntax(page_id: str) -> None:
    document = (ROOT / "webui" / f"{page_id}.html").read_text(encoding="utf-8")
    parser = DocumentContract()
    parser.feed(document)
    assert len(parser.ids) == len(set(parser.ids))
    assert not parser.forbidden and not parser.resources
    assert parser.inline_scripts == 2
    assert '<html lang="zh-CN">' in document
    assert 'name="viewport"' in document
    scripts = re.findall(r"<script>(.*?)</script>", document, re.S)
    for script in scripts:
        assert not re.search(r"\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|Worker|postMessage)\s*\(", script)
        assert not re.search(
            r"(?:innerHTML|outerHTML|insertAdjacentHTML|localStorage|sessionStorage|document\.cookie|location\.)",
            script,
        )
        assert not re.search(r"\b(?:user_id|stream_id|source_path|reference_path|upload_id)\s*:", script)
        if shutil.which("node"):
            subprocess.run(["node", "--check"], input=script, text=True, capture_output=True, check=True)
    for method in ("ready", "query", "action", "upload", "context"):
        assert f"window.maibot.{method}" in document
    for control in parser.controls:
        attributes = control["attributes"]
        if control["tag"] == "dialog":
            assert attributes["aria-labelledby"] in parser.ids
    assert "@media (max-width: 700px)" in document
    assert "@media (max-width: 390px)" in document
    assert ":root.dark" in document and "var(--maibot-" in document
    assert "chart" not in document.lower() and "stat-tile" not in document.lower()


# 下列执行的是页面构建产物及假的 MessagePort 等价宿主，不是 Python 业务测试。
# 假 DOM 可验证交互与协议，不能证明真实浏览器布局、焦点或图片解码正确。
NODE_HARNESS = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const crypto = require("node:crypto").webcrypto;
const payload = JSON.parse(fs.readFileSync(0, "utf8"));
class Events {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, callback) { if (!this.listeners.has(type)) this.listeners.set(type, []); this.listeners.get(type).push(callback); }
  dispatchEvent(event) { event.preventDefault ||= (() => { event.defaultPrevented = true; }); for (const callback of [...(this.listeners.get(event.type) || [])]) callback(event); return !event.defaultPrevented; }
}
class Node extends Events {
  constructor(tag = "div", attrs = {}) {
    super(); this.tagName = tag.toUpperCase(); this.attrs = {}; this.dataset = {}; this.children = []; this.parentNode = null;
    this._text = ""; this._value = ""; this.disabled = false; this.checked = false; this.hidden = false; this.open = false; this.files = [];
    const classes = new Set();
    this.classList = { toggle: (name, force) => { const on = force === undefined ? !classes.has(name) : force; if (on) classes.add(name); else classes.delete(name); return on; }, remove: (name) => classes.delete(name), add: (name) => classes.add(name), contains: (name) => classes.has(name) };
    Object.defineProperty(this, "className", { get: () => [...classes].join(" "), set: (value) => { classes.clear(); String(value).split(/\s+/).filter(Boolean).forEach((x) => classes.add(x)); } });
    this.style = { colorScheme: "", setProperty() {} };
    for (const [key, value] of Object.entries(attrs)) this.setAttribute(key, value ?? "");
  }
  setAttribute(name, value) {
    value = String(value); this.attrs[name] = value;
    if (name === "class") this.className = value;
    if (name === "disabled" || name === "checked" || name === "hidden" || name === "multiple") this[name] = true;
    if (name === "value") this.value = value;
    if (name === "id") this.id = value;
    if (name.startsWith("data-")) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
  }
  getAttribute(name) { return this.attrs[name] ?? null; }
  removeAttribute(name) { delete this.attrs[name]; if (name === "src") delete this.src; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  set textContent(value) { this._text = String(value); this.replaceChildren(); }
  get value() { return this._value; }
  set value(value) { this._value = String(value); }
  get options() { return this.children.filter((child) => child.tagName === "OPTION"); }
  append(...nodes) { for (const node of nodes) { const child = typeof node === "string" ? Object.assign(new Node("text"), { _text: node }) : node; child.parentNode = this; this.children.push(child); } }
  replaceChildren(...nodes) { for (const child of this.children) child.parentNode = null; this.children = []; this.append(...nodes); }
  closest(selector) { for (let node = this; node; node = node.parentNode) if (matches(node, selector)) return node; return null; }
  showModal() { assert.equal(this.open, false, "同一个 dialog 不能重复打开"); this.open = true; }
  close() { this.open = false; }
  click() { if (!this.disabled) this.dispatchEvent({ type: "click", target: this }); }
}
function matches(node, selector) {
  const attr = selector.match(/^\[([^\]=]+)(?:="([^"]*)")?\]$/);
  if (attr) return attr[1].startsWith("data-") ? Object.hasOwn(node.dataset, attr[1].slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())) && (attr[2] === undefined || node.getAttribute(attr[1]) === attr[2]) : Object.hasOwn(node.attrs, attr[1]) && (attr[2] === undefined || node.attrs[attr[1]] === attr[2]);
  if (selector.startsWith(".")) return node.classList.contains(selector.slice(1));
  return node.tagName.toLowerCase() === selector;
}
class Document extends Events {
  constructor(controls) {
    super(); this.visibilityState = "visible"; this.nodes = new Map(); this.documentElement = new Node("html");
    for (const control of controls) { const node = new Node(control.tag, control.attributes); this.nodes.set(control.attributes.id, node); for (const value of control.options) node.append(new Node("option", { value })); if (node.options.length) node.value = node.options[0].value; }
  }
  getElementById(id) { assert(this.nodes.has(id), `缺失 DOM ID ${id}`); return this.nodes.get(id); }
  createElement(tag) { return new Node(tag); }
  createTextNode(text) { const node = new Node("text"); node.textContent = text; return node; }
  all() { const nodes = new Set(); const visit = (node) => { if (nodes.has(node)) return; nodes.add(node); node.children.forEach(visit); }; this.nodes.forEach(visit); return [...nodes]; }
  querySelectorAll(selector) { return this.all().filter((node) => selector.split(",").some((part) => matches(node, part.trim()))); }
}
function accepts(p, value) {
  if (p.type === "object") return value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).every((key) => Object.hasOwn(p.properties || {}, key)) && Object.entries(p.properties || {}).every(([key, child]) => Object.hasOwn(value, key) ? accepts(child, value[key]) : !child.required);
  if (p.type === "array") return Array.isArray(value) && value.length <= (p.max_items || 1000) && value.every((x) => accepts(p.items, x));
  const type = p.type === "integer" ? Number.isInteger(value) : p.type === "number" ? typeof value === "number" && Number.isFinite(value) : typeof value === p.type;
  return type && (!p.choices?.length || p.choices.includes(value)) && (typeof value !== "string" || value.length <= (p.max_length || 4000)) && (typeof value !== "number" || (value >= (p.minimum ?? -Infinity) && value <= (p.maximum ?? Infinity)));
}
function validate(binding, args) { assert(binding, "调用别名未声明"); assert(Object.keys(args).every((key) => Object.hasOwn(binding.parameters, key)), "存在未声明顶层参数"); for (const [key, p] of Object.entries(binding.parameters)) assert(Object.hasOwn(args, key) ? accepts(p, args[key]) : !p.required, `绑定参数不符合 schema ${key}`); }
const NOW = "2026-10-12T12:00:00+00:00";
const assetFixture = (id) => ({ id, name: `素材${id}`, category: "outfit", status: "active", tags: { type: "连衣裙", wearing_scenes: ["日常"], seasons: ["夏季"], styles: ["简约"], confidence: .9 }, manual_tags: { type: "原覆盖" }, effective_tags: { type: "原覆盖" }, updated_at: NOW, validation_errors: [], selectable: true });
const webTask = { task_id: "task-web", id: "task-web", kind: "reference", operation: "regenerate", origin: "webui", status: "failed", asset_id: "a1", asset_status: "active", error: "示例失败", created_at: NOW, can_cancel: false, can_retry: true, paid_request_started: true, batch_id: "" };
const qqTask = { ...webTask, task_id: "task-qq", id: "task-qq", origin: "qq", status: "queued", can_cancel: true, can_retry: true };
const completedTask = { ...webTask, task_id: "task-done", id: "task-done", origin: "automatic", status: "completed", error: "", can_cancel: false, can_retry: false };
function environment(pageId, handlers = {}) {
  const page = payload.declaration.pages[0];
  const document = new Document(payload.controls[pageId]);
  const events = new Events();
  const timers = new Map(); let timerId = 0;
  const calls = []; let inflight = 0; let maximum = 0;
  const clock = { setTimeout: (fn, ms) => { const id = ++timerId; timers.set(id, { fn, ms }); return id; }, clearTimeout: (id) => timers.delete(id), run: async (minimum = 0) => { const entry = [...timers.entries()].find(([, timer]) => timer.ms >= minimum); if (!entry) return false; timers.delete(entry[0]); entry[1].fn(); await flush(); return true; }, timers };
  const defaultQuery = (alias, args) => {
    if (alias === "gallery" && args.operation === "settings") return { auto_enable: true, tag_fields: {}, page_size: 12, max_batch: 20, person: { asset_id: "", version: "", status: "" } };
    if (alias === "gallery" && args.operation === "list") return { items: [assetFixture("a1"), assetFixture("a2")], total: 2 };
    if (alias === "gallery" && args.operation === "detail") return { asset: assetFixture(args.args.asset_id) };
    if (alias === "gallery" && args.operation === "images") return { images: args.args.asset_ids.map((id) => ({ asset_id: id, variant: args.args.variant, ready: args.args.variant === "thumbnail", data_url: args.args.variant === "thumbnail" ? "data:image/png;base64,YQ==" : null })) };
    if (alias === "tasks" && args.operation === "list") return { items: [webTask, qqTask, completedTask], total: 3 };
    if (alias === "tasks" && args.operation === "detail") return { task: args.args.task_id === "task-qq" ? qqTask : webTask };
    if (alias === "tasks" && args.operation === "receipt") return { receipt: null };
    throw new Error(`未定义假查询 ${alias}/${args.operation}`);
  };
  const defaultAction = (alias, args) => args.operation === "confirm" ? { success: true, confirmation: { token: "server-only-secret", summary: "服务端确认摘要", expires_at: 4070908800 } } : { success: true, task_id: "task-new", status: "queued" };
  const invoke = async (kind, alias, args, extra) => {
    if (kind === "query") validate(page.queries[alias], args);
    if (kind === "action") validate(page.actions[alias], args);
    if (kind === "upload") { assert(!Object.hasOwn(args, "upload_id"), "页面不得传上传凭证"); validate(page.actions[page.uploads[alias].action], { ...args, upload_id: "host-issued" }); }
    calls.push({ kind, alias, args: structuredClone(args), extra });
    maximum = Math.max(maximum, ++inflight);
    try {
      await Promise.resolve();
      if (handlers[kind]) return await handlers[kind](alias, args, extra, defaultQuery, defaultAction);
      if (kind === "query") return defaultQuery(alias, args);
      if (kind === "action") return defaultAction(alias, args);
      if (kind === "upload") return { stage_id: `stage-${calls.filter((call) => call.kind === "upload").length}`, expires_at: 4070908800 };
      return null;
    } finally { inflight -= 1; }
  };
  const window = {
    addEventListener: (...args) => events.addEventListener(...args), dispatchEvent: (...args) => events.dispatchEvent(...args),
    maibot: { ready: Promise.resolve(), context: { theme: "light", language: "en", dashboardStyle: "modern", styles: "" }, page: { id: "gallery", layout: "document", state: { view: pageId } }, query: (alias, args) => invoke("query", alias, args), action: (alias, args) => invoke("action", alias, args), upload: (alias, file, args, options) => invoke("upload", alias, args, { file, options }), navigate: (pageId, state) => invoke("navigate", pageId, state) },
  };
  const context = vm.createContext({ window, document, crypto, console, TextEncoder, AbortController, URL: { createObjectURL: () => "blob:local-only", revokeObjectURL() {} }, CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } }, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });
  const scripts = payload.scripts[pageId];
  vm.runInContext(scripts[0], context);
  return { document, window, context, calls, clock, loadPage: () => vm.runInContext(scripts[1], context), U: window.PhotoUI, $: (id) => document.getElementById(id), max: () => maximum };
}
async function flush() { for (let index = 0; index < 160; index += 1) await Promise.resolve(); }
async function click(env, id) { env.$(id).click(); await flush(); }
function change(env, id, value, type = "input") { const node = env.$(id); node.value = value; node.dispatchEvent({ type, target: node }); }
function assertNoSecret(env) { assert(!env.document.all().some((node) => node.textContent.includes("server-only-secret")), "确认token不应显示"); }
const scenarios = {
  async theme_navigation() {
    const env = environment("gallery"); await env.U.init();
    assert.equal(env.document.documentElement.lang, "zh-CN");
    assert.equal(env.document.documentElement.style.colorScheme, "light");
    env.window.maibot.context.theme = "dark"; env.window.dispatchEvent({ type: "maibot:context" });
    assert(env.document.documentElement.classList.contains("dark"));
    assert.equal(env.document.documentElement.style.colorScheme, "dark");
    env.window.maibot.context.theme = "light"; env.window.dispatchEvent({ type: "maibot:context" });
    assert(!env.document.documentElement.classList.contains("dark"));
    assert.equal(env.U.safeImage("data:image/svg+xml;base64,YQ=="), null);
    assert.equal(env.U.safeImage("https://private.example/a.png"), null);
    assert.equal(env.U.safeImage("data:image/png;base64,YQ=="), "data:image/png;base64,YQ==");
    env.U.registerView("tasks", async () => {});
    await env.U.navigate("tasks", { gallery_filters: { search: "测试" } });
    assert.equal(env.U.activeView, "tasks"); assert(env.$("gallery-view").hidden); assert(!env.$("tasks-view").hidden);
    assert.equal(env.calls.filter((call) => call.kind === "navigate").length, 0);
  },
  async timeout_receipt_only() {
    const env = environment("gallery", {
      action: async () => { throw new Error("Request timed out; an action may still have completed"); },
      query: async (_, args) => ({ receipt: { request_id: args.args.request_id, state: "accepted", task_id: "recovered-task", response: { success: true, task_id: "recovered-task" } } }),
    });
    await env.U.init();
    const result = await env.U.action("submit", { operation: "import", category: "outfit", stage_id: "st", name: "名字" }, "stable-request");
    assert.equal(result.task_id, "recovered-task");
    assert.equal(env.calls.filter((call) => call.kind === "action").length, 1);
    assert.equal(env.calls.filter((call) => call.kind === "query").length, 1);
    assert.equal(env.calls[1].args.operation, "receipt");
    assert.equal(env.calls[0].args.request_id, "stable-request");
    assert.equal(env.U.isUncertain(), false);
    await assert.rejects(() => env.U.action("submit", {}, "stable-request"), /已经发送/);
    assert.equal(env.calls.filter((call) => call.kind === "action").length, 1);
  },
  async unknown_receipt_lock() {
    let state = "preparing";
    const env = environment("gallery", { action: async () => { throw new Error("Request timed out"); }, query: async (_, args) => ({ receipt: { request_id: args.args.request_id, state, response: state === "completed" ? { success: true } : undefined } }) });
    await env.U.init();
    await assert.rejects(() => env.U.action("edit", { asset_id: "a1", name: "名字" }, "unknown-request"), /timed out/);
    assert(env.U.isUncertain()); assert.throws(() => env.U.requireResolved(), /尚未确定/);
    let carried;
    env.U.registerView("tasks", async (state) => { carried = state; });
    await env.U.navigate("tasks", {});
    assert.deepEqual(Array.from(carried.receipt_ids), ["unknown-request"]);
    state = "accepted"; await env.U.lookupReceipt("unknown-request");
    assert(env.U.isUncertain(), "有accepted而无response仍须未知锁");
    state = "completed"; await env.U.lookupReceipt("unknown-request");
    assert(!env.U.isUncertain());
    assert.equal(env.calls.filter((call) => call.kind === "action").length, 1);
  },
  async json_form_edit_no_models() {
    const env = environment("gallery"); env.loadPage(); await flush();
    const detail = env.document.all().find((node) => node.tagName === "BUTTON" && node.textContent === "详情与标签"); detail.click(); await flush();
    assert(env.$("asset-dialog").open);
    const input = env.document.all().find((node) => node.getAttribute("aria-label") === "服装类型");
    input.value = "新覆盖"; input.dispatchEvent({ type: "input" });
    assert.equal(JSON.parse(env.$("manual-json").value).type, "新覆盖");
    change(env, "manual-json", JSON.stringify({ styles: ["休闲"], seasons: ["春季"] })); await flush();
    assert.equal(env.document.all().find((node) => node.getAttribute("aria-label") === "风格（逗号分隔）").value, "休闲");
    change(env, "manual-json", '{"confidence":101}'); await flush();
    assert(env.$("save-asset").disabled); assert(!env.$("tag-json-error").hidden);
    change(env, "manual-json", '{"type":"校正"}'); await flush(); assert(!env.$("save-asset").disabled);
    await click(env, "clear-overrides"); assert.deepEqual(JSON.parse(env.$("manual-json").value), {});
    change(env, "asset-name", "更名参考"); await click(env, "save-asset");
    const actions = env.calls.filter((call) => call.kind === "action");
    assert.equal(actions.length, 1); assert.equal(actions[0].args.operation, "edit");
    assert.equal(actions[0].args.args.expected_version, NOW); assert.deepEqual(actions[0].args.args.manual_tags, {});
    assert.equal(env.calls.filter((call) => call.kind === "upload").length, 0);
  },
  async batch_stage_then_one_confirmation() {
    const env = environment("gallery"); env.loadPage(); await flush(); await click(env, "open-upload");
    env.$("upload-files").files = [{ name: "one.png", type: "image/png", size: 100 }, { name: "two.webp", type: "image/webp", size: 200 }];
    env.$("upload-files").dispatchEvent({ type: "change" }); await flush();
    env.document.querySelectorAll("[data-file-name]")[0].value = "编辑名称";
    env.document.querySelectorAll("[data-file-name]")[0].dispatchEvent({ type: "input" });
    await click(env, "stage-files");
    assert.equal(env.calls.filter((call) => call.kind === "upload").length, 2);
    assert.equal(env.calls.filter((call) => call.kind === "action").length, 0, "stage不能触发模型或confirm");
    change(env, "upload-operation", "extract", "change"); await click(env, "submit-files");
    const confirms = env.calls.filter((call) => call.kind === "action" && call.args.operation === "confirm");
    assert.equal(confirms.length, 1); assert.equal(confirms[0].args.args.action, "batch_submit");
    const signed = confirms[0].args.args.params.items;
    assert.equal(signed.length, 2); assert.equal(signed[0].name, "编辑名称");
    assert(env.$("confirm-dialog").open);
    assert(env.document.querySelectorAll("[data-file-name]").every((node) => node.disabled));
    await click(env, "confirm-approve");
    const submits = env.calls.filter((call) => call.kind === "action" && call.args.operation === "submit");
    assert.equal(submits.length, 2);
    for (let i = 0; i < 2; i += 1) { assert.equal(submits[i].args.request_id, signed[i].request_id); assert.equal(submits[i].args.confirm_token, "server-only-secret"); const { request_id, ...params } = signed[i]; assert.deepEqual(submits[i].args.args, params); }
    assert.equal(env.max(), 1, "所有桥接请求必须串行"); assertNoSecret(env);
    await click(env, "submit-files"); assert.equal(env.calls.filter((call) => call.kind === "action" && call.args.operation === "submit").length, 2, "doubleclick不重复提交");
  },
  async cancelled_confirmation_and_person_single() {
    const env = environment("gallery"); env.loadPage(); await flush(); await click(env, "open-upload");
    change(env, "upload-category", "person", "change");
    env.$("upload-files").files = [{ name: "one.png", type: "image/png", size: 100 }, { name: "two.png", type: "image/png", size: 100 }]; env.$("upload-files").dispatchEvent({ type: "change" }); await flush();
    assert.equal(env.document.querySelectorAll("[data-file-name]").length, 0); assert.equal(env.$("upload-files").multiple, false);
    env.$("upload-files").files = [{ name: "person.png", type: "image/png", size: 100 }]; env.$("upload-files").dispatchEvent({ type: "change" }); await flush();
    await click(env, "submit-files");
    const confirm = env.calls.find((call) => call.kind === "action" && call.args.operation === "confirm"); assert(confirm); assert.equal(confirm.args.args.action, "submit"); assert.equal(confirm.args.args.params.person_version, "none");
    await click(env, "confirm-cancel");
    assert.equal(env.calls.filter((call) => call.kind === "action" && call.args.operation === "submit").length, 0);
    assert.equal(env.calls.filter((call) => call.kind === "upload").length, 1);
  },
  async personality_generation_only_without_person() {
    const env = environment("gallery"); env.loadPage(); await flush(); await click(env, "open-person");
    assert(!env.$("generate-person").disabled); assert.equal(env.$("appearance-hint").value, "");
    await click(env, "generate-person");
    const confirm = env.calls.find((call) => call.kind === "action" && call.args.operation === "confirm");
    assert.equal(confirm.args.args.params.operation, "generate_person"); assert.equal(confirm.args.args.params.person_version, "none"); assert.equal(confirm.args.args.params.appearance_hint, "");
    await click(env, "confirm-cancel"); assert.equal(env.calls.filter((call) => call.kind === "action" && call.args.operation === "submit").length, 0);
    const occupied = environment("gallery", { query: async (alias, args, extra, query) => alias === "gallery" && args.operation === "settings" ? { ...query(alias, args), person: { asset_id: "person-id", version: NOW, status: "needs_review" } } : query(alias, args) });
    occupied.loadPage(); await flush(); await click(occupied, "open-person"); assert(occupied.$("generate-person").disabled); assert(occupied.$("person-version-note").textContent.includes("确认删除")); await click(occupied, "generate-person"); assert.equal(occupied.calls.filter((call) => call.kind === "action").length, 0);
  },
  async delete_partial_and_version_arrays() {
    const env = environment("gallery", { action: async (_, args, extra, query, action) => args.operation === "delete" ? { success: false, items: [{ asset_id: "a1", success: true }, { asset_id: "a2", success: false, error: "版本冲突" }] } : action(_, args) });
    env.loadPage(); await flush(); env.$("select-page").checked = true; env.$("select-page").dispatchEvent({ type: "change" }); await flush();
    await click(env, "delete-selected");
    const confirm = env.calls.find((call) => call.kind === "action" && call.args.operation === "confirm");
    assert.equal(confirm.args.args.action, "delete"); assert.deepEqual(confirm.args.args.params.versions, [{ asset_id: "a1", updated_at: NOW }, { asset_id: "a2", updated_at: NOW }]);
    await click(env, "confirm-approve");
    const deletion = env.calls.find((call) => call.kind === "action" && call.args.operation === "delete"); assert.equal(deletion.args.request_id, confirm.args.args.target_request_id);
    assert(env.$("result-dialog").open); assert(env.$("result-summary").textContent.includes("1/2")); assert(env.$("result-items").textContent.includes("版本冲突"));
    assert.equal(env.calls.filter((call) => call.kind === "action" && call.args.operation === "delete").length, 1); assertNoSecret(env);
  },
  async lazy_preview_and_image_batches() {
    const env = environment("gallery", { query: async (alias, args, extra, query) => alias === "gallery" && args.operation === "list" ? { items: Array.from({ length: 12 }, (_, i) => assetFixture(`a${i}`)), total: 12 } : query(alias, args) });
    env.loadPage(); await flush();
    const thumbnails = env.calls.filter((call) => call.kind === "query" && call.args.operation === "images"); assert.equal(thumbnails.length, 2); assert(thumbnails.every((call) => call.args.args.asset_ids.length <= 6));
    env.document.all().find((node) => node.tagName === "BUTTON" && node.textContent === "详情与标签").click(); await flush();
    await click(env, "preview-source"); await click(env, "preview-source");
    assert.equal(env.calls.filter((call) => call.kind === "action" && call.args.operation === "prepare_preview").length, 1);
    const sourceQueries = env.calls.filter((call) => call.kind === "query" && call.args.operation === "images" && call.args.args.variant === "source"); assert.equal(sourceQueries.length, 2); assert(sourceQueries.every((call) => call.args.args.asset_ids.length === 1));
  },
  async empty_thumbnail_cache_prepares_and_displays() {
    const cache = new Set();
    const env = environment("gallery", {
      query: async (alias, args, extra, query) => {
        if (alias === "gallery" && args.operation === "list") return { items: Array.from({ length: 12 }, (_, i) => assetFixture(`a${i}`)), total: 12 };
        if (alias === "gallery" && args.operation === "images") return { images: args.args.asset_ids.map((id) => ({ asset_id: id, variant: "thumbnail", ready: cache.has(id), data_url: cache.has(id) ? "data:image/png;base64,YQ==" : null })) };
        return query(alias, args);
      },
      action: async (_, args) => {
        assert.equal(args.operation, "prepare_preview", "页面加载只能准备缩略图，不能提交模型任务");
        assert.equal(args.args.variant, "thumbnail"); assert(args.args.asset_ids.length <= 6);
        args.args.asset_ids.forEach((id) => cache.add(id)); return { success: true };
      },
    });
    env.loadPage(); await flush();
    const calls = env.calls.filter((call) => call.kind === "action" || (call.kind === "query" && call.args.operation === "images"));
    assert.deepEqual(calls.map((call) => `${call.kind}:${call.args.operation}`), ["query:images", "action:prepare_preview", "query:images", "query:images", "action:prepare_preview", "query:images"]);
    assert(calls.every((call) => call.args.args.asset_ids.length <= 6));
    const images = env.document.all().filter((node) => node.tagName === "IMG" && node.parentNode?.classList.contains("asset-image"));
    assert.equal(images.length, 12); assert(images.every((node) => !node.hidden && node.src === "data:image/png;base64,YQ=="));
    assert.equal(env.max(), 1); assert.equal(env.$("receipt-count").textContent, "", "缓存准备不得堆积业务回执"); assert(!env.U.isUncertain());
    await click(env, "refresh-gallery");
    assert.equal(env.calls.filter((call) => call.kind === "action").length, 2, "已有缓存无需再次prepare");
    cache.delete("a3"); await click(env, "refresh-gallery");
    const preparations = env.calls.filter((call) => call.kind === "action");
    assert.equal(preparations.length, 3); assert.deepEqual(preparations[2].args.args.asset_ids, ["a3"], "部分缓存缺失时只能prepare缺失项");
    assert(images.every((node) => !node.hidden && node.src === "data:image/png;base64,YQ=="));
    assert.equal(env.$("receipt-count").textContent, ""); assert.equal(env.max(), 1);
  },
  async thumbnail_prepare_skips_hidden_and_stale_pages() {
    const hidden = environment("gallery", { query: async (alias, args, extra, query) => {
      if (alias === "gallery" && args.operation === "images") { hidden.document.visibilityState = "hidden"; return { images: [] }; }
      return query(alias, args);
    } });
    hidden.loadPage(); await flush(); assert.equal(hidden.calls.filter((call) => call.kind === "action").length, 0);
    const queued = environment("gallery", { query: async (alias, args, extra, query) => args.operation === "images" ? { images: [] } : query(alias, args) });
    const serial = queued.U.serial;
    queued.U.serial = (work) => serial(() => { queued.document.visibilityState = "hidden"; return work(); });
    queued.loadPage(); await flush(); assert.equal(queued.calls.filter((call) => call.kind === "action").length, 0, "派发前应再次检查页面可见性");
    let resolveImages;
    let first = true;
    const stale = environment("gallery", { query: async (alias, args, extra, query) => {
      if (args.operation === "images" && first) { first = false; return await new Promise((resolve) => { resolveImages = resolve; }); }
      return query(alias, args);
    } });
    stale.loadPage(); await flush(); assert(resolveImages);
    stale.$("refresh-gallery").click(); await flush(); resolveImages({ images: [] }); await flush();
    assert.equal(stale.calls.filter((call) => call.kind === "action").length, 0, "旧generation结果不能准备缩略图");
    const timeout = environment("gallery", { query: async (alias, args, extra, query) => args.operation === "images" ? { images: [] } : query(alias, args), action: async () => { throw new Error("Request timed out"); } });
    timeout.loadPage(); await flush(); assert.equal(timeout.calls.filter((call) => call.kind === "action").length, 1, "缓存action超时不能自动重放"); assert.equal(timeout.$("receipt-count").textContent, ""); assert(!timeout.U.isUncertain());
  },
  async single_entry_subpages_keep_state_and_pause_polling() {
    const env = environment("gallery"); env.loadPage(); await flush();
    assert.equal(env.U.activeView, "gallery"); assert.equal(env.clock.timers.size, 0);
    change(env, "search-filter", "保留筛选"); await click(env, "apply-filters");
    await click(env, "nav-tasks");
    assert.equal(env.U.activeView, "tasks"); assert(env.$("gallery-view").hidden); assert(!env.$("tasks-view").hidden);
    assert.equal(env.clock.timers.size, 1);
    await click(env, "nav-gallery");
    assert.equal(env.U.activeView, "gallery"); assert.equal(env.$("search-filter").value, "保留筛选");
    assert.equal(env.clock.timers.size, 0);
    env.document.dispatchEvent({ type: "visibilitychange" }); await flush();
    assert.equal(env.clock.timers.size, 0);
    await click(env, "nav-tasks"); await click(env, "nav-gallery"); await click(env, "nav-tasks");
    const before = env.calls.filter((call) => call.kind === "query" && call.alias === "tasks" && call.args.operation === "list").length;
    await click(env, "refresh-tasks");
    const after = env.calls.filter((call) => call.kind === "query" && call.alias === "tasks" && call.args.operation === "list").length;
    assert.equal(after, before + 1, "重复切页不得重复绑定按钮事件");
    assert.equal(env.calls.filter((call) => call.kind === "navigate").length, 0);
    assert.equal(env.calls.filter((call) => call.kind === "action").length, 0);
    assert.equal(env.max(), 1);
  },
  async visible_poll_backoff_and_cleanup() {
    let fail = false;
    const env = environment("tasks", { query: async (alias, args, extra, query) => { if (fail && args.operation === "list") throw new Error("查询失败"); return query(alias, args); } });
    env.loadPage(); await flush();
    assert.equal(env.clock.timers.size, 1); assert([...env.clock.timers.values()].some((timer) => timer.ms === 5000));
    assert(env.$("task-list").textContent.includes("已完成")); assert(env.$("task-list").textContent.includes("只读"));
    const lists = () => env.calls.filter((call) => call.kind === "query" && call.args.operation === "list").length;
    const count = lists(); env.document.visibilityState = "hidden"; env.document.dispatchEvent({ type: "visibilitychange" }); await flush();
    assert.equal(env.clock.timers.size, 0); await env.clock.run(); assert.equal(lists(), count);
    env.document.visibilityState = "visible"; env.document.dispatchEvent({ type: "visibilitychange" }); await flush(); assert.equal(lists(), count + 1);
    fail = true; await env.clock.run(5000); assert([...env.clock.timers.values()].some((timer) => timer.ms === 10000));
    await env.clock.run(10000); assert([...env.clock.timers.values()].some((timer) => timer.ms === 20000));
    env.window.dispatchEvent({ type: "pagehide" }); await flush(); assert.equal(env.clock.timers.size, 0);
    assert.equal(env.calls.filter((call) => call.kind === "action").length, 0);
  },
  async query_backoff_never_replays_action() {
    let attempts = 0;
    const env = environment("gallery", { query: async (_, args) => { attempts += 1; if (attempts < 3) throw new Error("临时查询失败"); return { items: [], total: 0 }; } });
    await env.U.init();
    const pending = env.U.query("gallery", "list", { offset: 0, limit: 12 }); await flush();
    assert.equal(attempts, 1); assert([...env.clock.timers.values()].some((timer) => timer.ms === 800));
    await env.clock.run(800); assert.equal(attempts, 2); assert([...env.clock.timers.values()].some((timer) => timer.ms === 1600));
    await env.clock.run(1600); await pending; assert.equal(attempts, 3);
    assert.equal(env.calls.filter((call) => call.kind === "action").length, 0);
  },
  async unknown_batch_stops_and_doubleclick_blocks() {
    const env = environment("gallery", { action: async (alias, args, extra, query, action) => { if (args.operation === "submit") throw new Error("Request timed out"); return action(alias, args); } });
    env.loadPage(); await flush(); await click(env, "open-upload");
    env.$("upload-files").files = [{ name: "one.png", type: "image/png", size: 100 }, { name: "two.png", type: "image/png", size: 100 }]; env.$("upload-files").dispatchEvent({ type: "change" }); await flush();
    change(env, "upload-operation", "extract", "change"); await click(env, "submit-files"); await click(env, "submit-files");
    assert.equal(env.calls.filter((call) => call.kind === "action" && call.args.operation === "confirm").length, 1);
    await click(env, "confirm-approve");
    assert.equal(env.calls.filter((call) => call.kind === "action" && call.args.operation === "submit").length, 1, "第一项未知后不能提交第二项");
    assert(env.U.isUncertain()); await click(env, "submit-files");
    assert.equal(env.calls.filter((call) => call.kind === "action" && call.args.operation === "submit").length, 1);
    assert(env.calls.some((call) => call.kind === "query" && call.args.operation === "receipt"));
  },
  async webui_only_retry_confirm() {
    const env = environment("tasks"); env.loadPage(); await flush();
    const rows = env.$("task-list").children;
    const qqRow = rows[1]; assert.equal(qqRow.children.at(-1).children[0].children.filter((node) => node.tagName === "BUTTON").length, 1);
    qqRow.children.at(-1).children[0].children[0].click(); await flush(); assert(env.$("task-retry").disabled); assert(env.$("task-cancel").disabled); await click(env, "task-close");
    const retry = rows[0].children.at(-1).children[0].children.find((node) => node.textContent === "重试"); retry.click(); await flush();
    const confirm = env.calls.find((call) => call.kind === "action" && call.args.operation === "confirm"); assert(confirm); assert.equal(confirm.args.args.action, "retry"); assert.equal(confirm.args.args.params.task_id, "task-web");
    await click(env, "confirm-approve");
    const action = env.calls.find((call) => call.kind === "action" && call.args.operation === "retry"); assert(action); assert.equal(action.args.request_id, confirm.args.args.target_request_id); assert.equal(action.args.confirm_token, "server-only-secret");
    assert.equal(env.calls.filter((call) => call.kind === "action" && call.args.operation === "retry").length, 1); assertNoSecret(env);
  },
};
(async () => { assert(scenarios[payload.scenario]); await scenarios[payload.scenario](); console.log(JSON.stringify({ passed: payload.scenario, kind: "构建产物假宿主协议验证（非真实浏览器）" })); })().catch((error) => { console.error(error.stack); process.exitCode = 1; });
"""


@pytest.mark.parametrize(
    "scenario",
    [
        "theme_navigation",
        "timeout_receipt_only",
        "unknown_receipt_lock",
        "json_form_edit_no_models",
        "batch_stage_then_one_confirmation",
        "cancelled_confirmation_and_person_single",
        "personality_generation_only_without_person",
        "delete_partial_and_version_arrays",
        "lazy_preview_and_image_batches",
        "empty_thumbnail_cache_prepares_and_displays",
        "thumbnail_prepare_skips_hidden_and_stale_pages",
        "single_entry_subpages_keep_state_and_pause_polling",
        "visible_poll_backoff_and_cleanup",
        "query_backoff_never_replays_action",
        "unknown_batch_stops_and_doubleclick_blocks",
        "webui_only_retry_confirm",
    ],
)
def test_built_pages_with_offline_fake_host_bridge(scenario: str) -> None:
    if not shutil.which("node"):
        pytest.skip("此离线协议测试需要 Node；未执行不应宣称交互通过")
    controls = {}
    scripts = {}
    for page_id in ("gallery", "tasks"):
        document = (ROOT / "webui" / "gallery.html").read_text(encoding="utf-8")
        parser = DocumentContract()
        parser.feed(document)
        controls[page_id] = parser.controls
        scripts[page_id] = re.findall(r"<script>(.*?)</script>", document, re.S)
    result = subprocess.run(
        ["node", "--input-type=commonjs", "-e", NODE_HARNESS],
        input=json.dumps(
            {"scenario": scenario, "declaration": DECLARATION, "controls": controls, "scripts": scripts},
            ensure_ascii=False,
        ),
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, f"{scenario}\n{result.stdout}\n{result.stderr}"
    assert json.loads(result.stdout)["passed"] == scenario
