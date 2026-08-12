# SPEC — Phase 3 雙語字幕排版規格 v1（2026-08-12）

nat 2026-08-12 授權 Claude 擬定本規格（PRD Phase 3 僅質化描述）；驗收方式＝真實成片＋contact sheet 由 nat 看片簽核後才算 Phase 3 完成。本檔是 PRD `docs/PRD-kinetic-explainer-auto-pipeline.md` Phase 3 的數值附錄，不修改 PRD 本文。所有數值以 1080×1920 canvas 為基準（`render_editor_timeline.py` 預設），其他解析度按寬度等比縮放。

## 1. Mobile type tokens

| token | 值 | 依據 |
|---|---|---|
| `caption.primary.size` | 52px（現行 style 預設，不變） | caption_compositor.py:331 |
| `caption.primary.floor` | 40px（= 52 × 0.77，低於此值 fail closed 而非再縮） | 現行 MIN_AUTOFIT_SCALE 0.82 → 42.6px，取整放寬至 40 |
| `caption.secondary.scale` | 0.62（現行 TRANSLATION_SCALE，不變） | caption_compositor.py:34 |
| `caption.secondary.floor` | 32px | Phase 0e QA 既有 font floor 32px，沿用為硬下限 |
規則（v1.1 修訂，2026-08-12 實作定案）：secondary 實際字級 = primary 實際字級 × 0.62，但不得低於 32px×render_scale（floor 與字級同單位，preview 等縮小輸出按比例縮）。floor 覆蓋比例時屬合法狀態：plan 記錄實際 `secondary_ratio` 並以 `caption_typography.floor_overrode_ratio` 回報，照常出片；**只有 floor 字級下譯文仍需第 3 行（版面確實放不下）才 fail closed**（所有品質等級、compositor 層拋錯、CLI exit 2 無輸出）。原 v1 的 `min_scale 0.55` 刪除（基準 52px 下名義 secondary 32.24px 與 floor 僅差 0.7%，該 token 無鑑別力且不可達）。排版規格版本以 `TYPOGRAPHY_REVISION` 常數進 plan cache revision hash，確保規格變更使舊 plan 失效。

## 2. Line-height rules

| 規則 | 值 |
|---|---|
| 中文主字幕行高 | 1.25 × 字級（CJK 可讀性） |
| 英文副字幕行高 | 1.20 × 字級 |
| 主／副區塊間距 | 0.35 × 副字幕字級 |
| 中文主字幕行數上限 | 2 行 |
| 英文副字幕行數上限 | 2 行（PRD「never creates a third subtitle line」＝英文區塊絕不出現第 3 行） |

帶強調（`effect_spans.font_scale`）時：主字幕行高 = 1.25 × max(base 字級, 該區塊最大 run 字級)，即強調把行距等比撐開、**任何情況不得小於 1.25 × base**。此為 v1.1 補訂：釘死在 1.25×base 時，強調 run 的 ascent 超過釘值會讓 CoreText 反向壓縮行盒（實測 font_scale 1.18 → 61.0px／規格 65.0，1.8 → 49.0px 已小於字級本身，兩行 CJK 字面相貼）。副字幕行高與區塊間距不受強調影響。

現況為 CoreText 原生 ascent+descent+leading（caption_compositor.py:581-584），改為顯式倍數；行高計算必須進入 caption 高度量測與 safe-area clamp（render_editor_timeline.py:1309-1355）的同一套數字，不得量測用一套、渲染用另一套。

## 3. 長英文縮短／重排（確定性演算法，非創作）

依序執行，全程以 compositor 實測寬度為準（不用字元數估）：

1. **wrap**：base size 換行；≤2 行且不觸 safe-area → 過。
2. **autofit**：整體（主+副等比）縮字至 primary floor 40px／secondary floor 32px；符合 → 過。
3. **semantic shortening（provider 重試）**：對該 caption instance 以明確字元預算重新要求翻譯（預算 = 2 行 × floor 字級實測行容量 × 0.95；**v1.2 修訂：無斷行機會的整段文本改用 1 行容量 × 0.95**，否則預算大於原譯文長度、重試無意義），最多重試 1 次；provider receipt 記錄 shortening 輪次與預算。已知限制：預算在 safe-area 收窄 max_width 前量測，收窄後才溢出的案例由既有 safe-area gate fail closed 兜底。
4. **fail closed**：仍放不下 → exit 2（沿用 6435c34 safe-area gate 語義），絕不靜默截斷、絕不第 3 行、絕不低於 floor 渲染。

