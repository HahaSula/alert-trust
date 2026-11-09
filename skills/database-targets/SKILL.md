---
name: database-targets
description: Database target pack of the alert-trust self-check. Applies the exporter-facing target pattern (catalog/target-pattern.yaml) and the per-target cards in catalog/targets/ (mysqld_exporter, postgres_exporter, and any card added later) to the targets listed in env.yaml - three layers of liveness (up, db up, collector success), whole metric groups that vanish instead of turning 0, exporter query deadline vs scrape_timeout, privileges, replication and multi-target /probe blast radius. Use for MySQL/MariaDB/PostgreSQL exporter alerts that never fire. 資料庫 exporter target 的盲點：三層存活、整組消失、查詢期限。
argument-hint: "[snapshot-dir] [target-id]"
---

# Database targets pack

處理 env 檔 `targets[]` 裡的每個 target。**目錄的 target 卡會持續擴充，每次都在執行時讀，不要從這份 skill 或記憶裡抄訊號清單。**

`$AT` 是 alert-trust 根目錄（plugin 安裝時 `${CLAUDE_PLUGIN_ROOT}`，在 repo 裡是 `${CLAUDE_SKILL_DIR}/../..`）。

## 全程規則

- **唯讀**：只讀 ruler 的 API 與快照。**不要連資料庫**，不執行 SQL、不改 exporter 帳號權限；需要資料庫端的資訊（GRANT、版本、replication 狀態）時，給使用者要執行的唯讀查詢，請他自己跑、遮蔽後貼回。
- **隱私**：DSN、帳號、主機名、資料庫名稱不寫進報告，用 target id 指稱。不要請使用者貼 DSN 或密碼。
- **數字標來源**：`量測`（快照檔與欄位）、`使用者說`、`推算`（公式與代入值）。
- **引用證據**：附卡片訊號或契約連結、`evidence` 等級與 `refs` 註腳，格式見 `$AT/skills/selfcheck/references/evidence.md`。
- 每次最多問 3 題；用使用者的語言。

## 先讀

1. `$AT/catalog/target-pattern.yaml`：每張 target 卡都要回答的 `required_signals`（每個有 `id`、`min_tier`、`question`、`why`、可能有 `contract`）。這是共同骨架。卡片的 `model` 決定用哪一組：`exporter`（預設）用 `required_signals`；`native`（資料庫自己提供 /metrics，例如 etcd、TiDB）用 `required_signals_native`，重點是 `up` 為 1 時服務仍可能不健康，要用健康 metric 與 blackbox 探測交叉判斷。
2. 對 env 檔每個 target，讀 `$AT/catalog/targets/<type>.yaml`（`type` 就是卡片 id）。用 `ls $AT/catalog/targets` 確認有哪些卡；**沒有對應卡**時，只用 target-pattern 的骨架逐題檢查，並在報告標明「沒有專屬卡」。
3. 卡片裡要用的欄位：`role`、`silent_failures`、`contracts`、`signals`（`tier`、`signal`、`metrics`、`applies_when`、`rules`、`upstream.note`）、`not_applicable`（若有）。`applies_when` 依環境判斷：例如 `replication`、`replica`、`mariadb galera`、`multi-target probe`、`pg_stat_statements`。
4. `$AT/catalog/contracts.yaml` 的 `exporter-deadline`，與 `$AT/docs/contracts.md#exporter-deadline` 的表（各 exporter 自己的期限從哪裡來）。
5. `$AT/docs/architecture/exporters.md`：連線池、collector 排隊、錯誤指標的差異。
6. 規則範本：卡片訊號 `rules:` 指到的 `$AT/rules/targets/*.yml`。範本裡的 `job="mysql"`、`job="postgres"` 是佔位符。

## 快照裡要讀的檔案

env 檔 target 的 `scraped_by` 是抓它的 ruler id。讀那個 ruler 的 `<scraped_by>/targets.json`（實際 job 名稱、`scrapeInterval`、`scrapeTimeout`、`lastError`、`lastScrapeDuration`、`scrapeUrl` 是否是 `/probe`）、`peaks.json`（`scrape_duration_peak{job}`、`up_min{job}`）、`metric_jobs.json`，以及**所有** ruler 的 `rules.json`（規則可能在觀察者上）。結構見 `$AT/skills/selfcheck/references/snapshot.md`。

