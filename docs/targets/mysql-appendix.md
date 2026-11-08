# 附錄：從資料庫原始碼到 metric（MySQL／MariaDB）

[主頁](mysql.md)列出每個訊號該看哪個 metric。這頁回答另一個問題：**這個 metric 真的是從那個狀態變數或那張表來的嗎？**每個訊號都追完整條鏈：

1. **資料庫端**：值在哪裡產生。MySQL 8.4.7（`mysql-8.4.7`）、MariaDB 11.4.13（`mariadb-11.4.13`）原始碼中定義狀態變數、欄位或遞增計數的那一行。
2. **Exporter 端**：mysqld_exporter v0.21.0 送哪一條 SQL、怎麼把一列結果變成 metric 名稱、哪些值被丟掉、預設有沒有開。
3. **Metric**：最後的名稱與 label。
4. **Lab 觀察**：metric 有沒有真的出現在實驗裡、哪個情境讓它動了。

每個原始碼參考都用 `gh api` 取 tag 上的檔案，逐行核對過關鍵字。另外做了一次輕量檢查：開 `general_log` 記下 exporter 一次 scrape 送出的所有 SQL。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-trace] Lab 欄的「實測」只用在親眼看到的情況；只讀過原始碼的標「讀碼」，推論標「推論」。

## 先講結論

