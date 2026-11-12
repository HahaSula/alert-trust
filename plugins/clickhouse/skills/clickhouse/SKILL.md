---
name: clickhouse
description: ClickHouse target pack of alert-trust (built-in <prometheus> /metrics plus query-exporter on system tables). Reads the clickhouse card from catalog/ at run time and checks it in the five sections (architecture, correct usage, correct observation, observation of misuse incl. bad queries, blind spots); native model - the /metrics endpoint is off by default, Keeper quorum loss while up and /ping stay healthy, server/keeper counts, heavy async metrics off by default, renamed async metrics. ClickHouse 監控檢查：照五段讀 clickhouse 卡。
argument-hint: "[env/<name>.yaml] [target-id]"
---

# ClickHouse（alert-trust-clickhouse）

檢查 env 檔 `targets[]` 裡 `type` 是 `clickhouse` 的 target。卡片：`clickhouse`（`$AT/catalog/targets/clickhouse.yaml`，索引在 `$AT/catalog/map.yaml` 的 `cards.clickhouse`）；model：native（內建 `<prometheus>` /metrics）＋ query-exporter 查 system 表，以卡片的 `model` 欄位為準。

## 路徑

- `$AT`：底下有 `catalog/`、`tools/`、`rules/`、`env/`、`queries/`。裝成 plugin 時是 `${CLAUDE_PLUGIN_ROOT}`（這個 plugin 的目錄；repo 裡是 symlink，從 git marketplace 安裝時已複製成實際檔案）；在 alert-trust repo clone 裡直接用（不裝 plugin，例如 opencode）時是 repo 根目錄。
- `$REF`：共用說明。plugin：`${CLAUDE_PLUGIN_ROOT}/references`；repo clone：`plugins/core/references`。
- `$AT/docs/` 只在 repo clone 有；裝成 plugin 時改讀網站，對照規則見 `$REF/five-sections.md#paths`。

## 流程

照 `$REF/five-sections.md` 做：先決條件 → 讀卡片 → 五段（架構 → 正確使用 → 正確觀測 → 不正確使用時的觀測（含 `bad_queries`）→ 目前無法觀測的痕跡）→ 回報。全程規則（唯讀、不連資料庫、隱私、數字標來源、引用證據）與深度界線也在那份。**訊號清單每次從卡片讀，不要從這份 skill 抄。**這份只寫 ClickHouse 專屬的部分。

「怎麼監控」頁：`$AT/docs/targets/clickhouse.md`；元件頁：`$AT/docs/components/clickhouse.md`（產生的頁面，repo 裡要先跑 `tools/build_pages.py`；沒有就讀網站）。

## ClickHouse 專屬

- `/metrics` 預設不存在（`<prometheus>` 區塊在預設 config 裡是註解）；沒打開時看起來和 process 停掉一樣（卡片 `silent_failures`）。
- Keeper 失去 quorum 時 server 的 `up` 與部分 HTTP 端點仍正常，health-second-path 要用卡片寫的 metric 與端點內容判斷。
- component-count：server（含副本）與 Keeper 分開算。
- 部分 async metric 預設不算、或在新版改成 label，照舊名稱寫的規則回傳空；看訊號的 `versions`。
- exporter 沒輸出的 system 表（mutations、Distributed 佇列、備份等）用 query-exporter 補，設定在 `$AT/queries/clickhouse/clickhouse.yaml`（卡片 `blind_spots.covered_by`）。

## 訪談（每輪最多 3 個單一問題，已知的跳過）

1. 有幾台 server、幾個副本、幾台 Keeper（還是用 ZooKeeper）？
2. `<prometheus>` 區塊開了哪些子項？`asynchronous_metrics_enable_heavy_metrics` 有沒有開？
3. 有沒有 query-exporter 或其他工具在查 system 表？

## 回報

照 `$REF/five-sections.md#output` 每個 target 一份五段表。被 `selfcheck` 或 `rule-review` 呼叫時，把結果交回它匯集；使用者直接呼叫時，最後列這張卡「建議先處理的 3 件事」。
