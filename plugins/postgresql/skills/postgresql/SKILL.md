---
name: postgresql
description: PostgreSQL target pack of alert-trust (postgres_exporter). Reads the postgres_exporter card from catalog/ at run time and checks it in the five sections (architecture, correct usage, correct observation, observation of misuse incl. bad queries, blind spots); exporter model - up plus absent, pg_up, collectors that vanish on missing privileges or extensions, exporter deadline vs scrape_timeout. PostgreSQL 監控檢查：照五段讀 postgres_exporter 卡。
argument-hint: "[env/<name>.yaml] [target-id]"
---

# PostgreSQL（alert-trust-postgresql）

檢查 env 檔 `targets[]` 裡 `type` 是 `postgres_exporter` 的 target（別名：`postgres`、`postgresql`）。卡片：`postgres_exporter`（`$AT/catalog/targets/postgres_exporter.yaml`，索引在 `$AT/catalog/map.yaml` 的 `cards.postgres_exporter`）；model：exporter（postgres_exporter；卡片沒寫 `model` 就是 exporter），以卡片的 `model` 欄位為準。

## 路徑

- `$AT`：底下有 `catalog/`、`tools/`、`rules/`、`env/`、`queries/`。裝成 plugin 時是 `${CLAUDE_PLUGIN_ROOT}`（這個 plugin 的目錄；repo 裡是 symlink，從 git marketplace 安裝時已複製成實際檔案）；在 alert-trust repo clone 裡直接用（不裝 plugin，例如 opencode）時是 repo 根目錄。
- `$REF`：共用說明。plugin：`${CLAUDE_PLUGIN_ROOT}/references`；repo clone：`plugins/core/references`。
- `$AT/docs/` 只在 repo clone 有；裝成 plugin 時改讀網站，對照規則見 `$REF/five-sections.md#paths`。

## 流程

照 `$REF/five-sections.md` 做：先決條件 → 讀卡片 → 五段（架構 → 正確使用 → 正確觀測 → 不正確使用時的觀測（含 `bad_queries`）→ 目前無法觀測的痕跡）→ 回報。全程規則（唯讀、不連資料庫、隱私、數字標來源、引用證據）與深度界線也在那份。**訊號清單每次從卡片讀，不要從這份 skill 抄。**這份只寫 PostgreSQL 專屬的部分。

「怎麼監控」頁：`$AT/docs/targets/postgres.md`；元件頁：`$AT/docs/components/postgres_exporter.md`（產生的頁面，repo 裡要先跑 `tools/build_pages.py`；沒有就讀網站）。

## PostgreSQL 專屬

- 卡片 id 是 `postgres_exporter`；env 的 `postgres`、`postgresql` 都對到它。
- 規則範本 `$AT/rules/targets/` 裡的 `job="postgres"` 是佔位符。
- 權限不足、extension 不存在（例如 `pg_stat_statements`）時只有那部分 collector 消失；`pg_stat_statements` 相關訊號只在 `applies_when` 成立時適用。
- 卡片的 `not_applicable_aspects` 寫了 PostgreSQL 核心沒有的面向（例如共識），回報成「不適用」並附 why。
- Patroni、pgBackRest 的規則只讀碼（見卡片 `evidence`）；query-exporter 補盲點的範例在 `$AT/queries/query-exporter/`。

## 訪談（每輪最多 3 個單一問題，已知的跳過）

1. 拓撲：單機、streaming replication、有沒有 Patroni、有沒有 replication slot？（決定 `applies_when`）
2. exporter 帳號的權限：請使用者自己查 `pg_roles`（是否有 `pg_monitor`），遮蔽後貼回。
3. 有沒有裝 `pg_stat_statements`？exporter 的期限或 collector 參數有沒有改過？

## 回報

照 `$REF/five-sections.md#output` 每個 target 一份五段表。被 `selfcheck` 或 `rule-review` 呼叫時，把結果交回它匯集；使用者直接呼叫時，最後列這張卡「建議先處理的 3 件事」。
