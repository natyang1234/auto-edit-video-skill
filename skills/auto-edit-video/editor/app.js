const elements = {};
let projectPayload = null;
let state = null;
let selectedOverlayId = null;
let selectedAudioEventId = null;
let saveTimer = null;
let toastTimer = null;
let renderPollTimer = null;
let pipelinePollTimer = null;
let history = [];
let redoStack = [];
let captionPreviews = {};
let captionPollTimer = null;
let editGeneration = 0;
let lastHistoryPushAt = 0;
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
let deskProviderRecords = [];
let deskProviderStatusLoaded = false;
let deskProviderSearchBusy = false;
let projectFonts = [];
let projectFontInfo = null;
const loadedProjectFontIds = new Set();
const loadingProjectFontIds = new Set();
let projectFontPreviewError = "";
let lastSaveSucceeded = true;
let presetSaveInFlight = 0;
// FX previews are deliberately local drafts. They never touch editor state
// until the server compare-and-swap confirm succeeds.
let fxPreviewDraft = null;
let fxPreviewReplaceOverlayId = null;
let fxPreviewTrigger = null;
let fxPreviewJob = null;
// The current job may be a standard-quality upgrade while the video element
// still shows the already-ready draft. Keep the displayed media's job/meta
// separate from the job that is running (and eventually confirmable).
let fxPreviewVisibleJob = null;
let fxPreviewVisibleMeta = null;
// Metadata callbacks can arrive after a different comparison candidate has
// become current. Every visible-media replacement advances this token so an
// old event cannot seek the newly selected video pair.
let fxPreviewVisibleMediaToken = 0;
let fxPreviewPollTimer = null;
let fxPreviewDebounceTimer = null;
let fxPreviewCancelPromise = null;
let fxPreviewGeneration = 0;
let fxPreviewReadySignature = "";
let fxPreviewDraftSignature = "";
let fxPreviewCatalogBreakpoint = null;
let fxPreviewConfirmed = false;
let activeFxPreviewCategory = "";
let fxPreviewLibraryFilter = "";
let fxPreviewStaleDraft = null;
let fxPreviewSyncing = false;
let fxPreviewUpgradeStarted = false;
let fxPreviewUpgradeCancelled = false;
// Comparison keeps independent frozen drafts/jobs.  The original preview
// remains deliberately single-job; comparison submits its own strict queue.
let fxCompareActive = false;
let fxCompareCandidates = [];
let fxCompareFrozen = null;
let fxCompareStarting = false;
let fxCompareRunning = false;
let fxCompareStopping = false;
let fxCompareConfirming = false;
let fxCompareSelectedId = "";
let fxCompareRun = 0;
let fxCompareSession = 0;
let fxCompareSelectionToken = 0;
let fxCompareCleanupPromise = null;
let fxCompareQueuePromise = null;
let fxCompareCloseRequested = false;
let fxCompareRoundStale = false;
let fxPreviewPostInFlight = null;
const fxPreviewLifecycles = new Set();
let fxPreviewUnverifiedJob = null;
let fxPreviewRecoveryPromise = null;
let fxPreviewOpenIntent = 0;
// Last state both sides agreed on. Diffing against this is what lets a save
// conflict be replayed: it says which top-level sections *this* window changed,
// so the background pipeline's writes can be kept and ours re-applied on top.
let lastSyncedState = null;
let pipelineBusy = false;
// Top-level keys the server owns; never treat them as a local edit to replay.
const SERVER_OWNED_STATE_KEYS = Object.freeze([
  "revision",
  "updated_at",
  "source_sha256",
  "highlight_plan_revision",
  "project_id",
  "schema_version",
]);
const PIPELINE_BUSY_STATES = Object.freeze(["pending", "running"]);
const DIRECTOR_PIPELINE_BUSY_NOTE = "字幕校準中，完成後可切換";
const PROVIDER_AVAILABILITY_MESSAGES = Object.freeze({
  svg_rasterizer_unavailable: "SVG 匯入目前不可用：本機安全轉檔引擎未就緒。",
  provider_disabled: "此素材來源目前已停用。",
  provider_unavailable: "此素材來源目前無法使用。",
});

const DIRECTOR_CARD_META = {
  "teacher-punch": { icon: "教", eyebrow: "清楚拆解" },
  "high-energy": { icon: "爆", eyebrow: "強 Hook" },
  "kinetic-explainer": { icon: "動", eyebrow: "動畫解說" },
  documentary: { icon: "聞", eyebrow: "衝突脈絡" },
  minimal: { icon: "視", eyebrow: "第一視角" },
  "editorial-clean": { icon: "編", eyebrow: "克制精準" },
};
const DIRECTOR_ORDER = ["teacher-punch", "high-energy", "documentary", "minimal", "editorial-clean", "kinetic-explainer"];
const ROLE_LAYOUTS = {
  hook: { x: 50, y: 50, width: 100, height: 100 },
  concept: { x: 50, y: 56, width: 93, height: 22 },
  rule: { x: 50, y: 56, width: 93, height: 27 },
  memory: { x: 50, y: 16, width: 87, height: 15 },
  recap: { x: 50, y: 41, width: 92, height: 48 },
};

const byId = (id) => document.getElementById(id);
const deepCopy = (value) => JSON.parse(JSON.stringify(value));

function isDirectorAvailable(preset) {
  return Boolean(preset) && preset.available !== false;
}

function directorUnavailableReason(preset) {
  const missing = Array.isArray(preset?.missing_capabilities)
    ? preset.missing_capabilities.filter(Boolean).sort()
    : [];
  return missing.length
    ? `尚未就緒：缺少 ${missing.join(", ")}`
    : "尚未就緒";
}

function cacheElements() {
  [
    "project-name", "platform-select", "director-select", "style-pack-select", "save-state", "save-button",
    "template-select", "template-group-tabs", "template-grid", "template-controls",
    "template-name", "template-description", "template-motion-chip", "frame-controls",
    "template-frame-x", "template-frame-x-output", "template-frame-y", "template-frame-y-output",
    "template-frame-width", "template-frame-width-output", "template-frame-height", "template-frame-height-output",
    "subject-controls", "template-subject-x", "template-subject-x-output",
    "template-subject-y", "template-subject-y-output", "template-subject-scale", "template-subject-scale-output",
    "background-controls", "template-background-color-row", "template-background-color",
    "template-background-fit-row", "template-background-fit",
    "template-background-button", "template-background-input", "template-background-status", "open-fx-menu",
    "template-capability-note", "template-background-image", "template-background-video",
    "render-button", "source-file-name", "source-file-detail", "source-preview-button",
    "transcript-calibration-status", "transcript-calibration-title", "transcript-calibration-copy",
    "highlight-count", "highlight-list", "editing-brief", "director-grid", "director-pipeline-note",
    "candidate-count",
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
    "audio-event-form", "audio-event-id", "audio-event-role", "audio-event-asset", "audio-event-start-sample",
    "audio-event-gain-db", "audio-event-start-seconds", "audio-event-policy", "audio-event-error",
    "timeline-scroll", "timeline-ruler", "timeline-tracks", "playhead", "toast", "download-output",
    "fx-panel", "fx-panel-note", "fx-cats", "fx-search", "fx-grid", "fx-spec", "fx-vars",
    "fx-preview-dialog", "fx-preview-close", "fx-preview-cats", "fx-preview-search", "fx-preview-grid",
    "fx-preview-compare-controls", "fx-preview-compare-toggle", "fx-preview-compare-add", "fx-preview-compare-start", "fx-preview-compare-discard", "fx-preview-compare-note", "fx-preview-compare-candidates",
    "fx-preview-selected-name", "fx-preview-spec", "fx-preview-source", "fx-preview-duration",
    "fx-preview-apply-source", "fx-preview-apply-duration", "fx-preview-apply-window-label",
    "fx-preview-mode", "fx-preview-quality", "fx-preview-text-fit", "fx-preview-window-label", "fx-preview-replace-note", "fx-preview-fields", "fx-preview-advanced-fields", "fx-preview-status",
    "fx-preview-video", "fx-preview-source-video", "fx-preview-source-time", "fx-preview-effect-time",
    "fx-preview-layout-check", "fx-preview-layout-check-heading", "fx-preview-layout-check-toggle",
    "fx-preview-layout-check-summary", "fx-preview-layout-check-list",
    "fx-preview-composition-check", "fx-preview-composition-check-heading", "fx-preview-composition-check-status",
    "fx-preview-composition-check-summary", "fx-preview-composition-check-samples", "fx-preview-composition-check-list",
    "fx-preview-composition-switch-timeline",
    "fx-preview-resync", "fx-preview-draft-notice", "fx-preview-draft-copy", "fx-preview-restore-draft",
    "fx-preview-discard-draft", "fx-preview-retry", "fx-preview-cancel-upgrade", "fx-preview-cancel", "fx-preview-confirm"
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
    const rawError = payload?.error;
    const errorMessage = typeof rawError === "string"
      ? rawError
      : rawError?.message || rawError?.detail || rawError?.error || "";
    const errors = Array.isArray(payload?.errors)
      ? payload.errors.map((item) => typeof item === "string" ? item : item?.message || JSON.stringify(item)).join("；")
      : "";
    const message = errorMessage || errors || `請求失敗（${response.status}）`;
    const error = new Error(message);
    error.status = response.status;
    // Callers need to tell a compare-and-swap conflict (recoverable: reload and
    // replay) apart from a validation failure (not recoverable by retrying).
    error.code = payload && typeof payload.error_code === "string" ? payload.error_code : null;
    error.reason = payload && typeof payload.reason === "string"
      ? payload.reason
      : payload && typeof payload.error_reason === "string"
        ? payload.error_reason
        : error.code;
    error.payload = payload;
    throw error;
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

const AUDIO_SAMPLE_RATE = 48000;
const AUDIO_IDENTIFIER_PATTERN = /^[A-Za-z0-9_-]{1,120}$/;
const AUDIO_SHA256_PATTERN = /^[0-9a-f]{64}$/;
const AUDIO_TIMELINE_INVALID_REASON = "音效時間軸資料格式無法使用";
const AUDIO_ROLE_LABELS = Object.freeze({
  title_enter: "標題入場",
  row_reveal: "列揭示",
  transition: "轉場",
  grid_fill: "網格填入",
  count_tick: "數字跳動",
  typing: "打字",
  complete: "完成",
});

function audioEventTimeline() {
  const timeline = projectPayload?.audio_event_timeline;
  return timeline && typeof timeline === "object" ? timeline : null;
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function isSafeAudioIdentifier(value) {
  return typeof value === "string" && AUDIO_IDENTIFIER_PATTERN.test(value);
}

function isAudioSha256(value) {
  return typeof value === "string" && AUDIO_SHA256_PATTERN.test(value);
}

function safeAudioReason(reason) {
  const compact = String(reason || "").replace(/\s+/g, " ").trim();
  if (!compact || compact.length > 120 || /[\\/]|[0-9a-f]{64}/i.test(compact)) {
    return AUDIO_TIMELINE_INVALID_REASON;
  }
  return compact;
}

function validateAudioEventTimeline(timeline = audioEventTimeline()) {
  if (!isPlainObject(timeline)) {
    return { valid: false, reason: AUDIO_TIMELINE_INVALID_REASON };
  }
  if (timeline.editable !== true || timeline.status === "unavailable") {
    return {
      valid: false,
      reason: safeAudioReason(timeline.reason) || "音效時間軸不可編輯",
    };
  }
  if (!["ready", "edited"].includes(timeline.status)) {
    return { valid: false, reason: AUDIO_TIMELINE_INVALID_REASON };
  }
  if (!isSafeAudioIdentifier(timeline.source_render_id)
    || !isAudioSha256(timeline.source_plan_sha256)
    || !isAudioSha256(timeline.source_timeline_revision)
    || !Array.isArray(timeline.events)) {
    return { valid: false, reason: AUDIO_TIMELINE_INVALID_REASON };
  }
  const ids = new Set();
  for (const event of timeline.events) {
    if (!isPlainObject(event)
      || !isSafeAudioIdentifier(event.id)
      || !isSafeAudioIdentifier(event.role)
      || !isSafeAudioIdentifier(event.asset_id)
      || !isAudioSha256(event.source_event_sha256)
      || !Number.isSafeInteger(event.event_start_sample)
      || event.event_start_sample < 0
      || !Number.isSafeInteger(event.duration_samples)
      || event.duration_samples <= 0
      || typeof event.gain_db !== "number"
      || !Number.isFinite(event.gain_db)
      || event.gain_db < -24
      || event.gain_db > -6
      || ids.has(event.id)) {
      return { valid: false, reason: AUDIO_TIMELINE_INVALID_REASON };
    }
    ids.add(event.id);
  }
  return { valid: true, reason: "" };
}

function audioTimelineEvents() {
  const timeline = audioEventTimeline();
  return validateAudioEventTimeline(timeline).valid ? timeline.events : [];
}

function currentAudioEvent() {
  return audioTimelineEvents().find((event) => String(event.id) === String(selectedAudioEventId)) || null;
}

function audioEventRoleLabel(event) {
  return AUDIO_ROLE_LABELS[event?.role] || String(event?.role || "音效");
}

function audioEventEdited(event) {
  const edits = state?.audio_event_edits?.events;
  return Boolean(event?.edited)
    || Boolean(Array.isArray(edits) && edits.some((item) => String(item.id) === String(event?.id)));
}

function audioEventDomId(eventId) {
  return `timeline-audio-event-${String(eventId || "event").replace(/[^A-Za-z0-9_-]+/g, "-")}`;
}

function audioEventTitle(event) {
  const gain = Number(event?.gain_db);
  const gainLabel = Number.isFinite(gain) ? `${gain} dB` : "未知";
  return `ID ${event?.id || "未知"} · 素材 ${event?.asset_id || "未知"} · 起始 sample ${event?.event_start_sample ?? "未知"} · 增益 ${gainLabel}`;
}

function audioTimelineReasonSummary() {
  return validateAudioEventTimeline(audioEventTimeline()).reason || AUDIO_TIMELINE_INVALID_REASON;
}

function audioEventStartSeconds(event) {
  return Number(event?.event_start_sample) / AUDIO_SAMPLE_RATE;
}

function audioEventEditFor(event) {
  return state?.audio_event_edits?.events?.find(
    (item) => String(item.id) === String(event?.id),
  ) || null;
}

function orderedAudioEventEdits(edits) {
  const sourceOrder = new Map(audioTimelineEvents().map((event, index) => [String(event.id), index]));
  return [...edits].sort((left, right) => {
    const leftIndex = sourceOrder.get(String(left.id));
    const rightIndex = sourceOrder.get(String(right.id));
    if (leftIndex !== undefined && rightIndex !== undefined && leftIndex !== rightIndex) {
      return leftIndex - rightIndex;
    }
    return String(left.id).localeCompare(String(right.id));
  });
}

function upsertAudioEventEdit(event, eventStartSample, gainDb) {
  const timeline = audioEventTimeline();
  if (!timeline?.source_render_id || !timeline.source_plan_sha256 || !timeline.source_timeline_revision) {
    return false;
  }
  const existingEnvelope = state.audio_event_edits;
  const existingEvents = Array.isArray(existingEnvelope?.events)
    ? existingEnvelope.events
      .filter((item) => item && item.id !== undefined)
      .map((item) => ({
        id: item.id,
        source_event_sha256: item.source_event_sha256,
        event_start_sample: item.event_start_sample,
        gain_db: item.gain_db,
      }))
    : [];
  const nextEvent = {
    id: event.id,
    source_event_sha256: event.source_event_sha256,
    event_start_sample: eventStartSample,
    gain_db: gainDb,
  };
  const index = existingEvents.findIndex((item) => String(item.id) === String(event.id));
  if (index >= 0) existingEvents[index] = nextEvent;
  else existingEvents.push(nextEvent);
  state.audio_event_edits = {
    schema_version: 1,
    source_render_id: timeline.source_render_id,
    source_plan_sha256: timeline.source_plan_sha256,
    source_timeline_revision: timeline.source_timeline_revision,
    events: orderedAudioEventEdits(existingEvents),
  };
  return true;
}

function setAudioEventError(message = "") {
  if (!elements["audio-event-error"]) return;
  elements["audio-event-error"].textContent = message;
  elements["audio-event-error"].hidden = !message;
}

function renderAudioEventInspectorMeta(event) {
  if (!event || !elements["audio-event-form"]) return;
  elements["audio-event-id"].value = event.id || "";
  elements["audio-event-role"].value = audioEventRoleLabel(event);
  elements["audio-event-asset"].value = event.asset_id || "";
  elements["audio-event-start-seconds"].textContent =
    `換算：${audioEventStartSeconds(event).toFixed(3)} 秒（${AUDIO_SAMPLE_RATE} samples/秒）`;
}

function renderAudioEventInspector(event) {
  if (!event || !elements["audio-event-form"]) return;
  elements["audio-event-form"].hidden = false;
  elements["audio-event-id"].value = event.id || "";
  elements["audio-event-role"].value = audioEventRoleLabel(event);
  elements["audio-event-asset"].value = event.asset_id || "";
  elements["audio-event-start-sample"].value = String(event.event_start_sample ?? "");
  elements["audio-event-gain-db"].value = String(event.gain_db ?? -12);
  renderAudioEventInspectorMeta(event);
  setAudioEventError("");
}

function handleAudioEventInput() {
  const event = currentAudioEvent();
  if (!event || !audioEventTimeline()?.editable) return;
  const sampleRaw = String(elements["audio-event-start-sample"].value || "").trim();
  const gainRaw = String(elements["audio-event-gain-db"].value || "").trim();
  const sample = Number(sampleRaw);
  const gain = Number(gainRaw);
  const errors = [];
  if (!sampleRaw || !Number.isSafeInteger(sample) || sample < 0) {
    errors.push("起始 sample 必須是大於等於 0 的安全整數。 ");
  }
  if (!gainRaw || !Number.isFinite(gain) || gain < -24 || gain > -6) {
    errors.push("增益必須是 -24 至 -6 dB 的有限數值。");
  }
  if (errors.length) {
    setAudioEventError(errors.join(""));
    return;
  }
  setAudioEventError("");
  const changed = Number(event.event_start_sample) !== sample || Number(event.gain_db) !== gain;
  if (!changed) {
    renderAudioEventInspectorMeta(event);
    return;
  }
  if (!upsertAudioEventEdit(event, sample, gain)) {
    setAudioEventError("音效時間軸來源不足，這次調整未套用。");
    return;
  }
  event.event_start_sample = sample;
  event.gain_db = gain;
  event.edited = true;
  const timeline = audioEventTimeline();
  if (timeline) timeline.status = "edited";
  renderAudioEventInspectorMeta(event);
  markDirty("音效變更，儲存中…");
}

function currentOverlay() {
  return state?.overlays?.find((overlay) => overlay.id === selectedOverlayId) || null;
}

function pushHistory() {
  if (!state) return;
  const now = Date.now();
  if (now - lastHistoryPushAt < 500 && history.length) {
    // Coalesce a typing burst into one undo step: the snapshot taken at the
    // start of the burst already captures the pre-burst state.
    redoStack = [];
    return;
  }
  lastHistoryPushAt = now;
  history.push(deepCopy(state));
  if (history.length > 40) history.shift();
  redoStack = [];
}

function undo() {
  if (fxCompareRoundLocked()) {
    fxPreviewSetStatus("比較進行中，已鎖定撤銷；請先停止或丟棄本輪。", "error");
    return;
  }
  if (!history.length) {
    showToast("目前沒有可撤銷的變更");
    return;
  }
  redoStack.push(deepCopy(state));
  if (redoStack.length > 40) redoStack.shift();
  state = history.pop();
  selectedOverlayId = state.review?.selected_overlay_id || null;
  activeTemplateGroup = projectPayload.video_templates?.[state.video_template?.id]?.group || "fixed";
  markDirty("已撤銷上一個變更");
  renderAll();
}

function redo() {
  if (fxCompareRoundLocked()) {
    fxPreviewSetStatus("比較進行中，已鎖定重做；請先停止或丟棄本輪。", "error");
    return;
  }
  if (!redoStack.length) {
    showToast("目前沒有可重做的變更");
    return;
  }
  history.push(deepCopy(state));
  if (history.length > 40) history.shift();
  state = redoStack.pop();
  selectedOverlayId = state.review?.selected_overlay_id || null;
  markDirty("已重做變更");
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
  editGeneration += 1;
  captionPreviews = {};
  ensureSourcePreview();
  setSaveState(message, "dirty");
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => saveState(false), 650);
  renderPreviewOverlays(true);
  renderLayerList();
  renderTimeline();
}

async function pollCaptionPreviews(attempt = 0, generation = editGeneration) {
  clearTimeout(captionPollTimer);
  try {
    const status = await request("/api/captions/status");
    if (generation !== editGeneration || stateDirty) {
      return; // the user edited again — this poll cycle is stale
    }
    if (status.engine?.status !== "present") return;
    if (status.ready) {
      captionPreviews = {};
      for (const item of status.items || []) {
        captionPreviews[item.id] = item;
      }
      renderPreviewOverlays(true);
      return;
    }
    if (status.job?.state === "failed") {
      showToast(`字幕渲染失敗：${status.job.error || "未知錯誤"}`, "error");
      return;
    }
  } catch (_error) {
    return;
  }
  if (attempt < 16 && generation === editGeneration) {
    captionPollTimer = setTimeout(
      () => pollCaptionPreviews(attempt + 1, generation), 500
    );
  }
}

function markStateSynced() {
  lastSyncedState = deepCopy(state);
}

function changedStateKeys(before, after) {
  if (!before || !after) return [];
  const keys = new Set([...Object.keys(before), ...Object.keys(after)]);
  const changed = [];
  keys.forEach((key) => {
    if (SERVER_OWNED_STATE_KEYS.includes(key)) return;
    if (JSON.stringify(before[key]) !== JSON.stringify(after[key])) changed.push(key);
  });
  return changed;
}

function stateMatchesLastSynced() {
  return Boolean(lastSyncedState && state && changedStateKeys(lastSyncedState, state).length === 0);
}

/**
 * A background pipeline write (semantic caption calibration and friends) bumps
 * the revision under us, so our compare-and-swap save 409s. Reload the server's
 * copy, replay the sections this window actually changed on top of it, and only
 * fall back to "we reloaded, your change to that section is gone" for sections
 * both sides touched. The result distinguishes a valid reload from an
 * unusable response so callers never infer sync from a false retry result.
 */
async function reconcileStateConflict() {
  const payload = await request("/api/project");
  const fresh = payload.state;
  if (!fresh || typeof fresh !== "object" || Array.isArray(fresh)) {
    return { retryable: false, reloaded: false };
  }
  const localChanges = changedStateKeys(lastSyncedState, state);
  const serverChanges = changedStateKeys(lastSyncedState, fresh);
  // Without a synced baseline there is no honest diff, so never guess: adopt
  // the server copy and say so, rather than replaying the whole local state
  // over somebody else's work.
  const collisions = lastSyncedState
    ? localChanges.filter((key) => serverChanges.includes(key))
    : ["*"];
  const replayable = localChanges.filter((key) => !serverChanges.includes(key));
  const merged = deepCopy(fresh);
  replayable.forEach((key) => { merged[key] = deepCopy(state[key]); });
  projectPayload = payload;
  state = merged;
  lastSyncedState = deepCopy(fresh);
  selectedOverlayId = state.overlays.some((overlay) => overlay.id === selectedOverlayId)
    ? selectedOverlayId
    : state.review?.selected_overlay_id || state.overlays[0]?.id || null;
  renderAll();
  // The writer is usually the local pipeline; keep watching it so the panel
  // refreshes itself (and re-enables the director) when it finishes.
  if (PIPELINE_BUSY_STATES.includes(payload.pipeline_status?.state)) pollPipelineStatus();
  if (collisions.length) {
    showToast("背景處理中，已重載最新版", "error");
    return { retryable: false, reloaded: true };
  }
  return { retryable: true, reloaded: true };
}

async function saveState(showConfirmation = true, allowDuringComparisonStart = false) {
  if (fxCompareRoundLocked() && !allowDuringComparisonStart) {
    fxPreviewSetStatus("比較輪次已鎖定，完成、停止或丟棄前不能另行儲存。", "error");
    return false;
  }
  const saveGeneration = editGeneration;
  presetSaveInFlight += 1;
  clearTimeout(saveTimer);
  saveTimer = null;
  try {
    let payload;
    try {
      payload = await putEditorState();
    } catch (error) {
      if (error.code !== "revision_conflict") throw error;
      // Somebody (almost always the local pipeline) wrote while we were editing.
      const reconciliation = await reconcileStateConflict();
      if (!reconciliation.retryable) {
        const ready = reconciliation.reloaded
          && saveGeneration === editGeneration
          && stateMatchesLastSynced();
        stateDirty = !ready;
        lastSaveSucceeded = ready;
        setSaveState(ready ? "已重載最新版" : "仍有編輯尚未儲存", ready ? "saved" : "dirty");
        return;
      }
      payload = await putEditorState();
      showToast("背景處理已更新專案，你的變更已重新套用", "info");
    }
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
      showToast("時間軸內容已變更，時間軸核可已失效", "info");
    }
    if ((payload.invalidated_gates || []).includes("final")) {
      if (projectPayload?.manifest?.approvals?.final) {
        projectPayload.manifest.approvals.final.approved = false;
      }
      if (projectPayload?.approval_current) projectPayload.approval_current.final = false;
      renderDeliveryQa();
    }
    markStateSynced();
    lastSaveSucceeded = true;
    setSaveState("已儲存", "saved");
    pollCaptionPreviews();
    if (showConfirmation) showToast("時間軸已儲存", "success");
  } catch (error) {
    lastSaveSucceeded = false;
    setSaveState("儲存失敗", "error");
    if (error.code === "revision_conflict") {
      showToast("背景處理中，已重載最新版", "error");
    } else {
      showToast(`儲存失敗：${error.message}`, "error");
    }
  }
  finally { presetSaveInFlight -= 1; }
}

function putEditorState() {
  return request("/api/editor-state", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    // CAS: the server 409s if someone else saved since our last sync,
    // instead of silently clobbering their edits.
    body: JSON.stringify({ ...state, x_expected_revision: state.revision || null }),
  });
}

