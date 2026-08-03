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