1. **Exporter 送出的 SQL 和原始碼一致。**一次 scrape 中成功執行的每一條語句都在 `general_log` 裡，和 collector 檔案裡的常數一字不差（MySQL 的 `general_log` 不記失敗的語句，那部分改用錯誤計數確認）（見[下一節](#sql)）。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-trace]
2. **MySQL 8.4 每次 scrape 先失敗 8 次才拿到 replica 狀態。**slave_status 依序試 `SHOW ALL SLAVES STATUS`、`SHOW SLAVE STATUS`，每個再加三種後綴，最後才是 `SHOW REPLICA STATUS`。exporter 帳號的 ER_PARSE_ERROR 每次 scrape 加 8；`general_log` 不記這些失敗的語句。MariaDB 第一條就成功。<span class="at-chip at-ev-tested">實測</span>[^my-trace-ex-slave-loop][^exp-mysql-trace]
3. **值是 NULL 或不是數字時，series 不存在，不是 0。**所有「通用」collector（global_status、global_variables、slave_status）都用同一個 `parseStatus`：只認 Yes／No／ON／OFF 等幾個字和數字，其餘（含 NULL）直接跳過。`Seconds_Behind_*` 是 NULL 時 series 消失，就是這個原因。<span class="at-chip at-ev-tested">實測</span>[^my-parse-status][^my-trace-ex-parse-float][^exp-mysql-ha]
4. **MariaDB 的 Seconds_Behind_Master 比 MySQL 更容易是 NULL。**MySQL 只在 SQL thread 停、或 IO thread 停而且 SQL thread 已追完時給 NULL；MariaDB 只要 IO thread 不是在讀（`Connecting`、`Preparing`、`No`）就是 NULL，即使 relay log 還有沒套用完的事件。<span class="at-chip at-ev-code">讀碼</span>[^mysql-sbs-null][^my-trace-mdb-sbm-null]
5. **Deadlock 名稱差一個 `_total`，原因在 InnoDB 的旗標。**exporter 對 `INNODB_METRICS.type` 是 `counter` 或 `status_counter` 的計數器加 `_total`。MySQL 的 `lock_deadlocks` 沒有旗標，type 是 `counter`；MariaDB 的有 `MONITOR_DISPLAY_CURRENT`，type 是 `value`。<span class="at-chip at-ev-tested">實測</span>[^my-trace-ex-im-total][^my-trace-my-deadlock-mon][^my-trace-mdb-deadlock-mon]
6. **MariaDB replica 的 GTID 位置其實有輸出。**v0.21.0 會把 `SHOW ALL SLAVES STATUS` 的 `Gtid_IO_Pos`、`Gtid_Slave_Pos` 拆成 `mysql_slave_status_gtid_slave_pos{domain_id,server_id}`。但 errant transaction 出現在 replica 的 `gtid_binlog_pos`（全域變數，字串），仍然看不到。<span class="at-chip at-ev-tested">實測</span>[^my-trace-ex-slave-gtid][^exp-mysql-ha]
7. **`wsrep_ready` 是編譯進去的，不是 Galera 給的。**MariaDB 官方映像用 `WITH_WSREP` 編譯，`wsrep_ready` 在狀態變數表裡固定存在，沒開 Galera 時是 OFF，exporter 輸出 0。<span class="at-chip at-ev-tested">實測</span>[^my-trace-mdb-wsrep-ifdef][^exp-mysql-trace]

## Exporter 怎麼把一列變成 metric {#transform}

global_status、global_variables、slave_status 三個預設 collector 不寫死 metric 清單，資料庫回什麼就輸出什麼，所以名稱完全由資料庫決定。規則如下。

| 步驟 | 做法 | 依據 |
|---|---|---|
| 連線 | 每次 scrape 開新的連線池（最多 2 條），先送 `SELECT @@version`；失敗就記 "Error opening connection" 並讓 `mysql_up` = 0，其他 collector 都不跑 | [^my-trace-ex-maxopen][^my-trace-ex-version-query][^my-trace-ex-conn-error][^my-trace-ex-up] |
| Session 設定 | DSN 預設帶 `lock_wait_timeout=2`，每條連線先送 `SET lock_wait_timeout = 2`（`general_log` 每次出現 2 次，對應 2 條連線） | [^my-trace-ex-lockwait][^exp-mysql-trace] |
| 解析值 | `yes`／`on` → 1；`no`／`off`／`disabled`／`connecting` → 0；`primary` → 1；`non-primary`／`disconnected` → 0；日期時間 → Unix 秒；其他用 `ParseFloat`，失敗就跳過。NULL 讀成空字串，`ParseFloat("")` 失敗，也跳過 | [^my-parse-status][^my-parse-connecting][^my-trace-ex-parse-primary][^my-trace-ex-parse-float] |
| 名稱 | 非 `[a-zA-Z0-9_]` 的字元換成 `_`，全部轉小寫，前面加 `mysql_<collector>_`（例：`Threads_running` → `mysql_global_status_threads_running`） | [^my-trace-ex-validname][^my-trace-ex-gs-generic] |
| 分組 | global_status 中 `com_`、`handler_`、`connection_errors_`、`innodb_buffer_pool_pages_`、`innodb_rows_`、`performance_schema_` 開頭的變成帶 label 的 `_total` metric（例：`connection_errors_total{error}`） | [^my-global-status-re][^my-trace-ex-gs-connerr] |
| 型別 | 只有少數名稱標成 gauge 或 counter（`threads_running`、`uptime`、`slow_queries`⋯），其他是 untyped；名稱都**不加** `_total`，所以 `slow_queries` 是 counter 但沒有 `_total` | [^my-trace-ex-gs-types] |
| slave_status | 每個欄位名稱轉小寫當 metric 名稱，label 是 `master_host`、`master_uuid`、`channel_name`、`connection_name`；沒有 replica 時查詢回 0 列，所以完全沒有 series | [^my-trace-ex-slave-name][^my-trace-ex-slave-labels][^my-slave-skip] |

- **字串變數一律消失。**`gtid_executed`（`uuid:1-637`）、`gtid_binlog_pos`（`0-2-645`）、`version`、`Last_SQL_Error` 都不是數字，被跳過。只有寫死在 `textItems` 裡的幾個（`version`、`wsrep_cluster_name`、`transaction_isolation` 等）會變成 `_info` 類的 label。<span class="at-chip at-ev-tested">實測</span>：兩台的 `mysql_global_variables_gtid_executed`、`..._gtid_binlog_pos`、`..._version` 數量都是 0。[^my-global-variables-parse][^exp-mysql-trace]
- **以 `.數字` 結尾的字串也被跳過。**`parseStatus` 想把 `mysql-bin.000003` 解析成 3，但 `logRE.Find` 回傳整段比對結果，再拿去 `ParseFloat` 一定失敗。所以 `Master_Log_File`、`Relay_Master_Log_File` 不會輸出，實驗的 replica 名稱清單裡也沒有。<span class="at-chip at-ev-tested">實測</span>[^my-trace-ex-parse-logre][^exp-mysql-ha]

## Exporter 實際送出的 SQL {#sql}

開 `general_log` 做一次 scrape（collector：global_status、global_variables、slave_status、info_schema.innodb_metrics、info_schema.innodb_trx、info_schema.processlist、heartbeat，加上預設開的 innodb_cmp、innodb_cmpmem、query_response_time）。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-trace]

