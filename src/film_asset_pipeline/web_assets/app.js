const state = {
  workspace: { summary: {}, assets: [], profiles: {} },
  stage: "all",
  search: "",
  sort: localStorage.getItem("filmAssetSort") || "generated_desc",
  viewMode: localStorage.getItem("filmAssetViewMode") || "grid",
  thumbnailSize: localStorage.getItem("filmAssetThumbnailSize") || "medium",
  selected: new Set(),
  activeAsset: null,
  jobTimer: null,
};

const ACTIVE_JOB_KEY = "filmAssetActiveJob";
const FRONTEND_VERSION = document.querySelector('meta[name="app-version"]')?.content || "unknown";

const stageLabels = {
  all: "全部资产",
  source_frames: "来源静帧",
  awaiting_2d: "待处理静帧",
  image_review: "2D 待审核",
  ready_for_3d: "待生成 3D",
  model_generation: "3D 生成中",
  model_review: "3D 待审核",
  approved: "审核通过",
  rejected: "已驳回",
};

const statusLabels = {
  source_frame: "已有2D",
  awaiting_2d: "待处理",
  image_review: "2D待审",
  ready_for_3d: "待生成",
  model_generation: "生成中",
  model_review: "3D待审",
  approved: "已通过",
  rejected: "已驳回",
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `请求失败：${response.status}`);
  return payload;
}

async function loadWorkspace() {
  state.workspace = await api("/api/workspace");
  if (state.workspace.frontend_version && state.workspace.frontend_version !== FRONTEND_VERSION) {
    console.warn(`前后端版本暂时不同：${FRONTEND_VERSION} / ${state.workspace.frontend_version}`);
  }
  const valid = new Set(state.workspace.assets.map((asset) => asset.asset_id));
  state.selected.forEach((id) => { if (!valid.has(id)) state.selected.delete(id); });
  render();
}

function render() {
  renderMetrics();
  renderConfigurationStatus();
  renderAssets();
  renderSelection();
}

function renderConfigurationStatus() {
  const configuration = state.workspace.configuration || {};
  const arkReady = Boolean(configuration.ark?.configured);
  const hunyuanReady = Boolean(configuration.hunyuan?.configured);
  const text = arkReady && hunyuanReady
    ? "即梦 2D / 混元 3D 已配置"
    : arkReady
    ? "即梦已配置 · 混元待配置"
    : "请先配置模型密钥";
  $("#service-status").textContent = text;
  $(".service-dot").classList.toggle("is-warning", !(arkReady && hunyuanReady));
}

function renderMetrics() {
  const s = state.workspace.summary;
  const generated = (s.model_review || 0) + (s.approved || 0);
  const pending = (s.awaiting_2d || 0) + (s.image_review || 0) + (s.ready_for_3d || 0) + (s.model_generation || 0);
  $("#metric-total").textContent = s.total || 0;
  $("#metric-pending").textContent = pending;
  $("#metric-models").textContent = generated;
  $("#metric-approved").textContent = s.approved || 0;
  $("#nav-total").textContent = s.total || 0;
  $("#nav-source-frames").textContent = s.source_frames || 0;
  $("#nav-awaiting-2d").textContent = s.awaiting_2d || 0;
  $("#nav-image-review").textContent = s.image_review || 0;
  $("#nav-ready-3d").textContent = s.ready_for_3d || 0;
  $("#nav-model-generation").textContent = s.model_generation || 0;
  $("#nav-model-review").textContent = s.model_review || 0;
  $("#nav-approved").textContent = s.approved || 0;
  $("#nav-rejected").textContent = s.rejected || 0;
}

function visibleAssets() {
  const needle = state.search.trim().toLowerCase();
  const assets = state.workspace.assets.filter((asset) => {
    const stageMatch = state.stage === "all"
      ? asset.stage !== "source_frame"
      : state.stage === "source_frames"
      ? ["source_frame", "awaiting_2d"].includes(asset.stage)
      : asset.stage === state.stage;
    const haystack = [asset.title, asset.asset_id, ...(asset.tags || [])].join(" ").toLowerCase();
    return stageMatch && (!needle || haystack.includes(needle));
  });
  const direction = state.sort.endsWith("_desc") ? -1 : 1;
  const field = state.sort.startsWith("title_") ? "title" : "generated_at";
  return assets.sort((left, right) => {
    const a = String(left[field] || "");
    const b = String(right[field] || "");
    return a.localeCompare(b, "zh-CN", { numeric: true, sensitivity: "base" }) * direction;
  });
}

