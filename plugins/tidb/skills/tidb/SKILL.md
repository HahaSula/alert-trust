---
name: tidb
description: TiDB target pack of alert-trust (native /metrics of TiDB server, TiKV and PD). Reads the tidb card from catalog/ at run time and checks it in the five sections (architecture, correct usage, correct observation, observation of misuse incl. bad queries, blind spots); native model - up stays 1 while the cluster is unhealthy, health second path, per-role component counts, and official rules that never fire (wrong metric names, hard-coded job, PD leader premise). TiDB 監控檢查：照五段讀 tidb 卡。
argument-hint: "[env/<name>.yaml] [target-id]"
---

# TiDB（alert-trust-tidb）

檢查 env 檔 `targets[]` 裡 `type` 是 `tidb` 的 target。卡片：`tidb`（`$AT/catalog/targets/tidb.yaml`，索引在 `$AT/catalog/map.yaml` 的 `cards.tidb`）；model：native（TiDB server、TiKV、PD 各自的 /metrics），以卡片的 `model` 欄位為準。

## 路徑

- `$AT`：底下有 `catalog/`、`tools/`、`rules/`、`env/`、`queries/`。裝成 plugin 時是 `${CLAUDE_PLUGIN_ROOT}`（這個 plugin 的目錄；repo 裡是 symlink，從 git marketplace 安裝時已複製成實際檔案）；在 alert-trust repo clone 裡直接用（不裝 plugin，例如 opencode）時是 repo 根目錄。
- `$REF`：共用說明。plugin：`${CLAUDE_PLUGIN_ROOT}/references`；repo clone：`plugins/core/references`。
- `$AT/docs/` 只在 repo clone 有；裝成 plugin 時改讀網站，對照規則見 `$REF/five-sections.md#paths`。

## 流程

照 `$REF/five-sections.md` 做：先決條件 → 讀卡片 → 五段（架構 → 正確使用 → 正確觀測 → 不正確使用時的觀測（含 `bad_queries`）→ 目前無法觀測的痕跡）→ 回報。全程規則（唯讀、不連資料庫、隱私、數字標來源、引用證據）與深度界線也在那份。**訊號清單每次從卡片讀，不要從這份 skill 抄。**這份只寫 TiDB 專屬的部分。

「怎麼監控」頁：`$AT/docs/targets/tidb.md`；元件頁：`$AT/docs/components/tidb.md`（產生的頁面，repo 裡要先跑 `tools/build_pages.py`；沒有就讀網站）。

## TiDB 專屬

- 一張卡涵蓋 TiDB server、TiKV、PD 三種角色；component-count 要分角色算。
- 官方規則直接套用前先看訊號的 `upstream.note`：lab 實測到 metric 名稱錯、`job` 寫死、PD 規則以 leader 為前提而回傳空、部分 metric 在 v8.5.8 不存在（卡片有參考）。
- PD 內嵌 etcd；PD 的 leader 抓不到時，以 leader 為前提的規則全部回傳空。
- 慢磁碟、write stall、GC 卡住沒做實驗，回報照卡片 `evidence` 標。

## 訪談（每輪最多 3 個單一問題，已知的跳過）

1. 每種角色（PD、TiKV、TiDB server）各有幾台？有沒有 TiFlash 或 TiCDC？
2. 有沒有 blackbox 探測健康端點？探測哪個角色的哪個端點？
3. 規則是官方的（tidb-ansible／TiUP 產生）還是自己寫的？job 名稱是什麼？

## 回報

照 `$REF/five-sections.md#output` 每個 target 一份五段表。被 `selfcheck` 或 `rule-review` 呼叫時，把結果交回它匯集；使用者直接呼叫時，最後列這張卡「建議先處理的 3 件事」。
