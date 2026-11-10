# 附錄：從原始碼到 metric（YugabyteDB）

[主頁](yugabytedb.md)列出每個訊號該看哪個 metric。這頁回答另一個問題：**這個 metric 或健康端點是在哪一行產生的？什麼時候更新、誰會輸出？**YugabyteDB 沒有 exporter，鏈只有兩段：

1. **資料庫端**：YugabyteDB v2026.1.3.0（commit `f2635f5`，和映像 `2026.1.3.0-b100` 的 `version_metadata.json` 同一個 git hash），只讀 Apache-2.0 的核心（`src/`）。YugabyteDB Anywhere 的告警範本（`managed/`，Polyform Free Trial）只引用名稱與行號。
2. **Metric**：名稱、型別、更新時機。

每個原始碼參考都在 tag 上依關鍵字找到行號。「Lab」欄只寫在 `lab/targets/yugabytedb` 親眼看到的情況。

## 先講結論

1. **`/status` 一律回 `{}`。**master 與 tserver 共用同一個 handler，不看任何狀態。<span class="at-chip at-ev-tested">實測</span>[^yb-h-status][^yb-exp-raw]
2. **master 的 JSON API 在 follower 上會轉送給 leader，失敗時仍回 200。**`/api/v1/health-check`、`/api/v1/tablet-replication`、`/api/v1/tablet-under-replication` 都是 `RegisterLeaderOrRedirect`；轉送失敗只把錯誤寫進內容。所以 blackbox 要比對 body 裡的 key，不能只看狀態碼。只有 `/api/v1/is-leader` 用狀態碼（leader 200、其他 503）。<span class="at-chip at-ev-tested">實測</span>[^yb-m-h-leader-or-redirect][^yb-m-h-redirect-nolist][^yb-m-h-redirect-fail][^yb-m-h-isleader-503][^yb-exp-raw]
3. **叢集層的 metric 只有 master leader 在寫。**`num_tablet_servers_live`、`num_tablet_servers_dead`、`is_load_balancing_enabled` 由 leader 的背景執行緒每輪寫一次；follower 一直是初始值 0，失去 leader 時歸 0。<span class="at-chip at-ev-tested">實測</span>[^yb-m-bg-report][^yb-m-report-metrics][^yb-m-bg-reset][^yb-m-reset-metrics][^yb-exp-raw]
4. **leaderless 與 under-replicated tablet 沒有 metric。**資料庫只在 master 的 JSON API 算；YBA 範本用的 `yb_node_leaderless_tablet_count`、`yb_node_underreplicated_tablet_count`、`yb_node_is_master_leader` 在核心原始碼裡找不到，是 YBA 平台產生的。<span class="at-chip at-ev-code">讀碼</span>[^yb-m-h-leaderless][^yb-m-h-underrep-running][^yba-leaderless][^yba-underrep][^yba-master-leader]
5. **tablet 層級的 metric 輸出時加總到表。**同一台 tserver 上同一張表的 tablet 合成一條（`metric_type="tablet"` 但 label 只有 table），`is_raft_leader` 是「這台有幾個 leader」。<span class="at-chip at-ev-tested">實測</span>[^yb-c-is-leader][^yb-exp-raw]

## 健康端點 {#health}