function populatePresets() {
  elements["platform-select"].replaceChildren();
  Object.entries(projectPayload.platform_presets).forEach(([id, preset]) => {
    const option = document.createElement("option");
    option.value = id;
    const stale =
      preset.review_due_at && Date.parse(preset.review_due_at) < Date.now();
    option.textContent = stale ? `${preset.label}（規格待重核）` : preset.label;
    if (stale) option.title = `平台規格核對已過期：${preset.review_due_at}`;
    elements["platform-select"].append(option);
  });
  elements["director-select"].replaceChildren();
  Object.entries(projectPayload.director_presets).forEach(([id, preset]) => {
    const option = document.createElement("option");
    const available = isDirectorAvailable(preset);
    option.value = id;
    option.disabled = !available;
    option.setAttribute("aria-disabled", String(!available));
    option.textContent = available
      ? preset.label
      : `${preset.label}（${directorUnavailableReason(preset)}）`;
    option.title = available ? preset.description : directorUnavailableReason(preset);
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
  elements["style-pack-select"].replaceChildren();
  const noPack = document.createElement("option");
  noPack.value = "";
  noPack.textContent = "（無——系統預設）";
  elements["style-pack-select"].append(noPack);
  for (const pack of projectPayload.style_packs || []) {
    const option = document.createElement("option");
    option.value = pack.id;
    option.textContent = pack.label || pack.id;
    option.title = pack.description || pack.id;
    elements["style-pack-select"].append(option);
  }
  elements["platform-select"].value = state.canvas.platform_id;
  elements["director-select"].value = state.director_style;
  const template = projectPayload.video_templates?.[state.video_template?.id];
  activeTemplateGroup = template?.group || "fixed";
  elements["template-select"].value = state.video_template?.id || "dynamic-craft";
  elements["style-pack-select"].value = state.style_pack?.project_default || "";
  renderTemplatePicker();
  renderDirectorCards();
}

function renderDirectorCards() {
  elements["director-grid"].replaceChildren();
  Object.entries(projectPayload.director_presets)
    .sort(([left], [right]) => DIRECTOR_ORDER.indexOf(left) - DIRECTOR_ORDER.indexOf(right))
    .forEach(([id, preset], index) => {
      const meta = DIRECTOR_CARD_META[id] || { icon: "導", eyebrow: "導演風格" };
      const available = isDirectorAvailable(preset);
      const button = document.createElement("button");
      button.type = "button";
      button.className = `director-card${id === state.director_style ? " is-selected" : ""}`;
      button.disabled = !available;
      button.setAttribute("role", "radio");
      button.setAttribute("aria-checked", String(id === state.director_style));
      button.setAttribute("aria-disabled", String(!available));
      button.title = available ? preset.description : directorUnavailableReason(preset);
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
      description.textContent = available
        ? preset.description
        : `${preset.description}（${directorUnavailableReason(preset)}）`;
      copy.append(eyebrow, name, description);
      const check = document.createElement("i");
      check.className = "director-check";
      check.setAttribute("aria-hidden", "true");
      check.textContent = "✓";
      button.append(icon, copy, check);
      button.addEventListener("click", () => {
        if (pipelineBusy) {
          showToast(DIRECTOR_PIPELINE_BUSY_NOTE, "error");
          return;
        }
        if (!isDirectorAvailable(preset)) {
          showToast(directorUnavailableReason(preset), "error");
          return;
        }
        elements["director-select"].value = id;
        elements["director-select"].dispatchEvent(new Event("change", { bubbles: true }));
      });
      elements["director-grid"].append(button);
    });
  applyPipelineBusyState();
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
      button.dataset.templateId = id;
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
  const canvasWidth = Math.max(1, Number(state.canvas.width) || 1080);
  const canvasHeight = Math.max(1, Number(state.canvas.height) || 1920);
  const ratio = `${canvasWidth} / ${canvasHeight}`;
  elements["stage-frame"].style.setProperty("--canvas-ratio", ratio);
  // The stage frame also has to fit the zone's height; styles.css derives that
  // from these two numbers, so the aspect ratio survives a short viewport.
  elements["stage-frame"].style.setProperty("--canvas-w", String(canvasWidth));
  elements["stage-frame"].style.setProperty("--canvas-h", String(canvasHeight));
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
    const currentPreset = projectPayload?.director_presets?.[state?.director_style];
    if (!isDirectorAvailable(currentPreset)) {
      showToast(directorUnavailableReason(currentPreset), "error");
      return;
    }
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
  const typeLabels = { caption: "字幕", emphasis: "特效字", title: "標題卡", card: "字卡", image: "圖片", gif: "GIF", video: "插入影片", animation: "動畫字卡", fx: "特效" };
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

function renderFormulaPanel(payload) {
  const host = document.getElementById("highlight-list");
  if (!host || !payload || !payload.viral_structure_plan) return;
  const plan = payload.viral_structure_plan;
  const narrative = payload.narrative_plan;
  let box = document.getElementById("formula-panel");
  if (!box) {
    box = document.createElement("div");
    box.id = "formula-panel";
    box.style.cssText =
      "margin:8px 0;padding:8px;border:1px solid var(--line,#8a7f6d);" +
      "font-size:12px;line-height:1.5;background:rgba(0,0,0,0.04)";
    host.parentElement.insertBefore(box, host);
  }
  const selected = plan.selected || {};
  const candidate = (plan.candidates || []).find(
    (item) => item.idea_id === selected.idea_id
  );
  const warnings = []
    .concat(candidate ? candidate.warnings || [] : [])
    .concat(narrative ? narrative.warnings || [] : []);
  const reanchor = narrative && narrative.reanchor ? narrative.reanchor.status : null;
  box.innerHTML = "";
  const title = document.createElement("strong");
  title.textContent = `敘事公式：${selected.formula || "—"}`;
  box.appendChild(title);
  const meta = document.createElement("div");
  meta.textContent =
    `candidate ${selected.idea_id || "—"}｜integrity ` +
    `${candidate ? candidate.integrity_gate : "—"}` +
    (reanchor ? `｜evidence re-anchor：${reanchor}` : "");
  box.appendChild(meta);
  if (narrative && Array.isArray(narrative.segments)) {
    const segments = document.createElement("div");
    segments.textContent =
      "段落：" +
      narrative.segments
        .map((seg) => `${seg.purpose} ${seg.source_start}s–${seg.source_end}s`)
        .join("｜");
    box.appendChild(segments);
  }
  for (const warning of warnings) {
    const line = document.createElement("div");
    line.style.color = "#a33";
    line.textContent = `⚠ ${warning}`;
    box.appendChild(line);
  }
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
    captionPreviews[overlay.id]?.artifact_hash || null,
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
    const captionRaster =
      !designCard && ["caption", "emphasis"].includes(overlay.type)
        ? captionPreviews[overlay.id]
        : null;
    if (captionRaster && overlay.id !== selectedOverlayId) {
      // WYSIWYG: the compositor PNG IS the output pixel source; CSS only
      // downsizes the canvas-native raster (never upscales a smaller one).
      node = document.createElement("img");
      node.src = captionRaster.url;
      node.alt = overlay.text || "字幕";
      const rasterScale =
        elements["stage-frame"].clientWidth / Math.max(1, state.canvas.width);
      node.style.width = `${captionRaster.width * rasterScale}px`;
      node.classList.add("is-caption-raster");
    } else if (overlay.type === "fx") {
      // The real pixels come from HyperFrames at render time; the stage shows the
      // menu thumbnail (in the pack's theme when one exists) and the code.
      node = document.createElement("div");
      const effect = fxEffectByCode(overlay.code);
      const thumb = fxThumbUrl(effect);
      if (thumb) { const img = document.createElement("img"); img.src = thumb; img.alt = effect?.name || overlay.code; node.append(img); }
      const tag = document.createElement("span");
      tag.textContent = `${overlay.code || "FX"} ${effect?.name || ""}${effect?.matte ? " ⓜ" : ""}`;
      node.append(tag);
    } else if (assetType) {
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
    node.style.setProperty("--overlay-font", projectFontCssFamily(style));
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

async function addEffectSpan() {
  const overlay = currentOverlay();
  if (!overlay || !["caption", "emphasis"].includes(overlay.type) || overlay.design_role) return;
  const input = elements["overlay-text"];
  let start = input.selectionStart;
  let end = input.selectionEnd;
  let text = input.value.slice(start, end);
  if (!text.trim() || end <= start) {
    showToast("請先在字幕文字中選取要強調的字詞", "error");
    input.focus();
    return;
  }
  // Server is the boundary authority: snap the raw selection to grapheme
  // cluster boundaries so emoji/combining marks can never be half-selected.
  try {
    const snapped = await request("/api/captions/snap", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: input.value, start_char: start, end_char: end }),
    });
    if (snapped.removed) {
      showToast("選取範圍無法對齊完整字元，請重新選取", "error");
      return;
    }
    start = snapped.start_char;
    end = snapped.end_char;
    text = snapped.text;
  } catch (_error) {
    // Engine unavailable: keep the raw selection; server-side validation
    // will reject only when boundary enforcement is active.
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

// ---- 風格菜單特效（fx overlay）：目錄來自 /api/project 的 fx_catalog ----
let activeFxCategory = "";
let lastFxCode = "";

function fxCatalog() { return projectPayload?.fx_catalog || { effects: [], categories: {}, schemes: {} }; }
function fxEffects() { return fxCatalog().effects || []; }
function fxEffectByCode(code) { return fxEffects().find((effect) => effect.code === code) || null; }
function fxThemeNo() {
  const packId = String(state?.style_pack?.project_default || "");
  const match = packId.match(/^theme-([A-Za-z0-9]+)-(paper|navy)$/);
  return match ? match[1] : "kwons";
}
function fxThumbUrl(effect) {
  if (!effect) return "";
  const byTheme = effect.thumbs_by_theme || {};
  const rel = byTheme[fxThemeNo()] || effect.thumb || "";
  return rel ? `${fxCatalog().thumb_base || "/fx-thumbs/"}${rel}` : "";
}

function fxPreviewIsBlocked(effect) {
  return String(effect?.code || "").toUpperCase() === "B1" && !fxPreviewDraft?.material?.asset_id;
}

function fxPreviewReplaceOverlay() {
  if (!fxPreviewReplaceOverlayId || !Array.isArray(state?.overlays)) return null;
  return state.overlays.find((overlay) => String(overlay.id) === String(fxPreviewReplaceOverlayId)) || null;
}

const FX_PREVIEW_PREFERENCES_KEY = "auto-edit:fx-preview-preferences:v1";
const FX_PREVIEW_DRAFTS_KEY = "auto-edit:fx-preview-drafts:v1";

function fxPreviewStorageRead(key, fallback) {
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : fallback;
  } catch (_error) {
    return fallback;
  }
}

function fxPreviewStorageWrite(key, value) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch (_error) {
    return false;
  }
}

function fxPreviewProjectIdentity() {
  const projectId = String(projectPayload?.manifest?.project_id || projectPayload?.state?.project_id || "").trim();
  return projectId || "unknown-project";
}

function fxPreviewDraftKey() {
  return `${fxPreviewProjectIdentity()}::${fxPreviewReplaceOverlayId || "new"}`;
}

function fxPreviewCurrentRevision() {
  return String(state?.revision || "");
}

function fxPreviewSavedDraft() {
  const drafts = fxPreviewStorageRead(FX_PREVIEW_DRAFTS_KEY, {});
  return drafts[fxPreviewDraftKey()] && typeof drafts[fxPreviewDraftKey()] === "object"
    ? drafts[fxPreviewDraftKey()]
    : null;
}

function fxPreviewPersistDraft() {
  if (!fxPreviewDraft || fxPreviewIsBlocked(fxEffectByCode(fxPreviewDraft.code))) return;
  const drafts = fxPreviewStorageRead(FX_PREVIEW_DRAFTS_KEY, {});
  drafts[fxPreviewDraftKey()] = {
    revision: fxPreviewCurrentRevision(),
    draft: deepCopy(fxPreviewDraft),
    saved_at: new Date().toISOString(),
  };
  fxPreviewStorageWrite(FX_PREVIEW_DRAFTS_KEY, drafts);
}

function fxPreviewDiscardSavedDraft() {
  if (fxCompareRoundLocked()) return;
  const drafts = fxPreviewStorageRead(FX_PREVIEW_DRAFTS_KEY, {});
  delete drafts[fxPreviewDraftKey()];
  fxPreviewStorageWrite(FX_PREVIEW_DRAFTS_KEY, drafts);
  fxPreviewStaleDraft = null;
  fxPreviewRenderDraftNotice();
}

function fxPreviewPreferences() {
  const stored = fxPreviewStorageRead(FX_PREVIEW_PREFERENCES_KEY, {});
  return {
    favorites: Array.isArray(stored.favorites) ? stored.favorites.filter((code) => typeof code === "string") : [],
    recent: Array.isArray(stored.recent) ? stored.recent.filter((code) => typeof code === "string") : [],
  };
}

function fxPreviewSavePreferences(preferences) {
  fxPreviewStorageWrite(FX_PREVIEW_PREFERENCES_KEY, preferences);
}

function fxPreviewIsFavorite(code) {
  return fxPreviewPreferences().favorites.includes(code);
}

function fxPreviewToggleFavorite(code) {
  if (fxCompareRoundLocked()) return;
  const preferences = fxPreviewPreferences();
  preferences.favorites = preferences.favorites.includes(code)
    ? preferences.favorites.filter((item) => item !== code)
    : [...preferences.favorites, code];
  fxPreviewSavePreferences(preferences);
  fxPreviewRenderCatalog();
}

function fxPreviewRememberRecent(code) {
  if (!code) return;
  const preferences = fxPreviewPreferences();
  preferences.recent = [code, ...preferences.recent.filter((item) => item !== code)].slice(0, 12);
  fxPreviewSavePreferences(preferences);
}

function fxPreviewRenderDraftNotice() {
  const notice = elements["fx-preview-draft-notice"];
  if (!notice) return;
  const copy = elements["fx-preview-draft-copy"];
  const restore = elements["fx-preview-restore-draft"];
  const discard = elements["fx-preview-discard-draft"];
  const hasCurrentDraft = Boolean(fxPreviewDraft?.__restored);
  const stale = fxPreviewStaleDraft;
  notice.hidden = !hasCurrentDraft && !stale;
  restore.hidden = !stale;
  discard.hidden = !hasCurrentDraft && !stale;
  if (stale) {
    copy.textContent = stale.revision === fxPreviewCurrentRevision()
      ? "偵測到未套用的替換草稿；目前顯示時間軸上的設定，可選擇還原草稿。"
      : "偵測到舊版本的未套用候選草稿；專案版本已變更，不會自動還原或套用。可選擇還原或丟棄。";
  } else if (hasCurrentDraft) {
    copy.textContent = "已還原未套用候選草稿；可繼續試看、直接丟棄，確認前不會改動時間軸。";
  }
}

function fxPreviewRestoreStaleDraft() {
  if (fxCompareRoundLocked()) return;
  const saved = fxPreviewStaleDraft;
  if (!saved?.draft) return;
  const effect = fxEffectByCode(saved.draft.code);
  if (!effect) {
    fxPreviewDiscardSavedDraft();
    return;
  }
  fxPreviewDraft = fxPreviewBuildDraft(effect, saved.draft, fxPreviewReplaceOverlay(), fxPreviewSourceStart());
  fxPreviewDraft.__restored = true;
  fxPreviewStaleDraft = null;
  fxPreviewPersistDraft();
  fxPreviewRenderDialog();
  fxPreviewInvalidateAndSchedule();
}

function fxPreviewCaptionText(start) {
  const active = currentOverlay();
  const candidates = (state?.overlays || [])
    .filter((overlay) => ["caption", "emphasis", "title", "animation"].includes(overlay.type))
    .filter((overlay) => Number(overlay.start) <= start + 0.01 && Number(overlay.end) >= start - 0.01)
    .sort((left, right) => Number(left.start) - Number(right.start));
  const preferred = active && ["caption", "emphasis", "title", "animation"].includes(active.type)
    ? active
    : candidates[0];
  return String(preferred?.text || candidates[0]?.text || "").trim();
}

function fxPreviewSourceStart() {
  const overlay = fxPreviewReplaceOverlay();
  const current = Number(elements["preview-video"]?.currentTime || 0);
  if (showingRenderedMedia) {
    // The rendered output has a different time axis. Preserve the selected FX
    // layer's source start, or the last visible time before switching back.
    const fallback = Number.isFinite(Number(overlay?.start)) ? Number(overlay.start) : current;
    ensureSourcePreview();
    return fallback;
  }
  return Number.isFinite(Number(overlay?.start)) ? Number(overlay.start) : current;
}

function fxPreviewTextSlotUsesCaption(item) {
  const id = String(item?.id || "").toLowerCase();
  const allowed = new Set(["title", "text", "caption", "copy", "subtitle", "headline", "body", "label", "message", "cta", "sub"]);
  if (!allowed.has(id)) return false;
  if (Array.isArray(item?.options) || Array.isArray(item?.enum)) return false;
  const defaultValue = String(item?.default ?? "").trim();
  return !defaultValue || !Number.isFinite(Number(defaultValue));
}

function fxPreviewMaxDuration(effect, start) {
  const sourceDuration = Number(duration() || 0);
  const sourceTail = Math.max(0.1, sourceDuration - start);
  const effectMax = Number(effect?.media_max);
  return Math.max(0.1, Math.min(4, sourceTail, Number.isFinite(effectMax) && effectMax > 0 ? effectMax : 4));
}

function fxPreviewValidOpacity(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
}

function fxPreviewBuildDraft(effect, seed = null, overlay = null, fallbackStart = 0) {
  if (!effect) return null;
  const seedWindow = seed?.window || {};
  const start = Number.isFinite(Number(seedWindow.start_s))
    ? Number(seedWindow.start_s)
    : Number.isFinite(Number(overlay?.start)) ? Number(overlay.start) : Number(fallbackStart || 0);
  const safeStart = Math.max(0, Math.min(start, Math.max(0, Number(duration() || 0) - 0.1)));
  const maxDuration = fxPreviewMaxDuration(effect, safeStart);
  const overlayDuration = overlay ? Math.max(0.01, Number(overlay.end) - Number(overlay.start)) : 3;
  const rawDuration = Number.isFinite(Number(seedWindow.duration_s))
    ? Number(seedWindow.duration_s)
    : Math.min(overlayDuration, maxDuration);
  const text = fxPreviewCaptionText(safeStart);
  const priorValues = seed?.values || overlay?.values || {};
  const values = {};
  (effect.vars || []).filter((item) => item.kind === "text").forEach((item) => {
    values[item.id] = Object.prototype.hasOwnProperty.call(priorValues, item.id)
      ? String(priorValues[item.id] ?? "")
      : (fxPreviewTextSlotUsesCaption(item) && text ? text : String(item.default ?? ""));
  });
  const placement = (effect.vars || []).find((item) => item.kind === "placement");
  const priorFonts = seed?.font_ids || overlay?.font_ids || {};
  const sourceDuration = Math.max(0.1, Number(duration() || 0.1));
  const priorApply = seed?.apply_window || {};
  const applyStart = Math.max(0, Math.min(Number.isFinite(Number(priorApply.start_s)) ? Number(priorApply.start_s) : (overlay ? Number(overlay.start) : safeStart), sourceDuration - 0.1));
  const applyDuration = Math.max(0.1, Math.min(sourceDuration - applyStart, Number.isFinite(Number(priorApply.duration_s)) ? Number(priorApply.duration_s) : (overlay ? Number(overlay.end) - Number(overlay.start) : Math.min(3, sourceDuration - applyStart))));
  return {
    code: effect.code,
    window: {
      start_s: Math.max(applyStart, Math.min(safeStart, applyStart + applyDuration - 0.1)),
      duration_s: Math.max(0.1, Math.min(maxDuration, Number.isFinite(rawDuration) ? rawDuration : maxDuration)),
    },
    apply_window: { start_s: applyStart, duration_s: applyDuration },
    values,
    placement: seed?.placement ?? overlay?.placement ?? placement?.default ?? "",
    scheme: seed?.scheme ?? overlay?.scheme ?? "",
    opacity: fxPreviewValidOpacity(seed?.opacity) ? seed.opacity : fxPreviewValidOpacity(overlay?.style?.opacity) ? overlay.style.opacity : 1,
    font_ids: { ...priorFonts },
    subject: seed?.subject ?? null,
    text_fit: seed?.text_fit !== false,
    material: seed?.material || null,
    preview_options: { mode: seed?.preview_options?.mode === "timeline" ? "timeline" : "single", quality: seed?.preview_options?.quality === "standard" ? "standard" : "draft" },
  };
}

function fxPreviewSignature() {
  if (!fxPreviewDraft) return "";
  const stable = deepCopy(fxPreviewDraft);
  if (stable.preview_options) delete stable.preview_options.quality;
  return JSON.stringify(stable);
}

function fxPreviewSetStatus(message, tone = "") {
  const status = elements["fx-preview-status"];
  if (!status) return;
  status.textContent = message;
  status.classList.toggle("is-error", tone === "error");
  status.classList.toggle("is-success", tone === "success");
}

const FX_PREVIEW_LAYOUT_CHECK_LABELS = Object.freeze({
  safe_area: "文字接近安全區邊緣",
  text_overflow: "文字超出可用範圍",
  subject_overlap: "文字可能遮住人物或臉",
  face_overlap: "文字可能遮住人物或臉",
  subject_measurement_unavailable: "未能可靠檢查，請看畫面確認",
  face_measurement_unavailable: "未能可靠檢查，請看畫面確認",
  dom_measurement_unavailable: "未能可靠檢查，請看畫面確認",
  text_too_small: "文字已大幅縮小，建議縮短",
});
const FX_PREVIEW_LAYOUT_CHECK_UNAVAILABLE = "未能可靠檢查，請看畫面確認";
const FX_PREVIEW_LAYOUT_CHECK_DISABLED = "停用";

function fxPreviewLayoutCheckKey(value) {
  return String(value || "").trim().toLowerCase().replace(/-/g, "_");
}

function fxPreviewLayoutCheckMeasurementAvailable(value) {
  const key = fxPreviewLayoutCheckKey(value);
  return Boolean(key) && !["unknown", "unavailable", "disabled", "none", "null"].includes(key);
}

function fxPreviewRenderTextLayout(textLayout = null) {
  const panel = elements["fx-preview-layout-check"];
  const summary = elements["fx-preview-layout-check-summary"];
  const list = elements["fx-preview-layout-check-list"];
  const toggle = elements["fx-preview-layout-check-toggle"];
  if (!panel || !summary || !list || !toggle) return;
  panel.hidden = true;
  summary.textContent = "";
  list.replaceChildren();
  list.hidden = true;
  toggle.hidden = true;
  toggle.setAttribute("aria-expanded", "false");
  toggle.textContent = "查看細節";
  if (!textLayout || typeof textLayout !== "object" || Array.isArray(textLayout)) return;

  const details = [];
  const addDetail = (label) => {
    if (label && !details.includes(label) && details.length < 10) details.push(label);
  };
  let unknownFinding = false;
  const findings = Array.isArray(textLayout.findings) ? textLayout.findings : [];
  findings.forEach((finding) => {
    const kind = fxPreviewLayoutCheckKey(finding?.kind);
    const label = FX_PREVIEW_LAYOUT_CHECK_LABELS[kind];
    if (label) addDetail(label);
    else if (kind) unknownFinding = true;
  });

  const version = Number(textLayout.version);
  const measurement = fxPreviewLayoutCheckKey(textLayout.measurement);
  const status = fxPreviewLayoutCheckKey(textLayout.status);
  const faceMeasurement = fxPreviewLayoutCheckKey(textLayout.face_measurement);
  const measurementReliable = version === 2
    && measurement === "composition_dom"
    && fxPreviewLayoutCheckMeasurementAvailable(faceMeasurement);
  const statusKnown = ["pass", "warn"].includes(status);
  const disabled = measurement === "disabled";
  const unavailable = !measurementReliable || !statusKnown || unknownFinding;
  if ((!disabled && unavailable) || (!disabled && !details.length && status === "warn")) {
    addDetail(FX_PREVIEW_LAYOUT_CHECK_UNAVAILABLE);
  }

  if (disabled) {
    summary.textContent = `特效排版：${FX_PREVIEW_LAYOUT_CHECK_DISABLED}`;
  } else if (unavailable) {
    summary.textContent = `特效排版：${FX_PREVIEW_LAYOUT_CHECK_UNAVAILABLE}`;
  } else if (status === "warn" || details.length) {
    summary.textContent = details.length
      ? `特效排版：有 ${details.length} 項提醒`
      : "特效排版：有提醒，請看畫面確認";
  } else {
    summary.textContent = "特效排版：通過";
  }
  details.forEach((detail) => {
    const item = document.createElement("li");
    item.textContent = detail;
    list.append(item);
  });
  toggle.hidden = !details.length;
  panel.hidden = false;
}

function fxPreviewToggleTextLayoutDetails() {
  const toggle = elements["fx-preview-layout-check-toggle"];
  const list = elements["fx-preview-layout-check-list"];
  if (!toggle || !list || toggle.hidden) return;
  const expanded = toggle.getAttribute("aria-expanded") === "true";
  list.hidden = expanded;
  toggle.setAttribute("aria-expanded", String(!expanded));
  toggle.textContent = expanded ? "查看細節" : "收合細節";
}

const FX_PREVIEW_COMPOSITION_FINDING_LABELS = Object.freeze({
  text_subject_overlap: "文字可能遮住人物或臉",
  visibility_uncertain: "人物是否被遮住，無法確認",
  source_subject_unmeasured: "來源人物未能可靠量測",
  ocr_unavailable: "文字辨識未完成",
  reference_unavailable: "來源畫面參照未完成",
  analysis_incomplete: "抽樣檢查未完成",
  full_timeline_required: "完整畫面檢查需要完整時間軸",
});
const FX_PREVIEW_COMPOSITION_FINDING_KINDS = new Set(Object.keys(FX_PREVIEW_COMPOSITION_FINDING_LABELS));
const FX_PREVIEW_COMPOSITION_RECEIPT_KEYS = new Set([
  "version", "scope", "measurement", "status", "source_sha256", "media_sha256", "frame_size",
  "sample_count", "measured_count", "elapsed_s", "engines", "samples", "findings",
]);
const FX_PREVIEW_COMPOSITION_SAMPLE_KEYS = new Set([
  "output_at", "source_at", "measured", "source_face_count", "reference_face_count",
  "visible_face_count", "text_box_count",
]);
const FX_PREVIEW_COMPOSITION_FINDING_KEYS = new Set([
  "kind", "output_at", "source_at", "subject_box", "text_box",
]);
const FX_PREVIEW_COMPOSITION_ENGINE_KEYS = new Set(["faces", "ocr"]);
const FX_PREVIEW_COMPOSITION_FRAME_KEYS = new Set(["width", "height"]);
const FX_PREVIEW_COMPOSITION_BOX_KEYS = new Set(["x", "y", "width", "height"]);
const FX_PREVIEW_COMPOSITION_SHA_RE = /^[0-9a-f]{64}$/;
const FX_PREVIEW_COMPOSITION_UNMEASURED = "完整畫面檢查：目前無法可靠量測，請看預覽確認。";

function fxPreviewFiniteNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function fxPreviewNonNegativeInteger(value, max = 1000000) {
  const number = Number(value);
  return Number.isInteger(number) && number >= 0 && number <= max ? number : null;
}

function fxPreviewCompositionMapSegments(job) {
  if (!Array.isArray(job?.time_map)) return [];
  const segments = job.time_map.map((item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return null;
    const outputStart = fxPreviewFiniteNumber(item.output_start_s);
    const outputEnd = fxPreviewFiniteNumber(item.output_end_s);
    const sourceStart = fxPreviewFiniteNumber(item.source_start_s);
    const sourceEnd = fxPreviewFiniteNumber(item.source_end_s);
    if ([outputStart, outputEnd, sourceStart, sourceEnd].some((value) => value === null)
      || outputStart < 0 || sourceStart < 0 || outputEnd <= outputStart || sourceEnd <= sourceStart) return null;
    return { outputStart, outputEnd, sourceStart, sourceEnd };
  }).filter(Boolean);
  if (!segments.length || segments[0].outputStart > 0.012) return [];
  for (let index = 1; index < segments.length; index += 1) {
    const previous = segments[index - 1];
    const current = segments[index];
    if (Math.abs(current.outputStart - previous.outputEnd) > 0.012
      || current.sourceStart < previous.sourceEnd - 0.012) return [];
  }
  return segments;
}

function fxPreviewCompositionMapPair(outputAt, sourceAt, job, requireMetadata = false) {
  const output = fxPreviewFiniteNumber(outputAt);
  const source = fxPreviewFiniteNumber(sourceAt);
  if (output === null || source === null || output < 0 || source < 0) return null;
  const outputVideo = elements["fx-preview-video"];
  const sourceVideo = elements["fx-preview-source-video"];
  if (requireMetadata && (!outputVideo || !sourceVideo || outputVideo.readyState < 1 || sourceVideo.readyState < 1)) return null;
  const outputDuration = fxPreviewFiniteNumber(job?.render_duration_s) ?? fxPreviewFiniteNumber(outputVideo?.duration);
  const sourceDuration = fxPreviewFiniteNumber(sourceVideo?.duration) ?? fxPreviewFiniteNumber(duration());
  if (outputDuration === null || sourceDuration === null
    || output > outputDuration + 0.01 || source > sourceDuration + 0.01) return null;
  const epsilon = 0.012;
  for (const segment of fxPreviewCompositionMapSegments(job)) {
    const outputInside = output >= segment.outputStart - epsilon && output <= segment.outputEnd + epsilon;
    const sourceInside = source >= segment.sourceStart - epsilon && source <= segment.sourceEnd + epsilon;
    if (!outputInside || !sourceInside) continue;
    const outputSpan = segment.outputEnd - segment.outputStart;
    const sourceSpan = segment.sourceEnd - segment.sourceStart;
    const progress = Math.max(0, Math.min(1, (output - segment.outputStart) / outputSpan));
    const expectedSource = segment.sourceStart + progress * sourceSpan;
    if (Math.abs(expectedSource - source) <= 0.03) {
      return { outputAt: output, sourceAt: source, segment };
    }
  }
  return null;
}

function fxPreviewCompositionMapOutput(outputAt, job) {
  const output = fxPreviewFiniteNumber(outputAt);
  if (output === null || output < 0) return null;
  const segment = fxPreviewCompositionMapSegments(job).find((item) => (
    output >= item.outputStart - 0.012 && output <= item.outputEnd + 0.012
  ));
  if (!segment) return null;
  const progress = Math.max(0, Math.min(1, (output - segment.outputStart) / (segment.outputEnd - segment.outputStart)));
  return segment.sourceStart + progress * (segment.sourceEnd - segment.sourceStart);
}

function fxPreviewCompositionMapSource(sourceAt, job) {
  const source = fxPreviewFiniteNumber(sourceAt);
  if (source === null || source < 0) return null;
  const segment = fxPreviewCompositionMapSegments(job).find((item) => (
    source >= item.sourceStart - 0.012 && source <= item.sourceEnd + 0.012
  ));
  if (!segment) return null;
  const progress = Math.max(0, Math.min(1, (source - segment.sourceStart) / (segment.sourceEnd - segment.sourceStart)));
  return segment.outputStart + progress * (segment.outputEnd - segment.outputStart);
}

function fxPreviewCompositionBoxValid(box, frameSize) {
  if (!box || typeof box !== "object" || Array.isArray(box)) return false;
  if (Object.keys(box).some((key) => !FX_PREVIEW_COMPOSITION_BOX_KEYS.has(key))) return false;
  const x = fxPreviewFiniteNumber(box.x);
  const y = fxPreviewFiniteNumber(box.y);
  const width = fxPreviewFiniteNumber(box.width);
  const height = fxPreviewFiniteNumber(box.height);
  const frameWidth = fxPreviewFiniteNumber(frameSize?.width);
  const frameHeight = fxPreviewFiniteNumber(frameSize?.height);
  return [x, y, width, height, frameWidth, frameHeight].every((value) => value !== null)
    && width > 0 && height > 0 && x >= 0 && y >= 0
    && x + width <= frameWidth + 0.01 && y + height <= frameHeight + 0.01;
}

function fxPreviewCompositionReceipt(raw, job) {
  const fallback = {
    version: 1,
    scope: "",
    measurement: "",
    status: "unmeasured",
    source_sha256: "",
    media_sha256: "",
    frame_size: null,
    sample_count: null,
    measured_count: null,
    elapsed_s: null,
    engines: {},
    samples: [],
    findings: [],
    reason: "完整畫面檢查結果未提供",
  };
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return fallback;
  const receipt = deepCopy(raw);
  const unknownReceiptKeys = Object.keys(receipt).some((key) => !FX_PREVIEW_COMPOSITION_RECEIPT_KEYS.has(key));
  const inferredScope = job?.preview_options?.mode === "single"
    ? "single-effect" : job?.preview_options?.mode === "timeline" ? "full-timeline" : "";
  const scopeFieldValid = receipt.scope === "full-timeline" || receipt.scope === "single-effect";
  const scope = scopeFieldValid
    ? receipt.scope : inferredScope;
  const measurement = receipt.measurement === "sampled-local-frames" ? receipt.measurement : "";
  const status = ["pass", "warn", "unmeasured"].includes(receipt.status) ? receipt.status : "unmeasured";
  const frameSize = receipt.frame_size && typeof receipt.frame_size === "object" && !Array.isArray(receipt.frame_size)
    ? { width: fxPreviewFiniteNumber(receipt.frame_size.width), height: fxPreviewFiniteNumber(receipt.frame_size.height) }
    : null;
  const frameShapeValid = Boolean(receipt.frame_size && typeof receipt.frame_size === "object"
    && !Array.isArray(receipt.frame_size)
    && Object.keys(receipt.frame_size).every((key) => FX_PREVIEW_COMPOSITION_FRAME_KEYS.has(key)));
  const frameValid = Boolean(frameSize && Number.isInteger(frameSize.width) && Number.isInteger(frameSize.height)
    && frameSize.width > 0 && frameSize.height > 0 && frameSize.width <= 16384 && frameSize.height <= 16384);
  const sampleCount = fxPreviewNonNegativeInteger(receipt.sample_count, 8);
  const measuredCount = fxPreviewNonNegativeInteger(receipt.measured_count, 8);
  const elapsed = fxPreviewFiniteNumber(receipt.elapsed_s);
  const engines = receipt.engines && typeof receipt.engines === "object" && !Array.isArray(receipt.engines)
    ? { faces: receipt.engines.faces, ocr: receipt.engines.ocr }
    : {};
  const enginesValid = engines.faces === "opencv-yunet" && engines.ocr === "macos-vision"
    && Object.keys(receipt.engines || {}).every((key) => FX_PREVIEW_COMPOSITION_ENGINE_KEYS.has(key));
  const rawSamples = Array.isArray(receipt.samples) ? receipt.samples : [];
  const samplesShapeValid = rawSamples.length <= 8 && rawSamples.every((sample) => (
    sample && typeof sample === "object" && !Array.isArray(sample)
    && Object.keys(sample).every((key) => FX_PREVIEW_COMPOSITION_SAMPLE_KEYS.has(key))
  ));
  const samples = rawSamples.slice(0, 8).map((sample) => ({
    output_at: fxPreviewFiniteNumber(sample?.output_at),
    source_at: fxPreviewFiniteNumber(sample?.source_at),
    measured: sample?.measured === true,
    source_face_count: fxPreviewNonNegativeInteger(sample?.source_face_count, 64),
    reference_face_count: fxPreviewNonNegativeInteger(sample?.reference_face_count, 64),
    visible_face_count: fxPreviewNonNegativeInteger(sample?.visible_face_count, 64),
    text_box_count: fxPreviewNonNegativeInteger(sample?.text_box_count, 128),
  }));
  const sampleValuesValid = samples.every((sample) => (
    sample.output_at !== null && sample.source_at !== null && typeof sample.measured === "boolean"
    && sample.source_face_count !== null && sample.reference_face_count !== null
    && sample.visible_face_count !== null && sample.text_box_count !== null
  ));
  const rawFindings = Array.isArray(receipt.findings) ? receipt.findings : [];
  const findingsShapeValid = rawFindings.length <= 32 && rawFindings.every((finding) => (
    finding && typeof finding === "object" && !Array.isArray(finding)
    && Object.keys(finding).every((key) => FX_PREVIEW_COMPOSITION_FINDING_KEYS.has(key))
    && FX_PREVIEW_COMPOSITION_FINDING_KINDS.has(finding.kind)
    && (finding.kind !== "text_subject_overlap"
      || (Object.prototype.hasOwnProperty.call(finding, "subject_box")
        && Object.prototype.hasOwnProperty.call(finding, "text_box")))
  ));
  const findings = rawFindings.slice(0, 32).map((finding) => {
    const item = { kind: finding.kind };
    for (const key of ["output_at", "source_at"]) {
      if (Object.prototype.hasOwnProperty.call(finding, key)) item[key] = fxPreviewFiniteNumber(finding[key]);
    }
    for (const key of ["subject_box", "text_box"]) {
      if (Object.prototype.hasOwnProperty.call(finding, key)) item[key] = deepCopy(finding[key]);
    }
    return item;
  });
  const samplePairsValid = samplesShapeValid && samples.every((sample) => (
    sample.output_at !== null && sample.source_at !== null
    && fxPreviewCompositionMapPair(sample.output_at, sample.source_at, job)
  ));
  const timedFindingsValid = findingsShapeValid && findings.every((finding) => {
    const hasTimes = finding.output_at !== undefined || finding.source_at !== undefined;
    if (!hasTimes) return true;
    return finding.output_at !== null && finding.source_at !== undefined && finding.source_at !== null
      && fxPreviewCompositionMapPair(finding.output_at, finding.source_at, job);
  });
  const boxesValid = findingsShapeValid && findings.every((finding) => (
    (!Object.prototype.hasOwnProperty.call(finding, "subject_box") || fxPreviewCompositionBoxValid(finding.subject_box, frameSize))
    && (!Object.prototype.hasOwnProperty.call(finding, "text_box") || fxPreviewCompositionBoxValid(finding.text_box, frameSize))
  ));
  const sourceSha = String(receipt.source_sha256 || "");
  const mediaSha = String(receipt.media_sha256 || "");
  const shaValid = FX_PREVIEW_COMPOSITION_SHA_RE.test(sourceSha)
    && FX_PREVIEW_COMPOSITION_SHA_RE.test(mediaSha)
    && sourceSha !== "0".repeat(64) && mediaSha !== "0".repeat(64);
  const countsValid = sampleCount !== null && measuredCount !== null && measuredCount <= sampleCount
    && sampleCount === rawSamples.length;
  const baseValid = !unknownReceiptKeys && receipt.version === 1 && scopeFieldValid && Boolean(scope) && Boolean(measurement)
    && frameShapeValid && frameValid && elapsed !== null && elapsed >= 0 && enginesValid && countsValid
    && samplesShapeValid && sampleValuesValid && samplePairsValid && findingsShapeValid && timedFindingsValid && boxesValid;
  const passValid = baseValid && shaValid && scope === "full-timeline" && status === "pass"
    && sampleCount > 0 && measuredCount === sampleCount && samples.length === sampleCount
    && samples.every((sample) => sample.measured === true && sample.source_face_count !== null
      && sample.reference_face_count !== null && sample.visible_face_count !== null && sample.text_box_count !== null)
    && findings.length === 0;
  let safeStatus = status;
  let reason = "";
  if (!baseValid || (status === "pass" && !passValid)) {
    safeStatus = "unmeasured";
    reason = "完整畫面檢查結果不完整，無法可靠判定";
  }
  return {
    version: 1,
    scope,
    measurement,
    status: safeStatus,
    source_sha256: sourceSha,
    media_sha256: mediaSha,
    frame_size: frameValid ? frameSize : null,
    sample_count: countsValid ? sampleCount : null,
    measured_count: countsValid ? measuredCount : null,
    elapsed_s: elapsed !== null && elapsed >= 0 ? elapsed : null,
    engines: enginesValid ? engines : {},
    samples,
    findings: findingsShapeValid ? findings : [],
    reason,
  };
}

function fxPreviewCompositionFindingLabel(finding) {
  return FX_PREVIEW_COMPOSITION_FINDING_LABELS[finding?.kind] || "檢查提醒";
}

function fxPreviewCompositionStatusLabel(status) {
  return status === "pass" ? "抽樣通過" : status === "warn" ? "有提醒" : "未量測";
}

function fxPreviewCompositionJumpToIssue(outputAt, sourceAt, job, token) {
  if (token !== fxPreviewVisibleMediaToken || String(fxPreviewVisibleJob?.id || "") !== String(job?.id || "")) return;
  const pair = fxPreviewCompositionMapPair(outputAt, sourceAt, job, true);
  const source = elements["fx-preview-source-video"];
  const preview = elements["fx-preview-video"];
  if (!pair || !source || !preview) {
    fxPreviewSetStatus("這個提醒的時間目前無法可靠對應。", "error");
    return;
  }
  source.pause();
  preview.pause();
  source.currentTime = pair.sourceAt;
  preview.currentTime = pair.outputAt;
  if (elements["fx-preview-source-time"]) elements["fx-preview-source-time"].textContent = formatTime(pair.sourceAt);
  if (elements["fx-preview-effect-time"]) elements["fx-preview-effect-time"].textContent = formatTime(pair.outputAt);
  fxPreviewSetStatus(`已停在原片 ${formatTime(pair.sourceAt)}／預覽 ${formatTime(pair.outputAt)}。`, "success");
}

function fxPreviewRenderCompositionCheck(rawCheck = null, job = fxPreviewVisibleJob) {
  const panel = elements["fx-preview-composition-check"];
  const statusNode = elements["fx-preview-composition-check-status"];
  const summary = elements["fx-preview-composition-check-summary"];
  const samples = elements["fx-preview-composition-check-samples"];
  const list = elements["fx-preview-composition-check-list"];
  const switchButton = elements["fx-preview-composition-switch-timeline"];
  if (!panel || !statusNode || !summary || !samples || !list || !switchButton) return;
  panel.hidden = !job;
  panel.removeAttribute("data-status");
  statusNode.textContent = "";
  summary.textContent = "";
  samples.textContent = "";
  list.replaceChildren();
  switchButton.hidden = true;
  switchButton.disabled = false;
  if (!job) return;
  const check = fxPreviewCompositionReceipt(rawCheck, job);
  panel.dataset.status = check.status;
  statusNode.textContent = fxPreviewCompositionStatusLabel(check.status);
  const scopeSingle = check.scope === "single-effect"
    || (check.scope === "" && !fxCompareActive && fxPreviewDraft?.preview_options?.mode === "single");
  const fullTimelineRequired = check.findings.some((finding) => finding.kind === "full_timeline_required");
  if (check.status === "pass") {
    summary.textContent = `抽樣 ${check.sample_count} 個時間點，未發現明顯文字遮住人物；不代表每一格都已檢查。`;
  } else if (scopeSingle && fullTimelineRequired) {
    summary.textContent = "單一效果預覽不是完整時間軸，尚未完成完整畫面檢查。";
  } else if (check.status === "warn") {
    const count = check.findings.length;
    summary.textContent = count ? `抽樣 ${check.sample_count ?? "—"} 個時間點，發現 ${count} 項提醒。` : "抽樣檢查有提醒，請看預覽確認。";
  } else {
    summary.textContent = scopeSingle
      ? "單一效果預覽不是完整時間軸；目前無法可靠量測。"
      : FX_PREVIEW_COMPOSITION_UNMEASURED;
  }
  samples.textContent = check.sample_count === null
    ? "抽樣數量未能確認。"
    : `抽樣 ${check.sample_count} 個時間點 · 完成量測 ${check.measured_count ?? "—"} 個`
      + (check.elapsed_s === null ? "" : ` · 檢查耗時 ${check.elapsed_s.toFixed(1)} 秒`);
  const token = fxPreviewVisibleMediaToken;
  const visibleJob = deepCopy(job);
  const timedGroups = new Map();
  const untimedFindings = [];
  check.findings.slice(0, 32).forEach((finding) => {
    const hasTimes = finding.output_at !== undefined || finding.source_at !== undefined;
    const pair = hasTimes ? fxPreviewCompositionMapPair(finding.output_at, finding.source_at, visibleJob, false) : null;
    if (pair) {
      const key = `${formatTime(pair.sourceAt)}|${formatTime(pair.outputAt)}`;
      const group = timedGroups.get(key) || {
        sourceAt: pair.sourceAt,
        outputAt: pair.outputAt,
        labels: [],
      };
      const label = fxPreviewCompositionFindingLabel(finding);
      if (!group.labels.includes(label)) group.labels.push(label);
      timedGroups.set(key, group);
      return;
    }
    untimedFindings.push(finding);
  });
  [...timedGroups.values(), ...untimedFindings].forEach((entry) => {
    const timed = Object.prototype.hasOwnProperty.call(entry, "sourceAt");
    const item = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = timed ? entry.labels.join("、") : fxPreviewCompositionFindingLabel(entry);
    item.append(label);
    if (timed) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button button-secondary";
      button.textContent = `查看原片時間 ${formatTime(entry.sourceAt)}`;
      button.setAttribute("aria-label", `查看原片 ${formatTime(entry.sourceAt)}（預覽 ${formatTime(entry.outputAt)}）`);
      button.dataset.outputAt = String(entry.outputAt);
      button.dataset.sourceAt = String(entry.sourceAt);
      button.addEventListener("focus", () => button.scrollIntoView({ block: "nearest" }));
      button.addEventListener("click", () => fxPreviewCompositionJumpToIssue(
        entry.outputAt, entry.sourceAt, visibleJob, token,
      ));
      item.append(button);
    }
    list.append(item);
  });
  if (check.status === "warn" && !check.findings.length) {
    const item = document.createElement("li");
    item.textContent = "抽樣結果需要人工看預覽確認。";
    list.append(item);
  }
  const canSwitch = scopeSingle && !fxCompareActive && fxPreviewDraft?.preview_options?.mode !== "timeline";
  switchButton.hidden = !canSwitch;
  switchButton.disabled = fxCompareActive;
}

function fxPreviewSwitchToTimeline() {
  if (fxCompareActive || !fxPreviewDraft || !elements["fx-preview-mode"]) return;
  if (elements["fx-preview-mode"].value === "timeline") return;
  elements["fx-preview-mode"].value = "timeline";
  elements["fx-preview-mode"].dispatchEvent(new Event("change", { bubbles: true }));
}

function fxPreviewUpdateWindowControls() {
  if (!fxPreviewDraft) return;
  const effect = fxEffectByCode(fxPreviewDraft.code);
  elements["fx-preview-selected-name"].textContent = effect ? `#${effect.num} ${effect.name}` : "—";
  const sourceDuration = Number(duration() || 0);
  const maxStart = Math.max(0, sourceDuration - 0.1);
  const start = Math.max(0, Math.min(maxStart, Number(fxPreviewDraft.window.start_s) || 0));
  const maxDuration = fxPreviewMaxDuration(effect, start);
  fxPreviewDraft.window.start_s = start;
  const apply = fxPreviewDraft.apply_window || (fxPreviewDraft.apply_window = { start_s: start, duration_s: maxDuration });
  apply.start_s = Math.max(0, Math.min(Number(apply.start_s) || 0, Math.max(0, sourceDuration - 0.1)));
  apply.duration_s = Math.max(0.1, Math.min(sourceDuration - apply.start_s, Number(apply.duration_s) || maxDuration));
  if (start < apply.start_s) fxPreviewDraft.window.start_s = apply.start_s;
  const actualStart = fxPreviewDraft.window.start_s;
  const clipDuration = Math.max(0.1, Math.min(maxDuration, apply.start_s + apply.duration_s - actualStart, Number(fxPreviewDraft.window.duration_s) || maxDuration));
  fxPreviewDraft.window.duration_s = clipDuration;
  elements["fx-preview-source"].value = String(actualStart);
  elements["fx-preview-source"].max = String(maxStart);
  elements["fx-preview-duration"].value = String(clipDuration);
  elements["fx-preview-duration"].max = String(maxDuration);
  elements["fx-preview-window-label"].textContent = `${formatTime(actualStart)} — ${formatTime(actualStart + clipDuration)}`;
  elements["fx-preview-apply-source"].value = String(apply.start_s);
  elements["fx-preview-apply-duration"].value = String(apply.duration_s);
  elements["fx-preview-apply-window-label"].textContent = `${formatTime(apply.start_s)} — ${formatTime(apply.start_s + apply.duration_s)}`;
  elements["fx-preview-mode"].value = fxPreviewDraft.preview_options?.mode || "single";
  elements["fx-preview-quality"].value = fxPreviewDraft.preview_options?.quality || "draft";
  elements["fx-preview-text-fit"].checked = fxPreviewDraft.text_fit !== false;
  const overlay = fxPreviewReplaceOverlay();
  const originalDuration = overlay ? Math.max(0, Number(overlay.end) - Number(overlay.start)) : 0;
  const note = elements["fx-preview-replace-note"];
  if (note) {
    note.hidden = !overlay;
    if (!note.hidden) note.textContent = `原特效範圍 ${originalDuration.toFixed(2)} 秒；試看片段不會縮短實際套用範圍。`;
  }
}

function fxPreviewSetVisibleMedia(job) {
  fxPreviewVisibleMediaToken += 1;
  fxPreviewVisibleJob = job ? deepCopy(job) : null;
  if (!job) {
    fxPreviewVisibleMeta = null;
    return;
  }
  const renderDuration = Number(job.render_duration_s);
  fxPreviewVisibleMeta = {
    render_duration_s: Number.isFinite(renderDuration) ? renderDuration : null,
    time_map: Array.isArray(job.time_map) ? deepCopy(job.time_map) : [],
    text_layout: job.text_layout && typeof job.text_layout === "object" ? deepCopy(job.text_layout) : null,
    composition_check: job.composition_check && typeof job.composition_check === "object"
      ? deepCopy(job.composition_check) : null,
  };
  fxPreviewRenderTextLayout(fxPreviewVisibleMeta.text_layout);
  fxPreviewRenderCompositionCheck(fxPreviewVisibleMeta.composition_check, fxPreviewVisibleJob);
}

function fxPreviewClearVisibleMedia() {
  fxPreviewVisibleMediaToken += 1;
  fxPreviewVisibleJob = null;
  fxPreviewVisibleMeta = null;
  fxPreviewRenderTextLayout(null);
  fxPreviewRenderCompositionCheck(null, null);
}

function fxPreviewSourceTimeForEffectTime(effectTime) {
  const map = Array.isArray(fxPreviewVisibleMeta?.time_map) ? fxPreviewVisibleMeta.time_map : [];
  const point = Math.max(0, Number(effectTime) || 0);
  const mapped = fxPreviewCompositionMapOutput(point, fxPreviewVisibleJob);
  if (mapped !== null) return mapped;
  if (map.length) return null;
  return Math.max(0, Number(fxPreviewDraft?.window?.start_s || 0) + Math.max(0, Number(effectTime) || 0));
}

function fxPreviewSetSubjectFromSourceClick(event) {
  if (fxPreviewDraft?.code !== "P5") return;
  const video = elements["fx-preview-source-video"];
  const rect = video.getBoundingClientRect();
  const videoRatio = Number(video.videoWidth) / Math.max(1, Number(video.videoHeight));
  const boxRatio = rect.width / Math.max(1, rect.height);
  if (!Number.isFinite(videoRatio) || videoRatio <= 0) return;
  const contentWidth = videoRatio > boxRatio ? rect.width : rect.height * videoRatio;
  const contentHeight = videoRatio > boxRatio ? rect.width / videoRatio : rect.height;
  const left = rect.left + (rect.width - contentWidth) / 2;
  const top = rect.top + (rect.height - contentHeight) / 2;
  if (event.clientX < left || event.clientX > left + contentWidth || event.clientY < top || event.clientY > top + contentHeight) {
    fxPreviewSetStatus("請點選原片畫面範圍，黑邊不會設定主角位置。", "error");
    return;
  }
  fxPreviewDraft.subject = {
    x: Math.max(0, Math.min(1, (event.clientX - left) / contentWidth)),
    y: Math.max(0, Math.min(1, (event.clientY - top) / contentHeight)),
  };
  fxPreviewSetStatus("已設定主角位置；可重新試看確認。", "success");
  fxPreviewDraftChanged();
}

function fxPreviewSyncVideos(origin = "effect") {
  const source = elements["fx-preview-source-video"];
  const effect = elements["fx-preview-video"];
  if (!source || !effect || !fxPreviewDraft || fxPreviewSyncing) return;
  fxPreviewSyncing = true;
  const sourceTime = origin === "source"
    ? Math.max(0, Number(source.currentTime) || 0)
    : fxPreviewSourceTimeForEffectTime(effect.currentTime);
  const effectTime = origin === "source"
    ? fxPreviewCompositionMapSource(sourceTime, fxPreviewVisibleJob)
      ?? (Array.isArray(fxPreviewVisibleMeta?.time_map) && fxPreviewVisibleMeta.time_map.length
        ? null : Math.max(0, sourceTime - Number(fxPreviewDraft.window.start_s || 0)))
    : Math.max(0, Number(effect.currentTime) || 0);
  if (sourceTime === null || effectTime === null) {
    fxPreviewSyncing = false;
    return;
  }
  if (Math.abs(Number(source.currentTime || 0) - sourceTime) > 0.08) source.currentTime = sourceTime;
  if (Math.abs(Number(effect.currentTime || 0) - effectTime) > 0.08) effect.currentTime = effectTime;
  if (elements["fx-preview-source-time"]) elements["fx-preview-source-time"].textContent = formatTime(sourceTime);
  if (elements["fx-preview-effect-time"]) elements["fx-preview-effect-time"].textContent = formatTime(effectTime);
  setTimeout(() => { fxPreviewSyncing = false; }, 0);
}

function fxPreviewPrepareSourceVideo() {
  const source = elements["fx-preview-source-video"];
  if (!source || !sourceMediaUrl) return;
  const mediaToken = fxPreviewVisibleMediaToken;
  const visibleJobId = String(fxPreviewVisibleJob?.id || "");
  const expectedSourceUrl = new URL(sourceMediaUrl, location.href).href;
  if (source.src !== new URL(sourceMediaUrl, location.href).href) {
    source.src = sourceMediaUrl;
    source.load();
  }
  const setStart = () => {
    if (mediaToken !== fxPreviewVisibleMediaToken || visibleJobId !== String(fxPreviewVisibleJob?.id || "")
      || source.src !== expectedSourceUrl) return;
    fxPreviewSyncVideos("effect");
  };
  if (source.readyState >= 1) setStart();
  else source.addEventListener("loadedmetadata", setStart, { once: true });
}

function fxPreviewPlayBoth(origin = "effect") {
  const source = elements["fx-preview-source-video"];
  const effect = elements["fx-preview-video"];
  fxPreviewSyncVideos(origin);
  if (origin === "source") effect.play().catch(() => {});
  else source.play().catch(() => {});
}

function fxPreviewPauseBoth(origin = "effect") {
  const source = elements["fx-preview-source-video"];
  const effect = elements["fx-preview-video"];
  if (origin === "source") effect.pause();
  else source.pause();
}

function fxPreviewRenderCatalog() {
  const catalog = fxCatalog();
  const categories = catalog.categories || {};
  const tabs = [["favorites", "我的收藏"], ["recent", "最近試用"], ["", "全部"], ...Object.entries(categories)];
  elements["fx-preview-cats"].replaceChildren(...tabs.map(([key, label]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.setAttribute("role", "tab");
    const selected = key === "favorites" || key === "recent"
      ? key === fxPreviewLibraryFilter
      : !fxPreviewLibraryFilter && key === activeFxPreviewCategory;
    button.setAttribute("aria-selected", String(selected));
    button.className = selected ? "is-selected" : "";
    button.textContent = key && key !== "favorites" && key !== "recent" ? `${key} ${label}` : label;
    button.addEventListener("click", () => {
      if (key === "favorites" || key === "recent") {
        fxPreviewLibraryFilter = key;
        activeFxPreviewCategory = "";
      } else {
        fxPreviewLibraryFilter = "";
        activeFxPreviewCategory = key;
      }
      fxPreviewRenderCatalog();
    });
    return button;
  }));
  const query = String(elements["fx-preview-search"].value || "").trim().toLowerCase();
  const preferences = fxPreviewPreferences();
  const list = fxEffects().filter((effect) => (!activeFxPreviewCategory || effect.category === activeFxPreviewCategory)
    && (!fxPreviewLibraryFilter || (fxPreviewLibraryFilter === "favorites"
      ? preferences.favorites.includes(effect.code)
      : preferences.recent.includes(effect.code)))
    && (!query || [effect.code, effect.name, effect.spec].join(" ").toLowerCase().includes(query)));
  elements["fx-preview-grid"].replaceChildren(...list.map((effect) => {
    const selected = effect.code === fxPreviewDraft?.code;
    const button = document.createElement("div");
    button.className = `fx-card fx-preview-card${selected ? " is-selected" : ""}`;
    button.setAttribute("role", "radio");
    button.setAttribute("aria-checked", String(selected));
    button.tabIndex = 0;
    button.dataset.fxCode = effect.code;
    button.title = fxPreviewIsBlocked(effect)
      ? `#${effect.num} ${effect.name}\n需要B-roll素材，請先至素材區處理`
      : `#${effect.num} ${effect.name}\n卡片為風格示意；選取後用本片試看`;
    const thumb = fxThumbUrl(effect);
    if (thumb) {
      const img = document.createElement("img");
      img.loading = "lazy";
      img.src = thumb;
      img.alt = `${effect.name} 風格示意`;
      button.append(img);
    }
    const code = document.createElement("span");
    code.className = "fx-code";
    code.textContent = effect.code;
    button.append(code);
    if (effect.matte) {
      const m = document.createElement("span");
      m.className = "fx-m";
      m.textContent = "ⓜ";
      m.title = "需要人物去背";
      button.append(m);
    }
    const name = document.createElement("span");
    name.className = "fx-name";
    name.textContent = effect.name;
    button.append(name);
    button.addEventListener("click", () => fxPreviewSelectEffect(effect.code));
    button.addEventListener("keydown", (event) => {
      if (event.target !== button) return;
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        fxPreviewSelectEffect(effect.code);
      }
    });
    const favorite = document.createElement("button");
    favorite.type = "button";
    favorite.className = `fx-preview-favorite${fxPreviewIsFavorite(effect.code) ? " is-favorite" : ""}`;
    favorite.setAttribute("aria-label", `${fxPreviewIsFavorite(effect.code) ? "取消收藏" : "收藏"} ${effect.name}`);
    favorite.setAttribute("aria-pressed", String(fxPreviewIsFavorite(effect.code)));
    favorite.textContent = fxPreviewIsFavorite(effect.code) ? "★" : "☆";
    favorite.addEventListener("click", (event) => {
      event.stopPropagation();
      fxPreviewToggleFavorite(effect.code);
    });
    button.append(favorite);
    return button;
  }));
}

