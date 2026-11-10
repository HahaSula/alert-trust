# 附錄：從資料庫原始碼到 metric（MySQL）

[主頁](mysql.md)列出每個訊號該看哪個 metric。這頁回答另一個問題：**這個 metric 真的是從那個狀態變數或那張表來的嗎？**每個訊號都追完整條鏈：

1. **資料庫端**：值在哪裡產生。MySQL 8.4.7（`mysql-8.4.7`）原始碼中定義狀態變數、欄位或遞增計數的那一行；本次拆卡新增的兩個訊號另外核對 8.0.46、8.4.13、9.7.4。
2. **Exporter 端**：mysqld_exporter v0.21.0 送哪一條 SQL、怎麼把一列結果變成 metric 名稱、哪些值被丟掉、預設有沒有開。
3. **Metric**：最後的名稱與 label。
4. **Lab 觀察**：metric 有沒有真的出現在實驗裡、哪個情境讓它動了（MySQL 8.4.7 那一邊）。

每個原始碼參考都用 `gh api` 取 tag 上的檔案，逐行核對過關鍵字。另外做了一次輕量檢查：開 `general_log` 記下 exporter 一次 scrape 送出的所有 SQL。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-trace] Lab 欄的「實測」只用在親眼看到的情況；只讀過原始碼的標「讀碼」，推論標「推論」。版本之間的差異見 [MySQL 各版本的監控差異](versions/mysql.md)；MariaDB 的追蹤在[另一頁](mariadb-appendix.md)。

## 先講結論

1. **Exporter 送出的 SQL 和原始碼一致。**一次 scrape 中成功執行的每一條語句都在 `general_log` 裡，和 collector 檔案裡的常數一字不差（`general_log` 不記失敗的語句，那部分改用錯誤計數確認）。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-trace]
2. **8.4 每次 scrape 先失敗 8 次才拿到 replica 狀態。**slave_status 依序試 `SHOW ALL SLAVES STATUS`、`SHOW SLAVE STATUS`，每個再加三種後綴，最後才是 `SHOW REPLICA STATUS`。exporter 帳號的 ER_PARSE_ERROR 每次 scrape 加 8；`general_log` 不記這些失敗的語句。8.0 上 `SHOW SLAVE STATUS` 就成功，所以名稱是舊的 `slave_*`。<span class="at-chip at-ev-tested">實測</span>[^my-trace-ex-slave-loop][^exp-mysql-trace]<span class="at-chip at-ev-code">讀碼</span>[^ver-my80-show-slave-status][^ver-ex-slave-loop]
3. **值是 NULL 或不是數字時，series 不存在，不是 0。**所有「通用」collector（global_status、global_variables、slave_status）都用同一個 `parseStatus`：只認 Yes／No／ON／OFF 等幾個字和數字，其餘（含 NULL）直接跳過。`Seconds_Behind_Source` 是 NULL 時 series 消失，就是這個原因。<span class="at-chip at-ev-tested">實測</span>[^my-parse-status][^my-trace-ex-parse-float][^exp-mysql-ha]
4. **Deadlock 的 `_total` 來自 InnoDB 的計數器類型。**exporter 對 `INNODB_METRICS.type` 是 `counter` 或 `status_counter` 的計數器加 `_total`；MySQL 的 `lock_deadlocks` 沒有旗標，type 是 `counter`。<span class="at-chip at-ev-tested">實測</span>[^my-trace-ex-im-total][^my-trace-my-deadlock-mon]
5. **`Innodb_data_pending_fsyncs` 在三條線都是 redo log 加 tablespace 的 pending flush。**<span class="at-chip at-ev-code">讀碼</span>[^spl-my80-pending-fsyncs][^spl-my84-pending-fsyncs-calc][^spl-my97-pending-fsyncs]

## Exporter 怎麼把一列變成 metric {#transform}

global_status、global_variables、slave_status 三個預設 collector 不寫死 metric 清單，資料庫回什麼就輸出什麼，所以名稱完全由資料庫決定。規則如下。

