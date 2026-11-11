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

function populateDirectorOptions(presets) {
  const select = byId("import-director");
  select.replaceChildren();
  const entries = Object.entries(presets || {});
  entries.forEach(([profileId, preset]) => {
    const option = document.createElement("option");
    const unavailable = preset && preset.available === false;
    const missing = Array.isArray(preset && preset.missing_capabilities)
      ? preset.missing_capabilities.filter(Boolean).sort()
      : [];
    const reason = missing.length
      ? `尚未就緒：缺少 ${missing.join(", ")}`
      : "尚未就緒";
    option.value = profileId;
    option.disabled = unavailable;
    option.textContent = unavailable
      ? `${(preset && preset.label) || profileId}（${reason}）`
      : ((preset && preset.label) || profileId);
    option.title = unavailable
      ? reason
      : ((preset && (preset.description || preset.label)) || profileId);
    select.append(option);
  });
  const teacher = entries.find(([profileId, preset]) => (
    profileId === "teacher-punch" && (!preset || preset.available !== false)
  ));
  const fallback = entries.find(([, preset]) => !preset || preset.available !== false);
  select.value = (teacher || fallback || [""])[0];
  select.disabled = !select.value;
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
  byId("studio-start-hint").textContent = "影片已選擇，可開始處理";
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
        transcription_glossary: byId("import-transcription-glossary").value.trim(),
        transcription_calibration: byId("import-transcription-calibration").value.trim(),
        contextual_semantic_calibration: byId("import-contextual-semantic").checked,
        subtitle_mode: "source",
        platform: byId("import-platform").value,
        duration_profile: byId("import-duration").value,
        edit_preset: byId("import-preset").value,
        director_profile: byId("import-director").value,
        editing_brief: byId("import-brief").value.trim(),
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
    populateDirectorOptions(payload.director_presets);
    byId("dropzone-note").textContent = `MP4、MOV、M4V、WEBM · 上限 ${formatBytes(payload.max_import_bytes)}`;
  } catch (error) {
    showError(`Studio 啟動失敗：${error.message}`);
    byId("choose-video").disabled = true;
  }
}

initialize();

let folderFiles = [];

function bindFolderEvents() {
  const input = byId("folder-input");
  const chooseButton = byId("choose-folder");
  const startButton = byId("start-folder-import");
  if (!input || !chooseButton || !startButton) return;
  chooseButton.addEventListener("click", () => input.click());
  input.addEventListener("change", () => {
    folderFiles = Array.from(input.files || []).filter(
      (file) => file.webkitRelativePath && !file.name.startsWith(".")
    );
    const summary = byId("folder-summary");
    if (folderFiles.length) {
      const root = folderFiles[0].webkitRelativePath.split("/")[0];
      byId("folder-name").textContent = root;
      byId("folder-count").textContent =
        `${folderFiles.length} 個檔案｜` +
        formatBytes(folderFiles.reduce((sum, file) => sum + file.size, 0));
      summary.hidden = false;
      startButton.disabled = false;
    } else {
      summary.hidden = true;
      startButton.disabled = true;
    }
  });
  startButton.addEventListener("click", () => {
    startFolderImport().catch((error) => {
      showError(error.message || String(error));
      setProgress(0, "資料夾匯入失敗", "修正後可重試。");
    });
  });
}

function folderRelativePath(file) {
  return file.webkitRelativePath.split("/").slice(1).join("/") || file.name;
}