function fxPreviewRenderFields() {
  const effect = fxEffectByCode(fxPreviewDraft?.code);
  if (!effect || !fxPreviewDraft) {
    elements["fx-preview-fields"].replaceChildren();
    return;
  }
  const rows = [];
  const advancedRows = [];
  if (effect.code === "B1") {
    const label = document.createElement("label");
    label.className = "field fx-preview-field";
    const span = document.createElement("span");
    span.textContent = "已匯入素材";
    const select = document.createElement("select");
    select.innerHTML = '<option value="">選擇已匯入素材</option>';
    label.append(span, select);
    rows.push(label);
    request("/api/assets/library").then((payload) => {
      const assets = Array.isArray(payload.assets) ? payload.assets : [];
      assets.filter((asset) => asset?.asset_id && asset?.sha256).forEach((asset) => {
        const option = document.createElement("option");
        option.value = asset.asset_id;
        option.textContent = String(asset.path || asset.asset_id).split("/").pop();
        option.dataset.sha256 = asset.sha256;
        select.append(option);
      });
      select.value = fxPreviewDraft?.material?.asset_id || "";
    }).catch(() => {});
    select.addEventListener("change", () => {
      const option = select.selectedOptions[0];
      fxPreviewDraft.material = select.value ? { asset_id: select.value, sha256: option?.dataset.sha256 || "" } : null;
      fxPreviewDraftChanged();
    });
  }
  const textVars = (effect.vars || []).filter((item) => item.kind === "text");
  textVars.forEach((item) => {
    const label = document.createElement("label");
    label.className = "field fx-preview-field";
    const span = document.createElement("span");
    span.textContent = item.label || item.id;
    const input = document.createElement("input");
    input.type = "text";
    input.maxLength = 200;
    input.value = fxPreviewDraft.values?.[item.id] ?? item.default ?? "";
    input.addEventListener("input", () => {
      fxPreviewDraft.values[item.id] = input.value;
      fxPreviewDraftChanged();
    });
    label.append(span, input);
    rows.push(label);
  });
  const placement = document.createElement("label");
  placement.className = "field fx-preview-field";
  const placementLabel = document.createElement("span");
  placementLabel.textContent = "擺位（避開臉）";
  const placementSelect = document.createElement("select");
  [["", "跟隨效果"], ["top", "上"], ["bottom", "下"], ["left", "左"], ["right", "右"]].forEach(([value, label]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    placementSelect.append(option);
  });
  placementSelect.value = fxPreviewDraft.placement || "";
  placementSelect.addEventListener("change", () => {
    fxPreviewDraft.placement = placementSelect.value;
    fxPreviewDraftChanged();
  });
  placement.append(placementLabel, placementSelect);
  rows.push(placement);

  const opacityLabel = document.createElement("label");
  opacityLabel.className = "field fx-preview-field fx-preview-opacity";
  const opacityTitle = document.createElement("span");
  opacityTitle.textContent = "特效透明度";
  const opacityOutput = document.createElement("output");
  const opacityInput = document.createElement("input");
  opacityInput.type = "range";
  opacityInput.min = "0";
  opacityInput.max = "100";
  opacityInput.step = "1";
  opacityInput.value = String(Math.round(fxPreviewDraft.opacity * 100));
  opacityInput.setAttribute("aria-label", "特效透明度");
  opacityOutput.textContent = `${opacityInput.value}%`;
  opacityInput.addEventListener("input", () => {
    fxPreviewDraft.opacity = Number(opacityInput.value) / 100;
    opacityOutput.textContent = `${opacityInput.value}%`;
    fxPreviewDraftChanged();
  });
  opacityLabel.append(opacityTitle, opacityOutput, opacityInput);
  rows.push(opacityLabel);

  const scheme = document.createElement("label");
  scheme.className = "field fx-preview-field";
  const schemeLabel = document.createElement("span");
  schemeLabel.textContent = `字卡色組（主題 ${fxThemeNo()}）`;
  const schemeSelect = document.createElement("select");
  [["", "跟隨風格包"], ...Object.entries(fxCatalog().schemes || {})].forEach(([value, label]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    schemeSelect.append(option);
  });
  schemeSelect.value = fxPreviewDraft.scheme || "";
  schemeSelect.addEventListener("change", () => {
    fxPreviewDraft.scheme = schemeSelect.value;
    fxPreviewDraftChanged();
  });
  scheme.append(schemeLabel, schemeSelect);
  advancedRows.push(scheme);

  const fonts = (fxCatalog().fonts || []).filter((font) => font.verified !== false);
  if (fonts.length) {
    ["display", "body"].forEach((role) => {
      const label = document.createElement("label");
      label.className = "field fx-preview-field";
      const span = document.createElement("span");
      span.textContent = role === "display" ? "標題字型（已驗證）" : "內文字型（已驗證）";
      const select = document.createElement("select");
      const inherit = document.createElement("option");
      inherit.value = "";
      inherit.textContent = "跟隨風格包";
      select.append(inherit);
      fonts.forEach((font) => {
        const option = document.createElement("option");
        option.value = font.id;
        option.textContent = font.label || font.id;
        option.style.fontFamily = font.family || "inherit";
        select.append(option);
      });
      select.value = fxPreviewDraft.font_ids?.[role] || "";
      select.addEventListener("change", () => {
        fxPreviewDraft.font_ids = fxPreviewDraft.font_ids || {};
        if (select.value) fxPreviewDraft.font_ids[role] = select.value;
        else delete fxPreviewDraft.font_ids[role];
        fxPreviewDraftChanged();
      });
      label.append(span, select);
      advancedRows.push(label);
    });
  }
  elements["fx-preview-fields"].replaceChildren(...rows);
  elements["fx-preview-advanced-fields"].replaceChildren(...advancedRows);
}

