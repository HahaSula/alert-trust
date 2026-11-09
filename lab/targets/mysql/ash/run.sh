#!/usr/bin/env bash
# 九個面向補缺＋MariaDB ASH 取樣器原型。約 8–10 分鐘，結束後 docker compose down -v。
#   A Group Replication：三台 ONLINE 時 exporter 輸出什麼；暫停兩台（失去 quorum）後倖存節點看到什麼
#   B Galera：沒開 Galera 的 MariaDB 輸出哪些 wsrep_*；三台正常；一台限 CPU 觸發 flow control；暫停兩台變 non-Primary
#   C 資料保護：binlog 開關、保留期限、sync_binlog；MariaDB 沒有 replica 時 binlog 會不會依 binlog_expire_logs_seconds 刪除
#   D 容量：auto_increment 與 schema 大小；MySQL information_schema 統計快取（information_schema_stats_expiry）
#   E ASH：MariaDB 每秒取樣 PROCESSLIST＋performance_schema；row lock、metadata lock、SLEEP、重查詢同時發生時取樣結果與成本
# 輸出：results/run-YYYYMMDD.md。時間用 host 的 wall clock（這台機器容器時鐘不準）。
# ./run.sh ash：只起 mdb、gr1 與它們的 exporter，只跑 E（約 2 分鐘），輸出 results/run-ash-YYYYMMDD.md。
set -u
cd "$(dirname "$0")"
MODE=${1:-all}
if [ "$MODE" = ash ]; then OUT=results/run-ash-$(date +%Y%m%d).md; UP="mdb ex-mdb gr1 ex-gr1"; else OUT=results/run-$(date +%Y%m%d).md; UP=""; fi
DC="docker compose"
log() { echo "$*" | tee -a "$OUT"; }
code() { log '```'; tee -a "$OUT"; log '```'; }
msql() { $DC exec -T "$1" mariadb -uroot -prootpw -N -e "$2" 2>&1; }
msqlt() { $DC exec -T "$1" mariadb -uroot -prootpw -t -e "$2" 2>&1; }
ysql() { $DC exec -T "$1" mysql -uroot -prootpw -N -e "$2" 2>&1 | grep -v 'Using a password'; }
ysqlt() { $DC exec -T "$1" mysql -uroot -prootpw -t -e "$2" 2>&1 | grep -v 'Using a password'; }
mbg() { $DC exec -d -T "$1" sh -c "mariadb -uroot -prootpw -e \"$2\" >/tmp/bg.\$\$.log 2>&1"; }
ybg() { $DC exec -d -T "$1" sh -c "mysql -uroot -prootpw -e \"$2\" >/tmp/bg.\$\$.log 2>&1"; }
metrics() { curl -s "http://127.0.0.1:$1/metrics" | grep -v '^#' | grep -E "$2" | sort; }
section() { log ""; log "## $1  ($(date +%T))"; }

trap '$DC down -v >/dev/null 2>&1' EXIT
mkdir -p results; : > "$OUT"
$DC down -v >/dev/null 2>&1
log "# run $(date '+%F %T')"
log ""
log "映像：mariadb:11.4（$(docker run --rm --entrypoint mariadbd mariadb:11.4 --version | awk '{print $3}')，galera-4 26.4.27）、mysql:8.4.7、prom/mysqld-exporter:v0.21.0"
$DC up -d --wait $UP >/dev/null 2>&1 || { log "compose up 失敗"; $DC ps -a | code; exit 1; }
log "- compose up 完成 $(date +%T)"

if [ "$MODE" = all ]; then
# ---------- A：Group Replication ----------
section "A1 Group Replication 三台 ONLINE"
ysql gr1 "SET GLOBAL group_replication_bootstrap_group=ON; START GROUP_REPLICATION; SET GLOBAL group_replication_bootstrap_group=OFF;"
for n in gr2 gr3; do ysql $n "START GROUP_REPLICATION USER='rpl', PASSWORD='rplpw';"; done
for i in $(seq 1 40); do
  c=$(ysql gr1 "SELECT COUNT(*) FROM performance_schema.replication_group_members WHERE MEMBER_STATE='ONLINE'")
  [ "$c" = 3 ] && break; sleep 2
