#!/usr/bin/env bash
# Target-facing 實驗：MariaDB 11.4 與 MySQL 8.4 的 primary + async replica。
# 情境：s1 基線、s2 長交易／鎖等待／undo history、s3 deadlock、s4 延遲三種、s5 replication 中斷、
#       s6 semi-sync 退回 async、s7 連線打滿與每帳號上限、s8 primary 被 kill、s9 failover 與舊 primary 回來。
# 結果寫到 results/run-YYYYMMDD.md 與 results/names-*.txt；結束時 docker compose down -v。
set -u
cd "$(dirname "$0")"
OUT=results/run-$(date +%Y%m%d).md
P=http://127.0.0.1:22090/api/v1
KEEP='__name__|instance|collector|error|state|user|server_id|alertname|alertstate|cluster|command'
q() { curl -s --get "$P/query" --data-urlencode "query=$1" | python3 -I -c '
import json, re, sys
d = json.load(sys.stdin)
if d.get("status") != "success": print("ERROR", d.get("error")); sys.exit()
keep = re.compile(sys.argv[1])
r = d["data"]["result"]
out = []
for x in r:
    lab = ",".join(f"{k}={v}" for k, v in sorted(x["metric"].items()) if keep.fullmatch(k))
    v = x["value"][1]
    try: v = f"{float(v):.6g}"
    except ValueError: pass
    out.append(f"{{{lab}}} {v}")
print("empty" if not out else "; ".join(sorted(out)))' "$KEEP"; }
log() { echo "$*" | tee -a "$OUT"; }
show() { log "- \`$1\` → $(q "$1")"; }
alerts() { log "- ALERTS → $(q 'count by (alertname, instance, cluster, alertstate) (ALERTS)')"; }
my() { docker compose exec -T "$1" mysql -uroot -prootpw -N -e "$2" 2>&1 | grep -v 'Using a password'; }
mdb() { docker compose exec -T "$1" mariadb -uroot -prootpw -N -e "$2" 2>&1; }
sqlr() { case "$1" in my*) my "$@" ;; *) mdb "$@" ;; esac; }
bg() { # bg <node> <user> <pass> <sql> : 背景 client，不等結果
  local bin=mysql; case "$1" in mdb*) bin=mariadb ;; esac
  docker compose exec -d -T "$1" sh -c "$bin -h127.0.0.1 -u$2 -p$3 -e \"$4\" >/tmp/bg.\$\$.log 2>&1"; }
stamp() { log ""; log "## $1  ($(date +%T))"; }

trap 'docker compose down -v >/dev/null 2>&1' EXIT
mkdir -p results; : > "$OUT"
docker compose down -v >/dev/null 2>&1
docker compose up -d --wait >/dev/null 2>&1 || docker compose up -d
sleep 20   # MySQL 映像的 healthcheck 在暫時 server 上就會過，多等一下讓 init 跑完

stamp "s0 設定 replication"
my my2 "RESET BINARY LOGS AND GTIDS; CHANGE REPLICATION SOURCE TO SOURCE_HOST='my1', SOURCE_USER='repl', SOURCE_PASSWORD='replpw', SOURCE_AUTO_POSITION=1, GET_SOURCE_PUBLIC_KEY=1; START REPLICA; SET GLOBAL super_read_only=ON;"
mdb mdb2 "RESET MASTER; CHANGE MASTER TO MASTER_HOST='mdb1', MASTER_USER='repl', MASTER_PASSWORD='replpw', MASTER_USE_GTID=slave_pos; START SLAVE; SET GLOBAL read_only=ON;"
sleep 20

stamp "s1 基線"
for i in mdb1 mdb2 my1 my2; do
  curl -s --get "$P/query" --data-urlencode "query=count by (__name__) ({instance=\"$i\",__name__=~\"mysql_.*\"})" \
    | python3 -I -c 'import json,sys; print("\n".join(sorted(r["metric"]["__name__"] for r in json.load(sys.stdin)["data"]["result"])))' > "results/names-$i.txt"
