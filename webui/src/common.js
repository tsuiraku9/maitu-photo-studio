/* 子页面共用的官方桥接客户端：只查询可退避，action/upload 不自动重放。 */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const plainObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const categories = { person: "人物", outfit: "服装", scene: "场景" };
  const assetStatuses = { active: "已启用", disabled: "已停用", needs_review: "待审核", deleted: "已删除" };
  const taskStatuses = { queued: "排队中", running: "处理中", generated: "已生成", sent: "已投递", completed: "已完成", failed: "失败", cancelled: "已取消" };
  const operations = { photo: "人物写真", scene_photo: "场景照片", image: "场景照片（历史）", notification_retry: "通知重试", import: "导入参考图", extract: "提取并生成参考板", replace: "替换参考图", retag: "重新标注", regenerate: "重新生成参考板", generate_person: "生成人物参考板" };
  const origins = { webui: "网页", qq: "聊天 / QQ", automatic: "自动任务", startup: "启动扫描", unknown: "未知来源" };
  const views = new Map();
  let activeView = "";
  let initialization = null;
  const receipts = new Map();
  let queue = Promise.resolve();
  let exclusive = false;
  let confirmationResolver = null;

  function element(tag, attributes = {}, text) {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attributes)) {
      if (name === "class") node.className = value;
      else if (name === "disabled" || name === "checked") node[name] = !!value;
      else node.setAttribute(name, String(value));
    }
    if (text !== undefined) node.textContent = String(text);
    return node;
  }
  function button(text, onClick, attributes = {}) {
    const node = element("button", { type: "button", ...attributes }, text);
    node.addEventListener("click", onClick);
    return node;
  }
  function message(error) {
    const raw = error instanceof Error ? error.message : String(error);
    if (/timed out|timeout|超时/i.test(raw)) return "请求超时，操作可能已受理。请按原请求号查询回执，不要重复提交。";
    if (/cancelled|AbortError/i.test(raw)) return "操作已取消；已收到的暂存图片不会自动撤回。";
    if (/two requests|concurrency limit/i.test(raw)) return "宿主已有请求正在执行，请稍后重试查询。";
    if (/upload connection failed/i.test(raw)) return "上传连接中断，请查询回执；不要自动重新上传。";
    return raw;
  }
  function notify(text, error = false) {
    const node = $("notice");
    $("notice-text").textContent = text;
    node.hidden = !text;
    node.classList.toggle("error", error);
    node.setAttribute("role", error ? "alert" : "status");
  }
  function serial(work) {
    const next = queue.then(work, work);
    queue = next.catch(() => undefined);
    return next;
  }
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  function ensureVisible() {
    if (document.visibilityState === "hidden") throw new Error("页面已隐藏，暂停查询；返回页面后可刷新。");
  }
  function response(result, allowPartial = false) {
    if (!plainObject(result)) throw new Error("服务端返回结构不符合约定。");
    if (result.success === false && !(allowPartial && Array.isArray(result.items))) {
      const error = new Error(String(result.error || result.message || "操作被服务端拒绝，请刷新数据后检查。"));
      error.definite = true;
      throw error;
    }
    return result;
  }
  async function query(alias, operation, args = {}, options = {}) {
    const retries = options.retries ?? 2;
    for (let attempt = 0; ; attempt += 1) {
      if (options.visible !== false) ensureVisible();
      try {
        return response(await serial(() => {
          if (options.visible !== false) ensureVisible();
          return window.maibot.query(alias, { operation, args: clone(args) });
        }));
      } catch (error) {
        if (error.definite || attempt >= retries || document.visibilityState === "hidden") throw error;
        await sleep(Math.min(8000, 800 * (2 ** attempt)));
      }
    }
  }
  function requestId() {
    // HTTP 宿主也可使用 getRandomValues，不依赖仅安全上下文可用的 randomUUID。
    return Array.from(crypto.getRandomValues(new Uint32Array(4)), (value) => value.toString(16).padStart(8, "0")).join("");
  }
  function updateLocks() {
    document.querySelectorAll("[data-mutate], [data-navigation]").forEach((node) => {
      node.disabled = exclusive || node.dataset.unavailable === "true";
    });
    document.dispatchEvent(new CustomEvent("photo:lock", { detail: exclusive }));
  }
  function unavailable(node, value) {
    node.dataset.unavailable = String(!!value);
    node.disabled = exclusive || !!value;
  }
  async function runExclusive(work, label = "正在处理，请勿重复点击。") {
    if (exclusive) return;
    exclusive = true;
    updateLocks();
    $("busy-label").textContent = label;
    try { return await work(); }
    catch (error) { notify(message(error), true); }
    finally { exclusive = false; $("busy-label").textContent = ""; updateLocks(); }
  }
  function isUncertain() {
    return Array.from(receipts.values()).some((item) => item.state === "unknown" || item.state === "preparing");
  }
  function requireResolved() {
    if (isUncertain()) throw new Error("有请求的受理结果尚未确定。请先在「请求回执」中找回结果，避免重复操作。");
  }
  async function action(operation, args, id = requestId(), token, options = {}) {
    const tracked = options.tracked !== false;
    const params = { operation, request_id: id, args: clone(args) };
    if (token) params.confirm_token = token;
    if (tracked) {
      if (receipts.has(id)) throw new Error("这个请求号已经发送；只能查询原请求的回执，不能重放操作。");
      receipts.set(id, { request_id: id, operation, state: "preparing", task_id: "", response: null });
      renderReceipts();
    }
    try {
      // 所有 action 只有一次调用，没有重试循环，包括超时后。
      const result = response(await serial(() => window.maibot.action("manage", params)), ["enable", "disable", "delete"].includes(operation));
      if (tracked) {
        receipts.set(id, { request_id: id, operation, state: result.success === false ? "failed" : "accepted", task_id: result.task_id || "", response: result });
        renderReceipts();
      }
      return result;
    } catch (error) {
      if (tracked) {
        const item = receipts.get(id);
        item.state = error.definite ? "failed" : "unknown";
        item.error = message(error);
        renderReceipts();
        if (!error.definite) {
          notify(`请求结果未知。保留原请求号 ${id}，正在尝试只读查询回执。`, true);
          let found = null;
          try { found = await lookupReceipt(id); }
          catch (_) { /* 原请求号保留在回执面板；查询失败不触发 action。 */ }
          if (found && found.response && ["accepted", "completed", "failed"].includes(found.state)) return response(found.response, ["enable", "disable", "delete"].includes(operation));
        }
      }
      throw error;
    }
  }
  async function upload(file, id, options = {}) {
    if (receipts.has(id)) throw new Error("这个上传请求号已经发送，请查询回执，不要重复上传。");
    receipts.set(id, { request_id: id, operation: "stage", state: "preparing", task_id: "", response: null });
    renderReceipts();
    try {
      const result = response(await serial(() => window.maibot.upload("library", file, { request_id: id }, options)));
      receipts.set(id, { request_id: id, operation: "stage", state: "completed", task_id: "", response: result });
      renderReceipts();
      return result;
    } catch (error) {
      const item = receipts.get(id);
      item.state = error.definite ? "failed" : "unknown";
      item.error = message(error);
      renderReceipts();
      throw error;
    }
  }
  async function lookupReceipt(id) {
    if (!id || id.length > 128) throw new Error("请输入有效的请求号（最多 128 字符）。");
    const result = await query("tasks", "receipt", { request_id: id });
    const receipt = result.receipt;
    if (receipt === null || receipt === undefined) {
      notify("暂未找到回执，不能据此判定请求未执行。请保留请求号，稍后再次查询。", true);
      return null;
    }
    if (!plainObject(receipt) || receipt.request_id !== id) throw new Error("回执请求号与查询不一致。");
    if (!["preparing", "accepted", "completed", "failed"].includes(receipt.state)) throw new Error("回执状态不符合约定，原请求仍视为未知。");
    const previous = receipts.get(id) || { operation: "unknown" };
    const next = { ...previous, ...receipt };
    if (!plainObject(next.response)) {
      next.response = null;
      next.state = receipt.state === "preparing" ? "preparing" : "unknown";
    }
    receipts.set(id, next);
    renderReceipts();
    if (next.state === "preparing" || !next.response) notify("请求仍在准备或结果尚未知，请等待并再次查询，不要重新提交。", true);
    else notify(`已找回请求回执${next.task_id ? `，任务号：${next.task_id}` : ""}。`);
    document.dispatchEvent(new CustomEvent("photo:receipt", { detail: next }));
    return next;
  }
  function renderReceipts() {
    const list = $("receipt-list");
    if (!list) return;
    list.replaceChildren();
    const items = Array.from(receipts.values()).slice(-40).reverse();
    $("receipt-count").textContent = items.length ? `（${items.length}）` : "";
    if (!items.length) list.append(element("p", { class: "help" }, "暂无当前页面的请求。刷新前请记下尚未确定的请求号；隔离页面不保存登录凭证。"));
    const states = { preparing: "准备中，暂勿重交", unknown: "结果未知，请查询", accepted: "已受理", completed: "已完成", failed: "已失败" };
    for (const item of items) {
      const row = element("div", { class: "receipt-row" });
      const label = item.operation === "stage" ? "图片暂存" : operations[item.operation] || { submit: "提交任务", delete: "删除素材", edit: "编辑素材", enable: "启用素材", disable: "停用素材", cancel: "取消任务", retry: "重试任务" }[item.operation] || "请求";
      row.append(element("p", {}, `${label} · ${states[item.state] || "结果未知"}`), element("code", { class: "receipt-id" }, item.request_id));
      if (item.task_id) row.append(element("p", { class: "help" }, `任务号：${item.task_id}`));
      if (item.error && ["unknown", "failed"].includes(item.state)) row.append(element("p", { class: "help" }, item.error));
      row.append(button("查询回执", () => { lookupReceipt(item.request_id).catch((error) => notify(message(error), true)); }));
      list.append(row);
    }
  }
  function ask({ title, description, summary = "", approve = "确认提交", danger = false }) {
    if (confirmationResolver) throw new Error("已有确认窗口，请先完成或取消。");
    $("confirm-title").textContent = title;
    $("confirm-description").textContent = description;
    $("confirm-summary").textContent = summary;
    $("confirm-approve").textContent = approve;
    $("confirm-approve").classList.toggle("danger", danger);
    $("confirm-approve").classList.toggle("primary", !danger);
    const dialog = $("confirm-dialog");
    dialog.showModal();
    return new Promise((resolve) => { confirmationResolver = resolve; });
  }
  function finishConfirmation(accepted) {
    if (!confirmationResolver) return;
    const resolve = confirmationResolver;
    confirmationResolver = null;
    $("confirm-dialog").close();
    resolve(accepted);
  }
  async function confirmAction(operation, params, targetId, display) {
    const result = await action("confirm", { action: operation, params: clone(params), target_request_id: targetId }, requestId(), undefined, { tracked: false });
    const confirmation = result.confirmation;
    if (!plainObject(confirmation) || typeof confirmation.token !== "string" || !confirmation.token) throw new Error("服务端未返回有效确认凭证，未提交任务。");
    const expires = expiryMillis(confirmation.expires_at);
    if (!Number.isFinite(expires)) throw new Error("服务端确认有效期不正确，未提交任务。");
    const accepted = await ask({ ...display, summary: String(confirmation.summary || "") + `\n确认有效至：${formatTime(confirmation.expires_at)}` });
    if (!accepted) return null;
    if (Date.now() >= expires) throw new Error("确认已过期，未提交任务。请重新发起并确认。");
    return confirmation.token;
  }
  function expiryMillis(value) {
    // expires_at 是后端 Unix 秒；ISO 时间只用于素材版本／创建时间兼容。
    return typeof value === "number" && Number.isFinite(value) ? value * 1000 : typeof value === "string" ? Date.parse(value) : NaN;
  }
  function formatTime(value) {
    if (value === null || value === undefined || value === "") return "未提供";
    const date = new Date(expiryMillis(value));
    return Number.isFinite(date.getTime()) ? date.toLocaleString("zh-CN", { hour12: false }) : String(value);
  }
  function safeImage(value) {
    if (typeof value !== "string" || value.length > 512 * 1024) return null;
    return /^data:image\/(?:jpeg|png|webp);base64,[A-Za-z0-9+/=]+$/.test(value) ? value : null;
  }
  function setImage(node, value) {
    const safe = safeImage(value);
    if (!safe) { node.removeAttribute("src"); node.hidden = true; return false; }
    node.src = safe;
    node.hidden = false;
    return true;
  }
  function applyContext() {
    const context = window.maibot.context;
    document.documentElement.classList.toggle("dark", context.theme === "dark");
    // 业务界面固定简体中文；主题变量和主题切换由宿主提供。
    document.documentElement.lang = "zh-CN";
    document.documentElement.style.colorScheme = context.theme === "dark" ? "dark" : "light";
  }
  async function navigate(page, state = {}) {
    if (exclusive) { notify("请等待当前上传、确认或操作结束再切换页面。", true); return; }
    const carried = Array.from(receipts.values()).filter((item) => ["unknown", "preparing"].includes(item.state)).map((item) => item.request_id).slice(-20);
    state = clone(state);
    if (carried.length) state.receipt_ids = carried;
    if (plainObject(state.gallery_filters) && typeof state.gallery_filters.search === "string") state.gallery_filters.search = state.gallery_filters.search.slice(0, 1000);
    if (new TextEncoder().encode(JSON.stringify(state)).length > 4096) { notify("页面状态超过宿主导航配额，请记下未知请求号后减少搜索文字。", true); return; }
    try {
      if (!views.has(page)) throw new Error("未知子页面");
      activeView = page;
      for (const id of ["gallery", "tasks"]) {
        $(`${id}-view`).hidden = id !== page;
        $(`nav-${id}`).setAttribute("aria-current", id === page ? "page" : "false");
        unavailable($(`nav-${id}`), id === page);
      }
      document.querySelectorAll("dialog").forEach((node) => { if (node.open) node.close(); });
      $("view-title").textContent = page === "gallery" ? "参考图库" : "任务列表";
      document.dispatchEvent(new CustomEvent("photo:view", { detail: { view: page } }));
      await views.get(page)(state);
    }
    catch (error) { notify(message(error), true); }
  }
  function registerView(id, start) { views.set(id, start); }
  async function startStudio() {
    const state = await init();
    await navigate(state.view === "tasks" ? "tasks" : "gallery", state);
  }
  function init() { return initialization ||= initialize(); }
  async function initialize() {
    if (!window.maibot) throw new Error("请从 MaiBot 侧栏的「麦麦写真」打开此页面；独立文件不连接服务。");
    await window.maibot.ready;
    applyContext();
    window.addEventListener("maibot:context", applyContext);
    $("notice-close").addEventListener("click", () => notify(""));
    $("confirm-approve").addEventListener("click", () => finishConfirmation(true));
    $("confirm-cancel").addEventListener("click", () => finishConfirmation(false));
    $("confirm-dialog").addEventListener("cancel", (event) => { event.preventDefault(); finishConfirmation(false); });
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") finishConfirmation(false); });
    $("receipt-lookup").addEventListener("click", () => lookupReceipt($("receipt-input").value.trim()).catch((error) => notify(message(error), true)));
    const state = plainObject(window.maibot.page.state) ? window.maibot.page.state : {};
    if (Array.isArray(state.receipt_ids)) for (const id of state.receipt_ids.slice(0, 20)) {
      if (typeof id === "string" && id.length <= 128) receipts.set(id, { request_id: id, operation: "unknown", state: "unknown", task_id: "", response: null });
    }
    renderReceipts();
    return state;
  }
  function fatal(error) {
    const node = $("notice");
    if (node) notify(message(error), true);
    if ($("busy-label")) $("busy-label").textContent = "未连接宿主，所有操作不可用。";
    document.querySelectorAll("button, input, select, textarea").forEach((node) => { node.disabled = true; });
  }
  window.PhotoUI = { $, element, button, clone, plainObject, categories, assetStatuses, taskStatuses, operations, origins, notify, message, query, action, upload, serial, requestId, runExclusive, unavailable, updateLocks, requireResolved, isUncertain, lookupReceipt, ask, confirmAction, expiryMillis, formatTime, safeImage, setImage, navigate, registerView, startStudio, init, fatal, get activeView() { return activeView; }, get busy() { return exclusive; } };
})();
