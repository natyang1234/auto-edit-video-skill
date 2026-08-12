# SPEC — Phase 3 雙語字幕排版規格 v1（2026-08-12）

nat 2026-08-12 授權 Claude 擬定本規格（PRD Phase 3 僅質化描述）；驗收方式＝真實成片＋contact sheet 由 nat 看片簽核後才算 Phase 3 完成。本檔是 PRD `docs/PRD-kinetic-explainer-auto-pipeline.md` Phase 3 的數值附錄，不修改 PRD 本文。所有數值以 1080×1920 canvas 為基準（`render_editor_timeline.py` 預設），其他解析度按寬度等比縮放。

## 1. Mobile type tokens

| token | 值 | 依據 |
|---|---|---|
| `caption.primary.size` | 52px（現行 style 預設，不變） | caption_compositor.py:331 |
| `caption.primary.floor` | 40px（= 52 × 0.77，低於此值 fail closed 而非再縮） | 現行 MIN_AUTOFIT_SCALE 0.82 → 42.6px，取整放寬至 40 |
| `caption.secondary.scale` | 0.62（現行 TRANSLATION_SCALE，不變） | caption_compositor.py:34 |
| `caption.secondary.floor` | 32px | Phase 0e QA 既有 font floor 32px，沿用為硬下限 |
| `caption.secondary.min_scale` | 0.55（re-chunk 前允許的最低比例；32/52≈0.615，此值僅在 primary 已縮字時生效） | 推導 |

規則：secondary 實際字級 = primary 實際字級 × 0.62，但不得低於 32px；若兩條件衝突（primary 縮到 40px 時 0.62×40=24.8 < 32），以 32px floor 優先，此時進入第 3 節縮短/重排流程，不得渲染低於 32px 的英文。

## 2. Line-height rules

| 規則 | 值 |
|---|---|
| 中文主字幕行高 | 1.25 × 字級（CJK 可讀性） |
| 英文副字幕行高 | 1.20 × 字級 |
| 主／副區塊間距 | 0.35 × 副字幕字級 |
| 中文主字幕行數上限 | 2 行 |
| 英文副字幕行數上限 | 2 行（PRD「never creates a third subtitle line」＝英文區塊絕不出現第 3 行） |

現況為 CoreText 原生 ascent+descent+leading（caption_compositor.py:581-584），改為顯式倍數；行高計算必須進入 caption 高度量測與 safe-area clamp（render_editor_timeline.py:1309-1355）的同一套數字，不得量測用一套、渲染用另一套。

## 3. 長英文縮短／重排（確定性演算法，非創作）

依序執行，全程以 compositor 實測寬度為準（不用字元數估）：

1. **wrap**：base size 換行；≤2 行且不觸 safe-area → 過。
2. **autofit**：整體（主+副等比）縮字至 primary floor 40px／secondary floor 32px；符合 → 過。
3. **semantic shortening（provider 重試）**：對該 caption instance 以明確字元預算重新要求翻譯（預算 = 2 行 × floor 字級實測行容量 × 0.95），最多重試 1 次；provider receipt 記錄 shortening 輪次。
4. **fail closed**：仍放不下 → exit 2（沿用 6435c34 safe-area gate 語義），絕不靜默截斷、絕不第 3 行、絕不低於 floor 渲染。

## 4. Terminology / number preservation（nat 拍板 2026-08-12）

- 數字、百分比、單位、品牌名、產品名、專有名詞翻譯時**逐字保留**，不改寫、不換算——契約已存在（caption_delivery.py:54,958-1020 `identity_preserved`/`identity_reason`），Phase 3 只補 **mutation 測試**：竄改數字、單位換算、品牌改寫、專名音譯等變體必須被 `_validate_translations` 擋下。
- 第 3 節 shortening 重試的字元預算 prompt 必須明示「數字/專名不可刪減」；shortening 後的譯文仍須通過 identity 驗證。

## 5. 驗收（Phase 3 完成定義）

1. 每個 sub-slice：exact RED→GREEN、targeted＋full suite、fresh verifier `CONFIRMED`、獨立 commit。
2. 真實素材出一支雙語成片＋contact sheet，六平台 preset 至少驗 instagram-reels 與 tiktok（safe-area 最嚴的兩個：bottom 18%/20%）。
3. **nat 親自看片簽核**後 Phase 3 才標 complete；工程不得代簽。
