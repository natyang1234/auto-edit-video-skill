# Auto Edit Video 自動剪輯 Skill

這是一個可攜式 [Agent Skill](https://agentskills.io/)。使用者只要交給本機 Agent 一個 A-roll／口播影片檔，Agent 就自行建立本機單字時間碼轉錄與專案，提出保守刪剪、輸出新的 MP4 並執行 QA。另提供選配的 loopback-only Studio，可從本機匯入影片、描述剪輯需求、選擇五種確定性導演策略、產生最多十段語意精華，並逐段改字幕／圖層、預覽、QA、核可與下載。

[English](README.md)

[第一階段 Agent-first 完整規格](skills/auto-edit-video/references/AGENT_FIRST.zh-TW.md)

## 預設用法：只交給 Agent 一個影片檔

預設 Agent-first 路徑不需要操作介面。使用者只要把 Agent 能讀取的本機影片檔交給它：

```text
請用 auto-edit-video 自動剪輯 ./input.mp4
```

Agent 應自行建立工作專案、在本機轉錄、提出並套用保守剪輯、輸出新的 MP4、執行 QA，再回報成品。使用者不必另外準備 Whisper JSON、SRT、manifest，也不必打開預覽網頁。

時長不固定為 30 秒。使用者可用自然語言指定平台與 `短／中／長`、要求 Agent 看完逐字稿後 `自動` 選擇，或直接指定秒數。若沒有精華、縮短或平台需求，預設做保守的全長清理。UI 不是此路徑的必要條件；LINE、雲端上傳與社群發佈仍是必須另外明確授權的外部動作。

## 選配：本機 Studio GUI

使用者明確要求介面時，可啟動內建 Studio：

```bash
SKILL=/auto-edit-video/實際安裝路徑
python3 "$SKILL/scripts/auto_edit.py" studio \
  --projects-root /本機/自動剪輯專案目錄 \
  --port 8765 --open
```

瀏覽器只把選定的本機 File 傳給 loopback Studio。Studio 會驗證影片、建立不可變的 owned copy、執行本機 Whisper，再依平台短／中／長、剪輯重點與五種確定性導演策略產生最多十段有原文證據的精華。使用者可逐段保留／排除、改標題與起訖、校正字幕、加入字卡／動畫／授權素材、預覽並分別輸出。

字幕可直接選取句內字詞，套用可輸出的彈出、螢光或底線效果；字幕與設計圖卡可拖曳，並可調整位置、寬度／高度。GUI 會即時提示平台安全框與同時段圖層重疊。設計模式會把字幕、重點字與圖卡烘焙進同一份 HTML／GSAP graphic package，避免預覽能調、MP4 卻落回另一套字幕 renderer。

中英混合影片可選「中英混合」並用分號填入術語，例如 `It; to V; cigarette`。即使維持自動或中文模式，本機 Whisper 也會收到「英文保留原文、不做中文音譯」提示；提示會限制長度並排除過長例句，避免英文偏置反而降低相鄰中文準確度。另可填入經確認的正字規則，例如 `複數=富數;It is=意思;例句=音譽句`；正字與誤字可不同長度，系統會保留完整來源時間範圍並盡量沿用 word boundaries，必要時可在 manifest 加 `start`／`end` 限定歧義詞的時間範圍。專案會分開儲存機械檢查與語義校準報告；機械警示為 0 不代表語義已審核，套用規則後仍是 `applied_needs_review`，並把過長 Whisper 段落切成 GUI 可讀的定時字幕。

畫面模板與導演策略彼此獨立。編輯器內建 3 種固定鏡位、2 種可選動態鏡位，以及純色／專案圖片／循環影片背景共 3 種本機人物去背模板。固定模板不會輸出任何來源畫面的縮放或重新取景 tween；去背模板可調人物 X／Y／大小，最終時間軸仍使用原片音軌。瀏覽器畫布會明確標示為人物定位預覽，真實去背邊緣以輸出的預覽 MP4 為準。

人工閘依序為 `destructive_edit` → `highlight_selection` → `timeline` → `final`，每一閘都綁定當下 revision。正式輸出會自動建立 frozen snapshot、SHA-256 receipt、機械 QA 與九宮格；只有目前版本通過 QA 且人工核可後才顯示下載。頁面 renderer 目前不會悄悄忽略 interior delete：若審查決定含 `delete`，正式輸出會 fail closed，必須先走 `render_cut.py`，或把該提案改為 keep。

## 平台時長預設

括號內是建議目標；完整規則與官方依據見[第一階段規格](skills/auto-edit-video/references/AGENT_FIRST.zh-TW.md)。

| 平台 | 短 | 中 | 長 |
|---|---:|---:|---:|
| 通用直式／Instagram Reels | 15–30 秒（30） | 45–90 秒（60） | 120–180 秒（180） |
| YouTube Shorts | 15–30 秒（30） | 45–60 秒（60） | 90–180 秒（180） |
| TikTok | 15–30 秒（30） | 45–90 秒（60） | 120–300 秒（180） |
| 小紅書 3:4／9:16 | 15–30 秒（30） | 45–60 秒（60） | 90–180 秒（120） |
| YouTube 橫式 | 60–180 秒（120） | 300–600 秒（480） | 600–1200 秒（900） |

這是編輯建議，不是平台上傳上限。`auto` 會選擇足以保留完整主張的最小級距；原片不足時不補空白、不重複，也不改變播放速度湊秒數。

## 內建核心

- 原始影片保持不可變，`project.json` 是唯一事實來源。
- 偵測低風險停頓、贅字與緊鄰口吃。
- `destructive_edit`、`highlight_selection`、`timeline`、`final` 四道人工作業閘門。
- 依核准決策輸出剪切版，之後強制重新轉錄。
- 繁中、英文、中英雙語或關閉可見字幕。
- 中英混合辨識、專案詞彙表、可稽核同音字校準，以及分離的機械／語義審核狀態。
- loopback Studio 匯入、本機頁面編輯器、語意精華、定時文字／媒體圖層與社群畫布。
- MP4／封面確定性輸出、機械 QA 與 contact sheet。
- 已安裝時才啟用 Edge、Rumi/Fish、HyperFrames、進階字幕與視覺字卡整合。

本 Skill 永不呼叫 CapCut，也不包含 API 金鑰、私有聲音、影片素材、創作者個資或單機路徑設定。

## 支援的 Agent

| Agent／執行環境 | 安裝位置 | 支援方式 |
|---|---|---|
| OpenAI Codex | `$CODEX_HOME/skills` 或 `~/.codex/skills` | 原生 Agent Skill |
| Claude Code | `~/.claude/skills` | 原生自訂 Skill |
| Grok Build | `~/.grok/skills` | 原生 Skill，也相容 Claude／Agent Skills |
| OpenClaw | `~/.openclaw/skills` | 原生 managed/local Skill |
| Hermes Agent | `$HERMES_HOME/skills` 或 `~/.hermes/skills` | 原生檔案型 Skill |
| 其他相容 Agent | `~/.agents/skills` | Agent Skills 通用目錄 |

這個套件適用於能存取本機檔案、Python 與 FFmpeg 的 coding agent。只有網頁聊天、無本機 shell 的介面，不能僅靠安裝 Markdown 就剪輯本機影片。

## 系統需求

- macOS、Linux，或 Windows WSL。
- Python 3.10 以上。
- `ffmpeg`、`ffprobe` 已在 `PATH`。
- 可顯示中文的字型；自動偵測失敗時設定 `AUTO_EDIT_FONT=/字型/絕對路徑`。
- 本機 Whisper 相容轉錄引擎，可產生 word timestamps。Agent 負責從輸入影片建立轉錄，不會要求使用者先準備 JSON，也不會暗中上傳原始音訊。

`edge-tts`、Node.js／HyperFrames 與其他視覺技能都是選配。3 種人物去背模板另需本機 `rembg` CPU 環境與已下載的 `isnet-general-use.onnx`；缺少任一項就會停用，不會下載模型或上傳影格。任何雲端配音都要先取得明確同意。

## 安裝

一次安裝到所有支援的本機 Agent：

```bash
git clone https://github.com/natyang1234/auto-edit-video-skill.git
cd auto-edit-video-skill
./install.sh --agent all
```

只安裝單一目標：

```bash
./install.sh --agent codex
./install.sh --agent claude
./install.sh --agent grok
./install.sh --agent openclaw
./install.sh --agent hermes
./install.sh --agent shared
```

安裝器預設不覆蓋既有版本。`--force` 會先建立時間戳備份再替換；`--dry-run` 只顯示目的地。安裝後請開新 Agent session 或重新啟動 CLI，讓技能清單刷新。

Codex 也可直接使用內建安裝器：

```text
$skill-installer install https://github.com/natyang1234/auto-edit-video-skill/tree/main/skills/auto-edit-video
```

## 驗證

依實際安裝位置執行：

```bash
python3 ~/.agents/skills/auto-edit-video/scripts/auto_edit.py preflight
```

可用的安裝會回傳 `"ready": true`，模式為 `"standalone"` 或 `"extended"`；缺少選配整合不會阻擋內建核心。

## 內部／進階工作流

以下命令供 Agent 或開發者執行；不是要求一般使用者逐項輸入：

```bash
SKILL=/auto-edit-video/實際安裝路徑

python3 "$SKILL/scripts/auto_edit.py" init \
  --input /影片/絕對路徑.mp4 \
  --project-dir /專案/絕對路徑 \
  --platform youtube-shorts \
  --duration-profile long \
  --source-language zh-TW \
  --transcription-calibration "複數=富數;雪茄=學家|雪家;cigar=ciger" \
  --subtitle-mode zh

python3 "$SKILL/scripts/auto_edit.py" import-whisper \
  --manifest /專案/絕對路徑/project.json \
  --whisper-json /轉錄/whisper.json \
  --model large-v3-turbo

python3 "$SKILL/scripts/auto_edit.py" analyze-edits \
  --manifest /專案/絕對路徑/project.json

python3 "$SKILL/scripts/auto_edit.py" plan-highlights \
  --manifest /專案/絕對路徑/project.json \
  --director high-energy --count 10 \
  --brief "優先保留鉤子清楚、資訊完整的片段"

python3 "$SKILL/scripts/auto_edit.py" editor \
  --project-dir /專案/絕對路徑 --port 8765 --open
```

可先用 `duration-presets` 取得所有平台矩陣。若以 `--duration-profile auto` 建立專案，Agent 應在本機轉錄後執行 `set-target` 寫回選定級距；使用者指定秒數時改傳 `--target-duration 75`。

明確核准破壞式刪剪、且逐項審核 edit decisions 後：

```bash
python3 "$SKILL/scripts/render_cut.py" \
  --manifest /專案/絕對路徑/project.json
```

重新轉錄剪切版、完成時間軸並取得 timeline 核准，再輸出 `final.mp4` 與執行：

```bash
python3 "$SKILL/scripts/qa_video.py" \
  --video /專案/絕對路徑/renders/final.mp4
```

通過 QA 與人工畫面檢查後，仍須另取得 final 核准。

## 選配整合探索

Skill 會搜尋同層與各 Agent 的使用者技能目錄。自訂額外根目錄：

```bash
export AUTO_EDIT_SKILLS_ROOTS="/opt/agent-skills:$HOME/my-skills"
```

可個別指定 `VIDEO_AUTOPILOT_SKILL_DIR`、`CUT_NARRATION_SKILL_DIR`、`NARRATION_VIDEO_SKILL_DIR`、`EMBEDDED_CAPTIONS_SKILL_DIR`、`TALKING_HEAD_RECUT_SKILL_DIR`、`HYPERFRAMES_MEDIA_SKILL_DIR` 與 `RUMI_VOICE_SYSTEM`。

## 安全界線

- 原始素材不修改。
- 頁面編輯器預設只綁 loopback；遠端開放需明示。
- Studio 匯入含 CSRF／Host／Origin、防 traversal、大小／格式／container／ffprobe 檢查與原子建案。
- 上傳素材限制在專案內，檢查類型／大小並記錄來源。
- 安裝器不會自行呼叫系統套件管理器。
- 未明確同意前不執行任何雲端 TTS。
- AI 產生的刪剪、字幕、翻譯與視覺配置都只是待審提案。
- render snapshot 會綁定來源、素材、選段、editor revision 與核可；final 另綁輸出、QA 與九宮格 hash。

## 開發驗證

```bash
python3 scripts/validate_skill.py
python3 -m unittest discover -s skills/auto-edit-video/tests -p 'test_*.py'
bash tests/test_install.sh
```

採用 [MIT License](LICENSE)。
