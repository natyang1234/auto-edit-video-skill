# Analysis Engine Contract（PRD §7.4.1.1 具體化）

## 欄位分類與引擎
| 類別 | 欄位 | 引擎 | determinism |
|---|---|---|---|
| deterministic | duration/resolution/fps/loudness | ffprobe（既有） | 相同輸入同輸出，version pin |
| deterministic | silences/shots | ffmpeg filters（既有路徑） | 同上 |
| deterministic | word-timed transcript | 本機 openai-whisper（既有） | version+model pin |
| deterministic | ocr_spans | **optional capability**：`engines.ocr.status=not_configured|present` | present 時 version pin |
| 語意 | truth_map/retention_risks/idea_candidates/視覺語意標籤 | primary＝**agent-managed session**；optional＝loopback Ollama | 不要求跨次 bit-identical |

## OCR optional capability（Codex 預審 B2 處置）
Phase 0 只固定資料形狀：`status=not_configured` 時 `ocr_spans` 必須為空陣列、不得由其他
來源冒充；依賴 OCR 的下游（burned-caption 偵測）標 `unavailable`。正式選型
（tesseract／macOS Vision）＝Phase 1a 開工決策，選定後補 version pin。

## Frozen artifact 與 determinism 邊界
語意欄位寫入 `content_analysis.json`（schema 強制 `frozen:true`＋engine id/model/
prompt_policy_version/generated_at/revision hash）後**凍結**。determinism 邊界＝frozen
artifact 之後：Formula Router 等下游在相同 artifact revision＋policy version 下必須同 hash
（PRD §9.2）。重跑語意分析＝新 revision→下游 stale→正常 invalidation，不覆寫舊 revision。

## Ollama 候選的量級帳（依 governance 20-JUDGMENT §2）
qwen2.5:7b context 32K 不足吃長片全逐字稿→必須分段＋map-reduce；本機 16GB 有 Jetsam
前科→單 session 併發上限 1、`OLLAMA_KEEP_ALIVE` 沿用 2m、每片請求數上限須在實作時
實測並記錄。未附量級帳不得把 Ollama 設為 default。

## 離線能力矩陣
| 情境 | 行為 |
|---|---|
| 離線＋無語意引擎＋無 frozen artifact | pipeline 停在 `content_analyzed` 前，UI/CLI 明示原因 |
| 離線＋有 frozen artifact | 下游（router/plan/rough cut/render）照常 |
| 離線＋deterministic 欄位 | 照常產生 |
| 禁止 | 規則式 stub 冒充語意分析結果 |
