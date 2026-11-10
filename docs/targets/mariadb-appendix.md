# 附錄：從資料庫原始碼到 metric（MariaDB）

[主頁](mariadb.md)列出每個訊號該看哪個 metric。這頁回答另一個問題：**這個 metric 真的是從那個狀態變數或那張表來的嗎？**每個訊號都追完整條鏈：

1. **資料庫端**：值在哪裡產生。MariaDB 11.4.13（`mariadb-11.4.13`）原始碼中定義狀態變數、欄位或遞增計數的那一行；本次拆卡新增的兩個訊號另外核對 10.6.28、10.11.19、11.8.9、12.3.3。
2. **Exporter 端**：mysqld_exporter v0.21.0 送哪一條 SQL、怎麼把一列結果變成 metric 名稱、哪些值被丟掉、預設有沒有開。
3. **Metric**：最後的名稱與 label。
4. **Lab 觀察**：metric 有沒有真的出現在實驗裡、哪個情境讓它動了（MariaDB 11.4.13 那一邊）。

每個原始碼參考都用 `gh api` 取 tag 上的檔案，逐行核對過關鍵字。另外做了一次輕量檢查：開 `general_log` 記下 exporter 一次 scrape 送出的所有 SQL。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-trace] 版本之間的差異見 [MariaDB 各版本的監控差異](versions/mariadb.md)；MySQL 的追蹤在[另一頁](mysql-appendix.md)。

## 先講結論

1. **Exporter 送出的 SQL 和原始碼一致**，`general_log` 記到的語句和 collector 檔案裡的常數一字不差；slave_status 的 `SHOW ALL SLAVES STATUS` 第一條就成功。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-trace][^ver-mdb-show-all-slaves]
2. **值是 NULL 或不是數字時，series 不存在，不是 0。**`Seconds_Behind_Master` 是 NULL 時 series 消失。<span class="at-chip at-ev-tested">實測</span>[^my-parse-status][^my-trace-ex-parse-float][^exp-mysql-ha]
3. **Seconds_Behind_Master 比 MySQL 更容易是 NULL。**只要 IO thread 不是在讀（`Connecting`、`Preparing`、`No`）就是 NULL，即使 relay log 還有沒套用完的事件。<span class="at-chip at-ev-code">讀碼</span>[^my-trace-mdb-sbm-null]
4. **Deadlock 有兩個來源，名稱不同。**global status 的 `Innodb_deadlocks`（預設就有）；`INNODB_METRICS` 的 `lock_deadlocks` 旗標有 `MONITOR_DISPLAY_CURRENT`，type 是 `value`，所以 exporter 不加 `_total`。<span class="at-chip at-ev-tested">實測</span>[^mariadb-innodb-deadlocks][^my-trace-mdb-deadlock-mon][^my-trace-ex-im-total]
5. **replica 的 GTID 位置有輸出，errant 交易沒有。**v0.21.0 會把 `Gtid_IO_Pos`、`Gtid_Slave_Pos` 拆成 `mysql_slave_status_gtid_slave_pos{domain_id,server_id}`；errant transaction 出現在 replica 的 `gtid_binlog_pos`（全域變數，字串），仍然看不到。<span class="at-chip at-ev-tested">實測</span>[^my-trace-ex-slave-gtid][^exp-mysql-ha]
6. **`wsrep_ready` 是編譯進去的，不是 Galera 給的。**官方映像用 `WITH_WSREP` 編譯，`wsrep_ready` 在狀態變數表裡固定存在，沒開 Galera 時是 OFF，exporter 輸出 0。<span class="at-chip at-ev-tested">實測</span>[^my-trace-mdb-wsrep-ifdef][^exp-mysql-trace]
7. **`Innodb_data_pending_fsyncs` 的來源在 10.11 換了。**10.6 是 redo log 加 tablespace 的 pending flush；10.11 起直接讀 `fil_n_pending_tablespace_flushes`。<span class="at-chip at-ev-code">讀碼</span>[^spl-mdb106-pending-fsyncs-calc][^spl-mdb1011-pending-fsyncs]

