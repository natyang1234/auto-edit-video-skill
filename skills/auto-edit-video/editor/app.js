const elements = {};
let projectPayload = null;
let state = null;
let selectedOverlayId = null;
let saveTimer = null;
let toastTimer = null;
let renderPollTimer = null;
let history = [];
let sourceMediaUrl = null;
let showingRenderedMedia = false;
let lastOverlaySignature = "";

const byId = (id) => document.getElementById(id);
const deepCopy = (value) => JSON.parse(JSON.stringify(value));

function cacheElements() {
  [
    "project-name", "platform-select", "director-select", "save-state", "save-button",
    "render-button", "candidate-count", "candidate-list", "approve-cuts", "layer-list",
    "asset-upload-button", "asset-input", "canvas-resolution", "source-meta", "stage-frame",
    "preview-video", "overlay-layer", "safe-zone", "stage-empty", "warning-copy", "jump-start",
    "play-button", "current-time", "total-time", "scrubber", "toggle-safe-zone", "style-tab",
    "publish-tab", "style-panel", "publish-panel", "inspector-empty", "layer-form", "selected-type",
    "selected-name", "delete-layer", "text-field", "overlay-text", "overlay-start", "overlay-end",
    "font-family", "font-size", "font-size-output", "asset-width-row", "asset-width",
    "asset-width-output", "font-color", "emphasis-color", "position-x", "position-x-output",
    "position-y", "position-y-output", "overlay-animation", "overlay-visible", "generate-copy",
    "publish-title", "publish-body", "publish-hashtags", "cover-text", "cover-time",
    "cover-time-output", "generate-cover", "cover-preview", "approve-timeline", "render-final",
    "voice-enabled", "voice-language", "voice-gender", "voice-id", "voice-mode",
    "voice-speed", "save-voice", "voice-status",
    "timeline-scroll", "timeline-ruler", "timeline-tracks", "playhead", "toast"
  ].forEach((id) => { elements[id] = byId(id); });
}

async function request(url, options = {}) {
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

function formatTime(seconds) {
  const safe = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
  const minutes = Math.floor(safe / 60);
  const remainder = safe - minutes * 60;
  return `${String(minutes).padStart(2, "0")}:${remainder.toFixed(3).padStart(6, "0")}`;
}

function duration() {
  return Number(projectPayload?.manifest?.source?.duration_s || elements["preview-video"].duration || 0);
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
  try {
    const payload = await request("/api/editor-state", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state),
    });
    state.updated_at = payload.updated_at;
    state.revision = payload.revision;
    if ((payload.invalidated_gates || []).includes("timeline")) {
      if (projectPayload?.manifest?.approvals?.timeline) {
        projectPayload.manifest.approvals.timeline.approved = false;
      }
      elements["approve-timeline"].textContent = "核可時間軸";
      showToast("畫面內容已變更，時間軸核可已失效", "info");
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
  elements["platform-select"].value = state.canvas.platform_id;
  elements["director-select"].value = state.director_style;
}

function applyCanvas() {
  const preset = projectPayload.platform_presets[state.canvas.platform_id];
  const ratio = `${state.canvas.width} / ${state.canvas.height}`;
  elements["stage-frame"].style.setProperty("--canvas-ratio", ratio);
  elements["preview-video"].style.objectFit = state.canvas.fit === "contain" ? "contain" : "cover";
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

function decisionItems() {
  return (projectPayload.edit_candidates?.items || []).map((candidate) => {
    const checkbox = elements["candidate-list"].querySelector(`[data-candidate-id="${candidate.id}"]`);
    return { candidate_id: candidate.id, action: checkbox?.checked ? "delete" : "keep", review_status: "pending" };
  });
}

async function approveCuts() {
  elements["approve-cuts"].disabled = true;
  try {
    await request("/api/edit-decisions", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: decisionItems(), approved: true }),
    });
    const result = await request("/api/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gate: "destructive_edit", confirmed_by: "local-editor-user", note: "Reviewed edit decisions in Auto Edit Studio" }),
    });
    projectPayload.manifest.approvals.destructive_edit = result.approval;
    elements["approve-cuts"].textContent = "刪除決定已核可";
    showToast("刪除決定已核可；尚未執行實際裁切", "success");
  } catch (error) {
    showToast(`核可失敗：${error.message}`, "error");
  } finally {
    elements["approve-cuts"].disabled = false;
  }
}

function layerLabel(overlay) {
  const typeLabels = { caption: "字幕", emphasis: "特效字", title: "標題卡", card: "字卡", image: "圖片", gif: "GIF", video: "插入影片", animation: "動畫字卡" };
  return typeLabels[overlay.type] || overlay.type;
}