function fxPreviewRenderDialog() {
  const effect = fxEffectByCode(fxPreviewDraft?.code);
  if (!effect || !fxPreviewDraft) return;

  const notes = [effect.spec || "", effect.matte ? "ⓜ 需要人物去背" : ""].filter(Boolean);
  elements["fx-preview-spec"].textContent = notes.join("｜");
  fxPreviewUpdateWindowControls();
  fxPreviewRenderFields();
  fxPreviewRenderCatalog();
}

function fxPreviewStatusForJob(job) {
  const stateName = String(job?.state || "pending").toLowerCase();
  const labels = {
    pending: "預覽排隊中（待渲染，不是即時完成）",
    running: "預覽渲染中",
    matting: "人物去背處理中",
    rendering: "預覽渲染中",
    ready: "預覽已完成",
    cache_hit: "預覽已完成（快取）",
    failed: "預覽失敗",
    cancelled: "預覽已取消",
  };
  let message = labels[stateName] || `預覽狀態：${stateName}`;
  if (job?.cache_hit && stateName !== "cache_hit") message += "（快取）";
  const phaseLabels = {
    pending: "排隊中",
    queued: "排隊中",
    cancelling: "取消中",
    running: "處理中",
    matting: "人物去背中",
    matte: "人物去背中",
    preparing: "準備中",
    composing: "準備效果與圖層",
    encoding: "編碼中",
    validating: "驗證中",
    rendering: "渲染中",
    ready: "完成",
    cache_hit: "快取完成",
  };
  if (job?.phase && ![stateName, "ready", "cache_hit"].includes(String(job.phase))) {
    message += ` · ${phaseLabels[String(job.phase)] || "處理中"}`;
  }
  if (job?.message) message += `：${job.message}`;
  message += fxPreviewTimingSuffix(job);
  if (job?.error && stateName === "failed") {
    const error = typeof job.error === "string"
      ? job.error
      : job.error.message || job.error.detail || job.error.error || JSON.stringify(job.error);
    message += `：${error}`;
  }
  return message;
}

function fxPreviewTimingSuffix(job, label = "已用") {
  if (typeof job?.elapsed_s !== "number" || !Number.isFinite(job.elapsed_s)) return "";
  let suffix = ` · ${label} ${Math.round(job.elapsed_s)} 秒`;
  if (job.state === "running" && job.phase !== "cancelling" && !job.error) {
    const eta = job.eta_remaining_s;
    suffix += typeof eta === "number" && Number.isFinite(eta)
      ? ` · 預估還需 ${Math.ceil(eta)} 秒（依近期同設定估算）`
      : Number.isFinite(job.eta_sample_count) && job.eta_sample_count >= 3
        ? " · 已超過近期預估，仍在處理" : " · 預估時間計算中";
  }
  return suffix;
}

function fxPreviewApplyJob(job, generation, signature) {
  if (generation !== fxPreviewGeneration || signature !== fxPreviewDraftSignature || !elements["fx-preview-dialog"].open) return false;
  fxPreviewJob = job || null;
  const stateName = String(job?.state || "pending").toLowerCase();
  const ready = ["ready", "cache_hit"].includes(stateName) && Boolean(job?.media_url);
  const failed = stateName === "failed";
  const quality = String(job?.preview_options?.quality || fxPreviewDraft?.preview_options?.quality || "draft");
  const upgrading = quality === "standard" && !ready && !failed;
  fxPreviewSetStatus(failed && quality === "standard"
    ? `清晰版預覽失敗；快速版仍可播放，請手動重試清晰版。${fxPreviewTimingSuffix(job, "清晰版耗時")}`
    : ready && quality === "draft"
      ? `快速版預覽可播放；正在背景準備清晰版，完成後才能套用。${fxPreviewTimingSuffix(job, "快速版耗時")}`
      : fxPreviewStatusForJob(job), failed ? "error" : ready ? "success" : "");
  elements["fx-preview-retry"].hidden = !failed;
  elements["fx-preview-cancel-upgrade"].hidden = !upgrading;
  fxPreviewReadySignature = ready && quality === "standard" ? signature : "";
  elements["fx-preview-confirm"].disabled = !ready || fxPreviewReadySignature !== fxPreviewDraftSignature;
  if (ready) {
    // A ready job is the only job allowed to become the media currently shown
    // in the player. A standard upgrade can otherwise replace fxPreviewJob
    // while the draft media remains on screen, which would make seeking use
    // the wrong time map.
    fxPreviewSetVisibleMedia(job);
    const video = elements["fx-preview-video"];
    const nextUrl = String(job.media_url);
    if (video.src !== new URL(nextUrl, location.href).href) {
      video.src = nextUrl;
      video.load();
    }
    fxPreviewPrepareSourceVideo();
    fxPreviewSyncVideos("effect");
    fxPreviewPlayBoth("effect");
    if (quality === "draft" && !fxPreviewUpgradeStarted && !fxPreviewUpgradeCancelled) {
      fxPreviewUpgradeStarted = true;
      setTimeout(() => fxPreviewStartStandardUpgrade(generation, signature), 0);
    }
  }
  return true;
}

function fxPreviewStartStandardUpgrade(generation, signature) {
  if (generation !== fxPreviewGeneration || signature !== fxPreviewDraftSignature || !fxPreviewDraft || !elements["fx-preview-dialog"]?.open || fxPreviewUpgradeCancelled) return;
  fxPreviewDraft.preview_options.quality = "standard";
  fxPreviewPersistDraft();
  elements["fx-preview-confirm"].disabled = true;
  elements["fx-preview-cancel-upgrade"].hidden = false;
  fxPreviewSetStatus("快速版預覽可播放；正在背景準備清晰版。");
  fxPreviewStart(generation, signature, true);
}

function fxPreviewCancelCurrentJob() {
  clearTimeout(fxPreviewPollTimer);
  fxPreviewPollTimer = null;
  const job = fxPreviewJob;
  fxPreviewJob = null;
  if (!job?.id) return Promise.resolve(null);
  if (fxPreviewJobTerminationVerified(job)) {
    if (fxPreviewUnverifiedJob?.id === job.id) fxPreviewUnverifiedJob = null;
    return Promise.resolve(job);
  }
  return fxCompareCancelAndVerify(job).then((result) => {
    if (result.verified) {
      if (fxPreviewUnverifiedJob?.id === job.id) fxPreviewUnverifiedJob = null;
      return result.job;
    }
    fxPreviewUnverifiedJob = result.job || job;
    throw result.error || new Error("預覽工作停止尚未確認");
  });
}

// The backend may spend up to about 5.2s verifying TERM/KILL and process-group
// exit. Give that local operation margin, but never let a missing HTTP response
// hold the UI recovery promise indefinitely. Status lookups do not terminate a
// process and therefore use a shorter per-attempt deadline.
const FX_PREVIEW_CANCEL_REQUEST_TIMEOUT_MS = 6500;
const FX_PREVIEW_CANCEL_STATUS_TIMEOUT_MS = 2000;

async function fxPreviewRequestWithDeadline(url, options = {}, timeoutMs = FX_PREVIEW_CANCEL_STATUS_TIMEOUT_MS) {
  const controller = new AbortController();
  const boundedMs = Math.max(50, Number(timeoutMs) || FX_PREVIEW_CANCEL_STATUS_TIMEOUT_MS);
  const timeout = setTimeout(() => controller.abort(), boundedMs);
  try {
    // request() includes JSON body consumption, so the signal and timer cover
    // both response headers and a body that stalls after headers arrive.
    const payload = await request(url, { ...options, signal: controller.signal });
    // request() intentionally converts JSON parse failures into an error
    // payload. If abort happened while response.json() was pending, do not let
    // that conversion turn a timed-out body into a successful cancellation.
    if (controller.signal.aborted) throw new DOMException("request deadline exceeded", "AbortError");
    return payload;
  } catch (error) {
    if (!controller.signal.aborted) throw error;
    const timeoutError = new Error("預覽請求逾時，工作狀態尚未確認");
    timeoutError.name = "TimeoutError";
    timeoutError.code = "request_timeout";
    timeoutError.reason = "request_timeout";
    timeoutError.cause = error;
    throw timeoutError;
  } finally {
    clearTimeout(timeout);
  }
}

