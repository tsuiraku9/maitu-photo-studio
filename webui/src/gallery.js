(() => {
  "use strict";
  const U = window.PhotoUI;
  const { $, element, button } = U;
  const TAG_FIELDS = {
    person: [
      { key: "appearance_summary", label: "外貌摘要", type: "string" },
      { key: "confidence", label: "标签置信度（0～1）", type: "number" },
    ],
    outfit: [
      { key: "type", label: "服装类型", type: "string" },
      { key: "wearing_scenes", label: "适用场景（逗号分隔）", type: "array" },
      { key: "seasons", label: "季节（逗号分隔）", type: "array" },
      { key: "styles", label: "风格（逗号分隔）", type: "array" },
      { key: "confidence", label: "标签置信度（0～1）", type: "number" },
    ],
    scene: [
      { key: "room_type", label: "空间类型", type: "string" },
      { key: "privacy_eligible", label: "确认属于室内私密空间", type: "boolean" },
      { key: "scene_signature", label: "场景指纹", type: "string" },
      { key: "confidence", label: "标签置信度（0～1）", type: "number" },
    ],
  };
  const COSTLY = new Set(["extract", "regenerate", "generate_person"]);
  let settings = null;
  let settingsLoading = null;
  let filters = { category: "", status: "", search: "", offset: 0, limit: 12 };
  let items = [];
  let total = 0;
  let listGeneration = 0;
  let listLoading = false;
  const selected = new Map();
  let asset = null;
  let detailGeneration = 0;
  let manualDraft = {};
  let tagsValid = true;
  const preparedPreviews = new Set();
  const uploads = { rows: [], replacement: null, stop: false, active: false, controller: null };

  function maxBatch() { return settings ? Math.min(20, Math.max(1, settings.max_batch)) : 20; }
  function personVersion() {
    const person = settings.person;
    return person.asset_id ? `${person.asset_id}:${person.version}` : "none";
  }
  function versionOf(value) {
    if (!value.updated_at || typeof value.updated_at !== "string") throw new Error("素材缺少更新时间，不能安全修改，请刷新后重试。");
    return value.updated_at;
  }
  async function loadSettings(force = false) {
    if (settings && !force) return settings;
    if (settingsLoading) return settingsLoading;
    settingsLoading = U.query("gallery", "settings", {}).then((result) => {
      if (typeof result.auto_enable !== "boolean" || !Number.isInteger(result.page_size) || !Number.isInteger(result.max_batch) || !U.plainObject(result.person) || typeof result.person.asset_id !== "string" || typeof result.person.version !== "string") throw new Error("配置查询结构不符合约定，写操作已停止。");
      if (result.person.asset_id && !result.person.version) throw new Error("人物配置缺少版本，不能安全覆盖。");
      settings = result;
      $("settings-note").textContent = `处理后${settings.auto_enable ? "自动启用合格素材" : "保留为停用素材"} · 每批上限 ${maxBatch()} 张 · 人物单张`;
      return settings;
    }).finally(() => { settingsLoading = null; });
    return settingsLoading;
  }
  function filterControls(value) {
    $("category-filter").value = value.category;
    $("status-filter").value = value.status;
    $("search-filter").value = value.search;
    const option = Array.from($("page-size").options).find((item) => Number(item.value) === value.limit);
    if (!option) $("page-size").append(element("option", { value: value.limit }, `${value.limit} 张`));
    $("page-size").value = String(value.limit);
  }
  function applyFilters() {
    if (U.busy) { U.notify("请等待当前操作完成再更改筛选。"); return; }
    filters = { category: $("category-filter").value, status: $("status-filter").value, search: $("search-filter").value.trim(), offset: 0, limit: Number($("page-size").value) };
    refreshGallery();
  }
  function updateSelection() {
    $("selection-count").textContent = selected.size ? `已选择 ${selected.size} 张（最多 ${maxBatch()} 张）` : "未选择素材";
    const pickable = items.filter((item) => item.status !== "deleted");
    $("select-page").checked = pickable.length > 0 && pickable.every((item) => selected.has(item.id));
    $("select-page").indeterminate = selected.size > 0 && !$("select-page").checked;
    const blocked = !settings || !selected.size || listLoading;
    for (const id of ["enable-selected", "disable-selected", "retag-selected", "regenerate-selected", "delete-selected"]) U.unavailable($(id), blocked);
    $("clear-selection").disabled = U.busy || !selected.size;
    $("select-page").disabled = U.busy || listLoading || !pickable.length;
    $("previous-page").disabled = U.busy || listLoading || filters.offset === 0;
    $("next-page").disabled = U.busy || listLoading || filters.offset + filters.limit >= total;
  }
  function tagSummary(value) {
    const tags = value.effective_tags || {};
    return (TAG_FIELDS[value.category] || []).filter((field) => field.key !== "confidence" && tags[field.key] !== undefined).map((field) => {
      const part = tags[field.key];
      return `${field.label.replace("（逗号分隔）", "")}：${Array.isArray(part) ? part.join("、") : typeof part === "boolean" ? (part ? "是" : "否") : String(part)}`;
    }).join(" · ").slice(0, 200) || "尚无标签";
  }
  function renderGallery(generation) {
    const grid = $("gallery-grid");
    grid.replaceChildren();
    $("gallery-empty").hidden = items.length !== 0;
    const imageNodes = new Map();
    for (const item of items) {
      const card = element("article", { class: "asset-card", "aria-label": `${U.categories[item.category]}：${item.name}` });
      const imageButton = button("", () => openAsset(item.id), { class: "asset-image", "aria-label": `查看 ${item.name} 详情` });
      const image = element("img", { alt: `${item.name} 缩略图`, loading: "lazy", decoding: "async", hidden: "" });
      const placeholder = element("span", { class: "help" }, "正在读取缩略图…");
      imageButton.append(image, placeholder);
      imageNodes.set(item.id, { image, placeholder });
      image.addEventListener("error", () => { image.hidden = true; placeholder.hidden = false; placeholder.textContent = "缩略图未能显示；可打开详情按需预览。"; });
      const content = element("div", { class: "asset-content" });
      const titleRow = element("div", { class: "row" });
      const select = element("input", { type: "checkbox", "aria-label": `选择 ${item.name}`, disabled: item.status === "deleted" });
      select.addEventListener("change", () => {
        if (select.checked && selected.size >= maxBatch()) { select.checked = false; U.notify(`每批最多选择 ${maxBatch()} 张。`, true); return; }
        if (select.checked) selected.set(item.id, U.clone(item)); else selected.delete(item.id);
        card.classList.toggle("selected", select.checked);
        updateSelection();
      });
      select.dataset.assetSelect = item.id;
      titleRow.append(select, element("h3", { class: "asset-title" }, item.name));
      const labels = element("div", { class: "row" });
      labels.append(element("span", { class: "badge" }, U.categories[item.category] || "未知类别"), element("span", { class: "badge" }, U.assetStatuses[item.status] || "未知状态"));
      if (!item.selectable && item.status === "active") labels.append(element("span", { class: "badge" }, "不参与自动选图"));
      content.append(titleRow, labels, element("p", { class: "asset-tags" }, tagSummary(item)), element("p", { class: "help" }, `更新：${U.formatTime(item.updated_at)}`));
      if (item.validation_errors?.length) content.append(element("p", { class: "help" }, `标签待审核：${item.validation_errors.length} 项`));
      content.append(button("详情与标签", () => openAsset(item.id)));
      card.append(imageButton, content);
      grid.append(card);
    }
    $("page-label").textContent = total ? `第 ${Math.floor(filters.offset / filters.limit) + 1} 页 · 共 ${total} 张素材` : "共 0 张素材";
    updateSelection();
    void loadThumbnails(generation, imageNodes);
  }
  async function loadThumbnails(generation, nodes) {
    const current = () => generation === listGeneration && U.activeView === "gallery" && document.visibilityState !== "hidden";
    const snapshot = [...items];
    const showImages = (ids, received) => {
      for (const id of ids) {
        const node = nodes.get(id);
        const image = received.get(id);
        const ready = image?.ready === true && U.setImage(node.image, image.data_url);
        node.placeholder.hidden = !!ready;
        if (!ready) node.placeholder.textContent = "缩略图尚未准备；可刷新重试或打开详情。";
      }
    };
    const imageMap = (result) => {
      if (!U.plainObject(result) || !Array.isArray(result.images)) throw new Error("缩略图响应结构不符合约定。");
      return new Map(result.images.filter((image) => image.variant === "thumbnail").map((image) => [image.asset_id, image]));
    };
    for (let offset = 0; offset < snapshot.length; offset += 6) {
      if (!current()) return;
      const assetIds = snapshot.slice(offset, offset + 6).map((item) => item.id);
      try {
        const received = imageMap(await U.query("gallery", "images", { asset_ids: assetIds, variant: "thumbnail" }));
        if (!current()) return;
        showImages(assetIds, received);
        const missing = assetIds.filter((id) => received.get(id)?.ready !== true || !U.safeImage(received.get(id)?.data_url));
        if (!missing.length) continue;
        for (const id of missing) nodes.get(id).placeholder.textContent = "正在准备缩略图缓存…";
        // 仅生成缩略图缓存，不调用模型；派发前再次检查，避免排队期间页面已变化。
        // 不进入业务回执，也不自动重放这个短 action。
        const prepared = await U.serial(() => {
          if (!current()) return null;
          return window.maibot.action("manage", { operation: "prepare_preview", request_id: U.requestId(), args: { asset_ids: missing, variant: "thumbnail" } });
        });
        if (!current() || prepared === null) return;
        if (!U.plainObject(prepared) || prepared.success === false) throw new Error(String(prepared?.error || "缩略图缓存准备失败。"));
        const images = imageMap(await U.query("gallery", "images", { asset_ids: missing, variant: "thumbnail" }));
        if (!current()) return;
        showImages(missing, images);
      } catch (error) {
        if (!current()) return;
        for (const id of assetIds) nodes.get(id).placeholder.textContent = "缩略图读取失败；可刷新或打开详情。";
      }
    }
  }
  async function refreshGallery() {
    const generation = ++listGeneration;
    const snapshot = U.clone(filters);
    listLoading = true;
    $("gallery-grid").setAttribute("aria-busy", "true");
    updateSelection();
    try {
      const result = await U.query("gallery", "list", snapshot);
      if (generation !== listGeneration) return;
      if (!Array.isArray(result.items) || !Number.isInteger(result.total) || result.total < 0) throw new Error("素材列表结构不符合约定。");
      if (result.items.some((item) => !item.id || !TAG_FIELDS[item.category] || typeof item.name !== "string")) throw new Error("素材字段不完整。");
      items = result.items;
      total = result.total;
      selected.clear();
      if (!items.length && filters.offset >= total && filters.offset > 0) {
        filters.offset = Math.max(0, Math.floor(Math.max(0, total - 1) / filters.limit) * filters.limit);
        return refreshGallery();
      }
      renderGallery(generation);
    } catch (error) {
      if (generation === listGeneration) { U.notify(U.message(error), true); $("page-label").textContent = "查询失败，请刷新重试"; }
    } finally {
      if (generation === listGeneration) { listLoading = false; $("gallery-grid").setAttribute("aria-busy", "false"); updateSelection(); }
    }
  }
  function validateManual(value, category) {
    if (!U.plainObject(value)) throw new Error("人工覆盖必须是 JSON 对象。");
    const fields = new Map(TAG_FIELDS[category].map((field) => [field.key, field]));
    for (const [key, entry] of Object.entries(value)) {
      const field = fields.get(key);
      if (!field) throw new Error(`该类别不支持标签字段：${key}`);
      if (field.type === "string" && (typeof entry !== "string" || entry.length > 4000)) throw new Error(`${field.label} 必须是最多 4000 字符的文字。`);
      if (field.type === "boolean" && typeof entry !== "boolean") throw new Error(`${field.label} 必须为布尔值（true / false）。`);
      if (field.type === "number" && (typeof entry !== "number" || !Number.isFinite(entry) || entry < 0 || entry > 1)) throw new Error("置信度必须是 0～1 的数字，不能填百分数。");
      if (field.type === "array" && (!Array.isArray(entry) || entry.length > 100 || entry.some((part) => typeof part !== "string" || part.length > 4000))) throw new Error(`${field.label} 必须是最多 100 项的文字数组。`);
    }
    return value;
  }
  function updateTagView(syncJson = true) {
    $("ai-tags").textContent = JSON.stringify(asset.tags || {}, null, 2);
    $("manual-tags").textContent = JSON.stringify(manualDraft, null, 2);
    $("effective-tags").textContent = JSON.stringify({ ...(asset.tags || {}), ...manualDraft }, null, 2);
    if (syncJson) $("manual-json").value = JSON.stringify(manualDraft, null, 2);
    $("tag-json-error").hidden = true;
    tagsValid = true;
    updateDetailControls();
  }
  function tagError(error) {
    tagsValid = false;
    $("tag-json-error").hidden = false;
    $("tag-json-error").textContent = U.message(error);
    updateDetailControls();
  }
  function renderTagFields() {
    const container = $("tag-fields");
    container.replaceChildren();
    for (const field of TAG_FIELDS[asset.category]) {
      const wrapper = element("div", { class: "tag-field" });
      const override = element("input", { type: "checkbox", checked: Object.hasOwn(manualDraft, field.key) });
      const label = element("label", { class: "check-label" });
      label.append(override, document.createTextNode(`覆盖：${field.label}`));
      const source = Object.hasOwn(manualDraft, field.key) ? manualDraft[field.key] : (asset.tags || {})[field.key];
      let input;
      if (field.type === "boolean") {
        input = element("select", { "aria-label": field.label });
        input.append(element("option", { value: "false" }, "否"), element("option", { value: "true" }, "是，已确认"));
        input.value = String(source === true);
      } else {
        input = element(field.key === "appearance_summary" ? "textarea" : "input", { "aria-label": field.label });
        if (field.type === "number") { input.type = "number"; input.min = "0"; input.max = "1"; input.step = "0.01"; }
        else if (input.tagName === "INPUT") input.type = "text";
        input.maxLength = 4000;
        input.value = field.type === "array" ? (Array.isArray(source) ? source.join(", ") : "") : source === undefined ? "" : String(source);
      }
      input.disabled = !override.checked || asset.status === "deleted" || U.busy;
      override.disabled = asset.status === "deleted" || U.busy;
      const write = () => {
        try {
          if (override.checked) {
            let value = input.value;
            if (field.type === "number") { if (!value.trim()) throw new Error("置信度不能为空；取消勾选可恢复 AI 值。"); value = Number(value); }
            else if (field.type === "boolean") value = value === "true";
            else if (field.type === "array") value = value.split(/[,，]/).map((part) => part.trim()).filter(Boolean);
            const next = { ...manualDraft, [field.key]: value };
            validateManual(next, asset.category);
            manualDraft = next;
          } else delete manualDraft[field.key];
          updateTagView(true);
        } catch (error) { tagError(error); }
      };
      override.addEventListener("change", () => { input.disabled = !override.checked; write(); });
      input.addEventListener("input", write);
      wrapper.append(label, input);
      container.append(wrapper);
    }
  }
  function updateDetailControls() {
    const deleted = !asset || asset.status === "deleted";
    for (const id of ["replace-asset", "asset-retag", "asset-regenerate", "clear-overrides"]) U.unavailable($(id), deleted || !settings);
    U.unavailable($("save-asset"), deleted || !tagsValid || !settings);
    $("asset-name").disabled = deleted || U.busy;
    $("manual-json").disabled = deleted || U.busy;
  }
  async function openAsset(id) {
    if (U.busy) return;
    const generation = ++detailGeneration;
    try {
      const result = await U.query("gallery", "detail", { asset_id: id });
      if (generation !== detailGeneration) return;
      if (!U.plainObject(result.asset) || result.asset.id !== id || !TAG_FIELDS[result.asset.category]) throw new Error("素材详情结构不符合约定。");
      asset = result.asset;
      $("asset-dialog-title").textContent = `${U.categories[asset.category]} · 素材详情`;
      $("asset-name").value = asset.name;
      $("asset-info").textContent = `素材号：${asset.id} · ${U.assetStatuses[asset.status] || "未知状态"} · 更新：${U.formatTime(asset.updated_at)} · ${asset.selectable ? "可参与自动选图" : "不参与自动选图"}`;
      const errors = Array.isArray(asset.validation_errors) ? asset.validation_errors : [];
      $("asset-validation").textContent = errors.map(String).join("\n");
      $("asset-validation").hidden = !errors.length;
      $("asset-preview-area").hidden = true;
      $("asset-preview").hidden = true;
      $("asset-preview").removeAttribute("src");
      $("preview-status").textContent = "预览按需准备；不会下载完整原图。";
      manualDraft = U.clone(asset.manual_tags || {});
      try { validateManual(manualDraft, asset.category); updateTagView(); }
      catch (error) { $("manual-json").value = JSON.stringify(manualDraft, null, 2); tagError(error); }
      renderTagFields();
      if (!$("asset-dialog").open) $("asset-dialog").showModal();
    } catch (error) { U.notify(U.message(error), true); }
  }
  function closeAsset() {
    if (U.busy) { U.notify("请等待当前操作结束再关闭详情。"); return; }
    detailGeneration += 1;
    $("asset-dialog").close();
    $("asset-preview").removeAttribute("src");
    asset = null;
  }
  async function preview(variant) {
    if (!asset) return;
    const snapshot = U.clone(asset);
    const generation = detailGeneration;
    const key = `${snapshot.id}:${snapshot.updated_at}:${variant}`;
    await U.runExclusive(async () => {
      $("preview-status").textContent = "正在准备预览…";
      if (!preparedPreviews.has(key)) {
        await U.action("prepare_preview", { asset_ids: [snapshot.id], variant }, U.requestId(), undefined, { tracked: false });
        preparedPreviews.add(key);
      }
      const result = await U.query("gallery", "images", { asset_ids: [snapshot.id], variant });
      if (generation !== detailGeneration) return;
      if (!Array.isArray(result.images)) throw new Error("预览响应结构不符合约定。");
      const image = result.images.find((entry) => entry.asset_id === snapshot.id && entry.variant === variant);
      const ready = image?.ready === true && U.setImage($("asset-preview"), image.data_url);
      if (!ready) U.setImage($("asset-preview"), null);
      $("asset-preview-area").hidden = !ready;
      $("preview-status").textContent = ready ? `${variant === "source" ? "来源图" : "参考板"}预览已加载（缩放副本）。` : "预览仍在准备；再次点击此按钮只查询状态，不重复准备。";
    }, "正在读取单张图片预览…");
  }
  async function saveAsset() {
    if (!asset || !tagsValid) return;
    const snapshot = U.clone(asset);
    const name = $("asset-name").value.trim();
    const manual = U.clone(manualDraft);
    await U.runExclusive(async () => {
      U.requireResolved();
      if (!name) throw new Error("素材名称不能为空。");
      validateManual(manual, snapshot.category);
      await U.action("edit", { asset_id: snapshot.id, name, manual_tags: manual, expected_version: versionOf(snapshot) });
      U.notify("名称与人工标签已保存；没有调用模型。");
      $("asset-dialog").close();
      asset = null;
      await refreshGallery();
    }, "正在按素材版本保存编辑…");
  }
  function selectionSnapshot() {
    if (!selected.size) throw new Error("请先选择素材。");
    const result = Array.from(selected.values()).map(U.clone);
    if (result.length > maxBatch()) throw new Error(`每批最多 ${maxBatch()} 张。`);
    return result;
  }
  async function changeStatus(operation) {
    await U.runExclusive(async () => {
      U.requireResolved();
      const snapshot = selectionSnapshot();
      const params = { ids: snapshot.map((entry) => entry.id), versions: snapshot.map((entry) => ({ asset_id: entry.id, updated_at: versionOf(entry) })) };
      const id = U.requestId();
      let token;
      if (operation === "delete") {
        token = await U.confirmAction("delete", params, id, { title: "二次确认：删除素材", description: "删除包含已选择的素材及其版本。不会因为查询结果变化而删除其它素材；删除后无法从本页面撤销。", approve: "确认删除", danger: true });
        if (!token) return;
      }
      const result = await U.action(operation, params, id, token);
      showBatchResult(operation, result, snapshot);
      await refreshGallery();
    }, "正在处理所选素材，请勿重复点击…");
  }
  function showBatchResult(operation, result, snapshot) {
    const label = { enable: "启用", disable: "停用", delete: "删除" }[operation];
    const outcomes = Array.isArray(result.items) ? result.items : [];
    const successful = outcomes.filter((item) => item.success === true).length;
    $("result-title").textContent = `${label} · 批量操作结果`;
    $("result-summary").textContent = outcomes.length ? `${successful}/${outcomes.length} 项成功。${successful < outcomes.length ? "部分或全部条目未成功；已成功的条目不会回滚。" : "全部条目已完成。"}` : "服务端未返回逐项结果，请刷新图库核实，不能据此重复提交。";
    $("result-items").replaceChildren();
    for (const item of outcomes) {
      const name = snapshot.find((entry) => entry.id === item.asset_id)?.name || item.asset_id;
      const row = element("div", { class: "receipt-row" });
      row.append(element("p", {}, `${name}：${item.success === true ? "成功" : "未成功"}`));
      if (item.success !== true) row.append(element("p", { class: "task-error" }, String(item.error || "服务端未提供失败原因，请刷新后核实。")));
      $("result-items").append(row);
    }
    U.notify($("result-summary").textContent, result.success === false);
    $("result-dialog").showModal();
  }
  function submitParams(entry, operation, batchId) {
    const params = { operation, category: entry.category, name: entry.name, asset_id: entry.id, expected_version: versionOf(entry) };
    if (batchId) params.batch_id = batchId;
    if (entry.category === "person") params.person_version = personVersion();
    return params;
  }
  async function confirmSubmits(signedItems, warning) {
    const single = signedItems.length === 1;
    const operation = single ? "submit" : "batch_submit";
    const params = single ? withoutRequestId(signedItems[0]) : { items: signedItems };
    const target = single ? signedItems[0].request_id : U.requestId();
    return U.confirmAction(operation, params, target, { title: single ? "二次确认：模型费用与覆盖" : "二次确认：整批模型请求", description: warning, approve: single ? "确认并提交" : `确认整批 ${signedItems.length} 项` });
  }
  function withoutRequestId(item) {
    const { request_id: ignored, ...args } = item;
    return args;
  }
  async function submitAssetBatch(operation, targets) {
    await U.runExclusive(async () => {
      U.requireResolved();
      const snapshot = targets ? targets.map(U.clone) : selectionSnapshot();
      await loadSettings(true);
      if (snapshot.some((entry) => entry.category === "person") && snapshot.length !== 1) throw new Error("人物操作只能单张，不能与其它素材一起批量提交。");
      const batchId = snapshot.length > 1 ? U.requestId() : "";
      const signedItems = snapshot.map((entry) => ({ request_id: U.requestId(), ...submitParams(entry, operation, batchId) }));
      let token;
      if (COSTLY.has(operation)) {
        token = await confirmSubmits(signedItems, "将调用图像生成模型并可能覆盖参考板，产生模型费用。整批只确认一次，之后按已签名参数逐项串行提交；取消仅阻止后续提交，已开始的费用不会退回。");
        if (!token) return;
      } else {
        const approved = await U.ask({ title: "提交重新标注", description: "提交即调用视觉模型（VLM），可能计费。AI 标签会更新，人工覆盖仍保留；不是只修改本地标签。", summary: `${snapshot.length} 张素材：${snapshot.map((entry) => entry.name).join("、")}`, approve: "提交并调用 VLM" });
        if (!approved) return;
      }
      let accepted = 0;
      for (const item of signedItems) {
        if (document.visibilityState === "hidden") throw new Error("页面已隐藏，未提交的后续任务已停止。已提交任务可在任务列表中查看。");
        const result = await U.action("submit", withoutRequestId(item), item.request_id, token);
        accepted += 1;
        U.notify(`已受理 ${accepted}/${signedItems.length} 项${result.task_id ? `，最近任务号：${result.task_id}` : ""}。`);
      }
      if (asset) { $("asset-dialog").close(); asset = null; }
      await refreshGallery();
    }, "正在处理素材任务，遇到结果未知将停止后续提交…");
  }
  function revokeRows() {
    for (const row of uploads.rows) URL.revokeObjectURL(row.preview);
    uploads.rows = [];
  }
  function closeUpload() {
    if (uploads.active || U.busy) { U.notify("请先停止后续操作，并等待当前请求返回再关闭。"); return; }
    revokeRows();
    $("upload-dialog").close();
    uploads.replacement = null;
  }
  async function openUpload(replacement = null) {
    if (U.busy) return;
    try {
      await loadSettings(true);
      revokeRows();
      if (asset) { $("asset-dialog").close(); asset = null; }
      uploads.replacement = replacement ? U.clone(replacement) : null;
      uploads.stop = false;
      $("upload-title").textContent = replacement ? `替换参考图 · ${replacement.name}` : "上传图片";
      $("upload-category").value = replacement ? replacement.category : (filters.category || "outfit");
      $("upload-category").disabled = !!replacement;
      $("upload-operation").value = replacement ? "replace" : "import";
      $("upload-operation").disabled = !!replacement;
      $("upload-files").value = "";
      updateUploadRisk();
      renderUploads();
      $("upload-dialog").showModal();
    } catch (error) { U.notify(U.message(error), true); }
  }
  function updateUploadRisk() {
    const category = $("upload-category").value;
    const operation = $("upload-operation").value;
    $("upload-files").multiple = category !== "person" && !uploads.replacement;
    $("upload-risk").textContent = operation === "extract" ? "提取会调用视觉模型并生成参考板，可能产生两类模型费用；暂存完成后整批只做一次费用确认。人物参考覆盖也包含在确认中。" : category === "person" ? "人物仅允许单张。导入／替换人物即使不产生图像生成费用，也会再次确认当前人物版本与覆盖；视觉标注仍可能计费。" : "导入／替换已有参考图不进行图像生成，但提交时会调用视觉模型（VLM）标注，可能计费。仅上传并暂存不计模型费用。";
    updateUploadControls();
  }
  function addFiles(files) {
    if (U.busy || uploads.active) return;
    const category = $("upload-category").value;
    const limit = category === "person" || uploads.replacement ? 1 : maxBatch();
    if (files.length + uploads.rows.length > limit) { U.notify(`此操作最多选择 ${limit} 张图片；未追加文件。`, true); $("upload-files").value = ""; return; }
    for (const file of files) {
      if (!file.size || file.size > 64 * 1024 * 1024 || file.name.length > 512 || !["", "image/jpeg", "image/png", "image/webp"].includes(file.type) || (file.type === "" && !/\.(?:jpe?g|png|webp)$/i.test(file.name))) { U.notify(`「${file.name}」不符合图片格式或 65536 KB 限制；本次未追加文件。`, true); $("upload-files").value = ""; return; }
    }
    for (const file of files) uploads.rows.push({ key: U.requestId(), file, preview: URL.createObjectURL(file), name: uploads.replacement ? uploads.replacement.name : file.name.replace(/\.[^.]+$/, "").slice(0, 200), stageRequestId: U.requestId(), submitRequestId: U.requestId(), stage_id: "", expires_at: "", status: "pending", task_id: "", error: "" });
    $("upload-files").value = "";
    renderUploads();
  }
  function renderUploads() {
    const container = $("upload-list");
    container.replaceChildren();
    const labels = { pending: "尚未上传", uploading: "正在暂存", staged: "已暂存，未提交", submitting: "正在提交", submitted: "已受理任务", unknown: "结果未知，请查回执", failed: "请求失败，不会自动重试" };
    for (const row of uploads.rows) {
      const node = element("div", { class: "file-row" });
      node.append(element("img", { src: row.preview, alt: `${row.file.name} 的本地预览` }));
      const field = element("div", { class: "field" });
      const id = `file-name-${row.key}`;
      field.append(element("label", { for: id }, "提交名称"));
      const name = element("input", { id, maxlength: 200, "data-file-name": row.key });
      name.value = row.name;
      name.disabled = uploads.active || ["submitted", "submitting", "unknown", "failed"].includes(row.status);
      name.addEventListener("input", () => { row.name = name.value; updateUploadControls(); });
      field.append(name, element("p", { class: "help" }, `原文件：${row.file.name} · ${(row.file.size / 1024).toFixed(2)} KB`), element("p", { class: "help" }, `${labels[row.status]}${row.expires_at ? ` · 暂存有效至 ${U.formatTime(row.expires_at)}` : ""}${row.task_id ? ` · 任务号：${row.task_id}` : ""}`));
      if (row.error) field.append(element("p", { class: "task-error" }, row.error));
      const remove = button("移除", () => {
        if (uploads.active || row.status === "unknown") return;
        URL.revokeObjectURL(row.preview);
        uploads.rows = uploads.rows.filter((entry) => entry !== row);
        renderUploads();
      }, { "data-file-remove": row.key, disabled: uploads.active || U.busy || row.status === "unknown" });
      node.append(field, remove);
      container.append(node);
    }
    const staged = uploads.rows.filter((row) => row.status === "staged").length;
    const submitted = uploads.rows.filter((row) => row.status === "submitted").length;
    $("upload-progress").textContent = uploads.rows.length ? `共 ${uploads.rows.length} 张 · ${staged} 张已暂存 · ${submitted} 张任务已受理。未知结果必须先查回执。` : "尚未选择图片。";
    updateUploadControls();
  }
  function updateUploadControls() {
    const pending = uploads.rows.filter((row) => row.status === "pending");
    const ready = uploads.rows.filter((row) => ["pending", "staged"].includes(row.status));
    U.unavailable($("stage-files"), !pending.length || !settings || uploads.rows.some((row) => !row.name.trim()));
    U.unavailable($("submit-files"), !ready.length || !settings || uploads.rows.some((row) => !row.name.trim() || ["unknown", "failed"].includes(row.status)));
    const locked = uploads.active || U.busy;
    $("upload-files").disabled = locked;
    $("upload-category").disabled = locked || !!uploads.replacement;
    $("upload-operation").disabled = locked || !!uploads.replacement;
    $("upload-close").disabled = locked;
    document.querySelectorAll("[data-file-name]").forEach((node) => {
      const row = uploads.rows.find((entry) => entry.key === node.dataset.fileName);
      node.disabled = locked || !row || !["pending", "staged"].includes(row.status);
    });
    document.querySelectorAll("[data-file-remove]").forEach((node) => {
      const row = uploads.rows.find((entry) => entry.key === node.dataset.fileRemove);
      node.disabled = locked || row?.status === "unknown";
    });
    $("upload-stop").hidden = !uploads.active;
  }
  function validateUploadPlan() {
    U.requireResolved();
    const ready = uploads.rows.filter((row) => ["pending", "staged"].includes(row.status));
    if (!ready.length) throw new Error("请先选择尚未提交的图片。");
    const max = $("upload-category").value === "person" || uploads.replacement ? 1 : maxBatch();
    if (uploads.rows.length > max) throw new Error(`此操作最多 ${max} 张，请移除多余图片。`);
    if (ready.some((row) => !row.name.trim() || row.name.trim().length > 200)) throw new Error("每张图片都必须有 1～200 字符的名称。");
    for (const row of ready) if (row.stage_id && (!Number.isFinite(U.expiryMillis(row.expires_at)) || U.expiryMillis(row.expires_at) <= Date.now())) throw new Error("有暂存图片已过期或有效期未知，请移除对应行并重新选择；不会自动重新上传。");
    return ready;
  }
  async function stagePending(rows) {
    for (const row of rows) {
      if (uploads.stop || document.visibilityState === "hidden") return false;
      if (row.stage_id) continue;
      if (row.status !== "pending") throw new Error("上传状态尚未确定，请先查回执。");
      row.status = "uploading";
      renderUploads();
      uploads.controller = new AbortController();
      try {
        const result = await U.upload(row.file, row.stageRequestId, { signal: uploads.controller.signal, onProgress: (percent) => { $("upload-progress").textContent = `正在暂存「${row.name}」：${Math.round(percent)}%（这不是模型进度）`; } });
        if (typeof result.stage_id !== "string" || !result.stage_id || !Number.isFinite(U.expiryMillis(result.expires_at))) throw new Error("暂存响应缺少有效 stage_id 或有效期，请查询原请求回执。");
        row.stage_id = result.stage_id;
        row.expires_at = result.expires_at;
        row.status = "staged";
      } catch (error) {
        row.status = error.definite ? "failed" : "unknown";
        row.error = U.message(error);
        throw error;
      } finally { uploads.controller = null; renderUploads(); }
    }
    return !uploads.stop;
  }
  async function runUpload(submit) {
    await U.runExclusive(async () => {
      await loadSettings(true);
      const rows = validateUploadPlan();
      uploads.active = true;
      uploads.stop = false;
      updateUploadControls();
      try {
        const staged = await stagePending(rows);
        if (!staged || !submit) { U.notify(staged ? "图片已逐张暂存，尚未调用模型。" : "已停止后续操作；已暂存的图片保留，未自动提交模型请求。"); return; }
        const operation = $("upload-operation").value;
        const category = $("upload-category").value;
        const batchId = rows.length > 1 ? U.requestId() : "";
        const signedItems = rows.map((row) => {
          const params = { request_id: row.submitRequestId, operation, category, name: row.name.trim(), stage_id: row.stage_id };
          if (batchId) params.batch_id = batchId;
          if (uploads.replacement) { params.asset_id = uploads.replacement.id; params.expected_version = versionOf(uploads.replacement); }
          if (category === "person") params.person_version = personVersion();
          return params;
        });
        let token;
        if (COSTLY.has(operation) || category === "person") {
          token = await confirmSubmits(signedItems, category === "person" && !COSTLY.has(operation) ? "人物导入／替换将覆盖当前人物参考，待审的新人物也会替换旧人物。因此即使不产生图像生成费用也必须确认；视觉标注仍可能产生 VLM 费用。" : "将调用视觉模型和图像生成模型，可能产生费用。整批只确认一次，之后按已签名的名称、暂存图片与请求号逐项串行提交。人物参考覆盖也包含在确认中。");
          if (!token) { U.notify("已取消提交；图片仍只是暂存，没有触发生成模型。"); return; }
        } else {
          const approved = await U.ask({ title: "提交图片处理", description: "提交即调用视觉模型（VLM）标注，可能计费；这不是只保存暂存图片。", summary: `${rows.length} 张${U.categories[category]}参考图：${rows.map((row) => row.name).join("、")}`, approve: "提交并调用 VLM" });
          if (!approved) return;
        }
        for (let index = 0; index < rows.length; index += 1) {
          if (uploads.stop || document.visibilityState === "hidden") { U.notify("已停止后续提交；已受理的任务仍会继续。", true); break; }
          const row = rows[index];
          if (U.expiryMillis(row.expires_at) <= Date.now()) throw new Error("暂存图片已过期，未提交后续请求。请移除对应行后重新选择。");
          row.status = "submitting";
          renderUploads();
          try {
            const signed = signedItems[index];
            const result = await U.action("submit", withoutRequestId(signed), signed.request_id, token);
            row.status = "submitted";
            row.task_id = result.task_id || "";
            U.notify(`已受理 ${index + 1}/${rows.length} 项，请在任务列表中查看处理结果。`);
          } catch (error) { row.status = error.definite ? "failed" : "unknown"; row.error = U.message(error); throw error; }
          finally { renderUploads(); }
        }
        await refreshGallery();
      } finally { uploads.active = false; updateUploadControls(); renderUploads(); }
    }, submit ? "逐张暂存，确认后串行提交…" : "正在逐张暂存，不会调用模型…");
  }
  function stopUploads() {
    uploads.stop = true;
    uploads.controller?.abort();
    $("upload-progress").textContent = "正在停止后续操作；当前提交可能已被受理，请查看请求回执。";
  }
  async function openPerson() {
    if (U.busy) return;
    try {
      await loadSettings(true);
      $("person-version-note").textContent = settings.person.asset_id ? `当前已有人物参考：${settings.person.asset_id} · 更新：${U.formatTime(settings.person.version)}。不允许直接生成覆盖；请使用上传替换，或先在图库中确认删除现有人物。` : "当前没有人物参考；生成绑定「无人物」状态，按宿主人格处理。";
      U.unavailable($("generate-person"), !!settings.person.asset_id);
      $("person-dialog").showModal();
    } catch (error) { U.notify(U.message(error), true); }
  }
  async function generatePerson() {
    await U.runExclusive(async () => {
      U.requireResolved();
      const name = $("person-name").value.trim();
      const hint = $("appearance-hint").value.trim();
      if (!name) throw new Error("请填写人物名称；补充外貌提示可以留空。");
      if (settings.person.asset_id) throw new Error("已有的人物参考不能通过此入口直接覆盖，请使用上传替换或先确认删除。");
      const params = { operation: "generate_person", category: "person", name, appearance_hint: hint, person_version: personVersion() };
      const id = U.requestId();
      const token = await U.confirmAction("submit", params, id, { title: "二次确认：生成人物参考板", description: "按 MaiBot 宿主人格调用图像生成模型，可能产生费用。确认凭证绑定当前「无人物参考」状态和本次补充外貌提示，不会直接覆盖已有人物。", approve: "确认费用并生成人物参考板" });
      if (!token) return;
      const result = await U.action("submit", params, id, token);
      $("person-dialog").close();
      U.notify(`人物生成任务已受理${result.task_id ? `，任务号：${result.task_id}` : ""}。`);
    }, "正在准备人物生成确认…");
  }
  function recoveredReceipt(event) {
    const receipt = event.detail;
    if (!receipt.response || !["accepted", "completed", "failed"].includes(receipt.state)) return;
    for (const row of uploads.rows) {
      if (receipt.request_id === row.stageRequestId && row.status === "unknown") {
        if (receipt.response.success === false || !receipt.response.stage_id) { row.status = "failed"; row.error = String(receipt.response.error || "暂存回执未包含可用图片，请移除此行后重新选择。"); }
        else if (!Number.isFinite(U.expiryMillis(receipt.response.expires_at))) { row.status = "unknown"; row.error = "暂存回执缺少有效期，不能继续提交。"; }
        else { row.stage_id = receipt.response.stage_id; row.expires_at = receipt.response.expires_at; row.status = "staged"; row.error = ""; }
      } else if (receipt.request_id === row.submitRequestId && row.status === "unknown") {
        row.status = receipt.response.success === false ? "failed" : "submitted";
        row.task_id = receipt.task_id || receipt.response.task_id || "";
        row.error = row.status === "failed" ? String(receipt.response.error || "请求已失败") : "";
      }
    }
    renderUploads();
  }
  function bind() {
    $("result-close").addEventListener("click", () => $("result-dialog").close());
    $("nav-tasks").addEventListener("click", () => U.navigate("tasks", { gallery_filters: filters }));
    $("apply-filters").addEventListener("click", applyFilters);
    $("search-filter").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); applyFilters(); } });
    $("refresh-gallery").addEventListener("click", () => { if (!U.busy) refreshGallery(); });
    $("previous-page").addEventListener("click", () => { if (U.busy || listLoading) return; filters.offset = Math.max(0, filters.offset - filters.limit); refreshGallery(); });
    $("next-page").addEventListener("click", () => { if (U.busy || listLoading) return; filters.offset += filters.limit; refreshGallery(); });
    $("clear-selection").addEventListener("click", () => { selected.clear(); document.querySelectorAll("[data-asset-select]").forEach((node) => { node.checked = false; node.closest(".asset-card").classList.remove("selected"); }); updateSelection(); });
    $("select-page").addEventListener("change", () => {
      const selectAll = $("select-page").checked;
      const pickable = items.filter((entry) => entry.status !== "deleted");
      if (selectAll && pickable.length > maxBatch()) { $("select-page").checked = false; U.notify(`本页超过 ${maxBatch()} 张，请手动选择。`, true); return; }
      selected.clear();
      if (selectAll) for (const entry of pickable) selected.set(entry.id, U.clone(entry));
      document.querySelectorAll("[data-asset-select]").forEach((node) => { node.checked = selected.has(node.dataset.assetSelect); node.closest(".asset-card").classList.toggle("selected", node.checked); });
      updateSelection();
    });
    for (const operation of ["enable", "disable", "delete"]) $(`${operation}-selected`).addEventListener("click", () => changeStatus(operation));
    $("retag-selected").addEventListener("click", () => submitAssetBatch("retag"));
    $("regenerate-selected").addEventListener("click", () => submitAssetBatch("regenerate"));
    $("asset-close").addEventListener("click", closeAsset);
    $("asset-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closeAsset(); });
    $("preview-source").addEventListener("click", () => preview("source"));
    $("preview-reference").addEventListener("click", () => preview("reference"));
    $("manual-json").addEventListener("input", () => { if (!asset) return; try { manualDraft = U.clone(validateManual(JSON.parse($("manual-json").value), asset.category)); updateTagView(false); renderTagFields(); } catch (error) { tagError(error); } });
    $("clear-overrides").addEventListener("click", () => { if (!asset) return; manualDraft = {}; updateTagView(true); renderTagFields(); });
    $("save-asset").addEventListener("click", saveAsset);
    $("asset-retag").addEventListener("click", () => asset && submitAssetBatch("retag", [asset]));
    $("asset-regenerate").addEventListener("click", () => asset && submitAssetBatch("regenerate", [asset]));
    $("replace-asset").addEventListener("click", () => asset && openUpload(asset));
    $("open-upload").addEventListener("click", () => openUpload());
    $("upload-close").addEventListener("click", closeUpload);
    $("upload-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closeUpload(); });
    $("upload-files").addEventListener("change", () => addFiles(Array.from($("upload-files").files)));
    $("upload-category").addEventListener("change", updateUploadRisk);
    $("upload-operation").addEventListener("change", updateUploadRisk);
    $("stage-files").addEventListener("click", () => runUpload(false));
    $("submit-files").addEventListener("click", () => runUpload(true));
    $("upload-stop").addEventListener("click", stopUploads);
    $("open-person").addEventListener("click", openPerson);
    $("person-close").addEventListener("click", () => { if (!U.busy) $("person-dialog").close(); });
    $("person-dialog").addEventListener("cancel", (event) => { if (U.busy) event.preventDefault(); });
    $("generate-person").addEventListener("click", generatePerson);
    document.addEventListener("photo:receipt", recoveredReceipt);
    document.addEventListener("photo:lock", () => {
      updateSelection(); updateDetailControls(); updateUploadControls();
      document.querySelectorAll("[data-asset-select]").forEach((node) => { node.disabled = U.busy || items.find((item) => item.id === node.dataset.assetSelect)?.status === "deleted"; });
      if (asset && $("asset-dialog").open) renderTagFields();
      $("asset-close").disabled = U.busy;
      $("person-close").disabled = U.busy;
    });
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden" && uploads.active) uploads.stop = true; });
    window.addEventListener("pagehide", revokeRows);
  }
  let bound = false;
  let configured = false;
  async function start(navigation) {
    if (!bound) {
      bind();
      bound = true;
    }
    if (!configured) {
      await loadSettings();
      configured = true;
      filters.limit = Math.min(24, Math.max(1, settings.page_size));
    }
    const carried = navigation.gallery_filters;
    if (U.plainObject(carried)) {
      if (["", "person", "outfit", "scene"].includes(carried.category)) filters.category = carried.category;
      if (["", "active", "disabled", "needs_review"].includes(carried.status)) filters.status = carried.status;
      if (typeof carried.search === "string") filters.search = carried.search.slice(0, 4000);
      if (Number.isInteger(carried.offset) && carried.offset >= 0) filters.offset = carried.offset;
      if (Number.isInteger(carried.limit) && carried.limit > 0 && carried.limit <= 24) filters.limit = carried.limit;
    }
    filterControls(filters);
    $("busy-label").textContent = "";
    updateSelection(); updateDetailControls(); updateUploadControls();
    await refreshGallery();
    if (typeof navigation.asset_id === "string") await openAsset(navigation.asset_id);
  }
  U.registerView("gallery", start);
})();