## Exporter 怎麼把一列變成 metric {#transform}

| 步驟 | 做法 | 依據 |
|---|---|---|
| 連線 | 每次 scrape 開新的連線池（最多 2 條），先送 `SELECT @@version`；失敗就記 "Error opening connection" 並讓 `mysql_up` = 0，其他 collector 都不跑 | [^my-trace-ex-maxopen][^my-trace-ex-version-query][^my-trace-ex-conn-error][^my-trace-ex-up] |
| 版本與 flavor | `@@version` 取「主版號.次版號」轉成浮點數（10.6、10.11、11.4…都大於所有門檻，所以沒有 collector 被跳過）；字串含 `mariadb` 時當 MariaDB，只影響 eventsstatements 與 innodb_tablespaces | [^ver-ex-version-float][^ver-ex-version-gate][^ver-ex-flavor][^ver-ex-tablespaces-mariadb] |
| Session 設定 | DSN 預設帶 `lock_wait_timeout=2`，每條連線先送 `SET lock_wait_timeout = 2` | [^my-trace-ex-lockwait][^exp-mysql-trace] |
| 解析值 | `yes`／`on` → 1；`no`／`off`／`disabled`／`connecting` → 0；`primary` → 1；`non-primary`／`disconnected` → 0；日期時間 → Unix 秒（沒有時區，當 UTC）；其他用 `ParseFloat`，失敗就跳過 | [^my-parse-status][^my-parse-connecting][^my-trace-ex-parse-primary][^my-trace-ex-parse-float][^ver-ex-parse-datetime] |
| 名稱 | 非 `[a-zA-Z0-9_]` 的字元換成 `_`，全部轉小寫，前面加 `mysql_<collector>_` | [^my-trace-ex-validname][^my-trace-ex-gs-generic] |
| 分組 | global_status 中 `com_`、`handler_`、`connection_errors_`、`innodb_buffer_pool_pages_`、`innodb_rows_`、`performance_schema_` 開頭的變成帶 label 的 `_total` metric；MariaDB 11.4 沒有 `Innodb_rows_*` | [^my-global-status-re][^my-trace-ex-gs-connerr][^my-perf-mdb-no-rows] |
| 型別 | 只有少數名稱標成 gauge 或 counter（`threads_running`、`uptime`、`slow_queries`、`slave_running`⋯），其他是 untyped；名稱都**不加** `_total` | [^my-trace-ex-gs-types] |
| slave_status | `SHOW ALL SLAVES STATUS` 每個欄位名稱轉小寫當 metric 名稱，label 是 `master_host`、`master_uuid`、`connection_name`；`Gtid_*_Pos` 另外拆成 domain、server、序號；沒有 replica 時 0 列，完全沒有 series | [^my-trace-ex-slave-name][^my-trace-ex-slave-labels][^my-trace-ex-slave-gtid][^my-slave-skip] |

- **字串變數一律消失。**`gtid_binlog_pos`（`0-2-645`）、`version`、`Last_SQL_Error` 都不是數字，被跳過。只有寫死在 `textItems` 裡的幾個（`version`、`wsrep_cluster_name`、`transaction_isolation` 等）會變成 `_info` 類的 label。<span class="at-chip at-ev-tested">實測</span>[^my-global-variables-parse][^exp-mysql-trace]
- **以 `.數字` 結尾的字串也被跳過**，所以 `Master_Log_File`、`Relay_Master_Log_File` 不會輸出，實驗的 replica 名稱清單裡也沒有。<span class="at-chip at-ev-tested">實測</span>[^my-trace-ex-parse-logre][^exp-mysql-ha]

## Exporter 實際送出的 SQL {#sql}

開 `general_log` 做一次 scrape。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-trace]