| Collector | 送出的 SQL（MariaDB 11.4.13） | 送出的 SQL（MySQL 8.4.7） | 原始碼 |
|---|---|---|---|
| （連線） | `SET lock_wait_timeout = 2`、`SELECT @@version` | 同左 | [^my-trace-ex-lockwait][^my-trace-ex-version-query] |
| global_status | `SHOW GLOBAL STATUS` | 同左 | [^my-trace-ex-gs-query] |
| global_variables | `SHOW GLOBAL VARIABLES` | 同左 | [^my-trace-ex-gv-query] |
| slave_status | `SHOW ALL SLAVES STATUS`（第一條就成功） | 8 條語法錯誤後 `SHOW REPLICA STATUS` | [^my-slave-queries][^my-trace-ex-slave-suffix] |
| info_schema.innodb_metrics | 先查 `information_schema.columns` 找欄位名，再 `SELECT name, subsystem, type, comment, count FROM information_schema.innodb_metrics` 加 `` WHERE `enabled` = '1' `` | 同上，但條件是 `` WHERE `status` = 'enabled' `` | [^my-trace-ex-im-colquery][^my-trace-ex-im-switch] |
| info_schema.innodb_trx | `SELECT trx_state, COUNT(*), COALESCE(MAX(TIMESTAMPDIFF(SECOND, trx_started, NOW())), 0) FROM information_schema.innodb_trx GROUP BY trx_state` | 同左 | [^my-innodb-trx-query] |
| info_schema.processlist | `SELECT user, host, command, state, COUNT(*), SUM(time) FROM information_schema.processlist WHERE ID != connection_id() AND TIME >= 0 GROUP BY ...` | 同左 | [^my-processlist-query] |
| heartbeat | ``SELECT UNIX_TIMESTAMP(ts), UNIX_TIMESTAMP(NOW(6)), server_id from `heartbeat`.`heartbeat` `` | 同左 | [^my-heartbeat-query][^my-trace-ex-hb-now] |
| query_response_time | `SELECT @@query_response_time_stats` | 同一條，但 MySQL 沒有這個變數，查詢失敗；collector 把失敗當成「沒有資料」，仍回報成功（`general_log` 沒記到） | [^my-trace-ex-qrt-check] |

- **INNODB_METRICS 的「開關」欄名兩邊不同。**MySQL 是字串欄 `STATUS`（`enabled`／`disabled`），MariaDB 是整數欄 `ENABLED`。exporter 先查 `information_schema.columns` 決定用哪一個，所以多一條查詢。[^my-trace-my-ism-status-col][^my-trace-mdb-ism-enabled-col]
- **只取已啟用的計數器。**沒啟用的 INNODB_METRICS 計數器完全不輸出；本頁用到的 `trx_rseg_history_len`、`lock_deadlocks` 在兩邊都是 `MONITOR_DEFAULT_ON`。[^my-innodb-metrics-enabled][^my-trace-my-histlen-mon][^my-trace-mdb-histlen-mon]

## 穩定 {#stability}

