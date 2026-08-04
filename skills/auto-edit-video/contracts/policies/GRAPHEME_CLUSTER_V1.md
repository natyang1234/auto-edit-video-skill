# grapheme_cluster_v1 Segmentation Contract

## 問題
選字上色（effect_spans）需要跨 server（shaping/render）與 browser（編輯 UI）一致的
「字」邊界；瀏覽器 ICU 版本不可控。

## 契約
1. **Server 是唯一邊界權威**：以 **macOS runtime**（NSString composed character
   sequences ≒ UAX #29 EGC）產生 canonical boundary map（`caption_render_plan.items[].clusters`，
   UTF-16 code unit [start,end) 對）。macOS 為唯一支援的 segmentation runtime
   （nat 2026-08-04 核可，取代「跨 runtime pinned Unicode」宣稱）；macOS／Unicode
   版本記入 receipt，版本改變＝engine version 改變＝boundary map 全量重算與下游 stale。
   ZWJ family 與 RI 旗幟等版本敏感案例在 corpus 標記為允許記錄性差異。
2. **Browser 只映射不判斷**：編輯 UI 的選取以 UTF-16 offset 送 server，server 吸附
   （snap）到最近的 cluster 邊界後回寫；browser 不得自行用 `Intl.Segmenter` 當真相。
3. effect_span 的 start_char/end_char 必須落在 cluster 邊界上，否則 validation fail closed。
4. Shaping engine 與 Unicode 版本 pin 進 `caption_render_plan.receipt`；版本變更→
   boundary map 全量重算→caption revision 變更→下游 stale。
5. 測試 corpus：`fixtures/grapheme_corpus.json`（CJK／NFD 組合字／ZWJ emoji／
   膚色修飾／國旗／CRLF／中英混排），實作的 segmenter 必須逐案吻合 cluster 數。
