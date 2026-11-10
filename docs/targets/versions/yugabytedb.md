# YugabyteDB 各版本的監控差異

這頁列出 YugabyteDB 2.20、2024.2、2025.1、2025.2、2026.1 之間會影響監控的差異：master／tserver metric 的新增、YSQL 底層 PostgreSQL 的版本、YSQL 連線 metric。只看 Apache-2.0 的核心（`yugabyte/yugabyte-db`），不含 YugabyteDB Anywhere。目前的卡片核對過 v2026.1.3.0。

依據是各 stable 線最新 tag（`2.20.12_FINAL`、`2024.2.12.0-b45`、`2025.1.5.0-b103`、`2025.2.9.0-b70`、`2026.1.3.0-b100`）的原始碼：對卡片用到的每個 metric 名稱在五個 tag 裡 grep。沒有做實驗，全部是<span class="at-chip at-ev-code">讀碼</span>。2024.1 已 EOL，沒有比對。

## 結論 {#summary}

1. **`max_follower_heartbeat_delay` 在 2.20 不存在。**卡片 `master-quorum` 的 metric 從 2024.2 起才有；2.20 上規則回傳空，只能靠 blackbox 探 master。
2. **`outstanding_tablet_splits` 在 2.20 不存在。**卡片 `balancer` 的分裂積壓規則在 2.20 回傳空。
3. **YSQL 在 2025.1 從 PostgreSQL 11.2 換成 15.12。**用 postgres_exporter 監 YSQL 時，2.20、2024.2 走的是 13 以前的查詢（`total_time`、沒有 `wal_status`、沒有 `active_time`），2025.1 起走 13–16 的查詢；同一條規則在升版前後會多出或少掉 metric。
4. **YSQL 連線 metric 名稱五個版本相同**（`yb_ysqlserver_connection_total`、`_max_connection_total`、`_connection_over_limit_total`），只有原始檔改名（`pgsql_webserver_wrapper.cc` → `ybc_pg_webserver_wrapper.cc`）。
5. **其他卡片 metric 五個版本都有**：`hybrid_clock_skew`、`is_raft_leader`、`raft_term`、`follower_lag_ms`、`num_tablet_servers_live`／`_dead`、`ts_live_tablet_peers`、`ts_supportable_tablet_peers`、`server_memory_soft_limit`、`leader_memory_pressure_rejections`、`majority_sst_files_rejections`、`rocksdb_stall_micros`、`log_sync_latency`、`rpc_connections_alive`、`is_load_balancing_enabled`。

## 選的版本 {#versions}

| 版本線 | 這頁用的 tag | 狀態（[releases](https://docs.yugabyte.com/preview/releases/ybdb-releases/)，2026-11-10） |
|---|---|---|
| 2.20 LTS | `2.20.12_FINAL` | 2026-05-13 EOL |
| 2024.2 LTS | `2024.2.12.0-b45` | 維護到 2026-12-09 |
| 2025.1 STS | `2025.1.5.0-b103` | 維護到 2026-07-23（EOL 2027-01-23） |
| 2025.2 LTS | `2025.2.9.0-b70` | 維護到 2027-12-11 |
| 2026.1 STS | `2026.1.3.0-b100` | 卡片核對過；另有 `2026.1.4.0` 的 build tag，這頁沒有用 |

## 版本差異表 {#diff}

| 版本 | 變更 | 對監控的影響（哪個 metric／collector／規則） | exporter 或 endpoint 有沒有處理 | 依據 |
|---|---|---|---|---|
| 2024.2 | master 新增 `max_follower_heartbeat_delay`（master leader 多久沒收到所有 follower 的回應） | 2.20 上 `master-quorum` 回傳空 | 內建 `/prometheus-metrics` | [^ver-yb220-no-hb-delay][^ver-yb242-hb-delay] |
| 2024.2 | master 新增 `outstanding_tablet_splits` | 2.20 上 `balancer` 的分裂規則回傳空 | 同上 | [^ver-yb242-splits] |
| 2025.1 | YSQL 改以 PostgreSQL 15.12 為基礎（2.20、2024.2 是 11.2） | postgres_exporter 依伺服器版本換查詢：`pg_stat_statements` 欄位、`replication_slots` 的 `wal_status`、`stat_database` 的 `active_time` 在 2025.1 起才有；`postgres-carryover` 規則用的 `pg_up`、`pg_scrape_collector_success`、`pg_stat_database_xact_commit` 不受影響 | postgres_exporter 依版本切換（見 [PostgreSQL 版本差異](postgres.md#exporter)） | [^ver-yb220-pg11][^ver-yb242-pg11][^ver-yb251-pg15][^ver-pgx-slots-13][^ver-pgx-statdb-14][^ver-pgx-pss-17] |
| 2025.1 | YSQL 連線 metric 的原始檔改名，名稱不變 | 不影響 | 同上 | [^ver-yb220-ysql-conn][^ver-yb251-ysql-conn] |
| 2024.2 | RocksDB 統計名稱表移到 `util/statistics.cc`，名稱不變 | `write-stall` 的 `rocksdb_stall_micros` 不影響 | 同上 | [^ver-yb220-stall][^ver-yb242-stall] |
| 2025.1 | YSQL metric 多一組 `yb_ysqlserver_` 前綴的計數；語句延遲前綴仍是 `handler_latency_yb_ysqlserver_SQLProcessor_`（2026.1 相同） | `query-latency` 不影響 | 同上 | [^ver-yb220-ysql-prefix][^ver-yb251-ysql-prefix][^ver-yb261-ysql-prefix] |
| 2.20–2026.1 | `hybrid_clock_skew` 等卡片用的 metric 名稱相同（行號隨版本移動） | 其他訊號不受影響 | 同上 | [^ver-yb220-clock-skew]；這頁對五個 tag 逐一 grep |

## 各版本要注意的地方 {#notes}

- **2.20**：已 EOL。沒有 `max_follower_heartbeat_delay`、`outstanding_tablet_splits`；YSQL 是 PG 11.2。
- **2024.2**：master metric 齊了；YSQL 仍是 PG 11.2。
- **2025.1、2025.2**：YSQL 是 PG 15.12；從 2024.2 升上來時 postgres_exporter 輸出的 metric 會變多。
- **2026.1**：卡片核對的版本。

## 卡片上會隨版本變的訊號 {#signals}

| 訊號 | 隨版本變什麼 | 建議 |
|---|---|---|
| `master-quorum` | `max_follower_heartbeat_delay` 2024.2 起 | `applies_when: YugabyteDB 2024.2 以上`；2.20 只用 blackbox |
| `balancer` | `outstanding_tablet_splits` 2024.2 起 | 分裂那條規則 `applies_when: 2024.2 以上` |
| `postgres-carryover` | YSQL 的 PG 基底 2025.1 從 11 變 15 | 卡片註明 postgres_exporter 在 2025.1 前後走不同查詢；規則只用三個版本都有的 metric（目前已是） |
| 其他 | 名稱五個版本相同 | 不用改 |

這頁沒有比對 `/prometheus-metrics` 的輸出格式（例如 sample 是否帶時間戳）在各版本是否相同；卡片記錄的是 2026.1 的行為。YugabyteDB 的監控見 [YugabyteDB 怎麼監控](../yugabytedb.md)。
