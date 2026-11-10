# PostgreSQL 各版本的監控差異

這頁列出 PostgreSQL 13–18 之間會影響監控的差異：哪些 view 欄位改名或消失、哪些設定被移除、postgres_exporter v0.20.1 有沒有依版本處理。目前的卡片只核對過 PostgreSQL 16.15 與 17.11。

依據是各主版本最新 tag 的原始碼（`REL_13_23`、`REL_14_24`、`REL_15_19`、`REL_16_15`、`REL_17_11`、`REL_18_6`）與 postgres_exporter v0.20.1（目前最新 release）。沒有做實驗，全部是<span class="at-chip at-ev-code">讀碼</span>；標「推論」的是從原始碼推出、沒有單獨驗證的結果。

## 結論 {#summary}

1. **17 起 checkpoint 計數搬到 `pg_stat_checkpointer`，exporter 的對應 collector 預設關閉。**`stat_bgwriter`（預設開）在 17 以上只輸出四個欄位，`pg_stat_bgwriter_checkpoints_*` 消失；不加 `--collector.stat_checkpointer` 時 `checkpoint-pressure` 規則回傳空，collector 仍回報成功。
2. **`stat_statements` 依伺服器版本選查詢，不看 extension 版本。**`pg_upgrade` 到 17 後沒有 `ALTER EXTENSION pg_stat_statements UPDATE`，extension 還是 1.10，沒有 `shared_blk_read_time`，整個 collector 失敗（推論）。
3. **17 起 `pg_stat_progress_vacuum` 的第 6、7 個參數改成 byte。**exporter 不分版本直接讀 `param6`／`param7`，`pg_stat_progress_vacuum_max_dead_tuples` 在 17 以上其實是「byte 上限」，數字大很多，名稱不變。
4. **18 把 WAL 的寫入與 fsync 從 `pg_stat_wal` 移到 `pg_stat_io`。**自訂查詢 `stat_io` 依 `backend_type` 加總所有 object，18 起多了 `object = 'wal'` 的列，`pgq_io_writes`、`pgq_io_fsyncs` 的意義跟著變（多了 WAL）。
5. **replication slot 的欄位每版都在加。**13 加 `wal_status`、`safe_wal_size`，16 加 `conflicting`，17 加 `invalidation_reason`、`inactive_since`，18 多一種失效原因 `idle_timeout`。exporter 只在 13 以上查 `wal_status`，沒有輸出 `invalidation_reason`。

## 選的版本 {#versions}