done
log "- ONLINE 成員數（gr1 看）：$c"
ysqlt gr1 "SELECT MEMBER_HOST, MEMBER_STATE, MEMBER_ROLE, MEMBER_VERSION FROM performance_schema.replication_group_members ORDER BY MEMBER_HOST" | code
ysql gr1 "CREATE DATABASE app; CREATE TABLE app.ai (id TINYINT AUTO_INCREMENT PRIMARY KEY, x INT); CREATE TABLE app.w (id INT AUTO_INCREMENT PRIMARY KEY, v INT);"
sleep 6
metrics 22802 'replication_group|transactions_|collector_success\{collector="perf_schema.replication|flow_control_(applier|certifier)_threshold|flow_control_mode|unreachable_majority_timeout|member_expel_timeout|mysql_global_status_gr_' | code

section "A2 暫停 gr2、gr3（gr1 失去 quorum）"
docker pause at-myasp-gr2-1 at-myasp-gr3-1 >/dev/null
T=$(date +%s); sleep 20
log "- 暫停後 $(( $(date +%s) - T )) 秒"
ysqlt gr1 "SELECT MEMBER_HOST, MEMBER_STATE, MEMBER_ROLE FROM performance_schema.replication_group_members ORDER BY MEMBER_HOST" | code
metrics 22802 'replication_group_member_info|^mysql_up|collector_success\{collector="perf_schema.replication_group_members' | code
log "- gr1 寫入（5 秒 timeout）："
$DC exec -T gr1 sh -c "timeout 5 mysql -uroot -prootpw -e 'INSERT INTO app.w (v) VALUES (1)' 2>&1; echo exit=\$?" | grep -v 'Using a password' | code
docker unpause at-myasp-gr2-1 at-myasp-gr3-1 >/dev/null
for i in $(seq 1 30); do
  c=$(ysql gr1 "SELECT COUNT(*) FROM performance_schema.replication_group_members WHERE MEMBER_STATE='ONLINE'")
  [ "$c" = 3 ] && break; sleep 2
done
log "- 恢復後 $(( $(date +%s) - T )) 秒，ONLINE 成員數：$c"
ysqlt gr1 "SELECT MEMBER_HOST, MEMBER_STATE FROM performance_schema.replication_group_members ORDER BY MEMBER_HOST" | code
ysqlt gr1 "SELECT COUNT(*) AS rows_in_app_w FROM app.w" | code

# ---------- B：Galera ----------
section "B1 沒開 Galera 的 MariaDB（mdb）輸出的 wsrep_*"
metrics 22801 'wsrep|galera' | code
section "B2 Galera 三台正常（gal1）"
msqlt gal1 "SHOW GLOBAL STATUS WHERE Variable_name IN ('wsrep_cluster_status','wsrep_cluster_size','wsrep_ready','wsrep_local_state','wsrep_flow_control_paused','wsrep_flow_control_paused_ns','wsrep_local_recv_queue','wsrep_flow_control_active')" | code
metrics 22803 'wsrep_(cluster_status|cluster_size|ready|local_state |flow_control_paused|flow_control_sent|flow_control_recv|local_recv_queue |local_cert_failures|flow_control_active)|galera_status_info|wsrep_on |wsrep_desync ' | code

section "B3 flow control：gal2 限 0.05 CPU、gcs.fc_limit=1，gal1 寫 3000 筆單列交易"
msql gal1 "CREATE TABLE app.fc (id INT AUTO_INCREMENT PRIMARY KEY, pad CHAR(100));"
sleep 2
msql gal2 "SET GLOBAL wsrep_provider_options='gcs.fc_limit=1';"
before1=$(metrics 22803 '^mysql_global_status_wsrep_flow_control_paused_ns ' | awk '{print $2}')
before2=$(metrics 22804 '^mysql_global_status_wsrep_flow_control_sent ' | awk '{print $2}')
docker update --cpus 0.05 at-myasp-gal2-1 >/dev/null
T=$(date +%s)
python3 -I -c 'print("\n".join("INSERT INTO app.fc (pad) VALUES (MD5(%d));" % i for i in range(3000)))' \
  | $DC exec -T gal1 mariadb -uroot -prootpw &