能連 ruler 時，用 GET 查詢補量測（唯讀）：

```promql
count by (__name__) ({job="<job>"})                    # 現在有哪些 metric 族
<collector_success_metric>{job="<job>"} == 0           # 哪個 collector 失敗；metric 名稱從卡片讀
max_over_time(scrape_duration_seconds{job="<job>"}[7d])
```

## 0. 先決條件（不能跳過）

進入下面任何一節之前，確認：

1. **監控體系已確認**：評估規則的是 Prometheus 還是 vmalert、版本、資料從哪裡來、有沒有外部觀察者。沒有 `env/<name>.yaml` 時先用 `env-profile`，或依 `rule-review` 第 1 步問清楚。
2. **基礎設施的覆蓋已檢查**：已經跑過 `check_coverage.py`（沒有 API 時先用 `tools/rules_to_snapshot.py` 把 rule 檔轉成快照），並先回報第 1 級缺口。

使用者只丟 rule 檔要你「檢查」時，改走 `rule-review` skill，不要直接從時間契約開始。

## 檢查順序

依 target-pattern 的 `required_signals` 順序，每一題對照卡片裡同 id 的訊號：

1. **exporter-up**：有沒有 `up{job=<實際 job>} == 0` **加上** `absent(...)` 的規則？只看 exporter 自己的 `*_up`（上游 mixin 常見）是缺口：exporter 停機時那條 series 整條消失，規則回傳空。
2. **db-up**：有沒有看 exporter 輸出的資料庫連線 metric（名稱從卡片讀）？它和 exporter-up 要一起看。
3. **collectors-present**：服務停機或帳號被鎖時，collector 的結果整組消失。規則要寫成 `up == 1 unless on(job, instance) <collector_success>`，`== 0` 的寫法會回傳空。
4. **collector-success**：每個 collector 都成功嗎？權限不足、表或 extension 不存在時只有那個 collector 失敗，依賴它的規則安靜地回傳空。卡片的 `silent_failures` 列了已知的權限陷阱（例如 MariaDB 的 `SLAVE MONITOR`、MySQL 8.4 的欄位改名）。
5. **scrape-budget**（契約 `exporter-deadline`）：`exporter 自己的查詢期限 < scrape_timeout < scrape_interval`。代入：`scrapeTimeout`、`scrapeInterval` 從 `targets.json`（量測）；exporter 的期限依卡片與 contracts.md（例如 flag 的預設值，問使用者有沒有改，標「使用者說」）。違反時症狀從「資料庫停機」變成「exporter 停機」。建議 `scrape_duration_seconds > 0.8 × scrape_timeout` 預警。
6. **probe-blast-radius**：`targets.json` 的 `scrapeUrl` 是 `/probe` 時適用。一台 multi-target exporter 掛掉，背後所有 target 同時消失；規則要分得出是 exporter 還是服務。

然後看卡片裡 target-pattern 以外的訊號（依 `applies_when` 篩選），以及 `silent_failures` 裡和使用者版本相符的項目（卡片的 `versions_checked` 與使用者的版本不同時，標「未在此版本驗證」）。

## job 名稱

上游規則與 alert-trust 範本都依 job 名稱比對；名稱不符時規則照常載入、永遠回傳空。用 `targets.json` 的實際 job 名稱對照 env 檔的 `targets[].job` 與所有 `rules.json` 裡的 selector。`check_coverage.py --live` 回報 `rule_no_data` 時，先查這個。

## 訪談（最多 3 題一組，已知的跳過）

1. 這個資料庫有 replication、Galera、replica 或 replication slot 嗎？（決定 `applies_when` 的訊號）
2. Exporter 帳號有哪些權限？請使用者自己執行 `SHOW GRANTS`（MySQL／MariaDB）或 `\du`／`pg_roles` 查詢（PostgreSQL），遮蔽後貼回。
3. Exporter 的期限相關 flag 有沒有改過？（卡片寫了是哪個 flag）

## 回報

每個 target 一張表：

| 問題（target-pattern id） | 卡片訊號 | 狀態（covered／self_only／rule_no_data／missing／not_applicable／needs_input） | 依據 | 建議 |

缺口附卡片 `rules:` 指到的範本，提醒換掉佔位符 job 名稱與門檻，並用 `promtool check rules` 驗證。結果交回 `selfcheck`。