function renderAssets() {
  const assets = visibleAssets();
  $("#view-title").textContent = stageLabels[state.stage] || "全部资产";
  $("#view-caption").textContent = `${assets.length} 项资产 · 从静帧、设计图到 GLB 的可追溯映射`;
  const container = $("#asset-grid");
  container.className = `asset-grid view-${state.viewMode} thumb-${state.thumbnailSize}`;
  container.innerHTML = assets.map((asset) => state.viewMode === "list" ? assetListRow(asset) : assetCard(asset)).join("");
  $("#empty-state").hidden = assets.length > 0;
  $$(".asset-select").forEach((input) => input.addEventListener("change", selectAsset));
  $$(".asset-media").forEach((button) => button.addEventListener("click", () => openAsset(button.dataset.assetId)));
  $$('[data-edit-field]').forEach((cell) => cell.addEventListener("dblclick", beginInlineEdit));
  renderViewControls();
}

function assetCard(asset) {
  const selected = state.selected.has(asset.asset_id);
  const sourceFrame = ["source_frame", "awaiting_2d"].includes(asset.stage);
  const awaiting2D = asset.stage === "awaiting_2d";
  const modelDone = asset.model_status === "complete";
  const tags = (asset.tags || []).length ? asset.tags : ["待补充标签"];
  const spec = modelDone
    ? `${asset.enable_pbr ? "PBR" : "普通材质"} · ${formatFaces(asset.face_count)}`
    : asset.stage === "source_frame"
    ? "已有2D结果 · 可重新优化"
    : asset.image_error_code
    ? `上次失败 · ${friendlyErrorCode(asset.image_error_code)}`
    : "尚未生成模型";
  return `
    <article class="asset-card ${selected ? "is-selected" : ""}">
      <input class="asset-select" type="checkbox" aria-label="选择${escapeHtml(asset.title)}" data-asset-id="${asset.asset_id}" ${selected ? "checked" : ""}>
      <div class="asset-media" role="button" data-asset-id="${asset.asset_id}" aria-label="查看${escapeHtml(asset.title)}">
        <img src="${asset.preview_url || asset.image_url}" alt="${escapeHtml(asset.title)}资产图" loading="lazy">
      </div>
      <div class="asset-body">
        <div class="asset-title-row">
          <div><h3>${escapeHtml(asset.title)}</h3><span class="asset-id">${asset.asset_id}</span></div>
          <span class="status status-${asset.stage}">${statusLabels[asset.stage] || "状态未知"}</span>
        </div>
        <div class="pipeline-line" aria-label="处理进度">
          <span class="done">静帧</span><i></i><span class="${awaiting2D ? "" : "done"}">2D</span><i></i><span class="${modelDone ? "done" : ""}">3D</span>
        </div>
        <div class="tag-list">${tags.slice(0, 4).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}</div>
        <div class="card-foot"><span>${spec}</span>${asset.model_url ? `<a href="${asset.model_url}">下载 GLB</a>` : "<span>—</span>"}</div>
      </div>
    </article>`;
}

function assetListRow(asset) {
  const selected = state.selected.has(asset.asset_id);
  const tags = (asset.tags || []).join(", ");
  const preview = asset.preview_url || asset.image_url;
  const details = asset.model_status === "complete"
    ? `${asset.enable_pbr ? "PBR" : "普通材质"} · ${formatFaces(asset.face_count)}`
    : asset.image_error_code
    ? `上次失败 · ${friendlyErrorCode(asset.image_error_code)}`
    : "尚未生成模型";
  return `<article class="asset-list-row ${selected ? "is-selected" : ""}">
    <input class="asset-select" type="checkbox" aria-label="选择${escapeHtml(asset.title)}" data-asset-id="${asset.asset_id}" ${selected ? "checked" : ""}>
    <button class="asset-media list-thumbnail" type="button" data-asset-id="${asset.asset_id}" aria-label="查看${escapeHtml(asset.title)}">
      <img src="${preview}" alt="${escapeHtml(asset.title)}资产图" loading="lazy">
    </button>
    <div class="list-primary">
      <strong data-edit-field="title" data-asset-id="${asset.asset_id}" title="双击编辑名称">${escapeHtml(asset.title)}</strong>
      <span>${asset.asset_id}</span>
    </div>
    <div class="list-field" data-edit-field="tags" data-asset-id="${asset.asset_id}" title="双击编辑标签">
      <span>标签</span><strong>${escapeHtml(tags || "待补充")}</strong>
    </div>
    <div class="list-field list-notes" data-edit-field="notes" data-asset-id="${asset.asset_id}" title="双击编辑备注">
      <span>备注</span><strong>${escapeHtml(asset.notes || "双击填写")}</strong>
    </div>
    <div class="list-field"><span>生成时间</span><strong>${formatDate(asset.generated_at)}</strong></div>
    <div class="list-status"><span class="status status-${asset.stage}">${statusLabels[asset.stage] || "状态未知"}</span><small>${escapeHtml(details)}</small></div>
  </article>`;
}