| 步驟 | 做法 | 依據 |
|---|---|---|
| 連線 | 每次 scrape 開新的連線池（最多 2 條），先送 `SELECT @@version`；失敗就記 "Error opening connection" 並讓 `mysql_up` = 0，其他 collector 都不跑 | [^my-trace-ex-maxopen][^my-trace-ex-version-query][^my-trace-ex-conn-error][^my-trace-ex-up] |
| 版本 | `@@version` 取「主版號.次版號」轉成浮點數，小於 collector 的門檻就跳過；8.0、8.4、9.7 都高於所有門檻 | [^ver-ex-version-float][^ver-ex-version-gate] |
| Session 設定 | DSN 預設帶 `lock_wait_timeout=2`，每條連線先送 `SET lock_wait_timeout = 2`（`general_log` 每次出現 2 次，對應 2 條連線） | [^my-trace-ex-lockwait][^exp-mysql-trace] |
| 解析值 | `yes`／`on` → 1；`no`／`off`／`disabled`／`connecting` → 0；日期時間 → Unix 秒；其他用 `ParseFloat`，失敗就跳過。NULL 讀成空字串，`ParseFloat("")` 失敗，也跳過 | [^my-parse-status][^my-parse-connecting][^my-trace-ex-parse-float] |
| 名稱 | 非 `[a-zA-Z0-9_]` 的字元換成 `_`，全部轉小寫，前面加 `mysql_<collector>_`（例：`Threads_running` → `mysql_global_status_threads_running`） | [^my-trace-ex-validname][^my-trace-ex-gs-generic] |
| 分組 | global_status 中 `com_`、`handler_`、`connection_errors_`、`innodb_buffer_pool_pages_`、`innodb_rows_`、`performance_schema_` 開頭的變成帶 label 的 `_total` metric（例：`connection_errors_total{error}`） | [^my-global-status-re][^my-trace-ex-gs-connerr] |
| 型別 | 只有少數名稱標成 gauge 或 counter（`threads_running`、`uptime`、`slow_queries`⋯），其他是 untyped；名稱都**不加** `_total`。0.21.0 起 `replica_*_running`、`seconds_behind_*` 標成 gauge | [^my-trace-ex-gs-types][^ver-ex-slave-types] |
| slave_status | 每個欄位名稱轉小寫當 metric 名稱，label 是 `master_host`、`master_uuid`、`channel_name`；8.4 起 label 改讀 `Source_Host`／`Source_UUID`，label 名稱不變；沒有 replica 時查詢回 0 列，所以完全沒有 series | [^my-trace-ex-slave-name][^my-trace-ex-slave-labels][^ver-ex-slave-source-uuid][^my-slave-skip] |

- **字串變數一律消失。**`gtid_executed`（`uuid:1-637`）、`version`、`Last_SQL_Error` 都不是數字，被跳過。<span class="at-chip at-ev-tested">實測</span>：`mysql_global_variables_gtid_executed`、`..._version` 數量都是 0。[^my-global-variables-parse][^exp-mysql-trace]
- **以 `.數字` 結尾的字串也被跳過。**`parseStatus` 想把 `mysql-bin.000003` 解析成 3，但 `logRE.Find` 回傳整段比對結果，再拿去 `ParseFloat` 一定失敗。所以 `Source_Log_File`、`Relay_Source_Log_File` 不會輸出。<span class="at-chip at-ev-tested">實測</span>[^my-trace-ex-parse-logre][^exp-mysql-ha]

## Exporter 實際送出的 SQL {#sql}

開 `general_log` 做一次 scrape（collector：global_status、global_variables、slave_status、info_schema.innodb_metrics、info_schema.innodb_trx、info_schema.processlist、heartbeat，加上預設開的 innodb_cmp、innodb_cmpmem、query_response_time）。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-trace]

| Collector | 送出的 SQL（MySQL 8.4.7） | 原始碼 |
|---|---|---|
| （連線） | `SET lock_wait_timeout = 2`、`SELECT @@version` | [^my-trace-ex-lockwait][^my-trace-ex-version-query] |
| global_status | `SHOW GLOBAL STATUS` | [^my-trace-ex-gs-query] |
| global_variables | `SHOW GLOBAL VARIABLES` | [^my-trace-ex-gv-query] |
| slave_status | 8 條語法錯誤後 `SHOW REPLICA STATUS` | [^my-slave-queries][^my-trace-ex-slave-suffix] |
| info_schema.innodb_metrics | 先查 `information_schema.columns` 找欄位名，再 `SELECT name, subsystem, type, comment, count FROM information_schema.innodb_metrics` 加 `` WHERE `status` = 'enabled' `` | [^my-trace-ex-im-colquery][^my-trace-ex-im-switch] |
| info_schema.innodb_trx | `SELECT trx_state, COUNT(*), COALESCE(MAX(TIMESTAMPDIFF(SECOND, trx_started, NOW())), 0) FROM information_schema.innodb_trx GROUP BY trx_state` | [^my-innodb-trx-query] |
| info_schema.processlist | `SELECT user, host, command, state, COUNT(*), SUM(time) FROM information_schema.processlist WHERE ID != connection_id() AND TIME >= 0 GROUP BY ...` | [^my-processlist-query] |
| heartbeat | ``SELECT UNIX_TIMESTAMP(ts), UNIX_TIMESTAMP(NOW(6)), server_id from `heartbeat`.`heartbeat` `` | [^my-heartbeat-query][^my-trace-ex-hb-now] |
| query_response_time | `SELECT @@query_response_time_stats`；MySQL 沒有這個變數，查詢失敗，collector 把失敗當成「沒有資料」，仍回報成功（`general_log` 沒記到） | [^my-trace-ex-qrt-check] |