function fxPreviewCancelJobId(jobId, timeoutMs = FX_PREVIEW_CANCEL_REQUEST_TIMEOUT_MS) {
  if (!jobId) return Promise.resolve(null);
  return fxPreviewRequestWithDeadline(`/api/fx-previews/${encodeURIComponent(jobId)}/cancel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  }, timeoutMs).then((payload) => payload?.job || payload);
}

function fxPreviewTrackLifecycle(promise) {
  fxPreviewLifecycles.add(promise);
  fxPreviewPostInFlight = promise;
  const cleanup = () => {
    fxPreviewLifecycles.delete(promise);
    fxPreviewPostInFlight = fxPreviewLifecycles.size ? Promise.allSettled([...fxPreviewLifecycles]) : null;
  };
  promise.then(cleanup, cleanup);
  return promise;
}

async function fxPreviewWaitForLifecycles() {
  while (fxPreviewLifecycles.size) await Promise.allSettled([...fxPreviewLifecycles]);
}

function fxPreviewJobState(job) {
  return String(job?.state || "pending").toLowerCase();
}

function fxPreviewJobIsTerminal(job) {
  return ["ready", "cache_hit", "failed", "cancelled", "stale"].includes(fxPreviewJobState(job));
}

function fxPreviewJobTerminationVerified(job) {
  if (!fxPreviewJobIsTerminal(job)) return false;
  return String(job?.error?.code || "").toLowerCase() !== "termination_unverified";
}

function fxPreviewErrorIsTypedJobAbsent(error) {
  if (Number(error?.status) !== 404) return false;
  return String(error?.reason || error?.code || "").toLowerCase() === "job_not_found";
}

async function fxCompareCancelAndVerify(
  job,
  candidate = null,
  attempts = 3,
  forceTerminalCancel = false,
  requestTimeoutMs = FX_PREVIEW_CANCEL_REQUEST_TIMEOUT_MS,
  statusTimeoutMs = Math.min(requestTimeoutMs, FX_PREVIEW_CANCEL_STATUS_TIMEOUT_MS),
) {
  if (!job?.id) return { verified: true, job: null };
  if (!forceTerminalCancel && fxPreviewJobTerminationVerified(job)) return { verified: true, job };
  let current = job;
  try {
    current = await fxPreviewCancelJobId(job.id, requestTimeoutMs) || current;
    if (!current?.id) current = { ...current, id: job.id, status_url: job.status_url };
  } catch (error) {
    if (fxPreviewErrorIsTypedJobAbsent(error)) {
      if (candidate) {
        candidate.job = null;
        candidate.state = "failed";
        candidate.message = "工作已不在目前預覽管理器中；本輪已停止排程。";
      }
      // This proves only that the current API manager no longer owns the job;
      // it is not a claim about an OS process from an older server instance.
      return { verified: true, absent: true, job: null };
    }
    if (candidate) candidate.message = `停止請求失敗：${error.message}`;
    return { verified: false, job: current, error };
  }
  for (let attempt = 0; attempt < attempts && !fxPreviewJobTerminationVerified(current); attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 180));
    try {
      const payload = await fxPreviewRequestWithDeadline(
        current.status_url || `/api/fx-previews/${encodeURIComponent(current.id)}`,
        {},
        statusTimeoutMs,
      );
      current = payload?.job || payload || current;
    } catch (error) {
      if (candidate) candidate.message = `停止狀態無法確認：${error.message}`;
      return { verified: false, job: current, error };
    }
  }
  if (candidate) {
    candidate.job = current;
    candidate.state = fxPreviewJobTerminationVerified(current) ? fxPreviewJobState(current) : "stopping";
    candidate.message = fxPreviewJobTerminationVerified(current)
      ? fxPreviewStatusForJob(current)
      : "停止尚未確認；工作仍可能在執行，請重試停止。";
  }
  return { verified: fxPreviewJobTerminationVerified(current), job: current };
}

function fxCompareCandidateUsable(draft) {
  const effect = fxEffectByCode(draft?.code);
  if (!effect || effect.available === false) return "此效果目前不可用。";
  if (fxPreviewIsBlocked(effect)) return "B1 需要B-roll素材，不能加入比較。";
  if (effect.code === "P5" && !draft?.subject) return "P5 需要先在原片點選主角位置，不能加入比較。";
  const limit = Number(effect.media_max);
  if (Number.isFinite(limit) && limit > 0 && Number(draft?.window?.duration_s) > limit + 0.001) {
    return `此效果最多試看 ${limit} 秒；比較不會暗中縮短片段。`;
  }
  return "";
}

function fxCompareContextForDraft(draft) {
  return {
    revision: String(state?.revision || ""),
    source_sha256: String(state?.source_sha256 || projectPayload?.source_sha256 || ""),
    replace_overlay_id: fxPreviewReplaceOverlayId || null,
    window: deepCopy(draft?.window || {}),
    apply_window: deepCopy(draft?.apply_window || {}),
    mode: draft?.preview_options?.mode === "timeline" ? "timeline" : "single",
  };
}

function fxCompareSameContext(left, right) {
  if (!left || !right) return false;
  return left.revision === right.revision
    && left.source_sha256 === right.source_sha256
    && left.replace_overlay_id === right.replace_overlay_id
    && left.mode === right.mode
    && JSON.stringify(left.window) === JSON.stringify(right.window)
    && JSON.stringify(left.apply_window) === JSON.stringify(right.apply_window);
}

function fxCompareRoundLocked() {
  return Boolean(fxCompareActive && (fxCompareStarting || fxCompareRunning || fxCompareStopping || fxCompareConfirming
    || fxCompareCandidates.some((item) => item.state !== "queued")));
}

function fxCompareResetState() {
  fxCompareActive = false;
  fxCompareCandidates = [];
  fxCompareFrozen = null;
  fxCompareStarting = false;
  fxCompareRunning = false;
  fxCompareStopping = false;
  fxCompareConfirming = false;
  fxCompareSelectedId = "";
  fxCompareSelectionToken += 1;
  fxCompareCloseRequested = false;
  fxCompareRoundStale = false;
  fxCompareRun += 1;
  fxCompareSession += 1;
  fxPreviewJob = null;
  fxPreviewReadySignature = "";
  if (elements["fx-preview-confirm"]) elements["fx-preview-confirm"].disabled = true;
}

function fxCompareSetInteractionLocks() {
  const roundLocked = fxCompareRoundLocked();
  const confirmationLocked = fxCompareConfirming;
  ["fx-preview-search", "fx-preview-source", "fx-preview-duration", "fx-preview-apply-source",
    "fx-preview-apply-duration", "fx-preview-mode", "fx-preview-quality", "fx-preview-text-fit",
    "fx-preview-restore-draft", "fx-preview-discard-draft", "fx-preview-composition-switch-timeline"].forEach((id) => {
    if (elements[id]) elements[id].disabled = roundLocked;
  });
  document.querySelectorAll("#fx-preview-fields input, #fx-preview-fields select, #fx-preview-advanced-fields input, #fx-preview-advanced-fields select")
    .forEach((node) => { node.disabled = roundLocked; });
  document.querySelectorAll("#fx-preview-grid .fx-preview-card, #fx-preview-cats button, #fx-preview-grid .fx-preview-favorite")
    .forEach((node) => {
      node.setAttribute("aria-disabled", String(roundLocked));
      if (node instanceof HTMLButtonElement) node.disabled = roundLocked;
    });
  ["fx-preview-close", "fx-preview-cancel"].forEach((id) => {
    if (elements[id]) elements[id].disabled = confirmationLocked;
  });
}

function fxCompareRender() {
  const note = elements["fx-preview-compare-note"];
  const list = elements["fx-preview-compare-candidates"];
  if (!note || !list) return;
  const active = fxCompareActive;
  const started = fxCompareCandidates.some((item) => item.state !== "queued");
  elements["fx-preview-compare-toggle"].setAttribute("aria-pressed", String(active));
  elements["fx-preview-compare-toggle"].textContent = active ? "離開比較" : "比較風格";
  ["fx-preview-compare-add", "fx-preview-compare-start", "fx-preview-compare-discard"].forEach((id) => { elements[id].hidden = !active; });
  elements["fx-preview-compare-add"].disabled = !active || started || fxCompareStarting || fxCompareRunning || fxCompareStopping || fxCompareConfirming || fxCompareCandidates.length >= 4;
  elements["fx-preview-compare-start"].disabled = !active || started || fxCompareStarting || fxCompareRunning || fxCompareStopping || fxCompareConfirming || fxCompareRoundStale || fxCompareCandidates.length < 3;
  elements["fx-preview-compare-discard"].disabled = fxCompareConfirming;
  elements["fx-preview-compare-discard"].textContent = fxCompareStopping ? "重試停止" : "丟棄比較";
  note.hidden = !active;
  note.textContent = !active ? "" : fxCompareStopping ? "比較仍停止中；確認工作已終止前不會開始下一輪。"
    : fxCompareConfirming ? "正在套用已選候選；完成前已鎖定切換、關閉與歷史操作。"
      : fxCompareStarting ? "正在確認單款工作已停止與凍結版本；尚未送出比較工作。"
        : fxCompareRoundStale ? "比較內容已過期；整輪不可套用，請丟棄後重新建立。"
          : fxCompareRunning ? "正依序產生比較，伺服器一次只會有一個工作。"
      : `已加入 ${fxCompareCandidates.length}/4 款；同一來源、試看片段與套用範圍會在開始後凍結。`;
  list.replaceChildren(...fxCompareCandidates.map((candidate, index) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "fx-compare-card";
    card.dataset.fxCompareId = candidate.id;
    card.dataset.state = candidate.state;
    if (candidate.job?.id) card.dataset.jobId = candidate.job.id;
    card.setAttribute("aria-pressed", String(candidate.id === fxCompareSelectedId));
    card.disabled = candidate.state !== "ready" || fxCompareStarting || fxCompareRunning || fxCompareStopping || fxCompareConfirming || fxCompareRoundStale;
    const effect = fxEffectByCode(candidate.draft.code);
    const title = document.createElement("strong");
    title.textContent = `${index + 1}. ${effect ? `#${effect.num} ${effect.name}` : candidate.draft.code}`;
    const status = document.createElement("span");
    status.textContent = candidate.message || fxPreviewStatusForJob(candidate.job || { state: candidate.state });
    card.append(title, status);
    card.addEventListener("click", () => fxCompareSelect(candidate.id));
    return card;
  }));
  fxCompareSetInteractionLocks();
  if (fxPreviewVisibleMeta) fxPreviewRenderCompositionCheck(fxPreviewVisibleMeta.composition_check, fxPreviewVisibleJob);
}

function fxCompareEnter() {
  if (fxCompareActive) { fxCompareDiscard(); return; }
  if (fxCompareCleanupPromise || fxCompareStopping) {
    fxPreviewSetStatus("上一輪仍在確認停止，暫時不能開始新比較。", "error");
    return;
  }
  fxPreviewGeneration += 1;
  clearTimeout(fxPreviewDebounceTimer); clearTimeout(fxPreviewPollTimer);
  fxPreviewDebounceTimer = null; fxPreviewPollTimer = null;
  fxCompareActive = true;
  fxCompareSession += 1;
  fxCompareCandidates = [];
  fxCompareFrozen = null;
  fxCompareStarting = false;
  fxCompareConfirming = false;
  fxCompareRoundStale = false;
  fxCompareSelectedId = "";
  fxCompareSelectionToken += 1;
  fxPreviewReadySignature = "";
  elements["fx-preview-confirm"].disabled = true;
  fxPreviewCancelPromise = fxPreviewTrackLifecycle(fxPreviewCancelCurrentJob());
  fxPreviewCancelPromise.catch((error) => fxPreviewSetStatus(`單款預覽停止失敗：${error.message}`, "error"));
  fxPreviewSetStatus("比較模式：調整候選後按「加入比較」；準備期間不會產生預覽。", "success");
  fxCompareRender();
}

function fxCompareDiscard() {
  if (fxCompareStarting || fxCompareRunning || fxCompareStopping || fxCompareCandidates.some((item) => item.job?.id)) {
    void fxCompareStop();
    return;
  }
  fxCompareResetState();
  fxCompareRender();
  fxPreviewSetStatus("已丟棄比較候選，回到單一風格試看。");
}

function fxCompareAdd() {
  if (!fxCompareActive || !fxPreviewDraft || fxCompareCandidates.some((item) => item.state !== "queued") || fxCompareStarting || fxCompareRunning || fxCompareStopping || fxCompareConfirming || fxCompareCandidates.length >= 4) return;
  const reason = fxCompareCandidateUsable(fxPreviewDraft);
  if (reason) { fxPreviewSetStatus(reason, "error"); return; }
  const draft = deepCopy(fxPreviewDraft);
  const context = fxCompareContextForDraft(draft);
  if (!fxCompareFrozen) fxCompareFrozen = context;
  else if (!fxCompareSameContext(context, fxCompareFrozen)) {
    fxPreviewSetStatus("候選必須保留首款凍結的版本、來源、替換目標、試看／套用範圍與預覽模式。", "error");
    return;
  }
  const signature = JSON.stringify(draft);
  if (fxCompareCandidates.some((item) => item.signature === signature)) {
    fxPreviewSetStatus("這款設定已在比較清單中。", "error"); return;
  }
  fxCompareCandidates.push({ id: `fx-compare-${Date.now()}-${fxCompareCandidates.length}`, draft, signature, state: "queued", job: null, message: "等待開始" });
  fxCompareRender();
}

function fxCompareRequestBody(candidate, anchorId, frozen) {
  const draft = candidate.draft;
  const body = {
    schema_version: 1, expected_revision: frozen.revision,
    candidate: { code: draft.code, window: deepCopy(draft.window), apply_window: deepCopy(draft.apply_window), values: deepCopy(draft.values || {}), placement: draft.placement || "", scheme: draft.scheme || "", font_ids: deepCopy(draft.font_ids || {}), text_fit: draft.text_fit !== false },
    opacity: draft.opacity, preview_options: { ...(draft.preview_options || {}), mode: frozen.mode, quality: "standard" },
  };
  if (draft.code === "P5") body.candidate.subject = deepCopy(draft.subject);
  if (draft.code === "B1") body.candidate.material = deepCopy(draft.material);
  if (frozen.replace_overlay_id) body.replace_overlay_id = frozen.replace_overlay_id;
  if (anchorId) body.comparison_anchor_job_id = anchorId;
  return body;
}

async function fxCompareStart() {
  if (!fxCompareActive || fxCompareCandidates.length < 3 || fxCompareCandidates.some((item) => item.state !== "queued") || fxCompareStarting || fxCompareRunning || fxCompareStopping || fxCompareConfirming || fxCompareRoundStale) return;
  fxCompareStarting = true;
  const session = fxCompareSession;
  const frozen = deepCopy(fxCompareFrozen);
  const candidates = fxCompareCandidates.slice();
  const run = ++fxCompareRun;
  fxCompareSelectedId = "";
  fxCompareSelectionToken += 1;
  elements["fx-preview-confirm"].disabled = true;
  fxCompareRender();
  try {
    // A selection made just before Compare may have already crossed the POST
    // boundary.  Wait for that response so its late job can be cancelled by
    // the old generation before this serial queue claims the worker.
    await fxPreviewWaitForLifecycles();
    const priorCancellation = fxPreviewCancelPromise;
    let priorCancellationError = null;
    try {
      await Promise.resolve(priorCancellation);
    } catch (error) {
      // fxPreviewCancelCurrentJob records the exact known job before rejecting.
      // Do not let this permanently rejected promise make the bounded retry
      // below unreachable; without a known job, the original error still wins.
      priorCancellationError = error;
    }
    if (fxPreviewUnverifiedJob?.id) {
      const retryJob = fxPreviewUnverifiedJob;
      const stopped = await fxCompareCancelAndVerify(retryJob);
      if (fxPreviewUnverifiedJob?.id === retryJob.id) {
        fxPreviewUnverifiedJob = stopped.verified ? null : stopped.job || retryJob;
      }
      if (!stopped.verified) throw new Error("上一個單款工作停止尚未確認");
      if (fxPreviewUnverifiedJob?.id) throw new Error("另一個單款工作停止尚未確認");
      priorCancellationError = null;
    }
    if (priorCancellationError) throw priorCancellationError;
    if (fxPreviewCancelPromise === priorCancellation) fxPreviewCancelPromise = null;
    const currentJob = fxPreviewJob;
    if (currentJob?.id) {
      const stopped = await fxCompareCancelAndVerify(currentJob);
      if (!stopped.verified) throw new Error("單款預覽工作停止尚未確認");
      if (session !== fxCompareSession || !fxCompareActive || fxCompareStopping) return;
      fxPreviewJob = null;
    }
    if (stateDirty || !lastSaveSucceeded) await saveState(false, true);
    if (stateDirty || !lastSaveSucceeded || !/^[0-9a-f]{64}$/i.test(String(state?.revision || ""))) throw new Error("目前編輯內容尚未成功儲存或版本無法確認");
  } catch (error) {
    if (session === fxCompareSession && fxCompareActive && !fxCompareStopping) fxPreviewSetStatus(`無法開始比較：${error.message}`, "error");
    return;
  } finally {
    if (session === fxCompareSession) {
      fxCompareStarting = false;
      // A failed verification is intentionally retryable from this same Start
      // button. Re-render after the await so it does not remain disabled by the
      // earlier starting state.
      fxCompareRender();
    }
  }
  if (session !== fxCompareSession || !fxCompareActive || fxCompareStopping || run !== fxCompareRun) { fxCompareRender(); return; }
  const currentContext = fxCompareContextForDraft(candidates[0]?.draft);
  if (!fxCompareSameContext(currentContext, frozen)) {
    fxCompareRoundStale = true;
    fxPreviewSetStatus("專案或凍結比較內容已變更；本輪未送出，請丟棄後重新建立。", "error");
    fxCompareRender();
    return;
  }
  const mismatch = candidates.some((item) => !fxCompareSameContext(fxCompareContextForDraft(item.draft), frozen));
  if (mismatch) {
    fxCompareRoundStale = true;
    fxPreviewSetStatus("候選的凍結比較內容不一致；本輪未送出。", "error");
    fxCompareRender();
    return;
  }
  fxCompareRunning = true;
  fxCompareRender();
  const queuePromise = fxCompareRunQueue({ id: run, session, frozen, candidates });
  fxCompareQueuePromise = queuePromise;
  try { await queuePromise; }
  finally { if (fxCompareQueuePromise === queuePromise) fxCompareQueuePromise = null; }
}

function fxCompareErrorIsStale(error) {
  if (Number(error?.status) !== 409) return false;
  const reason = String(error?.reason || error?.code || "").toLowerCase();
  return !["render_busy", "busy", "preview_busy"].includes(reason);
}

function fxCompareJobIsStale(job) {
  const stateName = fxPreviewJobState(job);
  const reason = String(job?.reason || job?.error?.code || job?.error_code || "").toLowerCase();
  return stateName === "stale" || ["comparison_stale", "revision_conflict", "preview_stale", "source_changed", "source_stale"].includes(reason);
}

async function fxCompareRunQueue(run) {
  let anchorId = "";
  let queueHalted = false;
  for (const candidate of run.candidates) {
    if (run.id !== fxCompareRun || run.session !== fxCompareSession || fxCompareStopping || !fxCompareActive) break;
    candidate.state = "running"; candidate.message = "預覽排隊中（待渲染，不是即時完成）"; fxCompareRender();
    try {
      const payload = await request("/api/fx-previews", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(fxCompareRequestBody(candidate, anchorId, run.frozen)) });
      const job = payload.job || payload;
      candidate.job = job;
      if (run.id !== fxCompareRun || run.session !== fxCompareSession || fxCompareStopping || !fxCompareActive) {
        const stopped = await fxCompareCancelAndVerify(job, candidate, 3, true);
        if (!stopped.verified) fxCompareStopping = true;
        break;
      }
      const terminal = await fxCompareWaitTerminal(candidate, run);
      if (run.id !== fxCompareRun || run.session !== fxCompareSession || fxCompareStopping || !fxCompareActive) {
        const stopped = await fxCompareCancelAndVerify(terminal || candidate.job, candidate, 3, true);
        if (!stopped.verified) fxCompareStopping = true;
        break;
      }
      if (fxCompareJobIsStale(terminal)) {
        candidate.state = "stale";
        candidate.message = "比較內容已過期，整輪已停止。";
        fxCompareRoundStale = true;
        break;
      }
      if (!anchorId && terminal?.id && fxPreviewJobState(terminal) !== "cancelled") anchorId = terminal.id;
    } catch (error) {
      if (error?.fxComparePollUnknown) {
        queueHalted = true;
        const stopped = await fxCompareCancelAndVerify(error.fxPreviewJob || candidate.job, candidate);
        if (!stopped.verified) {
          fxCompareStopping = true;
          candidate.state = "stopping";
          candidate.message = "預覽狀態無法確認，停止也尚未確認；請重試停止。";
        } else if (stopped.absent) {
          candidate.state = "failed";
          candidate.message = "預覽狀態無法確認；目前管理器已無此工作，本輪已停止排程。";
        } else {
          candidate.state = fxPreviewJobState(stopped.job);
          candidate.message = "預覽狀態讀取失敗；工作已確認停止，本輪不再送出後續候選。";
        }
        break;
      }
      candidate.state = fxCompareErrorIsStale(error) ? "stale" : "failed";
      candidate.message = candidate.state === "stale" ? "比較內容已過期，整輪已停止。" : `預覽失敗：${error.message}`;
      if (candidate.state === "stale") { fxCompareRoundStale = true; break; }
    }
    fxCompareRender();
  }
  if (run.id !== fxCompareRun || run.session !== fxCompareSession) return;
  fxCompareRunning = false;
  if (fxCompareStopping) fxPreviewSetStatus("比較仍在停止；內容未套用。", "error");
  else if (fxCompareRoundStale) fxPreviewSetStatus("比較內容已過期；整輪不可套用。", "error");
  else if (queueHalted) fxPreviewSetStatus("預覽狀態讀取失敗；已停止排程，請丟棄本輪後重試。", "error");
  else fxPreviewSetStatus("比較完成。請明確選擇一款已完成的風格後再套用。", "success");
  fxCompareRender();
}

async function fxCompareWaitTerminal(candidate, run, statusTimeoutMs = FX_PREVIEW_CANCEL_STATUS_TIMEOUT_MS) {
  let job = candidate.job;
  while (run.id === fxCompareRun && run.session === fxCompareSession && !fxCompareStopping && fxCompareActive) {
    const status = String(job?.state || "pending").toLowerCase();
    if (["ready", "cache_hit", "failed", "cancelled", "stale"].includes(status)) {
      candidate.state = fxCompareJobIsStale(job) ? "stale" : status === "cache_hit" ? "ready" : status;
      candidate.message = fxCompareJobIsStale(job) ? "比較內容已過期，整輪已停止。" : fxPreviewStatusForJob(job);
      return job;
    }
    await new Promise((resolve) => setTimeout(resolve, 450));
    let payload;
    try {
      payload = await fxPreviewRequestWithDeadline(
        job.status_url || `/api/fx-previews/${encodeURIComponent(job.id)}`,
        {},
        statusTimeoutMs,
      );
    } catch (error) {
      error.fxComparePollUnknown = true;
      error.fxPreviewJob = job;
      throw error;
    }
    job = payload.job || payload; candidate.job = job; candidate.message = fxPreviewStatusForJob(job); fxCompareRender();
  }
  return job;
}

function fxCompareEffectTimeForSourceTime(sourceTime, job) {
  const map = Array.isArray(job?.time_map) ? job.time_map : [];
  const point = Math.max(0, Number(sourceTime) || 0);
  const mapped = fxPreviewCompositionMapSource(point, job);
  if (mapped !== null) return mapped;
  if (map.length) return null;
  return Math.max(0, point - Number(fxCompareFrozen?.window?.start_s || 0));
}

function fxCompareSelectionCurrent(session, token, candidateId, jobId) {
  const candidate = fxCompareCandidates.find((item) => item.id === candidateId);
  return Boolean(elements["fx-preview-dialog"]?.open
    && session === fxCompareSession
    && token === fxCompareSelectionToken
    && fxCompareSelectedId === candidateId
    && candidate?.job?.id === jobId);
}

function fxCompareSelect(id) {
  const candidate = fxCompareCandidates.find((item) => item.id === id);
  if (!candidate || candidate.state !== "ready" || fxCompareStarting || fxCompareRunning || fxCompareStopping || fxCompareConfirming || fxCompareRoundStale) return;
  const sourceVideo = elements["fx-preview-source-video"];
  const video = elements["fx-preview-video"];
  const sourceTime = Number.isFinite(Number(sourceVideo?.currentTime))
    ? Number(sourceVideo.currentTime)
    : fxPreviewSourceTimeForEffectTime(Number(video?.currentTime || 0));
  const wasPlaying = Boolean((video && !video.paused) || (sourceVideo && !sourceVideo.paused));
  fxCompareSelectedId = id;
  const session = fxCompareSession;
  const selectionToken = ++fxCompareSelectionToken;
  fxPreviewDraft = deepCopy(candidate.draft);
  fxPreviewDraftSignature = candidate.signature;
  fxPreviewJob = deepCopy(candidate.job);
  fxPreviewReadySignature = candidate.signature;
  // Keep the persistent editor description bound to the same frozen candidate
  // as the selected media/job. Rendering does not schedule a new preview; the
  // comparison render below reapplies the round interaction locks.
  fxPreviewRenderDialog();
  fxPreviewSetVisibleMedia(candidate.job);
  const jobId = candidate.job?.id;
  const effectTime = fxCompareEffectTimeForSourceTime(sourceTime, candidate.job);
  const applyMedia = () => {
    if (!fxCompareSelectionCurrent(session, selectionToken, id, jobId)) return;
    if (sourceVideo) sourceVideo.currentTime = sourceTime;
    if (effectTime !== null) video.currentTime = effectTime;
    fxPreviewSyncVideos("effect");
    if (wasPlaying) {
      video.play().catch(() => {});
      sourceVideo?.play().catch(() => {});
    } else {
      video.pause();
      sourceVideo?.pause();
    }
  };
  if (video.src !== new URL(candidate.job.media_url, location.href).href) {
    video.src = candidate.job.media_url;
    video.addEventListener("loadedmetadata", applyMedia, { once: true });
    video.load();
  } else applyMedia();
  fxPreviewPrepareSourceVideo();
  elements["fx-preview-confirm"].disabled = false;
  fxPreviewSetStatus("已選擇比較候選；尚未套用到時間軸。", "success");
  fxCompareRender();
}

async function fxCompareStop(closeAfter = false) {
  if (closeAfter) fxCompareCloseRequested = true;
  if (fxCompareConfirming) return false;
  if (fxCompareCleanupPromise) return fxCompareCleanupPromise;
  const retrying = fxCompareStopping;
  if (retrying) fxCompareCandidates.forEach((candidate) => {
    if (candidate.state === "stopping") candidate.state = "running";
  });
  fxCompareStopping = true;
  fxCompareStarting = false;
  fxCompareRunning = false;
  fxCompareRun += 1;
  fxCompareSelectionToken += 1;
  elements["fx-preview-confirm"].disabled = true;
  fxCompareRender();
  const cleanup = (async () => {
    await fxPreviewWaitForLifecycles();
    if (fxCompareQueuePromise) await Promise.allSettled([fxCompareQueuePromise]);
    let verified = true;
    for (const candidate of fxCompareCandidates) {
      if (!candidate.job?.id) continue;
      if (fxPreviewJobTerminationVerified(candidate.job)) continue;
      if (candidate.state === "stopping") {
        verified = false;
        continue;
      }
      const result = await fxCompareCancelAndVerify(candidate.job, candidate);
      if (!result.verified) verified = false;
    }
    if (!verified) {
      fxPreviewSetStatus("停止尚未獲得終止確認；維持停止中，禁止開始新工作。", "error");
      fxCompareRender();
      return false;
    }
    const shouldClose = fxCompareCloseRequested;
    fxCompareResetState();
    fxCompareRender();
    if (shouldClose && elements["fx-preview-dialog"]?.open) elements["fx-preview-dialog"].close("cancel");
    else fxPreviewSetStatus("已停止並丟棄比較候選，回到單一風格試看。");
    return true;
  })();
  fxCompareCleanupPromise = cleanup;
  try { return await cleanup; }
  finally { if (fxCompareCleanupPromise === cleanup) fxCompareCleanupPromise = null; }
}

function fxPreviewChangedMessage() {
  return "候選設定已變更，正在準備新的預覽（待渲染，不是即時完成）";
}

function fxPreviewInvalidateAndSchedule(keepVisiblePreview = false) {
  if (!fxPreviewDraft || !elements["fx-preview-dialog"]?.open) return;
  if (fxCompareActive) {
    fxPreviewReadySignature = "";
    elements["fx-preview-confirm"].disabled = true;
    fxPreviewSetStatus("比較模式中：變更候選不會產生單一預覽，請按「加入比較」。");
    return;
  }
  fxPreviewGeneration += 1;
  const generation = fxPreviewGeneration;
  clearTimeout(fxPreviewDebounceTimer);
  fxPreviewDebounceTimer = null;
  fxPreviewReadySignature = "";
  fxPreviewUpgradeStarted = false;
  fxPreviewUpgradeCancelled = false;
  fxPreviewDraftSignature = fxPreviewSignature();
  elements["fx-preview-confirm"].disabled = true;
  elements["fx-preview-retry"].hidden = true;
  if (!keepVisiblePreview) {
    fxPreviewClearVisibleMedia();
    const oldVideo = elements["fx-preview-video"];
    if (oldVideo) {
      oldVideo.pause();
      oldVideo.removeAttribute("src");
      oldVideo.load();
    }
  }
  fxPreviewSetStatus(fxPreviewChangedMessage());
  const cancellation = fxPreviewTrackLifecycle(fxPreviewCancelCurrentJob());
  fxPreviewCancelPromise = cancellation;
  cancellation.catch(() => {});
  const effect = fxEffectByCode(fxPreviewDraft.code);
  if (fxPreviewIsBlocked(effect)) {
    fxPreviewSetStatus("B1 需要B-roll素材，請先至素材區處理", "error");
    return;
  }
  if (effect?.available === false) {
    fxPreviewSetStatus("此效果目前不可用，未送出預覽。", "error");
    return;
  }
  fxPreviewDebounceTimer = setTimeout(async () => {
    try { await cancellation; }
    catch (error) {
      if (generation === fxPreviewGeneration) fxPreviewSetStatus(`舊預覽停止失敗：${error.message}`, "error");
      return;
    }
    if (generation !== fxPreviewGeneration) return;
    await fxPreviewStart(generation, fxPreviewDraftSignature);
  }, 350);
}

async function fxPreviewStart(generation, signature, _keepExistingPreview = false) {
  if (generation !== fxPreviewGeneration || signature !== fxPreviewDraftSignature || !fxPreviewDraft || !elements["fx-preview-dialog"].open) return;
  try {
    if (stateDirty || !lastSaveSucceeded) {
      await saveState(false);
      if (stateDirty || !lastSaveSucceeded) throw new Error("目前編輯內容尚未成功儲存");
    }
    if (!/^[0-9a-f]{64}$/i.test(String(state?.revision || ""))) {
      throw new Error("目前專案版本無法確認，請先重新載入專案");
    }
    const body = {
      schema_version: 1,
      expected_revision: state.revision,
      candidate: {
        code: fxPreviewDraft.code,
        window: {
          start_s: Number(fxPreviewDraft.window.start_s),
          duration_s: Number(fxPreviewDraft.window.duration_s),
        },
        apply_window: { ...(fxPreviewDraft.apply_window || fxPreviewDraft.window) },
        values: { ...(fxPreviewDraft.values || {}) },
        placement: fxPreviewDraft.placement || "",
        scheme: fxPreviewDraft.scheme || "",
        font_ids: { ...(fxPreviewDraft.font_ids || {}) },
        text_fit: fxPreviewDraft.text_fit !== false,
      },
      opacity: fxPreviewDraft.opacity,
      preview_options: { ...(fxPreviewDraft.preview_options || { mode: "single", quality: "draft" }) },
    };
    if (fxPreviewDraft.code === "P5") body.candidate.subject = fxPreviewDraft.subject;
    if (fxPreviewDraft.code === "B1" && fxPreviewDraft.material) body.candidate.material = { ...fxPreviewDraft.material };
    if (fxPreviewReplaceOverlayId) body.replace_overlay_id = fxPreviewReplaceOverlayId;
    fxPreviewSetStatus("預覽排隊中（待渲染，不是即時完成）");
    const lifecycle = fxPreviewTrackLifecycle((async () => {
      const payload = await request("/api/fx-previews", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const job = payload.job || payload;
      if (generation !== fxPreviewGeneration || signature !== fxPreviewDraftSignature || !elements["fx-preview-dialog"]?.open) {
        // A single-preview lifecycle is not complete until a late returned job
        // has a verified terminal cancellation. Compare/reopen cannot pass it.
        const stopped = await fxCompareCancelAndVerify(job, null, 3, true);
        if (!stopped.verified) {
          fxPreviewUnverifiedJob = stopped.job || job;
          throw new Error("晚回單款預覽的停止狀態未確認");
        }
        if (fxPreviewUnverifiedJob?.id === job?.id) fxPreviewUnverifiedJob = null;
        return { stale: true, job: stopped.job };
      }
      return { stale: false, payload, job };
    })());
    const outcome = await lifecycle;
    if (outcome.stale) return;
    const payload = outcome.payload;
    const job = outcome.job;
    fxPreviewApplyJob(job, generation, signature);
    const stateName = String(job?.state || "pending").toLowerCase();
    if (!["ready", "cache_hit", "failed", "cancelled"].includes(stateName)) {
      const statusUrl = job.status_url || `/api/fx-previews/${encodeURIComponent(job.id)}`;
      fxPreviewPollTimer = setTimeout(() => fxPreviewPoll(statusUrl, generation, signature), 450);
    }
  } catch (error) {
    if (generation !== fxPreviewGeneration || signature !== fxPreviewDraftSignature) return;
    if (error.status === 409) {
      const reason = String(error.reason || error.code || "").toLowerCase();
      if (["render_busy", "busy", "preview_busy"].includes(reason)) {
        fxPreviewSetStatus("預覽工作忙碌中，請稍候。", "error");
        elements["fx-preview-retry"].hidden = false;
      } else {
        fxPreviewSetStatus("專案已更新，請關閉試看並重新載入後再試。", "error");
        elements["fx-preview-retry"].hidden = true;
      }
    } else {
      fxPreviewSetStatus(`預覽失敗：${error.message}`, "error");
      elements["fx-preview-retry"].hidden = false;
    }
    elements["fx-preview-confirm"].disabled = true;
  }
}

async function fxPreviewPoll(statusUrl, generation, signature) {
  if (generation !== fxPreviewGeneration || signature !== fxPreviewDraftSignature) return;
  try {
    const payload = await request(statusUrl);
    if (generation !== fxPreviewGeneration || signature !== fxPreviewDraftSignature) return;
    const job = payload.job || payload;
    fxPreviewApplyJob(job, generation, signature);
    const stateName = String(job?.state || "pending").toLowerCase();
    if (!["ready", "cache_hit", "failed", "cancelled"].includes(stateName)) {
      fxPreviewPollTimer = setTimeout(() => fxPreviewPoll(statusUrl, generation, signature), 450);
    }
  } catch (error) {
    if (generation !== fxPreviewGeneration || signature !== fxPreviewDraftSignature) return;
    if (error.status === 409) {
      fxPreviewJob = null;
      fxPreviewReadySignature = "";
      fxPreviewSetStatus("專案已更新，請關閉試看並重新載入後再試。", "error");
      elements["fx-preview-retry"].hidden = true;
      elements["fx-preview-confirm"].disabled = true;
    } else {
      fxPreviewSetStatus(`預覽狀態讀取失敗：${error.message}`, "error");
      elements["fx-preview-retry"].hidden = false;
    }
  }
}

function fxPreviewDraftChanged() {
  if (fxCompareRoundLocked()) {
    fxPreviewSetStatus("比較已開始；請先丟棄本輪再修改候選。", "error");
    return;
  }
  fxPreviewUpdateWindowControls();
  fxPreviewPersistDraft();
  fxPreviewInvalidateAndSchedule();
}

function fxPreviewSelectEffect(code) {
  if (fxCompareRoundLocked()) return;
  const effect = fxEffectByCode(code);
  if (!effect) return;
  const overlay = fxPreviewReplaceOverlay();
  fxPreviewDraft = fxPreviewBuildDraft(effect, fxPreviewDraft, overlay, fxPreviewSourceStart());
  fxPreviewRememberRecent(effect.code);
  fxPreviewPersistDraft();
  fxPreviewRenderDialog();
  fxPreviewInvalidateAndSchedule();
}

function fxPreviewSyncCatalogDisclosure(event) {
  const dialog = elements["fx-preview-dialog"];
  const catalogDisclosure = document.getElementById("fx-preview-catalog-disclosure");
  if (!dialog?.open || !catalogDisclosure) return;
  const isMobile = typeof event?.matches === "boolean"
    ? event.matches
    : (fxPreviewCatalogBreakpoint?.matches ?? window.matchMedia("(max-width: 760px)").matches);
  const shouldOpen = !isMobile;
  if (catalogDisclosure.open === shouldOpen) return;

  const summary = catalogDisclosure.querySelector("summary");
  const activeElement = document.activeElement;
  const focusWasInCatalog = catalogDisclosure.contains(activeElement);
  catalogDisclosure.open = shouldOpen;
  if (!shouldOpen && focusWasInCatalog) summary?.focus();
}

function fxPreviewInstallCatalogBreakpointListener() {
  if (fxPreviewCatalogBreakpoint) return;
  fxPreviewCatalogBreakpoint = window.matchMedia("(max-width: 760px)");
  fxPreviewCatalogBreakpoint.addEventListener("change", fxPreviewSyncCatalogDisclosure);
}

async function openFxPreviewDialog(trigger = document.activeElement, replaceOverlayId = null, initialCode = "", portablePreset = null) {
  const openIntent = ++fxPreviewOpenIntent;
  const dialog = elements["fx-preview-dialog"];
  if (!dialog) return;
  if (fxCompareCleanupPromise || fxCompareStopping || fxPreviewLifecycles.size) {
    showToast("上一個預覽仍在確認停止，完成前不能開啟新一輪。", "error");
    return;
  }
  if (fxPreviewUnverifiedJob?.id) {
    showToast("正在重新確認上一個預覽已停止…");
    if (!fxPreviewRecoveryPromise) {
      const recoveryJob = fxPreviewUnverifiedJob;
      const recovery = (async () => {
        try { return await fxCompareCancelAndVerify(recoveryJob); }
        finally { if (fxPreviewRecoveryPromise === recovery) fxPreviewRecoveryPromise = null; }
      })();
      fxPreviewRecoveryPromise = recovery;
    }
    const stopped = await fxPreviewRecoveryPromise;
    if (openIntent !== fxPreviewOpenIntent) return;
    if (!stopped.verified) {
      fxPreviewUnverifiedJob = stopped.job || fxPreviewUnverifiedJob;
      showToast("上一個預覽停止尚未確認；請稍後再按一次試看以重試。", "error");
      return;
    }
    fxPreviewUnverifiedJob = null;
  }
  if (openIntent !== fxPreviewOpenIntent || fxCompareCleanupPromise || fxCompareStopping || fxPreviewLifecycles.size) return;
  if (dialog.open) dialog.close("replace");
  fxPreviewConfirmed = false;
  fxPreviewTrigger = trigger instanceof HTMLElement ? trigger : document.activeElement;
  fxPreviewReplaceOverlayId = replaceOverlayId || null;
  activeFxPreviewCategory = "";
  fxPreviewLibraryFilter = "";
  elements["fx-preview-search"].value = "";
  const overlay = fxPreviewReplaceOverlay();
  const first = fxEffectByCode(initialCode)
    || fxEffectByCode(overlay?.code)
    || fxEffectByCode(lastFxCode)
    || fxEffectByCode("T3")
    || fxEffectByCode("H1")
    || fxEffects().find((effect) => effect.available !== false && !fxPreviewIsBlocked(effect))
    || fxEffects()[0];
  if (!first) {
    showToast("找不到效果庫（edit-style-profiles）", "error");
    return;
  }
  const saved = fxPreviewSavedDraft();
  const savedEffect = fxEffectByCode(saved?.draft?.code);
  fxPreviewStaleDraft = saved && (portablePreset || overlay || saved.revision !== fxPreviewCurrentRevision()) ? saved : null;
  if (!portablePreset && !overlay && saved?.revision === fxPreviewCurrentRevision() && savedEffect && !fxPreviewIsBlocked(savedEffect)) {
    fxPreviewDraft = fxPreviewBuildDraft(savedEffect, saved.draft, overlay, fxPreviewSourceStart());
    fxPreviewDraft.__restored = true;
  } else {
    fxPreviewDraft = fxPreviewBuildDraft(first, null, overlay, fxPreviewSourceStart());
    if (portablePreset) {
      for (const key of ["values", "placement", "scheme", "font_ids", "opacity", "text_fit"]) {
        if (Object.prototype.hasOwnProperty.call(portablePreset, key)) fxPreviewDraft[key] = deepCopy(portablePreset[key]);
      }
      // Material and subject belong to this project, never to the portable preset.
      if (overlay?.code === first.code && first.code === "B1") fxPreviewDraft.material = overlay.material ? deepCopy(overlay.material) : null;
      if (overlay?.code === first.code && first.code === "P5") fxPreviewDraft.subject = overlay.subject ? deepCopy(overlay.subject) : null;
    }
  }
  fxPreviewDraftSignature = fxPreviewSignature();
  fxPreviewJob = null;
  fxPreviewClearVisibleMedia();
  clearTimeout(fxPreviewDebounceTimer);
  clearTimeout(fxPreviewPollTimer);
  fxPreviewRenderDialog();
  const catalogDisclosure = document.getElementById("fx-preview-catalog-disclosure");
  dialog.showModal();
  fxPreviewSyncCatalogDisclosure();
  dialog.querySelector(".fx-preview-editor-scroll").scrollTop = 0;
  fxPreviewPrepareSourceVideo();
  fxPreviewRenderDraftNotice();
  (catalogDisclosure.open ? elements["fx-preview-search"] : catalogDisclosure.querySelector("summary")).focus();
  fxPreviewInvalidateAndSchedule();
}

async function confirmFxPreview() {
  const dialog = elements["fx-preview-dialog"];
  const effect = fxEffectByCode(fxPreviewDraft?.code);
  if (!dialog?.open || !fxPreviewJob?.id || !effect || fxPreviewReadySignature !== fxPreviewDraftSignature) return;
  const selected = fxCompareActive
    ? fxCompareCandidates.find((item) => item.id === fxCompareSelectedId && item.state === "ready" && item.job?.id === fxPreviewJob.id)
    : null;
  if (fxCompareActive && (!selected || fxCompareStarting || fxCompareRunning || fxCompareStopping || fxCompareConfirming || fxCompareRoundStale)) return;
  const confirmSnapshot = {
    comparison: Boolean(fxCompareActive),
    session: fxCompareSession,
    selection_token: fxCompareSelectionToken,
    candidate_id: selected?.id || "",
    job_id: String(selected?.job?.id || fxPreviewJob.id),
    revision: String(fxCompareActive ? fxCompareFrozen?.revision : state.revision),
  };
  const before = deepCopy(state);
  const code = fxPreviewDraft.code;
  if (confirmSnapshot.comparison) fxCompareConfirming = true;
  elements["fx-preview-confirm"].disabled = true;
  fxPreviewSetStatus("正在套用到時間軸…");
  fxCompareRender();
  try {
    const result = await request(`/api/fx-previews/${encodeURIComponent(confirmSnapshot.job_id)}/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: confirmSnapshot.revision }),
    });
    if (confirmSnapshot.comparison && (!fxCompareSelectionCurrent(confirmSnapshot.session, confirmSnapshot.selection_token, confirmSnapshot.candidate_id, confirmSnapshot.job_id) || !fxCompareConfirming)) {
      fxCompareRoundStale = true;
      throw new Error("套用回應已不屬於目前比較輪次，請重新載入確認時間軸");
    }
    if (!result.state || typeof result.state !== "object") throw new Error("伺服器沒有回傳更新後的時間軸");
    history.push(before);
    if (history.length > 40) history.shift();
    redoStack = [];
    state = result.state;
    if (result.revision) state.revision = result.revision;
    if (result.approval_revisions) projectPayload.approval_revisions = result.approval_revisions;
    const invalidated = Array.isArray(result.invalidated_gates) ? result.invalidated_gates : [];
    invalidated.forEach((gate) => {
      if (projectPayload?.manifest?.approvals?.[gate]) projectPayload.manifest.approvals[gate].approved = false;
      if (gate === "highlight_selection") elements["approve-highlights"].textContent = "核可精華選段";
      if (gate === "timeline") elements["approve-timeline"].textContent = "核可時間軸";
      if (gate === "final") {
        projectPayload.approval_current = projectPayload.approval_current || {};
        projectPayload.approval_current.final = false;
      }
    });
    selectedOverlayId = result.overlay_id
      || state.review?.selected_overlay_id
      || state.overlays[0]?.id
      || null;
    lastFxCode = code;
    stateDirty = false;
    markStateSynced();
    renderAll();
    if (invalidated.includes("final")) renderDeliveryQa();
    updateScrubberBounds();
    showToast(result.overlay_id && fxPreviewReplaceOverlayId ? "特效已替換到時間軸" : "特效已加入時間軸", "success");
    fxPreviewConfirmed = true;
    fxPreviewDiscardSavedDraft();
    fxPreviewJob = null;
    clearTimeout(fxPreviewPollTimer);
    elements["fx-preview-video"].pause();
    if (confirmSnapshot.comparison) fxCompareResetState();
    dialog.close("confirm");
  } catch (error) {
    if (error.status === 409) {
      fxPreviewJob = null;
      fxPreviewReadySignature = "";
      if (confirmSnapshot.comparison) {
        fxCompareRoundStale = true;
        fxCompareCandidates.forEach((candidate) => {
          if (["ready", "running", "queued"].includes(candidate.state)) candidate.state = "stale";
        });
      }
      fxPreviewSetStatus(confirmSnapshot.comparison ? "專案已更新；整輪比較已失效，不能套用任何候選。" : "專案已更新，請關閉試看並重新載入後再試。", "error");
      elements["fx-preview-retry"].hidden = true;
      elements["fx-preview-confirm"].disabled = true;
      return;
    }
    fxPreviewSetStatus(`套用失敗：${error.message}`, "error");
    elements["fx-preview-confirm"].disabled = confirmSnapshot.comparison || fxCompareRoundStale;
  } finally {
    if (confirmSnapshot.comparison && confirmSnapshot.session === fxCompareSession) {
      fxCompareConfirming = false;
      fxCompareRender();
    }
  }
}

function closeFxPreviewDialog() {
  if (!elements["fx-preview-dialog"]?.open || fxCompareConfirming) return;
  if (fxCompareActive || fxCompareStopping || fxCompareCleanupPromise) {
    void fxCompareStop(true);
    return;
  }
  elements["fx-preview-dialog"].close("cancel");
}

function cleanupFxPreviewDialog() {
  const comparisonWasActive = fxCompareActive || fxCompareStopping;
  if (comparisonWasActive && !fxPreviewConfirmed) void fxCompareStop(false);
  clearTimeout(fxPreviewDebounceTimer);
  clearTimeout(fxPreviewPollTimer);
  fxPreviewDebounceTimer = null;
  fxPreviewPollTimer = null;
  fxPreviewGeneration += 1;
  if (!fxPreviewConfirmed && !comparisonWasActive) {
    fxPreviewCancelPromise = fxPreviewTrackLifecycle(fxPreviewCancelCurrentJob());
    fxPreviewCancelPromise.catch(() => {});
  } else if (comparisonWasActive) fxPreviewJob = null;
  const video = elements["fx-preview-video"];
  if (video) {
    video.pause();
    video.removeAttribute("src");
    video.load();
  }
  const sourceVideo = elements["fx-preview-source-video"];
  if (sourceVideo) {
    sourceVideo.pause();
    sourceVideo.removeAttribute("src");
    sourceVideo.load();
  }
  const trigger = fxPreviewTrigger;
  fxPreviewDraft = null;
  fxPreviewReplaceOverlayId = null;
  fxPreviewClearVisibleMedia();
  fxPreviewReadySignature = "";
  fxPreviewDraftSignature = "";
  fxPreviewTrigger = null;
  fxPreviewConfirmed = false;
  fxPreviewUpgradeStarted = false;
  fxPreviewUpgradeCancelled = false;
  fxPreviewStaleDraft = null;
  if (trigger && typeof trigger.focus === "function" && document.contains(trigger)) trigger.focus();
}