function renderViewControls() {
  $("#sort-order").value = state.sort;
  $("#view-grid").classList.toggle("is-active", state.viewMode === "grid");
  $("#view-list").classList.toggle("is-active", state.viewMode === "list");
  $("#view-grid").setAttribute("aria-pressed", String(state.viewMode === "grid"));
  $("#view-list").setAttribute("aria-pressed", String(state.viewMode === "list"));
  $("#thumbnail-sizes").hidden = state.viewMode !== "grid";
  $$('[data-thumbnail-size]').forEach((button) => button.classList.toggle("is-active", button.dataset.thumbnailSize === state.thumbnailSize));
}

function renderSelection() {
  const selectedAssets = state.workspace.assets.filter((asset) => state.selected.has(asset.asset_id));
  const processableAssets = selectedAssets.filter((asset) =>
    ["source_frame", "awaiting_2d", "image_review", "rejected"].includes(asset.stage) && asset.model_status !== "complete"
  );
  const processableSources = new Set(processableAssets.map((asset) => asset.source_file_name || asset.asset_id));
  const imageReviewCount = selectedAssets.filter((asset) => asset.stage === "image_review").length;
  const modelReviewCount = selectedAssets.filter((asset) => asset.stage === "model_review").length;
  const modelCount = selectedAssets.filter((asset) => asset.stage === "ready_for_3d").length;
  const rejectedCount = selectedAssets.filter((asset) => asset.stage === "rejected").length;
  $("#selection-count").textContent = selectedAssets.length ? `已选择 ${selectedAssets.length} 项` : "未选择资产";
  const visible = visibleAssets();
  const allVisibleSelected = visible.length > 0 && visible.every((asset) => state.selected.has(asset.asset_id));
  $("#select-visible").textContent = allVisibleSelected ? "取消全选" : "全选当前视图";
  $("#select-visible").disabled = visible.length === 0;

  $$(".stage-action").forEach((element) => { element.hidden = true; });
  const hints = {
    all: "请进入具体环节执行批量操作",
    source_frames: "选择来源静帧，可重新生成 1 张主视图或 3 张多视图",
    awaiting_2d: "选择静帧，并设置每个来源生成 1 张或 3 张",
    image_review: "审核设计图，只能批量通过或批量驳回",
    ready_for_3d: "选择已通过2D审核的设计图生成3D",
    model_generation: "3D任务由服务端执行，请查看右下角进度",
    model_review: "审核3D结果，只能批量通过或批量驳回",
    approved: "已通过资产可查看详情或下载GLB",
    rejected: "选择驳回项，可重新提交到相应审核环节",
  };
  $("#stage-action-hint").textContent = hints[state.stage] || "";

  if (["source_frames", "awaiting_2d"].includes(state.stage)) {
    $("#image-output-count").hidden = false;
    $("#process-2d").hidden = false;
    $("#process-2d").disabled = processableSources.size === 0;
    $("#process-2d").textContent = state.stage === "source_frames"
      ? `重新优化选中来源${processableSources.size ? `（${processableSources.size}）` : ""}`
      : `优化选中静帧${processableSources.size ? `（${processableSources.size}）` : ""}`;
  } else if (["image_review", "model_review"].includes(state.stage)) {
    const count = state.stage === "image_review" ? imageReviewCount : modelReviewCount;
    $("#batch-approve").hidden = false;
    $("#batch-reject").hidden = false;
    $("#batch-approve").disabled = count === 0;
    $("#batch-reject").disabled = count === 0;
    $("#batch-approve").textContent = `批量通过${count ? `（${count}）` : ""}`;
    $("#batch-reject").textContent = `批量驳回${count ? `（${count}）` : ""}`;
  } else if (state.stage === "ready_for_3d") {
    $("#generation-profile").hidden = false;
    $("#generate-selected").hidden = false;
    $("#generate-selected").disabled = modelCount === 0;
    $("#generate-selected").textContent = `生成选中资产${modelCount ? `（${modelCount}）` : ""}`;
  } else if (state.stage === "rejected") {
    $("#resubmit-selected").hidden = false;
    $("#resubmit-selected").disabled = rejectedCount === 0;
    $("#resubmit-selected").textContent = `重新提交审核${rejectedCount ? `（${rejectedCount}）` : ""}`;
  }
}