| Collector | 送出的 SQL（MariaDB 11.4.13） | 原始碼 |
|---|---|---|
| （連線） | `SET lock_wait_timeout = 2`、`SELECT @@version` | [^my-trace-ex-lockwait][^my-trace-ex-version-query] |
| global_status | `SHOW GLOBAL STATUS` | [^my-trace-ex-gs-query] |
| global_variables | `SHOW GLOBAL VARIABLES` | [^my-trace-ex-gv-query] |
| slave_status | `SHOW ALL SLAVES STATUS`（第一條就成功） | [^my-slave-queries][^my-trace-ex-slave-suffix] |
| info_schema.innodb_metrics | 先查 `information_schema.columns` 找欄位名，再 `SELECT name, subsystem, type, comment, count FROM information_schema.innodb_metrics` 加 `` WHERE `enabled` = '1' `` | [^my-trace-ex-im-colquery][^my-trace-ex-im-switch] |
| info_schema.innodb_trx | `SELECT trx_state, COUNT(*), COALESCE(MAX(TIMESTAMPDIFF(SECOND, trx_started, NOW())), 0) FROM information_schema.innodb_trx GROUP BY trx_state` | [^my-innodb-trx-query] |
| info_schema.processlist | `SELECT user, host, command, state, COUNT(*), SUM(time) FROM information_schema.processlist WHERE ID != connection_id() AND TIME >= 0 GROUP BY ...` | [^my-processlist-query] |
| heartbeat | ``SELECT UNIX_TIMESTAMP(ts), UNIX_TIMESTAMP(NOW(6)), server_id from `heartbeat`.`heartbeat` `` | [^my-heartbeat-query][^my-trace-ex-hb-now] |
| query_response_time | `SELECT @@query_response_time_stats`；沒裝 plugin 時查不到或是 0 就直接回成功 | [^my-trace-ex-qrt-check][^ver-ex-qrt-nil] |

- **INNODB_METRICS 的「開關」欄是整數欄 `ENABLED`**（MySQL 是字串欄 `STATUS`），所以 exporter 先查 `information_schema.columns`。[^my-trace-mdb-ism-enabled-col]
- **只取已啟用的計數器。**本頁用到的 `trx_rseg_history_len`、`lock_deadlocks` 是 `MONITOR_DEFAULT_ON`。[^my-innodb-metrics-enabled][^my-trace-mdb-histlen-mon]

## 穩定 {#stability}

| 訊號 | DB 來源（變數／表） | Exporter 查詢與轉換 | Metric | Lab 觀察 |
|---|---|---|---|---|
| 服務在不在 | 連線本身；第一條語句 `SELECT @@version` | 連不上或查詢失敗就回 0，其他 collector 不跑[^my-trace-ex-version-query][^my-trace-ex-conn-error] | `mysql_up` | 實測：停資料庫時 up 1、`mysql_up` 0，collector_success 整組消失[^exp-mysql]；連線打滿時 Error 1040（s7a）[^exp-mysql-ha] |
| 重啟 | `Uptime`[^my-trace-mdb-uptime] | `SHOW GLOBAL STATUS`，標成 gauge[^my-trace-ex-gs-types] | `mysql_global_status_uptime` | 實測：kill -9 後重啟約 15 秒時是 13（s8）[^exp-mysql-ha] |
| Undo history | `Innodb_history_list_length`：InnoDB 狀態變數，前綴 `Innodb_` 由 `SHOW_FUNC_ENTRY("Innodb")` 加上，值是 `trx_sys.history_size_approx()`[^my-trace-mdb-histlen-var][^my-trace-mdb-histlen-export][^my-trace-mdb-innodb-prefix] | `SHOW GLOBAL STATUS`，通用轉換 | `mysql_global_status_innodb_history_list_length` | 實測：consistent snapshot 擋住 purge 時 414（s2），結束後 1；開了 innodb_metrics 時 `trx_rseg_history_len` 值相同[^exp-mysql-ha] |
| 長交易 | `information_schema.INNODB_TRX.trx_started`（DATETIME）[^my-trace-mdb-trx-started] | 依 `trx_state` 分組，算 `NOW() − MAX(...)` 秒數；沒有交易時 0 列、沒有 series[^my-innodb-trx-query][^my-trace-ex-trx-desc] | `mysql_info_schema_innodb_trx_oldest_transaction_age_seconds{state}`、`..._innodb_trx_transactions{state}` | 實測：s1 沒有 series；s2 分成 `RUNNING`（14 秒）與 `LOCK WAIT`（11 秒）[^exp-mysql-ha] |
| Deadlock（狀態變數） | `Innodb_deadlocks` = `lock_sys.deadlocks`，偵測到 deadlock 時 `++`[^mariadb-innodb-deadlocks][^my-trace-mdb-deadlock-inc] | `SHOW GLOBAL STATUS`，untyped，不加 `_total` | `mysql_global_status_innodb_deadlocks` | 實測：s3 一次 deadlock 後 mdb1 從 0 到 1[^exp-mysql-ha] |
| Deadlock（INNODB_METRICS） | 直接讀 `lock_sys.deadlocks`，旗標有 `MONITOR_DISPLAY_CURRENT`，type `value`[^my-trace-mdb-deadlock-mon][^my-trace-mdb-ism-type] | type 不是 counter，當 gauge、不加 `_total`[^my-trace-ex-im-total] | `..._lock_lock_deadlocks` | 實測：s3 +1[^exp-mysql-ha][^exp-mysql-trace] |
| Replication 因錯誤停下 | `Last_SQL_Errno` = `rli->last_error().number`[^my-trace-mdb-last-sql-errno] | `SHOW ALL SLAVES STATUS`，欄名轉小寫 | `mysql_slave_status_last_sql_errno` | 實測：s5b 衝突寫入後 1062[^exp-mysql-ha] |