function applyFxCode(overlay, code) {
  const effect = fxEffectByCode(code);
  if (!effect) return;
  overlay.code = code;
  overlay.text = effect.name;
  overlay.values = {};
  effect.vars.filter((item) => item.kind === "text").forEach((item) => { overlay.values[item.id] = item.default; });
  const placement = effect.vars.find((item) => item.kind === "placement");
  overlay.placement = placement ? placement.default : "";
  overlay.scheme = overlay.scheme || "";
  if (effect.media_max && overlay.end - overlay.start > effect.media_max) overlay.end = overlay.start + effect.media_max;
  lastFxCode = code;
}
function renderFxPanel(overlay) {
  const catalog = fxCatalog();
  if (!catalog.available) {
    elements["fx-panel-note"].textContent = `效果庫不可用：${catalog.reason || "找不到 edit-style-profiles"}`;
    elements["fx-grid"].replaceChildren();
    return;
  }
  const cats = catalog.categories || {};
  const tabs = [["", "全部"], ...Object.entries(cats)];
  elements["fx-cats"].replaceChildren(...tabs.map(([key, label]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.setAttribute("role", "tab");
    button.className = key === activeFxCategory ? "is-selected" : "";
    button.textContent = key ? `${key} ${label}` : label;
    button.addEventListener("click", () => { activeFxCategory = key; renderFxPanel(overlay); });
    return button;
  }));
  const query = (elements["fx-search"].value || "").trim().toLowerCase();
  const list = fxEffects().filter((effect) => (!activeFxCategory || effect.category === activeFxCategory)
    && (!query || effect.code.toLowerCase().includes(query) || effect.name.toLowerCase().includes(query)));
  elements["fx-grid"].replaceChildren(...list.map((effect) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `fx-card${effect.code === overlay.code ? " is-selected" : ""}`;
    button.setAttribute("role", "radio");
    button.setAttribute("aria-checked", String(effect.code === overlay.code));
    button.disabled = effect.available === false;
    button.title = `#${effect.num} ${effect.name}\n${effect.spec || ""}`;
    const thumb = fxThumbUrl(effect);
    if (thumb) { const img = document.createElement("img"); img.loading = "lazy"; img.src = thumb; img.alt = effect.name; button.append(img); }
    const code = document.createElement("span"); code.className = "fx-code"; code.textContent = effect.code; button.append(code);
    if (effect.matte) { const m = document.createElement("span"); m.className = "fx-m"; m.textContent = "ⓜ"; button.append(m); }
    const name = document.createElement("span"); name.className = "fx-name"; name.textContent = effect.name; button.append(name);
    button.addEventListener("click", () => {
      openFxPreviewDialog(button, overlay.id, effect.code);
    });
    return button;
  }));
  const effect = fxEffectByCode(overlay.code);
  elements["fx-spec"].textContent = effect
    ? `#${effect.num} ${effect.name}｜${effect.spec || ""}${effect.matte ? "｜ⓜ 需要人物去背：輸出時自動用 matte_video.py 處理這一段。" : ""}${effect.needs_video ? "｜此效果會拿這一段原片做變形（最長 " + (effect.media_max || 4) + " 秒）。" : ""}`
    : "";
  const vars = effect ? effect.vars : [];
  const rows = [];
  vars.filter((item) => item.kind === "text").forEach((item) => {
    const label = document.createElement("label");
    const span = document.createElement("span"); span.textContent = item.label || item.id; label.append(span);
    const input = document.createElement("input");
    input.type = "text"; input.maxLength = 200;
    input.value = overlay.values?.[item.id] ?? item.default ?? "";
    input.addEventListener("input", () => {
      overlay.values = overlay.values || {};
      overlay.values[item.id] = input.value;
      markDirty("修改特效文字，儲存中…");
      renderLayerList();
    });
    label.append(input);
    rows.push(label);
  });
  const placement = vars.find((item) => item.kind === "placement");
  if (placement) {
    const label = document.createElement("label");
    const span = document.createElement("span"); span.textContent = "擺位（避開臉）"; label.append(span);
    const select = document.createElement("select");
    [["top", "上"], ["bottom", "下"], ["left", "左"], ["right", "右"]].forEach(([value, text]) => {
      const option = document.createElement("option"); option.value = value; option.textContent = text; select.append(option);
    });
    select.value = overlay.placement || placement.default || "top";
    select.addEventListener("change", () => { overlay.placement = select.value; markDirty("修改擺位，儲存中…"); });
    label.append(select);
    rows.push(label);
  }
  const schemeLabel = document.createElement("label");
  const schemeSpan = document.createElement("span"); schemeSpan.textContent = `字卡色組（主題 ${fxThemeNo()}）`; schemeLabel.append(schemeSpan);
  const schemeSelect = document.createElement("select");
  [["", "跟隨風格包"], ...Object.entries(catalog.schemes || {})].forEach(([value, text]) => {
    const option = document.createElement("option"); option.value = value; option.textContent = text; schemeSelect.append(option);
  });
  schemeSelect.value = overlay.scheme || "";
  schemeSelect.addEventListener("change", () => { overlay.scheme = schemeSelect.value; markDirty("修改色組，儲存中…"); });
  schemeLabel.append(schemeSelect);
  rows.push(schemeLabel);
  // 字型（2026-10-22）：目錄 id → 伺服器對照成 family；選項以該字型本身顯示預覽。
  if (Array.isArray(catalog.fonts) && catalog.fonts.length) {
    [["display", "標題字型（此效果）"], ["body", "內文字型（此效果）"]].forEach(([role, text]) => {
      const label = document.createElement("label");
      const span = document.createElement("span"); span.textContent = text; label.append(span);
      label.append(fxFontPicker(overlay, role));
      rows.push(label);
    });
  }
  elements["fx-vars"].replaceChildren(...rows);
}

const FX_FONT_FALLBACK = { display: '"Songti TC","Noto Serif TC",serif', body: '"PingFang TC","Noto Sans TC",sans-serif', mono: "Menlo,monospace", hand: '"Hannotate TC","Songti TC",serif' };
let fxFontPop = null;
function closeFxFontPop() { if (fxFontPop) { fxFontPop.remove(); fxFontPop = null; } }
document.addEventListener("click", (ev) => { if (fxFontPop && !fxFontPop.contains(ev.target)) closeFxFontPop(); });
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeFxFontPop(); });
function fxFontPicker(overlay, role) {
  const fonts = (fxCatalog().fonts || []).filter((f) => f.verified !== false);
  const byId = Object.fromEntries(fonts.map((f) => [f.id, f]));
  const stack = (id) => (byId[id] ? `"${byId[id].family}", ${FX_FONT_FALLBACK[role]}` : FX_FONT_FALLBACK[role]);
  const button = document.createElement("button");
  button.type = "button"; button.className = "fx-font-pick";
  button.title = "點開選字型（每個選項用自己的字型顯示）";
  const paint = () => {
    const id = overlay.font_ids?.[role] || "";
    button.textContent = id ? (byId[id]?.label || id) : "跟隨風格包";
    button.style.fontFamily = stack(id);
    button.classList.toggle("is-empty", !id);
  };
  paint();
  button.addEventListener("click", (ev) => {
    ev.stopPropagation();
    openFxFontPop(button, role, overlay.font_ids?.[role] || "", (id) => {
      overlay.font_ids = overlay.font_ids || {};
      if (id) overlay.font_ids[role] = id; else delete overlay.font_ids[role];
      if (!Object.keys(overlay.font_ids).length) delete overlay.font_ids;
      paint();
      markDirty("修改特效字型，儲存中…");
    });
  });
  return button;
}
function openFxFontPop(anchor, role, current, pick) {
  closeFxFontPop();
  const fonts = (fxCatalog().fonts || []).filter((f) => f.verified !== false);
  const langs = { all: "全部", tc: "繁中", en: "英文", jp: "日文", sys: "系統內建" };
  const rect = anchor.getBoundingClientRect();
  const pop = document.createElement("div"); pop.className = "fx-font-pop"; fxFontPop = pop;
  pop.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - 450))}px`;
  pop.style.top = `${Math.min(rect.bottom + 6, window.innerHeight - 80)}px`;
  pop.addEventListener("click", (ev) => ev.stopPropagation());
  let lang = "all"; let q = "";
  const input = document.createElement("input"); input.type = "search"; input.placeholder = "搜字型名、用途…"; input.className = "fx-font-q";
  const tabs = document.createElement("div"); tabs.className = "fx-font-tabs";
  const list = document.createElement("div"); list.className = "fx-font-list";
  const row = (id, sampleText, family, label, note, selected) => {
    const b = document.createElement("button"); b.type = "button"; b.className = `fx-font-row${selected ? " is-selected" : ""}`;
    const sm = document.createElement("span"); sm.className = "fx-font-sample"; sm.textContent = sampleText;
    sm.style.fontFamily = family ? `"${family}", ${FX_FONT_FALLBACK[role]}` : FX_FONT_FALLBACK[role];
    const nm = document.createElement("span"); nm.className = "fx-font-name"; nm.textContent = label;
    if (note) { const small = document.createElement("small"); small.textContent = note; nm.append(small); }
    b.append(sm, nm);
    b.addEventListener("click", () => { pick(id); closeFxFontPop(); });
    return b;
  };
  const render = () => {
    tabs.replaceChildren(...Object.entries(langs).map(([key, text]) => {
      const t = document.createElement("button"); t.type = "button"; t.className = key === lang ? "is-selected" : ""; t.textContent = text;
      t.addEventListener("click", () => { lang = key; render(); });
      return t;
    }));
    const term = q.trim().toLowerCase();
    const hit = (f) => (lang === "all" || f.lang === lang) && (!term || [f.label, f.family, f.note, f.cat, f.id].join(" ").toLowerCase().includes(term));
    const sorted = fonts.filter(hit).sort((a, b) => (b.role === role) - (a.role === role));
    const rows = [row("", "跟隨風格包", "", "（不指定）", "", !current)];
    let sep = false;
    sorted.forEach((f) => {
      if (!sep && f.role !== role) { const d = document.createElement("div"); d.className = "fx-font-sep"; d.textContent = "其他角色的字型（也可以用）"; rows.push(d); sep = true; }
      rows.push(row(f.id, f.sample || "Aa", f.family, f.label, `${f.note || ""}${f.family !== f.label ? `　${f.family}` : ""}`, current === f.id));
    });
    if (!sorted.length) { const d = document.createElement("div"); d.className = "fx-font-sep"; d.textContent = "沒有符合的字型"; rows.push(d); }
    list.replaceChildren(...rows);
  };
  input.addEventListener("input", () => { q = input.value; render(); });
  pop.append(input, tabs, list); document.body.append(pop); render(); input.focus();
}

function renderInspector() {
  const audioEvent = currentAudioEvent();
  if (audioEvent) {
    elements["inspector-empty"].hidden = true;
    elements["layer-form"].hidden = true;
    renderAudioEventInspector(audioEvent);
    return;
  }
  elements["audio-event-form"].hidden = true;
  const overlay = currentOverlay();
  if (!overlay) {
    elements["inspector-empty"].hidden = false;
    elements["layer-form"].hidden = true;
    return;
  }
  const fxType = overlay.type === "fx";
  const assetType = ["image", "gif", "video"].includes(overlay.type) || fxType;
  const designCard = Boolean(overlay.design_role);
  const style = overlay.style || (overlay.style = deepCopy(state.caption_defaults));
  const position = designCard ? editableLayout(overlay) : style;
  elements["inspector-empty"].hidden = true;
  elements["layer-form"].hidden = false;
  elements["selected-type"].textContent = fxType ? `FX · ${overlay.code || ""}` : overlay.type.toUpperCase();
  elements["selected-name"].textContent = fxType ? `特效 ${fxEffectByCode(overlay.code)?.name || overlay.code || ""}` : layerLabel(overlay);
  elements["fx-panel"].hidden = !fxType;
  if (fxType) renderFxPanel(overlay);
  elements["text-field"].hidden = assetType;
  elements["effect-editor"].hidden = !["caption", "emphasis"].includes(overlay.type) || designCard;
  elements["overlay-text"].value = overlay.text || "";
  elements["overlay-start"].value = overlay.start;
  elements["overlay-end"].value = overlay.end;
  populateFontSelect(style);
  elements["font-family"].closest("label").hidden = assetType || designCard;
  elements["font-size"].value = style.font_size || 58;
  elements["font-size-output"].value = style.font_size || 58;
  elements["font-size"].closest("label").hidden = assetType || designCard;
  elements["asset-width-row"].hidden = !assetType || fxType;
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
  elements["position-x"].closest(".field-pair, .field, label").hidden = fxType;
  elements["position-y"].closest(".field-pair, .field, label").hidden = fxType;
  elements["overlay-visible"].checked = overlay.visible !== false;
  if (!elements["effect-editor"].hidden) renderEffectSpanList(overlay);
  renderLayoutWarning();
}

function selectOverlay(id, seek = false) {
  selectedAudioEventId = null;
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

function selectAudioEvent(id, seek = false) {
  const event = audioTimelineEvents().find((item) => String(item.id) === String(id));
  if (!event) return;
  selectedAudioEventId = event.id;
  selectedOverlayId = null;
  if (seek) elements["preview-video"].currentTime = timelineBounds().start + audioEventStartSeconds(event);
  renderInspector();
  renderLayerList();
  renderTimeline();
  renderPreviewOverlays(true);
}

function addOverlay(type, source = null) {
  if (type === "fx") {
    // FX is a server-rendered candidate. Opening the dialog must not create a
    // local layer, history entry, dirty state, or approval invalidation.
    openFxPreviewDialog(document.activeElement, null);
    return;
  }
  pushHistory();
  const start = Math.min(elements["preview-video"].currentTime || 0, Math.max(0, duration() - 0.2));
  const end = Math.min(duration(), start + (type === "title" ? 2.5 : 3));
  const style = deepCopy(state.caption_defaults);
  if (["image", "gif", "video"].includes(type)) Object.assign(style, { width: 34, x: 50, y: 48, animation: "fade" });
  if (type === "emphasis") Object.assign(style, { font_size: Math.max(72, style.font_size), y: 62, color: style.emphasis_color });
  if (type === "title" || type === "animation") Object.assign(style, { box: true, y: 42, max_width: 82 });
  const id = `${type}-${Date.now().toString(36)}`;
  const defaults = { caption: "新增字幕", emphasis: "關鍵重點", title: "影片標題", animation: "補充說明" };
  const overlay = {
    id, type, start, end: Math.max(end, start + 0.2), text: defaults[type] || "", emphasis: [],
    visible: true, locked: false, z_index: ["image", "gif", "video"].includes(type) ? 10 : type === "fx" ? 25 : 30,
    style, source, provenance: source ? "user-uploaded-through-local-editor" : "manual editor layer",
    highlight_id: activeHighlight()?.id || null,
  };
  state.overlays.push(overlay);
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
  if (overlay.type === "fx") {
    // An effect block is full-frame and keeps its own text; the form only owns timing and visibility.
    overlay.start = Number(elements["overlay-start"].value);
    overlay.end = Math.max(overlay.start + 0.2, Number(elements["overlay-end"].value));
    overlay.visible = elements["overlay-visible"].checked;
    markDirty("圖層變更，儲存中…");
    renderLayoutWarning();
    return;
  }
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
  const chosenFont = projectFonts.find((font) => font.asset_id === elements["font-family"].value);
  if (chosenFont) {
    style.font_asset_id = chosenFont.asset_id;
    style.font_family = chosenFont.family || "";
    if (chosenFont.weight !== undefined) style.font_weight = chosenFont.weight;
  } else if (elements["font-family"].value === "__legacy__") {
    // The legacy option is intentionally explicit.  Never fabricate an asset
    // id from a family name: renderer trust comes only from a verified id.
    delete style.font_asset_id;
  } // Unknown/loading options deliberately preserve an existing binding.
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
    { name: "動畫", types: ["image", "gif", "video", "animation", "fx"] },
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
      const timeline = audioEventTimeline();
      const events = audioTimelineEvents();
      const timelineValidation = validateAudioEventTimeline(timeline);
      if (timelineValidation.valid && events.length) {
        events.forEach((event) => {
          const start = bounds.start + audioEventStartSeconds(event);
          const end = start + Math.max(0, Number(event.duration_samples || 0)) / AUDIO_SAMPLE_RATE;
          const visibleStart = Math.max(start, bounds.start);
          const visibleEnd = Math.min(end, bounds.end);
          if (!(visibleEnd > visibleStart)) return;
          const button = document.createElement("button");
          button.type = "button";
          button.id = audioEventDomId(event.id);
          button.dataset.eventId = String(event.id);
          button.className = `timeline-audio-event${audioEventEdited(event) ? " is-edited" : ""}${String(event.id) === String(selectedAudioEventId) ? " is-selected" : ""}`;
          button.style.left = `${Math.max(0, timelinePercent(visibleStart))}%`;
          button.style.width = `${Math.max(0.15, ((visibleEnd - visibleStart) / span) * 100)}%`;
          button.textContent = `${audioEventRoleLabel(event)}${audioEventEdited(event) ? " · 已調整" : ""}`;
          button.title = audioEventTitle(event);
          button.setAttribute("aria-label", `${audioEventRoleLabel(event)}音效：${audioEventTitle(event)}`);
          button.addEventListener("click", () => selectAudioEvent(event.id, true));
          lane.append(button);
        });
      } else if (timeline && !timelineValidation.valid) {
        audio.classList.add("is-unavailable");
        audio.textContent = `原始音訊 · 音效時間軸不可編輯：${audioTimelineReasonSummary()}`;
      }
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

// Fallback wording per pipeline error_code, so a status that stops the run
// never reaches the operator as the generic "需要處理".
const PIPELINE_ERROR_COPY = {
  no_speech_detected: "此影片無可用語音；沒有可製作的字幕與字卡。",
};

function pipelineMessage(pipeline, fallback) {
  return pipeline?.message || PIPELINE_ERROR_COPY[pipeline?.error_code] || fallback;
}

function renderSourceWarning() {
  const report = projectPayload.qa?.report || projectPayload.qa || {};
  const warnings = [];
  const pipeline = projectPayload.pipeline_status || {};
  if (["pending", "running"].includes(pipeline.state)) warnings.push(pipelineMessage(pipeline, "本機自動處理中"));
  if (["failed", "needs_attention", "stopped"].includes(pipeline.state)) warnings.push(pipelineMessage(pipeline, "本機自動處理需要處理"));
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

/**
 * While the local pipeline is still writing the project (semantic caption
 * calibration and friends) a director switch would race it: the switch saves
 * first, and that save loses the compare-and-swap. Say so on the cards instead
 * of letting the click fail.
 */
function applyPipelineBusyState() {
  const status = projectPayload?.pipeline_status || {};
  pipelineBusy = PIPELINE_BUSY_STATES.includes(status.state);
  const note = elements["director-pipeline-note"];
  if (note) {
    note.hidden = !pipelineBusy;
    note.textContent = pipelineBusy ? DIRECTOR_PIPELINE_BUSY_NOTE : "";
  }
  elements["director-select"].disabled = pipelineBusy;
  elements["director-grid"].querySelectorAll(".director-card").forEach((card) => {
    const preset = projectPayload?.director_presets?.[card.dataset.directorId];
    const available = preset ? isDirectorAvailable(preset) : true;
    card.disabled = pipelineBusy || !available;
    card.setAttribute("aria-disabled", String(card.disabled));
    if (pipelineBusy) card.title = DIRECTOR_PIPELINE_BUSY_NOTE;
  });
}

/**
 * Pull the pipeline's own writes into the open panel once it finishes, so the
 * user sees the calibrated captions without hunting for a refresh button. A
 * dirty panel keeps its edits: the merge path in ``saveState`` owns that case.
 */
async function refreshAfterPipeline() {
  // No baseline means no safe diff; leave unsaved work alone.
  if (stateDirty && !lastSyncedState) return;
  try {
    const payload = await request("/api/project");
    const fresh = payload.state;
    if (!fresh || typeof fresh !== "object") return;
    const localChanges = changedStateKeys(lastSyncedState, state);
    const merged = deepCopy(fresh);
    localChanges.forEach((key) => { merged[key] = deepCopy(state[key]); });
    projectPayload = payload;
    state = merged;
    lastSyncedState = deepCopy(fresh);
    selectedOverlayId = state.overlays.some((overlay) => overlay.id === selectedOverlayId)
      ? selectedOverlayId
      : state.review?.selected_overlay_id || state.overlays[0]?.id || null;
    renderAll();
    applyPipelineBusyState();
    showToast("本機自動處理已完成，已載入最新內容", "success");
  } catch (error) {
    showToast(`無法載入最新內容：${error.message}`, "error");
  }
}

function pollPipelineStatus() {
  clearInterval(pipelinePollTimer);
  pipelinePollTimer = setInterval(async () => {
    try {
      const status = await request("/api/pipeline-status");
      projectPayload.pipeline_status = status;
      renderSourceWarning();
      applyPipelineBusyState();
      if (PIPELINE_BUSY_STATES.includes(status.state)) return;
      clearInterval(pipelinePollTimer);
      if (["failed", "needs_attention", "stopped"].includes(status.state)) {
        showToast(pipelineMessage(status, "本機自動處理需要處理"), "error");
        return;
      }
      await refreshAfterPipeline();
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
  applyPipelineBusyState();
}

function bindEvents() {
  fxPreviewInstallCatalogBreakpointListener();
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
  elements["director-select"].addEventListener("change", async () => {
    const id = elements["director-select"].value;
    const preset = projectPayload.director_presets[id];
    if (pipelineBusy) {
      elements["director-select"].value = state.director_style;
      showToast(DIRECTOR_PIPELINE_BUSY_NOTE, "error");
      return;
    }
    if (!isDirectorAvailable(preset)) {
      elements["director-select"].value = state.director_style;
      showToast(directorUnavailableReason(preset), "error");
      return;
    }
    if (id === state.director_style) return;
    // Picking a director picks a whole editing language, so the panel says
    // out loud what regenerates and what stays: the cut and the transcript.
    const confirmed = window.confirm(`切換導演將以「${preset.label}」重新生成字卡與動態，字幕與剪點不變。繼續？`);
    if (!confirmed) {
      elements["director-select"].value = state.director_style;
      renderDirectorCards();
      return;
    }
    const previousDirector = state.director_style;
    elements["director-select"].disabled = true;
    setSaveState(`正在以「${preset.label}」重新生成字卡與動態…`, "saving");
    try {
      await saveState(false);
      if (stateDirty) throw new Error("目前編輯內容尚未成功儲存");
      const result = await request("/api/director", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ director: id, expected_revision: state.revision }),
      });
      pushHistory();
      state = result.state;
      markStateSynced();
      projectPayload.approval_revisions = result.approval_revisions;
      if (result.director_presets) projectPayload.director_presets = result.director_presets;
      selectedOverlayId = state.review?.selected_overlay_id || state.overlays[0]?.id || null;
      elements["approve-timeline"].textContent = "核可時間軸";
      renderAll();
      updateScrubberBounds();
      setSaveState("已重新生成", "saved");
      showToast(`已改用「${preset.label}」重新生成字卡與動態`, "success");
    } catch (error) {
      elements["director-select"].value = previousDirector;
      renderDirectorCards();
      setSaveState("導演切換失敗", "error");
      showToast(`切換導演失敗：${error.message}`, "error");
    } finally {
      elements["director-select"].disabled = false;
      applyPipelineBusyState();
    }
  });
  document.querySelectorAll("[data-add-type]").forEach((button) => button.addEventListener("click", () => addOverlay(button.dataset.addType)));
  elements["open-fx-menu"].addEventListener("click", (event) => {
    // Keep the href as a no-JS fallback, but keep the Studio flow in this
    // dialog when the editor is running.
    event.preventDefault();
    const overlay = currentOverlay();
    openFxPreviewDialog(event.currentTarget, overlay?.type === "fx" ? overlay.id : null);
  });
  elements["fx-search"].addEventListener("input", () => { const overlay = currentOverlay(); if (overlay?.type === "fx") renderFxPanel(overlay); });
  elements["fx-preview-search"].addEventListener("input", fxPreviewRenderCatalog);
  elements["fx-preview-compare-toggle"].addEventListener("click", fxCompareEnter);
  elements["fx-preview-compare-add"].addEventListener("click", fxCompareAdd);
  elements["fx-preview-compare-start"].addEventListener("click", fxCompareStart);
  elements["fx-preview-compare-discard"].addEventListener("click", fxCompareDiscard);
  elements["fx-preview-source"].addEventListener("input", () => {
    if (!fxPreviewDraft) return;
    fxPreviewDraft.window.start_s = Number(elements["fx-preview-source"].value) || 0;
    fxPreviewDraftChanged();
  });
  elements["fx-preview-duration"].addEventListener("input", () => {
    if (!fxPreviewDraft) return;
    fxPreviewDraft.window.duration_s = Number(elements["fx-preview-duration"].value) || 0.01;
    fxPreviewDraftChanged();
  });
  elements["fx-preview-apply-source"].addEventListener("input", () => {
    if (!fxPreviewDraft) return;
    fxPreviewDraft.apply_window.start_s = Number(elements["fx-preview-apply-source"].value) || 0;
    fxPreviewDraftChanged();
  });
  elements["fx-preview-apply-duration"].addEventListener("input", () => {
    if (!fxPreviewDraft) return;
    fxPreviewDraft.apply_window.duration_s = Number(elements["fx-preview-apply-duration"].value) || 0.1;
    fxPreviewDraftChanged();
  });
  elements["fx-preview-mode"].addEventListener("change", () => {
    if (!fxPreviewDraft) return;
    fxPreviewDraft.preview_options.mode = elements["fx-preview-mode"].value;
    fxPreviewDraftChanged();
  });
  elements["fx-preview-quality"].addEventListener("change", () => {
    if (!fxPreviewDraft) return;
    fxPreviewDraft.preview_options.quality = elements["fx-preview-quality"].value;
    fxPreviewDraftChanged();
  });
  elements["fx-preview-text-fit"].addEventListener("change", () => {
    if (!fxPreviewDraft) return;
    fxPreviewDraft.text_fit = elements["fx-preview-text-fit"].checked;
    fxPreviewDraftChanged();
  });
  elements["fx-preview-retry"].addEventListener("click", () => {
    if (!fxPreviewDraft) return;
    // Retry the current standard job without discarding the ready draft that
    // is still visible while the replacement is rendered.
    fxPreviewInvalidateAndSchedule(true);
  });
  elements["fx-preview-cancel-upgrade"].addEventListener("click", () => {
    fxPreviewUpgradeCancelled = true;
    elements["fx-preview-cancel-upgrade"].hidden = true;
    elements["fx-preview-confirm"].disabled = true;
    fxPreviewTrackLifecycle(fxPreviewCancelCurrentJob()).catch(() => {});
    fxPreviewSetStatus("已取消清晰版；快速版仍可播放，但必須取得清晰版才能套用。", "error");
  });
  elements["fx-preview-layout-check-toggle"].addEventListener("click", fxPreviewToggleTextLayoutDetails);
  elements["fx-preview-composition-switch-timeline"].addEventListener("click", fxPreviewSwitchToTimeline);
  elements["fx-preview-resync"].addEventListener("click", () => fxPreviewSyncVideos("effect"));
  elements["fx-preview-source-video"].addEventListener("play", () => fxPreviewPlayBoth("source"));
  elements["fx-preview-video"].addEventListener("play", () => fxPreviewPlayBoth("effect"));
  elements["fx-preview-source-video"].addEventListener("pause", () => fxPreviewPauseBoth("source"));
  elements["fx-preview-video"].addEventListener("pause", () => fxPreviewPauseBoth("effect"));
  elements["fx-preview-source-video"].addEventListener("seeking", () => fxPreviewSyncVideos("source"));
  elements["fx-preview-video"].addEventListener("seeking", () => fxPreviewSyncVideos("effect"));
  elements["fx-preview-source-video"].addEventListener("timeupdate", () => fxPreviewSyncVideos("source"));
  elements["fx-preview-video"].addEventListener("timeupdate", () => fxPreviewSyncVideos("effect"));
  elements["fx-preview-source-video"].addEventListener("click", fxPreviewSetSubjectFromSourceClick);
  elements["fx-preview-restore-draft"].addEventListener("click", fxPreviewRestoreStaleDraft);
  elements["fx-preview-discard-draft"].addEventListener("click", fxPreviewDiscardSavedDraft);
  elements["fx-preview-confirm"].addEventListener("click", confirmFxPreview);
  elements["fx-preview-cancel"].addEventListener("click", closeFxPreviewDialog);
  elements["fx-preview-close"].addEventListener("click", closeFxPreviewDialog);
  elements["fx-preview-dialog"].addEventListener("cancel", (event) => {
    if (!fxCompareActive && !fxCompareStopping && !fxCompareConfirming) return;
    event.preventDefault();
    if (!fxCompareConfirming) closeFxPreviewDialog();
  });
  elements["fx-preview-dialog"].addEventListener("close", cleanupFxPreviewDialog);
  window.addEventListener("beforeunload", () => {
    if (fxPreviewConfirmed) return;
    const jobs = [fxPreviewJob, ...fxCompareCandidates.map((candidate) => candidate.job)]
      .filter((job) => job?.id && !fxPreviewJobTerminationVerified(job));
    [...new Map(jobs.map((job) => [String(job.id), job])).values()].forEach((job) => {
      // Unload delivery is best-effort. Do not mutate local state or claim
      // termination: the next session must still respect any cleanup guard.
      fetch(`/api/fx-previews/${encodeURIComponent(job.id)}/cancel`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Auto-Edit-CSRF": csrfToken },
        body: JSON.stringify({}),
        keepalive: true,
      }).catch(() => {});
    });
  });
  // The 風格菜單 tab (/fx-menu) adds fx layers server-side; pick them up without a manual reload.
  window.addEventListener("message", (event) => {
    if (event.origin !== location.origin || event.data?.type !== "fx-applied") return;
    showToast(`風格菜單已加入 ${event.data.added} 個特效，重新載入…`);
    setTimeout(() => location.reload(), 600);
  });
  elements["asset-upload-button"].addEventListener("click", () => elements["asset-input"].click());
  elements["asset-input"].addEventListener("change", () => uploadAsset(elements["asset-input"].files[0]));
  elements["delete-layer"].addEventListener("click", deleteSelectedOverlay);
  elements["layer-form"].addEventListener("input", updateOverlayFromForm);
  elements["audio-event-form"].addEventListener("input", handleAudioEventInput);
  // Native selects dispatch ``change`` reliably, while ``input`` propagation
  // varies across browser automation and embedded WebKit. Font selection is a
  // render-affecting edit in its own right, so make that path explicit.
  elements["font-family"].addEventListener("change", updateOverlayFromForm);
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
      if (event.shiftKey) {
        redo();
      } else {
        undo();
      }
    }
    if (event.code === "Space" && !["INPUT", "TEXTAREA", "SELECT", "BUTTON", "SUMMARY"].includes(document.activeElement.tagName)) {
      event.preventDefault();
      if (elements["preview-video"].paused) elements["preview-video"].play(); else elements["preview-video"].pause();
    }
  });
}

async function initialize() {
  cacheElements();
  bindEvents();
  bindStylePresets();
  try {
    projectPayload = await request("/api/project");
    renderFormulaPanel(projectPayload);
    state = projectPayload.state;
    markStateSynced();
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
    presetLoad();
    // Load the production desk only after /api/project has established the
    // CSRF token; firing its POST list request during script startup races
    // initialization and produces a harmless but noisy 403 in the console.
    deskRefreshLayers();
    if (projectPayload.render_status?.state === "running") {
      const isBatch = projectPayload.render_status.mode === "batch";
      if (isBatch) {
        batchRenderActive = true;
        renderBatchProgress(projectPayload.render_status);
      }
      setRenderBusy(true);
      pollRenderStatus(isBatch ? "batch" : "single");
    }
    if (PIPELINE_BUSY_STATES.includes(projectPayload.pipeline_status?.state)) pollPipelineStatus();
  } catch (error) {
    elements["stage-empty"].innerHTML = "";
    const message = document.createElement("p");
    message.textContent = `專案載入失敗：${error.message}`;
    elements["stage-empty"].append(message);
    setSaveState("載入失敗", "error");
    showToast(`專案載入失敗：${error.message}`, "error");
  }
}

async function applyCaptionScopeStyle() {
  const overlay = currentOverlay();
  if (!overlay || !["caption", "emphasis"].includes(overlay.type) || overlay.design_role) {
    showToast("請先選取一個字幕圖層", "error");
    return;
  }
  const scope = byIdSafe("caption-style-scope")?.value || "single";
  pushHistory();
  try {
    const generation = editGeneration;
    const response = await request("/api/captions/apply-style", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        scope,
        overlay_id: overlay.id,
        style: overlay.style || {},
      }),
    });
    if (generation !== editGeneration) {
      showToast("套用完成，但你已繼續編輯——保留目前編輯內容", "success");
      return;
    }
    state = response.state;
    stateDirty = false;
    setSaveState("已儲存", "saved");
    showToast(`樣式已套用到 ${response.applied_to.length} 個字幕`, "success");
    renderAll();
    pollCaptionPreviews();
  } catch (error) {
    showToast(`樣式套用失敗：${error.message}`, "error");
  }
}

function byIdSafe(id) {
  return document.getElementById(id);
}

async function renderFontPanel() {
  const panel = byIdSafe("font-panel");
  if (!panel) return;
  try {
    const info = await request("/api/fonts");
    projectFontInfo = info;
    projectFonts = Array.isArray(info.fonts) ? info.fonts.filter((font) => font && font.asset_id) : [];
    populateFontSelect(currentOverlay()?.style || state?.caption_defaults || {});
    const legacy = info.legacy || {};
    const lines = [
      `字型引擎：${info.engine?.status === "present" ? info.engine.version : "不可用（drawtext 後備）"}`,
      projectFonts.length
        ? `已驗證專案字型：${projectFonts.length} 種${info.selected?.asset_id ? `｜目前 ${info.selected.family || info.selected.asset_id}` : ""}`
        : "未選專案字型；將使用未驗證的本機 legacy 字型。",
      info.selection_status === "unavailable"
        ? "⚠ 已選專案字型已缺失、竄改或不覆蓋目前文字；preview/final 會被擋。"
        : "",
      legacy.current ? `Legacy：${legacy.current}（未驗證，不能當作專案字型）` : "Legacy：未解析",
      `許可 fallback：${(info.sanctioned_fallbacks || []).join("、") || "無"}`,
    ];
    for (const name of info.disallowed_fallbacks || []) {
      lines.push(`⚠ 未許可的系統字型 fallback：${name}（final 會被擋）`);
    }
    if (projectFontPreviewError) lines.push(`⚠ ${projectFontPreviewError}`);
    panel.textContent = lines.filter(Boolean).join("｜");
    panel.style.cssText = "font-size:11px;opacity:0.75;margin-top:6px;line-height:1.6";
  } catch (_error) {
    panel.textContent = "⚠ 字型清單載入失敗；已選專案字型預覽不可用，請重試或以 MP4 preview/final 為準。";
    panel.style.cssText = "font-size:11px;opacity:0.75;margin-top:6px;line-height:1.6";
  }
}

function populateFontSelect(style = {}) {
  const select = elements["font-family"];
  if (!select) return;
  const currentId = String(style.font_asset_id || "");
  const currentFamily = String(style.font_family || "");
  select.replaceChildren();
  for (const font of projectFonts) {
    const option = document.createElement("option");
    option.value = String(font.asset_id);
    option.textContent = `${String(font.family || font.asset_id)}${font.style ? ` · ${String(font.style)}` : ""}${font.weight ? ` · ${String(font.weight)}` : ""}`;
    option.dataset.fontAssetId = String(font.asset_id);
    select.append(option);
  }
  const legacy = projectFontInfo?.legacy || {};
  // Always retain an explicit legacy choice. This prevents a project font
  // silently becoming selected for old projects on their next ordinary save.
  {
    const option = document.createElement("option");
    option.value = "__legacy__";
    option.textContent = `Legacy：${currentFamily || legacy.family || legacy.current || "系統字型"}（未驗證）`;
    select.append(option);
  }
  const matching = projectFonts.find((font) => String(font.asset_id) === currentId);
  if (currentId && !matching) {
    const option = document.createElement("option");
    option.value = `__unavailable__:${currentId}`;
    option.textContent = `專案字型 ${currentId}（載入中或不可用；保留既有綁定）`;
    option.disabled = true;
    select.append(option);
    select.value = option.value;
  } else {
    select.value = matching ? currentId : "__legacy__";
  }
}

function projectFontCssFamily(style = {}) {
  const assetId = String(style.font_asset_id || "");
  if (!assetId) {
    return `"${style.font_family || "PingFang TC"}"`;
  }
  if (!projectFonts.some((font) => String(font.asset_id) === assetId)) {
    return `"ProjectFont-${assetId}"`;
  }
  void loadProjectFont(assetId);
  // Do not add the display family as a silent fallback: while the browser
  // bytes are unavailable this is an explicitly degraded preview, never a
  // claim that it matches the selected project asset.
  return `"ProjectFont-${assetId}"`;
}

async function loadProjectFont(assetId) {
  if (!assetId || loadedProjectFontIds.has(assetId) || loadingProjectFontIds.has(assetId)) return;
  if (typeof FontFace === "undefined") {
    projectFontPreviewError = "此瀏覽器不支援專案字型預覽；請以 MP4 preview/final 為準。";
    renderFontPanel();
    return;
  }
  loadingProjectFontIds.add(assetId);
  try {
    const face = new FontFace(`ProjectFont-${assetId}`, `url(/api/fonts/${encodeURIComponent(assetId)}/bytes)`);
    await face.load();
    document.fonts.add(face);
    loadedProjectFontIds.add(assetId);
    projectFontPreviewError = "";
    renderPreviewOverlays(true);
  } catch (_error) {
    // Final rendering remains authoritative. Do not replace a failed project
    // preview font with an arbitrary browser-family fallback.
    loadedProjectFontIds.delete(assetId);
    projectFontPreviewError = "瀏覽器預覽字型載入失敗；請重試，MP4 preview/final 會阻擋未驗證字型。";
    renderFontPanel();
  } finally {
    loadingProjectFontIds.delete(assetId);
  }
}

// ---- 字幕字型挑選器（2026-10-22）：已驗證專案字型用實際字型檔預覽；目錄裡 Google Fonts 有的字型可一鍵匯入 ----
let captionFontPop = null;
let captionFontImportBusy = false;
function closeCaptionFontPop() { if (captionFontPop) { captionFontPop.remove(); captionFontPop = null; } }
document.addEventListener("click", (ev) => { if (captionFontPop && !captionFontPop.contains(ev.target)) closeCaptionFontPop(); });
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeCaptionFontPop(); });
function captionFontSelectAsset(assetId) {
  const select = elements["font-family"];
  if (!select) return;
  if (![...select.options].some((o) => o.value === assetId)) populateFontSelect({ font_asset_id: assetId });
  select.value = assetId;
  updateOverlayFromForm();
  loadProjectFont(assetId);
}
async function captionFontImportFromGoogle(entry, statusEl) {
  if (captionFontImportBusy) return;
  captionFontImportBusy = true;
  const say = (t) => { if (statusEl) statusEl.textContent = t; };
  try {
    say("記錄來源同意…");
    const consent = await request("/api/providers/consent", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider_id: entry.google ? "google-fonts" : "github-fonts", consented: true, confirmed_by: "editor" }) });
    if (consent && consent.ok === false) throw new Error(consent.error || "來源同意記錄失敗");
    const providerId = entry.google ? "google-fonts" : "github-fonts";
    const query = entry.google ? entry.google : entry.id;
    say(providerId === "google-fonts" ? "向 Google Fonts 倉庫查詢…" : "向作者 GitHub 倉庫（允許清單）查詢…");
    const found = await request("/api/assets/search", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider_id: providerId, query, page: 1 }) });
    if (found && found.ok === false) throw new Error(found.error || "查詢失敗");
    const results = Array.isArray(found?.items) ? found.items : (Array.isArray(found?.results) ? found.results : []);
    if (!results.length) throw new Error("倉庫裡沒有這個字型");
    // Prefer the upright face: "Family[wght].ttf" / "Family-Regular.ttf" over any "-Italic" file.
    const nameOf = (r) => String(r.candidate_id || r.download_url || "").split(":").pop();
    const upright = results.filter((r) => !/italic/i.test(nameOf(r)));
    const pick = upright.find((r) => /\[|-Regular\.ttf$/.test(nameOf(r))) || upright[0] || results[0];
    say("下載、驗證、寫收據…");
    const imported = await request("/api/assets/import-provider", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ import_token: pick.import_token }) });
    if (imported && imported.ok === false) throw new Error(imported.error || "匯入失敗");
    const assetId = imported?.asset_id || imported?.asset?.asset_id || imported?.item?.asset_id || "";
    await renderFontPanel();
    const match = projectFonts.find((f) => (assetId && f.asset_id === assetId) || String(f.family || "").toLowerCase() === String(entry.family).toLowerCase());
    if (match) captionFontSelectAsset(match.asset_id);
    showToast(`已匯入並套用字型：${entry.label}`, "success");
    closeCaptionFontPop();
  } catch (error) {
    const hint = /security policy/.test(String(error.message))
      ? "（驗證器拒收：最常見是字型沒有字幕用到的字，例如英文字型配中文字幕；或字型太複雜超出安全上限）"
      : "";
    say(`失敗：${error.message}${hint}`);
    showToast(`字型匯入失敗：${error.message}${hint}`, "error");
  } finally {
    captionFontImportBusy = false;
  }
}
function openCaptionFontPop(anchor) {
  closeCaptionFontPop();
  const style = currentOverlay()?.style || state?.caption_defaults || {};
  const currentId = String(style.font_asset_id || "");
  const catalogFonts = (fxCatalog().fonts || []);
  const importedFamilies = new Set(projectFonts.map((f) => String(f.family || "").toLowerCase()));
  const rect = anchor.getBoundingClientRect();
  const pop = document.createElement("div"); pop.className = "fx-font-pop"; captionFontPop = pop;
  pop.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - 450))}px`;
  pop.style.top = `${Math.min(rect.bottom + 6, window.innerHeight - 80)}px`;
  pop.addEventListener("click", (ev) => ev.stopPropagation());
  let q = "";
  const input = document.createElement("input"); input.type = "search"; input.placeholder = "搜字型名、用途…"; input.className = "fx-font-q";
  const list = document.createElement("div"); list.className = "fx-font-list";
  const status = document.createElement("div"); status.className = "fx-font-sep";
  const sep = (text) => { const d = document.createElement("div"); d.className = "fx-font-sep"; d.textContent = text; return d; };
  const row = ({ sampleText, family, label, note, selected, action, actionLabel }) => {
    const b = document.createElement("button"); b.type = "button"; b.className = `fx-font-row${selected ? " is-selected" : ""}`;
    const sm = document.createElement("span"); sm.className = "fx-font-sample"; sm.textContent = sampleText;
    sm.style.fontFamily = family;
    const nm = document.createElement("span"); nm.className = "fx-font-name"; nm.textContent = label;
    const small = document.createElement("small"); small.textContent = `${note || ""}${actionLabel ? `　→ ${actionLabel}` : ""}`; nm.append(small);
    b.append(sm, nm);
    b.addEventListener("click", action);
    return b;
  };
  const render = () => {
    const term = q.trim().toLowerCase();
    const rows = [];
    rows.push(sep("已驗證專案字型（用實際字型檔預覽）"));
    const own = projectFonts.filter((f) => !term || [f.family, f.style, f.asset_id].join(" ").toLowerCase().includes(term));
    own.forEach((f) => {
      loadProjectFont(f.asset_id);
      rows.push(row({ sampleText: "字幕 Caption Aa 0123", family: `"ProjectFont-${f.asset_id}"`, label: String(f.family || f.asset_id),
        note: `${f.style || ""}${f.weight ? ` · ${f.weight}` : ""}${f.license_spdx ? ` · ${f.license_spdx}` : ""}`, selected: currentId === f.asset_id,
        action: () => { captionFontSelectAsset(f.asset_id); closeCaptionFontPop(); } }));
    });
    if (!own.length) rows.push(sep("（尚無專案字型）"));
    rows.push(row({ sampleText: "Legacy 系統字型", family: '"PingFang TC","Noto Sans TC",sans-serif', label: "Legacy（未驗證）", note: "不能當專案字型；final 會標記",
      selected: !currentId, action: () => { elements["font-family"].value = "__legacy__"; updateOverlayFromForm(); closeCaptionFontPop(); } }));
    rows.push(sep("可一鍵匯入（Google Fonts／作者 GitHub 允許清單，匯入後走驗證與收據）"));
    const importable = catalogFonts.filter((f) => (f.google || f.github) && !importedFamilies.has(String(f.family).toLowerCase())
      && (!term || [f.label, f.family, f.note, f.cat, f.id].join(" ").toLowerCase().includes(term)));
    importable.forEach((f) => rows.push(row({ sampleText: f.sample || "Aa", family: `"${f.family}", ${FX_FONT_FALLBACK[f.role === "body" ? "body" : "display"]}`,
      label: f.label, note: `${f.note || ""}${f.family !== f.label ? `　${f.family}` : ""}`, selected: false, actionLabel: "匯入並套用",
      action: () => captionFontImportFromGoogle(f, status) })));
    if (!importable.length) rows.push(sep("（沒有符合的可匯入字型）"));
    rows.push(status);
    list.replaceChildren(...rows);
  };
  input.addEventListener("input", () => { q = input.value; render(); });
  pop.append(input, list); document.body.append(pop); render(); input.focus();
}
byIdSafe("font-pick-open")?.addEventListener("click", (ev) => { ev.preventDefault(); ev.stopPropagation(); openCaptionFontPop(ev.currentTarget); });