done
log "- metric 名稱數：$(wc -l results/names-*.txt | tr '\n' ' ')"
show 'mysql_exporter_collector_success == 0'
show '{__name__=~"mysql_slave_status_(slave|replica)_(io|sql)_running|mysql_slave_status_seconds_behind_(master|source)"}'
show 'mysql_heartbeat_now_timestamp_seconds - mysql_heartbeat_stored_timestamp_seconds'
show 'mysql_global_variables_read_only'
show 'mysql_global_variables_super_read_only'
show 'mysql_global_variables_gtid_mode'
show 'mysql_global_variables_gtid_strict_mode'
show 'mysql_global_variables_performance_schema'
show 'mysql_global_status_wsrep_ready'
show 'mysql_global_variables_wsrep_on'
show '{__name__=~"mysql_global_status_rpl_semi_sync_(master|source)_status"}'
show 'count by (instance) (mysql_sys_current_connections)'
show 'count by (instance) ({__name__=~"mysql_info_schema_innodb_trx_.*"})'
show 'mysql_global_variables_max_connections'
show 'mysql_mysql_max_user_connections{user=~"exporter|lim"}'
alerts

stamp "s2 長交易＋row lock 等待＋consistent snapshot 擋住 purge（兩台 primary）"
for n in mdb1 my1; do
  bg $n app apppw "BEGIN; UPDATE app.t SET v=v+1 WHERE id=1; DO SLEEP(30); COMMIT;"
  bg $n app apppw "START TRANSACTION WITH CONSISTENT SNAPSHOT; SELECT COUNT(*) FROM app.t; DO SLEEP(40); COMMIT;"
done
sleep 2
for n in mdb1 my1; do
  bg $n app apppw "UPDATE app.t SET v=v+1 WHERE id=1;"
  CH=$(for i in $(seq 1 400); do printf 'UPDATE app.t SET v=v+1 WHERE id=%d;' $((i % 190 + 5)); done)
  bg $n app apppw "$CH"
done
sleep 15
show 'mysql_info_schema_innodb_trx_oldest_transaction_age_seconds{instance=~"mdb1|my1"}'
show 'mysql_info_schema_innodb_trx_transactions{instance=~"mdb1|my1"}'
show 'mysql_global_status_innodb_row_lock_current_waits{instance=~"mdb1|my1"}'
show 'mysql_global_status_innodb_history_list_length'
show 'mysql_info_schema_innodb_metrics_transaction_trx_rseg_history_len'
show 'sum by (instance, state) (mysql_info_schema_processlist_threads{instance=~"mdb1|my1", state!=""})'
sleep 30
show 'mysql_global_status_innodb_history_list_length'
show 'mysql_info_schema_innodb_metrics_transaction_trx_rseg_history_len'
show 'mysql_global_status_innodb_row_lock_waits{instance=~"mdb1|my1"}'
alerts

stamp "s3 deadlock（兩台 primary）"
show 'mysql_global_status_innodb_deadlocks'
show '{__name__=~"mysql_info_schema_innodb_metrics_lock_lock_deadlocks(_total)?"}'
for n in mdb1 my1; do
  bg $n app apppw "BEGIN; UPDATE app.t SET v=v+1 WHERE id=10; DO SLEEP(2); UPDATE app.t SET v=v+1 WHERE id=11; COMMIT;"
  bg $n app apppw "BEGIN; UPDATE app.t SET v=v+1 WHERE id=11; DO SLEEP(2); UPDATE app.t SET v=v+1 WHERE id=10; COMMIT;"
done
sleep 12
show 'mysql_global_status_innodb_deadlocks'
show '{__name__=~"mysql_info_schema_innodb_metrics_lock_lock_deadlocks(_total)?"}'

stamp "s4a replica 的 SQL thread 被 metadata lock 擋住（replica 上開著交易，primary 做 ALTER）"
for n in mdb2 my2; do bg $n root rootpw "BEGIN; SELECT * FROM app.t LIMIT 1; DO SLEEP(35); COMMIT;"; done
sleep 2
mdb mdb1 "ALTER TABLE app.t ADD COLUMN c1 INT;"
my my1 "ALTER TABLE app.t ADD COLUMN c1 INT;"
sleep 25
show '{__name__=~"mysql_slave_status_(slave|replica)_sql_running|mysql_slave_status_seconds_behind_(master|source)"}'
show 'mysql_heartbeat_now_timestamp_seconds{instance=~"mdb2|my2"} - mysql_heartbeat_stored_timestamp_seconds{instance=~"mdb2|my2"}'
show 'sum by (instance, state) (mysql_info_schema_processlist_threads{instance=~"mdb2|my2", state=~".*[Ll]ock.*"})'
sleep 15