| 元件 | 端點 | 檢查 | 依據 |
|---|---|---|---|
| master、tserver | `/status` | 無，回 `{}` | [^yb-h-status][^yb-h-status-register] |
| master | `/api/v1/is-leader` | 不是已初始化的 leader 時 503 | [^yb-m-h-register-isleader][^yb-m-h-isleader][^yb-m-h-isleader-503] |
| master | `/api/v1/health-check`（轉送給 leader） | `dead_nodes`：不是 live 的 tserver；`under_replicated_tablets`：副本位置少於 RF，或副本落在 dead tserver 上 | [^yb-m-h-register-health][^yb-m-h-healthcheck][^yb-m-h-health-dead][^yb-m-h-health-underrep] |
| master | `/api/v1/tablet-replication`（轉送） | `leaderless_tablets`：沒有 valid leader 超過 `leaderless_tablet_alert_delay_secs`（120 秒） | [^yb-m-h-register-replication][^yb-m-h-leaderless][^yb-m-leaderless-delay] |
| master | `/api/v1/tablet-under-replication`（轉送） | RUNNING／UNKNOWN 副本數少於 placement 的要求；停掉的 tserver 被踢出 Raft 設定前仍算 RUNNING | [^yb-m-h-register-underrep][^yb-m-h-underrep-running] |
| tserver | `/api/v1/health-check` | 只列 FAILED 狀態的 tablet，不看 master | [^yb-ts-h-register-health][^yb-ts-h-health] |
| master、tserver、YCQL | `/prometheus-metrics` | 從 MetricRegistry 輸出；URL 參數 `metrics=`、`priority_regex=`、`max_metric_entries=`、`version=v2`（table／server allowlist） | [^yb-h-prom-register][^yb-h-prom-write][^yb-h-prom-maxentries-flag][^yb-h-prom-v2] |
| YSQL | `:13000/prometheus-metrics` | PostgreSQL 程序內的 webserver（yb_pg_metrics） | [^yb-ysql-m-conn] |

## master：leader、tserver 存活、balancer {#master}

| Metric | 產生的地方 | 更新時機 | Lab | 依據 |
|---|---|---|---|---|
| `is_raft_leader{table_name="sys.catalog"}` | master 的 sys catalog tablet（一組 Raft） | Raft 角色改變 | 只有 leader 是 1 | [^yb-c-is-leader] |
| `raft_term{table_name="sys.catalog"}` | 同上 | 新 term | 選舉後加 1 | [^yb-c-raft-term] |
| `num_tablet_servers_live`、`num_tablet_servers_dead` | master leader 的 `ReportMetrics` | leader 背景執行緒每輪；失去 leader 時 reset 成 0 | follower 一直 0 | [^yb-m-num-live][^yb-m-num-dead][^yb-m-report-metrics][^yb-m-bg-report][^yb-m-bg-reset] |
| `max_follower_heartbeat_delay` | 同上 | master leader 多久沒收到 master follower 的回應 | | [^yb-m-hb-delay] |
| `is_load_balancing_enabled` | cluster balancer | leader 寫入；follower 0 | follower 0、leader 1 | [^yb-m-lb-enabled] |
| `outstanding_tablet_splits` | tablet split manager | | | [^yb-m-split-outstanding] |

tserver 每 `heartbeat_interval_ms`（1 秒）送 heartbeat 給 master leader；超過 `tserver_unresponsive_timeout_ms`（60 秒）沒收到就不算 live。[^yb-ts-heartbeat-interval][^yb-m-ts-unresponsive]

## tablet 的 Raft 與 WAL {#tablet}

| Metric／設定 | 說明 | 依據 |
|---|---|---|
| `follower_lag_ms` | 距上次收到 leader `UpdateConsensus` 的時間，取 max；leader 每 `raft_heartbeat_interval_ms`（500ms）送一次，閒置時也更新 | [^yb-c-follower-lag][^yb-c-hb-interval] |
| `leader_failure_max_missed_heartbeat_periods` | 6：follower 約 3 秒沒收到 heartbeat 才發起選舉 | [^yb-c-missed-hb] |
| `leader_lease_duration_ms` | 2000：新 leader 要等舊 lease 過期才服務讀寫 | [^yb-c-lease][^yb-c-lease-default] |
| `follower_unavailable_considered_failed_sec` | 900：leader 把沒回應的 follower 踢出 Raft 設定 | [^yb-c-follower-failed] |
| `log_sync_latency` | WAL fsync 耗時（微秒）；event_stats 只有 `_sum` 與 `_count`，沒有 bucket | [^yb-log-sync] |
| `durable_wal_write`、`interval_durable_wal_write_ms` | 預設 false、1000：WAL 不是每次寫入都 fsync | [^yb-log-durable][^yb-log-interval-durable] |

## 時鐘 {#clock}