byIdSafe("apply-caption-scope")?.addEventListener("click", applyCaptionScopeStyle);
renderFontPanel();

byIdSafe("style-pack-select")?.addEventListener("change", (event) => {
  if (!state) return;
  pushHistory();
  const packId = event.target.value || null;
  state.style_pack = packId
    ? { project_default: packId, per_highlight: state.style_pack?.per_highlight || {} }
    : null;
  if (state.style_pack === null) delete state.style_pack;
  markDirty("視覺風格包已變更（結構卡與字幕預設將重新渲染）");
});

// ---------------------------------------------------------------------------
// 產出工作台：結構卡 CRUD／素材指派／變體生命週期／授權核可（Phase 1c UI）
// ---------------------------------------------------------------------------

async function deskPost(body) {
  return request("/api/structured-layers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function deskRefreshLayers() {
  const host = byIdSafe("desk-layer-list");
  if (!host) return;
  try {
    const bundle = await deskPost({ action: "list" });
    host.replaceChildren();
    for (const layer of bundle.layers?.items || []) {
      const beat = (bundle.visual_plan?.items || []).find(
        (item) => item.structured_layer_id === layer.id
      );
      const row = document.createElement("div");
      row.className = "batch-output-row";
      const label = document.createElement("span");
      label.textContent =
        `${layer.type}｜${layer.id.slice(-8)}` +
        (beat ? `｜${beat.start}s–${beat.end}s` : "");
      row.append(label);
      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "icon-button";
      edit.textContent = "編";
      edit.addEventListener("click", () => deskEditLayer(layer, beat));
      row.append(edit);
      const del = document.createElement("button");
      del.type = "button";
      del.className = "icon-button";
      del.textContent = "刪";
      del.addEventListener("click", async () => {
        await deskPost({ action: "delete", id: layer.id });
        showToast("結構卡已刪除；核可已失效", "success");
        deskRefreshLayers();
      });
      row.append(del);
      host.append(row);
    }
  } catch (_error) {
    host.textContent = "";
  }
}

let deskEditingLayerId = null;

async function deskSaveLayer() {
  let payload;
  try {
    payload = JSON.parse(byIdSafe("desk-layer-payload").value || "{}");
  } catch (error) {
    showToast(`payload JSON 解析失敗：${error.message}`, "error");
    return;
  }
  const body = {
    action: "upsert",
    layer: { type: byIdSafe("desk-layer-type").value, payload },
    timing: {
      start: Number(byIdSafe("desk-layer-start").value || 0),
      end: Number(byIdSafe("desk-layer-end").value || 0),
    },
  };
  if (deskEditingLayerId) body.id = deskEditingLayerId;
  try {
    await deskPost(body);
    showToast(
      deskEditingLayerId
        ? "結構卡已更新；timeline 核可已失效"
        : "結構卡已建立；timeline 核可已失效",
      "success"
    );
    deskEditingLayerId = null;
    byIdSafe("desk-layer-save").textContent = "新增結構卡";
    deskRefreshLayers();
  } catch (error) {
    showToast(`結構卡儲存失敗：${error.message}`, "error");
  }
}

function deskEditLayer(layer, beat) {
  deskEditingLayerId = layer.id;
  byIdSafe("desk-layer-type").value = layer.type;
  byIdSafe("desk-layer-payload").value = JSON.stringify(layer.payload || {}, null, 0);
  byIdSafe("desk-layer-start").value = beat ? beat.start : 0;
  byIdSafe("desk-layer-end").value = beat ? beat.end : 0;
  byIdSafe("desk-layer-save").textContent = `更新 ${layer.id.slice(-8)}`;
}

async function deskRefreshAssets() {
  const host = byIdSafe("desk-asset-library");
  if (!host) return;
  // The provider catalog is fetched the first time this details panel opens;
  // subsequent library refreshes (including after import) reuse it.
  void deskLoadProviders();
  try {
    const library = await request("/api/assets/library");
    host.replaceChildren();
    for (const asset of library.assets || []) {
      const row = document.createElement("div");
      row.className = "batch-output-row";
      const label = document.createElement("span");
      const pathName = String(asset.path || "").split("/").pop();
      let labelText = `${asset.kind}｜${pathName}`;
      if (["provider_id", "license_spdx", "review_status"].some((key) => key in asset)) {
        const providerId = String(asset.provider_id || "未知來源");
        const license = String(asset.license_spdx || "UNKNOWN");
        const review = asset.review_status === "approved" ? "已驗證" : "待審";
        labelText += `｜${providerId}｜${license}｜${review}`;
      }
      if (asset.asserted) labelText += "｜✓已授權";
      label.textContent = labelText;
      row.append(label);
      const assign = document.createElement("button");
      assign.type = "button";
      assign.className = "icon-button";
      assign.textContent = "＋beat";
      assign.title = "以目前播放位置起 3 秒建立素材 beat";
      assign.addEventListener("click", async () => {
        const total = duration() || 3;
        const start = Math.min(elements["preview-video"].currentTime || 0, Math.max(0, total - 0.2));
        const end = Math.min(start + 3, total);
        try {
          await deskPost({
            action: "asset_beat",
            asset: {
              path: asset.path,
              beat: asset.kind === "video" ? "broll" : "image",
            },
            timing: { start, end },
          });
          showToast("素材 beat 已建立", "success");
        } catch (error) {
          showToast(`素材 beat 建立失敗：${error.message}`, "error");
        }
      });
      row.append(assign);
      host.append(row);
    }
    if (!host.children.length) host.textContent = "（素材庫是空的）";
  } catch (_error) {
    host.textContent = "";
  }
}

function deskProviderElements() {
  return {
    select: byIdSafe("desk-provider-select"),
    query: byIdSafe("desk-provider-query"),
    consent: byIdSafe("desk-provider-consent"),
    disclosure: byIdSafe("desk-provider-disclosure"),
    search: byIdSafe("desk-provider-search"),
    status: byIdSafe("desk-provider-status"),
    results: byIdSafe("desk-provider-results"),
  };
}

function deskProviderIsAvailable(provider) {
  // Older image-provider payloads did not expose availability.  Keep those
  // providers usable while treating an explicit false as unavailable.
  return provider?.available !== false;
}

function deskProviderAvailabilityMessage(provider) {
  if (deskProviderIsAvailable(provider)) return "";
  const code = String(provider?.availability_code || "");
  return PROVIDER_AVAILABILITY_MESSAGES[code] || "此素材來源目前不可用。";
}

function deskProviderSearchDisabled(provider, busy = deskProviderSearchBusy) {
  return !deskProviderIsAvailable(provider) || Boolean(busy);
}

function deskSelectedProvider() {
  const { select } = deskProviderElements();
  const providerId = select?.value || "";
  return deskProviderRecords.find((provider) => String(provider.id) === providerId) || null;
}

function deskRenderProviderDisclosure() {
  const { select, consent, disclosure, search, status, results } = deskProviderElements();
  const provider = deskSelectedProvider();
  if (!provider) {
    if (consent) {
      consent.checked = false;
      consent.disabled = true;
    }
    if (disclosure) disclosure.textContent = "尚未載入來源揭露。";
    if (search) search.disabled = true;
    if (status) status.textContent = "目前沒有可用的開放素材來源。";
    if (results) results.replaceChildren();
    return;
  }
  if (!deskProviderIsAvailable(provider)) {
    const reason = deskProviderAvailabilityMessage(provider);
    if (consent) {
      consent.checked = false;
      consent.disabled = true;
    }
    if (disclosure) disclosure.textContent = reason;
    if (search) search.disabled = true;
    if (status) status.textContent = reason;
    if (results) results.replaceChildren();
    return;
  }
  const consentRequired = provider.consent_required !== false;
  const consented = Boolean(provider.consented);
  if (consent) {
    consent.checked = consented;
    consent.disabled = false;
  }
  if (disclosure) {
    const networkDisclosure = String(
      provider.network_disclosure || "搜尋會向所選來源送出關鍵詞；匯入後只保存本機檔案。",
    );
    disclosure.textContent = `${networkDisclosure}｜${consentRequired ? (consented ? "已同意" : "尚未同意") : "不需額外同意"}`;
  }
  if (search) search.disabled = deskProviderSearchDisabled(provider);
  if (status) {
    status.textContent = consentRequired && !consented
      ? "請閱讀揭露並勾選同意後搜尋。"
      : `已選擇 ${String(provider.label || provider.id)}，可開始搜尋。`;
  }
}

async function deskLoadProviders() {
  const { select, status } = deskProviderElements();
  if (!select || deskProviderStatusLoaded) {
    deskRenderProviderDisclosure();
    deskWireGenerateImage();
    deskWireAutoVisuals();
    return;
  }
  if (status) status.textContent = "正在載入開放素材來源…";
  try {
    const payload = await request("/api/providers/status");
    if (payload && payload.ok === false) throw new Error(payload.error || "來源狀態無法取得");
    deskProviderRecords = Array.isArray(payload?.providers) ? payload.providers : [];
    select.replaceChildren();
    for (const provider of deskProviderRecords) {
      if (!provider || !provider.id) continue;
      const option = document.createElement("option");
      option.value = String(provider.id);
      const label = String(provider.label || provider.id);
      option.textContent = deskProviderIsAvailable(provider) ? label : `${label}（不可用）`;
      if (!deskProviderIsAvailable(provider)) option.title = deskProviderAvailabilityMessage(provider);
      select.append(option);
    }
    deskProviderStatusLoaded = true;
    select.disabled = deskProviderRecords.length === 0;
    const firstAvailable = deskProviderRecords.find((provider) => deskProviderIsAvailable(provider));
    if (firstAvailable) select.value = String(firstAvailable.id);
    deskRenderProviderDisclosure();
  } catch (error) {
    select.disabled = true;
    if (status) status.textContent = `來源狀態載入失敗：${error.message}`;
    showToast(`開放素材來源載入失敗：${error.message}`, "error");
  }
}

function deskClearProviderResults(message = "") {
  const { results } = deskProviderElements();
  if (!results) return;
  results.replaceChildren();
  if (message) results.textContent = message;
}

function deskSafeLandingLink(rawUrl) {
  try {
    const url = new URL(String(rawUrl || ""));
    if (url.protocol !== "https:") return null;
    return url;
  } catch (_error) {
    return null;
  }
}

function deskProviderResultText(value, fallback = "—") {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

function deskProviderResultIsSvg(result, provider = deskSelectedProvider()) {
  return provider?.kind === "svg"
    || result?.kind === "svg"
    || String(result?.mime_type || "").toLowerCase() === "image/svg+xml";
}

function deskProviderResultIsFont(result, provider = deskSelectedProvider()) {
  return provider?.kind === "font" || result?.kind === "font"
    || /^font\//i.test(String(result?.mime_type || ""));
}

async function deskImportProviderAsset(result, row, button, importStatus) {
  const importToken = typeof result?.import_token === "string" ? result.import_token : "";
  if (!importToken || !button || button.disabled) return;
  const providerAtImport = deskSelectedProvider();
  button.disabled = true;
  if (importStatus) importStatus.textContent = "匯入中…";
  try {
    const payload = await request("/api/assets/import-provider", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ import_token: importToken }),
    });
    if (payload && payload.ok === false) throw new Error(payload.error || "匯入失敗");
    row?.classList.add("is-imported");
    button.textContent = "已匯入";
    if (importStatus) importStatus.textContent = "已匯入；來源與授權資訊已保存。";
    showToast("素材已匯入本機素材庫", "success");
    // SVG imports publish only a receipt-bound PNG derivative.  Refresh the
    // asset library; rights UI is intentionally not used as an SVG import
    // side-channel.  Preserve the existing image-provider refresh behavior.
    if (deskProviderResultIsFont(result, providerAtImport)) {
      await Promise.all([deskRefreshAssets(), renderFontPanel()]);
    } else if (deskProviderResultIsSvg(result, providerAtImport)) {
      await deskRefreshAssets();
    } else {
      await Promise.all([deskRefreshAssets(), deskRefreshRights()]);
    }
  } catch (error) {
    button.disabled = false;
    if (importStatus) importStatus.textContent = `匯入失敗：${error.message}`;
    showToast(`素材匯入失敗：${error.message}`, "error");
  }
}

