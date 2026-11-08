# mysqld_exporter v0.21.0 送出的 SQL（general_log，一次 scrape）

環境：mariadb:11.4、mysql:8.4.7（單機、非 replica），prom/mysqld-exporter:v0.21.0；
collector：global_status、global_variables、slave_status、info_schema.innodb_metrics、info_schema.innodb_trx、info_schema.processlist、heartbeat。

## mdb：11.4.13-MariaDB-ubu2404-log

```text
Query	SET lock_wait_timeout = 2
Query	SELECT @@version
Query	SELECT UNIX_TIMESTAMP(ts), UNIX_TIMESTAMP(NOW(6)), server_id from `heartbeat`.`heartbeat`
Query	SELECT page_size, compress_ops, compress_ops_ok, compress_time, uncompress_ops, uncompress_time FROM information_schema.innodb_cmp
Query	SHOW GLOBAL STATUS
Query	SET lock_wait_timeout = 2
Query	SHOW ALL SLAVES STATUS
Query	SELECT @@query_response_time_stats
Query	SELECT trx_state, COUNT(*) AS transactions, COALESCE(MAX(TIMESTAMPDIFF(SECOND, trx_started, NOW())), 0) AS oldest_transaction_seconds FROM information_schema.innodb_trx GROUP BY trx_state
Query	SHOW GLOBAL VARIABLES
Query	SELECT user, SUBSTRING_INDEX(host, ':', 1) AS host, COALESCE(command, '') AS command, COALESCE(state, '') AS state, COUNT(*) AS processes, SUM(time) AS seconds FROM information_schema.processlist WHERE ID != connection_id() AND TIME >= 0 GROUP BY user, host, command, state
Query	SELECT page_size, buffer_pool_instance, pages_used, pages_free, relocation_ops, relocation_time FROM information_schema.innodb_cmpmem
Query	SELECT column_name FROM information_schema.columns WHERE table_schema = 'information_schema' AND table_name = 'INNODB_METRICS' AND column_name IN ('status', 'enabled') LIMIT 1
Query	SELECT name, subsystem, type, comment, count FROM information_schema.innodb_metrics WHERE `enabled` = '1'
Quit	
Quit	
```

## my：8.4.7

```text
Query	SET lock_wait_timeout = 2
Query	SELECT @@version
Query	SELECT UNIX_TIMESTAMP(ts), UNIX_TIMESTAMP(NOW(6)), server_id from `heartbeat`.`heartbeat`
Query	SHOW GLOBAL VARIABLES
Query	SET lock_wait_timeout = 2
Query	SELECT page_size, compress_ops, compress_ops_ok, compress_time, uncompress_ops, uncompress_time FROM information_schema.innodb_cmp
Query	SHOW GLOBAL STATUS
Query	SELECT trx_state, COUNT(*) AS transactions, COALESCE(MAX(TIMESTAMPDIFF(SECOND, trx_started, NOW())), 0) AS oldest_transaction_seconds FROM information_schema.innodb_trx GROUP BY trx_state
Query	SELECT column_name FROM information_schema.columns WHERE table_schema = 'information_schema' AND table_name = 'INNODB_METRICS' AND column_name IN ('status', 'enabled') LIMIT 1
Query	SELECT user, SUBSTRING_INDEX(host, ':', 1) AS host, COALESCE(command, '') AS command, COALESCE(state, '') AS state, COUNT(*) AS processes, SUM(time) AS seconds FROM information_schema.processlist WHERE ID != connection_id() AND TIME >= 0 GROUP BY user, host, command, state
Query	SELECT page_size, buffer_pool_instance, pages_used, pages_free, relocation_ops, relocation_time FROM information_schema.innodb_cmpmem
Query	SELECT name, subsystem, type, comment, count FROM information_schema.innodb_metrics WHERE `status` = 'enabled'
Query	SHOW REPLICA STATUS
Quit	
Quit	
```

## my：general_log 沒記到的語句（performance_schema 依 digest 統計，含失敗的語句）

MySQL 的 general_log 不記語法錯誤的語句，所以 slave_status 的退回順序改用 digest 表看。