function selectAsset(event) {
  const id = event.target.dataset.assetId;
  event.target.checked ? state.selected.add(id) : state.selected.delete(id);
  renderAssets();
  renderSelection();
}

function toggleVisibleSelection() {
  const assets = visibleAssets();
  const shouldSelect = !assets.length || !assets.every((asset) => state.selected.has(asset.asset_id));
  assets.forEach((asset) => shouldSelect ? state.selected.add(asset.asset_id) : state.selected.delete(asset.asset_id));
  renderAssets();
  renderSelection();
}

function beginInlineEdit(event) {
  event.stopPropagation();
  const cell = event.currentTarget;
  if (cell.querySelector(".inline-editor")) return;
  const asset = state.workspace.assets.find((item) => item.asset_id === cell.dataset.assetId);
  if (!asset) return;
  const field = cell.dataset.editField;
  const original = cell.innerHTML;
  const value = field === "tags" ? (asset.tags || []).join(", ") : String(asset[field] || "");
  const input = document.createElement("input");
  input.className = "inline-editor";
  input.value = value;
  input.setAttribute("aria-label", `编辑${field === "title" ? "名称" : field === "tags" ? "标签" : "备注"}`);
  cell.replaceChildren(input);
  input.focus();
  input.select();
  let finished = false;
  const finish = async (save) => {
    if (finished) return;
    finished = true;
    if (!save || input.value === value) {
      cell.innerHTML = original;
      return;
    }
    const payload = field === "tags"
      ? { tags: input.value.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean) }
      : { [field]: input.value.trim() };
    input.disabled = true;
    try {
      await api(`/api/assets/${asset.asset_id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      });
      await loadWorkspace();
      toast("信息已保存");
    } catch (error) {
      cell.innerHTML = original;
      toast(error.message, true);
    }
  };
  input.addEventListener("keydown", (keyboardEvent) => {
    if (keyboardEvent.key === "Enter") finish(true);
    if (keyboardEvent.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
}

function openAsset(assetId) {
  const asset = state.workspace.assets.find((item) => item.asset_id === assetId);
  if (!asset) return;
  const sourceFrame = ["source_frame", "awaiting_2d"].includes(asset.stage);
  const awaiting2D = asset.stage === "awaiting_2d";
  state.activeAsset = asset;
  $("#dialog-title").textContent = asset.title;
  const preview = sourceFrame
    ? `<div class="compare-frame"><div class="pending-visual">${awaiting2D ? "等待批量优化 2D" : "已有2D结果，可重新优化"}</div><span>${awaiting2D ? "2D 尚未生成" : "从来源静帧重新生成资产视图"}</span></div>`
    : asset.preview_url
    ? `<div class="compare-frame"><img src="${asset.preview_url}" alt="3D模型预览"><span>3D 模型预览</span></div>`
    : `<div class="compare-frame"><img src="${asset.image_url}" alt="等待生成3D"><span>3D 尚未生成</span></div>`;
  $("#dialog-content").innerHTML = `
    <div class="dialog-grid">
      <section class="visual-review">
        <div class="compare-grid">
          <div class="compare-frame"><img src="${asset.image_url}" alt="${sourceFrame ? "原始静帧" : "优化后的2D资产图"}"><span>${sourceFrame ? "原始静帧" : "2D 资产设计图"}</span></div>
          ${preview}
        </div>
        ${sourceFrame ? "" : reviewBlock(asset, "image")}
        ${asset.model_status === "complete" ? reviewBlock(asset, "model") : ""}
      </section>
      <aside class="metadata-panel">
        <div class="field"><label for="asset-title">资产名称</label><input id="asset-title" value="${escapeHtml(asset.title)}"></div>
        <div class="field"><label for="asset-tags">检索标签（使用逗号分隔）</label><input id="asset-tags" value="${escapeHtml((asset.tags || []).join(", "))}" placeholder="年代, 材质, 类别, 影片"></div>
        <div class="field"><label for="asset-notes">审核与制作备注</label><textarea id="asset-notes" placeholder="记录修图、拓扑、材质或入库要求">${escapeHtml(asset.notes || "")}</textarea></div>
        <div class="facts">
          <div class="fact"><span>资产 ID</span><strong>${asset.asset_id}</strong></div>
          <div class="fact"><span>来源静帧</span><strong>${escapeHtml(asset.source_file_name || asset.source_name || "—")}</strong></div>
          <div class="fact"><span>自动目标资产</span><strong>${escapeHtml(asset.target_name || "自动识别主要物品")}</strong></div>
          <div class="fact"><span>结果序号</span><strong>${asset.result_count ? `${asset.result_index}/${asset.result_count}` : "—"}</strong></div>
          <div class="fact"><span>目标视图</span><strong>${escapeHtml(viewLabel(asset.result_index))}</strong></div>
          <div class="fact"><span>规范文件名</span><strong>${escapeHtml(asset.logical_file_name || "待生成")}</strong></div>
          <div class="fact"><span>2D 模型</span><strong>${escapeHtml(asset.image_model || (awaiting2D ? "等待处理" : asset.stage === "source_frame" ? "已有结果，可重新优化" : "—"))}</strong></div>
          ${asset.image_error_code ? `<div class="fact fact-error"><span>上次优化</span><strong>${escapeHtml(friendlyErrorCode(asset.image_error_code))}</strong></div><div class="fact fact-error"><span>处理建议</span><strong>${escapeHtml(friendlyErrorMessage(asset))}</strong></div>` : ""}
          <div class="fact"><span>3D 状态</span><strong>${statusLabels[asset.stage] || "状态未知"}</strong></div>
          <div class="fact"><span>面数目标</span><strong>${asset.face_count ? formatFaces(asset.face_count) : "—"}</strong></div>
          <div class="fact"><span>积分消耗</span><strong>${asset.credits_consumed ?? "—"}</strong></div>
        </div>
        <div class="metadata-actions">
          <button id="save-metadata" class="button button-primary" type="button">保存元数据</button>
          ${asset.model_url ? `<a class="button" href="${asset.model_url}">下载 GLB</a>` : ""}
        </div>
      </aside>
    </div>`;
  $("#save-metadata").addEventListener("click", saveMetadata);
  $$("[data-review]").forEach((button) => button.addEventListener("click", review));
  $("#asset-dialog").showModal();
}

function reviewBlock(asset, stage) {
  const current = asset[`${stage}_review`];
  const label = stage === "image" ? "2D 图像审核" : "3D 模型审核";
  return `<div class="review-block">
    <div><strong>${label}</strong><span>当前状态：${reviewText(current)}</span></div>
    <div class="review-buttons">
      <button class="button" type="button" data-review="${stage}:approved">通过</button>
      <button class="button button-danger" type="button" data-review="${stage}:rejected">驳回</button>
    </div>
  </div>`;
}

async function review(event) {
  const [stage, decision] = event.currentTarget.dataset.review.split(":");
  try {
    await api(`/api/assets/${state.activeAsset.asset_id}/review`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stage, decision }),
    });
    await loadWorkspace();
    openAsset(state.activeAsset.asset_id);
    toast("审核状态已更新");
  } catch (error) { toast(error.message, true); }
}

async function batchReview(decision) {
  const reviewStage = state.stage === "image_review" ? "image" : state.stage === "model_review" ? "model" : null;
  if (!reviewStage) return;
  const expectedStage = reviewStage === "image" ? "image_review" : "model_review";
  const assetIds = state.workspace.assets
    .filter((asset) => state.selected.has(asset.asset_id) && asset.stage === expectedStage)
    .map((asset) => asset.asset_id);
  if (!assetIds.length) return;
  const action = decision === "approved" ? "通过" : "驳回";
  if (decision === "rejected" && !confirm(`将驳回 ${assetIds.length} 项资产，并移动到“已驳回”。确认继续？`)) return;
  try {
    const result = await api("/api/actions/review", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ asset_ids: assetIds, stage: reviewStage, decision }),
    });
    state.selected.clear();
    await loadWorkspace();
    showResultDialog(`批量${action}完成`, `已${action} ${result.updated_count} 项资产。`, false);
  } catch (error) { showResultDialog(`批量${action}失败`, error.message, true); }
}

async function resubmitSelected() {
  const assetIds = state.workspace.assets
    .filter((asset) => state.selected.has(asset.asset_id) && asset.stage === "rejected")
    .map((asset) => asset.asset_id);
  if (!assetIds.length) return;
  try {
    const result = await api("/api/actions/review", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ asset_ids: assetIds, stage: "auto", decision: "pending" }),
    });
    state.selected.clear();
    await loadWorkspace();
    showResultDialog("已重新提交审核", `${result.updated_count} 项资产已回到对应的2D或3D审核环节。`, false);
  } catch (error) { showResultDialog("重新提交失败", error.message, true); }
}

async function saveMetadata() {
  const tags = $("#asset-tags").value.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean);
  try {
    await api(`/api/assets/${state.activeAsset.asset_id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: $("#asset-title").value, tags, notes: $("#asset-notes").value }),
    });
    await loadWorkspace();
    $("#asset-dialog").close();
    toast("资产元数据已保存");
  } catch (error) { toast(error.message, true); }
}

