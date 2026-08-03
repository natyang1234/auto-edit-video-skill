const elements = {};
let projectPayload = null;
let state = null;
let selectedOverlayId = null;
let saveTimer = null;
let toastTimer = null;
let renderPollTimer = null;
let pipelinePollTimer = null;
let history = [];
let sourceMediaUrl = null;
let showingRenderedMedia = false;
let lastOverlaySignature = "";
let highlightSegments = [];
let stateDirty = false;
let selectedEffectSpanId = null;
let effectCreationMode = false;
let activeTemplateGroup = "fixed";
let batchRenderActive = false;
let renderBusy = false;

const DIRECTOR_CARD_META = {
  "teacher-punch": { icon: "教", eyebrow: "清楚拆解" },
  "high-energy": { icon: "爆", eyebrow: "強 Hook" },
  documentary: { icon: "聞", eyebrow: "衝突脈絡" },
  minimal: { icon: "視", eyebrow: "第一視角" },
  "editorial-clean": { icon: "編", eyebrow: "克制精準" },
};
const DIRECTOR_ORDER = ["teacher-punch", "high-energy", "documentary", "minimal", "editorial-clean"];
const ROLE_LAYOUTS = {
  hook: { x: 50, y: 50, width: 100, height: 100 },
  concept: { x: 50, y: 56, width: 93, height: 22 },
  rule: { x: 50, y: 56, width: 93, height: 27 },
  memory: { x: 50, y: 16, width: 87, height: 15 },
  recap: { x: 50, y: 41, width: 92, height: 48 },
};

const byId = (id) => document.getElementById(id);
const deepCopy = (value) => JSON.parse(JSON.stringify(value));

function cacheElements() {
  [
    "project-name", "platform-select", "director-select", "save-state", "save-button",
    "template-select", "template-group-tabs", "template-grid", "template-controls",
    "template-name", "template-description", "template-motion-chip", "frame-controls",
    "template-frame-x", "template-frame-x-output", "template-frame-y", "template-frame-y-output",
    "template-frame-width", "template-frame-width-output", "template-frame-height", "template-frame-height-output",
    "subject-controls", "template-subject-x", "template-subject-x-output",
    "template-subject-y", "template-subject-y-output", "template-subject-scale", "template-subject-scale-output",
    "background-controls", "template-background-color-row", "template-background-color",
    "template-background-fit-row", "template-background-fit",
    "template-background-button", "template-background-input", "template-background-status",
    "template-capability-note", "template-background-image", "template-background-video",
    "render-button", "source-file-name", "source-file-detail", "source-preview-button",
    "transcript-calibration-status", "transcript-calibration-title", "transcript-calibration-copy",
    "highlight-count", "highlight-list", "editing-brief", "director-grid", "candidate-count",
    "highlight-editor", "highlight-title", "highlight-start", "highlight-end", "replan-highlights",
    "keep-highlight", "reject-highlight", "approve-highlights",
    "candidate-list", "approve-cuts", "layer-list", "layer-count",
    "asset-upload-button", "asset-input", "canvas-resolution", "source-meta", "stage-frame",
    "preview-video", "overlay-layer", "safe-zone", "stage-empty", "warning-copy", "jump-start",
    "play-button", "current-time", "total-time", "scrubber", "toggle-safe-zone", "style-tab",
    "publish-tab", "style-panel", "publish-panel", "inspector-empty", "layer-form", "selected-type",
    "selected-name", "delete-layer", "text-field", "overlay-text", "overlay-start", "overlay-end",
    "font-family", "font-size", "font-size-output", "asset-width-row", "asset-width",
    "asset-width-output", "font-color", "emphasis-color", "position-x", "position-x-output",
    "position-y", "position-y-output", "overlay-animation", "overlay-visible", "effect-editor",
    "effect-style", "effect-color", "effect-scale", "effect-scale-output", "add-effect-span",
    "effect-span-list", "overlay-max-width-row", "overlay-max-width-label", "overlay-max-width",
    "overlay-max-width-output", "card-height-row", "card-height", "card-height-output",
    "layout-warning", "generate-copy",
    "publish-title", "publish-body", "publish-hashtags", "cover-text", "cover-time",
    "cover-time-output", "generate-cover", "cover-preview", "approve-timeline", "render-final",
    "delivery-qa-status", "qa-contact-link", "approve-final",
    "render-batch-final", "batch-retained-count", "batch-render-progress",
    "batch-progress-label", "batch-progress-value", "batch-progress-bar",
    "batch-delivery-qa", "batch-qa-status", "batch-qa-grid",
    "batch-downloads", "download-batch-archive", "batch-output-list",
    "voice-enabled", "voice-language", "voice-gender", "voice-id", "voice-mode",
    "voice-speed", "save-voice", "voice-status",
    "timeline-scroll", "timeline-ruler", "timeline-tracks", "playhead", "toast", "download-output"
  ].forEach((id) => { elements[id] = byId(id); });
}

let csrfToken = "";

async function request(url, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  if (method !== "GET" && method !== "HEAD") {
    options.headers = { ...(options.headers || {}), "X-Auto-Edit-CSRF": csrfToken };
  }
  const response = await fetch(url, options);
  let payload = null;
  try {
    payload = await response.json();
  } catch (_error) {
    payload = { error: `伺服器回傳 ${response.status}` };
  }
  if (!response.ok) {
    const message = payload.error || (payload.errors || []).join("；") || `請求失敗（${response.status}）`;
    throw new Error(message);
  }
  if (payload && typeof payload.csrf_token === "string") {
    csrfToken = payload.csrf_token;
  }
  return payload;
}

function showToast(message, tone = "info") {
  clearTimeout(toastTimer);
  elements.toast.hidden = false;
  elements.toast.textContent = message;
  elements.toast.style.borderLeftColor = tone === "error" ? "var(--danger)" : tone === "success" ? "var(--green)" : "var(--amber)";
  toastTimer = setTimeout(() => { elements.toast.hidden = true; }, 4200);
}

function setSaveState(label, mode = "dirty") {
  elements["save-state"].classList.toggle("is-saved", mode === "saved");
  elements["save-state"].classList.toggle("is-error", mode === "error");
  elements["save-state"].querySelector("span:last-child").textContent = label;
}

function renderTranscriptStatus() {
  const container = elements["transcript-calibration-status"];
  const calibration = projectPayload?.transcript_calibration || {};
  const review = projectPayload?.transcript_review || {};
  const semantic = review.semantic_calibration || {};
  const contextual = projectPayload?.transcript_semantic_review
    || review.contextual_semantic_calibration
    || {};
  const contextualStatus = String(contextual.status || "not_configured");
  const reviewedUnits = Number(contextual.reviewed_unit_count || 0);
  const totalUnits = Number(contextual.total_unit_count || 0);
  const appliedContextual = Number(
    contextual.applied_correction_count ?? contextual.accepted_count ?? 0,
  );
  const pendingContextual = Number(contextual.pending_count || 0);
  const status = String(calibration.status || semantic.status || "not_configured");
  const correctionCount = Number(calibration.correction_count || semantic.correction_count || 0);
  const mechanicalCount = Number(review.mechanical_issue_count ?? review.issue_count ?? 0);
  container.classList.remove("is-applied", "is-pending", "is-warning");
  if (contextualStatus === "complete_needs_review") {
    container.classList.add(pendingContextual > 0 ? "is-pending" : "is-applied");
    elements["transcript-calibration-title"].textContent = "全文上下文語意校準已執行";
    elements["transcript-calibration-copy"].textContent = `已逐句檢查 ${reviewedUnits}/${totalUnits}；${appliedContextual} 處已更正、${pendingContextual} 處待確認。`;
    return;
  }
  if (contextualStatus === "partial_needs_review") {
    container.classList.add("is-warning");
    elements["transcript-calibration-title"].textContent = "全文語意校準未完成";
    elements["transcript-calibration-copy"].textContent = `目前只檢查 ${reviewedUnits}/${totalUnits}；請完成全文覆蓋後再使用字幕。`;
    return;
  }
  if (contextualStatus === "pending") {
    container.classList.add("is-pending");
    elements["transcript-calibration-title"].textContent = "等待全文上下文校準";
    elements["transcript-calibration-copy"].textContent = `目前 0/${totalUnits}；尚未完成逐句語意檢查。`;
    return;
  }
  if (status === "applied_needs_review") {
    container.classList.add("is-applied");
    elements["transcript-calibration-title"].textContent = "字幕語義校準已套用";
    elements["transcript-calibration-copy"].textContent = `${correctionCount} 處已更正；仍需逐段抽查。`;
    return;
  }
  if (status === "not_configured") {
    container.classList.add("is-warning");
    elements["transcript-calibration-title"].textContent = "字幕尚未語義校準";
    elements["transcript-calibration-copy"].textContent = `機械警示 ${mechanicalCount} 項；0 項也不代表語意正確。`;
    return;
  }
  container.classList.add("is-pending");
  elements["transcript-calibration-title"].textContent = "字幕校準待確認";
  elements["transcript-calibration-copy"].textContent = `目前狀態：${status}`;
}

function formatTime(seconds) {
  const safe = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
  const minutes = Math.floor(safe / 60);
  const remainder = safe - minutes * 60;
  return `${String(minutes).padStart(2, "0")}:${remainder.toFixed(3).padStart(6, "0")}`;
}

