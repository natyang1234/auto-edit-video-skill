# Contracts（Phase 0：資料契約與紅線）

本目錄是 PRD-auto-edit-folder-visual-composer Phase 0 的交付：所有 pipeline artifact 的
versioned／hashable／validatable 契約。

## 結構
- `schemas/`：契約 schema（`manifest.json` 的 `required_schemas` 為權威清單，registry 斷言 exact set）。
- `instances/`：必須通過自己 schema 的第一方實例（如 `style_pack__dark_data_presenter`）。
- `fixtures/`：每個 schema 的 valid／invalid 樣本；invalid 為逐 constraint 針對性負例。
- `policies/`：文字契約（timeline、analysis engine、migration、license、privacy、SVG、grapheme、download gate）。

## Schema dialect（重要）
schema 檔借用 JSON Schema 語法，但**不是** JSON Schema 2020-12 實作；驗證器
（`scripts/contract_registry.py`）只支援明列子集，**遇到未支援 keyword 直接報錯**（防
「keyword 被靜默忽略→fixture 假綠」）。跨檔約束（evidence 引用、timing SSOT 回指）由
semantic validators 顯式實作。

## Versioning 與 hash
- 每個 artifact 有整數 `schema_version`，只增不改義。
- Canonical hash＝`contract_registry.canonical_hash()`：sha256(compact sorted-keys UTF-8 JSON)。
  artifact JSON 禁 duplicate key／NaN／Infinity（parser 強制）。此 Python 實作為 normative。

## 驗證
```
python3 scripts/contract_registry.py validate
```
全套：schema dialect 檢查、manifest exact set、valid fixtures 全過、invalid fixtures 全被拒、
instances 通過。