async function uploadFrames(event) {
  const files = [...event.target.files];
  if (!files.length) return;
  const body = new FormData();
  files.forEach((file) => body.append("files", file));
  setUploading(true, files.length);
  setJobBar(true, "正在上传静帧", `正在保存 ${files.length} 张图片…`);
  try {
    const result = await api("/api/frames/upload", { method: "POST", body });
    await loadWorkspace();
    activateStage(result.count ? "awaiting_2d" : "source_frames");
    showUploadResult(result);
  } catch (error) {
    showResultDialog("上传失败", error.message, true);
  } finally {
    setJobBar(false);
    setUploading(false);
  }
  event.target.value = "";
}

function setUploading(uploading, count = 0) {
  $("#frame-upload").disabled = uploading;
  $("#upload-action").classList.toggle("is-disabled", uploading);
  $("#upload-text").textContent = uploading ? `正在上传 ${count} 张…` : "上传静帧";
}

function showUploadResult(result) {
  const saved = result.count || 0;
  const skipped = result.skipped_count || 0;
  const failed = result.failed_count || 0;
  const parts = [];
  if (saved) parts.push(`新增 ${saved} 张`);
  if (skipped) parts.push(`重复跳过 ${skipped} 张`);
  if (failed) parts.push(`失败 ${failed} 张`);
  const failureDetail = failed
    ? `\n${(result.failed || []).map((item) => `${item.name}：${item.reason}`).join("\n")}`
    : "";
  if (!saved && skipped && !failed) {
    showResultDialog("图片已存在", `所选图片已保存在“来源静帧”中。系统已跳过重复上传，你可以直接勾选原静帧重新优化。`, false);
    return;
  }
  const title = failed ? (saved ? "部分上传完成" : "上传失败") : "上传完成";
  const location = saved ? "。新增图片已显示在“待处理静帧”列表中" : "";
  showResultDialog(title, `${parts.join("，")}${location}${failureDetail}。`, failed > 0);
}