| 訊號 | DB 來源（變數／表） | Exporter 查詢與轉換 | Metric | Lab 觀察 |
|---|---|---|---|---|
| 服務在不在 | 連線本身；第一條語句 `SELECT @@version` | 連不上或查詢失敗就回 0，其他 collector 不跑[^my-trace-ex-version-query][^my-trace-ex-conn-error] | `mysql_up` | 實測：連線打滿時 exporter log 是 `exporter.go:186 ... Error 1040`，`mysql_up` = 0（s7a）[^exp-mysql-ha] |
| 重啟 | `Uptime` = 查詢時間 − `server_start_time`。MySQL[^my-trace-my-uptime][^my-trace-my-uptime-fn]；MariaDB[^my-trace-mdb-uptime] | `SHOW GLOBAL STATUS`，標成 gauge[^my-trace-ex-gs-types] | `mysql_global_status_uptime` | 實測：kill -9 後重啟約 15 秒時是 13／16（s8）[^exp-mysql-ha] |
| Undo history（MariaDB） | `Innodb_history_list_length`：InnoDB 狀態變數，前綴 `Innodb_` 由 `SHOW_FUNC_ENTRY("Innodb")` 加上，值是 `trx_sys.history_size_approx()`[^my-trace-mdb-histlen-var][^my-trace-mdb-histlen-export][^my-trace-mdb-innodb-prefix] | `SHOW GLOBAL STATUS`，通用轉換 | `mysql_global_status_innodb_history_list_length` | 實測：只在 mdb1／mdb2 的名稱清單裡；consistent snapshot 擋住 purge 時 414（s2）[^exp-mysql-ha] |
| Undo history（MySQL） | MySQL 8.4 的 InnoDB 狀態變數表沒有 `history_list_length`；只有 `INNODB_METRICS` 的 `trx_rseg_history_len`（值是 `trx_sys->rseg_history_len`，type `value`）[^my-trace-my-histlen-mon][^my-trace-my-histlen-val] | `info_schema.innodb_metrics`（預設關，要 PROCESS）；名稱 `innodb_metrics_` + subsystem + `_` + name；type 是 `value` 所以不加 `_total`[^my-innodb-metrics-name][^my-trace-ex-im-total] | `mysql_info_schema_innodb_metrics_transaction_trx_rseg_history_len` | 實測：四台都有（MariaDB 也有這個計數器）；my1 在 s2 是 434，交易結束後仍是 500[^exp-mysql-ha] |
| 長交易 | `information_schema.INNODB_TRX.trx_started`（DATETIME）。MySQL[^my-trace-my-trx-started]；MariaDB[^my-trace-mdb-trx-started] | 依 `trx_state` 分組，算 `NOW() − MAX(...)` 秒數；沒有交易時 0 列、沒有 series[^my-innodb-trx-query][^my-trace-ex-trx-desc] | `mysql_info_schema_innodb_trx_oldest_transaction_age_seconds{state}`、`..._innodb_trx_transactions{state}` | 實測：s1 沒有 series；s2 分成 `RUNNING`（14／16 秒）與 `LOCK WAIT`（11／14 秒）[^exp-mysql-ha] |
| Deadlock（MariaDB 狀態變數） | `Innodb_deadlocks` = `lock_sys.deadlocks`，偵測到 deadlock 時 `++`[^mariadb-innodb-deadlocks][^my-trace-mdb-deadlock-inc] | `SHOW GLOBAL STATUS`，untyped，不加 `_total` | `mysql_global_status_innodb_deadlocks` | 實測：只有 MariaDB；s3 一次 deadlock 後 mdb1 從 0 到 1[^exp-mysql-ha] |
| Deadlock（INNODB_METRICS） | `lock_deadlocks`：MySQL 在 `lock0wait.cc` 用 `MONITOR_INC`，type `counter`[^my-trace-my-deadlock-mon][^my-trace-my-deadlock-inc][^my-trace-my-ism-type]；MariaDB 直接讀 `lock_sys.deadlocks`，旗標有 `MONITOR_DISPLAY_CURRENT`，type `value`[^my-trace-mdb-deadlock-mon][^my-trace-mdb-ism-type] | `counter`／`status_counter` 且值 ≥ 0 才加 `_total`，其他當 gauge[^my-trace-ex-im-total] | MySQL：`..._lock_lock_deadlocks_total`；MariaDB：`..._lock_lock_deadlocks` | 實測：s3 兩邊各 +1，名稱差 `_total`；輕量檢查也一樣[^exp-mysql-ha][^exp-mysql-trace] |
| Replication 因錯誤停下 | `Last_SQL_Errno` = `rli->last_error().number`。MySQL[^my-trace-my-last-sql-errno]；MariaDB[^my-trace-mdb-last-sql-errno] | `SHOW REPLICA STATUS`／`SHOW ALL SLAVES STATUS`，欄名轉小寫 | `mysql_slave_status_last_sql_errno` | 實測：s5b 衝突寫入後兩邊都是 1062[^exp-mysql-ha] |

