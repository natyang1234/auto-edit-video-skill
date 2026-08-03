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
- 無法歸類（不在任何 receipt、也非 preview 命名）→ **fail closed 403**。

## /qa/ 規則
- final approval 尚未成立：可讀（人要先看 QA 才核可）。
- final approval 成立後：僅 current receipt 關聯的 QA 檔（report、contact sheet）可讀，
  舊輪 stale QA 檔 403（防止拿舊證據冒充 current delivery evidence）。

## CSRF threat model
editor server 補齊 Host＋Origin＋CSRF 三件套（與 studio 對齊）。明確界線：CSRF 防
**跨來源瀏覽器請求**；不防本機惡意程序直接連 loopback——那屬 OS 層信任邊界，
不在本工具威脅模型內（一台跑多 agent 的機器應以 OS 使用者隔離處理）。