## 使用 {#usage}

| 訊號 | DB 來源（變數／表） | Exporter 查詢與轉換 | Metric | Lab 觀察 |
|---|---|---|---|---|
| 執行中的 thread | `Threads_running` = `STATUS_VAR.threads_running`[^my-trace-mdb-threads-running] | `SHOW GLOBAL STATUS`，標成 gauge[^my-trace-ex-gs-types] | `mysql_global_status_threads_running` | 實測：perf lab 的鎖與 CPU 情境升到 10–12[^exp-mysql-perf] |
| thread cache 失效 | `Threads_created` = `thread_created`；one-thread-per-connection 時 thread cache 接不下才 `inc_thread_created()`[^spl-mdb114-threads-created][^spl-mdb114-thread-created-inc]；`Connections` = `global_thread_id`[^spl-mdb114-connections]；`thread_cache_size` 預設 256[^spl-mdb114-thread-cache-size] | `SHOW GLOBAL STATUS`，untyped，照名稱輸出[^my-global-status-skip][^res-mx-default-gs] | `mysql_global_status_threads_created`、`mysql_global_status_connections`、`mysql_global_variables_thread_cache_size` | 讀碼；五條線都有 `Threads_created`[^spl-mdb106-threads-created][^spl-mdb1011-threads-created][^spl-mdb118-threads-created][^spl-mdb123-threads-created] |
| 曾經到過的最高連線數 | `Max_used_connections`：允許 max_connections + 1，計入 extra_port 的連線[^my-trace-mdb-max-used][^my-trace-mdb-max-used-upd][^my-trace-mdb-conn-check]；11.4 起另有 `Max_used_connections_time`[^ver-mdb114-max-used-conn-time] | `SHOW GLOBAL STATUS`，untyped | `mysql_global_status_max_used_connections` | 實測：max_connections=30 打滿後是 31（s7a）[^exp-mysql-ha] |
| 被拒的連線 | `Connection_errors_max_connections`：`ER_CON_COUNT_ERROR` 時加 1，同時加 `Aborted_connects`[^my-trace-mdb-connerr-var][^mariadb-conn-errors] | 符合 `connection_errors_` 前綴，變成 counter，後半段當 `error` label[^my-global-status-re][^my-trace-ex-gs-connerr] | `mysql_global_status_connection_errors_total{error="max_connections"}` | 實測：打滿當下 exporter 也進不來；連線降下後 +2（s7a）[^exp-mysql-ha] |
| Row lock 等待 | `Innodb_row_lock_current_waits` = `lock_sys.get_wait_pending()`[^my-trace-mdb-rowlock-var][^my-trace-mdb-rowlock-export] | `SHOW GLOBAL STATUS`；前綴 `Innodb_` 由 InnoDB 的 `"Innodb"` 項目加上[^my-trace-mdb-innodb-prefix] | `mysql_global_status_innodb_row_lock_current_waits` | 實測：s2 一個交易等鎖時是 1[^exp-mysql-ha] |
| fsync 排隊 | `Innodb_data_pending_fsyncs`：10.6 = `log_sys.get_pending_flushes() + fil_n_pending_tablespace_flushes`[^spl-mdb106-pending-fsyncs][^spl-mdb106-pending-fsyncs-calc]；10.11、11.4、11.8、12.3 = `fil_n_pending_tablespace_flushes`（tablespace fsync 開始時 `++`）[^spl-mdb1011-pending-fsyncs][^spl-mdb114-pending-fsyncs][^spl-mdb118-pending-fsyncs][^spl-mdb123-pending-fsyncs][^spl-mdb114-tablespace-flush-inc] | `SHOW GLOBAL STATUS`，untyped | `mysql_global_status_innodb_data_pending_fsyncs` | 讀碼 |
| 慢查詢 | `Slow_queries` = `STATUS_VAR.long_query_count`[^my-trace-mdb-slow] | `SHOW GLOBAL STATUS`，標成 counter 但名稱**沒有** `_total`[^my-trace-ex-gs-types] | `mysql_global_status_slow_queries` | 實測：`SLEEP(30)` 也計入（s2 摘要）[^exp-mysql-ha] |
| Metadata lock 等待 | `information_schema.PROCESSLIST.state`（"Waiting for table metadata lock"）[^my-perf-mdb-mdl-state] | 依 user、host、command、state 分組；state 轉小寫、空白換 `_`[^my-processlist-query][^my-trace-ex-pl-sanitize] | `mysql_info_schema_processlist_threads{command,state="waiting_for_table_metadata_lock"}` | 實測：s4a replica 上 1[^exp-mysql-ha] |
| binlog 不刪（11.4 起） | purge 前檢查「處理過這個 binlog 的 replica 少於 `slave_connections_needed_for_purge`」就跳過[^spl-mdb114-purge-check][^ver-mdb114-purge-needed]；`Slaves_connected` = binlog dump thread 數[^spl-mdb114-slaves-connected] | `SHOW GLOBAL VARIABLES`、`SHOW GLOBAL STATUS` | `mysql_global_variables_slave_connections_needed_for_purge`、`mysql_global_status_slaves_connected` | 實測：expire 到期後檔案數沒有減少[^my-asp-exp] |