## 使用 {#usage}

| 訊號 | DB 來源（變數／表） | Exporter 查詢與轉換 | Metric | Lab 觀察 |
|---|---|---|---|---|
| 執行中的 thread | `Threads_running`。MySQL：`Global_THD_manager::get_num_thread_running()`[^my-trace-my-threads-running][^my-trace-my-threads-running-fn]；MariaDB：`STATUS_VAR.threads_running`[^my-trace-mdb-threads-running] | `SHOW GLOBAL STATUS`，標成 gauge[^my-trace-ex-gs-types] | `mysql_global_status_threads_running` | 實測：四台名稱清單都有；輕量檢查是 1／3。沒有情境刻意推高它[^exp-mysql-ha][^exp-mysql-trace] |
| 曾經到過的最高連線數 | `Max_used_connections`：MySQL 在 `check_and_incr_conn_count` 先檢查再加 1，所以最多 max_connections + 1[^my-trace-my-max-used][^my-trace-my-max-used-upd][^mysql-conn-reserved]；MariaDB 同樣允許 max_connections + 1，計入 extra_port 的連線[^my-trace-mdb-max-used][^my-trace-mdb-max-used-upd][^my-trace-mdb-conn-check] | `SHOW GLOBAL STATUS`，untyped | `mysql_global_status_max_used_connections` | 實測：max_connections=30 打滿後兩邊都是 31（s7a）[^exp-mysql-ha] |
| 被拒的連線 | `Connection_errors_max_connections`。MySQL：超過上限時 `m_connection_errors_max_connection++`（兩處）[^my-trace-my-connerr-var][^my-trace-my-connerr-inc][^my-trace-my-connerr-inc2]；MariaDB：`ER_CON_COUNT_ERROR` 時加 1，同時加 `Aborted_connects`[^my-trace-mdb-connerr-var][^mariadb-conn-errors] | 符合 `connection_errors_` 前綴，變成 counter，後半段當 `error` label[^my-global-status-re][^my-trace-ex-gs-connerr] | `mysql_global_status_connection_errors_total{error="max_connections"}` | 實測：打滿當下 exporter 也進不來；連線降下後 MariaDB +2、MySQL +10（s7a）[^exp-mysql-ha] |
| 連線數 | `Threads_connected`（connection_count） | `SHOW GLOBAL STATUS`，gauge | `mysql_global_status_threads_connected` | 實測：s7a 打滿時 series 消失，s7b 給 CONNECTION_ADMIN 後回來（MySQL 31）[^exp-mysql-ha] |
| Row lock 等待 | `Innodb_row_lock_current_waits`。MySQL：`srv_stats.n_lock_wait_current_count`，開始等鎖時 `inc()`[^my-trace-my-rowlock-var][^my-trace-my-rowlock-export][^my-trace-my-rowlock-inc]；MariaDB：`lock_sys.get_wait_pending()`[^my-trace-mdb-rowlock-var][^my-trace-mdb-rowlock-export] | `SHOW GLOBAL STATUS`；前綴 `Innodb_` 由 InnoDB 的 `"Innodb"` 項目加上[^my-trace-my-innodb-prefix][^my-trace-mdb-innodb-prefix] | `mysql_global_status_innodb_row_lock_current_waits` | 實測：s2 一個交易等鎖時兩邊都是 1[^exp-mysql-ha] |
| 慢查詢 | `Slow_queries` = `STATUS_VAR.long_query_count`。MySQL[^my-trace-my-slow]；MariaDB[^my-trace-mdb-slow] | `SHOW GLOBAL STATUS`，標成 counter 但名稱**沒有** `_total`[^my-trace-ex-gs-types] | `mysql_global_status_slow_queries` | 實測：`SLEEP(30)` 也計入（s2 摘要）[^exp-mysql-ha] |
| Metadata lock 等待 | `information_schema.PROCESSLIST.state` | 依 user、host、command、state 分組；state 轉小寫、空白換 `_`、去掉標點[^my-processlist-query][^my-trace-ex-pl-sanitize] | `mysql_info_schema_processlist_threads{command,state="waiting_for_table_metadata_lock"}` | 實測：s4a 兩台 replica 各 1[^exp-mysql-ha] |

