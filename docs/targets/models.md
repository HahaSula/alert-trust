# 資料庫的監控模型

這頁回答：每個資料庫有哪幾種監控方式、官方推薦哪一種、官方或準官方的告警規則在哪裡（釘在版本），以及 alert-trust 的 target 卡該以哪一種為基礎、哪一種補盲點。

資料來源是各專案的官方文件與 repo 檔（2026-11-09 核對）。這頁只讀碼與讀文件，沒有做實驗；標「未確認」的是沒有找到一手來源的說法。

## 先講結論

1. **資料庫分成兩類。**MySQL、MariaDB、PostgreSQL、Valkey／Redis、MongoDB 沒有可直接抓的 Prometheus 端點，要靠外部 exporter；etcd、TiDB（TiKV、PD）、YugabyteDB、ClickHouse 本身就有 `/metrics` 類端點，不需要 exporter。[^dbm-etcd-listen-metrics][^dbm-tidb-status-metrics][^dbm-yb-prom-endpoint][^dbm-ch-prom-commented]
2. **原生端點的卡片沒有「exporter」這一層。**`up == 0` 同時代表「資料庫掛了」和「抓不到」，分不開；exporter 那種「連得上 exporter 但連不上資料庫」的訊號（`mysql_up`、`pg_up`、`redis_up`）不存在。卡片的 exporter 面向要改成「抓取本身」：`up`、`scrape_duration_seconds`、回應大小。
3. **ClickHouse 的端點預設是關的。**預設 `config.xml` 的 `<prometheus>` 區塊整段被註解，要自己打開；而且端點只輸出 `system.metrics`、`system.events`、`system.asynchronous_metrics`、`system.errors` 這類全域數字，每張表的 parts、複寫佇列、mutation 要另外用 SQL 查。[^dbm-ch-prom-commented][^dbm-doc-ch-prometheus]
4. **MongoDB 8.3 有 OTel metrics，但不能取代 exporter。**8.3 加了把 OTel metrics 寫成 Prometheus 格式檔案（或用 HTTP 推送）的啟動參數，登錄的 metric 只有連線、index build 等十來個；8.0（LTS）與 8.2 的 tag 裡沒有這段程式碼。基礎仍是 Percona mongodb_exporter。[^dbm-mongo-otel-prom][^dbm-mongo-otel-names]
5. **廠商的整套監控都不監控自己的 Prometheus 與 Alertmanager，或只在同一套裡面監控。**TiUP 部署的規則檔裡沒有任何 `prometheus_*`、`alertmanager_*` 或 Watchdog 規則；blackbox 自己是否存活由 blackbox 自己探測。YugabyteDB Anywhere 與 PMM 有「告警查詢失敗」「agent 掛了」之類的平台告警，但評估它們的就是同一套平台。[^dbm-tiup-rule-files][^dbm-tidb-blacker-blackbox][^dbm-yb-alert-query-failed][^dbm-pmm-agent-down]所以 alert-trust 的監控鏈卡片與外部觀察者，在每一種模型下都還是需要。
6. **官方規則有可以直接指出的問題。**TiKV 的 apply worker CPU 規則用 `name="apply_.*"`（`=` 不是 `=~`），字面值不會比對到任何 thread，這條規則永遠不會觸發（[TiDB lab](tidb.md#model) 實測：對到 0 條）；TiDB、PD 的規則把 `job="tidb"`、`job="pd"` 寫死，job 名稱不同的環境會「規則在但沒資料」。[^dbm-tikv-rules-apply-matcher][^dbm-tidb-rules-keepalive]

## 總表 {#matrix}

模型：(a) 外部 exporter 查詢資料庫；(b) 資料庫內建的 Prometheus／OpenMetrics 端點；(c) agent／collector（OpenTelemetry Collector receiver、PMM client）；(d) 資料庫內的歷史與診斷資料；(e) 廠商提供的整套監控。

| 資料庫 | (a) exporter | (b) 原生端點 | (c) agent／collector | (d) 資料庫內歷史與診斷 | (e) 廠商整套 |
|---|---|---|---|---|---|
| MySQL | mysqld_exporter、query-exporter | — | OTel mysqlreceiver（beta）、PMM client | performance_schema、sys | PMM |
| MariaDB | mysqld_exporter、query-exporter | — | OTel mysqlreceiver（MariaDB 支援度未確認）、PMM client | performance_schema（預設關）、information_schema | PMM |
| PostgreSQL | postgres_exporter、query-exporter | — | OTel postgresqlreceiver（beta）、PMM client | pg_stat_*、pg_stat_statements | PMM、pgwatch |
| Valkey／Redis | redis_exporter | — | OTel redisreceiver（beta） | INFO、LATENCY、SLOWLOG／COMMANDLOG | PMM（有 valkey_down 範本） |
| etcd | — | `/metrics`、`/health`、`/livez`、`/readyz` | — | —（只有 debug 模式的 `/debug/pprof`） | —（只有 mixin 規則） |
| TiDB／TiKV／PD | — | 各元件 status port 的 `/metrics` | — | statements_summary、slow query、Top SQL、Continuous Profiling | TiUP（Prometheus＋Grafana＋Alertmanager＋blackbox＋node_exporter） |
| YugabyteDB | postgres_exporter 對 YSQL 要關掉 stat_replication 與 settings，多數儲存類數字是 0（[實測](yugabytedb.md#postgres)） | 7000／9000／13000／12000 的 `/prometheus-metrics` | YBA 的 OTel 匯出（有告警範本） | pg_stat_statements、Active Session History | YugabyteDB Anywhere、Aeon |
| ClickHouse | Altinity operator 內的 metrics-exporter | `<prometheus>` 的 `/metrics`（預設關） | — | system.metrics／events、`system.*_log`、metric_log | ClickHouse Cloud（Cloud API 的 Prometheus 端點） |
| MongoDB | Percona mongodb_exporter | 8.3 起有 OTel 檔案／推送（metric 很少） | OTel mongodbreceiver（beta）、PMM client | serverStatus、FTDC（diagnostic.data）、profiler、currentOp | Ops Manager、Cloud Manager、Atlas、PMM |

OTel receiver 的成熟度見 metadata.yaml。[^dbm-otel-mysql][^dbm-otel-postgresql][^dbm-otel-redis][^dbm-otel-mongodb]這頁只把 agent 與廠商平台當成模型列出，不比較產品。

## 各模型的取捨 {#tradeoffs}

| 模型 | 別的模型沒有的 | 成本與風險 | 對 monitor of monitor 的影響 |
|---|---|---|---|
| (a) exporter | 可自訂查詢（SQL、命令），能把資料庫沒有輸出成 metric 的狀態變成數字 | 佔連線：連線打滿時 exporter 也連不進去，連線數 metric 整組消失；單執行緒的 Valkey／Redis 上，SCAN 類收集會拖慢正式流量 | 多一個行程；但有 `*_up` 可以分開「exporter 活著、資料庫掛了」 |
| (b) 原生端點 | 不用多部署；內部 histogram（Raft、fsync、TSO）只有原生端點有 | 回應大小隨表、tablet、region 數成長；YugabyteDB 提供 `max_metric_entries` 與 allowlist 控制 | `up` 分不開資料庫掛了與抓不到；沒有 exporter 層可以監控 |
| (c) agent／collector | 一個 agent 收多種來源，可推送 | metric 名稱與 exporter 不同，規則不能共用；receiver 多為 beta | agent 本身變成新的單點，要有自己的心跳 |
| (d) 資料庫內歷史 | 告警之後回頭查「是哪一條 SQL」「當時在等什麼」 | 多數預設關或有保留上限（例如 Valkey 的 latency monitor 預設關、MongoDB FTDC 預設 250 MB 循環覆寫，shard server 加倍為 500 MB[^mg-ftdc-dir-size][^mg-ftdc-shard-double]） | 不是告警來源；匯出時要先彙總，避免高基數 |
| (e) 廠商整套 | 開箱即用的儀表板與規則，跟版本一起更新 | 綁定版本與部署方式；授權不同（例如 YugabyteDB Anywhere 是 Polyform Free Trial、PMM 是 AGPL-3.0） | 只在同一套裡面自我監控，或完全不監控自己 |

連線與單執行緒的風險：mysqld_exporter 連線打滿的實測與保留連線見[MySQL 頁](mysql.md)[^mysql-conn-reserved]；redis_exporter README 寫明大量 `--check-keys` 會讓 scrape 跑不完，並提醒「Redis 是單執行緒，SCAN COUNT 太大會影響正式環境」。[^dbm-redis-exp-checkkeys][^dbm-redis-exp-batch]

## MySQL／MariaDB {#mysql}

| 模型 | 工具 | 補什麼 | 成本 |
|---|---|---|---|
| (a) | mysqld_exporter v0.21.0、query-exporter 5.2.2 | 已做成元件卡（[MySQL](../components/mysql.md)、[MariaDB](../components/mariadb.md)）；自訂查詢補 errant GTID、MDL 等盲點 | 佔一條連線；預設只開 6 個 collector[^my-default-collectors] |
| (c) | OTel mysqlreceiver（metrics beta） | — | metric 名稱不同 |
| (d) | performance_schema、sys | 語句 digest、等待事件[^dbm-doc-mysql-pfs] | MariaDB 預設關 |
| (e) | PMM 3 | QAN 查詢分析；內建 mysql_down、複寫 IO／SQL thread 等範本[^dbm-rules-pmm] | 一套獨立平台 |

**官方規則：**mysqld-mixin（v0.21.0）的 general.yaml 只有一條 `MySQLDown`；複寫與 Galera 規則在 galera.yaml。[^dbm-mysqld-mixin-down][^dbm-mysqld-mixin-repl]覆蓋遠少於我們的[九個面向](mysql.md#aspects)。

**alert-trust：**維持 (a)。exporter 是唯一能自訂查詢、又能分開 exporter 與資料庫狀態的模型。(d) 用來在告警後查原因；performance_schema 的彙總值經 query-exporter 輸出（已做）。

## PostgreSQL {#postgres}

| 模型 | 工具 | 補什麼 | 成本 |
|---|---|---|---|
| (a) | postgres_exporter v0.20.1、query-exporter 5.2.2 | 已做成[元件卡](../components/postgres_exporter.md) | 佔一條連線；保留連線只給 superuser 等角色[^pg-src-reserved] |
| (c) | OTel postgresqlreceiver（metrics beta） | — | metric 名稱不同 |
| (d) | pg_stat_*、pg_stat_statements | 累積統計與語句彙總[^dbm-doc-pg-stats][^dbm-doc-pg-pgss] | pg_stat_statements 要 `shared_preload_libraries` |
| (e) | PMM 3、pgwatch | PMM 有 bloat、未 autovacuum、未使用的 replication slot 等範本[^dbm-rules-pmm]；pgwatch 用 SQL 收集存到自己的庫[^dbm-doc-pgwatch] | 一套獨立平台 |

**官方規則：**postgres_mixin（v0.20.1）有 15 條，包含 `PostgreSQLDown` 與 `PostgresHasExporterErrors`。[^dbm-pg-mixin-down][^dbm-pg-mixin-exporter-errors]

**alert-trust：**維持 (a)，理由同 MySQL。

## Valkey／Redis {#valkey}

| 模型 | 工具 | 補什麼 | 成本 |
|---|---|---|---|
| (a) | redis_exporter v1.97.0 | INFO 全部欄位、複寫、cluster slot；支援 Valkey 7–9 | `--check-keys`、`--count-keys` 走 SCAN，大庫會拖慢 scrape 與正式流量[^dbm-redis-exp-checkkeys][^dbm-redis-exp-batch] |
| (c) | OTel redisreceiver（metrics beta） | — | 只讀 INFO |
| (d) | LATENCY、SLOWLOG／COMMANDLOG | 哪一類事件（fork、AOF fsync、expire）變慢；慢命令 | latency monitor 預設關（`latency-monitor-threshold 0`）；Valkey 用 commandlog 設定取代 slowlog 設定[^dbm-valkey-latency-default][^dbm-valkey-commandlog] |
| (e) | PMM 3 | redis_down、valkey_down 範本 | — |

**官方建議：**Valkey 文件沒有 Prometheus 或告警建議，只說明 LATENCY 與 INFO。[^dbm-doc-valkey-latency][^dbm-doc-valkey-info]準官方規則是 redis_exporter 附的 redis-mixin：6 條（RedisDown、記憶體 > 90%、連線數、cluster slot fail／pfail、cluster state），都是 `for: 5m`。[^dbm-redis-mixin-down]

**alert-trust：**以 (a) redis_exporter 為基礎（另一個 agent 正在做卡片）。補盲點：打開 latency monitor，由 exporter 的 latency 收集輸出；不要開大範圍的 `--check-keys`。

## etcd {#etcd}

| 模型 | 工具 | 補什麼 | 成本 |
|---|---|---|---|
| (b) | `/metrics`（client port，或 `--listen-metrics-urls` 指定的獨立 listener） | Raft、WAL fsync、backend commit、DB 大小與 quota | `--metrics` 預設 basic；`extensive` 才有 server 端 gRPC histogram，序列變多[^dbm-etcd-metrics-level] |
| (b) | `/health`、`/livez`、`/readyz` | 給 blackbox 探測；`--listen-metrics-urls` 的 listener 不需要 client mTLS | — |
| (d) | — | etcd 沒有查詢歷史；`/debug/pprof` 只在 debug log level 開，官方警告會降低效能 | — |

**官方建議：**文件說用 Prometheus 抓 `/metrics`、用 Grafana，告警用 repo 裡的 `contrib/mixin`，並要求調整 job label、每個 cluster 用唯一 label。[^dbm-doc-etcd-monitoring]

**官方規則（etcd v3.7.3 mixin）：**

| 規則 | 條件 | 等級 |
|---|---|---|
| etcdInsufficientMembers | `up` 為 1 的成員少於過半[^dbm-etcd-mixin-insufficient] | critical |
| etcdNoLeader | `etcd_server_has_leader == 0` for 1m[^dbm-etcd-mixin-noleader] | critical |
| etcdHighFsyncDurations | WAL fsync p99 > 0.5s（warning）、> 1s（critical）[^dbm-etcd-mixin-fsync] | warning／critical |
| etcdDatabaseQuotaLowSpace | DB 大小 / quota > 95%[^dbm-etcd-mixin-quota] | critical |

另有 leader 變動次數、gRPC 失敗與延遲、成員通訊、提案失敗、commit 延遲、DB 成長預測、碎片率。

**alert-trust：**以 (b) 為基礎，不需要 exporter。補盲點：用 `--listen-metrics-urls` 把 scrape 與 client 流量分開；外部 blackbox 探 `/readyz`。注意 etcdInsufficientMembers 用 `up` 算成員數，Prometheus 抓不到（網路分割在監控端）也會觸發，要和 `etcd_server_has_leader` 一起看。

## TiDB（TiKV、PD） {#tidb}

| 模型 | 工具 | 補什麼 | 成本 |
|---|---|---|---|
| (b) | TiDB、TiKV、PD、TiFlash 的 status port `/metrics` | 各元件內部 histogram；PD 內嵌 etcd 的 `etcd_*` metric | TiKV 的序列多；未量測 |
| (d) | statements_summary、slow query、Top SQL、Continuous Profiling | 語句彙總、每條 SQL 的 CPU、持續 profile[^dbm-doc-tidb-stmt-summary][^dbm-doc-tidb-topsql][^dbm-doc-tidb-profiling] | Top SQL 與 profiling 需要 ng-monitoring 元件 |
| (e) | TiUP 部署的 Prometheus＋Grafana＋Alertmanager＋blackbox＋node_exporter | 規則、儀表板、port probe 一次裝好[^dbm-doc-tidb-deploy-monitoring] | 綁 TiUP 與版本 |

**官方建議：**TiDB 文件把 Prometheus＋Grafana＋Alertmanager 當成內建的監控框架，另有 PD 內建的 TiDB Dashboard。[^dbm-doc-tidb-framework]告警分 emergency、critical、warning 三級。[^dbm-doc-tidb-alert-rules]

**官方規則（v8.5.8）：**規則跟著原始碼走，每個版本一份。

| 檔案 | 例子 |
|---|---|
| pingcap/tidb `pkg/metrics/alertmanager/tidb.rules.yml`[^dbm-rules-tidb] | `TiDB_query_duration`：p99 > 1s[^dbm-tidb-rules-query]；`TiDB_monitor_keep_alive`：TiDB 內部心跳計數 10 分鐘內少於 100[^dbm-tidb-rules-keepalive] |
| tikv/tikv `metrics/alertmanager/tikv.rules.yml`[^dbm-rules-tikv] | `TiKV_write_stall`[^dbm-tikv-rules-stall]；raft log lag、apply／append 延遲 |
| tikv/pd `metrics/alertmanager/pd.rules.yml`[^dbm-rules-pd] | `PD_cluster_down_tikv_nums`：store down 數 > 0，且只在 `etcd_server_is_leader` 的 PD 上算[^dbm-pd-rules-store-down]；內嵌 etcd 的 WAL fsync p99 > 1s[^dbm-pd-rules-fsync] |
| pingcap/monitoring `platform-monitoring/ansible/rule/`（node、blacker、bypass）[^dbm-rules-tidb-platform] | blackbox port probe：TiDB、TiKV、PD、node_exporter、blackbox、Grafana、Pushgateway |

**和我們的做法不同或要注意：**

- TiUP 載入的規則檔（node、blacker、bypass 加各元件）沒有 Prometheus 或 Alertmanager 的規則，也沒有 Watchdog；`monitor_port_probe` 只探 Pushgateway、Grafana、node_exporter、blackbox。[^dbm-tiup-rule-files][^dbm-tiup-monitor-probe]文件的告警頁也沒有這兩個元件。[^dbm-doc-tidb-alert-rules]
- blackbox 掛了的告警（`Blackbox_exporter_server_is_down`）由 blackbox 自己探測，blackbox 停了時這條也拿不到資料。[^dbm-tidb-blacker-blackbox]
- PD 的叢集規則都加了 `and etcd_server_is_leader > 0`：PD leader 那台抓不到時，store down、region 缺副本等規則全部回傳空。[^dbm-pd-rules-store-down]
- `TiDB_monitor_keep_alive` 用的 `tidb_monitor_keep_alive_total` 在 v8.5.8 已經不存在（只剩 metrics_schema 的定義），這條規則永遠回傳空；見 [TiDB 頁](tidb.md#model)。[^dbm-tidb-rules-keepalive][^tidb-keepalive-infoschema]

**alert-trust：**以 (b) 為基礎，規則直接取上游 rules.yml（釘版本），改掉寫死的 job selector 與 apply matcher。補盲點：監控鏈卡片（Prometheus、Alertmanager、Watchdog、外部觀察者）補 TiUP 沒做的自我監控；SQL 層的 statements_summary 可用 query-exporter 走 MySQL 協定彙總輸出（未實測）。

## YugabyteDB {#yugabytedb}

| 模型 | 工具 | 補什麼 | 成本 |
|---|---|---|---|
| (b) | yb-master 7000、yb-tserver 9000、YSQL 13000、YCQL 12000 的 `/prometheus-metrics` | server 與 table 層級；tablet 層級不輸出 | 可用 `priority_regex`、allowlist、`max_metric_entries` 限制回應大小[^dbm-doc-yb-metrics][^dbm-yb-max-entries] |
| (d) | pg_stat_statements、Active Session History、cluster-wide views | YSQL 語句、等待事件取樣[^dbm-doc-yb-ash] | — |
| (e) | YugabyteDB Anywhere（YBA）、Aeon | 自帶 Prometheus 與約 100 個告警範本[^dbm-rules-yba] | YBA（`managed/`）是 Polyform Free Trial 授權，核心是 Apache 2.0[^dbm-yb-license] |

**官方建議：**文件以 `/prometheus-metrics` 搭配 Prometheus 為自建方式，YBA 與 Aeon 另有監控頁。[^dbm-doc-yb-monitor][^dbm-doc-yb-prom][^dbm-doc-yba-alert]

**YBA 告警範本（v2026.1.3.0）的例子：**`LEADERLESS_TABLETS`（`yb_node_leaderless_tablet_count` 持續大於 0）、`CLOCK_SKEW`（`hybrid_clock_skew`，門檻由參數決定）。[^dbm-yb-leaderless][^dbm-yb-clock-skew]平台自己的告警有 `ALERT_QUERY_FAILED`、`UNIVERSE_METRIC_COLLECTION_FAILURE` 等，用 `last_over_time(...[1d]) < 1` 判斷。[^dbm-yb-alert-query-failed][^dbm-yb-metric-collection]這些平台 metric 由 YBA 寫出、由 YBA 自己的 Prometheus 評估；YBA 或它的 Prometheus 停了時是否還有人通知，未確認（推論是沒有）。

**alert-trust：**以 (b) 為基礎，四個 port 各一個 job（見 [YugabyteDB 怎麼監控](yugabytedb.md)）。YBA 範本當作門檻與訊號清單的參考（leaderless tablet、under-replicated、clock skew 是分散式 SQL 特有的）。補盲點：ASH 與 pg_stat_statements 經自訂查詢彙總。

## ClickHouse {#clickhouse}

| 模型 | 工具 | 補什麼 | 成本 |
|---|---|---|---|
| (b) | `<prometheus>` 的 `/metrics`（範例 port 9363） | system.metrics、events、asynchronous_metrics、errors | 預設註解掉，要自己打開[^dbm-ch-prom-commented][^dbm-doc-ch-prometheus] |
| (b) | `/ping`、`/replicas_status` | 存活；replica 延遲超過 `max_replica_delay_for_distributed_queries` 時回 503 | — |
| (a) | Altinity clickhouse-operator 的 metrics-exporter | 查 system 表輸出 `chi_clickhouse_*`，含 parts、複寫延遲 | 只在 operator 部署下；社群維護 |
| (d) | `system.query_log`、`part_log`、`metric_log`、`asynchronous_metric_log`、`error_log` | 每秒的歷史 metric、查詢紀錄；`metric_log` 預設開[^dbm-ch-metric-log][^dbm-doc-ch-metric-log] | 佔磁碟；自己就是 MergeTree 表 |
| (e) | ClickHouse Cloud | Cloud API 提供 Prometheus 端點[^dbm-doc-ch-cloud-prom] | 只適用 Cloud |

**官方建議：**監控頁列出硬體、server metric、server event 三類，健康檢查用 `/ping` 與 `/replicas_status`；沒有告警規則或門檻。[^dbm-doc-ch-monitoring]ClickHouse 官方 repo 沒有 mixin 或 rules 檔（v26.10.2.103-stable 的檔案樹裡沒有）。

**準官方規則：**Altinity clickhouse-operator（release-0.27.4，Apache 2.0）的規則最完整，例如 `ClickHouseMaxPartCountForPartition`（> 100）、`ClickHouseReplicasMaxAbsoluteDelay`（> 300 秒），也有 `ClickHouseMetricsExporterDown` 這條自檢。[^dbm-altinity-parts][^dbm-altinity-replica-delay][^dbm-altinity-exporter-down]這是社群（廠商）規則，不是 ClickHouse 專案的。

**alert-trust：**以 (b) 為基礎（要求打開 `<prometheus>`）。補盲點：每張表的 parts、detached parts、mutation、複寫佇列不在端點裡，要用 SQL 查 system 表後彙總輸出；`/replicas_status` 交給 blackbox。

## MongoDB {#mongodb}

| 模型 | 工具 | 補什麼 | 成本 |
|---|---|---|---|
| (a) | Percona mongodb_exporter v0.54.0 | serverStatus、replSetGetStatus、getDiagnosticData、collStats、indexStats；支援 6.0 以上[^dbm-mongodb-exporter-versions] | 帳號要 `clusterMonitor` 與 local 的 `read`[^dbm-mongodb-exporter-roles]；collStats／indexStats 依集合數成長（README 沒寫成本，未確認） |
| (b) | 8.3 的 OTel metrics（Prometheus 格式檔案或 HTTP 推送） | 十來個連線與 index build metric | 只在 8.3；不是 scrape 端點；匯出連續失敗達上限時 mongod 會結束[^dbm-mongo-otel-prom][^dbm-mongo-otel-names] |
| (c) | OTel mongodbreceiver（metrics beta） | — | metric 名稱不同 |
| (d) | serverStatus、FTDC（`diagnostic.data`）、profiler、currentOp | FTDC 每秒記錄 serverStatus 等，預設 500 MB 循環覆寫 | FTDC 格式不公開，是給 MongoDB 工程師用的[^dbm-doc-mongo-ftdc] |
| (e) | Ops Manager、Cloud Manager、Atlas、PMM | 告警與自動化[^dbm-doc-opsmgr-alerts] | Ops Manager 屬 Enterprise Advanced；MongoDB 本身是 SSPL[^dbm-mongo-license] |

**官方建議：**自建部署的監控頁列出 mongostat、mongotop、serverStatus、replSetGetStatus 與 Ops Manager／Cloud Manager／Atlas，沒有提到 Prometheus。[^dbm-doc-mongo-monitoring]

**Atlas 建議的告警門檻（低／高優先）：**[^dbm-doc-atlas-alerts]

| 條件 | 低優先 | 高優先 |
|---|---|---|
| Oplog window | < 24h 持續 5 分鐘 | < 1h 持續 10 分鐘 |
| Replication lag | > 240 秒持續 5 分鐘 | > 1 小時持續 5 分鐘 |
| No primary | 5 分鐘 | 5 分鐘 |
| Host down | 15 分鐘 | 24 小時 |
| 磁碟分割區使用率 | > 90% | > 95% 持續 5 分鐘 |
| 選舉事件 | 1 小時內 > 3 | 1 小時內 > 30 |

**alert-trust：**以 (a) mongodb_exporter 為基礎。Atlas 的門檻當作起點（尤其 oplog window，對應我們的容量與複寫面向）。FTDC 不是告警來源，但要確認 `diagnostic.data` 放在持久磁碟，事件後才拿得到。

## 跨資料庫的結論 {#conclusions}

| 資料庫 | 卡片的基礎模型 | 理由 | 補盲點 |
|---|---|---|---|
| MySQL／MariaDB | (a) mysqld_exporter | 沒有原生端點；可自訂查詢 | query-exporter、(d) performance_schema |
| PostgreSQL | (a) postgres_exporter | 同上 | query-exporter、(d) pg_stat_statements |
| Valkey／Redis | (a) redis_exporter | 沒有原生端點 | (d) LATENCY、COMMANDLOG |
| etcd | (b) `/metrics` | 原生端點完整，官方 mixin 依它寫 | blackbox 探 `/readyz` |
| TiDB | (b) status port | 官方規則依它寫，跟版本走 | 監控鏈卡片補 Prometheus／Alertmanager 自檢；statements_summary |
| YugabyteDB | (b) `/prometheus-metrics` | 原生端點；YBA 範本可當訊號清單 | ASH、pg_stat_statements |
| ClickHouse | (b) `<prometheus>`（要打開） | 原生端點有全域數字 | SQL 查 system 表（parts、複寫、mutation）；`/replicas_status` |
| MongoDB | (a) mongodb_exporter | 8.0／8.2 沒有原生端點，8.3 的 OTel 太少 | Atlas 門檻；FTDC 保留 |

對 alert-trust 計畫的影響（提議，未改 `catalog/target-pattern.yaml`）：

1. **target 標準問題要分兩種。**原生端點的卡片沒有 exporter 程序，`exporter-query-timeout` 這類問題不適用；改問「scrape 耗時與回應大小」「`up` 分不開資料庫掛了與抓不到時，用什麼第二訊號區分」（例如 blackbox 探 `/readyz`、`/ping`）。
2. **共識類資料庫（etcd、PD、YugabyteDB master）的 cluster 面向，官方規則都用「leader 是否存在」與「成員過半」。**[^dbm-etcd-mixin-noleader][^dbm-pd-rules-store-down][^dbm-yb-leaderless]用 `up` 算成員數會把監控端的網路問題當成叢集問題。
3. **廠商整套不能取代監控鏈卡片。**TiUP 完全沒有 Prometheus／Alertmanager 規則，YBA 與 PMM 只在自己裡面自檢。[^dbm-tiup-rule-files][^dbm-yb-alert-query-failed][^dbm-pmm-vm-down]
4. **上游規則要釘版本後再用，並跑我們的 lint。**TiKV apply matcher 與寫死的 job selector 是 policy lint 能抓到的類型。[^dbm-tikv-rules-apply-matcher]

[^dbm-doc-etcd-monitoring]: 文件：[Monitoring etcd](https://etcd.io/docs/v3.6/op-guide/monitoring/)
[^dbm-doc-tidb-framework]: 文件：[TiDB Monitoring Framework Overview](https://docs.pingcap.com/tidb/stable/tidb-monitoring-framework/)
[^dbm-doc-tidb-alert-rules]: 文件：[TiDB Cluster Alert Rules](https://docs.pingcap.com/tidb/stable/alert-rules/)
[^dbm-doc-tidb-deploy-monitoring]: 文件：[TiDB Deploy Monitoring Services](https://docs.pingcap.com/tidb/stable/deploy-monitoring-services/)
[^dbm-doc-tidb-stmt-summary]: 文件：[TiDB Statement Summary Tables](https://docs.pingcap.com/tidb/stable/statement-summary-tables/)
[^dbm-doc-tidb-topsql]: 文件：[TiDB Top SQL](https://docs.pingcap.com/tidb/stable/top-sql/)
[^dbm-doc-tidb-profiling]: 文件：[TiDB Dashboard Instance Profiling](https://docs.pingcap.com/tidb/stable/dashboard-profiling/)
[^dbm-doc-ch-monitoring]: 文件：[ClickHouse Monitoring](https://clickhouse.com/docs/operations/monitoring)
[^dbm-doc-ch-prometheus]: 文件：[ClickHouse Prometheus protocols](https://clickhouse.com/docs/interfaces/prometheus)
[^dbm-doc-ch-metric-log]: 文件：[ClickHouse system.metric_log](https://clickhouse.com/docs/operations/system-tables/metric_log)
[^dbm-doc-ch-cloud-prom]: 文件：[ClickHouse Cloud Prometheus endpoint](https://clickhouse.com/docs/products/cloud/features/monitoring/prometheus)
[^dbm-doc-yb-monitor]: 文件：[Monitor YugabyteDB](https://docs.yugabyte.com/stable/launch-and-manage/monitor-and-alert/)
[^dbm-doc-yb-metrics]: 文件：[YugabyteDB Metrics](https://docs.yugabyte.com/stable/launch-and-manage/monitor-and-alert/metrics/)
[^dbm-doc-yb-prom]: 文件：[YugabyteDB Prometheus integration](https://docs.yugabyte.com/stable/explore/observability/prometheus-integration/)
[^dbm-doc-yb-ash]: 文件：[YugabyteDB Active Session History](https://docs.yugabyte.com/stable/launch-and-manage/monitor-and-alert/active-session-history-monitor/)
[^dbm-doc-yba-alert]: 文件：[YugabyteDB Anywhere alerts](https://docs.yugabyte.com/stable/yugabyte-platform/alerts-monitoring/alert/)
[^dbm-doc-mongo-monitoring]: 文件：[Monitoring a Self-Managed MongoDB Deployment](https://www.mongodb.com/docs/manual/administration/monitoring/)
[^dbm-doc-mongo-ftdc]: 文件：[MongoDB Full Time Diagnostic Data Capture](https://www.mongodb.com/docs/manual/administration/full-time-diagnostic-data-capture/)
[^dbm-doc-atlas-alerts]: 文件：[Guidance for Atlas Monitoring and Alerts](https://www.mongodb.com/docs/atlas/architecture/current/monitoring-alerts/)
[^dbm-doc-opsmgr-alerts]: 文件：[MongoDB Ops Manager Alerts](https://www.mongodb.com/docs/ops-manager/current/reference/alerts/)
[^dbm-doc-valkey-latency]: 文件：[Valkey Latency monitoring](https://valkey.io/topics/latency-monitor/)
[^dbm-doc-valkey-info]: 文件：[Valkey INFO](https://valkey.io/commands/info/)
[^dbm-doc-mysql-pfs]: 文件：[MySQL 8.4 Performance Schema](https://dev.mysql.com/doc/refman/8.4/en/performance-schema.html)
[^dbm-doc-pg-stats]: 文件：[PostgreSQL 17 The Cumulative Statistics System](https://www.postgresql.org/docs/17/monitoring-stats.html)
[^dbm-doc-pg-pgss]: 文件：[PostgreSQL 17 pg_stat_statements](https://www.postgresql.org/docs/17/pgstatstatements.html)
[^dbm-doc-pgwatch]: 文件：[pgwatch](https://pgwat.ch/latest/)
[^dbm-rules-tidb]: 上游規則：[pingcap/tidb v8.5.8 tidb.rules.yml](https://github.com/pingcap/tidb/blob/v8.5.8/pkg/metrics/alertmanager/tidb.rules.yml)
[^dbm-rules-tikv]: 上游規則：[tikv/tikv v8.5.8 tikv.rules.yml](https://github.com/tikv/tikv/blob/v8.5.8/metrics/alertmanager/tikv.rules.yml)
[^dbm-rules-pd]: 上游規則：[tikv/pd v8.5.8 pd.rules.yml](https://github.com/tikv/pd/blob/v8.5.8/metrics/alertmanager/pd.rules.yml)
[^dbm-rules-tidb-platform]: 上游規則：[pingcap/monitoring v8.5.8 platform-monitoring/ansible/rule](https://github.com/pingcap/monitoring/tree/v8.5.8/platform-monitoring/ansible/rule)
[^dbm-rules-yba]: 上游規則：[YugabyteDB Anywhere v2026.1.3.0 alert_templates.yml](https://github.com/yugabyte/yugabyte-db/blob/v2026.1.3.0/managed/src/main/resources/alert/alert_templates.yml)
[^dbm-rules-pmm]: 上游規則：[PMM v3.10.0 alerting-templates](https://github.com/percona/pmm/tree/v3.10.0/managed/data/alerting-templates)