async function startFolderImport() {
  clearError();
  if (!folderFiles.length) throw new Error("請先選擇資料夾。");
  const root = folderFiles[0].webkitRelativePath.split("/")[0];
  setProgress(2, "建立資料夾匯入工作階段…");
  const createResponse = await fetch("/api/folder-imports", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Auto-Edit-CSRF": studio.bootstrap.csrf_token,
    },
    body: JSON.stringify({
      root_display_name: root,
      project_name: root,
      settings: { source_language: "auto" },
      files: folderFiles.map((file) => ({
        path: folderRelativePath(file),
        size_bytes: file.size,
      })),
    }),
  });
  const created = await createResponse.json();
  if (!createResponse.ok) {
    throw new Error(created.message || created.error || "無法建立資料夾匯入。");
  }
  const session = created.session;
  for (let index = 0; index < folderFiles.length; index += 1) {
    const file = folderFiles[index];
    const path = folderRelativePath(file);
    setProgress(
      2 + Math.round((index / folderFiles.length) * 88),
      `上傳中（${index + 1}/${folderFiles.length}）`,
      path
    );
    const uploadResponse = await fetch(
      session.upload_url_template + encodeURIComponent(path),
      {
        method: "PUT",
        headers: {
          "Content-Type": "application/octet-stream",
          "X-Auto-Edit-CSRF": studio.bootstrap.csrf_token,
        },
        body: file,
      }
    );
    if (!uploadResponse.ok) {
      const failure = await uploadResponse.json().catch(() => ({}));
      throw new Error(failure.message || `檔案上傳失敗：${path}`);
    }
  }
  setProgress(92, "建立專案與清單…");
  const finalizeResponse = await fetch(session.finalize_url, {
    method: "POST",
    headers: { "X-Auto-Edit-CSRF": studio.bootstrap.csrf_token },
  });
  const finalized = await finalizeResponse.json();
  if (!finalizeResponse.ok) {
    throw new Error(finalized.message || finalized.error || "資料夾匯入失敗。");
  }
  setProgress(100, "完成，正在開啟剪輯室…");
  window.location.href = finalized.editor_url;
}

bindFolderEvents();

