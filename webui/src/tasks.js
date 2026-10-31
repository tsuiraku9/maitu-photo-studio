(() => {
  "use strict";
  const U = window.PhotoUI;
  const { $, element, button } = U;
  let filters = { status: "", origin: "", kind: "", task_id: "", offset: 0, limit: 12 };
  let galleryFilters = {};
  let items = [];
  let total = 0;
  let task = null;
  let generation = 0;
  let detailGeneration = 0;
  let loading = false;
  let pollingTimer = null;
  let pollFailures = 0;
  let disposed = false;

  function taskId(value) {
    const id = value.task_id || value.id;
    if (typeof id !== "string" || !id) throw new Error("任务缺少有效任务号。");
    return id;
  }
  function taskLabel(value) { return U.operations[value.operation] || U.operations[value.kind] || "其它处理任务"; }
  function originLabel(value) { return U.origins[value.origin] || "其它来源（只读）"; }
  function canMutate(value, operation) {
    return value.origin === "webui" && (operation === "cancel" ? value.can_cancel === true : value.can_retry === true);
  }
  function updatePagination() {
    $("previous-task-page").disabled = loading || U.busy || filters.offset === 0;
    $("next-task-page").disabled = loading || U.busy || filters.offset + filters.limit >= total;
    $("refresh-tasks").disabled = loading || U.busy;
  }
  function updateDetailControls() {
    U.unavailable($("task-cancel"), !task || !canMutate(task, "cancel"));
    U.unavailable($("task-retry"), !task || !canMutate(task, "retry"));
    U.unavailable($("task-view-asset"), !task || !task.asset_id);
    $("task-load-preview").disabled = !task?.result_available || U.busy;
    $("task-copy-id").disabled = !task || U.busy;
    $("task-close").disabled = U.busy;
    $("task-refresh-detail").disabled = loading || U.busy;
  }
  function renderTasks() {
    const list = $("task-list");
    list.replaceChildren();
    $("task-empty").hidden = items.length !== 0;
    $("task-table-wrap").hidden = items.length === 0;
    for (const entry of items) {
      const row = element("tr");
      const identity = element("td");
      identity.append(element("p", { class: "mono", title: taskId(entry) }, taskId(entry).slice(0, 12)), element("p", { class: "help" }, originLabel(entry)));
      const operation = element("td", {}, taskLabel(entry));
      const state = element("td");
      state.append(element("span", { class: `badge status-${entry.status}` }, U.taskStatuses[entry.status] || "未知状态"), element("p", { class: "help" }, entry.paid_request_started ? "模型请求已开始，可能已计费" : "尚未标记模型请求开始"));
      if (entry.delivery_status === "sent") state.append(element("p", { class: "help" }, entry.notification_failed ? "图片已投递 · 通知失败" : entry.planner_notified ? "图片已投递 · 已通知" : "图片已投递"));
      if (entry.error) state.append(element("p", { class: "task-error" }, String(entry.error)));
      const assetCell = element("td");
      if (entry.asset_id) assetCell.append(element("p", { class: "mono" }, `素材：${entry.asset_id}`));
      if (entry.asset_status) assetCell.append(element("p", { class: "help" }, `素材状态：${U.assetStatuses[entry.asset_status] || "未知状态"}`));
      if (entry.batch_id) assetCell.append(element("p", { class: "mono" }, `批次：${entry.batch_id}`));
      if (entry.parent_task_id) assetCell.append(element("p", { class: "mono" }, `父任务：${entry.parent_task_id.slice(0, 12)}`));
      if (entry.output_image?.size_kb !== undefined) assetCell.append(element("p", { class: "help" }, `发送图 ${Number(entry.output_image.size_kb).toFixed(2)} KB`));
      if (!entry.asset_id && !entry.batch_id) assetCell.append(element("span", { class: "help" }, entry.result_available ? "生成结果可查看" : entry.result_cleaned ? "结果已清理" : "暂无结果"));
      const created = element("td", {}, U.formatTime(entry.created_at));
      const actions = element("td");
      const toolbar = element("div", { class: "toolbar" });
      toolbar.append(button("详情", () => openTask(taskId(entry)), { disabled: U.busy }));
      if (entry.origin !== "webui") toolbar.append(element("span", { class: "help" }, "只读"));
      else {
        const cancel = button("取消", () => mutateTask("cancel", entry), { "data-mutate": "", class: "danger" });
        const retry = button("重试", () => mutateTask("retry", entry), { "data-mutate": "" });
        U.unavailable(cancel, !canMutate(entry, "cancel"));
        U.unavailable(retry, !canMutate(entry, "retry"));
        toolbar.append(cancel, retry);
      }
      actions.append(toolbar);
      row.append(identity, operation, state, assetCell, created, actions);
      [identity, operation, state, assetCell, created, actions].forEach((cell, index) => cell.setAttribute("data-label", ["任务与来源", "处理方式", "状态", "结果与关联", "创建时间", "操作"][index]));
      list.append(row);
    }
    $("task-page-label").textContent = total ? `第 ${Math.floor(filters.offset / filters.limit) + 1} 页 · 共 ${total} 条任务` : "共 0 条任务";
    $("task-total").textContent = String(total);
    updatePagination();
  }
  function metadata(label, value) {
    $("task-metadata").append(element("dt", {}, label), element("dd", {}, value));
  }
  function renderDetail() {
    if (!task) return;
    $("task-metadata").replaceChildren();
    metadata("任务号", taskId(task));
    metadata("处理方式", taskLabel(task));
    metadata("来源", originLabel(task));
    metadata("任务状态", U.taskStatuses[task.status] || "未知状态");
    metadata("图片投递", ({ sent: "已成功投递", failed: "投递失败", pending: "等待投递", none: "此任务不投递图片" })[task.delivery_status] || "暂无记录");
    metadata("Planner 通知", task.notification_failed ? "通知失败，图片投递状态见上方" : task.planner_notified ? "已通知" : "尚未通知或未启用");
    metadata("模型请求", task.paid_request_started ? "已经开始，可能已产生费用" : "尚未标记开始；不代表重试免费");
    metadata("素材号", task.asset_id || "暂无");
    metadata("素材状态", task.asset_status ? U.assetStatuses[task.asset_status] || "未知状态" : "暂无");
    metadata("批次号", task.batch_id || "不是批量任务");
    if (task.parent_task_id) metadata("父任务", task.parent_task_id);
    if (Array.isArray(task.references)) metadata("使用参考图", task.references.filter((entry) => entry.asset_id).map((entry) => `${U.categories[entry.role] || entry.role}：${entry.asset_id}`).join("\n") || "未使用参考图");
    if (task.output_image?.original_kb !== undefined) {
      metadata("图片大小", `${Number(task.output_image.original_kb).toFixed(2)} KB → ${Number(task.output_image.size_kb).toFixed(2)} KB`);
      metadata("发送压缩", task.output_image.enabled ? task.output_image.compressed ? "已压缩" : "未超限，原样发送" : "沿用原图（历史策略）");
      if (task.output_image.width) metadata("发送图尺寸", `${task.output_image.width} × ${task.output_image.height}`);
    }
    metadata("生成结果", task.result_available ? "可查看" : task.result_cleaned ? "结果已清理，保留任务记录" : "暂无可用结果");
    metadata("创建时间", U.formatTime(task.created_at));
    if (task.updated_at) metadata("更新时间", U.formatTime(task.updated_at));
    $("task-detail-error").hidden = !task.error;
    $("task-detail-error").textContent = task.error ? String(task.error) : "";
    $("task-readonly").hidden = task.origin === "webui";
    $("task-paid-note").hidden = !task.paid_request_started;
    updateDetailControls();
  }
  async function readDetail(id) {
    const current = ++detailGeneration;
    const result = await U.query("tasks", "detail", { task_id: id });
    if (current !== detailGeneration || disposed) return null;
    if (!U.plainObject(result.task) || taskId(result.task) !== id) throw new Error("任务详情响应与查询不一致。");
    task = result.task;
    renderDetail();
    return task;
  }
  async function openTask(id) {
    if (U.busy) return;
    $("task-result-image").hidden = true;
    $("task-preview-note").textContent = "预览按需加载，不调用模型。";
    try { if (await readDetail(id)) $("task-dialog").showModal(); }
    catch (error) { U.notify(U.message(error), true); }
  }
  function closeTask() {
    if (U.busy) return;
    detailGeneration += 1;
    task = null;
    $("task-dialog").close();
  }
  async function loadPreview() {
    if (!task?.result_available || U.busy) return;
    const id = taskId(task);
    await U.runExclusive(async () => {
      let preview = await U.query("tasks", "image", { task_id: id });
      if (!preview.ready && preview.available) {
        await U.action("prepare_task_preview", { task_id: id }, U.requestId(), undefined, { tracked: false });
        preview = await U.query("tasks", "image", { task_id: id });
      }
      if (!task || taskId(task) !== id) return;
      if (!preview.ready) { $("task-preview-note").textContent = "结果不可用或已清理，请刷新详情。"; return; }
      U.setImage($("task-result-image"), preview.data_url);
      $("task-result-image").hidden = false;
      $("task-preview-note").textContent = "此处为预览副本，原图按结果保留策略保存。";
    }, "正在准备结果预览…");
  }
  async function copyTaskId() {
    if (!task) return;
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(taskId(task));
      else {
        const field = element("textarea");
        field.value = taskId(task);
        field.setAttribute("aria-label", "待复制任务号");
        $("task-dialog").append(field);
        try {
          field.select();
          if (!document.execCommand("copy")) throw new Error("复制不可用");
        } finally { field.remove(); }
      }
      U.notify("任务号已复制。");
    } catch (_) { U.notify("请在详情中选中完整任务号复制。"); }
  }
  function schedulePoll() {
    if (pollingTimer !== null) clearTimeout(pollingTimer);
    pollingTimer = null;
    if (disposed || U.activeView !== "tasks" || !$("task-poll").checked) { $("poll-status").textContent = "自动刷新已关闭。"; return; }
    if (document.visibilityState === "hidden") { $("poll-status").textContent = "页面已隐藏，轮询暂停。"; return; }
    const interval = Math.min(60000, 5000 * (2 ** pollFailures));
    $("poll-status").textContent = pollFailures ? `查询失败后退避，约 ${interval / 1000} 秒后再查。` : "页面可见时每 5 秒只读刷新。";
    pollingTimer = setTimeout(() => {
      pollingTimer = null;
      if (disposed || U.activeView !== "tasks" || document.visibilityState === "hidden" || !$("task-poll").checked) return;
      if (U.busy || loading) { schedulePoll(); return; }
      void refreshTasks(true);
    }, interval);
  }
  async function refreshTasks(poll = false) {
    if (disposed || U.activeView !== "tasks" || document.visibilityState === "hidden") { schedulePoll(); return; }
    if (loading) return;
    const current = ++generation;
    loading = true;
    $("task-list").setAttribute("aria-busy", "true");
    updatePagination();
    try {
      // 失败的查询可指数退避；轮询不重试 action，也不创建新任务。
      const result = await U.query("tasks", "list", U.clone(filters), { retries: poll ? 0 : 2 });
      if (current !== generation || disposed) return;
      if (!Array.isArray(result.items) || !Number.isInteger(result.total) || result.total < 0) throw new Error("任务列表结构不符合约定。");
      result.items.forEach(taskId);
      items = result.items;
      total = result.total;
      if (!items.length && filters.offset >= total && filters.offset > 0) {
        filters.offset = Math.max(0, Math.floor(Math.max(0, total - 1) / filters.limit) * filters.limit);
        loading = false;
        return refreshTasks(poll);
      }
      renderTasks();
      if (task && $("task-dialog").open) await readDetail(taskId(task));
      pollFailures = 0;
    } catch (error) {
      if (current === generation) {
        pollFailures = Math.min(4, pollFailures + 1);
        $("task-page-label").textContent = "查询失败，请刷新重试";
        if (!poll || pollFailures === 1) U.notify(U.message(error), true);
      }
    } finally {
      if (current === generation) { loading = false; $("task-list").setAttribute("aria-busy", "false"); updatePagination(); }
      schedulePoll();
    }
  }
  async function mutateTask(operation, snapshot) {
    await U.runExclusive(async () => {
      U.requireResolved();
      const id = taskId(snapshot);
      // 不信任旧列表按钮；在发起写操作之前重新核对来源和能力。
      const result = await U.query("tasks", "detail", { task_id: id });
      if (!U.plainObject(result.task) || taskId(result.task) !== id || !canMutate(result.task, operation)) throw new Error("任务来源或状态已经改变，此任务目前不可执行该操作。请刷新列表。");
      const params = { task_id: id };
      const request = U.requestId();
      let token;
      if (operation === "retry") {
        token = await U.confirmAction("retry", params, request, { title: "二次确认：重试任务与模型费用", description: "重试可能再次调用视觉模型或图像生成模型并计费。原任务已经发生的费用不会退回；本页面不会自动重试。", approve: "确认费用并重试" });
        if (!token) return;
      } else {
        const accepted = await U.ask({ title: "取消网页任务", description: "只会取消允许取消的后续处理。模型请求如果已经开始，费用不会退回；不能保证中止已经在执行的模型调用。", summary: `任务号：${id}`, approve: "确认取消任务", danger: true });
        if (!accepted) return;
      }
      const mutation = await U.action(operation, params, request, token);
      U.notify(operation === "cancel" ? "已提交取消请求，请以刷新后的任务状态为准。" : `重试请求已受理${mutation.task_id ? `，任务号：${mutation.task_id}` : ""}。`);
      await refreshTasks(false);
    }, operation === "cancel" ? "正在确认取消任务…" : "正在确认重试费用…");
    schedulePoll();
  }
  function applyFilters() {
    if (U.busy || loading) return;
    filters = { status: $("task-status-filter").value, origin: $("task-origin-filter").value, kind: $("task-kind-filter").value, task_id: $("task-id-filter").value.trim(), offset: 0, limit: Number($("task-page-size").value) };
    const after = $("task-after-filter").value;
    const before = $("task-before-filter").value;
    if (after) filters.created_after = new Date(`${after}T00:00:00`).toISOString();
    if (before) { const end = new Date(`${before}T00:00:00`); end.setDate(end.getDate() + 1); filters.created_before = end.toISOString(); }
    void refreshTasks(false);
  }
  function bind() {
    $("nav-gallery").addEventListener("click", () => U.navigate("gallery", { gallery_filters: galleryFilters }));
    $("apply-task-filters").addEventListener("click", applyFilters);
    $("refresh-tasks").addEventListener("click", () => { if (!U.busy) void refreshTasks(false); });
    $("previous-task-page").addEventListener("click", () => { if (U.busy || loading) return; filters.offset = Math.max(0, filters.offset - filters.limit); void refreshTasks(false); });
    $("next-task-page").addEventListener("click", () => { if (U.busy || loading) return; filters.offset += filters.limit; void refreshTasks(false); });
    $("task-poll").addEventListener("change", schedulePoll);
    $("task-close").addEventListener("click", closeTask);
    $("task-load-preview").addEventListener("click", loadPreview);
    $("task-copy-id").addEventListener("click", copyTaskId);
    $("task-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closeTask(); });
    $("task-refresh-detail").addEventListener("click", () => { if (task && !U.busy) readDetail(taskId(task)).catch((error) => U.notify(U.message(error), true)); });
    $("task-view-asset").addEventListener("click", () => { if (task?.asset_id) U.navigate("gallery", { asset_id: task.asset_id, gallery_filters: galleryFilters }); });
    $("task-cancel").addEventListener("click", () => task && mutateTask("cancel", task));
    $("task-retry").addEventListener("click", () => task && mutateTask("retry", task));
    document.addEventListener("photo:lock", () => { updatePagination(); updateDetailControls(); schedulePoll(); });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") schedulePoll();
      else if (U.activeView === "tasks" && $("task-poll").checked && !U.busy && !loading) void refreshTasks(true);
      else schedulePoll();
    });
    document.addEventListener("photo:receipt", () => { if (U.activeView === "tasks" && !U.busy && !loading && document.visibilityState !== "hidden") void refreshTasks(false); });
    document.addEventListener("photo:view", schedulePoll);
    window.addEventListener("pagehide", () => { disposed = true; if (pollingTimer !== null) clearTimeout(pollingTimer); });
  }
  let bound = false;
  async function start(state) {
    if (U.plainObject(state.gallery_filters)) galleryFilters = state.gallery_filters;
    if (!bound) { bind(); bound = true; }
    $("busy-label").textContent = "";
    updateDetailControls();
    await refreshTasks(false);
    if (typeof state.task_id === "string" && state.task_id) await openTask(state.task_id);
  }
  U.registerView("tasks", start);
})();
