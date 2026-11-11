# 剪輯工作流程（八階段）

主入口是 `auto-edit-video`。依使用者的片種、目標和素材選擇需要的能力；各階段可回到前段修正。純 cut-only、低風險口播清理與轉碼沿用 auto-edit-video 現有授權，不增加創作審查點。

| 階段 | 目的 | 主 skill | 按需副 skill／參考 | 輸入 → 產出 | 既有審查點 |
|---|---|---|---|---|---|
| 1. 接件與片種目標 | 確認素材可讀、內容類型、平台／畫幅／長度與交付目標；先盤點是否缺素材。 | `auto-edit-video` | `video-edit`（移除背景、穩定、升頻等專題查詢）；`video-shotcraft`（產品／網頁／AI 分鏡是常見情境，也可按需抽取單鏡頭） | 本機影片與自然語言需求 → 專案範圍、技術 preflight、初始缺口紀錄 | 只詢問會實質改變成品且需求未暗示的選擇。影片不可讀或必要依賴缺失時依原規則回報。候選素材可先找，不等於選片或入片。 |
| 2. 轉錄與敘事粗剪 | 讀懂內容，建立轉錄與粗剪；保留意思、數字、人名、否定與條件。 | `auto-edit-video` | `story-rhythm-sound` 的 narrative／pacing 配方（訪談骨架或節奏問題）；`ffmpeg-skill`（明確機械剪接／轉碼） | 原片、目標與轉錄 → transcript、粗剪決策、cut map | 使用者明確提出自動剪輯請求時，該請求涵蓋低風險停頓／贅字／緊鄰口吃清理。高風險語意剪除預設保留；不因純 cut-only 或轉碼新增創作 gate。 |
| 3. 風格與分鏡 | 只有在需要視覺方向或敘事設計時，決定風格、鏡頭順序、字卡／效果意圖。 | `edit-style-profiles` | `story-rhythm-sound`（只讀適用配方）；`edit-style-profiles/references/filmcraft-foundations/`（鏡頭意圖、構圖、剪點、調色或明確電影感聲音）；`video-shotcraft`（產品／網頁／AI 分鏡常見情境，或按需使用單鏡頭卡） | 粗剪或 brief → storyboard、風格選擇、決策紀錄 | 要加特效或設計字卡時，沿用分鏡與開場 A/B/C 先審；純剪接不加特效可跳過。`story-rhythm-sound`、filmcraft 是決策參考，不是 renderer。不要把 `video-shotcraft` 的完整創作流程無條件套到一般口播。 |
| 4. 素材缺口與選片 | 依工作所需挑選或補充素材並追蹤來源；修改時可回到任一階段。 | `edit-style-profiles`（共用素材缺口流程 SSOT） | `find-broll`（口播 B-roll 候選與對位）；`suggest-sfx`（音效素材）；`video-shotcraft`（產品／網頁／AI 分鏡素材，或按需單鏡頭） | 任一階段發現的缺口 → `working/asset-gaps.md`、來源與授權紀錄、素材選擇／落地狀態 | 缺口盤點與客觀候選搜尋可在 intake、分鏡、改稿、QA 發生，無需重問搜尋許可；不得將候選當偏好已確認。沿用原分鏡核可、最終選片及素材匯入審查。 |
| 5. 畫面、字幕與特效 | 依核准的分鏡和風格完成構圖、字幕、字卡、圖層與特效。 | `auto-edit-video`（執行）＋ `edit-style-profiles`（效果／風格） | `skill-captions`、`ra-audio-to-subtitles`（正式字幕時間軸）；`palette-catalog`（配色）；`ffmpeg-skill`（指定參數的機械處理）；口播視覺 QA 參考（被要求時） | 已選素材、分鏡、轉錄 → 畫面方案、字幕／效果圖層與預覽 | 保留分鏡、先審 cue、素材匯入與時間線審查；只按請求啟用口播視覺 QA。機械工具不替人判斷美學。 |
| 6. 聲音 | 整理人聲、按需選配樂與音效；把聲音方向轉成可檢視的計畫。 | `auto-edit-video` | `suggest-sfx`（SFX cue、來源與混音）；`story-rhythm-sound`（明確要求紀錄片聲音）；`filmcraft-foundations/cinematic-sound.md`（明確要求電影感聲音）；BGM 選型按需讀共用 `video-shotcraft/references/sound-design.md`，已選 BGM 且需卡點時讀 `music-beat-sync.md` | 畫面節點、音軌與聲音目標 → audio plan、`sfx-plan.json`、混音與量測紀錄 | `suggest-sfx` cue 表先審後混，遵守 cue 密度、授權與人聲優先；BGM 選曲與卡點方法按需使用，不代表套用整套產品片流程。交付不把量測當真人試聽。素材缺口仍走同一 SSOT。 |
| 7. 渲染 | 依核准計畫輸出新檔，保留原片與可追溯的專案決策。 | `auto-edit-video` | `ffmpeg-skill`（明確、確定性的機械任務）；`yichen-jianying-edit-local`（使用者明確指定剪映原生草稿路線時） | 時間線、字幕、聲音、輸出規格 → 新版渲染檔與技術報告 | Studio 僅在使用者要求介面時走既有 `destructive_edit → highlight_selection → timeline → final` 閘點。剪映必須先跑 doctor，且匹配特定 App、runtime 與 toolchain；CapCut 不等於剪映。 |
| 8. QA、交付與修正寫回 | 檢查檔案、畫面、聲音與交付條件；修正後回到受影響階段。 | `auto-edit-video` | `ffmpeg-skill` 的 probe/check/look（客觀檢查）；`edit-style-profiles` 的 correction log（使用者明確糾正風格時） | 渲染檔與 QA 結果 → 交付檔、限制／未驗證項、修正決策 | 依原流程做機械 QA 和必要視覺檢查；不得把可讀技能文件或離線產物說成 runtime、真人聽看或平台驗收。QA 發現缺口時回到素材缺口流程。 |

