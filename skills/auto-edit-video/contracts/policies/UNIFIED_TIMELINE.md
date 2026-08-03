# Unified Timeline 與 Renderer Contract（PRD §7.11.1 具體化）

## 現況（2026-08-04 審查核實）
三套互斥 timeline 模型（`edit_decisions.json`／`highlight_plan.json`／`editor_state.json`）＋
兩個互不相容 renderer：`render_cut.py` 只 trim/concat；`render_editor_timeline.py` 只單源
overlays 且遇已核可 delete 拒繪 final。`source_cut.mp4` timeline renderer 永不消費。

## 契約
1. **Master timeline＝ordered source segments**（schema：`master_timeline.schema.json`；
   editor_state v2 亦內含 `segments`）。narrative reorder、破壞性刪除、highlight ranges 全部
   表達為 segments 序列；沒有 segments 之外的剪切真相。
2. **單一 render 管線**：`render_editor_timeline.py`（Phase 1a 改寫）依 segments 做
   trim→concat→overlay compositing，preview／final 共用 code path。字幕與 overlay 時間一律以
   **post-cut timeline** 表示，renderer 負責映射回 source ranges。
3. **`render_cut.py` 降級 legacy**：僅保留舊專案相容；不得再產生統一管線讀不到的中間產物
   （`cut_map.json`、`source_cut.mp4` 進入唯讀凍結）。agent-first cut-only 流程改走統一管線。
4. **單向遷移（v1→v2 不轉換 deletes）**：migration 一律建立單一全長 segment
   （`origin: default_full_source`），**不**讀取 `edit_decisions.json`——把 approved deletes
   正確轉成 segments 需要統一 renderer 的 post-cut 時間軸映射，那是 Phase 1a 的工作。
   安全性由既有 fail-closed 檢查保證：只要存在 approved delete，editor 渲染路徑就拒絕
   final（`approved_destructive_deletes` 檢查原樣保留），因此刪除段不可能被靜默重新納入
   輸出。Phase 1a 落地 segments 轉換後，`edit_decisions.json` 才轉唯讀存查。
5. **Invalidation 與 receipt 綁定**：`segments` 是 `editor_state_revision()` 的 hash 輸入之一
   （Phase 0 起），因此 segments 任何變更→revision 變更→timeline／final approvals 失效；
   render snapshot／receipt 透過 `state_revision` 已密碼學綁定 segments，**不另設冗餘的
   `segments_hash` 欄位**（單一綁定來源，避免兩個 hash 漂移）。

## 驗收（Phase 1a 綁定）
含一次 reorder 的 plan 由統一 renderer 輸出，MP4 duration＝Σ segments ±1 frame；
字幕落在 post-cut 正確位置。