function renderLayerList() {
  elements["layer-list"].replaceChildren();
  [...state.overlays]
    .sort((a, b) => a.start - b.start || (a.z_index || 0) - (b.z_index || 0))
    .forEach((overlay) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `layer-row${overlay.id === selectedOverlayId ? " is-selected" : ""}`;
      const title = document.createElement("strong");
      title.textContent = `${layerLabel(overlay)} · ${overlay.text || overlay.source?.split("/").pop() || "未命名"}`;
      const timing = document.createElement("span");
      timing.textContent = `${overlay.start.toFixed(2)}–${overlay.end.toFixed(2)}s${overlay.visible === false ? " · 已隱藏" : ""}`;
      button.append(title, timing);
      button.addEventListener("click", () => selectOverlay(overlay.id, true));
      elements["layer-list"].append(button);
    });
}

function createTextWithEmphasis(overlay) {
  const fragment = document.createDocumentFragment();
  const text = String(overlay.text || "");
  const phrases = overlay.type === "emphasis" ? [text] : (overlay.emphasis || []).filter(Boolean);
  if (!phrases.length) {
    fragment.append(document.createTextNode(text));
    return fragment;
  }
  let cursor = 0;
  phrases.forEach((phrase) => {
    const index = text.indexOf(phrase, cursor);
    if (index < 0) return;
    fragment.append(document.createTextNode(text.slice(cursor, index)));
    const mark = document.createElement("mark");
    mark.textContent = phrase;
    fragment.append(mark);
    cursor = index + phrase.length;
  });
  fragment.append(document.createTextNode(text.slice(cursor)));
  return fragment;
}