## HA {#ha}

| 訊號 | DB 來源（變數／表） | Exporter 查詢與轉換 | Metric | Lab 觀察 |
|---|---|---|---|---|
| IO thread | 欄名 `Slave_IO_Running`，值 `No`／`Connecting`／`Preparing`／`Yes`[^my-trace-mdb-io-running-vals]；五條線欄名相同[^ver-mdb106-sbm-col][^ver-mdb114-sbm-col] | 欄名轉小寫；`Yes` → 1，`No`／`Connecting` → 0；**`Preparing` 不在表裡，會被跳過**[^my-parse-connecting][^my-trace-ex-slave-name] | `mysql_slave_status_slave_io_running` | 實測：s4b 停 IO thread 時 0；s8 primary 被 kill 時 0、`last_io_errno` = 2003。`Preparing` 未觀察到[^exp-mysql-ha] |
| SQL thread | `Slave_SQL_Running`（`Yes`／`No`） | 同上 | `mysql_slave_status_slave_sql_running` | 實測：s5a STOP SQL_THREAD 時 0[^exp-mysql-ha] |
| 延遲（伺服器算的） | `Seconds_Behind_Master`：IO thread 不是 `Yes` 或 SQL thread 停時就 NULL[^my-trace-mdb-sbm-field][^my-trace-mdb-sbm-null] | NULL 讀成空字串，`ParseFloat` 失敗，整條 series 不輸出[^my-slave-skip][^my-trace-ex-parse-float] | `mysql_slave_status_seconds_behind_master` | 實測：s4b、s5a、s5b 都**消失**，同時 heartbeat 延遲 21–29 秒[^exp-mysql-ha] |
| 延遲（11.8 起） | `information_schema.SLAVE_STATUS` 產生的 `Master_last_event_time`、`Slave_last_event_time`、`Master_Slave_time_diff`，只在 `SHOW ALL SLAVES STATUS` 出現[^ver-mdb118-is-slave-status][^ver-mdb118-last-event-time][^ver-mdb118-all-only-cols] | datetime 轉 Unix 秒（當 UTC）[^ver-ex-parse-datetime] | `mysql_slave_status_master_slave_time_diff` 等 | 讀碼（lab 是 11.4） |
| 延遲（heartbeat） | 不是資料庫內建。由外部程式（pt-heartbeat 或 EVENT）在 primary 寫 `ts`、`server_id`，經 binlog 複製到 replica | `SELECT UNIX_TIMESTAMP(ts), UNIX_TIMESTAMP(NOW(6)), server_id`；預設 `heartbeat`.`heartbeat`[^my-heartbeat-query][^my-trace-ex-hb-schema][^my-trace-ex-hb-now][^my-trace-ex-hb-default] | `mysql_heartbeat_now_timestamp_seconds{server_id}`、`mysql_heartbeat_stored_timestamp_seconds{server_id}` | 實測：s1 差值 0.4–0.6 秒；s4b IO 停時 29 秒；s4c 寫入程式停時每台一起變大[^exp-mysql-ha] |
| 能不能寫 | `read_only`（布林）；沒有 `super_read_only`[^my-trace-mdb-readonly] | `SHOW GLOBAL VARIABLES`，`ON`／`OFF` → 1／0[^my-global-variables-parse] | `mysql_global_variables_read_only` | 實測：s1 primary 0、replica 1；s9 升級後 0[^exp-mysql-ha] |
| Semi-sync | 伺服器內建 `Rpl_semi_sync_master_status`，一直存在[^my-trace-mdb-semisync-status][^ver-mdb123-semisync-master] | `SHOW GLOBAL STATUS`，通用轉換 | `mysql_global_status_rpl_semi_sync_master_status`，另有 `_clients`、`_no_tx` | 實測：s1 是 0；s6 replica 開 semi-sync 後 1、clients 1[^exp-mysql-ha] |
| Galera 就緒 | `wsrep_ready`：在 `#ifdef WITH_WSREP` 區塊內，值由 `wsrep_ready_get()` 給，沒開 Galera 時 OFF[^my-trace-mdb-wsrep-ifdef][^my-trace-mdb-wsrep-ready] | `SHOW GLOBAL STATUS`，`OFF` → 0；`wsrep_cluster_status` 的 `Primary`／`Disconnected` 也會轉成 1／0[^my-trace-ex-parse-primary] | `mysql_global_status_wsrep_ready`、`mysql_global_variables_wsrep_on` | 實測：沒有 Galera 的 mdb1／mdb2 都是 `wsrep_ready` = 0、`wsrep_on` = 0[^exp-mysql-ha][^exp-mysql-trace] |
| Galera quorum | `wsrep_cluster_status`（Primary、non-Primary、Disconnected）、`wsrep_cluster_size`[^my-asp-mdb-wsrep-status-var][^my-asp-mdb-wsrep-primary][^my-asp-mdb-wsrep-nonprimary][^my-asp-mdb-wsrep-size-set] | 字串轉 1／0[^my-asp-ex-parse-nonprimary][^my-asp-ex-gs-wsrep-status] | `mysql_global_status_wsrep_cluster_status`、`..._cluster_size` | 實測：三台暫停兩台時 gal1 是 0、size 1[^my-asp-exp] |
| GTID 內容 | 全域變數 `gtid_binlog_pos`（字串）[^my-trace-mdb-gtid-binlog-pos]；`SHOW ALL SLAVES STATUS` 的 `Gtid_IO_Pos`、`Gtid_Slave_Pos`[^my-trace-mdb-gtid-slave-pos] | 全域變數被跳過；`Gtid_*_Pos` 另外按逗號與 `-` 拆成 domain、server、序號[^my-trace-ex-slave-gtid] | `mysql_slave_status_gtid_slave_pos{domain_id,server_id}`、`..._gtid_io_pos{...}`（只在 replica） | 實測：mdb2 名稱清單有 `gtid_slave_pos`、`gtid_io_pos`；s5b 的 errant GTID 在 `gtid_binlog_pos`（`0-2-645`），不在 `Gtid_Slave_Pos`（`0-1-644`），所以仍看不到[^exp-mysql-ha] |
| 權限 | `SHOW SLAVE STATUS` 要 `SLAVE MONITOR`（10.6／10.11 的 `SUPER` 也可以，11.0 起不行）；`REPLICATION CLIENT` 是 `BINLOG MONITOR` 的別名[^ver-mdb1011-slave-status-priv][^ver-mdb114-slave-status-priv][^ver-mdb-repl-client-alias] | 三種語法都失敗時回傳最後一個錯誤，collector 0[^ver-ex-slave-loop] | `mysql_exporter_collector_success{collector="collect.slave_status"}` | 實測：收回後 Error 1227[^exp-mysql] |