WP=$!
sleep 6
log "- 寫入中（$(( $(date +%s) - T )) 秒）gal2："
metrics 22804 'wsrep_(local_recv_queue |flow_control_sent |flow_control_paused_ns )' | code
log "- 寫入中 gal1："
metrics 22803 'wsrep_(flow_control_paused_ns |flow_control_recv |local_send_queue )' | code
wait $WP
log "- 3000 筆寫完：$(( $(date +%s) - T )) 秒"
docker update --cpus 0 at-myasp-gal2-1 >/dev/null 2>&1 || docker update --cpus 2 at-myasp-gal2-1 >/dev/null
after1=$(metrics 22803 '^mysql_global_status_wsrep_flow_control_paused_ns ' | awk '{print $2}')
after2=$(metrics 22804 '^mysql_global_status_wsrep_flow_control_sent ' | awk '{print $2}')
log "- gal1 flow_control_paused_ns：$before1 → $after1；gal2 flow_control_sent：$before2 → $after2"
msql gal2 "SET GLOBAL wsrep_provider_options='gcs.fc_limit=16';"
log "- 對照：同樣 3000 筆、不限 CPU"
T=$(date +%s)
python3 -I -c 'print("\n".join("INSERT INTO app.fc (pad) VALUES (MD5(%d));" % i for i in range(3000)))' \
  | $DC exec -T gal1 mariadb -uroot -prootpw
log "- 3000 筆寫完：$(( $(date +%s) - T )) 秒"

section "B4 暫停 gal2、gal3（gal1 變 non-Primary）"
docker pause at-myasp-gal2-1 at-myasp-gal3-1 >/dev/null
T=$(date +%s); sleep 25
log "- 暫停後 $(( $(date +%s) - T )) 秒"
msqlt gal1 "SHOW GLOBAL STATUS WHERE Variable_name IN ('wsrep_cluster_status','wsrep_cluster_size','wsrep_ready','wsrep_local_state','wsrep_local_state_comment')" | code
metrics 22803 'wsrep_(cluster_status|cluster_size|ready|local_state) |^mysql_up|collector_success' | code
log "- gal1 讀取："
msql gal1 "SELECT COUNT(*) FROM app.fc" | code
docker unpause at-myasp-gal2-1 at-myasp-gal3-1 >/dev/null
for i in $(seq 1 30); do
  s=$(msql gal1 "SELECT VARIABLE_VALUE FROM information_schema.GLOBAL_STATUS WHERE VARIABLE_NAME='WSREP_CLUSTER_SIZE'")
  [ "$s" = 3 ] && break; sleep 2
done
log "- 恢復後 $(( $(date +%s) - T )) 秒：cluster_size=$s"
metrics 22803 'wsrep_(cluster_status|cluster_size|ready) ' | code

# ---------- C：資料保護 ----------
section "C1 binlog 與耐久性設定（exporter 輸出）"
log "- MariaDB（mdb，--log-bin、binlog_expire_logs_seconds=20）："
metrics 22801 '^mysql_binlog_|global_variables_(log_bin|binlog_expire_logs_seconds|expire_logs_days|max_binlog_total_size|slave_connections_needed_for_purge|sync_binlog|innodb_flush_log_at_trx_commit|gtid_strict_mode|binlog_format) ' | code
log "- MySQL（gr1，預設值）："
metrics 22802 '^mysql_binlog_|global_variables_(log_bin|binlog_expire_logs_seconds|binlog_expire_logs_auto_purge|sync_binlog|innodb_flush_log_at_trx_commit|gtid_mode) ' | code
log "- 沒開 binlog 的 MariaDB（gal1）："
metrics 22803 '^mysql_binlog_|global_variables_log_bin |collector_success\{collector="global_variables' | code

section "C2 MariaDB 沒有 replica 時 binlog 會不會過期刪除"
msql mdb "FLUSH BINARY LOGS; FLUSH BINARY LOGS; FLUSH BINARY LOGS;"
log "- FLUSH 三次後：$(msql mdb 'SHOW BINARY LOGS' | wc -l) 個檔"
sleep 30
msql mdb "FLUSH BINARY LOGS;"
log "- 30 秒後再 FLUSH（slave_connections_needed_for_purge=$(msql mdb 'SELECT @@slave_connections_needed_for_purge')）：$(msql mdb 'SHOW BINARY LOGS' | wc -l) 個檔"
msql mdb "SET GLOBAL slave_connections_needed_for_purge=0; FLUSH BINARY LOGS;"
log "- 改成 0 後 FLUSH：$(msql mdb 'SHOW BINARY LOGS' | wc -l) 個檔"
metrics 22801 '^mysql_binlog_(files|size_bytes|file_number) ' | code