function renderPreviewOverlays(force = false) {
  if (!state || showingRenderedMedia) return;
  const time = elements["preview-video"].currentTime || 0;
  const active = state.overlays.filter((overlay) => overlay.visible !== false && time >= overlay.start && time < overlay.end);
  const signature = JSON.stringify(active.map((overlay) => [overlay.id, overlay.text, overlay.source, overlay.style, overlay.emphasis, selectedOverlayId]));
  if (!force && signature === lastOverlaySignature) return;
  lastOverlaySignature = signature;
  elements["overlay-layer"].replaceChildren();
  active.sort((a, b) => (a.z_index || 0) - (b.z_index || 0)).forEach((overlay) => {
    const style = overlay.style || {};
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
      node.append(createTextWithEmphasis(overlay));
    }
    node.className = `preview-overlay type-${overlay.type} motion-${style.animation || "none"}`;
    node.style.left = `${style.x ?? 50}%`;
    node.style.top = `${style.y ?? 76}%`;
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
    if (overlay.id === selectedOverlayId) node.style.outline = "2px solid var(--vermilion)";
    elements["overlay-layer"].append(node);
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
  const style = overlay.style || (overlay.style = deepCopy(state.caption_defaults));
  elements["inspector-empty"].hidden = true;
  elements["layer-form"].hidden = false;
  elements["selected-type"].textContent = overlay.type.toUpperCase();
  elements["selected-name"].textContent = layerLabel(overlay);
  elements["text-field"].hidden = assetType;
  elements["overlay-text"].value = overlay.text || "";
  elements["overlay-start"].value = overlay.start;
  elements["overlay-end"].value = overlay.end;
  elements["font-family"].value = style.font_family || "PingFang TC";
  elements["font-size"].value = style.font_size || 58;
  elements["font-size-output"].value = style.font_size || 58;
  elements["font-size"].closest("label").hidden = assetType;
  elements["asset-width-row"].hidden = !assetType;
  elements["asset-width"].value = style.width || 32;
  elements["asset-width-output"].value = `${style.width || 32}%`;
  elements["font-color"].value = style.color || "#f7f2e8";
  elements["emphasis-color"].value = style.emphasis_color || "#ffd447";
  elements["font-color"].closest(".field-pair").hidden = assetType;
  elements["position-x"].value = style.x ?? 50;
  elements["position-x-output"].value = `${style.x ?? 50}%`;
  elements["position-y"].value = style.y ?? 76;
  elements["position-y-output"].value = `${style.y ?? 76}%`;
  elements["overlay-animation"].value = style.animation || "none";
  elements["overlay-visible"].checked = overlay.visible !== false;
}

function selectOverlay(id, seek = false) {
  selectedOverlayId = id;
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
  overlay.text = elements["overlay-text"].value;
  overlay.start = Math.max(0, Number(elements["overlay-start"].value) || 0);
  overlay.end = Math.min(duration(), Math.max(overlay.start + 0.01, Number(elements["overlay-end"].value) || overlay.start + 0.01));
  style.font_family = elements["font-family"].value;
  style.font_size = Number(elements["font-size"].value);
  style.width = Number(elements["asset-width"].value);
  style.color = elements["font-color"].value;
  style.emphasis_color = elements["emphasis-color"].value;
  style.x = Number(elements["position-x"].value);
  style.y = Number(elements["position-y"].value);
  style.animation = elements["overlay-animation"].value;
  overlay.visible = elements["overlay-visible"].checked;
  elements["font-size-output"].value = style.font_size;
  elements["asset-width-output"].value = `${style.width}%`;
  elements["position-x-output"].value = `${style.x}%`;
  elements["position-y-output"].value = `${style.y}%`;
  markDirty("圖層變更，儲存中…");
}

function timelineWidth() {
  return Math.max(elements["timeline-scroll"].clientWidth, Math.ceil(duration() * 38));
}

function renderTimeline() {
  if (!state) return;
  const width = timelineWidth();
  elements["timeline-ruler"].style.width = `${width}px`;
  elements["timeline-tracks"].style.width = `${width}px`;
  elements["timeline-ruler"].replaceChildren();
  const tickStep = duration() > 180 ? 30 : duration() > 70 ? 10 : 5;
  for (let second = 0; second <= duration(); second += tickStep) {
    const tick = document.createElement("div");
    tick.className = "ruler-tick";
    tick.style.left = `${(second / Math.max(duration(), 1)) * 100}%`;
    const label = document.createElement("span");
    label.textContent = `${second}s`;
    tick.append(label);
    elements["timeline-ruler"].append(tick);
  }
  const groups = [
    { name: "字幕", types: ["caption"] },
    { name: "字卡與特效", types: ["emphasis", "title", "card"] },
    { name: "素材與動畫", types: ["image", "gif", "video", "animation"] },
  ];
  elements["timeline-tracks"].replaceChildren();
  groups.forEach((group, groupIndex) => {
    const track = document.createElement("div");
    track.className = "timeline-track";
    track.setAttribute("aria-label", group.name);
    state.overlays.filter((overlay) => group.types.includes(overlay.type)).forEach((overlay) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `timeline-item type-${overlay.type}${overlay.id === selectedOverlayId ? " is-selected" : ""}`;
      button.style.left = `${(overlay.start / Math.max(duration(), 1)) * 100}%`;
      button.style.width = `${Math.max(0.15, ((overlay.end - overlay.start) / Math.max(duration(), 1)) * 100)}%`;
      button.textContent = overlay.text || overlay.source?.split("/").pop() || layerLabel(overlay);
      button.title = `${layerLabel(overlay)} ${overlay.start.toFixed(2)}–${overlay.end.toFixed(2)} 秒`;
      button.addEventListener("click", () => selectOverlay(overlay.id, true));
      track.append(button);
    });
    if (groupIndex === 0) {
      const decisionMap = new Map(decisionItems().map((item) => [item.candidate_id, item.action]));
      (projectPayload.edit_candidates?.items || []).filter((candidate) => decisionMap.get(candidate.id) === "delete").forEach((candidate) => {
        const cut = document.createElement("span");
        cut.className = "cut-item";
        cut.style.left = `${(candidate.start / Math.max(duration(), 1)) * 100}%`;
        cut.style.width = `${Math.max(0.12, ((candidate.end - candidate.start) / Math.max(duration(), 1)) * 100)}%`;
        track.append(cut);
      });
    }
    elements["timeline-tracks"].append(track);
  });
  updatePlayhead();
}

function updatePlayhead() {
  const percent = (elements["preview-video"].currentTime / Math.max(duration(), 1)) * 100;
  elements.playhead.style.left = `${Math.max(0, Math.min(100, percent))}%`;
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
    const result = await request("/api/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gate: "timeline", confirmed_by: "local-editor-user", note: "Approved live preview timeline in Auto Edit Studio" }),
    });
    projectPayload.manifest.approvals.timeline = result.approval;
    elements["approve-timeline"].textContent = "時間軸已核可";
    showToast("時間軸已核可，可以輸出最終影片", "success");
  } catch (error) {
    showToast(`核可失敗：${error.message}`, "error");
  } finally {
    elements["approve-timeline"].disabled = false;
  }
}

async function startRender(quality = "preview") {
  await saveState(false);
  const button = quality === "preview" ? elements["render-button"] : elements["render-final"];
  button.disabled = true;
  try {
    const result = await request("/api/render", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ quality }),
    });
    showToast(result.status.message);
    pollRenderStatus(button);
  } catch (error) {
    button.disabled = false;
    showToast(`輸出未開始：${error.message}`, "error");
  }
}