function formatClipTime(seconds) {
  const safe = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
  const minutes = Math.floor(safe / 60);
  const remainder = Math.floor(safe % 60);
  return `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
}

function duration() {
  return Number(projectPayload?.manifest?.source?.duration_s || elements["preview-video"].duration || 0);
}

function activeHighlight() {
  const activeId = state?.active_highlight_id;
  if (!activeId || !Array.isArray(state?.highlights)) return null;
  return state.highlights.find((item) => String(item.id) === String(activeId)) || null;
}

function retainedHighlights() {
  if (!Array.isArray(state?.highlights)) return [];
  return state.highlights.filter((item) => item.review_status === "approved");
}

function updateBatchRetainedCount() {
  const count = retainedHighlights().length;
  elements["batch-retained-count"].textContent = `已保留 ${count} 段`;
  elements["render-batch-final"].textContent = `批次輸出已保留精華（${count}）`;
  elements["render-batch-final"].disabled = renderBusy || batchRenderActive || count === 0;
}

function setRenderBusy(isBusy) {
  renderBusy = Boolean(isBusy);
  elements["render-button"].disabled = renderBusy;
  elements["render-final"].disabled = renderBusy;
  if (renderBusy) elements["approve-final"].disabled = true;
  updateBatchRetainedCount();
}

function overlayBelongsToHighlight(overlay, highlight = activeHighlight()) {
  const scopedId = overlay?.highlight_id ? String(overlay.highlight_id) : "";
  if (!highlight) return !scopedId;
  return !scopedId || scopedId === String(highlight.id);
}

function timelineBounds() {
  const active = activeHighlight();
  return active
    ? { start: Number(active.start), end: Number(active.end) }
    : { start: 0, end: duration() };
}

function timelineDuration() {
  const bounds = timelineBounds();
  return Math.max(0.01, bounds.end - bounds.start);
}

function updateScrubberBounds() {
  const bounds = timelineBounds();
  elements.scrubber.min = String(bounds.start);
  elements.scrubber.max = String(bounds.end);
  const current = Number(elements["preview-video"].currentTime || 0);
  if (!showingRenderedMedia && (current < bounds.start || current > bounds.end)) {
    elements["preview-video"].currentTime = bounds.start;
  }
}

function sourcePathToUrl(source) {
  if (!source) return "";
  return `/${source.split("/").map(encodeURIComponent).join("/")}`;
}

function currentOverlay() {
  return state?.overlays?.find((overlay) => overlay.id === selectedOverlayId) || null;
}

function pushHistory() {
  if (!state) return;
  history.push(deepCopy(state));
  if (history.length > 40) history.shift();
}

function undo() {
  if (!history.length) {
    showToast("目前沒有可撤銷的變更");
    return;
  }
  state = history.pop();
  selectedOverlayId = state.review?.selected_overlay_id || null;
  activeTemplateGroup = projectPayload.video_templates?.[state.video_template?.id]?.group || "fixed";
  markDirty("已撤銷上一個變更");
  renderAll();
}

function ensureSourcePreview() {
  if (!showingRenderedMedia || !sourceMediaUrl) return;
  const time = Math.min(elements["preview-video"].currentTime || 0, duration());
  elements["preview-video"].src = sourceMediaUrl;
  elements["preview-video"].addEventListener("loadedmetadata", () => {
    elements["preview-video"].currentTime = time;
  }, { once: true });
  showingRenderedMedia = false;
  elements["overlay-layer"].hidden = false;
}

function markDirty(message = "尚未儲存") {
  stateDirty = true;
  ensureSourcePreview();
  setSaveState(message, "dirty");
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => saveState(false), 650);
  renderPreviewOverlays(true);
  renderLayerList();
  renderTimeline();
}

async function saveState(showConfirmation = true) {
  clearTimeout(saveTimer);
  saveTimer = null;
  try {
    const payload = await request("/api/editor-state", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state),
    });
    state.updated_at = payload.updated_at;
    state.revision = payload.revision;
    projectPayload.approval_revisions = payload.approval_revisions || projectPayload.approval_revisions;
    stateDirty = false;
    if ((payload.invalidated_gates || []).includes("highlight_selection")) {
      if (projectPayload?.manifest?.approvals?.highlight_selection) {
        projectPayload.manifest.approvals.highlight_selection.approved = false;
      }
      elements["approve-highlights"].textContent = "核可精華選段";
    }
    if ((payload.invalidated_gates || []).includes("timeline")) {
      if (projectPayload?.manifest?.approvals?.timeline) {
        projectPayload.manifest.approvals.timeline.approved = false;
      }
      elements["approve-timeline"].textContent = "核可時間軸";
      showToast("畫面內容已變更，時間軸核可已失效", "info");
    }
    if ((payload.invalidated_gates || []).includes("final")) {
      if (projectPayload?.manifest?.approvals?.final) {
        projectPayload.manifest.approvals.final.approved = false;
      }
      if (projectPayload?.approval_current) projectPayload.approval_current.final = false;
      renderDeliveryQa();
    }
    setSaveState("已儲存", "saved");
    if (showConfirmation) showToast("時間軸已儲存", "success");
  } catch (error) {
    setSaveState("儲存失敗", "error");
    showToast(`儲存失敗：${error.message}`, "error");
  }
}

function populatePresets() {
  elements["platform-select"].replaceChildren();
  Object.entries(projectPayload.platform_presets).forEach(([id, preset]) => {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = preset.label;
    elements["platform-select"].append(option);
  });
  elements["director-select"].replaceChildren();
  Object.entries(projectPayload.director_presets).forEach(([id, preset]) => {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = preset.label;
    option.title = preset.description;
    elements["director-select"].append(option);
  });
  elements["template-select"].replaceChildren();
  Object.entries(projectPayload.video_templates || {}).forEach(([id, preset]) => {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = preset.label;
    option.disabled = preset.available === false;
    elements["template-select"].append(option);
  });
  elements["platform-select"].value = state.canvas.platform_id;
  elements["director-select"].value = state.director_style;
  const template = projectPayload.video_templates?.[state.video_template?.id];
  activeTemplateGroup = template?.group || "fixed";
  elements["template-select"].value = state.video_template?.id || "dynamic-craft";
  renderTemplatePicker();
  renderDirectorCards();
}

function renderDirectorCards() {
  elements["director-grid"].replaceChildren();
  Object.entries(projectPayload.director_presets)
    .sort(([left], [right]) => DIRECTOR_ORDER.indexOf(left) - DIRECTOR_ORDER.indexOf(right))
    .forEach(([id, preset], index) => {
      const meta = DIRECTOR_CARD_META[id] || { icon: "導", eyebrow: "導演風格" };
      const button = document.createElement("button");
      button.type = "button";
      button.className = `director-card${id === state.director_style ? " is-selected" : ""}`;
      button.setAttribute("role", "radio");
      button.setAttribute("aria-checked", String(id === state.director_style));
      button.dataset.directorId = id;
      button.style.setProperty("--director-index", String(index));

      const icon = document.createElement("span");
      icon.className = "director-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = meta.icon;
      const copy = document.createElement("span");
      copy.className = "director-copy";
      const eyebrow = document.createElement("small");
      eyebrow.textContent = meta.eyebrow;
      const name = document.createElement("strong");
      name.textContent = preset.label;
      const description = document.createElement("span");
      description.textContent = preset.description;
      copy.append(eyebrow, name, description);
      const check = document.createElement("i");
      check.className = "director-check";
      check.setAttribute("aria-hidden", "true");
      check.textContent = "✓";
      button.append(icon, copy, check);
      button.addEventListener("click", () => {
        elements["director-select"].value = id;
        elements["director-select"].dispatchEvent(new Event("change", { bubbles: true }));
      });
      elements["director-grid"].append(button);
    });
}

function templateStateDefaults(templateId) {
  const preset = projectPayload.video_templates[templateId];
  return {
    id: templateId,
    frame: deepCopy(preset.frame_defaults),
    subject: { x: 50, y: 54, scale: 1, feather: 2, mask_stride: 3 },
    background: { color: "#17251d", source: null, fit: "cover", blur: 0 },
  };
}

function selectVideoTemplate(templateId) {
  const preset = projectPayload.video_templates?.[templateId];
  if (!preset || preset.available === false) {
    showToast(preset?.unavailable_reason || "這個模板目前不可用", "error");
    return;
  }
  pushHistory();
  const previous = state.video_template || {};
  const next = templateStateDefaults(templateId);
  if (previous.subject) next.subject = { ...next.subject, ...deepCopy(previous.subject) };
  if (previous.background?.color) next.background.color = previous.background.color;
  if (previous.background?.fit) next.background.fit = previous.background.fit;
  state.video_template = next;
  activeTemplateGroup = preset.group;
  elements["template-select"].value = templateId;
  renderTemplatePicker();
  applyCanvas();
  markDirty(`已套用「${preset.label}」畫面模板`);
  showToast(`畫面模板已改為「${preset.label}」`, "success");
}

function renderTemplatePicker() {
  const catalog = projectPayload.video_templates || {};
  const currentId = state.video_template?.id || "dynamic-craft";
  const current = catalog[currentId];
  if (!current) return;
  elements["template-select"].value = currentId;
  elements["template-grid"].replaceChildren();
  elements["template-group-tabs"].querySelectorAll("[data-template-group]").forEach((button) => {
    const selected = button.dataset.templateGroup === activeTemplateGroup;
    button.setAttribute("aria-selected", String(selected));
    button.classList.toggle("is-selected", selected);
  });
  Object.entries(catalog)
    .filter(([, preset]) => preset.group === activeTemplateGroup)
    .forEach(([id, preset]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `template-card${id === currentId ? " is-selected" : ""}`;
      button.setAttribute("role", "radio");
      button.setAttribute("aria-checked", String(id === currentId));
      button.disabled = preset.available === false;
      const motion = preset.camera_motion === "none" ? "固定" : preset.camera_motion === "punch" ? "推近" : "重構";
      const marker = document.createElement("span");
      marker.className = "template-card-marker";
      marker.textContent = preset.subject_mode === "cutout" ? "人" : motion.slice(0, 1);
      const copy = document.createElement("span");
      copy.innerHTML = `<strong></strong><small></small>`;
      copy.querySelector("strong").textContent = preset.short_label || preset.label;
      copy.querySelector("small").textContent = preset.available === false ? "本機功能未就緒" : `${motion}鏡位`;
      button.title = preset.description;
      button.append(marker, copy);
      button.addEventListener("click", () => selectVideoTemplate(id));
      elements["template-grid"].append(button);
    });

  elements["template-name"].textContent = current.label;
  elements["template-description"].textContent = current.description;
  const motionLabel = current.camera_motion === "none" ? "固定鏡位" : current.camera_motion === "punch" ? "克制推近" : "動態重構";
  elements["template-motion-chip"].textContent = motionLabel;
  elements["template-motion-chip"].dataset.motion = current.camera_motion;

  const frame = state.video_template.frame;
  [["x", "%"], ["y", "%"], ["width", "%"], ["height", "%"]].forEach(([key, suffix]) => {
    const input = elements[`template-frame-${key}`];
    const output = elements[`template-frame-${key}-output`];
    input.value = String(frame[key]);
    output.textContent = `${Math.round(Number(frame[key]))}${suffix}`;
  });
  const isCutout = current.subject_mode === "cutout";
  elements["frame-controls"].hidden = isCutout;
  elements["subject-controls"].hidden = !isCutout;
  elements["background-controls"].hidden = !isCutout;
  const subject = state.video_template.subject;
  ["x", "y"].forEach((key) => {
    elements[`template-subject-${key}`].value = String(subject[key]);
    elements[`template-subject-${key}-output`].textContent = `${Math.round(Number(subject[key]))}%`;
  });
  elements["template-subject-scale"].value = String(subject.scale);
  elements["template-subject-scale-output"].textContent = `${Math.round(Number(subject.scale) * 100)}%`;
  const background = state.video_template.background;
  elements["template-background-color"].value = background.color || "#17251d";
  const needsAsset = ["image", "video"].includes(current.background_mode);
  elements["template-background-color-row"].hidden = current.background_mode !== "solid";
  elements["template-background-fit-row"].hidden = !needsAsset;
  elements["template-background-fit"].value = background.fit || "cover";
  elements["template-background-button"].hidden = !needsAsset;
  elements["template-background-status"].hidden = !needsAsset;
  elements["template-background-button"].textContent = current.background_mode === "video" ? "選擇背景影片" : "選擇背景圖片";
  elements["template-background-input"].accept = current.background_mode === "video"
    ? "video/mp4,video/quicktime"
    : "image/png,image/jpeg,image/webp";
  const source = String(background.source || "");
  elements["template-background-status"].textContent = source
    ? `已選：${source.split("/").pop()}`
    : "尚未選擇背景素材；輸出會先擋下來";
  const capability = projectPayload.template_capabilities?.cutout || {};
  elements["template-capability-note"].hidden = !isCutout;
  elements["template-capability-note"].classList.toggle("is-error", isCutout && !capability.available);
  if (isCutout) {
    elements["template-capability-note"].textContent = capability.available
      ? "本機人物去背模型已就緒。處理可能較慢；畫布先顯示人物定位預覽，產生預覽後會看到真實去背邊緣。"
      : `人物去背不可用：${capability.reason || "本機引擎未就緒"}`;
  }
}

function templateAssetUrl(source) {
  const name = String(source || "").split("/").pop();
  return name ? `/assets/${encodeURIComponent(name)}` : "";
}

function applyTemplatePreview() {
  const template = projectPayload.video_templates?.[state.video_template?.id];
  if (!template) return;
  const video = elements["preview-video"];
  const stage = elements["stage-frame"];
  const image = elements["template-background-image"];
  const backgroundVideo = elements["template-background-video"];
  const frame = state.video_template.frame;
  const background = state.video_template.background;
  image.hidden = true;
  backgroundVideo.hidden = true;
  image.style.objectFit = background.fit === "contain" ? "contain" : "cover";
  backgroundVideo.style.objectFit = background.fit === "contain" ? "contain" : "cover";
  stage.classList.toggle("is-cutout-guide", template.subject_mode === "cutout");
  stage.style.setProperty("--template-background", background.color || "#17251d");

  if (template.subject_mode === "cutout") {
    const source = templateAssetUrl(background.source);
    if (template.background_mode === "image" && source) {
      image.src = source;
      image.hidden = false;
    } else if (template.background_mode === "video" && source) {
      if (backgroundVideo.getAttribute("src") !== source) backgroundVideo.src = source;
      backgroundVideo.hidden = false;
      backgroundVideo.play().catch(() => {});
    }
    const subject = state.video_template.subject;
    video.style.inset = "auto";
    video.style.left = `${subject.x}%`;
    video.style.top = `${subject.y}%`;
    video.style.width = `${Math.min(140, 72 * Number(subject.scale))}%`;
    video.style.height = `${Math.min(140, 72 * Number(subject.scale))}%`;
    video.style.transform = "translate(-50%, -50%)";
    video.style.objectFit = "contain";
    return;
  }
  video.style.inset = "auto";
  video.style.left = `${Number(frame.x) - Number(frame.width) / 2}%`;
  video.style.top = `${Number(frame.y) - Number(frame.height) / 2}%`;
  video.style.width = `${frame.width}%`;
  video.style.height = `${frame.height}%`;
  video.style.transform = "none";
  video.style.objectFit = frame.fit === "contain" ? "contain" : "cover";
}

function applyCanvas() {
  const preset = projectPayload.platform_presets[state.canvas.platform_id];
  const ratio = `${state.canvas.width} / ${state.canvas.height}`;
  elements["stage-frame"].style.setProperty("--canvas-ratio", ratio);
  applyTemplatePreview();
  elements["canvas-resolution"].textContent = `${state.canvas.width} × ${state.canvas.height} · ${preset.aspect}`;
  elements["safe-zone"].style.setProperty("--safe-top", `${preset.safe.top}%`);
  elements["safe-zone"].style.setProperty("--safe-right", `${preset.safe.right}%`);
  elements["safe-zone"].style.setProperty("--safe-bottom", `${preset.safe.bottom}%`);
  elements["safe-zone"].style.setProperty("--safe-left", `${preset.safe.left}%`);
  elements["safe-zone"].hidden = !state.canvas.show_safe_zones;
  elements["toggle-safe-zone"].setAttribute("aria-pressed", String(state.canvas.show_safe_zones));
}

function renderCandidateList() {
  const candidates = projectPayload.edit_candidates?.items || [];
  const decisions = new Map((projectPayload.edit_decisions?.items || []).map((item) => [item.candidate_id, item.action]));
  elements["candidate-count"].textContent = String(candidates.length);
  elements["candidate-list"].replaceChildren();
  if (!candidates.length) {
    const empty = document.createElement("p");
    empty.className = "section-note";
    empty.textContent = "這支影片沒有低風險刪除建議。";
    elements["candidate-list"].append(empty);
    return;
  }
  candidates.forEach((candidate) => {
    const label = document.createElement("label");
    label.className = "candidate-item";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.dataset.candidateId = candidate.id;
    checkbox.checked = (decisions.get(candidate.id) || candidate.default_action) === "delete";
    const copy = document.createElement("span");
    const title = document.createElement("strong");
    const typeLabels = { silence: "刪除空白", filler: "刪除贅字", stutter: "刪除前一次卡詞", repetition: "刪除前一個重複", false_start: "刪除未完成句" };
    title.textContent = typeLabels[candidate.type] || candidate.type;
    const meta = document.createElement("span");
    meta.textContent = `${candidate.start.toFixed(2)}–${candidate.end.toFixed(2)}s · ${candidate.risk === "high" ? "高風險" : "低風險"}`;
    copy.append(title, meta);
    label.append(checkbox, copy);
    label.addEventListener("mouseenter", () => { elements["preview-video"].currentTime = candidate.start; });
    elements["candidate-list"].append(label);
  });
}

function deriveHighlightSegments() {
  const supplied = Array.isArray(state.highlights) ? state.highlights : [];
  const validSupplied = supplied.filter((item) =>
    Number.isFinite(Number(item.start)) && Number.isFinite(Number(item.end)) && Number(item.end) > Number(item.start)
  ).slice(0, 10);
  if (validSupplied.length) {
    return validSupplied.map((item, index) => ({
      id: String(item.id || `highlight-${index + 1}`),
      start: Number(item.start),
      end: Number(item.end),
      title: String(item.title || item.text || `精華片段 ${index + 1}`),
      reviewStatus: String(item.review_status || "pending"),
      score: Number(item.score || 0),
      overlayId: item.overlay_id || null,
      source: "AI 精華",
    }));
  }

  const captions = state.overlays
    .filter((overlay) => overlay.type === "caption" && overlay.visible !== false)
    .sort((a, b) => a.start - b.start);
  if (!captions.length) return [];
  const count = Math.min(10, captions.length);
  const groups = [];
  for (let index = 0; index < count; index += 1) {
    const firstIndex = Math.floor((index * captions.length) / count);
    const lastIndex = Math.max(firstIndex, Math.floor(((index + 1) * captions.length) / count) - 1);
    const first = captions[firstIndex];
    const last = captions[lastIndex];
    const rawTitle = captions.slice(firstIndex, lastIndex + 1).map((item) => item.text).join(" ").trim();
    groups.push({
      id: `caption-section-${index + 1}`,
      start: first.start,
      end: last.end,
      title: rawTitle || `字幕段落 ${index + 1}`,
      reviewStatus: "pending",
      score: 0,
      overlayId: first.id,
      source: "字幕段落",
    });
  }
  return groups;
}

function renderHighlightList() {
  highlightSegments = deriveHighlightSegments();
  elements["highlight-count"].textContent = String(highlightSegments.length);
  updateBatchRetainedCount();
  elements["highlight-list"].replaceChildren();
  if (!highlightSegments.length) {
    const empty = document.createElement("div");
    empty.className = "highlight-empty";
    const title = document.createElement("strong");
    title.textContent = "尚未建立片段";
    const copy = document.createElement("span");
    copy.textContent = "完成本機轉錄後，字幕段落會顯示在這裡。";
    empty.append(title, copy);
    elements["highlight-list"].append(empty);
    return;
  }
  highlightSegments.forEach((segment, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `highlight-row is-${segment.reviewStatus}`;
    button.dataset.highlightIndex = String(index);
    button.style.setProperty("--clip-index", String(index));
    button.setAttribute("aria-label", `片段 ${index + 1}：${segment.title}`);

    const number = document.createElement("span");
    number.className = "highlight-number";
    number.textContent = String(index + 1).padStart(2, "0");
    const thumb = document.createElement("span");
    thumb.className = "highlight-thumb";
    thumb.setAttribute("aria-hidden", "true");
    const copy = document.createElement("span");
    copy.className = "highlight-copy";
    const title = document.createElement("strong");
    title.textContent = segment.title;
    const meta = document.createElement("span");
    const reviewLabels = { approved: "已保留", rejected: "已排除", pending: "待確認" };
    meta.textContent = `${reviewLabels[segment.reviewStatus] || "待確認"} · ${formatClipTime(segment.start)}–${formatClipTime(segment.end)}`;
    copy.append(title, meta);
    const durationLabel = document.createElement("span");
    durationLabel.className = "highlight-duration";
    durationLabel.textContent = `${Math.max(0, segment.end - segment.start).toFixed(0)}s`;
    button.append(number, thumb, copy, durationLabel);
    button.addEventListener("click", () => {
      selectHighlight(segment.id, true);
    });
    elements["highlight-list"].append(button);
  });
  updateActiveHighlight();
  renderHighlightEditor();
}

function updateActiveHighlight() {
  const activeIndex = highlightSegments.findIndex((segment) => segment.id === state.active_highlight_id);
  elements["highlight-list"].querySelectorAll(".highlight-row").forEach((button, index) => {
    const active = index === activeIndex;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "true");
    else button.removeAttribute("aria-current");
  });
}

function selectHighlight(highlightId, seek = true) {
  const segment = (state.highlights || []).find((item) => String(item.id) === String(highlightId));
  if (!segment) return;
  const changed = state.active_highlight_id !== segment.id;
  state.active_highlight_id = segment.id;
  const selected = state.overlays.find((overlay) => overlay.id === selectedOverlayId);
  if (!selected || !overlayBelongsToHighlight(selected, segment) || selected.end <= segment.start || selected.start >= segment.end) {
    selectedOverlayId = state.overlays.find(
      (overlay) => overlayBelongsToHighlight(overlay, segment) && overlay.end > segment.start && overlay.start < segment.end
    )?.id || null;
    state.review = state.review || {};
    state.review.selected_overlay_id = selectedOverlayId;
  }
  if (seek) {
    ensureSourcePreview();
    elements["preview-video"].currentTime = Number(segment.start);
  }
  if (changed) markDirty("作用中片段已變更，儲存中…");
  renderHighlightList();
  renderTimeline();
  renderInspector();
  updateScrubberBounds();
}

function renderHighlightEditor() {
  const highlight = activeHighlight();
  elements["highlight-editor"].hidden = !highlight;
  if (!highlight) return;
  elements["highlight-title"].value = highlight.title || "";
  elements["highlight-start"].value = Number(highlight.start).toFixed(2);
  elements["highlight-end"].value = Number(highlight.end).toFixed(2);
  elements["keep-highlight"].classList.toggle("is-selected", highlight.review_status === "approved");
  elements["reject-highlight"].classList.toggle("is-selected", highlight.review_status === "rejected");
}

function updateActiveHighlightFields() {
  const highlight = activeHighlight();
  if (!highlight) return;
  const start = Math.max(0, Number(elements["highlight-start"].value));
  const end = Math.min(duration(), Number(elements["highlight-end"].value));
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start + 0.01) {
    showToast("片段結束時間必須晚於開始時間", "error");
    renderHighlightEditor();
    return;
  }
  pushHistory();
  highlight.title = elements["highlight-title"].value.trim() || highlight.title;
  highlight.start = Number(start.toFixed(3));
  highlight.end = Number(end.toFixed(3));
  markDirty("精華範圍已變更，儲存中…");
  renderHighlightList();
  renderTimeline();
  updateScrubberBounds();
}

function reviewActiveHighlight(reviewStatus) {
  const highlight = activeHighlight();
  if (!highlight) return;
  pushHistory();
  highlight.review_status = reviewStatus;
  markDirty(reviewStatus === "approved" ? "片段已標記保留" : "片段已標記排除");
  renderHighlightList();
}

function decisionItems() {
  return (projectPayload.edit_candidates?.items || []).map((candidate) => {
    const checkbox = elements["candidate-list"].querySelector(`[data-candidate-id="${candidate.id}"]`);
    return { candidate_id: candidate.id, action: checkbox?.checked ? "delete" : "keep", review_status: "pending" };
  });
}

async function approveCuts() {
  elements["approve-cuts"].disabled = true;
  try {
    const decisions = await request("/api/edit-decisions", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: decisionItems(), approved: true }),
    });
    const result = await request("/api/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        gate: "destructive_edit",
        expected_revision: decisions.approval_revision,
        confirmed_by: "local-editor-user",
        note: "Reviewed edit decisions in Auto Edit Studio",
      }),
    });
    projectPayload.manifest.approvals.destructive_edit = result.approval;
    projectPayload.approval_revisions = result.approval_revisions;
    elements["approve-cuts"].textContent = "刪除決定已核可";
    showToast("刪除決定已核可；尚未執行實際裁切", "success");
  } catch (error) {
    showToast(`核可失敗：${error.message}`, "error");
  } finally {
    elements["approve-cuts"].disabled = false;
  }
}

async function approveHighlights() {
  elements["approve-highlights"].disabled = true;
  try {
    if (!(state.highlights || []).some((item) => item.review_status === "approved")) {
      throw new Error("請先把至少一個精華片段標記為保留");
    }
    await saveState(false);
    if (stateDirty) throw new Error("精華片段尚未成功儲存");
    const result = await request("/api/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        gate: "highlight_selection",
        expected_revision: projectPayload.approval_revisions.highlight_selection,
        confirmed_by: "local-editor-user",
        note: "Reviewed transcript-grounded highlight selection in Auto Edit Studio",
      }),
    });
    projectPayload.manifest.approvals.highlight_selection = result.approval;
    projectPayload.approval_revisions = result.approval_revisions;
    elements["approve-highlights"].textContent = "精華選段已核可";
    showToast("精華選段已核可；接著可校對字幕與時間軸", "success");
  } catch (error) {
    showToast(`精華核可失敗：${error.message}`, "error");
  } finally {
    elements["approve-highlights"].disabled = false;
  }
}

async function replanHighlights() {
  const button = elements["replan-highlights"];
  button.disabled = true;
  try {
    await saveState(false);
    if (stateDirty) throw new Error("目前編輯內容尚未成功儲存");
    const result = await request("/api/plan-highlights", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        director: state.director_style,
        brief: state.editing_brief || "",
        count: 10,
        expected_revision: state.revision,
      }),
    });
    projectPayload.manifest = result.manifest;
    projectPayload.highlight_plan = result.highlight_plan;
    projectPayload.approval_revisions = result.approval_revisions;
    state = result.state;
    selectedOverlayId = state.review?.selected_overlay_id || state.overlays[0]?.id || null;
    elements["editing-brief"].value = state.editing_brief || "";
    elements["approve-highlights"].textContent = "核可精華選段";
    elements["approve-timeline"].textContent = "核可時間軸";
    renderAll();
    updateScrubberBounds();
    setSaveState("已重新規劃", "saved");
    showToast(`已依「${projectPayload.director_presets[state.director_style].label}」重新產生 ${state.highlights.length} 段精華`, "success");
  } catch (error) {
    showToast(`重新選段失敗：${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
}

function layerLabel(overlay) {
  const typeLabels = { caption: "字幕", emphasis: "特效字", title: "標題卡", card: "字卡", image: "圖片", gif: "GIF", video: "插入影片", animation: "動畫字卡" };
  return typeLabels[overlay.type] || overlay.type;
}

function renderLayerList() {
  const bounds = timelineBounds();
  const visibleOverlays = activeHighlight()
    ? state.overlays.filter((overlay) => overlayBelongsToHighlight(overlay) && overlay.end > bounds.start && overlay.start < bounds.end)
    : state.overlays.filter((overlay) => overlayBelongsToHighlight(overlay, null));
  elements["layer-list"].replaceChildren();
  elements["layer-count"].textContent = String(visibleOverlays.length);
  if (!visibleOverlays.length) {
    const empty = document.createElement("p");
    empty.className = "layer-list-empty";
    empty.textContent = "目前沒有圖層；可從上方加入字幕、動畫或素材。";
    elements["layer-list"].append(empty);
    return;
  }
  [...visibleOverlays]
    .sort((a, b) => a.start - b.start || (a.z_index || 0) - (b.z_index || 0))
    .forEach((overlay) => {
      const button = document.createElement("button");
      button.type = "button";
      const semanticPending = overlay.type === "caption"
        && overlay.semantic_review?.status === "pending";
      button.className = `layer-row${overlay.id === selectedOverlayId ? " is-selected" : ""}${semanticPending ? " is-semantic-pending" : ""}`;
      const title = document.createElement("strong");
      title.textContent = `${layerLabel(overlay)} · ${overlay.text || overlay.source?.split("/").pop() || "未命名"}`;
      const timing = document.createElement("span");
      const candidate = overlay.semantic_review?.candidates?.[0];
      const semanticNote = semanticPending
        ? ` · 語意待確認${candidate ? `：${candidate.source} → ${candidate.replacement}` : ""}`
        : "";
      timing.textContent = `${overlay.start.toFixed(2)}–${overlay.end.toFixed(2)}s${overlay.visible === false ? " · 已隱藏" : ""}${semanticNote}`;
      if (semanticPending) button.title = "此字幕有全文上下文校準候選，請在右側字幕欄確認。";
      button.append(title, timing);
      button.addEventListener("click", () => selectOverlay(overlay.id, true));
      elements["layer-list"].append(button);
    });
}

function normalizedEffectSpans(overlay) {
  const text = String(overlay.text || "");
  const explicit = Array.isArray(overlay.effect_spans) ? overlay.effect_spans : [];
  const valid = explicit.filter((span) => Number.isInteger(span.start_char)
    && Number.isInteger(span.end_char)
    && span.start_char >= 0
    && span.end_char > span.start_char
    && span.end_char <= text.length
    && text.slice(span.start_char, span.end_char) === String(span.text || ""))
    .sort((a, b) => a.start_char - b.start_char || a.end_char - b.end_char);
  if (valid.length) return valid;
  const phrases = overlay.type === "emphasis" ? [text] : (overlay.emphasis || []).filter(Boolean);
  let cursor = 0;
  return phrases.flatMap((phrase, index) => {
    const start = text.indexOf(phrase, cursor);
    if (start < 0) return [];
    const end = start + phrase.length;
    cursor = end;
    return [{
      id: `legacy-fx-${index + 1}`,
      text: phrase,
      start_char: start,
      end_char: end,
      style: {
        effect: "pop",
        color: overlay.style?.emphasis_color || "#ffd447",
        font_scale: 1.18,
      },
    }];
  });
}

function createTextWithEmphasis(overlay) {
  const fragment = document.createDocumentFragment();
  const text = String(overlay.text || "");
  const spans = normalizedEffectSpans(overlay);
  let cursor = 0;
  spans.forEach((span) => {
    if (span.start_char < cursor) return;
    fragment.append(document.createTextNode(text.slice(cursor, span.start_char)));
    const mark = document.createElement("mark");
    const effectStyle = span.style || {};
    const effect = ["pop", "highlight", "underline"].includes(effectStyle.effect) ? effectStyle.effect : "pop";
    const fontScale = Math.min(3, Math.max(0.5, Number(effectStyle.font_scale) || 1.18));
    mark.className = `effect-word effect-${effect}`;
    mark.dataset.effect = effect;
    mark.style.setProperty("--effect-color", effectStyle.color || overlay.style?.emphasis_color || "#ffd447");
    mark.style.setProperty("--effect-scale", String(fontScale));
    mark.style.setProperty("--effect-font-size", `${fontScale}em`);
    mark.style.setProperty("--effect-pop-from", String(0.72 / fontScale));
    mark.textContent = text.slice(span.start_char, span.end_char);
    fragment.append(mark);
    cursor = span.end_char;
  });
  fragment.append(document.createTextNode(text.slice(cursor)));
  return fragment;
}

function isFullScreenHook(overlay) {
  const layout = overlay.layout || ROLE_LAYOUTS.hook;
  return overlay.design_role === "hook"
    && Number(layout.width ?? 100) >= 95
    && Number(layout.height ?? 100) >= 90;
}

function captionReplacedAtTime(overlay, time) {
  if (!["caption", "emphasis"].includes(overlay.type) || overlay.design_role) return false;
  return state.overlays.some((item) => item.visible !== false
    && overlayBelongsToHighlight(item)
    && isFullScreenHook(item)
    && time >= Number(item.start)
    && time < Number(item.end));
}

function renderPreviewOverlays(force = false) {
  if (!state || showingRenderedMedia) return;
  const time = elements["preview-video"].currentTime || 0;
  const active = state.overlays.filter((overlay) => overlayBelongsToHighlight(overlay)
    && overlay.visible !== false
    && time >= overlay.start
    && time < overlay.end
    && !captionReplacedAtTime(overlay, time));
  const signature = JSON.stringify(active.map((overlay) => [
    overlay.id, overlay.text, overlay.source, overlay.style, overlay.layout,
    overlay.emphasis, overlay.effect_spans, selectedOverlayId,
  ]));
  if (!force && signature === lastOverlaySignature) return;
  lastOverlaySignature = signature;
  elements["overlay-layer"].replaceChildren();
  active.sort((a, b) => (a.z_index || 0) - (b.z_index || 0)).forEach((overlay) => {
    const style = overlay.style || {};
    const designCard = Boolean(overlay.design_role);
    const layout = overlay.layout || {};
    const assetType = ["image", "gif", "video"].includes(overlay.type);
    let node;
    if (assetType) {
      node = overlay.type === "video" ? document.createElement("video") : document.createElement("img");
      node.src = sourcePathToUrl(overlay.source);
      if (node.tagName === "VIDEO") {
        node.muted = true;
        node.loop = true;
        node.autoplay = true;
        node.playsInline = true;
      }
      node.alt = overlay.provenance || "插入素材";
    } else {
      node = document.createElement("p");
      if (designCard && overlay.kicker) {
        const kicker = document.createElement("small");
        kicker.textContent = overlay.kicker;
        node.append(kicker);
      }
      node.append(createTextWithEmphasis(overlay));
    }
    node.className = `preview-overlay type-${overlay.type} motion-${style.animation || "none"}`;
    node.dataset.overlayId = overlay.id;
    node.style.left = `${designCard ? (layout.x ?? 50) : (style.x ?? 50)}%`;
    node.style.top = `${designCard ? (layout.y ?? 50) : (style.y ?? 76)}%`;
    node.style.zIndex = String(overlay.z_index || 0);
    node.style.setProperty("--overlay-max-width", `${style.max_width ?? 84}%`);
    node.style.setProperty("--overlay-color", style.color || "#f7f2e8");
    node.style.setProperty("--overlay-emphasis", style.emphasis_color || "#ffd447");
    node.style.setProperty("--overlay-stroke", style.stroke_color || "#17130f");
    node.style.setProperty("--overlay-font", `"${style.font_family || "PingFang TC"}"`);
    const stageScale = elements["stage-frame"].clientWidth / Math.max(1, state.canvas.width);
    node.style.setProperty("--overlay-font-size", `${Math.max(12, (style.font_size || 58) * stageScale)}px`);
    node.style.setProperty("--overlay-weight", String(style.font_weight || 800));
    node.style.setProperty("--overlay-asset-width", `${style.width || 32}%`);
    if (designCard) {
      node.classList.add("is-design-card", `design-${overlay.design_role}`);
      node.style.setProperty("--overlay-card-width", `${layout.width ?? 84}%`);
      node.style.setProperty("--overlay-card-height", `${layout.height ?? 24}%`);
    }
    if (overlay.id === selectedOverlayId) {
      node.style.outline = "2px solid var(--vermilion)";
      node.classList.add("is-draggable");
      node.tabIndex = 0;
      node.setAttribute("aria-label", `${layerLabel(overlay)}；拖曳可調整位置`);
      enableOverlayDrag(node, overlay);
    }
    elements["overlay-layer"].append(node);
  });
}

function editableLayout(overlay) {
  if (!overlay.design_role) return overlay.style || (overlay.style = {});
  if (!overlay.layout) overlay.layout = deepCopy(ROLE_LAYOUTS[overlay.design_role] || { x: 50, y: 50, width: 84, height: 24 });
  return overlay.layout;
}

function effectLabel(effect) {
  return { pop: "重點彈出", highlight: "螢光標記", underline: "動態底線" }[effect] || "重點彈出";
}

function renderEffectSpanList(overlay) {
  elements["effect-span-list"].replaceChildren();
  const spans = Array.isArray(overlay.effect_spans) ? overlay.effect_spans : (overlay.effect_spans = []);
  if (!effectCreationMode && !spans.some((span) => span.id === selectedEffectSpanId)) {
    selectedEffectSpanId = spans[0]?.id || null;
  }
  const selected = spans.find((span) => span.id === selectedEffectSpanId);
  if (selected) {
    elements["effect-style"].value = selected.style?.effect || "pop";
    elements["effect-color"].value = selected.style?.color || overlay.style?.emphasis_color || "#ffd447";
    elements["effect-scale"].value = selected.style?.font_scale || 1.18;
  } else if (!effectCreationMode) {
    elements["effect-color"].value = overlay.style?.emphasis_color || "#ffd447";
  }
  elements["effect-scale-output"].value = `${Number(elements["effect-scale"].value).toFixed(2)}×`;
  if (!spans.length) {
    const empty = document.createElement("p");
    empty.className = "effect-span-empty";
    empty.textContent = "尚未指定重點字；選取字幕中的字詞即可加入。";
    elements["effect-span-list"].append(empty);
    return;
  }
  spans.forEach((span) => {
    const row = document.createElement("div");
    row.className = `effect-span-row${span.id === selectedEffectSpanId ? " is-selected" : ""}`;
    row.style.setProperty("--effect-chip-color", span.style?.color || "#ffd447");
    const select = document.createElement("button");
    select.type = "button";
    select.className = "effect-span-select";
    const title = document.createElement("strong");
    title.textContent = span.text;
    const meta = document.createElement("span");
    meta.textContent = `${effectLabel(span.style?.effect)} · ${Number(span.style?.font_scale || 1.18).toFixed(2)}×`;
    select.append(title, meta);
    select.addEventListener("click", () => {
      effectCreationMode = false;
      selectedEffectSpanId = span.id;
      renderEffectSpanList(overlay);
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "effect-span-remove";
    remove.textContent = "移除";
    remove.setAttribute("aria-label", `移除重點字 ${span.text}`);
    remove.addEventListener("click", () => {
      pushHistory();
      overlay.effect_spans = spans.filter((item) => item.id !== span.id);
      overlay.emphasis = overlay.effect_spans.map((item) => item.text);
      selectedEffectSpanId = overlay.effect_spans[0]?.id || null;
      markDirty("已移除重點字");
      renderEffectSpanList(overlay);
      renderPreviewOverlays(true);
    });
    row.append(select, remove);
    elements["effect-span-list"].append(row);
  });
}

function addEffectSpan() {
  const overlay = currentOverlay();
  if (!overlay || !["caption", "emphasis"].includes(overlay.type) || overlay.design_role) return;
  const input = elements["overlay-text"];
  const start = input.selectionStart;
  const end = input.selectionEnd;
  const text = input.value.slice(start, end);
  if (!text.trim() || end <= start) {
    showToast("請先在字幕文字中選取要強調的字詞", "error");
    input.focus();
    return;
  }
  const spans = Array.isArray(overlay.effect_spans) ? overlay.effect_spans : [];
  if (spans.some((span) => start < span.end_char && end > span.start_char)) {
    showToast("這段文字已和另一個重點字重疊", "error");
    return;
  }
  pushHistory();
  const span = {
    id: `fx-${Date.now().toString(36)}`,
    text,
    start_char: start,
    end_char: end,
    style: {
      effect: elements["effect-style"].value,
      color: elements["effect-color"].value,
      font_scale: Number(elements["effect-scale"].value),
    },
  };
  overlay.effect_spans = [...spans, span].sort((a, b) => a.start_char - b.start_char);
  overlay.emphasis = overlay.effect_spans.map((item) => item.text);
  effectCreationMode = false;
  selectedEffectSpanId = span.id;
  markDirty("已加入可輸出的重點字");
  renderEffectSpanList(overlay);
  renderPreviewOverlays(true);
}

function prepareEffectCreation() {
  const input = elements["overlay-text"];
  if (input.selectionEnd <= input.selectionStart) return;
  effectCreationMode = true;
  selectedEffectSpanId = null;
  elements["effect-span-list"].querySelectorAll(".effect-span-row").forEach((row) => row.classList.remove("is-selected"));
}

function reconcileEffectSpans(overlay, nextText) {
  const spans = Array.isArray(overlay.effect_spans) ? overlay.effect_spans : [];
  const occupied = [];
  overlay.effect_spans = spans.flatMap((span) => {
    let start = span.start_char;
    let end = span.end_char;
    if (nextText.slice(start, end) !== span.text) {
      const first = nextText.indexOf(span.text);
      const unique = first >= 0 && nextText.indexOf(span.text, first + 1) < 0;
      if (!unique) return [];
      start = first;
      end = first + span.text.length;
    }
    if (occupied.some(([usedStart, usedEnd]) => start < usedEnd && end > usedStart)) return [];
    occupied.push([start, end]);
    return [{ ...span, start_char: start, end_char: end }];
  }).sort((a, b) => a.start_char - b.start_char);
  overlay.emphasis = overlay.effect_spans.map((span) => span.text);
}

function layoutBoxForOverlay(overlay) {
  const source = overlay.design_role ? (overlay.layout || ROLE_LAYOUTS[overlay.design_role]) : (overlay.style || {});
  const x = Number(source?.x ?? 50);
  const y = Number(source?.y ?? (overlay.type === "caption" ? 76 : 50));
  if (overlay.design_role) {
    return { x, y, width: Number(source?.width ?? 84), height: Number(source?.height ?? 24) };
  }
  if (["image", "gif", "video"].includes(overlay.type)) {
    const width = Number(source?.width ?? 32);
    return { x, y, width, height: width * 0.7 };
  }
  const width = Number(source?.max_width ?? 84);
  const canvasWidth = Math.max(1, Number(state.canvas.width || 1080));
  const canvasHeight = Math.max(1, Number(state.canvas.height || 1920));
  const fontSize = Number(source?.font_size ?? 58);
  const charactersPerLine = Math.max(1, Math.floor((canvasWidth * width / 100) / Math.max(1, fontSize * 0.95)));
  const lines = Math.max(1, Math.ceil(String(overlay.text || "").length / charactersPerLine));
  const height = Math.min(100, (lines * fontSize * 1.35 / canvasHeight) * 100);
  return { x, y, width, height };
}

function boxesOverlap(a, b) {
  return Math.abs(a.x - b.x) * 2 < a.width + b.width
    && Math.abs(a.y - b.y) * 2 < a.height + b.height;
}

function renderLayoutWarning() {
  const overlay = currentOverlay();
  if (!overlay || !elements["layout-warning"]) return;
  const box = layoutBoxForOverlay(overlay);
  const preset = projectPayload?.platform_presets?.[state.canvas.platform_id];
  const safe = preset?.safe || { top: 8, right: 8, bottom: 18, left: 8 };
  const warnings = [];
  const left = box.x - box.width / 2;
  const right = box.x + box.width / 2;
  const top = box.y - box.height / 2;
  const bottom = box.y + box.height / 2;
  if (left < safe.left || right > 100 - safe.right || top < safe.top || bottom > 100 - safe.bottom) {
    warnings.push("超出平台安全框");
  }
  const conflicts = state.overlays.filter((item) => item.id !== overlay.id
    && item.visible !== false
    && Number(item.end) > Number(overlay.start)
    && Number(item.start) < Number(overlay.end)
    && !(isFullScreenHook(item) && ["caption", "emphasis"].includes(overlay.type))
    && !(isFullScreenHook(overlay) && ["caption", "emphasis"].includes(item.type))
    && boxesOverlap(box, layoutBoxForOverlay(item)))
    .slice(0, 3)
    .map((item) => layerLabel(item));
  if (conflicts.length) warnings.push(`與 ${conflicts.join("、")} 重疊`);
  elements["layout-warning"].classList.toggle("is-warning", warnings.length > 0);
  elements["layout-warning"].textContent = warnings.length
    ? `版面警示：${warnings.join("；")}。可拖曳預覽圖層或調整下方位置與尺寸。`
    : "目前位置在安全範圍內，且沒有和同時間圖層重疊。";
}

function enableOverlayDrag(node, overlay) {
  node.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    pushHistory();
    const target = editableLayout(overlay);
    const origin = { x: Number(target.x ?? 50), y: Number(target.y ?? 50), clientX: event.clientX, clientY: event.clientY };
    node.classList.add("is-dragging");
    node.setPointerCapture(event.pointerId);
    const move = (moveEvent) => {
      const bounds = elements["stage-frame"].getBoundingClientRect();
      target.x = Math.max(0, Math.min(100, origin.x + (moveEvent.clientX - origin.clientX) / bounds.width * 100));
      target.y = Math.max(0, Math.min(100, origin.y + (moveEvent.clientY - origin.clientY) / bounds.height * 100));
      node.style.left = `${target.x}%`;
      node.style.top = `${target.y}%`;
      elements["position-x"].value = String(Math.round(target.x));
      elements["position-y"].value = String(Math.round(target.y));
      elements["position-x-output"].value = `${Math.round(target.x)}%`;
      elements["position-y-output"].value = `${Math.round(target.y)}%`;
      renderLayoutWarning();
    };
    const stop = () => {
      node.classList.remove("is-dragging");
      node.removeEventListener("pointermove", move);
      node.removeEventListener("pointerup", stop);
      node.removeEventListener("pointercancel", stop);
      markDirty("位置已拖曳更新");
    };
    node.addEventListener("pointermove", move);
    node.addEventListener("pointerup", stop);
    node.addEventListener("pointercancel", stop);
  });
}

function renderInspector() {
  const overlay = currentOverlay();
  if (!overlay) {
    elements["inspector-empty"].hidden = false;
    elements["layer-form"].hidden = true;
    return;
  }
  const assetType = ["image", "gif", "video"].includes(overlay.type);
  const designCard = Boolean(overlay.design_role);
  const style = overlay.style || (overlay.style = deepCopy(state.caption_defaults));
  const position = designCard ? editableLayout(overlay) : style;
  elements["inspector-empty"].hidden = true;
  elements["layer-form"].hidden = false;
  elements["selected-type"].textContent = overlay.type.toUpperCase();
  elements["selected-name"].textContent = layerLabel(overlay);
  elements["text-field"].hidden = assetType;
  elements["effect-editor"].hidden = !["caption", "emphasis"].includes(overlay.type) || designCard;
  elements["overlay-text"].value = overlay.text || "";
  elements["overlay-start"].value = overlay.start;
  elements["overlay-end"].value = overlay.end;
  elements["font-family"].value = style.font_family || "PingFang TC";
  elements["font-family"].closest("label").hidden = assetType || designCard;
  elements["font-size"].value = style.font_size || 58;
  elements["font-size-output"].value = style.font_size || 58;
  elements["font-size"].closest("label").hidden = assetType || designCard;
  elements["asset-width-row"].hidden = !assetType;
  elements["asset-width"].value = style.width || 32;
  elements["asset-width-output"].value = `${style.width || 32}%`;
  elements["font-color"].value = style.color || "#f7f2e8";
  elements["emphasis-color"].value = style.emphasis_color || "#ffd447";
  elements["font-color"].closest(".field-pair").hidden = assetType || designCard;
  elements["overlay-max-width-row"].hidden = assetType;
  elements["overlay-max-width-label"].firstChild.textContent = designCard ? "圖卡寬度 " : "字幕最大寬度 ";
  elements["overlay-max-width"].value = designCard ? (position.width ?? 84) : (style.max_width ?? 84);
  elements["overlay-max-width-output"].value = `${designCard ? (position.width ?? 84) : (style.max_width ?? 84)}%`;
  elements["card-height-row"].hidden = !designCard;
  elements["card-height"].value = position.height ?? 24;
  elements["card-height-output"].value = `${position.height ?? 24}%`;
  elements["position-x"].value = position.x ?? 50;
  elements["position-x-output"].value = `${position.x ?? 50}%`;
  elements["position-y"].value = position.y ?? (designCard ? 50 : 76);
  elements["position-y-output"].value = `${position.y ?? (designCard ? 50 : 76)}%`;
  elements["overlay-animation"].value = style.animation || "none";
  elements["overlay-animation"].closest("label").hidden = assetType || designCard;
  elements["overlay-visible"].checked = overlay.visible !== false;
  if (!elements["effect-editor"].hidden) renderEffectSpanList(overlay);
  renderLayoutWarning();
}

function selectOverlay(id, seek = false) {
  selectedOverlayId = id;
  selectedEffectSpanId = null;
  effectCreationMode = false;
  state.review = state.review || {};
  state.review.selected_overlay_id = id;
  const overlay = currentOverlay();
  if (seek && overlay) elements["preview-video"].currentTime = overlay.start;
  renderInspector();
  renderLayerList();
  renderTimeline();
  renderPreviewOverlays(true);
}

function addOverlay(type, source = null) {
  pushHistory();
  const start = Math.min(elements["preview-video"].currentTime || 0, Math.max(0, duration() - 0.2));
  const end = Math.min(duration(), start + (type === "title" ? 2.5 : 3));
  const style = deepCopy(state.caption_defaults);
  if (["image", "gif", "video"].includes(type)) Object.assign(style, { width: 34, x: 50, y: 48, animation: "fade" });
  if (type === "emphasis") Object.assign(style, { font_size: Math.max(72, style.font_size), y: 62, color: style.emphasis_color });
  if (type === "title" || type === "animation") Object.assign(style, { box: true, y: 42, max_width: 82 });
  const id = `${type}-${Date.now().toString(36)}`;
  const defaults = { caption: "新增字幕", emphasis: "關鍵重點", title: "影片標題", animation: "補充說明" };
  state.overlays.push({
    id, type, start, end: Math.max(end, start + 0.2), text: defaults[type] || "", emphasis: [],
    visible: true, locked: false, z_index: ["image", "gif", "video"].includes(type) ? 10 : 30,
    style, source, provenance: source ? "user-uploaded-through-local-editor" : "manual editor layer",
    highlight_id: activeHighlight()?.id || null,
  });
  selectOverlay(id, true);
  markDirty("新增圖層，儲存中…");
}

function deleteSelectedOverlay() {
  const overlay = currentOverlay();
  if (!overlay) return;
  pushHistory();
  state.overlays = state.overlays.filter((item) => item.id !== overlay.id);
  selectedOverlayId = state.overlays[0]?.id || null;
  state.review.selected_overlay_id = selectedOverlayId;
  markDirty("已刪除圖層");
  renderAll();
  showToast("圖層已刪除；按 ⌘Z 撤銷");
}

function updateOverlayFromForm() {
  const overlay = currentOverlay();
  if (!overlay) return;
  const style = overlay.style || (overlay.style = {});
  const designCard = Boolean(overlay.design_role);
  const position = editableLayout(overlay);
  const nextText = elements["overlay-text"].value;
  if (nextText !== overlay.text) {
    reconcileEffectSpans(overlay, nextText);
    if (overlay.semantic_review?.status === "pending") {
      const unresolved = (overlay.semantic_review.candidates || [])
        .filter((candidate) => nextText.includes(String(candidate.source || "")));
      overlay.semantic_review = {
        ...overlay.semantic_review,
        status: unresolved.length ? "pending" : "resolved_manual",
        candidates: unresolved,
        resolved_at: unresolved.length ? null : new Date().toISOString(),
      };
    }
  }
  overlay.text = nextText;
  overlay.start = Math.max(0, Number(elements["overlay-start"].value) || 0);
  overlay.end = Math.min(duration(), Math.max(overlay.start + 0.01, Number(elements["overlay-end"].value) || overlay.start + 0.01));
  style.font_family = elements["font-family"].value;
  style.font_size = Number(elements["font-size"].value);
  style.width = Number(elements["asset-width"].value);
  style.color = elements["font-color"].value;
  style.emphasis_color = elements["emphasis-color"].value;
  position.x = Number(elements["position-x"].value);
  position.y = Number(elements["position-y"].value);
  if (designCard) {
    position.width = Number(elements["overlay-max-width"].value);
    position.height = Number(elements["card-height"].value);
  } else {
    style.max_width = Number(elements["overlay-max-width"].value);
  }
  style.animation = elements["overlay-animation"].value;
  const selectedSpan = effectCreationMode
    ? null
    : (overlay.effect_spans || []).find((span) => span.id === selectedEffectSpanId);
  if (selectedSpan) {
    selectedSpan.style = selectedSpan.style || {};
    selectedSpan.style.effect = elements["effect-style"].value;
    selectedSpan.style.color = elements["effect-color"].value;
    selectedSpan.style.font_scale = Number(elements["effect-scale"].value);
  }
  overlay.visible = elements["overlay-visible"].checked;
  elements["font-size-output"].value = style.font_size;
  elements["asset-width-output"].value = `${style.width}%`;
  elements["overlay-max-width-output"].value = `${designCard ? position.width : style.max_width}%`;
  elements["card-height-output"].value = `${position.height ?? 24}%`;
  elements["position-x-output"].value = `${position.x}%`;
  elements["position-y-output"].value = `${position.y}%`;
  elements["effect-scale-output"].value = `${Number(elements["effect-scale"].value).toFixed(2)}×`;
  markDirty("圖層變更，儲存中…");
  if (!elements["effect-editor"].hidden) renderEffectSpanList(overlay);
  renderLayoutWarning();
}

function timelineWidth() {
  return Math.max(elements["timeline-scroll"].clientWidth, Math.ceil(timelineDuration() * 38));
}

function timelinePercent(time) {
  const bounds = timelineBounds();
  return ((Number(time) - bounds.start) / timelineDuration()) * 100;
}

function renderTimeline() {
  if (!state) return;
  const width = timelineWidth();
  elements["timeline-ruler"].style.width = `${width}px`;
  elements["timeline-tracks"].style.width = `${width}px`;
  elements["timeline-ruler"].replaceChildren();
  const rulerLabel = document.createElement("span");
  rulerLabel.className = "ruler-label";
  rulerLabel.textContent = "時間";
  elements["timeline-ruler"].append(rulerLabel);
  const bounds = timelineBounds();
  const span = timelineDuration();
  const tickStep = span > 180 ? 30 : span > 70 ? 10 : span > 25 ? 5 : 2;
  for (let second = bounds.start; second <= bounds.end + 0.001; second += tickStep) {
    const tick = document.createElement("div");
    tick.className = "ruler-tick";
    const progress = (second - bounds.start) / span;
    tick.style.left = `${84 + progress * Math.max(0, width - 84)}px`;
    const label = document.createElement("span");
    label.textContent = formatClipTime(second);
    tick.append(label);
    elements["timeline-ruler"].append(tick);
  }
  const groups = [
    { name: "影片", kind: "source" },
    { name: "動畫", types: ["image", "gif", "video", "animation"] },
    { name: "字卡", types: ["emphasis", "title", "card"] },
    { name: "字幕", types: ["caption"] },
    { name: "音訊", kind: "audio" },
  ];
  elements["timeline-tracks"].replaceChildren();
  groups.forEach((group) => {
    const track = document.createElement("div");
    track.className = "timeline-track";
    track.setAttribute("aria-label", group.name);
    const trackLabel = document.createElement("span");
    trackLabel.className = "track-label";
    trackLabel.textContent = group.name;
    const lane = document.createElement("div");
    lane.className = `track-lane lane-${group.kind || "overlay"}`;

    if (group.kind === "source") {
      const active = activeHighlight();
      const sourceSegments = active ? [active] : highlightSegments.length ? highlightSegments : [{
        id: "source-full",
        start: 0,
        end: duration(),
        title: "完整 A-roll",
      }];
      sourceSegments.forEach((segment, index) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "timeline-source-item";
        button.style.left = `${Math.max(0, timelinePercent(segment.start))}%`;
        button.style.width = `${Math.max(0.2, ((segment.end - segment.start) / span) * 100)}%`;
        button.textContent = String(index + 1).padStart(2, "0");
        button.title = `${segment.title} · ${formatClipTime(segment.start)}–${formatClipTime(segment.end)}`;
        button.addEventListener("click", () => {
          if (segment.id !== "source-full") selectHighlight(segment.id, true);
          else elements["preview-video"].currentTime = 0;
        });
        lane.append(button);
      });
      const decisionMap = new Map(decisionItems().map((item) => [item.candidate_id, item.action]));
      (projectPayload.edit_candidates?.items || []).filter((candidate) =>
        decisionMap.get(candidate.id) === "delete"
        && candidate.end > bounds.start
        && candidate.start < bounds.end
      ).forEach((candidate) => {
        const cut = document.createElement("span");
        cut.className = "cut-item";
        cut.style.left = `${Math.max(0, timelinePercent(Math.max(candidate.start, bounds.start)))}%`;
        cut.style.width = `${Math.max(0.12, ((Math.min(candidate.end, bounds.end) - Math.max(candidate.start, bounds.start)) / span) * 100)}%`;
        lane.append(cut);
      });
    } else if (group.kind === "audio") {
      const audio = document.createElement("span");
      audio.className = "timeline-audio-item";
      audio.textContent = "原始音訊";
      lane.append(audio);
    } else {
      state.overlays.filter((overlay) =>
        group.types.includes(overlay.type)
        && overlayBelongsToHighlight(overlay)
        && overlay.end > bounds.start
        && overlay.start < bounds.end
      ).forEach((overlay) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `timeline-item type-${overlay.type}${overlay.id === selectedOverlayId ? " is-selected" : ""}`;
        button.style.left = `${Math.max(0, timelinePercent(Math.max(overlay.start, bounds.start)))}%`;
        button.style.width = `${Math.max(0.15, ((Math.min(overlay.end, bounds.end) - Math.max(overlay.start, bounds.start)) / span) * 100)}%`;
        button.textContent = overlay.text || overlay.source?.split("/").pop() || layerLabel(overlay);
        button.title = `${layerLabel(overlay)} ${overlay.start.toFixed(2)}–${overlay.end.toFixed(2)} 秒`;
        button.addEventListener("click", () => selectOverlay(overlay.id, true));
        button.addEventListener("dblclick", () => {
          selectOverlay(overlay.id, true);
          if (!["image", "gif", "video"].includes(overlay.type)) {
            elements["overlay-text"].focus();
            elements["overlay-text"].select();
          }
        });
        lane.append(button);
      });
    }
    track.append(trackLabel, lane);
    elements["timeline-tracks"].append(track);
  });
  updatePlayhead();
}