## 陷阱 {#pitfalls}

- **Seconds_Behind_Master 是 NULL 時 series 消失**，而且比 MySQL 更常是 NULL。延遲規則用 heartbeat；thread 停下由 `slave_*_running == 0` 負責。[^my-trace-mdb-sbm-null][^my-trace-ex-parse-float]
- **`Preparing` 會讓 IO thread series 短暫消失。**這是 IO thread 連上 primary 後、開始讀之前的狀態；`parseStatus` 不認得，series 那一次 scrape 不存在。影響是推論，實驗沒有抓到這個狀態。<span class="at-chip at-ev-inferred">推論</span>[^my-trace-mdb-io-running-vals][^my-parse-status]
- **`wsrep_ready` 在每台 MariaDB 都存在。**`wsrep_ready != 1` 要配 `wsrep_on == 1`。[^my-trace-mdb-wsrep-ifdef]
- **`perf_schema.replication_applier_status_by_worker` 一定失敗。**MariaDB 的表沒有 exporter 要的欄位，版本門檻又擋不住。[^ver-mdb-pfs-worker-cols][^ver-ex-applier-version]
- **字串變數不輸出。**errant GTID、`Last_SQL_Error` 的文字、binlog 檔名都看不到，要另外查。[^my-global-variables-parse][^my-trace-ex-parse-logre]

