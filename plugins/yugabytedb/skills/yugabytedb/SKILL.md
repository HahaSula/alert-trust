---
name: yugabytedb
description: YugabyteDB target pack of alert-trust (native /prometheus-metrics of yb-master and yb-tserver). Reads the yugabytedb card from catalog/ at run time and checks it in the five sections (architecture, correct usage, correct observation, observation of misuse incl. bad queries, blind spots); native model - self-timestamped samples that keep stale values for minutes, leader-only cluster metrics, health endpoints that return 200 on failure, master/tserver counts, YSQL via postgres_exporter. YugabyteDB 監控檢查：照五段讀 yugabytedb 卡。
argument-hint: "[env/<name>.yaml] [target-id]"
---

# YugabyteDB（alert-trust-yugabytedb）

檢查 env 檔 `targets[]` 裡 `type` 是 `yugabytedb` 的 target。卡片：`yugabytedb`（`$AT/catalog/targets/yugabytedb.yaml`，索引在 `$AT/catalog/map.yaml` 的 `cards.yugabytedb`）；model：native（yb-master、yb-tserver 的 /prometheus-metrics），以卡片的 `model` 欄位為準。

## 路徑

- `$AT`：底下有 `catalog/`、`tools/`、`rules/`、`env/`、`queries/`。裝成 plugin 時是 `${CLAUDE_PLUGIN_ROOT}`（這個 plugin 的目錄；repo 裡是 symlink，從 git marketplace 安裝時已複製成實際檔案）；在 alert-trust repo clone 裡直接用（不裝 plugin，例如 opencode）時是 repo 根目錄。
- `$REF`：共用說明。plugin：`${CLAUDE_PLUGIN_ROOT}/references`；repo clone：`plugins/core/references`。
- `$AT/docs/` 只在 repo clone 有；裝成 plugin 時改讀網站，對照規則見 `$REF/five-sections.md#paths`。

## 流程

照 `$REF/five-sections.md` 做：先決條件 → 讀卡片 → 五段（架構 → 正確使用 → 正確觀測 → 不正確使用時的觀測（含 `bad_queries`）→ 目前無法觀測的痕跡）→ 回報。全程規則（唯讀、不連資料庫、隱私、數字標來源、引用證據）與深度界線也在那份。**訊號清單每次從卡片讀，不要從這份 skill 抄。**這份只寫 YugabyteDB 專屬的部分。

「怎麼監控」頁：`$AT/docs/targets/yugabytedb.md`；元件頁：`$AT/docs/components/yugabytedb.md`（產生的頁面，repo 裡要先跑 `tools/build_pages.py`；沒有就讀網站）。

## YugabyteDB 專屬

- endpoint 的 sample 自帶時間戳：停掉的節點舊值會在查詢裡留到 lookback 結束，計數 leader 或存活節點的規則會誤判（卡片 `silent_failures`；改 `honor_timestamps: false` 能不能解決目前是推論，沒有 lab）。
- 叢集層 metric 只有 master leader 在寫，follower 是 0；聚合方式照卡片。
- 部分健康端點在轉送失敗時仍回 HTTP 200，只看狀態碼的 probe 不夠（卡片寫了哪些不能用）。
- component-count：master 與 tserver 分開算。
- YSQL 可以用 postgres_exporter 監控，但部分預設 collector 在 YSQL 上會出問題（卡片 `silent_failures`）；YSQL 的 PostgreSQL 面向另外對照 `/alert-trust-postgresql:postgresql`。

## 訪談（每輪最多 3 個單一問題，已知的跳過）

1. master 與 tserver 各有幾台？用 YSQL、YCQL 還是都有？
2. 有沒有 blackbox 探測健康端點？比對的是狀態碼還是內容？
3. 這些 job 有沒有設 `honor_timestamps` 或 `track_timestamps_staleness`？

## 回報

照 `$REF/five-sections.md#output` 每個 target 一份五段表。被 `selfcheck` 或 `rule-review` 呼叫時，把結果交回它匯集；使用者直接呼叫時，最後列這張卡「建議先處理的 3 件事」。