| 版本線 | 這頁用的 tag | 狀態（[versioning policy](https://www.postgresql.org/support/versioning/)，2026-11-10） |
|---|---|---|
| 13 | `REL_13_23` | 2025-11-13 EOL |
| 14 | `REL_14_24` | 支援到 2026-11-12 |
| 15 | `REL_15_19` | 支援到 2027-11-11 |
| 16 | `REL_16_15` | 卡片核對過 |
| 17 | `REL_17_11` | 卡片核對過、lab 實測 |
| 18 | `REL_18_6` | 最新主版本 |
| postgres_exporter | `v0.20.1` | 最新 release；19 只有 `REL_19_RC1`，沒有比對 |

## 版本差異表 {#diff}

| 版本 | 變更 | 對監控的影響（哪個 metric／collector／規則） | exporter 或 endpoint 有沒有處理 | 依據 |
|---|---|---|---|---|
| 13 | `pg_replication_slots` 加 `wal_status`、`safe_wal_size` | `pg_replication_slots_safe_wal_size_bytes`、`wal_status` 只在 13 以上有 | 有：13 以上才用新查詢 | [^ver-pg13-slot-walstatus][^ver-pgx-slots-13] |
| 13 | `wal_keep_segments` 改成 `wal_keep_size` | `pg_settings_wal_keep_segments` 消失，改看 `wal_keep_size` | 不需要（照 `pg_settings` 輸出） | [^ver-pg13-wal-keep-size] |
| 13 | pg_stat_statements 1.8：`total_time` 拆成 `total_plan_time`、`total_exec_time` | 慢 SQL 查詢要用 `total_exec_time` | 有：13 以上用 `total_exec_time` | [^ver-pg13-pss][^ver-pg13-pss-exec-time][^ver-pgx-pss-17] |
| 14 | 新增 `pg_stat_wal`；`pg_stat_database` 加 `session_time`、`active_time`、`sessions_*` | 自訂查詢 `pgq_wal_*`、`pgq_db_sessions_*` 只在 14 以上能跑；13 上每次都失敗 | exporter 的 `stat_database` 只在 14 以上輸出 `active_time`；自訂查詢要把 13 的 DSN 拿掉 | [^ver-pg14-wal][^ver-pg14-sessions][^ver-pgx-statdb-14] |
| 14 | pg_stat_statements 1.9：加 `toplevel` | 巢狀呼叫的語句會有兩列（top level 與否） | 沒有用到 | [^ver-pg14-pss] |
| 15 | 統計改放共享記憶體，`stats_temp_directory` 移除 | `pg_settings_stats_temp_directory` 消失（字串，exporter 本來就不輸出） | 不需要 | [^ver-pg14-stats-temp-dir] |
| 15 | pg_stat_statements 1.10：加 `jit_*`、`temp_blk_read_time` | 新欄位，沒有規則依賴 | 沒有輸出 | [^ver-pg15-pss] |
| 16 | 新增 `pg_stat_io` | 自訂查詢 `stat_io` 只在 16 以上能跑 | exporter 沒有 collector | [^ver-pg16-io] |
| 16 | `vacuum_defer_cleanup_age` 移除；新增 `reserved_connections`（配合 `pg_use_reserved_connections`） | 16 以前 exporter 帳號沒有保留連線可用；`connection-headroom` 的保留位要看兩個設定 | 不需要（照 `pg_settings` 輸出） | [^ver-pg15-defer-cleanup][^ver-pg16-reserved] |
| 16 | `pg_replication_slots` 加 `conflicting` | 邏輯 slot 在 standby 上因衝突失效時才看得到 | 沒有輸出 | [^ver-pg16-slot-conflicting] |
| 17 | `pg_stat_bgwriter` 移除 `checkpoints_timed`、`checkpoints_req`、`checkpoint_*_time`、`buffers_checkpoint`、`buffers_backend*`；新增 `pg_stat_checkpointer` | `pg_stat_bgwriter_checkpoints_timed_total` 等消失；`checkpoint-pressure`、`version-semantics` 只寫舊名的規則回傳空 | 部分：`stat_bgwriter` 改用四欄查詢；`stat_checkpointer` **預設關**，17 以下只記 warn、回報成功 | [^ver-pg16-bgwriter-ckpt][^ver-pg17-bgwriter][^ver-pg17-checkpointer][^ver-pgx-bgwriter-17][^ver-pgx-checkpointer-off][^ver-pgx-checkpointer-before17] |
| 17 | pg_stat_statements 1.11：`blk_read_time`／`blk_write_time` 改名 `shared_blk_read_time`／`shared_blk_write_time` | `pg_stat_statements_block_read_seconds_total` 的來源欄位換了 | 有，但依**伺服器版本**選查詢：extension 沒升到 1.11 時查詢失敗，`pg_scrape_collector_success{collector="stat_statements"}` 0（推論） | [^ver-pg17-pss][^ver-pg17-pss-shared-blk][^ver-pgx-pss-17][^ver-pgx-pss-shared-blk] |
| 17 | `pg_stat_progress_vacuum`：`param6`／`param7` 改成 `max_dead_tuple_bytes`／`dead_tuple_bytes`，`num_dead_item_ids` 移到 `param8` | `pg_stat_progress_vacuum_max_dead_tuples`、`_num_dead_tuples` 在 17 以上是 byte，不是筆數 | **沒有**：不分版本讀 `param6`／`param7` | [^ver-pg16-vacuum-progress][^ver-pg17-vacuum-progress][^ver-pgx-vacuum-param6] |
| 17 | `pg_replication_slots` 加 `invalidation_reason`、`inactive_since`、`failover`、`synced` | slot 為什麼失效、閒置多久可以直接查 | 沒有輸出；要自訂查詢 | [^ver-pg17-slot-invalidation] |
| 17 | `old_snapshot_threshold` 移除 | `pg_settings_old_snapshot_threshold_seconds` 消失 | 不需要 | [^ver-pg16-old-snapshot] |
| 18 | `pg_stat_wal` 移除 `wal_write`、`wal_sync`、`wal_write_time`、`wal_sync_time` | 查這四欄的自訂查詢在 18 失敗；本 repo 的 `stat_wal` 只查 records／fpi／bytes／buffers_full，不受影響 | exporter 沒有 `pg_stat_wal` collector | [^ver-pg17-wal-write][^ver-pg18-wal] |
| 18 | `pg_stat_io` 加 `object = 'wal'`；`op_bytes` 換成 `read_bytes`／`write_bytes`／`extend_bytes` | `pgq_io_writes`、`pgq_io_fsyncs` 依 backend_type 加總所有 object，18 起含 WAL；`wal-io-sources` 的基準要重取 | 沒有：自訂查詢要加 `WHERE object <> 'wal'` 或分開輸出 | [^ver-pg18-io-bytes][^ver-pg18-io-wal] |
| 18 | `pg_stat_checkpointer` 加 `num_done`、`slru_written` | 多了「完成的 checkpoint 數」，可以和 `num_timed`、`num_requested` 對照 | 沒有輸出 `num_done` | [^ver-pg18-checkpointer-done] |
| 18 | slot 失效原因多 `idle_timeout`（新設定 `idle_replication_slot_timeout`） | 閒置太久的 slot 會被設成失效，`pg_replication_slots_active` 0；`replication-slots` 規則看不出是 timeout | 沒有輸出失效原因 | [^ver-pg18-slot-idle-timeout][^ver-pg18-guc-idle-slot] |
| 18 | pg_stat_statements 1.12：加 `wal_buffers_full`、`parallel_workers_*` | 新欄位，沒有規則依賴 | 沒有輸出 | [^ver-pg18-pss][^ver-pg18-pss-wal-buffers] |
| 13–18 | `pg_stat_replication`、`pg_stat_wal_receiver` 欄位清單六個版本都一樣 | `standby-streaming`、`wal-receiver`、`replication-lag` 不受版本影響 | 不需要 | [^ver-pg13-statrep][^ver-pg18-statrep][^ver-pg18-walrcv] |

## postgres_exporter 怎麼處理版本 {#exporter}

| 項目 | 做法 | 會怎麼壞 | 依據 |
|---|---|---|---|
| 版本判斷 | 每條新連線查一次伺服器版本，collector 用 semver 比較 | 只看伺服器版本，不看 extension 版本 | [^ver-pgx-version] |
| `stat_checkpointer` | 預設關；17 以下 `Update` 直接回 nil（記 warn） | 17 上沒開：checkpoint metric 全部消失，`pg_scrape_collector_success` 沒有這個 collector；開了但在 16 上：成功、沒資料 | [^ver-pgx-checkpointer-off][^ver-pgx-checkpointer-before17] |
| `stat_bgwriter` | 17 以上改查四欄 | 17 上 `pg_stat_bgwriter_checkpoints_*` 消失，collector 成功 | [^ver-pgx-bgwriter-17] |
| `stat_statements` | 依伺服器版本選三組查詢（<13、13–16、≥17） | 伺服器 17、extension 1.10：欄位不存在，collector 失敗（推論） | [^ver-pgx-pss-17][^ver-pgx-pss-shared-blk] |
| `stat_progress_vacuum` | 不分版本讀 `param6`、`param7` | 17 起單位從筆數變 byte | [^ver-pgx-vacuum-param6][^ver-pg17-vacuum-progress] |
| `replication_slots` | 13 以上加查 `wal_status`、`safe_wal_size` | 17 的 `invalidation_reason` 與 18 的 `idle_timeout` 看不到 | [^ver-pgx-slots-13] |

## 各版本要注意的地方 {#notes}

- **13**：已 EOL。沒有 `pg_stat_wal`、`pg_stat_io`、`sessions_*`，本 repo 的自訂查詢有三組不能用；`wal_keep_size` 取代 `wal_keep_segments`。
- **14、15**：14 在 2026-11-12 EOL。checkpoint 仍在 `pg_stat_bgwriter`；`pg_stat_io` 還沒有。
- **16**：有 `pg_stat_io`、`reserved_connections`；checkpoint 仍在 `pg_stat_bgwriter`。
- **17**：要打開 `--collector.stat_checkpointer`；升版後要 `ALTER EXTENSION pg_stat_statements UPDATE`；`pg_stat_progress_vacuum_max_dead_tuples` 改成 byte。
- **18**：`pg_stat_io` 含 WAL，自訂查詢要分開；slot 可能因 `idle_replication_slot_timeout` 失效；`pg_stat_wal` 少四欄。

## 卡片上會隨版本變的訊號 {#signals}

| 訊號 | 隨版本變什麼 | 建議 |
|---|---|---|
| `checkpoint-pressure`、`version-semantics` | 17 起 checkpoint metric 換名，且要另開 collector | 規則保持新舊名稱 `or`；`applies_when` 寫「17 以上要 `--collector.stat_checkpointer`」（卡片已寫）；加一條「17 以上卻沒有 `pg_stat_checkpointer_*`」的自檢 |
| `pg-stat-statements`、`slow-sql` | 17 起伺服器版本與 extension 版本不一致時整個 collector 失敗 | `collector-success` 已涵蓋；runbook 加「檢查 `SELECT extversion FROM pg_extension WHERE extname='pg_stat_statements'`」 |
| `autovacuum` | 只用 `heap_blks_scanned`，不受 17 的單位改變影響；但 dashboard 若用 `max_dead_tuples` 會錯 | 卡片註明 17 起 `max_dead_tuples` 是 byte |
| `wal-io-sources` | 18 起 `pgq_io_*` 含 WAL | 自訂查詢加 `object` label 或排除 `wal`；`applies_when` 標 16 以上 |
| `replication-slots` | 13 起才有 `safe_wal_size`；18 多 `idle_timeout` 失效 | `applies_when` 標 13 以上；18 加一條自訂查詢輸出 `invalidation_reason` |
| `session-errors`、`custom-queries` | `sessions_*` 14 起、`pg_stat_io` 16 起 | 依 DSN 版本把查詢的 `databases` 拆開 |
| `connection-headroom` | 16 起才有 `reserved_connections` | 16 以下只有 `superuser_reserved_connections`，exporter 帳號要是 superuser 才能用保留位 |

其他訊號（`standby-streaming`、`wal-receiver`、`replication-lag`、`archiver`、`wraparound`、`deadlocks-rollbacks`、`cache-and-temp`）用的 view 欄位在 13–18 沒有找到改名或移除。`pg_stat_activity` 的 `wait_event` 名稱在各版本有增減，卡片沒有依賴特定 wait event，這頁沒有逐一比對。

MySQL 與 MariaDB 的差異在 [MySQL 各版本的監控差異](mysql.md)、[MariaDB 各版本的監控差異](mariadb.md)。PostgreSQL 本體的監控見 [PostgreSQL 怎麼監控](../postgres.md)。