async function process2D() {
  const candidates = state.workspace.assets.filter((asset) =>
    state.selected.has(asset.asset_id)
    && ["source_frame", "awaiting_2d", "image_review", "rejected"].includes(asset.stage)
    && asset.model_status !== "complete"
  );
  const bySource = new Map();
  candidates.forEach((asset) => {
    const key = asset.source_file_name || asset.asset_id;
    if (!bySource.has(key)) bySource.set(key, asset.asset_id);
  });
  const assetIds = [...bySource.values()];
  if (!assetIds.length) {
    showResultDialog("请先选择资产", "请勾选待处理静帧或尚未进入3D的2D资产，再开始优化。", true);
    return;
  }
  const outputCount = Number($("#image-output-count").value) === 1 ? 1 : 3;
  const outputDescription = outputCount === 1 ? "1 张完整主视图" : "3 张不同视图";
  const estimatedImages = assetIds.length * outputCount;
  if (!confirm(`将调用即梦 API 处理 ${assetIds.length} 个来源静帧，每个来源生成${outputDescription}，预计最多返回 ${estimatedImages} 张图片，可能产生费用。确认继续？`)) return;
  try {
    const job = await api("/api/actions/process-2d", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ asset_ids: assetIds, output_count: outputCount, confirmation: "PROCESS_2D" }),
    });
    watchJob(job, `2D 批量优化 · 每项${outputCount}张`);
  } catch (error) { toast(error.message, true); }
}

async function generateSelected() {
  const assetIds = state.workspace.assets
    .filter((asset) => state.selected.has(asset.asset_id) && asset.stage !== "awaiting_2d" && asset.image_review === "approved")
    .map((asset) => asset.asset_id);
  const profile = $("#generation-profile").value;
  const info = state.workspace.profiles[profile];
  const estimate = (info?.estimated_credits || 0) * assetIds.length;
  if (!confirm(`将为 ${assetIds.length} 项资产生成3D，预计最多消耗约 ${estimate} 积分。确认继续？`)) return;
  try {
    const job = await api("/api/actions/generate-3d", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ asset_ids: assetIds, profile, confirmation: "GENERATE_3D" }),
    });
    watchJob(job, "3D 批量生成");
  } catch (error) { toast(error.message, true); }
}