function updatePlayhead() {
  const bounds = timelineBounds();
  const progress = Math.max(0, Math.min(1, (elements["preview-video"].currentTime - bounds.start) / timelineDuration()));
  const width = timelineWidth();
  elements.playhead.style.left = `${84 + progress * Math.max(0, width - 84)}px`;
}

function renderPublishing() {
  const publishing = state.publishing || (state.publishing = {});
  elements["publish-title"].value = publishing.title || "";
  elements["publish-body"].value = publishing.body || "";
  elements["publish-hashtags"].value = (publishing.hashtags || []).map((tag) => `#${String(tag).replace(/^#/, "")}`).join(" ");
  elements["cover-text"].value = publishing.cover?.text || publishing.title || "";
  elements["cover-time"].max = String(duration());
  elements["cover-time"].value = publishing.cover?.time || Math.min(1, duration());
  elements["cover-time-output"].value = `${Number(elements["cover-time"].value).toFixed(2)}s`;
  if (publishing.cover?.output) {
    elements["cover-preview"].src = `${publishing.cover.output}?v=${Date.now()}`;
    elements["cover-preview"].hidden = false;
  }
}

function voiceFamily(language) {
  return String(language || "").startsWith("zh") ? "zh" : "en";
}

function voiceMatchesLanguage(entry, language) {
  const entryLanguage = String(entry.language || "").toLowerCase();
  return entryLanguage === voiceFamily(language) || entryLanguage === String(language).toLowerCase();
}