## HA {#ha}

| 訊號 | DB 來源（變數／表） | Exporter 查詢與轉換 | Metric | Lab 觀察 |
|---|---|---|---|---|
| IO thread | MySQL 8.4 欄名 `Replica_IO_Running`，值 `Yes`／`Connecting`／`No`[^my-trace-my-io-running-field][^my-trace-my-io-running-val]；MariaDB 欄名 `Slave_IO_Running`，值 `No`／`Connecting`／`Preparing`／`Yes`[^my-trace-mdb-io-running-vals] | 欄名轉小寫；`Yes` → 1，`No`／`Connecting` → 0；**`Preparing` 不在表裡，會被跳過**[^my-parse-connecting][^my-trace-ex-slave-name] | MySQL：`mysql_slave_status_replica_io_running`；MariaDB：`mysql_slave_status_slave_io_running` | 實測：s4b 停 IO thread 時兩邊 0；s8 primary 被 kill 時 0、`last_io_errno` = 2003。`Preparing` 未觀察到[^exp-mysql-ha] |
| SQL thread | `Replica_SQL_Running`／`Slave_SQL_Running`（`Yes`／`No`） | 同上 | `..._replica_sql_running`／`..._slave_sql_running` | 實測：s5a STOP SQL_THREAD 時 0[^exp-mysql-ha] |
| 延遲（伺服器算的） | `Seconds_Behind_Source`（MySQL）＝ now − 正在套用事件的 primary 時間戳 − 時鐘差；SQL thread 停、或 IO 停且已追完時 NULL[^my-trace-my-sbs-field][^mysql-sbs-null][^mysql-sbs-compute][^my-trace-my-sbs-null2]。`Seconds_Behind_Master`（MariaDB）：IO thread 不是 `Yes` 或 SQL thread 停時就 NULL[^my-trace-mdb-sbm-field][^my-trace-mdb-sbm-null] | NULL 讀成空字串，`ParseFloat` 失敗，整條 series 不輸出[^my-slave-skip][^my-trace-ex-parse-float] | `mysql_slave_status_seconds_behind_source`／`..._seconds_behind_master` | 實測：s4b、s5a、s5b 兩邊都**消失**，同時 heartbeat 延遲 21–29 秒[^exp-mysql-ha] |
| 延遲（heartbeat） | 不是資料庫內建。由外部程式（pt-heartbeat 或 EVENT）在 primary 寫 `ts`、`server_id`，經 binlog 複製到 replica | `SELECT UNIX_TIMESTAMP(ts), UNIX_TIMESTAMP(NOW(6)), server_id`；欄名寫死；預設 `heartbeat`.`heartbeat`；`--collect.heartbeat.utc` 改用 `UTC_TIMESTAMP(6)`[^my-heartbeat-query][^my-trace-ex-hb-schema][^my-trace-ex-hb-now][^my-trace-ex-hb-default] | `mysql_heartbeat_now_timestamp_seconds{server_id}`、`mysql_heartbeat_stored_timestamp_seconds{server_id}` | 實測：s1 差值 0.4–0.7 秒；s4b IO 停時 27–29 秒；s4c 寫入程式停時四台一起變大[^exp-mysql-ha]。`VARCHAR(26)` 的 pt-heartbeat 格式也能解析[^exp-mysql-trace] |
| 能不能寫 | `read_only`（兩邊都是布林）；`super_read_only` 只有 MySQL。MySQL[^my-trace-my-readonly][^my-trace-my-super-readonly]；MariaDB[^my-trace-mdb-readonly] | `SHOW GLOBAL VARIABLES`，`ON`／`OFF` → 1／0[^my-global-variables-parse] | `mysql_global_variables_read_only`、`..._super_read_only` | 實測：s1 primary 0、replica 1；`super_read_only` 只在 my1／my2 的名稱清單；s9 升級後 0[^exp-mysql-ha] |
| Semi-sync | MySQL：plugin 註冊 `Rpl_semi_sync_source_status`（SHOW_BOOL），沒裝 plugin 就沒有這個變數[^my-trace-my-semisync-prefix][^my-trace-my-semisync-status]；MariaDB：伺服器內建 `Rpl_semi_sync_master_status`，一直存在[^my-trace-mdb-semisync-status] | `SHOW GLOBAL STATUS`，通用轉換 | `mysql_global_status_rpl_semi_sync_source_status`／`..._master_status`，另有 `_clients`、`_no_tx` | 實測：s1 只有 MariaDB 有（0）；s6 MySQL 裝 plugin 後 1，ACK 逾時後 0[^exp-mysql-ha] |
| Galera 就緒 | `wsrep_ready`：在 `#ifdef WITH_WSREP` 區塊內，值由 `wsrep_ready_get()` 給，沒開 Galera 時 OFF[^my-trace-mdb-wsrep-ifdef][^my-trace-mdb-wsrep-ready] | `SHOW GLOBAL STATUS`，`OFF` → 0；`wsrep_cluster_status` 的 `Primary`／`Disconnected` 也會轉成 1／0[^my-trace-ex-parse-primary] | `mysql_global_status_wsrep_ready`、`mysql_global_variables_wsrep_on` | 實測：沒有 Galera 的 mdb1／mdb2 都是 `wsrep_ready` = 0、`wsrep_on` = 0；MySQL 沒有這組[^exp-mysql-ha][^exp-mysql-trace] |
| GTID 開關 | `gtid_mode`（MySQL，`ON`／`OFF`） | `SHOW GLOBAL VARIABLES`，`ON`／`OFF` → 1／0 | `mysql_global_variables_gtid_mode` | 實測：只在 my1／my2，值 1[^exp-mysql-ha] |
| GTID 內容（MySQL） | `gtid_executed` 全域變數、`SHOW REPLICA STATUS` 的 `Executed_Gtid_Set`，都是字串[^my-trace-my-gtid-executed][^my-trace-my-executed-gtid-set] | 不是數字，被跳過[^my-global-variables-parse][^my-slave-skip] | 沒有 | 實測：名稱清單沒有；s5b errant GTID 看不到[^exp-mysql-ha] |
| GTID 內容（MariaDB） | 全域變數 `gtid_binlog_pos`（字串）[^my-trace-mdb-gtid-binlog-pos]；`SHOW ALL SLAVES STATUS` 的 `Gtid_IO_Pos`、`Gtid_Slave_Pos`[^my-trace-mdb-gtid-slave-pos] | 全域變數被跳過；`Gtid_*_Pos` 另外按逗號與 `-` 拆成 domain、server、序號[^my-trace-ex-slave-gtid] | `mysql_slave_status_gtid_slave_pos{domain_id,server_id}`、`..._gtid_io_pos{...}`（只在 replica） | 實測：mdb2 名稱清單有 `gtid_slave_pos`、`gtid_io_pos`；s5b 的 errant GTID 在 `gtid_binlog_pos`（`0-2-645`），不在 `Gtid_Slave_Pos`（`0-1-644`），所以仍看不到[^exp-mysql-ha] |