# ---------- D：容量 ----------
section "D1 auto_increment 與 schema 大小"
msql mdb "INSERT INTO app.ai (x) SELECT n FROM app.d a, (SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9 UNION SELECT 10) b;"
log "- MariaDB：插入 100 列後"
metrics 22801 'auto_increment_column|schema_size_bytes|collector_success\{collector="(auto_increment|info_schema.schema_size)' | code
ysql gr1 "INSERT INTO app.ai (x) VALUES (1),(2),(3),(4),(5),(6),(7),(8),(9),(10);"
log "- MySQL：插入 10 列後第一次 scrape"
metrics 22802 'auto_increment_column\{' | code
ysql gr1 "INSERT INTO app.ai (x) SELECT 1 FROM performance_schema.setup_instruments LIMIT 50;"
log "- MySQL：再插入 50 列（實際 AUTO_INCREMENT=$(ysql gr1 "SELECT MAX(id)+1 FROM app.ai")），information_schema_stats_expiry=$(ysql gr1 'SELECT @@global.information_schema_stats_expiry')"
metrics 22802 'auto_increment_column\{' | code
ysql gr1 "ANALYZE TABLE app.ai" >/dev/null
log "- MySQL：ANALYZE TABLE 之後"
metrics 22802 'auto_increment_column\{' | code

fi
# ---------- E：ASH 取樣器 ----------
section "E1 MariaDB 診斷工具是否存在"
msqlt mdb "SELECT (SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='sys') AS sys_schema,
  (SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='performance_schema' AND TABLE_NAME LIKE 'events_statements_history%') AS stmt_history_tables,
  (SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='performance_schema' AND TABLE_NAME='events_statements_histogram_by_digest') AS digest_histogram,
  (SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='performance_schema' AND TABLE_NAME='events_errors_summary_global_by_error') AS errors_summary,
  (SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='performance_schema' AND TABLE_NAME='data_lock_waits') AS data_lock_waits,
  (SELECT GROUP_CONCAT(COLUMN_NAME) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='performance_schema' AND TABLE_NAME='events_statements_summary_by_digest' AND (COLUMN_NAME LIKE 'QUANTILE%' OR COLUMN_NAME LIKE '%CPU%' OR COLUMN_NAME LIKE 'QUERY_SAMPLE%')) AS digest_extra_cols" | code
ysqlt gr1 "SELECT (SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='performance_schema' AND TABLE_NAME='events_statements_histogram_by_digest') AS digest_histogram,
  (SELECT GROUP_CONCAT(COLUMN_NAME) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='performance_schema' AND TABLE_NAME='events_statements_summary_by_digest' AND (COLUMN_NAME LIKE 'QUANTILE%' OR COLUMN_NAME LIKE '%CPU%' OR COLUMN_NAME LIKE 'QUERY_SAMPLE%')) AS digest_extra_cols" | code
msqlt mdb "SELECT NAME, ENABLED FROM performance_schema.setup_consumers" | code

log "- 開 events_statements_current 與 events_waits_current 兩個 consumer（MariaDB 預設全關），以及 MDL、table I/O、InnoDB file I/O instrument"
section "E2 開取樣器，同時製造 row lock、metadata lock、SLEEP、重查詢"
msql mdb "UPDATE performance_schema.setup_consumers SET ENABLED='YES' WHERE NAME IN ('events_statements_current', 'events_waits_current');
          UPDATE performance_schema.setup_instruments SET ENABLED='YES', TIMED='YES' WHERE NAME LIKE 'wait/lock/metadata/%' OR NAME LIKE 'wait/io/table/%' OR NAME LIKE 'wait/lock/table/%' OR NAME LIKE 'wait/io/file/innodb/%';"