function watchJob(job, title) {
  if (state.jobTimer) clearTimeout(state.jobTimer);
  localStorage.setItem(ACTIVE_JOB_KEY, JSON.stringify({ job_id: job.job_id, title }));
  setJobBar(true, title, job.message || "任务已进入本地队列");
  const poll = async () => {
    try {
      const current = await api(`/api/jobs/${job.job_id}`);
      setJobBar(true, title, current.message);
      renderJobProgress(current.progress);
      if (["complete", "failed"].includes(current.status)) {
        localStorage.removeItem(ACTIVE_JOB_KEY);
        setJobBar(false);
        await loadWorkspace();
        if (current.kind === "process-2d") {
          activateStage("image_review");
        }
        showResultDialog(
          current.status === "complete" ? `${title}完成` : `${title}失败`,
          current.message || (current.status === "complete" ? "任务已完成。" : "任务执行失败。"),
          current.status === "failed",
        );
        return;
      }
      state.jobTimer = setTimeout(poll, 3000);
    } catch (error) {
      localStorage.removeItem(ACTIVE_JOB_KEY);
      setJobBar(false);
      await loadWorkspace().catch(() => {});
      showResultDialog("任务状态已刷新", "本地服务曾重启，旧任务编号已失效；工作台已按实际输出重新加载。", false);
    }
  };
  poll();
}

function setJobBar(visible, title = "", message = "") {
  const element = $("#job-bar");
  element.hidden = !visible;
  element.style.display = visible ? "flex" : "none";
  if (visible) {
    $("#job-title").textContent = title;
    $("#job-message").textContent = message;
  }
  if (!visible) renderJobProgress();
}

function renderJobProgress(progress = {}) {
  const percent = Math.max(0, Math.min(100, progress.percent || 0));
  $("#job-progress-fill").style.width = `${percent}%`;
  $(".job-progress").setAttribute("aria-valuenow", String(percent));
  $("#job-stats").textContent = progress.total
    ? `${progress.completed || 0}/${progress.total} · 成功 ${progress.succeeded || 0} · 失败 ${progress.failed || 0}`
    : "正在初始化任务…";
}

function showResultDialog(title, message, isError = false) {
  $("#result-title").textContent = title;
  $("#result-message").textContent = message;
  $("#result-icon").textContent = isError ? "!" : "✓";
  $("#result-icon").classList.toggle("is-error", isError);
  const dialog = $("#result-dialog");
  if (dialog.open) dialog.close();
  dialog.showModal();
}

function toast(message, isError = false) {
  const element = $("#toast");
  element.textContent = message;
  element.style.borderColor = isError ? "var(--danger)" : "var(--line)";
  element.hidden = false;
  element.style.display = "block";
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { element.hidden = true; element.style.display = "none"; }, 3600);
}

function formatFaces(value) {
  if (!value) return "—";
  return value >= 10000 ? `${Math.round(value / 10000)}万面` : `${value}面`;
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

function reviewText(value) {
  return ({ pending: "待审核", approved: "已通过", rejected: "已驳回" })[value] || "待审核";
}

function viewLabel(index) {
  return ({1: "前侧四分之三视图", 2: "侧视图", 3: "后侧四分之三视图"})[index] || "待生成后确定";
}

function friendlyErrorCode(code) {
  return code === "SetLimitExceeded" ? "推理限额已触发" : code;
}

function friendlyErrorMessage(asset) {
  if (asset.image_error_code === "SetLimitExceeded") {
    return "请先在火山引擎模型开通页调整或关闭安心体验模式，再重新勾选优化";
  }
  return asset.image_error_message || "请检查接口配置后重试";
}

function applySettingsStatus(configuration) {
  const ark = configuration?.ark || {};
  const hunyuan = configuration?.hunyuan || {};
  $("#ark-config-state").textContent = ark.configured ? "已配置" : "未配置";
  $("#ark-config-state").classList.toggle("is-ready", Boolean(ark.configured));
  $("#ark-key-hint").textContent = ark.masked_key
    ? `当前密钥：${ark.masked_key}。输入新值可替换，留空保持不变。`
    : "用于 Seedream 5.0 Lite 图生图。";
  $("#hunyuan-config-state").textContent = hunyuan.configured ? "已配置" : "未配置";
  $("#hunyuan-config-state").classList.toggle("is-ready", Boolean(hunyuan.configured));
  $("#hunyuan-api-style").value = hunyuan.api_style || "tencentcloud_sdk";
  toggleCredentialFields();
}

async function openSettings() {
  let configuration = state.workspace.configuration;
  if (!configuration) configuration = await api("/api/settings/status");
  applySettingsStatus(configuration);
  const dialog = $("#settings-dialog");
  if (!dialog.open) dialog.showModal();
}

function toggleCredentialFields() {
  const sdk = $("#hunyuan-api-style").value === "tencentcloud_sdk";
  $("#sdk-credentials").hidden = !sdk;
  $("#openai-credentials").hidden = sdk;
}

function credentialPayload() {
  const optional = (selector) => $(selector).value.trim() || null;
  return {
    ark_api_key: optional("#ark-api-key"),
    hunyuan_api_style: $("#hunyuan-api-style").value,
    hunyuan_api_key: optional("#hunyuan-api-key"),
    tencentcloud_secret_id: optional("#tencent-secret-id"),
    tencentcloud_secret_key: optional("#tencent-secret-key"),
  };
}

async function persistSettings() {
  const configuration = await api("/api/settings/credentials", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(credentialPayload()),
  });
  state.workspace.configuration = configuration;
  ["#ark-api-key", "#hunyuan-api-key", "#tencent-secret-id", "#tencent-secret-key"]
    .forEach((selector) => { $(selector).value = ""; });
  applySettingsStatus(configuration);
  renderConfigurationStatus();
  return configuration;
}

