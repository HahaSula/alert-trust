---
name: mongodb
description: MongoDB target pack of alert-trust (Percona mongodb_exporter). Reads the mongodb_exporter card from catalog/ at run time and checks it in the five sections (architecture, correct usage, correct observation, observation of misuse incl. bad queries, blind spots); exporter model - collector flags that must be enabled or no replication series exist, clusterMonitor privilege, mongodb_up vs up, lag series that vanish without a primary. MongoDB 監控檢查：照五段讀 mongodb_exporter 卡。
argument-hint: "[env/<name>.yaml] [target-id]"
---

# MongoDB（alert-trust-mongodb）

檢查 env 檔 `targets[]` 裡 `type` 是 `mongodb_exporter` 的 target（別名：`mongodb`、`mongo`）。卡片：`mongodb_exporter`（`$AT/catalog/targets/mongodb_exporter.yaml`，索引在 `$AT/catalog/map.yaml` 的 `cards.mongodb_exporter`）；model：exporter（Percona mongodb_exporter），以卡片的 `model` 欄位為準。

## 路徑

- `$AT`：底下有 `catalog/`、`tools/`、`rules/`、`env/`、`queries/`。裝成 plugin 時是 `${CLAUDE_PLUGIN_ROOT}`（這個 plugin 的目錄；repo 裡是 symlink，從 git marketplace 安裝時已複製成實際檔案）；在 alert-trust repo clone 裡直接用（不裝 plugin，例如 opencode）時是 repo 根目錄。
- `$REF`：共用說明。plugin：`${CLAUDE_PLUGIN_ROOT}/references`；repo clone：`plugins/core/references`。
- `$AT/docs/` 只在 repo clone 有；裝成 plugin 時改讀網站，對照規則見 `$REF/five-sections.md#paths`。

## 流程

照 `$REF/five-sections.md` 做：先決條件 → 讀卡片 → 五段（架構 → 正確使用 → 正確觀測 → 不正確使用時的觀測（含 `bad_queries`）→ 目前無法觀測的痕跡）→ 回報。全程規則（唯讀、不連資料庫、隱私、數字標來源、引用證據）與深度界線也在那份。**訊號清單每次從卡片讀，不要從這份 skill 抄。**這份只寫 MongoDB 專屬的部分。

「怎麼監控」頁：`$AT/docs/targets/mongodb.md`；元件頁：`$AT/docs/components/mongodb_exporter.md`（產生的頁面，repo 裡要先跑 `tools/build_pages.py`；沒有就讀網站）。

## MongoDB 專屬

- 卡片 id 是 `mongodb_exporter`；env 的 `mongodb`、`mongo` 都對到它。
- 沒開 collector 參數時 exporter 幾乎只輸出 `mongodb_up`，複寫等 series 完全不存在（卡片 `silent_failures`）；先確認 collector 旗標，再看其他訊號。
- 帳號權限不足時 `mongodb_up` 與 `up` 都可能是 1，只有大量 series 消失；照卡片對照。
- 沒有 primary 時複寫延遲的 series 整組消失，「lag > N」規則回傳空；sharded cluster 目前只讀碼。

## 訪談（每輪最多 3 個單一問題，已知的跳過）

1. 拓撲：replica set 幾個成員、有沒有 arbiter、是不是 sharded？（決定 `applies_when`）
2. exporter 帳號的角色：請使用者自己執行 `db.getUser()`，遮蔽後貼回。
3. mongodb_exporter 開了哪些 `--collector.*` 旗標？

## 回報

照 `$REF/five-sections.md#output` 每個 target 一份五段表。被 `selfcheck` 或 `rule-review` 呼叫時，把結果交回它匯集；使用者直接呼叫時，最後列這張卡「建議先處理的 3 件事」。
