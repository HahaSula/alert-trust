---
name: mysql
description: MySQL target pack of alert-trust (mysqld_exporter). Reads the mysql card from catalog/ at run time and checks it in the five sections (architecture, correct usage, correct observation, observation of misuse incl. bad queries, blind spots); exporter model - up plus absent, mysql_up, collector_success groups that vanish, exporter deadline vs scrape_timeout, privileges, /probe blast radius. MySQL 監控檢查：照五段讀 mysql 卡。
argument-hint: "[env/<name>.yaml] [target-id]"
---

# MySQL（alert-trust-mysql）

檢查 env 檔 `targets[]` 裡 `type` 是 `mysql` 的 target（別名：`mysqld_exporter`、`mysqld`（這兩個別名也可能是 MariaDB，要先問））。卡片：`mysql`（`$AT/catalog/targets/mysql.yaml`，索引在 `$AT/catalog/map.yaml` 的 `cards.mysql`）；model：exporter（mysqld_exporter），以卡片的 `model` 欄位為準。

## 路徑

- `$AT`：底下有 `catalog/`、`tools/`、`rules/`、`env/`、`queries/`。裝成 plugin 時是 `${CLAUDE_PLUGIN_ROOT}`（這個 plugin 的目錄；repo 裡是 symlink，從 git marketplace 安裝時已複製成實際檔案）；在 alert-trust repo clone 裡直接用（不裝 plugin，例如 opencode）時是 repo 根目錄。
- `$REF`：共用說明。plugin：`${CLAUDE_PLUGIN_ROOT}/references`；repo clone：`plugins/core/references`。
- `$AT/docs/` 只在 repo clone 有；裝成 plugin 時改讀網站，對照規則見 `$REF/five-sections.md#paths`。

## 流程

照 `$REF/five-sections.md` 做：先決條件 → 讀卡片 → 五段（架構 → 正確使用 → 正確觀測 → 不正確使用時的觀測（含 `bad_queries`）→ 目前無法觀測的痕跡）→ 回報。全程規則（唯讀、不連資料庫、隱私、數字標來源、引用證據）與深度界線也在那份。**訊號清單每次從卡片讀，不要從這份 skill 抄。**這份只寫 MySQL 專屬的部分。

「怎麼監控」頁：`$AT/docs/targets/mysql.md`；元件頁：`$AT/docs/components/mysql.md`（產生的頁面，repo 裡要先跑 `tools/build_pages.py`；沒有就讀網站）。

## MySQL 專屬

- MySQL 與 MariaDB 是兩張卡（`mysql`、`mariadb`），共用 mysqld_exporter、metric 前綴都是 `mysql_`，但版本差異與專屬訊號不同。env 檔寫 `mysqld_exporter` 或 `mysqld` 時先問是哪一種；沒答案前用 `mysql` 卡並標明假設。是 MariaDB 時改用 `/alert-trust-mariadb:mariadb`（沒裝就照 `$REF/five-sections.md#generic` 讀 `mariadb` 卡）。
- 規則範本 `$AT/rules/targets/mysql.yml` 的 `job="mysql"` 是佔位符。
- exporter 型的三層存活（`up`、`mysql_up`、`mysql_exporter_collector_success`）在停機時各自消失的方式不同，照卡片 `silent_failures` 逐條對照使用者的規則。
- 版本差異頁 `$AT/docs/targets/versions/`（只讀碼）；卡片多數參考釘在 8.4.7，使用者版本不同時標「未在此版本驗證」。
- exporter 沒輸出的痕跡（第 5 段）若 `options` 提到 query-exporter，設定範例在 `$AT/queries/query-exporter/`。

## 訪談（每輪最多 3 個單一問題，已知的跳過）

1. 拓撲：單機、非同步複寫、semi-sync、Group Replication？有沒有 heartbeat 表？（決定 `applies_when` 與 `features`）
2. exporter 帳號的權限：請使用者自己執行 `SHOW GRANTS`，遮蔽後貼回。
3. exporter 的期限相關 flag（例如 `--timeout-offset`）有沒有改過？

## 回報

照 `$REF/five-sections.md#output` 每個 target 一份五段表。被 `selfcheck` 或 `rule-review` 呼叫時，把結果交回它匯集；使用者直接呼叫時，最後列這張卡「建議先處理的 3 件事」。
