---
name: mariadb
description: MariaDB target pack of alert-trust (mysqld_exporter). Reads the mariadb card from catalog/ at run time and checks it in the five sections (architecture, correct usage, correct observation, observation of misuse incl. bad queries, blind spots); exporter model plus MariaDB specifics such as SLAVE MONITOR for replication series and Galera. MariaDB 監控檢查：照五段讀 mariadb 卡。
argument-hint: "[env/<name>.yaml] [target-id]"
---

# MariaDB（alert-trust-mariadb）

檢查 env 檔 `targets[]` 裡 `type` 是 `mariadb` 的 target（別名：`mysqld_exporter`、`mysqld`（先問是 MySQL 還是 MariaDB））。卡片：`mariadb`（`$AT/catalog/targets/mariadb.yaml`，索引在 `$AT/catalog/map.yaml` 的 `cards.mariadb`）；model：exporter（mysqld_exporter），以卡片的 `model` 欄位為準。

## 路徑

- `$AT`：底下有 `catalog/`、`tools/`、`rules/`、`env/`、`queries/`。裝成 plugin 時是 `${CLAUDE_PLUGIN_ROOT}`（這個 plugin 的目錄；repo 裡是 symlink，從 git marketplace 安裝時已複製成實際檔案）；在 alert-trust repo clone 裡直接用（不裝 plugin，例如 opencode）時是 repo 根目錄。
- `$REF`：共用說明。plugin：`${CLAUDE_PLUGIN_ROOT}/references`；repo clone：`plugins/core/references`。
- `$AT/docs/` 只在 repo clone 有；裝成 plugin 時改讀網站，對照規則見 `$REF/five-sections.md#paths`。

## 流程

照 `$REF/five-sections.md` 做：先決條件 → 讀卡片 → 五段（架構 → 正確使用 → 正確觀測 → 不正確使用時的觀測（含 `bad_queries`）→ 目前無法觀測的痕跡）→ 回報。全程規則（唯讀、不連資料庫、隱私、數字標來源、引用證據）與深度界線也在那份。**訊號清單每次從卡片讀，不要從這份 skill 抄。**這份只寫 MariaDB 專屬的部分。

「怎麼監控」頁：`$AT/docs/targets/mariadb.md`；元件頁：`$AT/docs/components/mariadb.md`（產生的頁面，repo 裡要先跑 `tools/build_pages.py`；沒有就讀網站）。

## MariaDB 專屬

- MariaDB 與 MySQL 是兩張卡，共用 mysqld_exporter 與 `mysql_` 前綴；這個 skill 只讀 `mariadb` 卡。env 檔寫 `mysqld_exporter` 時先確認是 MariaDB。
- 複寫 series 需要的權限和 MySQL 不同（卡片 `silent_failures`：`SHOW SLAVE STATUS` 要 `SLAVE MONITOR`）；權限不足時只有那部分消失，依賴它的規則安靜地回傳空。
- 沒有啟用 Galera 的 MariaDB 也會輸出 wsrep 相關 metric，上游規則可能誤報；Galera 訊號只在 `applies_when: mariadb galera` 時適用（照卡片）。
- 規則範本 `$AT/rules/targets/mariadb.yml` 的 `job="mariadb"` 是佔位符。
- MariaDB 沒有 ASH 類的取樣，缺口與取樣器原型在 `$AT/docs/targets/mariadb-ash.md`（第 5 段）；query-exporter 範例在 `$AT/queries/query-exporter/`。

## 訪談（每輪最多 3 個單一問題，已知的跳過）

1. 拓撲：單機、複寫、Galera？（決定 `applies_when` 與 `features`）
2. exporter 帳號的權限：請使用者自己執行 `SHOW GRANTS`，遮蔽後貼回（特別看 `SLAVE MONITOR`）。
3. exporter 的期限相關 flag（例如 `--timeout-offset`）有沒有改過？

## 回報

照 `$REF/five-sections.md#output` 每個 target 一份五段表。被 `selfcheck` 或 `rule-review` 呼叫時，把結果交回它匯集；使用者直接呼叫時，最後列這張卡「建議先處理的 3 件事」。