## 階段 5 可選工具路由

- 主口播剪輯可沿用 `auto-edit-video` 內建字幕。正式字幕樣式交給 [skill-captions](~/.claude/skills/skill-captions/SKILL.md)：它只處理外觀與燒錄，必須先有最終音訊的真實時間碼及 `caption-qc.json` 通過；沒有最終時間碼時先走 [ra-audio-to-subtitles](~/.claude/skills/ra-audio-to-subtitles/SKILL.md)。不得按字數、場景長度或 TTS 片段長度猜時間。
- 字幕樣式／關鍵詞色：依需求使用 [skill-captions](~/.claude/skills/skill-captions/SKILL.md)；主題色可查 [palette-catalog](~/.claude/skills/palette-catalog/SKILL.md)。Auto Edit 主題包由 `auto-edit-video/scripts/theme_packs.py` 建立。
- 特效元件：在 `edit-style-profiles/hyperframes-blocks/` 選現有 HyperFrames block；Auto Edit 的特效 overlay 由 `auto-edit-video/scripts/fx_overlay.py` 渲染。HyperFrames 是底層 renderer／block 路由，不另列為 skill。
- 只有分鏡選到需人物去背的效果（ⓜ）時，才用 `edit-style-profiles/tools/matte_video.py`；依其 `tools/README-matte.md` 使用本機流程。
- 審查點仍依上表：需特效／設計字卡先審分鏡；不因可選工具而新增批准或把單純字幕／剪切變成創作流程。

## 跨階段：素材缺口唯一流程

素材類別為 `footage`、`still`、`sfx`、`bgm`、`template`、`lut`。在接件、分鏡／計畫建立、改稿及 QA 時都檢查；沒有缺口或純文字刪修，在專案 `working/asset-gaps.md` 記 `No gaps.`。唯一 SSOT 是 [asset-gap-sourcing.md](~/.claude/skills/edit-style-profiles/references/asset-gap-sourcing.md)。

狀態只按證據前進：`discovered` → `searched` → `candidate` → `downloaded` → `placed` → `verified`；受阻記 `blocked` 和原因。候選頁不等於下載；下載不等於已放入時間線；放入也不等於已核實內容、用途與授權。未知授權寫「待查」，不能標 `verified`。搜尋可先於分鏡核可，依原流程等待最終選片、素材匯入或 cue 審查；不新增批准，也不以搜尋許可略過原有審查。

## 三種介面入口各自用途

- **風格選單**：`edit-style-profiles/menu/index.html`；用來挑效果、主題顏色與色組。來源：[edit-style-profiles/SKILL.md](~/.claude/skills/edit-style-profiles/SKILL.md)。
- **menu_editor**：`edit-style-profiles/tools/menu_editor.py`；編輯真實特效片段、參數與時間位置。來源同上。它和風格選單是不同工具。
- **Auto Edit Studio**：只在使用者要求 GUI 時使用；負責匯入、剪輯控制與時間線流程。來源：[PAGE_EDITOR.md](~/.claude/skills/auto-edit-video/references/PAGE_EDITOR.md)。它與上述兩項是不同入口；本流程文件不表示面板已整合。

## 技能狀態與限制

本清單代表文件路由可用性，不代表當前主機已安裝完整 runtime、依賴或完成實機驗收。`yichen-jianying-edit-local` 是本機 fork，現況為 doctor／離線規劃可用、native runtime pending；doctor、App 身分／版本／hash、runtime 與 toolchain gate 全部通過前不得原生操作。本機共享包 `yichen-jianying-edit` 是不同入口，不代表本機 fork runtime 已通過；CapCut 不能取代特定剪映 App。`ffmpeg-skill` 做明確機械工作，不替代內容、構圖或美學判斷。`video-edit` 是背景移除、調色、升頻等專題概述與查詢入口，不替代主剪輯流程。
