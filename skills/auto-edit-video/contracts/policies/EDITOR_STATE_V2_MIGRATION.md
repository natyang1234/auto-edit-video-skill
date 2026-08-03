# editor_state v1 → v2 升級表

## 觸發與流程
EditorServer 載入 `schema_version:1` 的 `editor_state.json` 時呼叫
`migrate_editor_state_v1_to_v2()`：升級→原子落盤（`atomic_write_json`）→逐 gate 覆寫
approval→記 `migrated_from`。升級是顯式一次性動作，之後只接受 v2。

## 新欄位與 default
| 欄位 | default | 語意 |
|---|---|---|
| `segments` | 單一全長 segment（`origin: default_full_source`，0→source duration） | 7.11.1 的剪切真相；v1 專案無剪切資訊→全長 |
| `variants` | `[]` | 尚無 variant |
| `rights` | `{asserted:false, assertion_revision:null}` | 未 assert，final gate 會擋 |
| `migrated_from` | `{schema_version:1, at, reason, previous_revision}` | 稽核用 |

## v1 既有 gate 處置（逐一，Codex 預審 M9）
| gate | 處置 | 理由 |
|---|---|---|
| `destructive_edit` | **失效**（`approved:false`＋migration reason） | v2 起刪除段收編進 segments；舊核可對應的 edit_decisions 語意已被遷移改變 |
| `highlight_selection` | **失效** | 綁 editor_state revision，revision 必然改變；且 highlights 與 segments 關係是新語意 |
| `timeline` | **失效** | 同上，直接 hash 整份 state |
| `final` | **失效** | 依賴 timeline；且下載閘（DOWNLOAD_GATE.md）要求 current final approval |

原則：**不做選擇性保留**。寧可要求重核可，不冒「舊核可涵蓋新語意」的假核可風險。
每個 gate 的失效都必須由 migration 程式顯式寫入（`approved:false`＋reason＋時間），
不得只依賴 revision 漂移的間接效果；四個 gate 各有一條測試斷言不可沿用。

## 失敗行為
migration 落盤失敗（磁碟／權限）→保留原 v1 檔不動、回 500、不得半寫；
v0／未知版本→fail closed 拒載。
