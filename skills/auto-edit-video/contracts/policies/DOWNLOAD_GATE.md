# Server-side Download Gate（/renders/、/qa/）

## 威脅
現況兩端點只檢查路徑範圍；「核可後才能下載」只是前端藏連結。任何可達 loopback 的
本機程序可未核可取得 final MP4／batch ZIP。

## /renders/ 規則
- `quality=preview` 產物：不擋（審閱需要）。
- final 產物（單支 MP4、batch item、batch ZIP）**全部四條同時成立才可下載**：
  1. current final approval（該 scope）存在且未 stale；
  2. `delivery_qa_errors()==[]`；
  3. 請求的 canonical relative path 出現在 current delivery receipt
     （single：`output`；batch：各 item `output`；ZIP：`archive`）；
  4. 磁碟檔 SHA-256 等於 receipt 記載值（`output_sha256`／`archive_sha256`）。
- **variant 產物**（`working/delivery_qa/<variant_id>.json` 有對應 receipt）走自己的
  核可槽，條件同上但以該 variant 為 scope：`variant_approval_is_current()` 成立、
  輸出檔 SHA-256 等於 receipt 的 `output_sha256`，再加下面第五條。
- 無法歸類（不在任何 receipt、也非 preview 命名）→ **fail closed 403**。

### 第五條：QA report 必須經得起重驗（2026-08-05 新增）
三條路（single／batch／variant）在放行前都要重讀 receipt 指向的 QA report，全部成立才過：
- 路徑經 `scoped_project_path(..., "qa")` 解析，**拒絕 symlink（含中繼目錄）**、
  絕對路徑、`..`、以及 `qa/` 以外的位置；
- 檔案 SHA-256 等於 receipt 的 `report_sha256`；
- `status == "pass"`；
- **含 `policy` 區塊**——這是強制門檻上線前的舊報告與之後的分界。
  舊專案會被擋下，補救是**重新 render**（只重跑 `qa_video.py` 會讓報告位元組改變、
  `report_sha256` 對不上 receipt，撞到另一個錯誤）。

### 第六條：finalized envelope 必須存在且對得上（2026-08-13 新增）
四條路（direct／single／batch／variant）都是「先 publish 進 finalized envelope，才產生
可消費指標」。發布時綁一次不夠——**消費時要再問一次同樣的問題**，否則刪掉 envelope
等於把交付悄悄降級回「相信 receipt」。放行前另外檢查：
- receipt 宣告的 `delivery_envelope` 必須位於 `working/delivery_envelopes/<id>.json`，
  且 `<id>` 與 receipt 的 `render_id`／`batch_id` 一致；
- 該 envelope `state == "finalized"`、`render_id` 相符、（有宣告時）
  `prepared_envelope_hash` 與 receipt 相符；
- 被下載的路徑必須真的由該 envelope 發布（`artifacts.output`，或 batch 的
  `batch.members[].output`）；
- **磁碟位元組的 SHA-256 等於 envelope 記載值**（不只等於 receipt 記載值）。
任一條不成立 → 403，訊息一律 `... does not match its finalized delivery envelope`
（不分辨失敗型態，避免當成探測介面）。核可閘（`delivery_qa_errors`）套用同一組規則。

**Grandfather 條款（取捨）**：只有「receipt 本身宣告了 `delivery_envelope`」才強制。
envelope 機制上線前發布的 final，其 receipt 沒有這個欄位，維持原本 receipt-only 閘，
不必重 render。代價：能寫入 receipt 的攻擊者可以刪掉該欄位把自己降級成舊格式——但同
一個攻擊者本來就能改 `output_sha256`，威脅模型內並未變差（本機同使用者程序不在防護
範圍，見下方 CSRF 段）。若哪天要收掉這個條款，做法是專案層一次性 migration 標記
（「本專案之後所有 final 都必須帶 envelope」），而不是逐 receipt 猜測年代。

## /qa/ 規則
- final approval 尚未成立：可讀（人要先看 QA 才核可）。
- final approval 成立後：僅 current receipt 關聯的 QA 檔（report、contact sheet）可讀，
  舊輪 stale QA 檔 403（防止拿舊證據冒充 current delivery evidence）。

## CSRF threat model
editor server 補齊 Host＋Origin＋CSRF 三件套（與 studio 對齊）。明確界線：CSRF 防
**跨來源瀏覽器請求**；不防本機惡意程序直接連 loopback——那屬 OS 層信任邊界，
不在本工具威脅模型內（一台跑多 agent 的機器應以 OS 使用者隔離處理）。

## 七、訊息一致性的精確範圍（2026-08-13 驗證後修訂，取代 §六「不分辨失敗型態」的字面陳述）

envelope 驗證失敗（state/id/prepared-hash/磁碟 sha256 不符、envelope 缺失）一律回統一訊息 `... does not match its finalized delivery envelope`。但三個 **receipt 自述型前置檢查**各有專屬訊息：envelope 欄位不可讀、envelope 路徑落在目錄外、envelope 名稱屬於他 render。此三者僅由 receipt 內容決定——同一路徑每次請求答案固定、下載端無任何可變輸入可探測，故不構成 probing oracle（第四輪對抗驗證 probe A14–A17/B11 實證）。另註：direct 路由由 `revalidate_finalized_delivery` 把關（早於本閘存在的等價機制），`verify_published_output` 的生產呼叫點為 single/batch/variant 三路由；variant receipt 原生無 render_id 欄位，其 id 一致性由 envelope 自身 render_id 與宣告檔名的比對承擔。