stamp "s4b replica 的 IO thread 停 25 秒（primary 照常寫 heartbeat）"
mdb mdb2 "STOP SLAVE IO_THREAD;"; my my2 "STOP REPLICA IO_THREAD;"
sleep 25
show '{__name__=~"mysql_slave_status_(slave|replica)_(io|sql)_running|mysql_slave_status_seconds_behind_(master|source)"}'
show 'mysql_heartbeat_now_timestamp_seconds{instance=~"mdb2|my2"} - mysql_heartbeat_stored_timestamp_seconds{instance=~"mdb2|my2"}'
alerts
mdb mdb2 "START SLAVE IO_THREAD;"; my my2 "START REPLICA IO_THREAD;"
sleep 10

stamp "s4c primary 的 heartbeat 寫入停 25 秒（event_scheduler=OFF），replication 正常"
mdb mdb1 "SET GLOBAL event_scheduler=OFF;"; my my1 "SET GLOBAL event_scheduler=OFF;"
sleep 25
show 'mysql_heartbeat_now_timestamp_seconds - mysql_heartbeat_stored_timestamp_seconds'
show '{__name__=~"mysql_slave_status_seconds_behind_(master|source)"}'
mdb mdb1 "SET GLOBAL event_scheduler=ON;"; my my1 "SET GLOBAL event_scheduler=ON;"
sleep 8

stamp "s5a STOP SQL_THREAD 20 秒"
mdb mdb2 "STOP SLAVE SQL_THREAD;"; my my2 "STOP REPLICA SQL_THREAD;"
sleep 20
show '{__name__=~"mysql_slave_status_(slave|replica)_(io|sql)_running|mysql_slave_status_seconds_behind_(master|source)"}'
show 'mysql_heartbeat_now_timestamp_seconds{instance=~"mdb2|my2"} - mysql_heartbeat_stored_timestamp_seconds{instance=~"mdb2|my2"}'
alerts
mdb mdb2 "START SLAVE SQL_THREAD;"; my my2 "START REPLICA SQL_THREAD;"
sleep 8

stamp "s5b 衝突寫入：replica 先寫 id=1000，primary 再寫同一個 id"
log "- my1 gtid_executed 前：$(my my1 'SELECT @@gtid_executed' | tr -d '\n')"
mdb mdb2 "SET GLOBAL read_only=OFF; INSERT INTO app.t (id) VALUES (1000); SET GLOBAL read_only=ON;"
my my2 "SET GLOBAL super_read_only=OFF; INSERT INTO app.t (id) VALUES (1000); SET GLOBAL super_read_only=ON;"
mdb mdb1 "INSERT INTO app.t (id) VALUES (1000);"; my my1 "INSERT INTO app.t (id) VALUES (1000);"
sleep 15
show '{__name__=~"mysql_slave_status_(slave|replica)_(io|sql)_running|mysql_slave_status_last_(sql|io)_errno"}'
show '{__name__=~"mysql_slave_status_seconds_behind_(master|source)"}'
alerts
log "- my2 是否有 primary 沒有的 GTID（errant）：GTID_SUBSET(my2, my1) = $(my my1 "SELECT GTID_SUBSET('$(my my2 'SELECT @@gtid_executed' | tr -d '\n')', @@gtid_executed)" | tr -d '\n')"
log "- my2 gtid_executed：$(my my2 'SELECT @@gtid_executed' | tr -d '\n')"
log "- mdb2 gtid_binlog_pos / gtid_slave_pos：$(mdb mdb2 'SELECT @@gtid_binlog_pos, @@gtid_slave_pos' | tr '\n\t' '  ')"
log "- exporter 有沒有任何 gtid_executed／errant 相關數值：$(q 'count by (__name__) ({__name__=~"mysql_.*gtid.*", instance="my2"})')"
mdb mdb2 "SET GLOBAL read_only=OFF; DELETE FROM app.t WHERE id=1000; SET GLOBAL read_only=ON; START SLAVE SQL_THREAD;"
my my2 "SET GLOBAL super_read_only=OFF; DELETE FROM app.t WHERE id=1000; SET GLOBAL super_read_only=ON; START REPLICA SQL_THREAD;"
sleep 10
show '{__name__=~"mysql_slave_status_(slave|replica)_sql_running|mysql_slave_status_last_sql_errno"}'