- **INNODB_METRICS 的「開關」欄是字串欄 `STATUS`**（`enabled`／`disabled`）。exporter 先查 `information_schema.columns` 決定用哪一個欄名，所以多一條查詢。[^my-trace-my-ism-status-col]
- **只取已啟用的計數器。**沒啟用的 INNODB_METRICS 計數器完全不輸出；本頁用到的 `trx_rseg_history_len`、`lock_deadlocks` 是 `MONITOR_DEFAULT_ON`（8.4.7）。[^my-innodb-metrics-enabled][^my-trace-my-histlen-mon]

## 穩定 {#stability}

| 訊號 | DB 來源（變數／表） | Exporter 查詢與轉換 | Metric | Lab 觀察 |
|---|---|---|---|---|
| 服務在不在 | 連線本身；第一條語句 `SELECT @@version` | 連不上或查詢失敗就回 0，其他 collector 不跑[^my-trace-ex-version-query][^my-trace-ex-conn-error] | `mysql_up` | 實測：連線打滿時 exporter log 是 `exporter.go:186 ... Error 1040`，`mysql_up` = 0（s7a）[^exp-mysql-ha] |
| 重啟 | `Uptime` = 查詢時間 − `server_start_time`[^my-trace-my-uptime][^my-trace-my-uptime-fn] | `SHOW GLOBAL STATUS`，標成 gauge[^my-trace-ex-gs-types] | `mysql_global_status_uptime` | 實測：kill -9 後重啟約 15 秒時是 16（s8）[^exp-mysql-ha] |
| Undo history | 8.4 的 InnoDB 狀態變數表沒有 `history_list_length`；只有 `INNODB_METRICS` 的 `trx_rseg_history_len`（值是 `trx_sys->rseg_history_len`，type `value`）[^my-trace-my-histlen-mon][^my-trace-my-histlen-val] | `info_schema.innodb_metrics`（預設關，要 PROCESS）；名稱 `innodb_metrics_` + subsystem + `_` + name；type 是 `value` 所以不加 `_total`[^my-innodb-metrics-name][^my-trace-ex-im-total] | `mysql_info_schema_innodb_metrics_transaction_trx_rseg_history_len` | 實測：my1 在 s2 是 434，交易結束後仍是 500[^exp-mysql-ha] |
| 長交易 | `information_schema.INNODB_TRX.trx_started`（DATETIME）[^my-trace-my-trx-started] | 依 `trx_state` 分組，算 `NOW() − MAX(...)` 秒數；沒有交易時 0 列、沒有 series[^my-innodb-trx-query][^my-trace-ex-trx-desc] | `mysql_info_schema_innodb_trx_oldest_transaction_age_seconds{state}`、`..._innodb_trx_transactions{state}` | 實測：s1 沒有 series；s2 分成 `RUNNING`（16 秒）與 `LOCK WAIT`（14 秒）[^exp-mysql-ha] |
| Deadlock | `lock_deadlocks`：`lock0wait.cc` 用 `MONITOR_INC`，type `counter`[^my-trace-my-deadlock-mon][^my-trace-my-deadlock-inc][^my-trace-my-ism-type]；沒有 `Innodb_deadlocks` status | `counter`／`status_counter` 且值 ≥ 0 才加 `_total`，其他當 gauge[^my-trace-ex-im-total] | `..._lock_lock_deadlocks_total` | 實測：s3 一次 deadlock 後 +1；輕量檢查也一樣[^exp-mysql-ha][^exp-mysql-trace] |
| Replication 因錯誤停下 | `Last_SQL_Errno` = `rli->last_error().number`[^my-trace-my-last-sql-errno] | `SHOW REPLICA STATUS`，欄名轉小寫 | `mysql_slave_status_last_sql_errno` | 實測：s5b 衝突寫入後 1062[^exp-mysql-ha] |

