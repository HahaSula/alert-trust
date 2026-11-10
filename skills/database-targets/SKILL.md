---
name: database-targets
description: Database target pack of the alert-trust self-check. Reads the target cards in catalog/targets/ at run time (MySQL/MariaDB, PostgreSQL, Valkey/Redis, MongoDB via exporters; etcd, TiDB, YugabyteDB, ClickHouse via their own /metrics; plus any card added later) and applies the standard questions of catalog/target-pattern.yaml for the card's model - exporter: three layers of liveness (up, db up, collector success), metric groups that vanish instead of turning 0, exporter query deadline vs scrape_timeout, privileges, multi-target blast radius; native: up stays 1 while the database is unhealthy, so cross-check up, a blackbox health probe and the database's own health metric, plus scrape size limits and per-role member counts. Use for database alerts that never fire or to find database monitoring gaps. 資料庫 target 的盲點：exporter 型的三層存活與查詢期限，native 型的 up／探測／健康 metric 交叉判斷。
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
2. 對 env 檔每個 target，讀 `$AT/catalog/targets/<type>.yaml`（`type` 就是卡片 id）。用 `ls $AT/catalog/targets` 確認有哪些卡；**沒有對應卡**時，只用 target-pattern 的骨架逐題檢查，並在報告標明「沒有專屬卡」。MySQL 與 MariaDB 是兩張卡（`mysql`、`mariadb`，共用 mysqld_exporter、metric 前綴都是 `mysql_`，但版本差異與專屬訊號不同）；env 檔寫 `mysqld_exporter` 時先問是哪一種，沒答案前用 `mysql` 卡並標明假設。
3. 卡片裡要用的欄位：`role`、`silent_failures`、`contracts`、`signals`（`tier`、`signal`、`metrics`、`applies_when`、`rules`、`upstream.note`）、`not_applicable`（若有）。`applies_when` 依環境判斷：例如 `replication`、`replica`、`mariadb galera`、`multi-target probe`、`pg_stat_statements`。
4. `$AT/catalog/contracts.yaml` 的 `exporter-deadline`，與 `$AT/docs/contracts.md#exporter-deadline` 的表（各 exporter 自己的期限從哪裡來）。
5. `$AT/docs/architecture/exporters.md`：連線池、collector 排隊、錯誤指標的差異。
6. 規則範本：卡片訊號 `rules:` 指到的 `$AT/rules/targets/*.yml`。範本裡的 `job="mysql"`、`job="mariadb"`、`job="postgres"` 是佔位符。

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

先看卡片的 `model`（沒寫就是 `exporter`），選對應的一組標準問題，依 target-pattern 的順序逐題對照卡片裡同 id（或 `aliases`）的訊號。

### model: exporter（`required_signals`）

1. **exporter-up**：有沒有 `up{job=<實際 job>} == 0` **加上** `absent(...)` 的規則？只看 exporter 自己的 `*_up`（上游 mixin 常見）是缺口：exporter 停機時那條 series 整條消失，規則回傳空。
2. **db-up**：有沒有看 exporter 輸出的資料庫連線 metric（名稱從卡片讀）？它和 exporter-up 要一起看。
3. **collectors-present**：服務停機或帳號被鎖時，collector 的結果整組消失。規則要寫成 `up == 1 unless on(job, instance) <collector_success>`，`== 0` 的寫法會回傳空。
4. **collector-success**：每個 collector 都成功嗎？權限不足、表或 extension 不存在、collector 參數沒開時，只有那部分消失，依賴它的規則安靜地回傳空。卡片的 `silent_failures` 列了已知陷阱（例如 MariaDB 的 `SLAVE MONITOR`、mongodb_exporter 沒開 collector 參數時沒有任何複寫 series）。
5. **scrape-budget**（契約 `exporter-deadline`）：`exporter 自己的查詢期限 < scrape_timeout < scrape_interval`。代入：`scrapeTimeout`、`scrapeInterval` 從 `targets.json`（量測）；exporter 的期限依卡片與 contracts.md（例如 redis_exporter 的連線逾時預設比常見的 `scrape_timeout` 長）。違反時症狀從「資料庫停機」變成「exporter 停機」。建議 `scrape_duration_seconds > 0.8 × scrape_timeout` 預警。
6. **probe-blast-radius**：`targets.json` 的 `scrapeUrl` 是 `/probe` 時適用。一台 multi-target exporter 掛掉，背後所有 target 同時消失；規則要分得出是 exporter 還是服務。
7. **exporter-query-timeout、replication-lag、role-detection**：照卡片的訊號與 `why`。