## 4. Terminology / number preservation（nat 拍板 2026-08-12）

- 數字、百分比、單位、品牌名、產品名、專有名詞翻譯時**逐字保留**，不改寫、不換算——契約已存在（caption_delivery.py:54,958-1020 `identity_preserved`/`identity_reason`），Phase 3 只補 **mutation 測試**：竄改數字、單位換算、品牌改寫、專名音譯等變體必須被 `_validate_translations` 擋下。
- 第 3 節 shortening 重試的字元預算 prompt 必須明示「數字/專名不可刪減」；shortening 後的譯文仍須通過 identity 驗證。
- **中文數字保真＝best-effort，不阻擋（v1.4，2026-08-13 主 session 裁定）**：來源以中文數字書寫的量（三十、百分之八十七、二十四）**不再硬性要求譯文出現對應阿拉伯數字**。`chinese_number_value`/`chinese_percent_value`/`source_number_sequence` 保留為 metadata／warning（另見 `chinese_number_advisories`），其產出不進任何 blocking 檢查。
  - 理由＝誤殺率遠高於命中率：十年前→"10 years ago"、前十名→"Top 10"、二十四小時→"Open 24/7"、三分之一→"1/3"、十點三十分→"Meeting at 10:30"、千萬別忘記→"Do not forget"、百分之零點五→"0.5%"、百分之八十七→"87 percent" 全為正確譯文卻被舊規則擋下。規則對日常中文語句頻繁 fail closed，實務結果是整組保真閘被關掉，連讀得準的阿拉伯數字也一併失去保護。
  - **對稱豁免**：來源含任何中文數字字元（`CHINESE_NUMERAL_CHARS`，含 一/十/萬 等成語用字）時，該 caption 一併豁免 `translation_number_invented` 與 `translation_number_order`——讀不出來源數列，就無法宣稱譯文「憑空捏造」或「換序」，這兩項指控都是對來源數列的陳述。
  - **硬強制僅限來源阿拉伯數字**（不得退步）：來源寫成數字時，`translation_token_missing`（含 multiset 重複次數）、`translation_number_invented`、`translation_number_order` 全數維持；A1 正規化（2,000≡2000、全形數字、`12,00` 仍視為 1200 並擋）維持；`5mW≠5MW` 大小寫敏感維持；品牌名 casefold 維持。
  - 兜底＝§5 的 nat 看片簽核。特徵化測試：test_phase3_preservation_mutations.py（`ChineseNumberBestEffortTests`／`ChineseNumberFalsePositiveTests`／`ArabicNumberEnforcementSurvivesTests`）。
  - **豁免範圍為整句（已知缺口，v1.4 收案確認）**：來源含 一/十 等字（含 一起/一直 等慣用語）時該 caption 的 invented/order 一併關閉——「一起投資了 100 元」譯文憑空 +300% 會放行（存在性 token_missing 仍護住 100 本身，實測 3→5 被擋）。第五輪 verifier 建議的收窄法（僅解析出真數值才豁免＋每數字溯源）會對「二十四小時→Open 24/7」類慣用譯法製造新誤殺，故不採；記 Phase 4 backlog 再議。
- **已知接受的缺口（v1.3，2026-08-13 主 session 裁定）**：整句中文在合法 identity_reason 下原樣不翻可通過驗證——identity 機制約束「哪些理由」而非「可回聲多少」；收窄需武斷的名字/句子判別且違反 Phase 0c 已 checkpoint 契約。風險上限＝漏「未翻譯」不會漏「翻譯錯誤」，由 §5 的 nat 看片簽核把關。特徵化測試：test_phase3_preservation_mutations.py。

## 5. 驗收（Phase 3 完成定義）

1. 每個 sub-slice：exact RED→GREEN、targeted＋full suite、fresh verifier `CONFIRMED`、獨立 commit。
2. 真實素材出一支雙語成片＋contact sheet，六平台 preset 至少驗 instagram-reels 與 tiktok（safe-area 最嚴的兩個：bottom 18%/20%）。
3. **nat 親自看片簽核**後 Phase 3 才標 complete；工程不得代簽。
