# YugabyteDB 怎麼監控

**目標：知道 YugabyteDB 叢集現在能不能服務 YSQL 讀寫（master 有 leader、tablet 有過半副本、時鐘在範圍內、YSQL 還接受連線），以及哪一層、哪一台離不能還有多遠。**YugabyteDB 有兩種 process：yb-master（catalog、tablet 位置、balancer；masters 之間一組 Raft）與 yb-tserver（DocDB 儲存，每個 tablet 一組 Raft，YSQL 與 YCQL 也跑在裡面）。四個 port 各有 `/prometheus-metrics`，沒有 exporter；`/status` 一律回 `{}`，`up` 加上 `/status` 都不夠。這頁依九個面向從大到小排列：叢集 → 可用性 → 複寫 → 資源 → 工作量 → 維護 → 保護 → 容量，最前面是監測自檢。

依據是 YugabyteDB v2026.1.3.0 的原始碼（`gh api .../releases/latest` 回的最新穩定版，commit `f2635f5`；只讀 Apache-2.0 的核心，YugabyteDB Anywhere 的告警範本是 Polyform 授權，只引用名稱與行號），加上一組小實驗：master ×3、tserver ×3（RF=3），停 tserver、停兩台 tserver、停 master leader、停兩台 master、用完 YSQL 連線、postgres_exporter 對 YSQL<span class="at-chip at-ev-tested">實測</span>[^yb-exp]。這台機器的 Docker 容器時鐘比牆鐘慢約 30%，牆鐘還會被 WSL 往回調；秒數以主機 monotonic 時鐘量的 SQL、HTTP 耗時為準，Prometheus 與 YugabyteDB 自己報的秒數只當參考。訊號與規則在[元件卡](../components/yugabytedb.md)，原始碼位置在[附錄](yugabytedb-appendix.md)，機制在[分散式 SQL 架構](../architecture/db-distsql.md#yugabytedb)與[共識（Raft）架構](../architecture/db-raft.md#yugabytedb)。

## 先講結論

1. **master 失去 quorum 時，tserver 不會繼續服務 YSQL。**tserver 要持有 master leader 續約的 YSQL operation lease（`enable_ysql_operation_lease` 預設開，TTL 30 秒）；lab 停 2 台 master 約 30 秒後三台 tserver 同時結束 PostgreSQL：5433 拒絕連線、13000 的 `/prometheus-metrics` 消失，已經連著的 session 也被結束。tserver 的 9000 與 `/api/v1/health-check` 一直正常。<span class="at-chip at-ev-tested">實測</span>[^yb-exp-lease-log][^yb-ysql-lease-kill][^yb-ysql-lease-ttl]
2. **存活的 master 照常回應，而且 JSON API 回 200。**`up` 1、`/status` `{}`、`/api/v1/health-check` 200，但內容是 `Unable to locate leader master`；只有 `/api/v1/is-leader` 用狀態碼（503）。blackbox 要比對 body。<span class="at-chip at-ev-tested">實測</span>[^yb-exp-raw][^yb-m-h-redirect-nolist][^yb-m-h-isleader-503]
3. **每個 sample 都帶時間戳，Prometheus 預設不對它們做 staleness。**停掉的節點的最後值留在查詢裡 5 分鐘：停 master leader 後，舊 leader 的 `is_raft_leader`=1 與新 leader 同時存在，「兩個 leader」的規則誤判；牆鐘往回跳時約 14 分鐘丟掉 20,748 個 out-of-order sample。scrape 設定要加 `honor_timestamps: false`（沒有重跑驗證）。<span class="at-chip at-ev-tested">實測</span>[^yb-exp-raw][^yb-prom-track-ts-staleness][^yb-prom-honor-ts]
4. **時鐘超過 500ms 時 tserver 直接 crash。**預設 `fail_on_out_of_range_clock_skew=true`：lab 第一次啟動，牆鐘往回跳 7 秒，三台 tserver 在 20 秒內同時 FATAL。關掉 crash 後整段 run 有幾十行 skew ERROR，`hybrid_clock_skew` 卻一直是 0，所以時鐘要另外用 node_exporter 看。<span class="at-chip at-ev-tested">實測</span>[^yb-exp-clock][^yb-clk-fatal][^yb-clk-enable]
5. **叢集層的 metric 只有 master leader 在寫，tablet 健康沒有 metric。**`num_tablet_servers_live`／`dead` 在 follower 是 0；leaderless 與 under-replicated 只在 master 的 JSON API。YBA 範本用的 `yb_node_leaderless_tablet_count` 等是 YBA 平台產生的，自建 Prometheus 照抄會回傳空。<span class="at-chip at-ev-tested">實測</span>[^yb-exp-raw][^yb-m-bg-report][^yba-leaderless]
6. **master 的 dead 判定與 tablet 的 Raft 是兩個時間。**停 1 台 tserver：+75 秒 master 判定 dead（`tserver_unresponsive_timeout_ms` 60 秒）、health-check 列出副本在它上面的 tablet；`/api/v1/tablet-under-replication` 整段都是空的。再停 1 台：leaderless 要超過 120 秒才列出，期間讀取 19–21 秒逾時。<span class="at-chip at-ev-tested">實測</span>[^yb-exp-raw][^yb-m-ts-unresponsive][^yb-m-leaderless-delay]
7. **postgres_exporter 的預設設定會打掛 YSQL。**`stat_replication` 的查詢在 YSQL 上卡住不回，每次 scrape 多一條卡住的 backend，lab 50 秒內用光 12 條連線，連 ysqlsh 都進不去；`settings` 遇到 `pg_settings` 的重複列會輸出重複 series（探索時整個 `/metrics` 回 500，原始輸出沒有保存）。關掉這兩個之後其他 13 個 collector 成功，但很多數字是 0。見[和 PostgreSQL 的差異](#postgres)。<span class="at-chip at-ev-tested">實測</span>[^yb-exp-raw][^yb-exp-pgexp][^pgexp-statrepl-query]
8. **YSQL 連線用完時 TCP probe 仍成功。**看 `yb_ysqlserver_connection_over_limit_total` 的增加量。<span class="at-chip at-ev-tested">實測</span>[^yb-exp-raw][^yb-ysql-m-overlimit]

### 九個面向一覽 {#aspects}

| 面向 | 這頁的重點 | 元件卡的訊號 | 證據 |
|---|---|---|---|
| [監測自檢](#exporter) | 三種元件各自的三方交叉；時間戳與 staleness；job 名稱錯了要會響；metric 消失；scrape 預算 | endpoint-up、health-second-path、metric-families-present、scrape-budget、endpoint-blocking | <span class="at-chip at-ev-tested">實測</span> |
| [叢集](#cluster) | master leader；master quorum 與 YSQL lease；元件數；leader 變動；tablet 健康 | role-detection、master-quorum、component-count、leader-changes、tablet-health | <span class="at-chip at-ev-tested">實測</span> |
| [可用性](#availability) | tserver live／dead；YSQL 接不接受連線 | tserver-liveness、ysql-available | <span class="at-chip at-ev-tested">實測</span> |
| [複寫](#replication) | follower lag | replication-lag | <span class="at-chip at-ev-tested">實測</span> |
| [資源與飽和](#saturation) | 時鐘、記憶體、write stall、WAL fsync | clock-skew、memory、write-stall、wal-fsync | <span class="at-chip at-ev-tested">實測</span>／<span class="at-chip at-ev-code">讀碼</span> |
| [工作量](#workload) | YSQL 連線數、延遲、PostgreSQL 指標能沿用多少 | connections、query-latency、postgres-carryover | <span class="at-chip at-ev-tested">實測</span>／<span class="at-chip at-ev-code">讀碼</span> |
| [資料維護](#maintenance) | cluster balancer、tablet split | balancer | <span class="at-chip at-ev-tested">實測</span> |
| [資料保護](#protection) | 不在範圍；核心沒有備份 metric | （不適用） | <span class="at-chip at-ev-code">讀碼</span> |
| [容量](#capacity) | tablet 數 guardrail；磁碟要 node_exporter | capacity | <span class="at-chip at-ev-tested">實測</span> |

## 監控模型與官方建議 {#model}

YugabyteDB 屬於「資料庫內建端點」模型（見[資料庫監控模型](models.md#yugabytedb)）。

| 項目 | 內容 |
|---|---|
| 抓什麼 | master 7000、tserver 9000、YSQL 13000、YCQL 12000 的 `/prometheus-metrics`；URL 參數 `metrics=`、`priority_regex=`、`max_metric_entries=`、`version=v2`（table／server allowlist）[^yb-h-prom-register][^yb-h-prom-v2] |
| 健康端點 | `/status`（永遠 `{}`）；master 的 `/api/v1/is-leader`（leader 200、其他 503）、`/api/v1/health-check`、`/api/v1/tablet-replication`、`/api/v1/tablet-under-replication`（follower 轉送給 leader，失敗仍 200）；tserver 的 `/api/v1/health-check`（只看 FAILED tablet） |
| 官方建議 | 文件以 `/prometheus-metrics` 搭配 Prometheus 為自建方式；YugabyteDB Anywhere（YBA）自帶 Prometheus 與約 100 個告警範本（[Monitor YugabyteDB](https://docs.yugabyte.com/stable/launch-and-manage/monitor-and-alert/)） |
| 官方沒做的 | YBA 的範本由 YBA 自己的 Prometheus 評估，平台告警（`ALERT_QUERY_FAILED`、`UNIVERSE_METRIC_COLLECTION_FAILURE`）也是；YBA 或它的 Prometheus 停了時沒有外部觀察者（見 [models](models.md#yugabytedb)） |

**和 YBA 告警範本（v2026.1.3.0）的差異：**範本用 YBA relabel 後的 label（`universe_uuid`、`export_type`、`node_name`、`saved_name`），不能直接搬到自建 Prometheus。[^yba-memory]

| YBA 範本 | 用的 metric | 我們 | 差在哪 |
|---|---|---|---|
| LEADERLESS_TABLETS、UNDER_REPLICATED_TABLETS[^yba-leaderless][^yba-underrep] | `yb_node_leaderless_tablet_count`、`yb_node_underreplicated_tablet_count`（YBA 產生） | YBLeaderlessTablets、YBUnderReplicatedTablets、YBClusterHealthCheckDirty：blackbox 比對 master JSON API 的 body | 資料庫沒有這些 metric；範本的門檻參數名稱互相對調（leaderless 用 `underreplicated_tablets_secs_severe`） |
| MASTER_LEADER_MISSING[^yba-master-leader] | `yb_node_is_master_leader`（YBA 產生），for 300 秒 | YBMasterLeaderMetricsMissing（`is_raft_leader{sys.catalog}`）、YBMasterLeaderProbeMissing（`/api/v1/is-leader`），for 1 分鐘 | 資料庫有 `is_raft_leader`，不需要平台；要開 `honor_timestamps: false` |
| MASTER_UNDER_REPLICATED[^yba-master-underrep] | `ybp_universe_replication_factor`（平台） | YBMasterQuorumAtRisk、YBMasterFollowerHeartbeatDelay | 用 probe 與 master leader 的 `max_follower_heartbeat_delay` |
| DB_INSTANCE_DOWN[^yba-instance-down] | `up` 15 分鐘 < 1，且 YBA 認為 process 應該在跑 | YBEndpointDown（2 分鐘）＋每個 job 的 absent | 15 分鐘太久；job 名稱錯了時沒有範本會響 |
| CLOCK_SKEW[^dbm-yb-clock-skew] | `hybrid_clock_skew` 10 分鐘最大值 | YBClockSkew（250ms，例子） | 相同；lab 顯示瞬間的 skew 抓不到，另用 node_exporter |
| DB_COMPACTION_OVERLOAD、DB_MEMORY_OVERLOAD[^yba-compaction][^yba-memory] | `majority_sst_files_rejections`；記憶體壓力拒絕（`saved_name`） | YBWriteStall、YBMemoryPressureRejections | 拿掉 YBA 的 label；加上 `rocksdb_stall_micros` |
| HIGH_NUM_YSQL_CONNECTIONS[^yba-ysql-conn] | `yb_ysqlserver_connection_total` 對固定門檻 | YBYsqlConnectionsNearLimit（對上限的比例）、YBYsqlConnectionsRejected | 範本不看被拒的連線 |
| DB_YSQL_CONNECTION、DB_WRITE_READ_TEST_ERROR[^yba-ysql-connect] | YBA 健康檢查的 ysqlsh 連線與讀寫測試 | YBYsqlPortDown（tcp）；端到端 SQL probe 列為盲點 | 這是 YBA 才有的主動測試 |
| TABLET_PEERS_GUARDRAIL、TABLET_SERVER_AVG_WRITE_LATENCY[^yba-tablet-peers][^yba-write-latency] | `ts_live_tablet_peers`、Write RPC 延遲 | YBTabletPeersNearLimit、YBTServerWriteLatencyHigh | 相同門檻 |
| （平台）ALERT_QUERY_FAILED、HEALTH_CHECK_ERROR[^dbm-yb-alert-query-failed][^yba-health-check] | `ybp_*`，YBA 自己寫、自己評估 | 監控鏈卡片（Prometheus、Alertmanager、外部觀察者） | YBA 停了時誰通知，範本裡沒有答案 |
| （沒有） | — | 三方交叉、master quorum、YSQL lease 造成的 YSQL 全停、metric 消失、follower lag | — |

## 監測自檢 {#exporter}

### 三方交叉 {#health-second-path}

blackbox_exporter 從另一條路徑探測每台元件，probe job 把 `instance` relabel 成和 `/prometheus-metrics` target 相同的 `host:port`（設定見 `lab/targets/yugabytedb/prom/prometheus.yml`、`bb/blackbox.yml`）。

| 元件 | probe | 健康 metric | 為什麼 |
|---|---|---|---|
| master | `/api/v1/health-check`，body 要有 `"dead_nodes"`（拿得到 leader 的回應） | `is_raft_leader{table_name="sys.catalog"}` | `/status` 永遠 `{}`；JSON API 轉送失敗仍回 200[^yb-h-status][^yb-m-h-redirect-nolist] |
| tserver | `/api/v1/health-check`，`failed_tablets` 要空 | master leader 的 `num_tablet_servers_live` | tserver 的 health-check 不看 master[^yb-ts-h-health][^yb-m-num-live] |
| YSQL | tcp 5433 | `yb_ysqlserver_connection_over_limit_total` | 連線用完時 tcp 仍成功[^yb-ysql-m-overlimit] |

lab 的結果：<span class="at-chip at-ev-tested">實測</span>[^yb-exp-raw]

| 情境 | 對象 | `up` | probe | 健康 metric | 判讀 | 規則（firing 或 pending） |
|---|---|---|---|---|---|---|
| 正常 | 全部 | 1 | 1（is-leader 只有 leader） | leader `num_tablet_servers_live` 3、follower 0 | — | — |
| 停 tserver-3，+75 秒 | tserver-3 | 0 | 0 | live 2、dead 1 | process 停了 | YBEndpointDown、YBTServerHealthProbeFailing、YBYsqlPortDown、YBTServerDead、YBClusterHealthCheckDirty |
| 再停 tserver-2，+150 秒 | tserver-1 | 1 | 1 | live 1、dead 2；leaderless 列出 | tablet 沒有過半，讀取逾時 | YBTServerNotScraped；YBLeaderlessTablets、YBFollowerLagHigh pending |
| 停 master leader，+26 秒 | 舊 leader | 0 | 0 | 舊 leader 的 `is_raft_leader` 1 仍在 | 時間戳沒有 staleness | YBEndpointDown、YBMasterHealthProbeFailing pending；YBMasterLeaderCountWrong 誤判 pending |
| 停 2 台 master | 存活那台 | **1** | **0**（body 是錯誤訊息） | is-leader 全部 503 | master 沒有 quorum | YBMasterServingMetricsButNoLeader、YBMasterQuorumAtRisk、YBMasterHealthProbeFailing |
| 停 2 台 master | tserver ×3 | 9000 是 1；13000 是 **0** | tserver health-check 1；tcp **0** | lease 過期，PostgreSQL 結束 | YSQL 全停 | YBYsqlPortDown ×3 |
| YSQL 連線用完 | tserver-1 | 1 | tcp **1** | over_limit 1 分鐘 +7.3 | 新連線被拒 | YBYsqlConnectionsRejected；YBYsqlConnectionsNearLimit pending |

存活那台 master 的端點（主機時鐘）：`/prometheus-metrics` 200、1.8MB、0.03 秒；`/status` 200 `{}`；`/api/v1/is-leader` 503；`/api/v1/health-check` 200（1.5–3 秒，`<h2>Not found ...: Unable to locate leader master to redirect this request</h2>`）。<span class="at-chip at-ev-tested">實測</span>[^yb-exp-raw]

### 時間戳與 staleness {#timestamps}

YugabyteDB 的 `/prometheus-metrics` 在每個 sample 後面寫時間戳（毫秒）。Prometheus 預設 `honor_timestamps: true`、`track_timestamps_staleness: false`：帶時間戳的 series 在 target 停掉時不寫 staleness marker。[^yb-prom-track-ts-staleness][^yb-prom-honor-ts]

| 現象 | lab |
|---|---|
| 停掉的節點的值還在 | tserver-2 停 150 秒後 `ts_live_tablet_peers` 仍有它；停 master leader +26 秒，舊 leader `up` 0 但 `is_raft_leader` 1、`num_tablet_servers_live` 3 |
| 「兩個 leader」誤判 | YBMasterLeaderCountWrong pending |
| out-of-order | 牆鐘往回跳時約 14 分鐘 20,748 個 sample 被丟掉 |

所以四個 YugabyteDB job 都要設 `honor_timestamps: false`（或 `track_timestamps_staleness: true`）。用 `false` 重跑沒有做，能否同時避開 out-of-order 是推論。<span class="at-chip at-ev-tested">實測</span>（現象）[^yb-exp-raw]

### metric 消失、job 名稱、scrape 預算 {#exporter-scrape}

| 問題 | 做法 | 證據 |
|---|---|---|
| job 名稱錯了 | 每個 job 一條 `absent(up{job=...})`；YBA 範本沒有 | <span class="at-chip at-ev-code">讀碼</span> |
| 該有的 metric 還在嗎 | master `hybrid_clock_skew`＋`is_raft_leader{sys.catalog}`；tserver `ts_live_tablet_peers`＋`hybrid_clock_skew`；YSQL `yb_ysqlserver_max_connection_total` | <span class="at-chip at-ev-tested">實測</span>：名稱 master 2761、tserver 2274、YSQL 52、YCQL 266[^yb-exp-names] |
| scrape 預算 | master 4,576 個 sample、tserver 3,943（幾十個 tablet）；table 層級的 metric 隨表數成長，用 `version=v2` 的 allowlist 或 `max_metric_entries` 限制 | <span class="at-chip at-ev-tested">實測</span>[^yb-h-prom-v2] |
| `/prometheus-metrics` 會不會被卡 | 從 MetricRegistry 輸出、不拿 catalog 或 tablet 的鎖；lab 平常 0.003–0.03 秒，停機時最長約 3.6 秒（連線逾時） | <span class="at-chip at-ev-tested">實測</span>[^yb-h-prom-write] |

## 叢集 {#cluster}

| 問題 | 訊號 | 證據 |
|---|---|---|
| 抓得到 master leader 嗎 | `absent(is_raft_leader{table_name="sys.catalog"} == 1)`；`/api/v1/is-leader` 成功數 < 1 或 > 1 | <span class="at-chip at-ev-tested">實測</span>[^yb-c-is-leader][^yb-m-h-isleader] |
| master 還有過半嗎 | 拿得到 leader 回應的 master 數 < floor(預期 / 2) + 1（預期數寫在 `yb-params`）；leader 的 `max_follower_heartbeat_delay` > 10 秒（例子） | <span class="at-chip at-ev-tested">實測</span>[^yb-m-hb-delay] |
| 台數 | master 對 `yb-params`；tserver 對 master leader 記得的 live + dead | <span class="at-chip at-ev-tested">實測</span> |
| master leader 換太多次 | sys catalog 的 `raft_term` 15 分鐘變 ≥ 3 次（例子） | <span class="at-chip at-ev-tested">實測</span>[^yb-c-raft-term] |
| tablet 沒有 leader、少了副本 | blackbox 比對 `/api/v1/tablet-replication`、`/api/v1/health-check` 的 body | <span class="at-chip at-ev-tested">實測</span>[^yb-m-h-leaderless][^yb-m-h-health-underrep] |

**master 失去 quorum 時 YSQL 全停。**tserver 要持有 master leader 給的 YSQL operation lease 才能服務 YSQL；lease 30 秒沒有續約時 tserver 結束 PostgreSQL（`Lease has expired, killing pg sessions`）。[^yb-ysql-lease-flag][^yb-ysql-lease-ttl][^yb-ysql-lease-kill]lab 的順序：停 2 台 master 後 +12 秒已建立的 session 讀取成功、接著的 insert 15 秒 statement timeout；約 30 秒時三台 tserver 同時結束 PostgreSQL，新連線 `Connection refused`、13000 的 `up` 變 0；master 回來後 tserver 收到新的 lease epoch，又清掉所有 pg session。所以 YBYsqlPortDown 在三台同時響，原因是 master，不是 tserver。YCQL 在這時的行為沒有測。<span class="at-chip at-ev-tested">實測</span>[^yb-exp-lease-log][^yb-exp-raw]

**停 master leader。**kill 後 3 秒另一台的 `/api/v1/is-leader` 回 200（`leader_failure_max_missed_heartbeat_periods` 6 × 500ms），同時的一筆 insert 13 秒成功；`raft_term` 1 → 2。<span class="at-chip at-ev-tested">實測</span>[^yb-exp-raw][^yb-c-missed-hb]

**tablet 健康的兩個端點不一樣。**`/api/v1/health-check` 的 `under_replicated_tablets` 把「副本落在 dead tserver 上」也算進來，所以 master 判定 dead（60 秒）後就有；`/api/v1/tablet-under-replication` 只數 RUNNING 副本與 placement 要求，lab 整段都是空的（停 1 台、再停 2 台都是；原因推論是沒有設定 placement）。leaderless 由 master 依「多久沒有 valid leader」判定，超過 `leaderless_tablet_alert_delay_secs`（120 秒）才列出。<span class="at-chip at-ev-tested">實測</span>[^yb-m-h-health-underrep][^yb-m-h-underrep-running][^yb-m-leaderless-delay][^yb-exp-raw]

## 可用性 {#availability}

| 問題 | 訊號 | 證據 |
|---|---|---|
| tserver live／dead {#tserver-liveness} | master leader 的 `num_tablet_servers_dead` > 0（`tserver_unresponsive_timeout_ms` 60 秒） | <span class="at-chip at-ev-tested">實測</span>：+75 秒 dead 1[^yb-m-ts-unresponsive] |
| YSQL 接不接受連線 {#ysql-available} | `yb_ysqlserver_connection_over_limit_total` 的增加量；tcp probe | <span class="at-chip at-ev-tested">實測</span>[^yb-ysql-m-overlimit] |

停 1 台 tserver 時，剛好 leader 在它上面的 tablet 要等新 leader：lab 那時的一筆 insert 9 秒成功，之後讀寫 0 秒。<span class="at-chip at-ev-tested">實測</span>[^yb-exp-raw]

YSQL 連線上限 `ysql_max_connections` 是每台 tserver 各自的。lab（上限 12）在 tserver-1 開 12 條 `pg_sleep` 後，新連線 `too many clients`、tserver-2 照常；`superuser_reserved_connections`（3）保留給 superuser，但這些連線本身就是 superuser，一樣用完。已經連著的 postgres_exporter `pg_up` 仍是 1，看不出新連線被拒。<span class="at-chip at-ev-tested">實測</span>[^yb-exp-raw]

## 複寫 {#replication}

| 問題 | 訊號 | 證據 |
|---|---|---|
| follower 跟得上嗎 | `max by (instance) (follower_lag_ms)` > 10 秒（例子）：距上次收到 leader `UpdateConsensus` 的時間，閒置時也更新 | <span class="at-chip at-ev-tested">實測</span>：平常 0.5 秒左右，停 2 台後存活那台 155,648 毫秒[^yb-c-follower-lag] |

這不是 PostgreSQL 那種「沒有寫入時顯示 0」的延遲：leader 每 500ms 送 heartbeat。xCluster（跨叢集）延遲不在範圍，見[盲點](#blind-spots)。

## 資源與飽和 {#saturation}

| 問題 | 訊號（門檻） | 證據 |
|---|---|---|
| 時鐘 {#clock-skew} | `max_over_time(hybrid_clock_skew[10m])` > 250ms（例子） | <span class="at-chip at-ev-tested">實測</span>（crash）[^yb-clk-skew-def] |
| 記憶體 {#memory} | `mem_tracker_server / server_memory_soft_limit` > 0.9（例子）；記憶體壓力拒絕的增加量 | <span class="at-chip at-ev-tested">實測</span>（沒有觸發）[^yb-mem-soft][^yb-mem-leader-rej] |
| write stall {#write-stall} | `majority_sst_files_rejections` 增加（`sst_files_hard_limit` 48）；`rocksdb_stall_micros` 的 rate | <span class="at-chip at-ev-code">讀碼</span>[^yb-sst-rej][^yb-sst-hard] |
| WAL fsync {#wal-fsync} | `log_sync_latency` 平均 > 50ms（例子）；只有 sum／count | <span class="at-chip at-ev-code">讀碼</span>[^yb-log-sync][^yb-log-durable] |
| RPC 佇列溢出 | `increase(rpcs_queue_overflow[10m])` > 0（YBRpcQueueOverflow）；所有 service 共用一個計數，請求回 SERVER_TOO_BUSY；YBA 已刪掉對應的 DB_QUEUES_OVERFLOW | <span class="at-chip at-ev-code">讀碼</span>[^res-yb-overflow-incr][^res-yba-queues-overflow-deleted] |

**時鐘。**`hybrid_clock_skew` 是「最後的 hybrid time 比本機實體時鐘快多少」。超過 `max_clock_skew_usec`（500ms）而且有交易表的 tablet 已開啟時 `LOG(FATAL)`；關掉 `fail_on_out_of_range_clock_skew` 後只每秒寫 ERROR，超過 60 秒仍 FATAL。[^yb-clk-fatal][^yb-clk-fail-flag][^yb-clk-crash-bound][^yb-clk-enable]lab：第一次啟動用預設值，三台 tserver 同時 crash（skew 7 秒）；之後關掉 crash，每台有 2–26 行 ERROR，`max_over_time(hybrid_clock_skew[1h])` 全是 0。瞬間的 skew 在兩次 scrape 之間就被吸收，所以 YBClockSkew 只抓得到持續的偏移；時鐘同步本身要看 node_exporter 的 `node_timex_offset_seconds`。<span class="at-chip at-ev-tested">實測</span>[^yb-exp-clock][^yb-exp-raw]

**記憶體。**hard limit 預設是主機記憶體 × 0.85，soft limit 是 hard 的 85%。`mem_tracker_server` 不含 YSQL 的 PostgreSQL backend：lab tserver 容器約 400MB，`mem_tracker_server` 只有 4–9MB。<span class="at-chip at-ev-tested">實測</span>[^yb-mem-hard][^yb-exp-raw]

**慢磁碟、write stall、記憶體壓力沒有實驗。**門檻取自 YBA 範本或原始碼。

## 工作量 {#workload}

| 問題 | 訊號 | 證據 |
|---|---|---|
| 連線數 {#connections} | `yb_ysqlserver_connection_total / yb_ysqlserver_max_connection_total` > 0.8；YCQL 看 `rpc_connections_alive` | <span class="at-chip at-ev-tested">實測</span>[^yb-ysql-m-conn] |
| 延遲 {#query-latency} | `handler_latency_yb_ysqlserver_SQLProcessor_*Stmt` 平均 > 100ms（例子）；tserver Write RPC > 500ms（YBA） | <span class="at-chip at-ev-code">讀碼</span>[^yba-write-latency] |
| PostgreSQL 指標 | 見下一節 | <span class="at-chip at-ev-tested">實測</span> |

哪一條 SQL 造成負載，`/prometheus-metrics` 沒有；要查 pg_stat_statements 或 Active Session History（見 [models](models.md#yugabytedb)）。

### 和 PostgreSQL 的差異 {#postgres}

YSQL 是 PostgreSQL 15 的查詢層（`PostgreSQL 15.12-YB-2026.1.3.0`），資料在 DocDB，不在本地 heap。所以 PostgreSQL 的系統 view 都查得到，但「本地儲存」相關的數字沒有意義。lab 用 postgres_exporter v0.20.1（和 [PostgreSQL 頁](postgres.md)同一版）連 YSQL：<span class="at-chip at-ev-tested">實測</span>[^yb-exp-raw][^yb-exp-pgexp]

| PostgreSQL 的做法 | 在 YSQL 上 | 怎麼辦 |
|---|---|---|
| postgres_exporter 預設 collector | `stat_replication` 的查詢卡住（`RPCWait`），每次 scrape 多一條卡住的 backend，50 秒用光 12 條連線；exporter 停掉後 backend 還在 | `--no-collector.stat_replication`；卡住的用 `pg_terminate_backend` 清[^pgexp-statrepl-default][^pgexp-statrepl-query] |
| `settings` collector | `pg_settings` 裡 `log_statement_sample_rate`、`log_transaction_sample_rate` 各兩列，exporter 輸出重複 series，整個 `/metrics` 回 500、`up` 0（重複列是實測；回 500 是探索時看到、原始輸出沒有保存，只算推論） | `--no-collector.settings`[^pgexp-settings-query] |
| `pg_up`、`pg_stat_activity_count`、`pg_locks_count`、`pg_stat_database_xact_commit`／`xact_rollback` | 有值、會動 | 可以沿用；但每台 tserver 是獨立的 PostgreSQL，要每台各抓一次 |
| `pg_database_size_bytes`、`pg_stat_user_tables_n_live_tup`、`tup_inserted`、`pg_stat_bgwriter_*`、`pg_wal_*` | 0（表有 1000 列） | 改看 tserver 的 `rocksdb_total_sst_files_size`、`log_wal_size` |
| replication lag（`pg_replication_lag_seconds`）、`pg_current_wal_lsn()` | lag 恆為 0、`is_replica` 0；`pg_current_wal_lsn()` 回 not yet supported | 複寫是 tablet 的 Raft：看 `follower_lag_ms`、leaderless |
| 主庫／備庫角色（`pg_is_in_recovery()`） | 每台都是 `f`，沒有主備 | 角色是 master leader 與 tablet leader |
| 連線上限 `max_connections` | `ysql_max_connections`，每台各自；`yb_ysqlserver_*` 另有 metric | 用 13000 的 metric，不必靠 exporter |
| 鎖等待、長交易、vacuum | `pg_locks` 有；vacuum 不適用（DocDB 自己 compaction） | 長交易仍要看；vacuum 類規則不要搬 |

結論：PostgreSQL 卡片的「exporter 層」問題（collector 失敗、查詢卡住、連線被 exporter 吃掉）在 YSQL 上更嚴重；「儲存、複寫、vacuum」類訊號整組不適用，要換成 DocDB 與 Raft 的訊號。

## 資料維護 {#maintenance}

| 問題 | 訊號 | 證據 |
|---|---|---|
| cluster balancer 開著嗎 {#balancer} | master leader 的 `is_load_balancing_enabled` == 0 持續 30 分鐘（follower 永遠 0）；`outstanding_tablet_splits` 1 小時 > 0（例子） | <span class="at-chip at-ev-tested">實測</span>：leader 1、follower 0[^yb-m-lb-enabled] |

## 資料保護 {#protection}

不在這次範圍。核心的 `/prometheus-metrics` 沒有 snapshot schedule 或備份新鮮度的 metric；YBA 的 BACKUP_FAILURE、PITR_CONFIG_FAILURE 用 YBA 自己的 `ybp_*` metric。<span class="at-chip at-ev-code">讀碼</span>[^yba-pitr]備份工作成功後寫 textfile metric 給 node_exporter，並定期還原演練。

## 容量 {#capacity}

| 問題 | 訊號 | 證據 |
|---|---|---|
| 每台 tserver 的 tablet 數 | `ts_live_tablet_peers / ts_supportable_tablet_peers` > 0.9（YBA）；`-1` 代表沒有 guardrail（lab） | <span class="at-chip at-ev-tested">實測</span>[^yb-ts-supportable] |
| 磁碟 | `/prometheus-metrics` 沒有剩餘空間（drive 類只有 IO）；用 node_exporter | <span class="at-chip at-ev-tested">實測</span>（名稱清單）[^yb-exp-names] |

## 盲點與補法 {#blind-spots}

| 盲點 | 類別 | 補法 |
|---|---|---|
| SQL 層是否真的能執行交易 | 資料庫本身沒提供 | 外部 SQL probe 定期讀寫測試表（YBA 的 DB_WRITE_READ_TEST_ERROR 就是這個做法）；tcp probe 在連線用完時仍成功 |
| master leader 抓不到時的叢集狀態 | 元件有、只有 leader 輸出 | YBMasterLeaderMetricsMissing；`yb-admin` 或 `/api/v1/tablet-servers` |
| 時鐘同步 | 要靠其他元件 | node_exporter 的 `node_timex_offset_seconds`、`node_timex_sync_status`；已由 [主機與容器資源](../components/host.md) 補上（規則只讀碼，host lab 之後排程） |
| 磁碟空間、唯讀 | 要靠其他元件 | node_exporter 的 `node_filesystem_avail_bytes`；已由 [主機與容器資源](../components/host.md) 補上（規則只讀碼，host lab 之後排程） |
| 備份與 PITR | 要靠其他元件 | 備份工作寫 textfile metric；定期還原 |
| xCluster 延遲 | 要靠其他元件 | 沒有實測；用到時至少抓 consumer 端的 safe time lag |
| 哪一條 SQL 造成負載 | 元件有、`/prometheus-metrics` 沒輸出 | pg_stat_statements、ASH 經 query-exporter 彙總（未實測） |
| TLS 憑證到期 | 資料庫本身沒提供 | blackbox 的 `probe_ssl_earliest_cert_expiry` |

## 實驗 {#lab}

`lab/targets/yugabytedb`：YugabyteDB v2026.1.3.0-b100（master ×3、tserver ×3，RF=3，記憶體 hard limit master 256MB、tserver 384MB）、Prometheus v3.13.5（scrape 5 秒）、blackbox_exporter v0.27.0（timeout 4 秒）、postgres_exporter v0.20.1，載入 `rules/targets/yugabytedb.yml`。`follower_unavailable_considered_failed_sec` 120 秒、`ysql_max_connections` 12；`fail_on_out_of_range_clock_skew=false`（否則這台機器上 tserver 啟動就 crash）。`./run.sh` 約 14 分鐘加上啟動 2 分鐘，結束時 `docker compose down -v`（殘留容器、volume 0）。原始輸出與結果摘要見 `results/run-20261110.txt` 與 lab 的 README。[^yb-exp][^yb-exp-summary]