## 陷阱 {#pitfalls}

- **Seconds_Behind 是 NULL 時 series 消失。**`seconds_behind_source > 30` 這類規則在 replication 停下時回傳空。延遲規則用 heartbeat，或另外寫 `absent` 型的條件；thread 停下由 `*_running == 0` 負責。[^mysql-sbs-null][^my-trace-mdb-sbm-null][^my-trace-ex-parse-float]
- **MySQL 8.4 的 replica metric 叫 `replica_*`、`*_source`。**名稱直接來自 `SHOW REPLICA STATUS` 的欄名，exporter 不改名。規則要同時比對 `slave_*`／`replica_*` 與 `*_master`／`*_source`。[^my-trace-my-io-running-field][^my-trace-my-sbs-field][^my-trace-ex-slave-name]
- **MariaDB 的 `Preparing` 會讓 IO thread series 短暫消失。**這是 IO thread 連上 primary 後、開始讀之前的狀態；`parseStatus` 不認得，series 那一次 scrape 不存在。影響是推論，實驗沒有抓到這個狀態。<span class="at-chip at-ev-inferred">推論</span>[^my-trace-mdb-io-running-vals][^my-parse-status]
- **`wsrep_ready` 在每台 MariaDB 都存在。**`wsrep_ready != 1` 要配 `wsrep_on == 1`。[^my-trace-mdb-wsrep-ifdef]
- **Deadlock 名稱兩邊不同，而且可能翻轉。**MySQL 的 `lock_deadlocks` 若出現負值（exporter 註解提到的 MySQL bug 75966），就改輸出不帶 `_total` 的 gauge，名稱換掉。規則用 `or` 接起 `_total` 與不帶 `_total` 兩個名稱。[^my-trace-ex-im-total]
- **Undo history 兩邊來源不同。**MariaDB 從狀態變數直接拿到（預設開），MySQL 只能開 `info_schema.innodb_metrics`（預設關、要 PROCESS）。MariaDB 開了 innodb_metrics 時兩個名稱都有，值相同（s2 都是 414）。[^my-trace-mdb-histlen-var][^my-trace-my-histlen-mon][^exp-mysql-ha]
- **字串變數不輸出。**errant GTID、`Last_SQL_Error` 的文字、binlog 檔名都看不到，要另外查。[^my-global-variables-parse][^my-trace-ex-parse-logre]
- **MySQL 8.4 上每次 scrape 有 8 個語法錯誤。**監控 `Com_*` 或錯誤計數的人會看到 exporter 帳號固定產生 ER_PARSE_ERROR；這是 slave_status 的退回邏輯，不是設定錯。[^my-trace-ex-slave-loop][^exp-mysql-trace]

