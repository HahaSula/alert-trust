#!/usr/bin/env bash
# query-exporter 5.2.2 補 mysqld_exporter 盲點：MySQL 8.4.7 與 MariaDB 11.4 各一組 primary + replica。
# 情境：s0 基線、s1 連線打滿、s2 metadata lock 阻擋者、s3 replica 錯誤碼、s4 errant GTID、s5 MariaDB ASH 取樣器、s6 資料庫停機時的自訂 metric。
# 結果寫到 results/run-YYYYMMDD.md；結束時 docker compose down -v。
# 這台機器的容器時鐘不穩，只看數值與告警狀態，不看時間長短。
set -u
ONLY=${1:-all}   # ./run.sh ash：只跑 s0 與 s5
cd "$(dirname "$0")"
OUT=results/run-$(date +%Y%m%d)$([ "$ONLY" = all ] || echo "-$ONLY").md
DC="docker compose --profile qe"
P=http://127.0.0.1:22990/api/v1
KEEP='__name__|database|cluster|source_uuid|domain_id|server_id|errno|error_name|thread|wait_class|digest|query|status|alertname|alertstate|instance'
q() { curl -s --get "$P/query" --data-urlencode "query=$1" | python3 -I -c '
import json, re, sys
d = json.load(sys.stdin)
if d.get("status") != "success": print("ERROR", d.get("error")); sys.exit()
keep = re.compile(sys.argv[1])
out = []
for x in d["data"]["result"]:
    lab = ",".join(f"{k}={v}" for k, v in sorted(x["metric"].items()) if keep.fullmatch(k))
    v = x["value"][1]
    try: v = f"{float(v):.6g}"
    except ValueError: pass
    out.append(f"{{{lab}}} {v}")
print("empty" if not out else "; ".join(sorted(out)))' "$KEEP"; }
log() { echo "$*" | tee -a "$OUT"; }
show() { log "- \`$1\` → $(q "$1")"; }
alerts() { log "- ALERTS → $(q 'count by (alertname, database, alertstate) (ALERTS{alertname!~"MySQLExporter.*"})')"; }
my() { $DC exec -T "$1" mysql -uroot -prootpw -N -e "$2" 2>&1 | grep -v 'Using a password'; }
mdb() { $DC exec -T "$1" mariadb -uroot -prootpw -N -e "$2" 2>&1; }
code() { log '```'; tee -a "$OUT"; log '```'; }
fails() { show 'sum by (database, query, status) (queries_total{job="query-exporter", status!="success"})'; show 'sum by (database) (database_errors_total{job="query-exporter"})'; }

$DC down -v >/dev/null 2>&1
if [ "$ONLY" = ash ]; then
  # 這台機器的容器時鐘讓 APScheduler 常跳過 interval 排程（見 run-20261109.md 的 s5）；
  # ash 模式把 interval 拿掉，查詢改成每次 scrape 執行（query-exporter 的 aperiodic 模式），只為了驗證 SQL → metric。
  sed '/^    interval: /d' ../../../../queries/query-exporter/mysql.yaml > .qe-aperiodic.yaml
  export QE_CONFIG=./.qe-aperiodic.yaml
fi
: > "$OUT"
log "# query-exporter 自訂查詢實驗 $(date +%Y-%m-%d)"
log ""
log "MySQL 8.4.7（my1 → my2）、MariaDB 11.4（mdb1 → mdb2）、query-exporter 5.2.2、mysqld_exporter v0.21.0、Prometheus v3.13.5（scrape／evaluation 5s）。"
log "設定檔：queries/query-exporter/mysql.yaml（$([ "$ONLY" = ash ] && echo "刪掉 interval，每次 scrape 執行" || echo "原樣掛載")）；規則：rules/targets/mysql.yml。"
$DC up -d --wait my1 my2 mdb1 mdb2 >/dev/null 2>&1 || { echo "DB 起不來"; $DC down -v; exit 1; }
# healthcheck 在 entrypoint 的暫時 server 上就會通過；等到 init 跑完（root 密碼生效、app 帳號存在）
for n in my1 my2; do for i in $(seq 60); do [ "$(my $n "SELECT COUNT(*) FROM mysql.user WHERE user='qexporter'")" = 1 ] && break; sleep 2; done; done
for n in mdb1 mdb2; do for i in $(seq 60); do [ "$(mdb $n "SELECT COUNT(*) FROM mysql.user WHERE user='qexporter'")" = 1 ] && break; sleep 2; done; done
sleep 5
log "- 版本：$(my my1 'SELECT @@version')、$(mdb mdb1 'SELECT @@version')"
my my2 "RESET BINARY LOGS AND GTIDS; CHANGE REPLICATION SOURCE TO SOURCE_HOST='my1', SOURCE_USER='repl', SOURCE_PASSWORD='replpw', SOURCE_AUTO_POSITION=1, GET_SOURCE_PUBLIC_KEY=1; START REPLICA; SET GLOBAL super_read_only=ON;"
mdb mdb2 "RESET MASTER; CHANGE MASTER TO MASTER_HOST='mdb1', MASTER_USER='repl', MASTER_PASSWORD='replpw', MASTER_USE_GTID=slave_pos; START SLAVE; SET GLOBAL read_only=ON;"
mdb mdb1 "UPDATE performance_schema.setup_consumers SET ENABLED='YES' WHERE NAME IN ('events_statements_current', 'events_waits_current');
          UPDATE performance_schema.setup_instruments SET ENABLED='YES', TIMED='YES' WHERE NAME LIKE 'wait/lock/metadata/%' OR NAME LIKE 'wait/io/table/%' OR NAME LIKE 'wait/lock/table/%' OR NAME LIKE 'wait/io/file/innodb/%';"
$DC exec -T mdb1 sh -c 'mariadb -uroot -prootpw < /ash-sampler.sql'
mdb mdb1 "SET SESSION sql_log_bin=0; GRANT SELECT, EVENT ON ash.* TO 'qexporter'@'%';"
$DC up -d qe ex-my1 ex-mdb1 prom >/dev/null 2>&1
sleep 40

log ""
log "## s0 基線"
show 'count by (query, status) (queries{job="query-exporter"})'
show 'mysql_q_threads_connected'
show 'mysql_q_max_connections'
show 'mysql_q_read_only'
show 'mysql_q_gtid_executed_transactions'
show 'mysql_q_gtid_binlog_state_seq'
show 'mysql_q_mdl_waiting_sessions'
show 'mysql_q_replica_last_error_timestamp_seconds'
show 'mysql_q_ash_sampler_enabled'
show 'count(count by (__name__, database, source_uuid, domain_id, server_id, wait_class, digest, errno, channel, thread) ({__name__=~"mysql_q_.*"}))'
alerts

if [ "$ONLY" = all ]; then
log ""
log "## s1 連線打滿（my1、mdb1 的 max_connections=10，開 15 條 app 連線 SELECT SLEEP(75)）"
$DC exec -T my1 sh -c 'for i in $(seq 15); do mysql -uapp -papppw -e "SELECT SLEEP(75)" 2>/dev/null & done; wait' &
$DC exec -T mdb1 sh -c 'for i in $(seq 15); do mariadb -uapp -papppw -e "SELECT SLEEP(75)" 2>/dev/null & done; wait' &
sleep 20
log "- app 帳號的連線數（root 用保留的 +1 連線查）：my1 $(my my1 "SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE USER='app'")、mdb1 $(mdb mdb1 "SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE USER='app'")"
log "- 一般 port 用 qexporter 新開連線："
{ $DC exec -T my1 mysql -uqexporter -pqepw -hmy1 -P3306 -N -e 'SELECT "my1:3306 ok"' 2>&1 | grep -v 'Using a password'
  $DC exec -T mdb1 mariadb -uqexporter -pqepw -hmdb1 -P3306 -N -e 'SELECT "mdb1:3306 ok"' 2>&1; } | code
log "- admin port／extra_port 用 qexporter 新開連線："
{ $DC exec -T my1 mysql -uqexporter -pqepw -hmy1 -P33062 -N -e 'SELECT "my1:33062 ok"' 2>&1 | grep -v 'Using a password'
  $DC exec -T mdb1 mariadb -uqexporter -pqepw -hmdb1 -P3307 -N -e 'SELECT "mdb1:3307 ok"' 2>&1; } | code
show 'mysql_up{job="mysql"}'
show 'mysql_global_status_threads_connected{job="mysql"}'
show 'mysql_q_threads_connected'
show 'mysql_q_threads_connected / mysql_q_max_connections'
sleep 45
show 'mysql_q_threads_connected'
alerts
fails
wait
sleep 10
show 'mysql_up{job="mysql"}'
show 'max by (instance) (increase(mysql_global_status_connection_errors_total{job="mysql", error="max_connections"}[3m]))'

log ""
log "## s2 metadata lock：閒置交易擋住 ALTER，ALTER 後面排一個 SELECT"
blocker() { (echo "BEGIN; SELECT * FROM app.t LIMIT 1;"; sleep 75; echo "COMMIT;") | $DC exec -T "$1" "$2" -uapp -papppw app 2>/dev/null; }
blocker my1 mysql & blocker mdb1 mariadb &
sleep 3
for s in "my1 mysql" "mdb1 mariadb"; do set -- $s
  $DC exec -T "$1" "$2" -uroot -prootpw -e "ALTER TABLE app.t ADD COLUMN c INT" 2>/dev/null &
done
sleep 2
for s in "my1 mysql" "mdb1 mariadb"; do set -- $s
  $DC exec -T "$1" "$2" -uapp -papppw -e "SELECT COUNT(*) FROM app.t" 2>/dev/null >/dev/null &
done
sleep 50
show 'mysql_q_mdl_waiting_sessions{database=~"my1|mdb1"}'
show 'mysql_q_mdl_oldest_wait_seconds{database=~"my1|mdb1"}'
show 'mysql_q_mdl_blocking_sessions{database=~"my1|mdb1"}'
show 'mysql_q_mdl_idle_blocking_sessions{database=~"my1|mdb1"}'
show 'mysql_q_mdl_blocker_age_seconds{database=~"my1|mdb1"}'
show 'mysql_info_schema_processlist_threads{job="mysql"}'
log "- 同一時間 MySQL 的 sys.schema_table_lock_waits："
my my1 "SELECT object_name, waiting_lock_type, blocking_lock_type, blocking_pid IS NOT NULL FROM sys.schema_table_lock_waits" | code
alerts
wait
sleep 20
show 'mysql_q_mdl_waiting_sessions{database=~"my1|mdb1"}'
show 'mysql_q_mdl_blocking_sessions{database=~"my1|mdb1"}'

log ""
log "## s3 replica 錯誤碼：my2 先有 id=100（不寫 binlog），my1 再寫 id=100 → 1062"
my my2 "SET GLOBAL super_read_only=OFF; SET SESSION sql_log_bin=0; INSERT INTO app.t (id) VALUES (100); SET GLOBAL super_read_only=ON;"
mdb mdb2 "SET SESSION sql_log_bin=0; INSERT INTO app.t (id) VALUES (100);"
my my1 "INSERT INTO app.t (id) VALUES (100);"
mdb mdb1 "INSERT INTO app.t (id) VALUES (100);"
sleep 20
show 'mysql_q_replica_last_error_timestamp_seconds'
log "- my2 的 SHOW REPLICA STATUS（Last_SQL_Errno、Replica_SQL_Running）："
$DC exec -T my2 mysql -uroot -prootpw -e "SHOW REPLICA STATUS\G" 2>/dev/null | grep -E "Last_SQL_Errno|Replica_SQL_Running:" | code
log "- MariaDB 的 performance_schema.replication_applier_status_by_worker 列數（mdb2 performance_schema=OFF；slave_parallel_threads=0）與 Last_SQL_Errno："
{ mdb mdb2 "SELECT @@performance_schema, @@slave_parallel_threads, (SELECT COUNT(*) FROM performance_schema.replication_applier_status_by_worker)"
  $DC exec -T mdb2 mariadb -uroot -prootpw -e "SHOW SLAVE STATUS\G" | grep -E "Last_SQL_Errno"; } | code
sleep 50
alerts
my my2 "SET GLOBAL super_read_only=OFF; SET SESSION sql_log_bin=0; DELETE FROM app.t WHERE id=100; SET GLOBAL super_read_only=ON; START REPLICA SQL_THREAD;"
mdb mdb2 "SET SESSION sql_log_bin=0; DELETE FROM app.t WHERE id=100; START SLAVE SQL_THREAD;"
sleep 15
log "- 修好（刪掉 my2 的 id=100、START REPLICA SQL_THREAD）後 15 秒："
show 'mysql_q_replica_last_error_timestamp_seconds'
my my2 "SELECT COUNT(*) FROM performance_schema.replication_applier_status_by_worker WHERE LAST_ERROR_NUMBER <> 0" | code
sleep 70
log "- 再 70 秒（expiration 60 秒）："
show 'mysql_q_replica_last_error_timestamp_seconds'

log ""
log "## s4 errant GTID：在 read_only 的 replica 上直接寫一筆（寫 binlog）"
MYSET=$(my my1 "SELECT REPLACE(@@gtid_executed, '\n', '')")
my my2 "SET GLOBAL super_read_only=OFF; INSERT INTO app.t (id) VALUES (200); SET GLOBAL super_read_only=ON;"
mdb mdb2 "INSERT INTO app.t (id) VALUES (200);"
log "- my2：GTID_SUBSET(my2 的 gtid_executed, my1 的 gtid_executed) = $(my my2 "SELECT GTID_SUBSET(@@gtid_executed, '$MYSET')")；my2 server_uuid $(my my2 'SELECT @@server_uuid')"
log "- mdb2：gtid_binlog_pos $(mdb mdb2 'SELECT @@gtid_binlog_pos')、gtid_binlog_state $(mdb mdb2 'SELECT @@gtid_binlog_state')、gtid_slave_pos $(mdb mdb2 'SELECT @@gtid_slave_pos')"
mdb mdb1 "INSERT INTO app.t (id) VALUES (201);"
sleep 5
log "- mdb1 再寫一筆並複寫到 mdb2 後：mdb2 gtid_binlog_pos $(mdb mdb2 'SELECT @@gtid_binlog_pos')、gtid_binlog_state $(mdb mdb2 'SELECT @@gtid_binlog_state')"
sleep 40
show 'mysql_q_gtid_executed_transactions'
show 'mysql_q_gtid_binlog_state_seq'
alerts

fi
log ""
log "## s5 MariaDB ASH：一個交易持有 app.t id=1 的 row lock 40 秒，3 個 UPDATE 等它；之後停用取樣 EVENT"
mdb mdb1 "SET GLOBAL innodb_lock_wait_timeout=60"
$DC exec -T mdb1 mariadb -uapp -papppw app -e "BEGIN; UPDATE t SET v=v+1 WHERE id=1; DO SLEEP(40); COMMIT;" >/dev/null 2>&1 &
sleep 2
for i in 1 2 3; do $DC exec -T mdb1 mariadb -uapp -papppw app -e "UPDATE t SET v=v+1 WHERE id=1" >/dev/null 2>&1 & done
sleep 35
show 'mysql_q_ash_active_sessions'
show 'sum by (database) (mysql_q_ash_active_sessions{wait_class="lock_row"})'
show 'mysql_global_status_innodb_row_lock_current_waits{job="mysql"}'
log "- 同一時間直接查 ash.aas_60s："
mdb mdb1 "SELECT wait_class, digest_label, aas FROM ash.aas_60s" | code
fails
wait
mdb mdb1 "ALTER EVENT ash.ev_sample DISABLE"
sleep 40
show 'mysql_q_ash_sampler_enabled'
show 'mysql_q_ash_sampler_last_run_age_seconds'
alerts
mdb mdb1 "ALTER EVENT ash.ev_sample ENABLE"

if [ "$ONLY" = all ]; then
log ""
log "## s6 mdb2 停機：自訂 metric 在 expiration 後消失，失敗計數增加"
$DC stop mdb2 >/dev/null 2>&1
sleep 20
show 'count by (database) ({__name__=~"mysql_q_.*", database="mdb2"})'
show 'sum by (database) (database_errors_total{job="query-exporter"})'
show 'sum by (database, status) (queries_total{job="query-exporter", database="mdb2"})'
sleep 70
log "- 停機後約 90 秒："
show 'count by (database) ({__name__=~"mysql_q_.*", database="mdb2"})'
show 'sum by (database) (database_errors_total{job="query-exporter"})'
show 'sum by (database, query) (increase(queries_total{job="query-exporter", status="success", database="mdb2"}[1m]))'
alerts
log "- query-exporter log（最後幾行與 mdb2 有關的錯誤）："
$DC logs qe 2>&1 | grep -i "mdb2" | grep -iE "error|fail" | tail -3 | cut -c1-300 | code
fi
log "- APScheduler 跳過的排程次數（misfire_grace_time 預設 1 秒；這台機器容器的單調時鐘比牆鐘慢），依查詢："
$DC logs qe 2>&1 | grep -o 'Run time of job "[a-z_]*' | sed 's/Run time of job "//' | sort | uniq -c | code
show 'sum by (database, query) (queries_total{job="query-exporter", status="success"})'
log "- query-exporter log 中所有不同的錯誤訊息（去掉時間）："
$DC logs qe 2>&1 | grep -E "Error|error|timeout" | grep -v "was missed by" | sed -E 's/^qe-1 +\| //; s/^[0-9-]+ [0-9:]+ //' | cut -c1-200 | sort | uniq -c | sort -rn | head -15 | code

$DC down -v >/dev/null 2>&1
rm -f .qe-aperiodic.yaml
log ""
log "（結束：docker compose down -v）"