function deskRenderProviderResults(results) {
  const { results: host, status } = deskProviderElements();
  if (!host) return;
  const provider = deskSelectedProvider();
  host.replaceChildren();
  if (!Array.isArray(results) || results.length === 0) {
    host.textContent = "找不到符合條件的素材。";
    if (status) status.textContent = "搜尋完成：0 筆結果。";
    return;
  }
  if (status) status.textContent = `搜尋完成：${results.length} 筆結果。`;
  for (const result of results) {
    const row = document.createElement("article");
    row.className = "provider-result-row";
    const isSvgResult = deskProviderResultIsSvg(result, provider);
    const isFontResult = deskProviderResultIsFont(result, provider);
    row.dataset.kind = isFontResult ? "font" : (isSvgResult ? "svg" : "image");

    const title = document.createElement("strong");
    title.textContent = deskProviderResultText(result?.title, "（未命名素材）");
    row.append(title);

    const creator = document.createElement("span");
    creator.className = "provider-result-creator";
    creator.textContent = `作者：${deskProviderResultText(result?.creator)}`;
    row.append(creator);

    const dimensions = result?.width && result?.height
      ? `${deskProviderResultText(result.width)}×${deskProviderResultText(result.height)}`
      : "尺寸未知";
    const details = document.createElement("span");
    details.className = "provider-result-details";
    details.textContent = isFontResult
      ? `字型｜授權：${deskProviderResultText(result?.license_spdx, "UNKNOWN")}｜覆蓋：${deskProviderResultText(result?.coverage || result?.scripts)}｜${deskProviderResultText(result?.family, "未命名字族")}`
      : `${isSvgResult ? "格式：SVG｜" : ""}授權：${deskProviderResultText(result?.license_spdx, "UNKNOWN")}｜尺寸：${dimensions}｜${result?.attribution_required ? "需列名" : "可免列名"}`;
    row.append(details);

    const actions = document.createElement("div");
    actions.className = "provider-result-actions";
    const landingUrl = deskSafeLandingLink(result?.landing_url);
    if (landingUrl) {
      const sourceLink = document.createElement("a");
      sourceLink.href = landingUrl.toString();
      sourceLink.target = "_blank";
      sourceLink.rel = "noopener noreferrer";
      sourceLink.textContent = "來源頁";
      actions.append(sourceLink);
    }
    const importButton = document.createElement("button");
    importButton.type = "button";
    importButton.className = "icon-button provider-import-button";
    importButton.textContent = "匯入";
    if (typeof result?.import_token !== "string" || !result.import_token) {
      importButton.disabled = true;
    }
    const importStatus = document.createElement("span");
    importStatus.className = "provider-import-status";
    importStatus.setAttribute("role", "status");
    importStatus.setAttribute("aria-live", "polite");
    importButton.addEventListener("click", () => deskImportProviderAsset(result, row, importButton, importStatus));
    actions.append(importButton, importStatus);
    row.append(actions);
    host.append(row);
  }
}

function deskProviderChanged() {
  deskClearProviderResults();
  deskRenderProviderDisclosure();
}

async function deskSearchProviders() {
  const { select, query, consent, search, status } = deskProviderElements();
  const provider = deskSelectedProvider();
  const trimmedQuery = String(query?.value || "").trim();
  if (!provider || !select?.value) {
    if (status) status.textContent = "請先選擇素材來源。";
    return;
  }
  if (!deskProviderIsAvailable(provider)) {
    const reason = deskProviderAvailabilityMessage(provider);
    if (status) status.textContent = reason;
    if (search) search.disabled = true;
    return;
  }
  if (!trimmedQuery) {
    if (status) status.textContent = "請輸入搜尋關鍵詞。";
    showToast("請輸入搜尋關鍵詞", "error");
    return;
  }
  if (!consent?.checked) {
    if (status) status.textContent = "搜尋前必須勾選網路揭露同意。";
    showToast("請先勾選網路揭露同意", "error");
    return;
  }
  if (!search || search.disabled) return;
  deskProviderSearchBusy = true;
  search.disabled = true;
  deskClearProviderResults();
  try {
    if (provider.consent_required !== false && !provider.consented) {
      if (status) status.textContent = "正在記錄來源同意…";
      const consentPayload = await request("/api/providers/consent", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider_id: String(provider.id), consented: true, confirmed_by: "editor" }),
      });
      if (consentPayload && consentPayload.ok === false) {
        throw new Error(consentPayload.error || "來源同意記錄失敗");
      }
      provider.consented = true;
      deskRenderProviderDisclosure();
    }
    if (status) status.textContent = "搜尋中…";
    const payload = await request("/api/assets/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider_id: String(provider.id), query: trimmedQuery, page: 1 }),
    });
    if (payload && payload.ok === false) throw new Error(payload.error || "素材搜尋失敗");
    // The provider service exposes candidates as `items`. Keep `results` as a
    // temporary compatibility fallback for older mocked integrations.
    deskRenderProviderResults(payload?.items || payload?.results || []);
  } catch (error) {
    deskClearProviderResults(`搜尋失敗：${error.message}`);
    if (status) status.textContent = `搜尋失敗：${error.message}`;
    showToast(`開放素材搜尋失敗：${error.message}`, "error");
  } finally {
    deskProviderSearchBusy = false;
    if (search) search.disabled = deskProviderSearchDisabled(deskSelectedProvider(), false);
  }
}

async function deskRefreshVariants() {
  const host = byIdSafe("desk-variant-list");
  const presetSelect = byIdSafe("desk-variant-preset");
  if (!host || !presetSelect) return;
  if (!presetSelect.options.length && projectPayload?.platform_presets) {
    for (const [id, preset] of Object.entries(projectPayload.platform_presets)) {
      const option = document.createElement("option");
      option.value = id;
      option.textContent = preset.label;
      presetSelect.append(option);
    }
  }
  try {
    const status = await request("/api/variants/status");
    host.replaceChildren();
    for (const variant of status.variants || []) {
      const row = document.createElement("div");
      row.className = "batch-output-row";
      const summary = document.createElement("span");
      summary.textContent =
        `${variant.variant_id}｜${variant.preset_id}` +
        `｜TL:${variant.timeline?.approved ? "✓" : "—"}` +
        `｜FN:${variant.final?.approved ? "✓" : "—"}`;
      row.append(summary);
      const actions = [
        ["預覽", () => deskRenderVariant(variant.variant_id, "preview")],
        ["核TL", () => deskApproveVariant(variant.variant_id, "timeline", variant.timeline?.revision)],
        ["出FN", () => deskRenderVariant(variant.variant_id, "final")],
        ["核FN", () => deskApproveVariant(variant.variant_id, "final", variant.final?.revision)],
      ];
      for (const [text, handler] of actions) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "icon-button";
        button.textContent = text;
        button.addEventListener("click", handler);
        row.append(button);
      }
      if (variant.delivery?.output && variant.final?.approved) {
        const link = document.createElement("a");
        link.href = `/${variant.delivery.output}`;
        link.download = "";
        link.textContent = "下載";
        row.append(link);
      }
      host.append(row);
    }
    if (!host.children.length) host.textContent = "（尚未建立變體）";
  } catch (_error) {
    host.textContent = "";
  }
}

function deskAddVariant() {
  const preset = byIdSafe("desk-variant-preset").value;
  if (!preset || !state) return;
  pushHistory();
  state.variants = state.variants || [];
  const shortName = preset.replace(/[^a-z0-9-]/g, "").slice(0, 20);
  const suffix = Math.random().toString(16).slice(2, 8);
  state.variants.push({
    variant_id: `${shortName}-${suffix}`,
    preset_id: preset,
    overrides: [],
  });
  markDirty("已新增輸出變體");
  setTimeout(deskRefreshVariants, 1400);
}

async function deskRenderVariant(variantId, quality) {
  try {
    await request("/api/render-variant", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ variant_id: variantId, quality }),
    });
    showToast(`變體 ${variantId} ${quality} 輸出中…`, "success");
    deskWatchVariantRender(variantId, quality);
  } catch (error) {
    showToast(`變體輸出失敗：${error.message}`, "error");
  }
}

async function deskWatchVariantRender(variantId, quality, attempt = 0) {
  try {
    const status = await request("/api/render-status");
    if (status.state === "rendering" && attempt < 240) {
      setTimeout(() => deskWatchVariantRender(variantId, quality, attempt + 1), 1000);
      return;
    }
    if (status.state === "done" && status.variant_id === variantId) {
      showToast(`變體 ${variantId} 輸出完成`, "success");
      if (quality === "preview" && status.output) {
        // 直接切換播放器到 variant 預覽（輸出同源檢視）
        elements["preview-video"].src = `/${status.output}`;
        showingRenderedMedia = true;
        elements["overlay-layer"].hidden = true;
        elements["preview-video"].play?.();
      }
      deskRefreshVariants();
      return;
    }
    if (status.state === "error") {
      showToast(`變體輸出失敗：${status.message}`, "error");
    }
  } catch (_error) {
    /* status endpoint hiccup — silent */
  }
}

async function deskApproveVariant(variantId, gate, revision, isRetry = false) {
  if (!revision) {
    showToast("尚無可核可的 revision；先儲存／輸出", "error");
    return;
  }
  try {
    await request("/api/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        gate,
        variant_id: variantId,
        expected_revision: revision,
        confirmed_by: "editor",
      }),
    });
    showToast(`變體 ${variantId} ${gate} 已核可`, "success");
    deskRefreshVariants();
  } catch (error) {
    if (!isRetry) {
      // 快照在點擊與請求間漂移（背景渲染等）：同一次點擊意圖內
      // 取回 server 當下 revision 重試一次；再失敗才回報。
      try {
        const status = await request("/api/variants/status");
        const fresh = (status.variants || []).find(
          (variant) => variant.variant_id === variantId
        );
        const freshRevision = fresh?.[gate]?.revision;
        if (freshRevision && freshRevision !== revision) {
          return deskApproveVariant(variantId, gate, freshRevision, true);
        }
      } catch (_refreshError) {
        /* fall through to the error toast */
      }
    }
    showToast(`核可失敗：${error.message}`, "error");
    deskRefreshVariants();
  }
}

async function deskRefreshRights() {
  const host = byIdSafe("desk-rights-list");
  if (!host) return;
  try {
    const rights = await request("/api/rights");
    host.replaceChildren();
    for (const input of rights.inputs || []) {
      if (!input.requires_assertion) continue;
      const row = document.createElement("div");
      row.className = "batch-output-row";
      const label = document.createElement("span");
      label.textContent =
        `${input.path.split("/").pop()}｜${input.asserted ? "✓已授權" : "未授權"}`;
      row.append(label);
      if (!input.asserted) {
        const assertButton = document.createElement("button");
        assertButton.type = "button";
        assertButton.className = "icon-button";
        assertButton.textContent = "本人作品✓";
        assertButton.addEventListener("click", async () => {
          await request("/api/rights/assert", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ asset_path: input.path, basis: "own_work" }),
          });
          showToast("授權已記錄", "success");
          deskRefreshRights();
        });
        row.append(assertButton);
      }
      host.append(row);
    }
    if (!host.children.length) host.textContent = "（目前沒有需要授權的素材）";
  } catch (_error) {
    host.textContent = "";
  }
}

function bindProductionDesk() {
  byIdSafe("desk-layer-save")?.addEventListener("click", deskSaveLayer);
  byIdSafe("desk-variant-add")?.addEventListener("click", deskAddVariant);
  byIdSafe("desk-provider-select")?.addEventListener("change", deskProviderChanged);
  byIdSafe("desk-provider-search")?.addEventListener("click", deskSearchProviders);
  const refreshers = [
    ["desk-layers", deskRefreshLayers],
    ["desk-assets", deskRefreshAssets],
    ["desk-variants", deskRefreshVariants],
    ["desk-rights", deskRefreshRights],
  ];
  for (const [id, refresh] of refreshers) {
    byIdSafe(id)?.addEventListener("toggle", (event) => {
      if (event.target.open) refresh();
    });
  }
}

bindProductionDesk();


function deskWireGenerateImage() {
  const button = document.getElementById("desk-generate-button");
  const beatInput = document.getElementById("desk-generate-beat");
  const promptInput = document.getElementById("desk-generate-prompt");
  const status = document.getElementById("desk-generate-status");
  const preview = document.getElementById("desk-generate-preview");
  if (!button || !beatInput || !promptInput) return;

  button.addEventListener("click", async () => {
    const beatId = beatInput.value.trim();
    const prompt = promptInput.value.trim();
    if (!beatId || !prompt) {
      if (status) status.textContent = "要填段落 ID 和想要的畫面。";
      return;
    }
    button.disabled = true;
    if (status) status.textContent = "生成中，第一次大約 40 秒…";
    try {
      const payload = await request("/api/generate-image", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ beat_id: beatId, prompt }),
      });
      if (payload && payload.ok === false) throw new Error(payload.error || "生成失敗");
      const image = payload.image || {};
      if (status) {
        status.textContent = image.reused
          ? "這一段已經有圖了，沿用既有的。"
          : "生成完成，已存進專案素材。";
      }
      if (preview && image.path) {
        preview.src = `/${image.path}?v=${encodeURIComponent(image.sha256 || "")}`;
        preview.hidden = false;
      }
    } catch (error) {
      if (status) status.textContent = `生成失敗：${error.message}`;
    } finally {
      button.disabled = false;
    }
  });
}


function deskWireAutoVisuals() {
  const button = document.getElementById("desk-auto-button");
  const status = document.getElementById("desk-auto-status");
  if (!button) return;
  button.addEventListener("click", async () => {
    button.disabled = true;
    if (status) status.textContent = "排版中…";
    try {
      const payload = await request("/api/auto-visuals", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (payload && payload.ok === false) throw new Error(payload.error || "排版失敗");
      const beats = payload.beats || {};
      const decorated = Object.entries(beats)
        .filter(([beat]) => beat !== "keep_aroll")
        .map(([beat, count]) => `${beat} ${count}`)
        .join("、");
      if (status) {
        status.textContent = decorated
          ? `排好了：${decorated}，其餘維持原畫面。`
          : "逐字稿沒有可用的數字或列舉，全部維持原畫面。";
      }
      if (typeof refreshProject === "function") await refreshProject();
    } catch (error) {
      if (status) status.textContent = `排版失敗：${error.message}`;
    } finally {
      button.disabled = false;
    }
  });
}

// Portable presets contain only the documented settings, never project state.
let presetLibrary = { revision: "", presets: [] };
let presetLibraryEpoch = 0;
const presetNode = (id) => document.getElementById(id);
function presetStatus(message) { presetNode("preset-status").textContent = message; }
function presetSelected() { return presetLibrary.presets.find((p) => p.id === presetNode("preset-list").value) || null; }
function presetSetLibrary(result, selected = "") {
  presetLibrary = { revision: result.revision, presets: Array.isArray(result.presets) ? result.presets : [] };
  const list = presetNode("preset-list");
  const placeholder = document.createElement("option"); placeholder.value = ""; placeholder.textContent = "選擇預設";
  list.replaceChildren(placeholder);
  for (const preset of presetLibrary.presets) {
    const option = document.createElement("option"); option.value = preset.id;
    const unavailable = preset.availability?.status === "unavailable";
    option.textContent = `${preset.name}（${preset.kind === "caption" ? "字幕" : "特效"}${unavailable ? preset.availability.reason === "font_unavailable" ? "・缺字型" : "・暫時不可用" : ""}）`; list.append(option);
  }
  list.value = selected;
}
async function presetLoad() {
  const epoch = ++presetLibraryEpoch;
  try {
    const result = await request("/api/style-presets");
    if (epoch !== presetLibraryEpoch) return;
    const selected = presetNode("preset-list").value;
    presetSetLibrary(result, selected);
    const current = presetSelected();
    presetStatus(current?.availability?.status === "unavailable" ? (current.availability.message || "此預設暫時不可用，可更新或刪除。") : "預設庫已載入。");
  } catch (error) { if (epoch === presetLibraryEpoch) presetStatus(`預設庫讀取失敗：${error.message}`); }
}
function presetCapture() {
  const overlay = currentOverlay();
  if (!overlay || !["caption", "fx"].includes(overlay.type) || overlay.design_role) throw new Error("請先選取已確認的字幕或特效圖層。");
  if (stateDirty || presetSaveInFlight || !lastSaveSucceeded) throw new Error("目前編輯尚未儲存完成，請稍候再試。");
  if (overlay.type === "fx") {
    const settings = { code: overlay.code, values: deepCopy(overlay.values || {}), placement: overlay.placement || "", scheme: overlay.scheme || "", font_ids: deepCopy(overlay.font_ids || {}), opacity: overlay.style?.opacity ?? 1, text_fit: overlay.text_fit !== false };
    return { kind: "fx", settings };
  }
  const raw = overlay.style || {};
  const defaults = state.caption_defaults || {};
  const style = {
    font_size: Number(presetNode("font-size").value), color: presetNode("font-color").value.toUpperCase(),
    emphasis_color: presetNode("emphasis-color").value.toUpperCase(),
    stroke_color: String(raw.stroke_color ?? defaults.stroke_color ?? "#17130F").toUpperCase(),
    stroke_width: Number(raw.stroke_width ?? defaults.stroke_width ?? 3),
    x: Number(presetNode("position-x").value), y: Number(presetNode("position-y").value),
    max_width: Number(presetNode("overlay-max-width").value), animation: presetNode("overlay-animation").value,
  };
  const chosenId = presetNode("font-family").value;
  if (chosenId.startsWith("__unavailable__:")) throw new Error("目前字型未通過驗證，請重新匯入字型後再儲存。");
  const assetId = chosenId === "__legacy__" ? "" : chosenId;
  let font = null;
  if (assetId) {
    const verified = projectFonts.find((item) => item.asset_id === assetId);
    if (!verified || !verified.sha256 || !verified.family || typeof verified.style !== "string" || verified.weight == null) throw new Error("目前字型缺少已驗證的完整資訊，請重新匯入字型後再儲存。");
    font = Object.fromEntries(["sha256", "family", "style", "weight"].map((key) => [key, verified[key]]));
  }
  return { kind: "caption", settings: { style, font } };
}
function presetReview(title, details, confirmLabel = "確認") {
  const dialog = presetNode("preset-review");
  presetNode("preset-review-title").textContent = title;
  presetNode("preset-review-details").textContent = details;
  presetNode("preset-review-confirm").textContent = confirmLabel;
  dialog.showModal();
  return new Promise((resolve) => {
    const confirm = presetNode("preset-review-confirm"); const cancel = presetNode("preset-review-cancel");
    const cleanup = () => { confirm.removeEventListener("click", yes); cancel.removeEventListener("click", no); dialog.removeEventListener("cancel", no); };
    const yes = () => { cleanup(); resolve(true); };
    const no = (event) => { event?.preventDefault(); cleanup(); dialog.close(); resolve(false); };
    confirm.addEventListener("click", yes); cancel.addEventListener("click", no); dialog.addEventListener("cancel", no);
    cancel.focus();
  });
}
function presetDiff(before, after) {
  const lines = [];
  const labels = { font_size: "字體大小", color: "文字顏色", emphasis_color: "重點顏色", stroke_color: "描邊顏色", stroke_width: "描邊寬度", x: "水平位置", y: "垂直位置", max_width: "最大寬度", animation: "動畫", code: "特效", placement: "擺位", scheme: "字卡色組", opacity: "特效透明度", text_fit: "文字自動調整" };
  const text = (value) => value === undefined || value === null ? "未設定" : typeof value === "boolean" ? (value ? "開啟" : "關閉") : String(value);
  const fontText = (font) => font ? [font.family, font.style, font.weight].filter((x) => x !== undefined && x !== null && x !== "").join(" / ") : "保留目標字型";
  const add = (label, oldValue, newValue) => { if (oldValue !== newValue) lines.push(`${label}：${oldValue} → ${newValue}`); };
  const oldFont = before?.font, newFont = after?.font;
  if (Object.prototype.hasOwnProperty.call(after || {}, "font") && JSON.stringify(oldFont) !== JSON.stringify(newFont)) {
    const oldName = fontText(oldFont), newName = fontText(newFont);
    if (oldName === newName) lines.push(`字型：${newName}（字型檔已更換）`);
    else add("字型", oldName, newName);
  }
  for (const key of new Set([...Object.keys(before?.style || {}), ...Object.keys(after?.style || {})])) {
    add(labels[key] || "字幕設定", text(before?.style?.[key]), text(after?.style?.[key]));
  }
  const effect = fxEffectByCode(after?.code) || fxEffectByCode(before?.code);
  const effectName = (code) => code ? (fxEffectByCode(code)?.name || code) : "未設定";
  if ("code" in (before || {}) || "code" in (after || {})) add("特效", effectName(before?.code), effectName(after?.code));
  const placements = { "": "跟隨效果", top: "上", bottom: "下", left: "左", right: "右" };
  for (const key of ["placement", "scheme", "opacity", "text_fit"]) {
    if (!(key in (before || {})) && !(key in (after || {}))) continue;
    const format = (value) => {
      if (value === undefined || value === null) return "未設定";
      if (key === "placement") return placements[value] ?? text(value);
      if (key === "scheme") return value ? (fxCatalog().schemes?.[value] || text(value)) : "跟隨風格包";
      return text(value);
    };
    add(labels[key], format(before?.[key]), format(after?.[key]));
  }
  for (const key of new Set([...Object.keys(before?.values || {}), ...Object.keys(after?.values || {})])) {
    const label = (effect?.vars || []).find((item) => item.id === key)?.label || "特效文字";
    add(label, text(before?.values?.[key]), text(after?.values?.[key]));
  }
  for (const role of new Set([...Object.keys(before?.font_ids || {}), ...Object.keys(after?.font_ids || {})])) {
    const fontName = (id) => id ? ((fxCatalog().fonts || []).find((font) => font.id === id)?.label || "已選字型") : "跟隨風格包";
    const label = role === "display" ? "標題字型" : role === "body" ? "內文字型" : "特效字型";
    const oldId = before?.font_ids?.[role] || "", newId = after?.font_ids?.[role] || "";
    const oldName = fontName(oldId), newName = fontName(newId);
    if (oldId !== newId && oldName === newName) lines.push(`${label}：字型選擇已變更`);
    else add(label, oldName, newName);
  }
  return lines.join("\n") || "設定沒有差異。";
}
async function presetMutate(method) {
  let selected = presetSelected();
  try {
    if (method !== "POST" && !selected) throw new Error("請先選擇預設。");
    const body = { expected_library_revision: presetLibrary.revision };
    if (method !== "DELETE") {
      const captured = presetCapture();
      body.name = presetNode("preset-name").value; body.kind = captured.kind; body.settings = captured.settings;
      if (selected && method === "PUT") {
        if (selected.kind !== body.kind) throw new Error("更新時圖層種類必須與預設相同。");
        if (!await presetReview(`更新「${selected.name}」`, `名稱：${selected.name} → ${body.name}\n${presetDiff(selected.settings, body.settings)}`, "確認更新")) return;
      }
    } else if (!await presetReview(`刪除「${selected.name}」`, `將刪除預設「${selected.name}」。`, "確認刪除")) return;
    const url = `/api/style-presets${method === "POST" ? "" : `/${encodeURIComponent(selected.id)}`}`;
    const result = await request(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    presetLibraryEpoch += 1;
    presetSetLibrary(result, method === "DELETE" ? "" : selected?.id || ""); presetStatus(method === "DELETE" ? "已刪除預設。" : body.kind === "caption" && !body.settings.font ? "已儲存預設。未包含已驗證字型；套用時保留目標字型。" : "已儲存預設。");
  } catch (error) { presetStatus(`操作失敗：${error.message}${error.status === 409 ? "；請重新開啟預設庫取得最新清單。" : ""}`); }
  finally { if (presetNode("preset-review").open) presetNode("preset-review").close(); }
}
async function presetApply() {
  const preset = presetSelected();
  try {
    if (!preset) throw new Error("請先選擇預設。");
    if (stateDirty || presetSaveInFlight || !lastSaveSucceeded) throw new Error("目前編輯尚未儲存完成，請稍候再試。");
    const overlay = currentOverlay();
    if (preset.kind === "fx") {
      if (preset.availability?.status === "unavailable") throw new Error(preset.availability.message || (preset.availability.reason === "font_unavailable" ? "此預設缺少字型，請更新或刪除。" : "此預設暫時不可用，請更新或刪除。"));
      const before = overlay?.type === "fx" ? { code: overlay.code, values: overlay.values || {}, placement: overlay.placement || "", scheme: overlay.scheme || "", font_ids: overlay.font_ids || {}, opacity: overlay.style?.opacity ?? 1, text_fit: overlay.text_fit !== false } : {};
      if (!fxEffectByCode(preset.settings.code)) throw new Error("目前效果庫沒有這個特效。");
      if (!await presetReview(`試看「${preset.name}」`, `${overlay?.type === "fx" ? "將替換目前特效" : "將在目前位置新增特效"}，確認試看後仍需在試看畫面套用。\n${presetDiff(before, preset.settings)}`, "前往試看")) return;
      presetNode("preset-review").close();
      openFxPreviewDialog(presetNode("preset-apply"), overlay?.type === "fx" ? overlay.id : null, preset.settings.code, preset.settings);
      return;
    }
    if (!overlay || overlay.type !== "caption" || overlay.design_role) throw new Error("請先選取字幕圖層。");
    const generation = editGeneration; const selection = selectedOverlayId; const revision = state.revision;
    const body = { preset_id: preset.id, expected_library_revision: presetLibrary.revision, expected_revision: revision, overlay_id: selection, scope: presetNode("preset-scope").value };
    const drifted = () => stateDirty || presetSaveInFlight || !lastSaveSucceeded || editGeneration !== generation || selectedOverlayId !== selection || state.revision !== revision || presetNode("preset-scope").value !== body.scope;
    const plan = await request("/api/style-presets/caption-plan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (drifted()) throw new Error("編輯或選取已變更，請重新查看差異。");
    const labels = { "style.font_size": "字體大小", "style.color": "文字顏色", "style.emphasis_color": "重點顏色", "style.stroke_color": "描邊顏色", "style.stroke_width": "描邊寬度", "style.x": "水平位置", "style.y": "垂直位置", "style.max_width": "最大寬度", "style.animation": "動畫", font: "字型" };
    const display = (value, field) => field === "font" && value ? [value.family, value.style, value.weight].filter((part) => part !== undefined && part !== null && part !== "").join(" / ") : value == null ? "未設定" : String(value);
    const changes = Array.isArray(plan.changes) ? plan.changes : [];
    const grouped = new Map();
    for (const change of changes) {
      const line = `${labels[change.field] || change.field}：${change.field === "font" && change.before_unavailable ? "目前字型不可用" : display(change.before, change.field)} → ${display(change.after, change.field)}`;
      grouped.set(line, (grouped.get(line) || 0) + 1);
    }
    const allLines = [...grouped].map(([line, count]) => `${line}${count > 1 ? `（${count} 個字幕）` : ""}`);
    const description = allLines.slice(0, 30).join("\n") + (allLines.length > 30 ? `\n另有 ${allLines.length - 30} 種差異；共 ${changes.length} 項變更。` : "") || "設定沒有差異。";
    if (!await presetReview(`套用「${preset.name}」`, `範圍：${({single:"目前字幕",selection:"同精華",track:"整軌"})[body.scope]}\n${preset.settings.font ? "字型將依專案驗證" : "保留目標字幕字型"}\n${description}`, "確認套用")) return;
    if (drifted()) throw new Error("編輯或選取已變更，請重新查看差異。");
    const before = deepCopy(state);
    const result = await request("/api/style-presets/caption-apply", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...body, plan_digest: plan.plan_digest }) });
    if (result.no_change) { presetStatus("設定沒有差異，未新增撤銷步驟。"); return; }
    if (!result.state) throw new Error("伺服器沒有回傳更新後的時間軸。");
    history.push(before); if (history.length > 40) history.shift(); redoStack = []; lastHistoryPushAt = 0;
    state = result.state; if (result.revision) state.revision = result.revision;
    if (result.approval_revisions) projectPayload.approval_revisions = result.approval_revisions;
    for (const gate of result.invalidated_gates || []) {
      if (projectPayload?.manifest?.approvals?.[gate]) projectPayload.manifest.approvals[gate].approved = false;
      if (gate === "timeline") elements["approve-timeline"].textContent = "核可時間軸";
      if (gate === "highlight_selection") elements["approve-highlights"].textContent = "核可精華選段";
      if (gate === "final" && projectPayload.approval_current) projectPayload.approval_current.final = false;
    }
    stateDirty = false; lastSaveSucceeded = true; markStateSynced(); setSaveState("已儲存", "saved"); renderAll(); renderDeliveryQa(); pollCaptionPreviews(); presetStatus("字幕預設已套用，可使用撤銷還原。");
  } catch (error) { presetStatus(`套用失敗：${error.message}`); }
  finally { if (presetNode("preset-review").open) presetNode("preset-review").close(); }
}
function bindStylePresets() {
  presetNode("style-presets").addEventListener("toggle", (event) => { if (event.currentTarget.open) presetLoad(); });
  presetNode("preset-list").addEventListener("change", () => {
    const preset = presetSelected();
    if (preset) {
      presetNode("preset-name").value = preset.name;
      if (preset.availability?.status === "unavailable") presetStatus(preset.availability.message || "此預設暫時不可用，可更新或刪除。");
    }
  });
  presetNode("preset-create").addEventListener("click", () => presetMutate("POST"));
  presetNode("preset-update").addEventListener("click", () => presetMutate("PUT"));
  presetNode("preset-delete").addEventListener("click", () => presetMutate("DELETE"));
  presetNode("preset-apply").addEventListener("click", presetApply);
}

initialize();