## 沒有釘到原始碼行的部分 {#unpinned}

| 說法 | 現在的依據 | 等級 |
|---|---|---|
| MySQL 8.4 移除了 `SHOW SLAVE STATUS`、`SHOW ALL SLAVES STATUS` 不是 MySQL 語法 | 語法檔沒有核對；每次 scrape 8 個 ER_PARSE_ERROR、最後 `SHOW REPLICA STATUS` 成功 | 實測（行為），未讀碼 |
| MySQL 的 `general_log` 不記語法錯誤的語句 | 只有觀察：8 個錯誤都不在 `general_log` 裡 | 實測（行為），未讀碼 |
| MariaDB ≥ 10.5.9 的 replica 狀態要 SLAVE MONITOR | 前一輪實驗收回權限時 Error 1227；權限檢查那一行沒有核對 | 實測[^exp-mysql] |
| pt-heartbeat 的表格式是 `ts VARCHAR(26)`、`server_id` | exporter 的註解[^my-trace-ex-hb-schema]與本次輕量檢查；pt-heartbeat（Percona Toolkit）原始碼沒有讀 | 推論 |
| MySQL 的 `general_log` 不記「查不到系統變數」的語句（query_response_time 的檢查查詢） | 讀碼確定有送出[^my-trace-ex-qrt-check]，但 `general_log` 沒有；錯誤碼沒有另外計數 | 推論 |
| `Threads_running` 在負載下的行為 | 只讀碼；實驗沒有刻意推高 | 讀碼 |

## 實驗 {#lab}

- 主要情境：[`lab/targets/mysql/ha/`](https://github.com/HahaSula/alert-trust/tree/main/lab/targets/mysql/ha)，結果 `results/run-20261108.md`，各節點 s1 基線時的 metric 名稱 `results/names-{mdb1,mdb2,my1,my2}.txt`。名稱清單是基線時抓的，所以 `innodb_trx`（沒有交易）與 MySQL 的 semi-sync（還沒裝 plugin）不在清單裡，要看對應情境的查詢結果。[^exp-mysql-ha]
- SQL 追蹤：[`lab/targets/mysql/trace/`](https://github.com/HahaSula/alert-trust/tree/main/lab/targets/mysql/trace)，`./run.sh` 約 2 分鐘，結果 `results/general-log-20261109.md`。[^exp-mysql-trace]