```text
1	0	SHOW GLOBAL STATUS
1	0	SHOW GLOBAL VARIABLES
1	0	SHOW REPLICA STATUS
```

exporter 帳號的 ER_PARSE_ERROR（1064）次數（performance_schema.events_errors_summary_by_user_by_error）：scrape 前 0，一次 scrape 後 8。

## 對應的 metric（節錄）

### 127.0.0.1:22501

```text
mysql_exporter_collector_success{collector="collect.global_status"} 1
mysql_exporter_collector_success{collector="collect.global_variables"} 1
mysql_exporter_collector_success{collector="collect.heartbeat"} 1
mysql_exporter_collector_success{collector="collect.info_schema.innodb_cmp"} 1
mysql_exporter_collector_success{collector="collect.info_schema.innodb_cmpmem"} 1
mysql_exporter_collector_success{collector="collect.info_schema.innodb_metrics"} 1
mysql_exporter_collector_success{collector="collect.info_schema.innodb_trx"} 1
mysql_exporter_collector_success{collector="collect.info_schema.processlist"} 1
mysql_exporter_collector_success{collector="collect.info_schema.query_response_time"} 1
mysql_exporter_collector_success{collector="collect.slave_status"} 1
mysql_global_status_connection_errors_total{error="max_connections"} 0
mysql_global_status_innodb_deadlocks 0
mysql_global_status_innodb_history_list_length 0
mysql_global_status_innodb_row_lock_current_waits 0
mysql_global_status_max_used_connections 2
mysql_global_status_threads_running 1
mysql_global_status_uptime 7
mysql_global_status_wsrep_ready 0
mysql_global_variables_read_only 0
mysql_global_variables_wsrep_on 0
mysql_heartbeat_now_timestamp_seconds{server_id="1"} 1.794181171504688e+09
mysql_heartbeat_stored_timestamp_seconds{server_id="1"} 1.794181164218987e+09
mysql_info_schema_innodb_metrics_lock_lock_deadlocks 0
mysql_info_schema_innodb_metrics_transaction_trx_rseg_history_len 0
mysql_up 1
```

### 127.0.0.1:22502

```text
mysql_exporter_collector_success{collector="collect.global_status"} 1
mysql_exporter_collector_success{collector="collect.global_variables"} 1
mysql_exporter_collector_success{collector="collect.heartbeat"} 1
mysql_exporter_collector_success{collector="collect.info_schema.innodb_cmp"} 1
mysql_exporter_collector_success{collector="collect.info_schema.innodb_cmpmem"} 1
mysql_exporter_collector_success{collector="collect.info_schema.innodb_metrics"} 1
mysql_exporter_collector_success{collector="collect.info_schema.innodb_trx"} 1
mysql_exporter_collector_success{collector="collect.info_schema.processlist"} 1
mysql_exporter_collector_success{collector="collect.info_schema.query_response_time"} 1
mysql_exporter_collector_success{collector="collect.slave_status"} 1
mysql_global_status_connection_errors_total{error="max_connections"} 0
mysql_global_status_innodb_row_lock_current_waits 0
mysql_global_status_max_used_connections 2
mysql_global_status_threads_running 2
mysql_global_status_uptime 4
mysql_global_variables_gtid_mode 0
mysql_global_variables_read_only 0
mysql_global_variables_super_read_only 0
mysql_heartbeat_now_timestamp_seconds{server_id="11"} 1.79418117152899e+09
mysql_heartbeat_stored_timestamp_seconds{server_id="11"} 1.794181166699869e+09
mysql_info_schema_innodb_metrics_lock_lock_deadlocks_total 0
mysql_info_schema_innodb_metrics_transaction_trx_rseg_history_len 5
mysql_up 1
```

## 沒有輸出的字串變數（SHOW GLOBAL VARIABLES 有值，exporter 沒有 metric）

- 127.0.0.1:22501：gtid_executed／gtid_binlog_pos／version 的 mysql_global_variables_* 數量 = 0
- 127.0.0.1:22502：gtid_executed／gtid_binlog_pos／version 的 mysql_global_variables_* 數量 = 0