| 項目 | 說明 | 依據 |
|---|---|---|
| `hybrid_clock_skew` | 最後的 hybrid time 減本機實體時鐘（微秒，只取正值）：別台的時間比我快多少 | [^yb-clk-skew-metric][^yb-clk-skew-def] |
| `max_clock_skew_usec` | 500ms | [^yb-clk-max-skew] |
| skew 超過上限 | `fail_on_out_of_range_clock_skew`（預設 true）而且有交易表的 tablet 已開啟時 `LOG(FATAL)`；否則每秒寫一次 ERROR；超過 `clock_skew_force_crash_bound_usec`（60 秒）一律 FATAL | [^yb-clk-fail-flag][^yb-clk-enable][^yb-clk-fatal][^yb-clk-error][^yb-clk-crash-bound] |

Lab：預設設定下三台 tserver 啟動約 20 秒內同時 FATAL（`Too big clock skew is detected: 7.029s`）；同一台主機的容器共用核心時鐘，skew 是牆鐘往回跳造成的。master 只寫 ERROR（master 沒有開啟交易表的 tablet）。<span class="at-chip at-ev-tested">實測</span>[^yb-exp-clock]

## 記憶體、DocDB 與 YSQL {#tserver}

| Metric／設定 | 說明 | 依據 |
|---|---|---|
| `server_memory_hard_limit`、`server_memory_soft_limit`、`mem_tracker_server` | hard limit 預設是主機記憶體 × 0.85，soft limit 是 hard 的 85% | [^yb-mem-hard][^yb-mem-soft] |
| `leader_memory_pressure_rejections` 等 | 因記憶體壓力拒絕的寫入（tablet 層級，加總到表） | [^yb-mem-leader-rej] |
| `majority_sst_files_rejections` | 過半副本的 SST 檔數超過上限而拒絕的寫入；`sst_files_soft_limit` 24、`sst_files_hard_limit` 48 | [^yb-sst-rej][^yb-sst-soft][^yb-sst-hard] |
| `yb_ysqlserver_connection_total`、`yb_ysqlserver_max_connection_total`、`yb_ysqlserver_connection_over_limit_total` | YSQL webserver 輸出；上限是 `ysql_max_connections`（預設 0，由 PostgreSQL 決定） | [^yb-ysql-m-conn][^yb-ysql-m-max][^yb-ysql-m-overlimit][^yb-ysql-maxconn] |
| `enable_ysql_conn_mgr` | 預設 false（connection manager 沒開） | [^yb-connmgr] |
| `enable_ysql_operation_lease`、`master_ysql_operation_lease_ttl_ms` | 預設 true、30 秒：tserver 要持有 master leader 續約的 lease 才服務 YSQL；過期時 `KillPg`。lab：停 2 台 master 約 30 秒後三台同時結束 PostgreSQL | [^yb-ysql-lease-flag][^yb-ysql-lease-ttl][^yb-ysql-lease-kill][^yb-exp-lease-log] |
| `ts_supportable_tablet_peers` | -1 代表沒有 tablet 數 guardrail | [^yb-ts-supportable] |
| `metrics_retirement_age_ms` | 120 秒：表或 tablet 刪除後 metric 留多久 | [^yb-metrics-retire] |

## postgres_exporter 對 YSQL {#postgres-exporter}

| Collector | 預設 | 在 YSQL 上 | 依據 |
|---|---|---|---|
| `stat_replication` | 開 | 查詢 `pg_stat_replication` 卡住不回（`RPCWait`／`WaitingOnTServer`）；exporter 沒有 statement timeout，每次 scrape 多一條卡住的 backend | [^pgexp-statrepl-default][^pgexp-statrepl-query][^yb-exp-raw] |
| `settings` | 開 | YSQL 的 `pg_settings` 有兩個 GUC 各出現兩次，exporter 輸出重複的 series，整個 `/metrics` 回 500（重複列是實測；回 500 是探索時看到、原始輸出沒有保存，只算推論） | [^pgexp-settings-default][^pgexp-settings-query][^yb-exp-raw] |
| 其他 13 個預設 collector | 開 | 成功；但多數數值是 PostgreSQL 本地的計數，YSQL 的資料不在本地 heap（見主頁） | [^yb-exp-pgexp] |
