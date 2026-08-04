# License Policy（asset／font／SVG／生成物）

## Allowlist（可進 final）
CC0-1.0、CC-BY-4.0（須 attribution）、CC-BY-SA-4.0（須 attribution＋同條款標示）、
OFL-1.1（字型）、Apache-2.0、MIT、Unlicense、`internal-original`（自產）、
`user-owned`（rights assertion 核可）。

## 拒絕
CC-BY-NC／ND 系列、未知授權、來源不可考、以及來源條款禁 AI 訓練或再散佈的素材
（例：Kaboompics、SOZAIYA-SAN 標註禁 AI 訓練——不入庫）。

## 規則
1. 每個外部素材必須有 `asset_provenance` 記錄：spdx、來源 URL、下載 hash、verified_at。
2. attribution_required 的素材：final render 前自動彙整 `ATTRIBUTION.md`，缺任何一筆→
   final gate fail closed。
3. 生成物（ChatGPT／Gemini bridge）標 `license_class: generated`，僅作原創圖像／背景；
   事實型 chart／stat 必須 structured data code-render（PRD 紅線）。
4. 本機資料夾素材：未經 rights assertion（`rights_assertion.schema.json`）不得進 final。
5. rights assertion 與 license 記錄都綁 hash；素材檔變更→記錄失效。
6. 內建 provider 自動核可還必須同時符合固定 provider ID、
   `assets/providers/<provider-id>/` 路徑、canonical Creative Commons license URL，並有
   server 匯入流程產生、與 registry item/hash 一致的 consistency evidence；單獨新增或修改
   `assets/provenance.json` 不足以自動核可。

## Project filesystem 信任邊界

rights assertion、approval、provenance 與 provider consistency evidence 都是未簽章的 project
artifact。同一 OS 使用者／同 UID 可一致修改 project filesystem，屬本工具既有 trusted boundary，
不宣稱 consistency evidence 能抵抗該使用者偽造。從不可信來源下載、解壓、同步或接收的 project
不得直接視為已授權；開啟後必須重新 review／rights assert，再產生新的核可。consistency evidence
用於阻擋 accidental、malformed、stale 或只改 registry 的輸入，不是密碼學 authority。

Legacy provenance migration 只允許在使用者明確啟動 editor 的受控初始化，或真正寫入素材的
mutation 前執行。`load_registry()` 與 GET／HEAD request path 永遠純讀；遇到 legacy、未知或
近似格式時 fail closed，不得以讀取請求觸發 project filesystem 寫入。