stamp "s6 semi-sync 開在 primary、replica 沒開：等 ACK 逾時（3s）後退回 async"
mdb mdb1 "SET GLOBAL rpl_semi_sync_master_timeout=3000; SET GLOBAL rpl_semi_sync_master_enabled=ON;"
my my1 "INSTALL PLUGIN rpl_semi_sync_source SONAME 'semisync_source.so'; SET GLOBAL rpl_semi_sync_source_timeout=3000; SET GLOBAL rpl_semi_sync_source_enabled=ON;"
sleep 6
show '{__name__=~"mysql_global_status_rpl_semi_sync_(master|source)_status"}'
for n in mdb1 my1; do
  t0=$(date +%s.%N); sqlr $n "INSERT INTO app.t (id) VALUES (2000);" >/dev/null; t1=$(date +%s.%N)
  log "- $n 寫入一筆耗時（含 docker exec）：$(python3 -I -c "print(round($t1-$t0,2))")s"
done
sleep 10
show '{__name__=~"mysql_global_status_rpl_semi_sync_(master|source)_status"}'
show '{__name__=~"mysql_global_variables_rpl_semi_sync_(master|source)_enabled"}'
show '{__name__=~"mysql_global_status_rpl_semi_sync_(master|source)_no_tx"}'
log "- 開 replica 端 semi-sync 後重連 IO thread"
mdb mdb2 "SET GLOBAL rpl_semi_sync_slave_enabled=ON; STOP SLAVE IO_THREAD; START SLAVE IO_THREAD;"
my my2 "INSTALL PLUGIN rpl_semi_sync_replica SONAME 'semisync_replica.so'; SET GLOBAL rpl_semi_sync_replica_enabled=ON; STOP REPLICA IO_THREAD; START REPLICA IO_THREAD;"
sleep 3
mdb mdb1 "INSERT INTO app.t (id) VALUES (2001);"; my my1 "INSERT INTO app.t (id) VALUES (2001);"
sleep 10
show '{__name__=~"mysql_global_status_rpl_semi_sync_(master|source)_status"}'
show '{__name__=~"mysql_global_status_rpl_semi_sync_(master|source)_clients"}'
alerts

stamp "s7a 連線打滿：max_connections=30，app 帳號開 32 條 SLEEP(40)（exporter 沒有 CONNECTION_ADMIN）"
show 'mysql_global_status_connection_errors_total{error="max_connections"}'
for n in mdb1 my1; do for i in $(seq 1 32); do bg $n app apppw "DO SLEEP(40);"; done; done
sleep 15
show 'mysql_up{instance=~"mdb1|my1"}'
show 'mysql_global_status_threads_connected{instance=~"mdb1|my1"}'
show 'max_over_time(mysql_global_status_threads_connected{instance=~"mdb1|my1"}[1m]) / mysql_global_variables_max_connections'
log "- exporter log："; docker compose logs --since 12s ex-mdb1 ex-my1 2>&1 | grep -oE '(ex-[a-z0-9]+).*(Error 1040[^"]*|Too many connections[^"]*)' | sort | uniq -c | head -4 | tee -a "$OUT"
alerts
sleep 32
show 'mysql_up{instance=~"mdb1|my1"}'
show 'mysql_global_status_connection_errors_total{error="max_connections"}'
show 'mysql_global_status_max_used_connections{instance=~"mdb1|my1"}'
show 'increase(mysql_global_status_connection_errors_total{error="max_connections"}[2m])'