function preferredVoiceId(language, gender, voices) {
  if (voiceFamily(language) === "zh" && gender === "female") return "rumi";
  if (voiceFamily(language) === "zh" && gender === "male") return "溫暖磁性男聲旁白";
  return voices.find((voice) =>
    voice.provider === "edge" && String(voice.language).toLowerCase() === language.toLowerCase()
  )?.voice_id || voices.find((voice) => voice.provider === "edge")?.voice_id || voices[0]?.voice_id;
}

function populateVoiceOptions(requestedId = null) {
  const language = elements["voice-language"].value || "zh-TW";
  const gender = elements["voice-gender"].value || "female";
  const voices = (projectPayload.voice_catalog?.voices || []).filter((voice) =>
    voice.gender === gender && voiceMatchesLanguage(voice, language)
  );
  elements["voice-id"].replaceChildren();
  voices.forEach((voice) => {
    const option = document.createElement("option");
    option.value = voice.voice_id;
    option.dataset.provider = voice.provider;
    option.dataset.backend = voice.backend;
    option.textContent = `${voice.voice_id} · ${voice.description}`;
    elements["voice-id"].append(option);
  });
  const preferred = requestedId && voices.some((voice) => voice.voice_id === requestedId)
    ? requestedId
    : preferredVoiceId(language, gender, voices);
  if (preferred) elements["voice-id"].value = preferred;
  updateVoiceStatus();
}

