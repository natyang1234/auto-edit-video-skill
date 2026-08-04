# License Policy（asset／font／SVG／生成物）

## Allowlist（可進 final）
CC0-1.0、CC-BY-4.0（須 attribution）、CC-BY-SA-4.0（須 attribution＋同條款標示）、
OFL-1.1（字型）、Apache-2.0、Ubuntu-font-1.0（字型）、MIT、ISC、Unlicense、`internal-original`（自產）、
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
   image provider 必須符合 `assets/providers/<provider-id>/` 路徑與 canonical Creative
   Commons license URL；SVG repository provider 必須符合 `assets/generated/svg/` 路徑，且
   license evidence 是固定 provider ID→SPDX→official GitHub repo→pinned ref→LICENSE path；並有
   server 匯入流程產生、與 registry item/hash 一致的 consistency evidence；單獨新增或修改
   `assets/provenance.json` 不足以自動核可。
7. Google Fonts／Fontsource 字型只接受 project-private `assets/fonts/<sha256>.ttf|.otf`，
   並須有 server 產生的 strict v3 receipt，同時綁定 pinned commit／strict semver candidate、
   font bytes、實際下載且通過 SPDX text fingerprint 的 `licenses/<sha256>.txt`、validator identity
   與 import-time glyph coverage。Final resolve 必須重算所有實體 hash、重驗 license text 與當前
   project render text coverage；missing／舊版／手造 receipt 一律 fail closed。
8. 字型授權正文不是 marker check：`contracts/licenses/OFL-1.1.txt`、
   `contracts/licenses/Apache-2.0.txt` 與 `contracts/licenses/Ubuntu-font-1.0.txt` 是
   reviewable canonical template，下載內容只允許完整正文，
   或 bounded、明確格式的 copyright／Reserved Font Name header 加完整正文；任何截斷、增刪條款或
   cross-SPDX 內容都拒絕。Apache template 是 `https://www.apache.org/licenses/LICENSE-2.0.txt`
   的 exact 官方正文（SHA-256 `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`）。
   Ubuntu template 取自 Canonical legal page 所連結的官方
   `assets.ubuntu.com` plain-text UFL 1.0；不得用 marker、padding 或推測文字放行。

## Project filesystem 信任邊界

rights assertion、approval、provenance 與 provider consistency evidence 都是未簽章的 project
artifact。同一 OS 使用者／同 UID 可一致修改 project filesystem，屬本工具既有 trusted boundary，
不宣稱 consistency evidence 能抵抗該使用者偽造。從不可信來源下載、解壓、同步或接收的 project
不得直接視為已授權；開啟後必須重新 review／rights assert，再產生新的核可。consistency evidence
用於阻擋 accidental、malformed、stale 或只改 registry 的輸入，不是密碼學 authority。

Legacy provenance migration 只允許在使用者明確啟動 editor 的受控初始化，或真正寫入素材的
mutation 前執行。`load_registry()` 與 GET／HEAD request path 永遠純讀；遇到 legacy、未知或
近似格式時 fail closed，不得以讀取請求觸發 project filesystem 寫入。