stamp "s7b 同樣打滿，但 exporter 有 CONNECTION_ADMIN（MariaDB: CONNECTION ADMIN）"
mdb mdb1 "GRANT CONNECTION ADMIN ON *.* TO 'exporter'@'%';"; my my1 "GRANT CONNECTION_ADMIN ON *.* TO 'exporter'@'%';"
for n in mdb1 my1; do for i in $(seq 1 32); do bg $n app apppw "DO SLEEP(30);"; done; done
sleep 15
show 'mysql_up{instance=~"mdb1|my1"}'
show 'mysql_global_status_threads_connected{instance=~"mdb1|my1"}'
show 'mysql_exporter_collector_success{instance=~"mdb1|my1"} == 0'
log "- root 還能不能進來（保留的 +1 連線已被 exporter 用掉時）：mdb1=$(mdb mdb1 'SELECT 1' | tail -1)；my1=$(my my1 'SELECT 1' | tail -1)"
sleep 25
mdb mdb1 "REVOKE CONNECTION ADMIN ON *.* FROM 'exporter'@'%';"; my my1 "REVOKE CONNECTION_ADMIN ON *.* FROM 'exporter'@'%';"

stamp "s7c 每帳號上限：lim 帳號 MAX_USER_CONNECTIONS 2，開 3 條"
show 'mysql_global_status_aborted_connects{instance=~"mdb1|my1"}'
show '{__name__=~"mysql_global_status_connection_errors_total|mysql_global_status_max_used_connections", instance=~"mdb1|my1"}'
for n in mdb1 my1; do bg $n lim limpw "DO SLEEP(20);"; bg $n lim limpw "DO SLEEP(20);"; done
sleep 3
log "- 第三條 mdb1：$(docker compose exec -T mdb1 mariadb -h127.0.0.1 -ulim -plimpw -e 'SELECT 1' 2>&1 | tail -1)"
log "- 第三條 my1：$(docker compose exec -T my1 mysql -h127.0.0.1 -ulim -plimpw -e 'SELECT 1' 2>&1 | grep -v Warning | tail -1)"
sleep 8
show 'mysql_global_status_aborted_connects{instance=~"mdb1|my1"}'
show '{__name__=~"mysql_global_status_connection_errors_total|mysql_global_status_max_used_connections", instance=~"mdb1|my1"}'
show 'mysql_sys_current_connections{user="lim"}'
show 'max by (instance, user) (mysql_sys_current_connections{instance="my1"}) / on (instance, user) max by (instance, user) (label_replace(mysql_mysql_max_user_connections > 0, "user", "$1", "mysql_user", "(.*)"))'
sleep 12

stamp "s8 kill -9 兩台 primary，30 秒後再啟動"
docker compose kill -s KILL mdb1 my1 >/dev/null 2>&1
sleep 15
show 'up{instance=~"mdb1|my1"}'
show 'mysql_up{instance=~"mdb1|my1"}'
show '{__name__=~"mysql_slave_status_(slave|replica)_io_running|mysql_slave_status_last_io_errno"}'
sleep 15
docker compose start mdb1 my1 >/dev/null 2>&1
for t in 15 30 45 60 75; do
  sleep 15
  log "- 啟動後約 ${t}s：$(q '{__name__=~"mysql_slave_status_(slave|replica)_io_running|mysql_slave_status_last_io_errno|mysql_global_status_uptime"}')"
done
alerts

stamp "s9 failover：停舊 primary，replica 升級（RESET REPLICA ALL、關 read_only），舊 primary 再回來"
docker compose stop mdb1 my1 >/dev/null 2>&1
mdb mdb2 "STOP SLAVE; RESET SLAVE ALL; SET GLOBAL read_only=OFF;"
my my2 "STOP REPLICA; RESET REPLICA ALL; SET GLOBAL super_read_only=OFF; SET GLOBAL read_only=OFF;"
sleep 15
show '{__name__=~"mysql_slave_status_.*_running", instance=~"mdb2|my2"}'
show 'mysql_global_variables_read_only'
show 'count by (cluster) (mysql_global_variables_read_only == 0 unless on (instance) {__name__=~"mysql_slave_status_(slave|replica)_sql_running"})'
show 'mysql_global_variables_read_only{role="primary"}'
show 'mysql_heartbeat_now_timestamp_seconds - mysql_heartbeat_stored_timestamp_seconds'
alerts
docker compose start mdb1 my1 >/dev/null 2>&1
sleep 30
show 'mysql_global_variables_read_only'
show 'count by (cluster) (mysql_global_variables_read_only == 0 unless on (instance) {__name__=~"mysql_slave_status_(slave|replica)_sql_running"})'
sleep 70
alerts
log ""
log "結束 $(date +%T)"