function updateVoiceStatus() {
  const enabled = elements["voice-enabled"].checked;
  ["voice-language", "voice-gender", "voice-id", "voice-mode", "voice-speed"].forEach((id) => {
    elements[id].disabled = !enabled;
  });
  if (!enabled) {
    elements["voice-status"].textContent = "原聲模式；未啟用任何配音。";
    return;
  }
  const option = elements["voice-id"].selectedOptions[0];
  if (!option) {
    elements["voice-status"].textContent = "目前語言與聲線沒有可用語音。";
    return;
  }
  const provider = option.dataset.provider === "rumi" ? "Rumi／Fish" : "Edge";
  elements["voice-status"].textContent = `${option.value} · ${provider}；只儲存選擇，產生音訊前仍會要求雲端同意。`;
}

function renderVoicePanel() {
  const voice = projectPayload.manifest?.voiceover || {};
  elements["voice-enabled"].checked = Boolean(voice.enabled);
  elements["voice-language"].value = voice.language || "zh-TW";
  elements["voice-gender"].value = voice.gender || "female";
  elements["voice-mode"].value = voice.mode === "add" ? "add" : "replace";
  elements["voice-speed"].value = String(voice.speed || 1);
  populateVoiceOptions(voice.voice_id || null);
  updateVoiceStatus();
}

