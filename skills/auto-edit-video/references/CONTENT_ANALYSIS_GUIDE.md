# Content Analysis Guide（agent 操作契約，prompt policy `content-analysis-guide-v1`）

你（agent）負責語意判讀；**工具負責證據**。流程與紅線如下，違反任一條 freeze 會直接拒收。

## 流程

1. 先確認 `working/transcript_words.json` 與 `working/video_analysis.json` 存在（deterministic 事實）。
2. 跑 `auto_edit.py build-evidence-index --project-dir <dir>` → 工具從逐字稿產生
   `working/evidence_map.json`（引句＋數字，verbatim、含時間範圍）。**你不得建立、修改
   或補寫任何 evidence 條目**；freeze 會重新推導整份 index，任何手改都會被拒。
3. 讀逐字稿與 evidence map，撰寫 content analysis 草稿（JSON，欄位見
   `contracts/schemas/content_analysis.schema.json`）：
   - `truth_map`：主題／受眾／承諾／方法／證據／故事／CTA，每一項的 `evidence_ids`
     **只能引用 evidence map 既有 ID**；沒有證據支撐的主張不要寫進 proofs。
   - `retention_risks`：只作剪輯信號，不宣稱真實觀看數據。
   - `idea_candidates`：每個 candidate 一個 thesis、完整 source_ranges（秒）、可交付
     payoff、以及支撐它的 evidence_ids。多主題影片拆多個 candidate，不硬拼。
4. 跑 `auto_edit.py freeze-content-analysis --project-dir <dir> --input <draft.json>`。
   通過＝凍結落盤；之後下游（formula router／narrative plan）才可執行。
5. 重跑語意分析＝產生新 revision，走正常 invalidation；不要就地改舊 frozen 檔。

## 紅線（fabrication = 0）

- 禁止捏造原話、數字、成果；引用不到 evidence 的內容一律不得出現在 proofs／stat 類欄位。
- 禁止把「聽起來合理」的推論寫成 claim；不確定就放 retention_risks 或省略。
- `source_ranges` 必須落在影片長度內；freeze 會驗。
- 視覺語意（shot 分類）由你在此步一併判讀寫入 truth_map 相關敘述；
  `video_analysis.shots[].kind` 維持 `unknown`，不要回頭改它。