## 使用 {#usage}

| 訊號 | DB 來源（變數／表） | Exporter 查詢與轉換 | Metric | Lab 觀察 |
|---|---|---|---|---|
| 執行中的 thread | `Threads_running` = `Global_THD_manager::get_num_thread_running()`[^my-trace-my-threads-running][^my-trace-my-threads-running-fn] | `SHOW GLOBAL STATUS`，標成 gauge[^my-trace-ex-gs-types] | `mysql_global_status_threads_running` | 實測：名稱清單都有；perf lab 的鎖與 CPU 情境升到 11–13[^exp-mysql-ha][^exp-mysql-perf] |
| thread cache 失效 | `Threads_created`（`show_num_thread_created`）：cache 裡沒有閒置 thread、建立新 thread 後 `inc_thread_created()`[^spl-my84-threads-created][^spl-my84-thread-created-inc]；`Connections` 是連線嘗試總數[^spl-my84-connections]；`thread_cache_size` 沒設時自動是 8 + max_connections / 100（上限 100）[^spl-my84-thread-cache-var][^spl-my84-thread-cache-autosize] | `SHOW GLOBAL STATUS`，untyped，照名稱輸出[^my-global-status-skip][^res-mx-default-gs] | `mysql_global_status_threads_created`、`mysql_global_status_connections`、`mysql_global_variables_thread_cache_size` | 讀碼；三條線都有 `Threads_created`[^spl-my80-threads-created][^spl-my97-threads-created] |
| 曾經到過的最高連線數 | `Max_used_connections`：在 `check_and_incr_conn_count` 先檢查再加 1，所以最多 max_connections + 1[^my-trace-my-max-used][^my-trace-my-max-used-upd][^mysql-conn-reserved] | `SHOW GLOBAL STATUS`，untyped | `mysql_global_status_max_used_connections` | 實測：max_connections=30 打滿後是 31（s7a）[^exp-mysql-ha] |
| 被拒的連線 | `Connection_errors_max_connections`：超過上限時 `m_connection_errors_max_connection++`（兩處）[^my-trace-my-connerr-var][^my-trace-my-connerr-inc][^my-trace-my-connerr-inc2] | 符合 `connection_errors_` 前綴，變成 counter，後半段當 `error` label[^my-global-status-re][^my-trace-ex-gs-connerr] | `mysql_global_status_connection_errors_total{error="max_connections"}` | 實測：打滿當下 exporter 也進不來；連線降下後 +10（s7a）[^exp-mysql-ha] |
| 連線數 | `Threads_connected`（connection_count） | `SHOW GLOBAL STATUS`，gauge | `mysql_global_status_threads_connected` | 實測：s7a 打滿時 series 消失，s7b 給 CONNECTION_ADMIN 後回來（31）[^exp-mysql-ha] |
| Row lock 等待 | `Innodb_row_lock_current_waits` = `srv_stats.n_lock_wait_current_count`，開始等鎖時 `inc()`[^my-trace-my-rowlock-var][^my-trace-my-rowlock-export][^my-trace-my-rowlock-inc] | `SHOW GLOBAL STATUS`；前綴 `Innodb_` 由 InnoDB 的 `"Innodb"` 項目加上[^my-trace-my-innodb-prefix] | `mysql_global_status_innodb_row_lock_current_waits` | 實測：s2 一個交易等鎖時是 1[^exp-mysql-ha] |
| fsync 排隊 | `Innodb_data_pending_fsyncs` = `log_pending_flushes() + fil_n_pending_tablespace_flushes`[^spl-my84-pending-fsyncs][^spl-my84-pending-fsyncs-calc] | `SHOW GLOBAL STATUS`，untyped | `mysql_global_status_innodb_data_pending_fsyncs` | 讀碼；8.0.46、9.7.4 同一個算法[^spl-my80-pending-fsyncs][^spl-my97-pending-fsyncs] |
| 慢查詢 | `Slow_queries` = `STATUS_VAR.long_query_count`[^my-trace-my-slow] | `SHOW GLOBAL STATUS`，標成 counter 但名稱**沒有** `_total`[^my-trace-ex-gs-types] | `mysql_global_status_slow_queries` | 實測：`SLEEP(30)` 也計入（s2 摘要）[^exp-mysql-ha] |
| Metadata lock 等待 | `information_schema.PROCESSLIST.state` | 依 user、host、command、state 分組；state 轉小寫、空白換 `_`、去掉標點[^my-processlist-query][^my-trace-ex-pl-sanitize] | `mysql_info_schema_processlist_threads{command,state="waiting_for_table_metadata_lock"}` | 實測：s4a replica 上 1[^exp-mysql-ha] |

