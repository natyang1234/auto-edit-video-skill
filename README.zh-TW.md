# Auto Edit Video 自動剪輯 Skill

這是一個可攜式 [Agent Skill](https://agentskills.io/)，把本機影片與含單字時間碼的 Whisper JSON 轉成可審核、非破壞式的自動剪輯專案。它能提出刪剪建議、建立字幕與重點字、加入標題／字卡／動畫、切換社群尺寸，並用 FFmpeg 確定性輸出與執行交付 QA。

[English](README.md)

## 內建核心

- 原始影片保持不可變，`project.json` 是唯一事實來源。
- 偵測低風險停頓、贅字與緊鄰口吃。
- `destructive_edit`、`timeline`、`final` 三道人工作業閘門。
- 依核准決策輸出剪切版，之後強制重新轉錄。
- 繁中、英文、中英雙語或關閉可見字幕。
- 本機頁面編輯器、定時文字／媒體圖層與社群畫布。
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
- 本機產生、含 word timestamps 的 Whisper JSON。本 Skill 接受轉錄結果，不會暗中上傳原始音訊。

`edge-tts`、Node.js／HyperFrames 與其他視覺技能都是選配；任何雲端配音都要先取得明確同意。

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

## 最小工作流

```bash
SKILL=/auto-edit-video/實際安裝路徑

python3 "$SKILL/scripts/auto_edit.py" init \
  --input /影片/絕對路徑.mp4 \
  --project-dir /專案/絕對路徑 \
  --source-language zh-TW \
  --subtitle-mode zh

python3 "$SKILL/scripts/auto_edit.py" import-whisper \
  --manifest /專案/絕對路徑/project.json \
  --whisper-json /轉錄/whisper.json \
  --model large-v3-turbo

python3 "$SKILL/scripts/auto_edit.py" analyze-edits \
  --manifest /專案/絕對路徑/project.json

python3 "$SKILL/scripts/auto_edit.py" editor \
  --project-dir /專案/絕對路徑 --port 8765 --open
```

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
- 上傳素材限制在專案內，檢查類型／大小並記錄來源。
- 安裝器不會自行呼叫系統套件管理器。
- 未明確同意前不執行任何雲端 TTS。
- AI 產生的刪剪、字幕、翻譯與視覺配置都只是待審提案。

## 開發驗證

```bash
python3 scripts/validate_skill.py
python3 -m unittest discover -s skills/auto-edit-video/tests -p 'test_*.py'
bash tests/test_install.sh
```

採用 [MIT License](LICENSE)。