$DC exec -T mdb sh -c 'mariadb -uroot -prootpw < /ash-sampler.sql' 2>&1 | code
T=$(date +%s)
mbg mdb "BEGIN; UPDATE app.hot SET v=v+1 WHERE id=1; SELECT SLEEP(20); COMMIT;"
sleep 1
for i in 1 2 3; do mbg mdb "UPDATE app.hot SET v=v+1 WHERE id=1;"; done
mbg mdb "SELECT SLEEP(15);"
mbg mdb "SELECT COUNT(*) FROM app.big a JOIN app.big b ON a.k = b.k JOIN app.d c;"
mbg mdb "BEGIN; SELECT * FROM app.d LIMIT 1; SELECT SLEEP(15); COMMIT;"
sleep 1
mbg mdb "ALTER TABLE app.d ADD COLUMN z INT;"
sleep 1
for i in 1 2; do mbg mdb "SELECT * FROM app.d WHERE n = 1;"; done
sleep 6
log "- 情境開始後 $(( $(date +%s) - T )) 秒，exporter processlist collector："
metrics 22801 'processlist_threads\{' | grep -v ' 0$' | code
HEAVY=$(msql mdb "SELECT ID FROM information_schema.PROCESSLIST WHERE INFO LIKE 'SELECT COUNT(*) FROM app.big%' LIMIT 1")
log "- SHOW EXPLAIN FOR $HEAVY（執行中的重查詢）："
msqlt mdb "SHOW EXPLAIN FOR $HEAVY" | code
log "- SHOW ANALYZE FOR $HEAVY："
msqlt mdb "SHOW ANALYZE FOR $HEAVY" | code
sleep 16
log "- 情境開始後 $(( $(date +%s) - T )) 秒，取樣結果（每個 conn 每秒一列；依 state、等待、digest 加總 = active session 秒數）："
msqlt mdb "SELECT COUNT(DISTINCT ts) AS samples, COUNT(*) AS rows_, ROUND(COUNT(*)/COUNT(DISTINCT ts),2) AS avg_active FROM ash.sample" | code
msqlt mdb "SELECT LEFT(state,32) AS state, trx_state, wait_event, LEFT(COALESCE(digest_text, sql_head),60) AS stmt, COUNT(*) AS active_s
             FROM ash.sample GROUP BY 1,2,3,4 ORDER BY active_s DESC LIMIT 15" | code
log "- 有 digest 的列比例（events_statements_current 在語句執行中是否已有 DIGEST）："
msqlt mdb "SELECT SUM(digest IS NOT NULL) AS with_digest, SUM(wait_event IS NOT NULL) AS with_wait, COUNT(*) AS total FROM ash.sample" | code
log "- 彙總 view（給 exporter 用，最近 60 秒 AAS）："
msqlt mdb "SELECT * FROM ash.aas_60s ORDER BY aas DESC" | code

section "E3 取樣成本"
log "- 閒置時 CALL ash.take_sample() 200 次："
$DC exec -T mdb sh -c 'S=$(date +%s%N); for i in $(seq 1 4); do mariadb -uroot -prootpw -e "$(printf "CALL ash.take_sample();%.0s" $(seq 1 50))"; done; E=$(date +%s%N); echo "$S $E" | awk "{printf \"per_call_ms=%.2f (含 4 次 client 啟動)\\n\", (\$2-\$1)/200/1e6}"' 2>&1 | code
for i in $(seq 1 20); do mbg mdb "SELECT SLEEP(20);"; done
sleep 2
log "- 20 個 session 在跑時 CALL ash.take_sample() 200 次："
$DC exec -T mdb sh -c 'S=$(date +%s%N); for i in $(seq 1 4); do mariadb -uroot -prootpw -e "$(printf "CALL ash.take_sample();%.0s" $(seq 1 50))"; done; E=$(date +%s%N); echo "$S $E" | awk "{printf \"per_call_ms=%.2f (含 4 次 client 啟動)\\n\", (\$2-\$1)/200/1e6}"' 2>&1 | code
msqlt mdb "SELECT (SELECT COUNT(*) FROM ash.sample) AS rows_, ROUND((DATA_LENGTH+INDEX_LENGTH)/1024) AS kib FROM information_schema.TABLES WHERE TABLE_SCHEMA='ash' AND TABLE_NAME='sample'" | code
log "- MySQL 對照（gr1）：執行中的語句在 events_statements_current 有沒有 DIGEST"
ybg gr1 "SELECT SLEEP(8);"
sleep 3
ysqlt gr1 "SELECT t.PROCESSLIST_ID, s.DIGEST IS NOT NULL AS has_digest, LEFT(s.DIGEST_TEXT,30) AS digest_text, s.END_EVENT_ID IS NULL AS running
             FROM performance_schema.events_statements_current s JOIN performance_schema.threads t USING (THREAD_ID)
            WHERE s.SQL_TEXT LIKE 'SELECT SLEEP%'" | code

section "結束"
log "- $(date +%T)"