## 沒有釘到原始碼行的部分 {#unpinned}

| 說法 | 現在的依據 | 等級 |
|---|---|---|
| pt-heartbeat 的表格式是 `ts VARCHAR(26)`、`server_id` | exporter 的註解[^my-trace-ex-hb-schema]與本次輕量檢查；pt-heartbeat 原始碼沒有讀 | 推論 |
| 11.8 事件時間的時區偏移 | exporter 當 UTC 解析；伺服器時區不是 UTC 時會差一個偏移 | 推論 |
| 五條線的 wsrep 變數 | 只確認 `wsrep_on` 都有 | 未逐版比對 |

## 實驗 {#lab}

- 單台情境：[`lab/targets/mysql/`](https://github.com/HahaSula/alert-trust/tree/main/lab/targets/mysql) 根目錄，結果 `results/run-20261108.md`。[^exp-mysql]
- 主要情境：[`lab/targets/mysql/ha/`](https://github.com/HahaSula/alert-trust/tree/main/lab/targets/mysql/ha)，結果 `results/run-20261108.md`，MariaDB 節點 s1 基線時的 metric 名稱 `results/names-{mdb1,mdb2}.txt`。[^exp-mysql-ha]
- SQL 追蹤：[`lab/targets/mysql/trace/`](https://github.com/HahaSula/alert-trust/tree/main/lab/targets/mysql/trace)，結果 `results/general-log-20261109.md`。[^exp-mysql-trace]
