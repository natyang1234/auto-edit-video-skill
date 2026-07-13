const studio = {
  bootstrap: null,
  file: null,
  busy: false,
};

const byId = (id) => document.getElementById(id);

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / (1024 ** index)).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

function showError(message) {
  const error = byId("import-error");
  error.textContent = message;
  error.hidden = false;
}

function clearError() {
  byId("import-error").hidden = true;
  byId("import-error").textContent = "";
}

function setProgress(percent, title, note = "請保持這個視窗開啟。") {
  const normalized = Math.max(0, Math.min(100, Math.round(percent)));
  byId("import-progress").hidden = false;
  byId("import-progress-title").textContent = title;
  byId("import-progress-value").textContent = `${normalized}%`;
  byId("import-progress-bar").style.width = `${normalized}%`;
  byId("import-progress-note").textContent = note;
}

function selectFile(file) {
  clearError();
  if (!file) return;
  const suffix = `.${file.name.split(".").pop().toLowerCase()}`;
  if (!studio.bootstrap.source_extensions.includes(suffix)) {
    showError("請選擇 MP4、MOV、M4V 或 WEBM 影片。");
    return;
  }
  if (!file.type || !file.type.startsWith("video/")) {
    showError("瀏覽器無法確認這是影片檔，請換一個檔案。");
    return;
  }
  if (file.size > studio.bootstrap.max_import_bytes) {
    showError(`影片超過目前上限（${formatBytes(studio.bootstrap.max_import_bytes)}）。`);
    return;
  }
  studio.file = file;
  byId("import-file-name").textContent = file.name;
  byId("import-file-size").textContent = `${formatBytes(file.size)} · 原檔保持不變`;
  byId("import-file-summary").hidden = false;
  byId("import-dropzone").hidden = true;
  if (!byId("import-project-name").value.trim()) {
    byId("import-project-name").value = file.name.replace(/\.[^.]+$/, "");
  }
  byId("start-import").disabled = false;
}

async function createImportSession() {
  const file = studio.file;
  const response = await fetch("/api/imports", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Auto-Edit-CSRF": studio.bootstrap.csrf_token,
    },
    body: JSON.stringify({
      project_name: byId("import-project-name").value.trim() || file.name.replace(/\.[^.]+$/, ""),
      file: {
        name: file.name,
        size_bytes: file.size,
        last_modified_ms: file.lastModified,
        type: file.type,
      },
      settings: {
        source_language: byId("import-language").value,
        subtitle_mode: "source",
        platform: byId("import-platform").value,
        duration_profile: byId("import-duration").value,
        edit_preset: byId("import-preset").value,
      },
    }),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `無法建立匯入工作（${response.status}）`);
  return payload.import;
}

function uploadFile(session) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", session.upload_url);
    xhr.setRequestHeader("Content-Type", studio.file.type);
    xhr.setRequestHeader("X-Auto-Edit-CSRF", studio.bootstrap.csrf_token);
    xhr.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) return;
      const percent = (event.loaded / event.total) * 88;
      setProgress(percent, "正在複製影片…", `${formatBytes(event.loaded)} / ${formatBytes(event.total)}`);
    });
    xhr.addEventListener("load", () => {
      let payload = {};
      try { payload = JSON.parse(xhr.responseText); } catch (_error) { /* stable fallback below */ }
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(new Error(payload.error || `影片匯入失敗（${xhr.status}）`));
        return;
      }
      resolve(payload);
    });
    xhr.addEventListener("error", () => reject(new Error("無法連接本機 Studio，請重試。")));
    xhr.addEventListener("abort", () => reject(new Error("影片匯入已取消。")));
    xhr.send(studio.file);
  });
}

async function startImport() {
  if (!studio.file || studio.busy) return;
  clearError();
  studio.busy = true;
  byId("start-import").disabled = true;
  try {
    setProgress(1, "正在建立安全匯入工作…");
    const session = await createImportSession();
    const payload = await uploadFile(session);
    setProgress(100, "專案已建立", "即將開啟剪輯工作區…");
    window.location.assign(payload.editor_url);
  } catch (error) {
    showError(error.message);
    setProgress(0, "匯入未完成", "修正後可重新建立專案。");
    studio.busy = false;
    byId("start-import").disabled = false;
  }
}

function bindEvents() {
  const input = byId("source-video-input");
  const dropzone = byId("import-dropzone");
  const choose = () => input.click();
  byId("choose-video").addEventListener("click", (event) => {
    event.stopPropagation();
    choose();
  });
  byId("replace-video").addEventListener("click", choose);
  dropzone.addEventListener("click", choose);
  dropzone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      choose();
    }
  });
  input.addEventListener("change", () => selectFile(input.files[0]));
  ["dragenter", "dragover"].forEach((name) => dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.add("is-dragging");
  }));
  ["dragleave", "drop"].forEach((name) => dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.remove("is-dragging");
  }));
  dropzone.addEventListener("drop", (event) => selectFile(event.dataTransfer.files[0]));
  byId("start-import").addEventListener("click", startImport);
}

async function initialize() {
  bindEvents();
  try {
    const response = await fetch("/api/studio");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Studio 尚未就緒");
    studio.bootstrap = payload;
    byId("dropzone-note").textContent = `MP4、MOV、M4V、WEBM · 上限 ${formatBytes(payload.max_import_bytes)}`;
  } catch (error) {
    showError(`Studio 啟動失敗：${error.message}`);
    byId("choose-video").disabled = true;
  }
}

initialize();