## HA {#ha}

| 訊號 | DB 來源（變數／表） | Exporter 查詢與轉換 | Metric | Lab 觀察 |
|---|---|---|---|---|
| IO thread | 8.4 欄名 `Replica_IO_Running`，值 `Yes`／`Connecting`／`No`[^my-trace-my-io-running-field][^my-trace-my-io-running-val]；8.0 用舊語法時是 `Slave_IO_Running`[^ver-my80-old-names] | 欄名轉小寫；`Yes` → 1，`No`／`Connecting` → 0[^my-parse-connecting][^my-trace-ex-slave-name] | 8.4：`mysql_slave_status_replica_io_running`；8.0：`..._slave_io_running` | 實測（8.4.7）：s4b 停 IO thread 時 0；s8 primary 被 kill 時 0、`last_io_errno` = 2003[^exp-mysql-ha] |
| SQL thread | `Replica_SQL_Running`（8.0：`Slave_SQL_Running`） | 同上 | `..._replica_sql_running`／`..._slave_sql_running` | 實測：s5a STOP REPLICA SQL_THREAD 時 0[^exp-mysql-ha] |
| 延遲（伺服器算的） | `Seconds_Behind_Source` ＝ now − 正在套用事件的 primary 時間戳 − 時鐘差；SQL thread 停、或 IO 停且已追完時 NULL[^my-trace-my-sbs-field][^mysql-sbs-null][^mysql-sbs-compute][^my-trace-my-sbs-null2]；8.0 舊語法的欄名是 `Seconds_Behind_Master` | NULL 讀成空字串，`ParseFloat` 失敗，整條 series 不輸出[^my-slave-skip][^my-trace-ex-parse-float] | `mysql_slave_status_seconds_behind_source`（8.0：`_master`） | 實測：s4b、s5a、s5b 都**消失**，同時 heartbeat 延遲 21–28 秒[^exp-mysql-ha] |
| 延遲（heartbeat） | 不是資料庫內建。由外部程式（pt-heartbeat 或 EVENT）在 primary 寫 `ts`、`server_id`，經 binlog 複製到 replica | `SELECT UNIX_TIMESTAMP(ts), UNIX_TIMESTAMP(NOW(6)), server_id`；欄名寫死；預設 `heartbeat`.`heartbeat`；`--collect.heartbeat.utc` 改用 `UTC_TIMESTAMP(6)`[^my-heartbeat-query][^my-trace-ex-hb-schema][^my-trace-ex-hb-now][^my-trace-ex-hb-default] | `mysql_heartbeat_now_timestamp_seconds{server_id}`、`mysql_heartbeat_stored_timestamp_seconds{server_id}` | 實測：s1 差值 0.6 秒；s4b IO 停時 27 秒；s4c 寫入程式停時每台一起變大[^exp-mysql-ha]。`VARCHAR(26)` 的 pt-heartbeat 格式也能解析[^exp-mysql-trace] |
| 能不能寫 | `read_only`、`super_read_only`[^my-trace-my-readonly][^my-trace-my-super-readonly] | `SHOW GLOBAL VARIABLES`，`ON`／`OFF` → 1／0[^my-global-variables-parse] | `mysql_global_variables_read_only`、`..._super_read_only` | 實測：s1 primary 0、replica 1；s9 升級後 0[^exp-mysql-ha] |
| Semi-sync | plugin 註冊 `Rpl_semi_sync_source_status`（SHOW_BOOL），沒裝 plugin 就沒有這個變數[^my-trace-my-semisync-prefix][^my-trace-my-semisync-status]；9.7 只剩 source 這組 plugin[^ver-my97-semisync-plugins][^ver-my97-semisync-source-status] | `SHOW GLOBAL STATUS`，通用轉換 | `mysql_global_status_rpl_semi_sync_source_status`，另有 `_clients`、`_no_tx` | 實測：s1 沒有這組；s6 裝 plugin 後 1，ACK 逾時後 0[^exp-mysql-ha] |
| GTID 開關 | `gtid_mode`（`ON`／`OFF`） | `SHOW GLOBAL VARIABLES`，`ON`／`OFF` → 1／0 | `mysql_global_variables_gtid_mode` | 實測：值 1[^exp-mysql-ha] |
| GTID 內容 | `gtid_executed` 全域變數、`SHOW REPLICA STATUS` 的 `Executed_Gtid_Set`，都是字串[^my-trace-my-gtid-executed][^my-trace-my-executed-gtid-set] | 不是數字，被跳過[^my-global-variables-parse][^my-slave-skip] | 沒有 | 實測：名稱清單沒有；s5b errant GTID 看不到[^exp-mysql-ha] |
| GR 成員 | `performance_schema.replication_group_members`[^my-asp-my-grm-state-col] | 所有欄位變成 label、值 1[^my-asp-ex-grm-query][^my-asp-ex-grm-labels] | `mysql_perf_schema_replication_group_member_info` | 實測：三台暫停兩台時 2 列 UNREACHABLE[^my-asp-exp] |