### model: native（`required_signals_native`）

資料庫自己回應 `/metrics`，沒有 exporter 這一層。**`up` 只代表 process 回應了 HTTP**：失去 quorum、失去 leader、副本唯讀時，`up` 常常仍是 1（etcd、TiDB、YugabyteDB、ClickHouse 的 lab 都量到）。

1. **endpoint-up**：`up == 0` 加 `absent`。
2. **health-second-path**（最重要）：三個訊號交叉判斷，缺任何一個都要報缺口：
    - `up`
    - blackbox 從另一條路徑探測健康端點（端點與要比對的回應內容從卡片讀；有些端點永遠回 200 或只看關機旗標，卡片會寫哪些不能用）
    - 資料庫自己的健康 metric（例如 leader、quorum、唯讀副本數；有些只在 leader 上有值，卡片會寫）

    判斷表：`up` 0、探測 1 → scrape 本身的問題；兩者都 0 → process 或主機；`up` 1、健康 metric 異常或探測失敗 → 服務不健康。
3. **metric-families-present**：關鍵 metric 每個 instance 都在嗎？版本改名、功能沒開（例如 ClickHouse 的 `<prometheus>` 區塊預設關閉）時整組消失。
4. **scrape-budget**：`scrape_duration_seconds` 對 `scrape_timeout`，加上 `sample_limit`／`body_size_limit`：超過時整次 scrape 失敗、所有 series 一起消失。
5. **component-count**：每種角色的節點數對嗎（例如 PD／TiKV／TiDB、master／tserver、server／keeper）。
6. **endpoint-blocking**：產生 metric 時會不會被資料庫內部的鎖卡住；照卡片的結論（多半有原始碼行號）。
7. **replication-lag、role-detection**：照卡片。注意自帶時間戳的 endpoint（例如 YugabyteDB），停掉的節點舊值會留在查詢裡幾分鐘，計數 leader 的規則會誤判。

### 兩種 model 共同

然後看卡片裡標準問題以外的訊號（依 `applies_when` 篩選）、`silent_failures` 裡和使用者版本相符的項目（卡片的 `versions_checked` 與使用者的版本不同時，標「未在此版本驗證」），以及 `blind_spots`（目前無法監控的範圍；有 `covered_by` 的寫明要用哪個工具與設定補）。上游或社群規則要直接套用前，先看卡片 `upstream.note`：多張卡都量到官方規則 metric 名稱錯、label 不存在、或以 leader 為前提而永遠不響。

## job 名稱

上游規則與 alert-trust 範本都依 job 名稱比對；名稱不符時規則照常載入、永遠回傳空。用 `targets.json` 的實際 job 名稱對照 env 檔的 `targets[].job` 與所有 `rules.json` 裡的 selector。`check_coverage.py --live` 回報 `rule_no_data` 時，先查這個。

## 訪談（最多 3 題一組，已知的跳過）

1. 這個資料庫的拓撲？（replication、Galera、replica set、叢集角色與數量、Keeper／PD／master；決定 `applies_when` 的訊號與 component-count 的預期數量）
2. Exporter 型：exporter 帳號有哪些權限？請使用者自己執行唯讀查詢（例如 `SHOW GRANTS`、`pg_roles`、MongoDB 的 `db.getUser()`），遮蔽後貼回。Native 型：有沒有 blackbox 在探測健康端點、探測的是哪個端點？
3. Exporter 的期限相關 flag，或 native endpoint 的開關與設定（例如 ClickHouse `<prometheus>`、mongodb_exporter 的 collector 參數）有沒有改過？（卡片寫了是哪個）

## 回報

每個 target 一張表：

| 問題（target-pattern id） | 卡片訊號 | 狀態（covered／self_only／rule_no_data／missing／not_applicable／needs_input） | 依據 | 建議 |

缺口附卡片 `rules:` 指到的範本，提醒換掉佔位符 job 名稱與門檻，並用 `promtool check rules` 驗證。結果交回 `selfcheck`。