function pollRenderStatus(button) {
  clearInterval(renderPollTimer);
  renderPollTimer = setInterval(async () => {
    try {
      const status = await request("/api/render-status");
      if (status.state === "running") {
        setSaveState(status.message, "dirty");
        return;
      }
      clearInterval(renderPollTimer);
      button.disabled = false;
      if (status.state === "complete" && status.output) {
        const video = elements["preview-video"];
        const cacheBusted = `${status.output}?v=${Date.now()}`;
        video.src = cacheBusted;
        video.load();
        showingRenderedMedia = true;
        elements["overlay-layer"].hidden = true;
        setSaveState("輸出完成", "saved");
        showToast(status.message, "success");
      } else if (status.state === "failed") {
        setSaveState("輸出失敗", "error");
        showToast(`輸出失敗：${status.message}`, "error");
      }
    } catch (error) {
      clearInterval(renderPollTimer);
      button.disabled = false;
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
  if (!Object.keys(report).length) warnings.push("來源 QA 尚未載入");
  if (report.dead_border?.border_flag || report.border_flag) warnings.push("來源有黑邊");
  if (report.loudness?.ok === false) warnings.push(`音量 ${report.loudness.lufs} LUFS`);
  if (report.scene_pacing?.ok === false) warnings.push("有鏡頭節奏警告");
  const stages = projectPayload.manifest?.stages || {};
  const hasDraftPlans = state.overlays.some((overlay) =>
    ["working/emphasis_plan.json", "working/visual_plan.json"].includes(overlay.source)
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

function renderAll() {
  applyCanvas();
  renderCandidateList();
  renderLayerList();
  renderInspector();
  renderPublishing();
  renderTimeline();
  renderPreviewOverlays(true);
  renderSourceWarning();
}

function bindEvents() {
  elements["save-button"].addEventListener("click", () => saveState(true));
  elements["render-button"].addEventListener("click", () => startRender("preview"));
  elements["render-final"].addEventListener("click", () => startRender("final"));
  elements["approve-cuts"].addEventListener("click", approveCuts);
  elements["approve-timeline"].addEventListener("click", approveTimeline);
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
    renderInspector();
    showToast(`已套用「${preset.label}」到文字圖層`, "success");
  });
  document.querySelectorAll("[data-add-type]").forEach((button) => button.addEventListener("click", () => addOverlay(button.dataset.addType)));
  elements["asset-upload-button"].addEventListener("click", () => elements["asset-input"].click());
  elements["asset-input"].addEventListener("change", () => uploadAsset(elements["asset-input"].files[0]));
  elements["delete-layer"].addEventListener("click", deleteSelectedOverlay);
  elements["layer-form"].addEventListener("input", updateOverlayFromForm);
  elements["style-tab"].addEventListener("click", () => switchInspectorTab("style"));
  elements["publish-tab"].addEventListener("click", () => switchInspectorTab("publish"));
  elements["generate-copy"].addEventListener("click", generateCopy);
  elements["generate-cover"].addEventListener("click", generateCover);
  elements["save-voice"].addEventListener("click", saveVoiceSelection);
  elements["voice-enabled"].addEventListener("change", updateVoiceStatus);
  ["voice-language", "voice-gender"].forEach((id) => elements[id].addEventListener("change", () => populateVoiceOptions()));
  elements["voice-id"].addEventListener("change", updateVoiceStatus);
  ["publish-title", "publish-body", "publish-hashtags", "cover-text", "cover-time"].forEach((id) => elements[id].addEventListener("input", syncPublishingFromForm));
  elements["jump-start"].addEventListener("click", () => { elements["preview-video"].currentTime = 0; });
  elements["play-button"].addEventListener("click", () => {
    const video = elements["preview-video"];
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
    elements.scrubber.max = String(duration());
    elements["cover-time"].max = String(duration());
    elements["total-time"].textContent = formatTime(duration());
    elements["stage-empty"].hidden = true;
    renderTimeline();
  });
  elements["preview-video"].addEventListener("timeupdate", () => {
    const current = elements["preview-video"].currentTime;
    elements["current-time"].textContent = formatTime(current);
    elements.scrubber.value = String(current);
    updatePlayhead();
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
    populatePresets();
    if (sourceMediaUrl) {
      elements["preview-video"].src = sourceMediaUrl;
      elements["preview-video"].load();
    }
    if (projectPayload.manifest.approvals?.destructive_edit?.approved) elements["approve-cuts"].textContent = "刪除決定已核可";
    if (projectPayload.manifest.approvals?.timeline?.approved) elements["approve-timeline"].textContent = "時間軸已核可";
    setSaveState("已載入", "saved");
    renderAll();
    renderVoicePanel();
    if (projectPayload.render_status?.state === "running") pollRenderStatus(elements["render-button"]);
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