## 陷阱 {#pitfalls}

- **Seconds_Behind_Source 是 NULL 時 series 消失。**`seconds_behind_source > 30` 這類規則在 replication 停下時回傳空。延遲規則用 heartbeat；thread 停下由 `*_running == 0` 負責。[^mysql-sbs-null][^my-trace-ex-parse-float]
- **8.4 的 replica metric 叫 `replica_*`、`*_source`，8.0 是 `slave_*`、`*_master`。**名稱直接來自欄名，exporter 不改名。規則要同時比對兩種名稱；`*_running` 用 `__name__` 正規式（`or` 會把 label 相同的第二條丟掉）。[^my-trace-my-io-running-field][^my-trace-my-sbs-field][^my-trace-ex-slave-name][^ver-ex-slave-loop]
- **Deadlock 名稱可能翻轉。**`lock_deadlocks` 若出現負值（exporter 註解提到的 MySQL bug 75966），就改輸出不帶 `_total` 的 gauge。規則用 `or` 接起 `_total` 與不帶 `_total` 兩個名稱。[^my-trace-ex-im-total]
- **Undo history 與 deadlock 只能開 `info_schema.innodb_metrics`**（預設關、要 PROCESS）。[^my-trace-my-histlen-mon]
- **字串變數不輸出。**errant GTID、`Last_SQL_Error` 的文字、binlog 檔名都看不到，要另外查。[^my-global-variables-parse][^my-trace-ex-parse-logre]
- **每次 scrape 有 8 個語法錯誤。**監控 `Com_*` 或錯誤計數的人會看到 exporter 帳號固定產生 ER_PARSE_ERROR；這是 slave_status 的退回邏輯，不是設定錯。[^my-trace-ex-slave-loop][^exp-mysql-trace]

## 沒有釘到原始碼行的部分 {#unpinned}

| 說法 | 現在的依據 | 等級 |
|---|---|---|
| `general_log` 不記語法錯誤的語句 | 只有觀察：8 個錯誤都不在 `general_log` 裡 | 實測（行為），未讀碼 |
| pt-heartbeat 的表格式是 `ts VARCHAR(26)`、`server_id` | exporter 的註解[^my-trace-ex-hb-schema]與本次輕量檢查；pt-heartbeat（Percona Toolkit）原始碼沒有讀 | 推論 |
| `general_log` 不記「查不到系統變數」的語句（query_response_time 的檢查查詢） | 讀碼確定有送出[^my-trace-ex-qrt-check]，但 `general_log` 沒有；錯誤碼沒有另外計數 | 推論 |
| 8.0、9.7 的 INNODB_METRICS 預設計數器 | 沒有核對 | 未查 |

## 實驗 {#lab}

- 主要情境：[`lab/targets/mysql/ha/`](https://github.com/HahaSula/alert-trust/tree/main/lab/targets/mysql/ha)，結果 `results/run-20261108.md`，MySQL 節點 s1 基線時的 metric 名稱 `results/names-{my1,my2}.txt`。名稱清單是基線時抓的，所以 `innodb_trx`（沒有交易）與 semi-sync（還沒裝 plugin）不在清單裡，要看對應情境的查詢結果。[^exp-mysql-ha]
- SQL 追蹤：[`lab/targets/mysql/trace/`](https://github.com/HahaSula/alert-trust/tree/main/lab/targets/mysql/trace)，`./run.sh` 約 2 分鐘，結果 `results/general-log-20261109.md`。[^exp-mysql-trace]