async function saveVoiceSelection() {
  const button = elements["save-voice"];
  button.disabled = true;
  const option = elements["voice-id"].selectedOptions[0];
  const enabled = elements["voice-enabled"].checked;
  try {
    if (enabled && !option) throw new Error("這組語言與聲線沒有可用語音");
    const result = await request("/api/voice-selection", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled,
        language: elements["voice-language"].value,
        gender: elements["voice-gender"].value,
        provider: option?.dataset.provider || null,
        voice_id: option?.value || null,
        mode: elements["voice-mode"].value,
        speed: Number(elements["voice-speed"].value),
      }),
    });
    projectPayload.manifest.voiceover = result.voiceover;
    renderVoicePanel();
    showToast(enabled ? "配音選擇已儲存；尚未呼叫雲端" : "已切回原聲模式", "success");
  } catch (error) {
    showToast(`配音選擇儲存失敗：${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
}

function syncPublishingFromForm() {
  const publishing = state.publishing || (state.publishing = {});
  publishing.title = elements["publish-title"].value;
  publishing.body = elements["publish-body"].value;
  publishing.hashtags = elements["publish-hashtags"].value.split(/[\s,，]+/).map((tag) => tag.replace(/^#/, "").trim()).filter(Boolean);
  publishing.cover = publishing.cover || {};
  publishing.cover.text = elements["cover-text"].value;
  publishing.cover.time = Number(elements["cover-time"].value);
  elements["cover-time-output"].value = `${publishing.cover.time.toFixed(2)}s`;
  markDirty("發布草稿變更，儲存中…");
}

async function generateCopy() {
  elements["generate-copy"].disabled = true;
  try {
    const result = await request("/api/copy-draft", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ platform_id: state.canvas.platform_id }),
    });
    pushHistory();
    Object.assign(state.publishing, result.draft, { platform_id: state.canvas.platform_id });
    renderPublishing();
    markDirty("文案草稿已產生");
    showToast("已產生本機文案草稿；發布前請人工校對", "success");
  } catch (error) {
    showToast(`文案草稿失敗：${error.message}`, "error");
  } finally {
    elements["generate-copy"].disabled = false;
  }
}

async function generateCover() {
  syncPublishingFromForm();
  await saveState(false);
  elements["generate-cover"].disabled = true;
  try {
    const result = await request("/api/cover", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ platform_id: state.canvas.platform_id, time: state.publishing.cover.time, text: state.publishing.cover.text }),
    });
    state.publishing.cover.output = result.output;
    elements["cover-preview"].src = `${result.output}?v=${Date.now()}`;
    elements["cover-preview"].hidden = false;
    markDirty("封面已產生");
    showToast("封面預覽已產生", "success");
  } catch (error) {
    showToast(`封面產生失敗：${error.message}`, "error");
  } finally {
    elements["generate-cover"].disabled = false;
  }
}

async function approveTimeline() {
  await saveState(false);
  elements["approve-timeline"].disabled = true;
  try {
    if (stateDirty) throw new Error("目前時間軸尚未成功儲存");
    const result = await request("/api/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        gate: "timeline",
        expected_revision: projectPayload.approval_revisions.timeline,
        confirmed_by: "local-editor-user",
        note: "Approved live preview timeline in Auto Edit Studio",
      }),
    });
    projectPayload.manifest.approvals.timeline = result.approval;
    projectPayload.approval_revisions = result.approval_revisions;
    elements["approve-timeline"].textContent = "時間軸已核可";
    showToast("時間軸已核可，可以輸出最終影片", "success");
  } catch (error) {
    showToast(`核可失敗：${error.message}`, "error");
  } finally {
    elements["approve-timeline"].disabled = false;
  }
}

function artifactUrl(relative) {
  if (!relative) return "";
  const normalized = String(relative).replace(/^\/+/, "");
  return `/${normalized.split("/").map(encodeURIComponent).join("/")}`;
}

function isBatchDeliveryReceipt(receipt = {}) {
  return receipt?.kind === "batch"
    || receipt?.delivery_kind === "batch"
    || (Number(receipt?.schema_version || 0) >= 2 && Array.isArray(receipt?.items))
    || Array.isArray(receipt?.qa?.items);
}

function batchReceiptItems(receipt = {}) {
  if (Array.isArray(receipt?.items)) return receipt.items;
  if (Array.isArray(receipt?.qa?.items)) return receipt.qa.items;
  return [];
}

function batchReceiptFromStatus(status = {}) {
  const aggregate = status?.qa && typeof status.qa === "object" ? status.qa : {};
  const items = Array.isArray(aggregate.items) && aggregate.items.length
    ? aggregate.items
    : Array.isArray(status.items) ? status.items : [];
  return {
    ...aggregate,
    schema_version: Number(aggregate.schema_version || 2),
    kind: "batch",
    state_revision: aggregate.state_revision || status.state_revision || state?.revision || "",
    status: aggregate.status || (status.state === "complete" ? "pass" : "fail"),
    item_count: Number(aggregate.item_count || status.total_count || status.total_clips || items.length),
    items,
    archive: status.archive || aggregate.archive || status.output || "",
    archive_download_name: status.archive_download_name
      || aggregate.archive_download_name
      || status.download_name
      || "",
  };
}

function batchClipTitle(clipId, index) {
  const highlight = (state?.highlights || []).find((item) => String(item.id) === String(clipId));
  return highlight?.title || `精華片段 ${index + 1}`;
}

function renderBatchProgress(status = {}) {
  const total = Math.max(0, Number(status.total_count || status.total_clips || retainedHighlights().length || 0));
  const rawCompleted = Number(status.completed_count || status.completed_clips || 0);
  const completed = Math.max(0, Math.min(total || rawCompleted, rawCompleted));
  const currentId = status.current_clip_id;
  const currentIndex = (state?.highlights || []).findIndex((item) => String(item.id) === String(currentId));
  const currentTitle = currentId ? batchClipTitle(currentId, Math.max(0, currentIndex)) : "";
  const stateLabel = status.state === "complete"
    ? "整批輸出與 QA 已完成"
    : status.state === "qa_failed"
      ? "整批 QA 未通過"
      : status.state === "failed"
        ? "整批輸出失敗"
        : currentTitle ? `正在輸出：${currentTitle}` : "正在準備批次輸出";
  elements["batch-render-progress"].hidden = false;
  elements["batch-progress-label"].textContent = status.message || stateLabel;
  elements["batch-progress-value"].textContent = `${completed} / ${total}`;
  elements["batch-progress-bar"].max = Math.max(1, total);
  elements["batch-progress-bar"].value = completed;
}

function renderBatchQa(receipt = {}, canDownload = false) {
  const items = batchReceiptItems(receipt);
  const total = Number(receipt.item_count || items.length);
  const passed = items.filter((item) => item?.status === "pass").length;
  elements["batch-delivery-qa"].hidden = items.length === 0;
  elements["batch-qa-status"].textContent = items.length
    ? `逐段 QA：${passed} / ${total || items.length} 通過。請逐段查看九宮格與完整播放。`
    : "尚未取得逐段 QA。";
  elements["batch-qa-grid"].replaceChildren();

  items.forEach((item, index) => {
    const passedQa = item?.status === "pass";
    const card = document.createElement("article");
    card.className = `batch-qa-card${passedQa ? "" : " is-failed"}`;
    card.dataset.clipId = String(item?.clip_id || "");

    const heading = document.createElement("div");
    heading.className = "batch-qa-card-heading";
    const title = document.createElement("strong");
    title.textContent = `${String(index + 1).padStart(2, "0")} · ${batchClipTitle(item?.clip_id, index)}`;
    const chip = document.createElement("span");
    chip.className = "batch-qa-chip";
    chip.textContent = passedQa ? "QA 通過" : "QA 未通過";
    heading.append(title, chip);
    card.append(heading);

    const warnings = Array.isArray(item?.warnings) ? item.warnings : [];
    const summary = document.createElement("p");
    summary.textContent = warnings.length
      ? `${warnings.length} 項提醒；核可前請完整播放。`
      : "機械檢查完成；核可前仍需人工觀看。";
    card.append(summary);

    const links = document.createElement("div");
    links.className = "batch-qa-links";
    const contactSheet = item?.contact_sheet || item?.qa_contact;
    const report = item?.report || item?.qa_report;
    if (contactSheet) {
      const contactLink = document.createElement("a");
      contactLink.href = artifactUrl(contactSheet);
      contactLink.target = "_blank";
      contactLink.rel = "noopener";
      contactLink.textContent = "查看 QA 九宮格";
      links.append(contactLink);
    }
    if (report) {
      const reportLink = document.createElement("a");
      reportLink.href = artifactUrl(report);
      reportLink.target = "_blank";
      reportLink.rel = "noopener";
      reportLink.textContent = "查看 QA 報告";
      links.append(reportLink);
    }
    if (item?.output) {
      const playbackButton = document.createElement("button");
      playbackButton.type = "button";
      playbackButton.className = "batch-playback-button";
      playbackButton.textContent = "在主預覽播放";
      playbackButton.addEventListener("click", () => {
        const video = elements["preview-video"];
        video.src = `${artifactUrl(item.output)}?v=${Date.now()}`;
        video.load();
        showingRenderedMedia = true;
        elements["overlay-layer"].hidden = true;
        video.play().catch(() => {});
      });
      links.append(playbackButton);
    }
    if (links.childElementCount) card.append(links);
    elements["batch-qa-grid"].append(card);
  });

  elements["batch-downloads"].hidden = !canDownload;
  elements["batch-output-list"].replaceChildren();
  const archive = receipt.archive;
  elements["download-batch-archive"].hidden = !canDownload || !archive;
  if (canDownload && archive) {
    elements["download-batch-archive"].href = artifactUrl(archive);
    elements["download-batch-archive"].download = receipt.archive_download_name
      || String(archive).split("/").pop()
      || "auto-edit-highlights.zip";
  } else {
    elements["download-batch-archive"].href = "#";
    elements["download-batch-archive"].removeAttribute("download");
  }
  if (canDownload) {
    items.forEach((item, index) => {
      if (!item?.output) return;
      const link = document.createElement("a");
      link.href = artifactUrl(item.output);
      link.download = item.download_name || String(item.output).split("/").pop() || `highlight-${index + 1}.mp4`;
      link.textContent = `下載 ${String(index + 1).padStart(2, "0")} · ${batchClipTitle(item.clip_id, index)} MP4`;
      elements["batch-output-list"].append(link);
    });
  }
}

function renderDeliveryQa(receipt = projectPayload?.delivery_qa || {}) {
  projectPayload.delivery_qa = receipt || {};
  const isBatch = isBatchDeliveryReceipt(receipt);
  const hasReceipt = receipt && receipt.status === "pass";
  const isCurrent = hasReceipt && receipt.state_revision === state?.revision;
  const approval = projectPayload?.manifest?.approvals?.final || {};
  const isApproved = Boolean(
    approval.approved
    && approval.state_revision
    && approval.state_revision === projectPayload?.approval_revisions?.final
    && projectPayload?.approval_current?.final !== false
  );
  const warnings = Array.isArray(receipt?.warnings) ? receipt.warnings : [];
  const visual = receipt?.visual_quality || {};
  const visualPassed = visual.contract_applies !== true || visual.status === "pass";
  const visualSummary = visual.contract_applies === true
    ? `視覺契約：${visual.designed_card_count || 0} 張圖卡、覆蓋 ${Math.round((visual.designed_coverage_ratio || 0) * 100)}%`
    : "";

  elements["qa-contact-link"].hidden = isBatch || !hasReceipt || !receipt.contact_sheet;
  if (!elements["qa-contact-link"].hidden) {
    elements["qa-contact-link"].href = artifactUrl(receipt.contact_sheet);
  }
  elements["approve-final"].disabled = renderBusy || !isCurrent || isApproved;
  elements["approve-final"].textContent = isApproved
    ? isBatch ? "整批成片已核可" : "最終成片已核可"
    : isBatch ? "逐段檢查完成，核可整批成片" : "檢查完成，核可最終成片";

  if (isBatch) {
    const items = batchReceiptItems(receipt);
    const total = Number(receipt.item_count || items.length);
    const passed = items.filter((item) => item?.status === "pass").length;
    renderBatchProgress({
      state: hasReceipt ? "complete" : "qa_failed",
      completed_count: items.length,
      total_count: total,
      message: hasReceipt ? "整批輸出與 QA 已完成" : "整批 QA 未通過",
    });
    renderBatchQa(receipt, Boolean(isApproved && isCurrent && hasReceipt));
    if (!hasReceipt) {
      elements["delivery-qa-status"].textContent = items.length
        ? `整批 QA 未通過：${passed} / ${total || items.length} 段通過，不能核可或下載。`
        : "尚未執行整批最終輸出 QA。";
    } else if (!isCurrent) {
      elements["delivery-qa-status"].textContent = "整批 QA 對應舊時間軸，請重新輸出。";
    } else if (isApproved) {
      elements["delivery-qa-status"].textContent = `整批 ${total || items.length} 段的媒體 QA 與人工檢查均已通過。`;
    } else {
      elements["delivery-qa-status"].textContent = `整批 QA 通過 ${passed} / ${total || items.length} 段；請逐段查看九宮格與完整播放後再核可。`;
    }
    if (elements["download-output"].dataset.quality === "final") {
      elements["download-output"].hidden = true;
    }
    return;
  }

  elements["batch-delivery-qa"].hidden = true;
  elements["batch-downloads"].hidden = true;
  elements["batch-qa-grid"].replaceChildren();

  if (!hasReceipt) {
    elements["delivery-qa-status"].textContent = "尚未執行最終輸出 QA。";
  } else if (!isCurrent) {
    elements["delivery-qa-status"].textContent = "QA 成片對應舊時間軸，請重新輸出。";
  } else if (isApproved) {
    elements["delivery-qa-status"].textContent = `媒體 QA、視覺契約與人工檢查均已通過。${visualSummary ? ` ${visualSummary}。` : ""}`;
  } else if (!visualPassed) {
    elements["delivery-qa-status"].textContent = "媒體檔可播放，但視覺契約未通過，不能核可成片。";
  } else {
    elements["delivery-qa-status"].textContent = warnings.length
      ? `媒體 QA 與視覺契約通過；${visualSummary}。另有 ${warnings.length} 項提醒，請查看九宮格與完整播放。`
      : `媒體 QA 與視覺契約通過；${visualSummary || "請查看九宮格"}，完整播放後再核可。`;
  }

  if (isApproved && receipt.output) {
    const outputUrl = artifactUrl(receipt.output);
    elements["download-output"].href = outputUrl;
    elements["download-output"].download = String(receipt.output).split("/").pop() || "auto-edit-final.mp4";
    elements["download-output"].dataset.quality = "final";
    elements["download-output"].hidden = false;
  } else if (elements["download-output"].dataset.quality === "final") {
    elements["download-output"].hidden = true;
  }
}

async function approveFinal() {
  await saveState(false);
  elements["approve-final"].disabled = true;
  try {
    if (stateDirty) throw new Error("目前時間軸尚未成功儲存");
    const receipt = projectPayload.delivery_qa || {};
    if (receipt.status !== "pass" || receipt.state_revision !== state.revision) {
      throw new Error("請先完成目前版本的最終輸出與 QA");
    }
    const isBatch = isBatchDeliveryReceipt(receipt);
    const result = await request("/api/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        gate: "final",
        expected_revision: projectPayload.approval_revisions.final,
        confirmed_by: "local-editor-user",
        note: isBatch
          ? "Reviewed every batch clip playback and QA contact sheet in Auto Edit Studio"
          : "Reviewed final playback and QA contact sheet in Auto Edit Studio",
      }),
    });
    projectPayload.manifest.approvals.final = result.approval;
    projectPayload.approval_revisions = result.approval_revisions;
    projectPayload.approval_current = result.approval_current || projectPayload.approval_current || {};
    renderDeliveryQa();
    showToast(isBatch ? "整批成片已核可，可以下載 ZIP 與各段 MP4" : "最終成片已核可，可以下載", "success");
  } catch (error) {
    showToast(`最終核可失敗：${error.message}`, "error");
    renderDeliveryQa();
  }
}

async function startBatchRender() {
  setRenderBusy(true);
  try {
    await saveState(false);
    if (stateDirty) throw new Error("目前時間軸尚未成功儲存");
    const count = retainedHighlights().length;
    if (!count) throw new Error("請先保留至少一段精華");
    const result = await request("/api/render-batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        quality: "final",
        expected_revision: state.revision,
      }),
    });
    batchRenderActive = true;
    projectPayload.approval_current = projectPayload.approval_current || {};
    projectPayload.approval_current.final = false;
    renderDeliveryQa(projectPayload.delivery_qa || {});
    updateBatchRetainedCount();
    const accepted = result.status || {};
    renderBatchProgress({
      mode: "batch",
      state: accepted.state || "running",
      completed_count: accepted.completed_count || accepted.completed_clips || 0,
      total_count: accepted.total_count || accepted.total_clips || count,
      current_clip_id: accepted.current_clip_id,
      message: accepted.message || `準備輸出 ${count} 段精華`,
    });
    showToast(accepted.message || `已開始批次輸出 ${count} 段精華`);
    pollRenderStatus("batch");
  } catch (error) {
    batchRenderActive = false;
    setRenderBusy(false);
    renderDeliveryQa(projectPayload?.delivery_qa || {});
    showToast(`批次輸出未開始：${error.message}`, "error");
  }
}

async function startRender(quality = "preview") {
  setRenderBusy(true);
  try {
    await saveState(false);
    if (stateDirty) throw new Error("目前時間軸尚未成功儲存");
    const result = await request("/api/render", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        quality,
        clip_id: activeHighlight()?.id || null,
        expected_revision: state.revision,
      }),
    });
    showToast(result.status.message);
    pollRenderStatus("single");
  } catch (error) {
    setRenderBusy(false);
    renderDeliveryQa(projectPayload?.delivery_qa || {});
    showToast(`輸出未開始：${error.message}`, "error");
  }
}

function pollRenderStatus(expectedMode = "single") {
  clearInterval(renderPollTimer);
  renderPollTimer = setInterval(async () => {
    try {
      const status = await request("/api/render-status");
      const isBatch = status.mode === "batch" || expectedMode === "batch";
      if (status.state === "running") {
        if (isBatch) renderBatchProgress(status);
        setSaveState(status.message, "dirty");
        return;
      }
      clearInterval(renderPollTimer);
      setRenderBusy(false);
      renderDeliveryQa(projectPayload?.delivery_qa || {});
      if (isBatch) {
        batchRenderActive = false;
        updateBatchRetainedCount();
        renderBatchProgress(status);
        if (["complete", "qa_failed", "failed"].includes(status.state)) {
          projectPayload.delivery_qa = batchReceiptFromStatus(status);
          projectPayload.approval_revisions = status.approval_revisions || projectPayload.approval_revisions;
          if (projectPayload?.manifest?.approvals?.final) {
            projectPayload.manifest.approvals.final.approved = false;
          }
          projectPayload.approval_current = projectPayload.approval_current || {};
          projectPayload.approval_current.final = false;
          renderDeliveryQa(projectPayload.delivery_qa);
        }
        if (status.state === "complete") {
          setSaveState("整批輸出完成", "saved");
          showToast(status.message || "整批輸出與 QA 已完成", "success");
        } else if (status.state === "qa_failed") {
          setSaveState("整批 QA 未通過", "error");
          showToast(status.message || "整批 QA 未通過，請查看逐段結果", "error");
        } else {
          setSaveState("整批輸出失敗", "error");
          showToast(`整批輸出失敗：${status.message || "未知錯誤"}`, "error");
        }
        return;
      }
      if (status.state === "complete" && status.output) {
        const video = elements["preview-video"];
        const cacheBusted = `${status.output}?v=${Date.now()}`;
        video.src = cacheBusted;
        video.load();
        showingRenderedMedia = true;
        elements["overlay-layer"].hidden = true;
        if (status.quality === "final") {
          projectPayload.delivery_qa = status.qa || {};
          projectPayload.approval_revisions = status.approval_revisions || projectPayload.approval_revisions;
          if (projectPayload?.manifest?.approvals?.final) {
            projectPayload.manifest.approvals.final.approved = false;
          }
          projectPayload.approval_current = projectPayload.approval_current || {};
          projectPayload.approval_current.final = false;
          elements["download-output"].dataset.quality = "final";
          elements["download-output"].hidden = true;
          renderDeliveryQa(projectPayload.delivery_qa);
        } else {
          elements["download-output"].href = status.output;
          elements["download-output"].download = status.download_name || "auto-edit-preview.mp4";
          elements["download-output"].dataset.quality = "preview";
          elements["download-output"].hidden = false;
        }
        setSaveState("輸出完成", "saved");
        showToast(status.message, "success");
      } else if (status.state === "qa_failed") {
        const failures = status.qa?.failures || [];
        elements["delivery-qa-status"].textContent = failures.length
          ? `機械 QA 未通過：${failures.join("、")}`
          : `機械 QA 未通過：${status.message}`;
        elements["approve-final"].disabled = true;
        setSaveState("QA 未通過", "error");
        showToast(`最終輸出未取代上一版：${status.message}`, "error");
      } else if (status.state === "failed") {
        setSaveState("輸出失敗", "error");
        showToast(`輸出失敗：${status.message}`, "error");
      }
    } catch (error) {
      clearInterval(renderPollTimer);
      setRenderBusy(false);
      renderDeliveryQa(projectPayload?.delivery_qa || {});
      showToast(`無法取得輸出狀態：${error.message}`, "error");
    }
  }, 900);
}

async function uploadAsset(file) {
  if (!file) return;
  elements["asset-upload-button"].disabled = true;
  try {
    const result = await request(`/api/assets?filename=${encodeURIComponent(file.name)}`, {
      method: "POST",
      headers: { "Content-Type": file.type || "application/octet-stream" },
      body: await file.arrayBuffer(),
    });
    const suffix = file.name.split(".").pop().toLowerCase();
    const type = suffix === "gif" ? "gif" : ["mp4", "mov"].includes(suffix) ? "video" : "image";
    addOverlay(type, result.source);
    showToast("素材已加入專案並記錄來源", "success");
  } catch (error) {
    showToast(`素材加入失敗：${error.message}`, "error");
  } finally {
    elements["asset-upload-button"].disabled = false;
    elements["asset-input"].value = "";
  }
}

async function uploadTemplateBackground(file) {
  if (!file) return;
  const template = projectPayload.video_templates?.[state.video_template?.id];
  if (!template || !["image", "video"].includes(template.background_mode)) return;
  const allowed = template.background_mode === "video"
    ? ["video/mp4", "video/quicktime"]
    : ["image/png", "image/jpeg", "image/webp"];
  if (!allowed.includes(file.type)) {
    showToast(template.background_mode === "video" ? "請選擇 MP4 或 MOV" : "請選擇 PNG、JPG 或 WEBP", "error");
    elements["template-background-input"].value = "";
    return;
  }
  elements["template-background-button"].disabled = true;
  try {
    const result = await request(`/api/assets?filename=${encodeURIComponent(file.name)}`, {
      method: "POST",
      headers: { "Content-Type": file.type },
      body: await file.arrayBuffer(),
    });
    pushHistory();
    state.video_template.background.source = result.source;
    renderTemplatePicker();
    applyCanvas();
    markDirty("背景素材已加入模板");
    showToast("背景素材已加入專案並納入輸出版本", "success");
  } catch (error) {
    showToast(`背景素材加入失敗：${error.message}`, "error");
  } finally {
    elements["template-background-button"].disabled = false;
    elements["template-background-input"].value = "";
  }
}

function switchInspectorTab(tab) {
  const publish = tab === "publish";
  elements["style-tab"].setAttribute("aria-selected", String(!publish));
  elements["publish-tab"].setAttribute("aria-selected", String(publish));
  elements["style-panel"].hidden = publish;
  elements["publish-panel"].hidden = !publish;
  if (publish) renderPublishing();
}

function renderSourceWarning() {
  const report = projectPayload.qa?.report || projectPayload.qa || {};
  const warnings = [];
  const pipeline = projectPayload.pipeline_status || {};
  if (["pending", "running"].includes(pipeline.state)) warnings.push(pipeline.message || "本機自動處理中");
  if (["failed", "needs_attention", "stopped"].includes(pipeline.state)) warnings.push(pipeline.message || "本機自動處理需要處理");
  if (!Object.keys(report).length) warnings.push("來源 QA 尚未載入");
  if (report.dead_border?.border_flag || report.border_flag) warnings.push("來源有黑邊");
  if (report.loudness?.ok === false) warnings.push(`音量 ${report.loudness.lufs} LUFS`);
  if (report.scene_pacing?.ok === false) warnings.push("有鏡頭節奏警告");
  const stages = projectPayload.manifest?.stages || {};
  const hasDraftPlans = state.overlays.some((overlay) =>
    ["working/emphasis_plan.json", "working/visual_plan.json", "working/highlight_visual_plan.json"].includes(overlay.source)
  );
  if (stages.edit_review === "needs_review" || hasDraftPlans) {
    warnings.push("逐字稿與自動重點／字卡待校對");
  }
  if (projectPayload.manifest?.source?.contains_burned_captions) {
    warnings.push("這是已燒字幕的參考成片，疊加字幕僅供功能比對");
  }
  elements["warning-copy"].textContent = warnings.length
    ? `${warnings.join("、")}；核可前需處理。`
    : "來源 QA 已通過，仍需人工檢查字幕與畫面。";
}

function pollPipelineStatus() {
  clearInterval(pipelinePollTimer);
  pipelinePollTimer = setInterval(async () => {
    try {
      const status = await request("/api/pipeline-status");
      projectPayload.pipeline_status = status;
      renderSourceWarning();
      if (["pending", "running"].includes(status.state)) return;
      if (status.state === "needs_review") {
        if (stateDirty) return;
        clearInterval(pipelinePollTimer);
        window.location.reload();
        return;
      }
      clearInterval(pipelinePollTimer);
      if (["failed", "needs_attention", "stopped"].includes(status.state)) {
        showToast(status.message || "本機自動處理需要處理", "error");
      }
    } catch (error) {
      clearInterval(pipelinePollTimer);
      showToast(`無法讀取自動處理進度：${error.message}`, "error");
    }
  }, 1800);
}

function renderAll() {
  renderTemplatePicker();
  applyCanvas();
  renderCandidateList();
  renderHighlightList();
  renderLayerList();
  renderInspector();
  renderPublishing();
  renderTimeline();
  renderPreviewOverlays(true);
  renderSourceWarning();
  renderDeliveryQa();
}

function bindEvents() {
  elements["save-button"].addEventListener("click", () => saveState(true));
  elements["render-button"].addEventListener("click", () => startRender("preview"));
  elements["render-final"].addEventListener("click", () => startRender("final"));
  elements["render-batch-final"].addEventListener("click", startBatchRender);
  elements["approve-cuts"].addEventListener("click", approveCuts);
  elements["approve-highlights"].addEventListener("click", approveHighlights);
  elements["replan-highlights"].addEventListener("click", replanHighlights);
  elements["keep-highlight"].addEventListener("click", () => reviewActiveHighlight("approved"));
  elements["reject-highlight"].addEventListener("click", () => reviewActiveHighlight("rejected"));
  elements["highlight-title"].addEventListener("input", () => {
    const highlight = activeHighlight();
    if (!highlight) return;
    highlight.title = elements["highlight-title"].value;
    const segment = highlightSegments.find((item) => item.id === highlight.id);
    if (segment) segment.title = highlight.title;
    const activeTitle = elements["highlight-list"].querySelector(".highlight-row.is-active .highlight-copy strong");
    if (activeTitle) activeTitle.textContent = highlight.title;
    markDirty("片段標題變更，儲存中…");
  });
  ["highlight-title", "highlight-start", "highlight-end"].forEach((id) => {
    elements[id].addEventListener("change", updateActiveHighlightFields);
  });
  elements["approve-timeline"].addEventListener("click", approveTimeline);
  elements["approve-final"].addEventListener("click", approveFinal);
  elements["platform-select"].addEventListener("change", () => {
    pushHistory();
    const preset = projectPayload.platform_presets[elements["platform-select"].value];
    Object.assign(state.canvas, {
      platform_id: elements["platform-select"].value,
      width: preset.width,
      height: preset.height,
      fps: preset.fps,
    });
    state.publishing.platform_id = state.canvas.platform_id;
    markDirty("平台尺寸變更，儲存中…");
    applyCanvas();
    renderPublishing();
  });
  elements["template-group-tabs"].querySelectorAll("[data-template-group]").forEach((button) => {
    button.addEventListener("click", () => {
      activeTemplateGroup = button.dataset.templateGroup;
      renderTemplatePicker();
    });
  });
  elements["template-select"].addEventListener("change", () => selectVideoTemplate(elements["template-select"].value));
  ["x", "y", "width", "height"].forEach((key) => {
    elements[`template-frame-${key}`].addEventListener("input", () => {
      state.video_template.frame[key] = Number(elements[`template-frame-${key}`].value);
      elements[`template-frame-${key}-output`].textContent = `${Math.round(state.video_template.frame[key])}%`;
      applyCanvas();
      markDirty("影片框位置已變更");
    });
  });
  ["x", "y"].forEach((key) => {
    elements[`template-subject-${key}`].addEventListener("input", () => {
      state.video_template.subject[key] = Number(elements[`template-subject-${key}`].value);
      elements[`template-subject-${key}-output`].textContent = `${Math.round(state.video_template.subject[key])}%`;
      applyCanvas();
      markDirty("人物位置已變更");
    });
  });
  elements["template-subject-scale"].addEventListener("input", () => {
    state.video_template.subject.scale = Number(elements["template-subject-scale"].value);
    elements["template-subject-scale-output"].textContent = `${Math.round(state.video_template.subject.scale * 100)}%`;
    applyCanvas();
    markDirty("人物大小已變更");
  });
  elements["template-background-color"].addEventListener("input", () => {
    state.video_template.background.color = elements["template-background-color"].value;
    applyCanvas();
    markDirty("去背背景顏色已變更");
  });
  elements["template-background-fit"].addEventListener("change", () => {
    state.video_template.background.fit = elements["template-background-fit"].value;
    applyCanvas();
    markDirty("背景填滿方式已變更");
  });
  elements["template-background-button"].addEventListener("click", () => elements["template-background-input"].click());
  elements["template-background-input"].addEventListener("change", () => uploadTemplateBackground(elements["template-background-input"].files[0]));
  elements["director-select"].addEventListener("change", () => {
    pushHistory();
    const id = elements["director-select"].value;
    const preset = projectPayload.director_presets[id];
    state.director_style = id;
    state.caption_defaults = deepCopy(preset.caption);
    state.overlays.filter((overlay) => ["caption", "emphasis", "title", "card", "animation"].includes(overlay.type)).forEach((overlay) => {
      overlay.style = { ...overlay.style, ...deepCopy(preset.caption) };
      if (["title", "card", "animation"].includes(overlay.type)) overlay.style.box = true;
    });
    markDirty("剪輯導演風格已套用");
    renderDirectorCards();
    renderInspector();
    showToast(`已套用「${preset.label}」到文字圖層`, "success");
  });
  document.querySelectorAll("[data-add-type]").forEach((button) => button.addEventListener("click", () => addOverlay(button.dataset.addType)));
  elements["asset-upload-button"].addEventListener("click", () => elements["asset-input"].click());
  elements["asset-input"].addEventListener("change", () => uploadAsset(elements["asset-input"].files[0]));
  elements["delete-layer"].addEventListener("click", deleteSelectedOverlay);
  elements["layer-form"].addEventListener("input", updateOverlayFromForm);
  elements["add-effect-span"].addEventListener("click", addEffectSpan);
  ["select", "mouseup", "keyup"].forEach((eventName) => {
    elements["overlay-text"].addEventListener(eventName, prepareEffectCreation);
  });
  elements["style-tab"].addEventListener("click", () => switchInspectorTab("style"));
  elements["publish-tab"].addEventListener("click", () => switchInspectorTab("publish"));
  elements["generate-copy"].addEventListener("click", generateCopy);
  elements["generate-cover"].addEventListener("click", generateCover);
  elements["save-voice"].addEventListener("click", saveVoiceSelection);
  elements["voice-enabled"].addEventListener("change", updateVoiceStatus);
  ["voice-language", "voice-gender"].forEach((id) => elements[id].addEventListener("change", () => populateVoiceOptions()));
  elements["voice-id"].addEventListener("change", updateVoiceStatus);
  ["publish-title", "publish-body", "publish-hashtags", "cover-text", "cover-time"].forEach((id) => elements[id].addEventListener("input", syncPublishingFromForm));
  elements["jump-start"].addEventListener("click", () => {
    elements["preview-video"].currentTime = timelineBounds().start;
  });
  elements["play-button"].addEventListener("click", () => {
    const video = elements["preview-video"];
    const bounds = timelineBounds();
    if (!showingRenderedMedia && (video.currentTime < bounds.start || video.currentTime >= bounds.end)) {
      video.currentTime = bounds.start;
    }
    if (video.paused) video.play(); else video.pause();
  });
  elements["toggle-safe-zone"].addEventListener("click", () => {
    state.canvas.show_safe_zones = !state.canvas.show_safe_zones;
    elements["safe-zone"].hidden = !state.canvas.show_safe_zones;
    elements["toggle-safe-zone"].setAttribute("aria-pressed", String(state.canvas.show_safe_zones));
    markDirty("安全框設定變更");
  });
  elements.scrubber.addEventListener("input", () => { elements["preview-video"].currentTime = Number(elements.scrubber.value); });
  elements["preview-video"].addEventListener("loadedmetadata", () => {
    updateScrubberBounds();
    elements["cover-time"].max = String(duration());
    elements["total-time"].textContent = formatTime(duration());
    elements["stage-empty"].hidden = true;
    renderTimeline();
  });
  elements["preview-video"].addEventListener("timeupdate", () => {
    const current = elements["preview-video"].currentTime;
    const active = activeHighlight();
    if (!showingRenderedMedia && active && current >= Number(active.end)) {
      elements["preview-video"].pause();
      if (Math.abs(current - Number(active.end)) > 0.02) {
        elements["preview-video"].currentTime = Number(active.end);
      }
    }
    elements["current-time"].textContent = formatTime(current);
    elements.scrubber.value = String(current);
    updatePlayhead();
    updateActiveHighlight();
    renderPreviewOverlays(false);
  });
  elements["preview-video"].addEventListener("play", () => {
    elements["play-button"].querySelector("span:first-child").textContent = "Ⅱ";
    elements["play-button"].setAttribute("aria-label", "暫停影片");
  });
  elements["preview-video"].addEventListener("pause", () => {
    elements["play-button"].querySelector("span:first-child").textContent = "▶";
    elements["play-button"].setAttribute("aria-label", "播放影片");
  });
  elements["candidate-list"].addEventListener("change", () => renderTimeline());
  elements["source-preview-button"].addEventListener("click", () => {
    state.active_highlight_id = null;
    markDirty("已切換為完整來源預覽");
    renderHighlightList();
    updateScrubberBounds();
    elements["preview-video"].currentTime = 0;
    elements["preview-video"].play();
  });
  elements["editing-brief"].addEventListener("input", () => {
    state.editing_brief = elements["editing-brief"].value;
    markDirty("剪輯意圖變更，儲存中…");
  });
  window.addEventListener("resize", () => { renderPreviewOverlays(true); renderTimeline(); });
  window.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      saveState(true);
    }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "z") {
      event.preventDefault();
      undo();
    }
    if (event.code === "Space" && !["INPUT", "TEXTAREA", "SELECT", "BUTTON"].includes(document.activeElement.tagName)) {
      event.preventDefault();
      if (elements["preview-video"].paused) elements["preview-video"].play(); else elements["preview-video"].pause();
    }
  });
}

async function initialize() {
  cacheElements();
  bindEvents();
  try {
    projectPayload = await request("/api/project");
    state = projectPayload.state;
    selectedOverlayId = state.review?.selected_overlay_id || state.overlays[0]?.id || null;
    sourceMediaUrl = projectPayload.media_url;
    elements["project-name"].textContent = projectPayload.manifest.project_id || "未命名專案";
    const source = projectPayload.manifest.source || {};
    elements["source-meta"].textContent = `${source.width || "?"}×${source.height || "?"} · ${source.fps || "?"}fps · ${Number(source.duration_s || 0).toFixed(1)}s`;
    const stagedName = String(source.staged_path || source.original_path || "source video").split("/").pop();
    elements["source-file-name"].textContent = stagedName;
    elements["source-file-detail"].textContent = `${source.width || "?"}×${source.height || "?"} · ${source.fps || "?"}fps · ${formatClipTime(Number(source.duration_s || 0))}`;
    renderTranscriptStatus();
    elements["editing-brief"].value = state.editing_brief || "";
    populatePresets();
    if (sourceMediaUrl) {
      elements["preview-video"].src = sourceMediaUrl;
      elements["preview-video"].load();
    }
    if (projectPayload.manifest.approvals?.destructive_edit?.approved) elements["approve-cuts"].textContent = "刪除決定已核可";
    if (projectPayload.manifest.approvals?.highlight_selection?.approved) elements["approve-highlights"].textContent = "精華選段已核可";
    if (projectPayload.manifest.approvals?.timeline?.approved) elements["approve-timeline"].textContent = "時間軸已核可";
    setSaveState("已載入", "saved");
    renderAll();
    updateScrubberBounds();
    renderVoicePanel();
    if (projectPayload.render_status?.state === "running") {
      const isBatch = projectPayload.render_status.mode === "batch";
      if (isBatch) {
        batchRenderActive = true;
        renderBatchProgress(projectPayload.render_status);
      }
      setRenderBusy(true);
      pollRenderStatus(isBatch ? "batch" : "single");
    }
    if (["pending", "running"].includes(projectPayload.pipeline_status?.state)) pollPipelineStatus();
  } catch (error) {
    elements["stage-empty"].innerHTML = "";
    const message = document.createElement("p");
    message.textContent = `專案載入失敗：${error.message}`;
    elements["stage-empty"].append(message);
    setSaveState("載入失敗", "error");
    showToast(`專案載入失敗：${error.message}`, "error");
  }
}

initialize();