// User-facing guide snapshots from EDITING_WORKFLOW, the style menu presets,
// asset-gap-sourcing and filmcraft-foundations (2026-10-29). These describe
// choices and review steps; they are not backend execution states.
function initializeStudioGuide() {
  const views = Array.from(document.querySelectorAll('[data-studio-view]'));
  const navigation = Array.from(document.querySelectorAll('[data-studio-nav]'));
  const toggle = byId('studio-nav-toggle');
  function closeNavigation(returnFocus = false) {
    document.body.classList.remove('studio-nav-open');
    toggle.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-label', '開啟導覽');
    if (returnFocus) toggle.focus();
  }
  function showView() {
    const requested = window.location.hash.slice(1) || 'start';
    const current = views.some((view) => view.dataset.studioView === requested) ? requested : 'start';
    views.forEach((view) => { view.hidden = view.dataset.studioView !== current; });
    navigation.forEach((link) => {
      if (link.dataset.studioNav === current) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    closeNavigation();
  }
  toggle.addEventListener('click', () => {
    const open = document.body.classList.toggle('studio-nav-open');
    toggle.setAttribute('aria-expanded', String(open));
    toggle.setAttribute('aria-label', open ? '關閉導覽' : '開啟導覽');
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && document.body.classList.contains('studio-nav-open')) closeNavigation(true);
  });
  navigation.forEach((link) => link.addEventListener('click', () => {
    closeNavigation();
    // Focus the view after the browser has processed the hash navigation.
    requestAnimationFrame(() => byId('studio-main').focus({ preventScroll: true }));
  }));
  window.addEventListener('hashchange', showView);
  showView();

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function addBriefButton(card, request) {
    const button = element('button', '加入剪輯重點');
    button.type = 'button';
    button.addEventListener('click', () => {
      const brief = byId('import-brief');
      const next = [brief.value.trim(), request].filter(Boolean).join('\n');
      if (next.length > brief.maxLength) {
        byId('studio-action-status').textContent = '剪輯重點已接近字數上限，請先整理內容再加入。';
        window.location.hash = 'start';
        showView();
        showError('剪輯重點已接近字數上限，請先整理內容再加入。');
        brief.focus();
        return;
      }
      brief.value = next;
      brief.dispatchEvent(new Event('input', { bubbles: true }));
      byId('studio-action-status').textContent = '已加入剪輯重點，這是需求描述；尚未套用效果。';
      window.location.hash = 'start';
      showView();
      brief.focus();
    });
    card.append(button);
  }
  function link(parent, title, url) {
    const anchor = element('a', `${title} ↗`);
    anchor.href = url;
    anchor.target = '_blank';
    anchor.rel = 'noopener noreferrer';
    parent.append(anchor);
  }
  const workflow = [
    ['1. 接件', '確認片種、平台、長度與交付目標，盤點本機素材與缺口。', '先選影片並填寫專案設定；需要的內容直接寫進剪輯重點。'],
    ['2. 粗剪', '建立轉錄與粗剪，保留原意、數字、人名、否定與條件。', '低風險停頓與贅詞可依請求清理；可能改變原意的段落保留待確認。'],
    ['3. 分鏡', '需要視覺設計時，決定鏡頭順序、字卡與效果方向。', '要加特效或設計字卡時，先確認分鏡與開場；純剪接可跳過創作設計。'],
    ['4. 補素材', '先查本機，再按空鏡、圖片、音效、音樂、模板、調色、3D 模型、底片顆粒、字型或視覺處理需求找候選。', '搜尋候選與最終選片分開；記錄來源與授權，改稿或驗收也可回來補料。'],
    ['5. 畫面', '按確認方向安排構圖、字幕、字卡、圖層與效果。', '正式字幕依最終音訊的真實時間碼校對；效果與素材仍需預覽確認。'],
    ['6. 聲音', '優先保留清楚人聲，再按需要安排配樂與音效。', '先確認音效位置與密度，再混音；音量量測不能取代真人試聽。'],
    ['7. 輸出', '依已確認的時間線與交付規格產生新檔，保留原片。', '建立專案後，在剪輯工作區依序確認粗剪、精華提案、時間線及最終輸出。'],
    ['8. 驗收', '核對檔案、畫面、聲音和平台交付條件，記錄需要修正的地方。', '技術檢查、真人聽看與平台驗收分別確認；發現缺口就回到對應步驟。'],
  ];
  workflow.forEach(([title, goal, note]) => {
    const row = element('article', '', 'studio-workflow-row');
    const content = element('div');
    row.append(element('h2', title));
    content.append(element('p', goal), element('small', note));
    row.append(content);
    byId('studio-workflow-content').append(row);
  });
  const styles = [
    ['教學圖解', '知識型、講觀念、拆方法。以清楚的步驟與資訊圖解支撐說明。', '風格方向：教學圖解，用步驟與資訊圖解說清楚觀念；先確認分鏡和字卡內容。'],
    ['快速秀特效', '作品展示或工具介紹。短段落展示不同效果，避免遮住真正的內容。', '風格方向：快速秀特效，以短段落展示作品或工具，先確認開場與效果分鏡。'],
    ['中文雜誌風', '中文口播或個人品牌。用標題層級與少量字卡建立閱讀節奏。', '風格方向：中文雜誌風，標題層級清楚、字卡節制，字幕與畫面配色一致。'],
    ['接案說服', '成果、案例或提案。從問題、方法到結果，讓證據有清楚的位置。', '風格方向：接案說服，清楚呈現問題、方法與成果，使用可核實的案例畫面。'],
    ['前後對比', '改造、AI 或文案改寫。使用一致的比較條件，讓差異容易看懂。', '風格方向：前後對比，對照畫面標示清楚，維持可比較的條件。'],
    ['極簡口播', '日常口播。保留自然敘事，只在需要時加少量字卡。', '風格方向：極簡口播，保留自然敘事，只加少量必要字卡，避免特效干擾。'],
  ];
  styles.forEach(([title, description, request]) => {
    const card = element('article', '', 'studio-guide-card');
    card.append(element('h2', title), element('p', description));
    addBriefButton(card, request);
    byId('studio-style-content').append(card);
  });
  const sources = [
    ['素材影片', '補空鏡、情境與過場。涉及真實人物、店面或事件時，優先使用可核實的原始素材。', [['Pexels Videos','https://www.pexels.com/videos/'],['Mixkit 影片','https://mixkit.co/free-stock-video/'],['Pixabay 影片','https://pixabay.com/videos/'],['Coverr','https://coverr.co/']]],
    ['圖片與紀實素材', '先查本機圖片。公開館藏需逐項核對作者、內容與使用條款。', [['Wikimedia Commons','https://commons.wikimedia.org/'],['NASA 圖片與影片','https://images.nasa.gov/'],['Internet Archive','https://archive.org/']]],
    ['音效', '找環境聲、轉場聲或動作聲。按人聲節奏規劃位置；部分來源需帳號或付費授權。', [['Mixkit 音效','https://mixkit.co/free-sound-effects/'],['Freesound','https://freesound.org/'],['Soundsnap','https://www.soundsnap.com/'],['Epidemic Sound 音效','https://www.epidemicsound.com/sound-effects/']]],
    ['背景音樂', '按情緒、密度與敘事位置選曲；使用前確認曲目、帳號與發布用途授權。', [['Mixkit 音樂','https://mixkit.co/free-stock-music/'],['Bensound','https://www.bensound.com/'],['Epidemic Sound','https://www.epidemicsound.com/']]],
    ['影片模板', '先看既有效果能否滿足需求。外部模板需確認剪輯軟體、版本及使用授權。', [['Mixkit 模板','https://mixkit.co/free-video-templates/'],['Envato Elements','https://elements.envato.com/'],['AEJuice','https://aejuice.com/'],['Videobolt','https://videobolt.net/']]],
    ['調色與 LUT', '先處理曝光、白平衡與鏡頭匹配，再考慮 LUT。收藏清單中的未核實網站不列為可用素材。', [['Envato Elements 調色素材','https://elements.envato.com/']]],
    ['3D 模型', '確有模型缺口時，逐項確認作者、授權、格式、材質與相容性。', [['Sketchfab','https://sketchfab.com/']]],
    ['底片顆粒與疊加', '本機效果優先；真實底片掃描素材需確認用途與授權，並檢查字幕和品牌可讀性。', [['FilmLooks 底片疊加','https://shop.filmlooks.com/p/film-grain-overlays-effects-8mm-16mm-35mm/']]],
    ['復古視覺處理', '先用本機 VHS、ASCII 或 dithering 效果；外部工具試樣前確認隱私、輸出與使用條件。', [['Grainrad','https://www.grainrad.com/']]],
    ['字型與配對', '先查本機字型；外部英文字型需逐款確認授權、字重和字形覆蓋。', [['Fontshare','https://www.fontshare.com/']]],
  ];
  sources.forEach(([title, description, links]) => {
    const card = element('article', '', 'studio-guide-card');
    card.append(element('h2', title), element('p', description));
    links.forEach(([name, url]) => link(card, name, url));
    byId('studio-source-content').append(card);
  });
  const recipes = [
    ['剪點與接法', '剪點缺乏目的，或要使用 J/L、動作接法與交錯剪接時。', 'Shutterstock Tutorials', 'Wv3Hmf2Dxlo', '剪點需求：先說明每個剪點要推進什麼，再按需要規劃 J/L 或動作接法。'],
    ['鏡頭選擇', '需要補拍或挑選景別、視點與角度時。', 'Alex Zarfati', 'NJks7JfGKxM', '鏡頭需求：依敘事目的挑景別、角度與視點，缺少的鏡頭先列入素材缺口。'],
    ['構圖與焦點', '需要調整主體、視線、層次或裁切比例時。', 'Sightseeing Stan', 'ANdLOY4rW04', '構圖需求：主體焦點清楚，檢查視線、層次與平台裁切，避免字幕擋住關鍵畫面。'],
    ['色彩方向', '需要規劃情緒配色、膚色或飽和度時。', 'River Cody', 'Q7S46o8vUOQ', '色彩需求：先校正曝光、白平衡與膚色，再確認情緒配色與飽和度。'],
    ['橫移與速度變化', '已有適合的橫移畫面，想規劃速度變化與落點時。', 'Gaku Lange', 'kmnYxvGxLVE', '轉場需求：檢查橫移方向、素材幀率與銜接落點，再決定是否使用速度變化。'],
    ['逐鏡意圖', '需要說清運鏡、音樂或慢動作為何存在時。', 'Jacques Crafford', 'sqW7HCYKM2E', '分鏡需求：逐鏡說明運鏡、音樂與慢動作的敘事目的，沒有用途就簡化。'],
    ['電影感聲音', '明確需要電影感敘事聲音時，規劃環境、層次、留白與空間。', 'Jacob Nordin', 'I66_LmOTlSk', '聲音方向：電影感敘事聲音，規劃環境聲、層次與留白；先確認音效位置與密度，再混音試聽。'],
  ];
  recipes.forEach(([title, description, author, video, request]) => {
    const card = element('article', '', 'studio-guide-card');
    card.append(element('h2', title), element('p', description));
    link(card, `${author}・原始教學`, `https://www.youtube.com/watch?v=${video}`);
    addBriefButton(card, request);
    byId('studio-recipe-content').append(card);
  });
}

initializeStudioGuide();
