# query-exporter 自訂查詢實驗 2026-11-09

MySQL 8.4.7（my1 → my2）、MariaDB 11.4（mdb1 → mdb2）、query-exporter 5.2.2、mysqld_exporter v0.21.0、Prometheus v3.13.5（scrape／evaluation 5s）。
設定檔：queries/query-exporter/mysql.yaml（原樣掛載）；規則：rules/targets/mysql.yml。
- 版本：8.4.7、11.4.13-MariaDB-ubu2404-log

## s0 基線
- `count by (query, status) (queries{job="query-exporter"})` → empty
- `mysql_q_threads_connected` → {__name__=mysql_q_threads_connected,cluster=mdb,database=mdb1,instance=qe:9560} 1; {__name__=mysql_q_threads_connected,cluster=mdb,database=mdb2,instance=qe:9560} 0; {__name__=mysql_q_threads_connected,cluster=my,database=my1,instance=qe:9560} 2; {__name__=mysql_q_threads_connected,cluster=my,database=my2,instance=qe:9560} 1
- `mysql_q_max_connections` → {__name__=mysql_q_max_connections,cluster=mdb,database=mdb1,instance=qe:9560} 10; {__name__=mysql_q_max_connections,cluster=mdb,database=mdb2,instance=qe:9560} 30; {__name__=mysql_q_max_connections,cluster=my,database=my1,instance=qe:9560} 10; {__name__=mysql_q_max_connections,cluster=my,database=my2,instance=qe:9560} 30
- `mysql_q_read_only` → {__name__=mysql_q_read_only,cluster=mdb,database=mdb1,instance=qe:9560} 0; {__name__=mysql_q_read_only,cluster=mdb,database=mdb2,instance=qe:9560} 1; {__name__=mysql_q_read_only,cluster=my,database=my1,instance=qe:9560} 0; {__name__=mysql_q_read_only,cluster=my,database=my2,instance=qe:9560} 1
- `mysql_q_gtid_executed_transactions` → {__name__=mysql_q_gtid_executed_transactions,cluster=my,database=my1,instance=qe:9560,source_uuid=3d714c98-dc4b-11f1-b05b-7ebbcaf50b4b} 8; {__name__=mysql_q_gtid_executed_transactions,cluster=my,database=my2,instance=qe:9560,source_uuid=3d714c98-dc4b-11f1-b05b-7ebbcaf50b4b} 8
- `mysql_q_gtid_binlog_state_seq` → {__name__=mysql_q_gtid_binlog_state_seq,cluster=mdb,database=mdb1,domain_id=0,instance=qe:9560,server_id=1} 10; {__name__=mysql_q_gtid_binlog_state_seq,cluster=mdb,database=mdb2,domain_id=0,instance=qe:9560,server_id=1} 10
- `mysql_q_mdl_waiting_sessions` → {__name__=mysql_q_mdl_waiting_sessions,cluster=mdb,database=mdb1,instance=qe:9560} 0; {__name__=mysql_q_mdl_waiting_sessions,cluster=mdb,database=mdb2,instance=qe:9560} 0; {__name__=mysql_q_mdl_waiting_sessions,cluster=my,database=my1,instance=qe:9560} 0; {__name__=mysql_q_mdl_waiting_sessions,cluster=my,database=my2,instance=qe:9560} 0
- `mysql_q_replica_last_error_timestamp_seconds` → empty
- `mysql_q_ash_sampler_enabled` → {__name__=mysql_q_ash_sampler_enabled,cluster=mdb,database=mdb1,instance=qe:9560} 1
- `count(count by (__name__, database, source_uuid, domain_id, server_id, wait_class, digest, errno, channel, thread) ({__name__=~"mysql_q_.*"}))` → {} 39
- ALERTS → {alertname=MySQLBackupMetricMissing,alertstate=pending} 1; {alertname=MySQLDurabilityRelaxed,alertstate=pending} 1; {alertname=MySQLMultipleWritable,alertstate=pending} 1; {alertname=MySQLRestarted,alertstate=firing} 2; {alertname=MySQLTmpDiskTables,alertstate=pending} 1

## s5 MariaDB ASH：一個交易持有 app.t id=1 的 row lock 40 秒，3 個 UPDATE 等它；之後停用取樣 EVENT
- `mysql_q_ash_active_sessions` → {__name__=mysql_q_ash_active_sessions,cluster=mdb,database=mdb1,digest=other,instance=qe:9560,wait_class=file_io} 0.0167
- `sum by (database) (mysql_q_ash_active_sessions{wait_class="lock_row"})` → empty
- `mysql_global_status_innodb_row_lock_current_waits{job="mysql"}` → {__name__=mysql_global_status_innodb_row_lock_current_waits,instance=ex-mdb1:9104} 0; {__name__=mysql_global_status_innodb_row_lock_current_waits,instance=ex-my1:9104} 0
- 同一時間直接查 ash.aas_60s：
```
lock_row	other	1.6500
other	other	0.6000
```
- `sum by (database, query, status) (queries_total{job="query-exporter", status!="success"})` → empty
- `sum by (database) (database_errors_total{job="query-exporter"})` → empty
- `mysql_q_ash_sampler_enabled` → {__name__=mysql_q_ash_sampler_enabled,cluster=mdb,database=mdb1,instance=qe:9560} 1
- `mysql_q_ash_sampler_last_run_age_seconds` → {__name__=mysql_q_ash_sampler_last_run_age_seconds,cluster=mdb,database=mdb1,instance=qe:9560} 0
- ALERTS → {alertname=MySQLBackupMetricMissing,alertstate=pending} 1; {alertname=MySQLDurabilityRelaxed,alertstate=pending} 1; {alertname=MySQLHighThreadsRunning,alertstate=pending} 1; {alertname=MySQLMultipleWritable,alertstate=firing} 1; {alertname=MySQLRestarted,alertstate=firing} 2; {alertname=MySQLSlowQueries,alertstate=pending} 1; {alertname=MySQLTmpDiskTables,alertstate=pending} 1
- APScheduler 跳過的排程次數（misfire_grace_time 預設 1 秒；這台機器容器的單調時鐘比牆鐘慢），依查詢：
```
      3 mariadb_ash
      3 mariadb_ash_sampler
      3 mariadb_gtid
      4 mariadb_mdl
      4 mariadb_server
      3 mysql_gtid
      4 mysql_mdl
      4 mysql_replica_errors
      4 mysql_server
```
- `sum by (database, query) (queries_total{job="query-exporter", status="success"})` → {database=mdb1,query=mariadb_ash_sampler} 2; {database=mdb1,query=mariadb_ash} 2; {database=mdb1,query=mariadb_gtid} 2; {database=mdb1,query=mariadb_mdl} 5; {database=mdb1,query=mariadb_server} 5; {database=mdb2,query=mariadb_gtid} 2; {database=mdb2,query=mariadb_mdl} 5; {database=mdb2,query=mariadb_server} 5; {database=my1,query=mysql_gtid} 2; {database=my1,query=mysql_mdl} 5; {database=my1,query=mysql_replica_errors} 5; {database=my1,query=mysql_server} 5; {database=my2,query=mysql_gtid} 2; {database=my2,query=mysql_mdl} 5; {database=my2,query=mysql_replica_errors} 5; {database=my2,query=mysql_server} 5
- query-exporter log 中所有不同的錯誤訊息（去掉時間）：
```
```

（結束：docker compose down -v）
