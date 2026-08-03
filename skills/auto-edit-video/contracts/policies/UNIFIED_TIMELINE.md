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
4. **單向遷移**：`edit_decisions.json` 的 approved deletes 在 v1→v2 migration 時轉換為
   segments（刪除段＝不出現在 segments 序列），之後 `edit_decisions.json` 唯讀存查。
5. **Invalidation**：segments 任何變更→editor_state revision 變更→timeline／final approvals
   失效（沿用既有 revision-bound gate 機制）。

## 驗收（Phase 1a 綁定）
含一次 reorder 的 plan 由統一 renderer 輸出，MP4 duration＝Σ segments ±1 frame；
字幕落在 post-cut 正確位置。