async function saveSettings(event) {
  event.preventDefault();
  try {
    const configuration = await persistSettings();
    $("#settings-dialog").close();
    const ready = configuration.ark?.configured && configuration.hunyuan?.configured;
    showResultDialog(
      ready ? "模型配置已保存" : "配置已保存",
      ready
        ? "即梦 2D 与混元 3D 已配置，可以开始处理静帧。"
        : "已保存现有配置。尚未配置的服务可以稍后在“模型设置”中补充。",
      false,
    );
  } catch (error) {
    toast(error.message, true);
  }
}

async function testSettings() {
  try {
    await persistSettings();
    const result = await api("/api/settings/test", { method: "POST" });
    toast(result.valid ? result.message : `${result.message} ${result.errors.join("；")}`, !result.valid);
  } catch (error) {
    toast(error.message, true);
  }
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function activateStage(stage) {
  state.stage = stage;
  state.selected.clear();
  $$(".stage-link").forEach((item) => item.classList.toggle("is-active", item.dataset.stage === stage));
  render();
}

function setViewMode(mode) {
  state.viewMode = mode;
  localStorage.setItem("filmAssetViewMode", mode);
  renderAssets();
}

$$('[data-stage]').forEach((button) => button.addEventListener("click", () => activateStage(button.dataset.stage)));
$("#search").addEventListener("input", (event) => { state.search = event.target.value; renderAssets(); renderSelection(); });
$("#sort-order").addEventListener("change", (event) => {
  state.sort = event.target.value;
  localStorage.setItem("filmAssetSort", state.sort);
  renderAssets();
});
$("#view-grid").addEventListener("click", () => setViewMode("grid"));
$("#view-list").addEventListener("click", () => setViewMode("list"));
$$('[data-thumbnail-size]').forEach((button) => button.addEventListener("click", () => {
  state.thumbnailSize = button.dataset.thumbnailSize;
  localStorage.setItem("filmAssetThumbnailSize", state.thumbnailSize);
  renderAssets();
}));
$("#select-visible").addEventListener("click", toggleVisibleSelection);
$("#refresh").addEventListener("click", loadWorkspace);
$("#frame-upload").addEventListener("change", uploadFrames);
$("#process-2d").addEventListener("click", process2D);
$("#batch-approve").addEventListener("click", () => batchReview("approved"));
$("#batch-reject").addEventListener("click", () => batchReview("rejected"));
$("#resubmit-selected").addEventListener("click", resubmitSelected);
$("#generate-selected").addEventListener("click", generateSelected);
$("#open-settings").addEventListener("click", () => openSettings().catch((error) => showResultDialog("设置加载失败", error.message, true)));
$("#close-settings").addEventListener("click", () => $("#settings-dialog").close());
$("#hunyuan-api-style").addEventListener("change", toggleCredentialFields);
$("#credentials-form").addEventListener("submit", saveSettings);
$("#test-settings").addEventListener("click", testSettings);
setJobBar(false);
loadWorkspace()
  .then(() => {
    if (state.workspace.configuration?.setup_required) {
      openSettings().catch((error) => showResultDialog("设置加载失败", error.message, true));
    }
    try {
      const activeJob = JSON.parse(localStorage.getItem(ACTIVE_JOB_KEY));
      if (activeJob?.job_id) watchJob(activeJob, activeJob.title || "任务执行中");
    } catch (_) {
      localStorage.removeItem(ACTIVE_JOB_KEY);
    }
  })
  .catch((error) => showResultDialog("工作台加载失败", error.message, true));
