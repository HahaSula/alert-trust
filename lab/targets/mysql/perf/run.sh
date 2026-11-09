#!/usr/bin/env bash
# 效能與故障情境（MariaDB 11.4、MySQL 8.4 同時跑，各自一個工作負載）：
#   s0 基線、s1 慢查詢與 digest、s2 row lock、s3 metadata lock（DDL 卡在長交易後面）、s4 purge 被長交易擋住、
#   s5 CPU 限制、s6 buffer pool 太小、s7 暫存表落到磁碟、s8 redo log 太小、s9 連線風暴、s10 磁碟滿（24 MB tmpfs）、
#   s11 OOM kill（先 MariaDB 再 MySQL）。
# 輸出：results/run-YYYYMMDD.md（情境中的快照）、results/raw/（工作負載逐秒紀錄、時間窗、Prometheus 時間序列）、
#       results/summary-YYYYMMDD.md（analyze.py 依時間窗算出的變化與先後）。
# 時間都是 host 的 wall clock；容器內的時鐘在這台機器上約慢 30%，所以 SLEEP(n) 在容器內實際比 n 秒長。
# ./run.sh focus：只跑 s0、s3（metadata lock，前後留空檔）與 s11（OOM），結果寫到 results/run-focus-*.md、results/raw-focus/。
set -u
cd "$(dirname "$0")"
MODE=${1:-all}
D=$(date +%Y%m%d)
if [ "$MODE" = focus ]; then OUT=results/run-focus-$D.md; RAW=results/raw-focus; SUM=results/summary-focus-$D.md
else OUT=results/run-$D.md; RAW=results/raw; SUM=results/summary-$D.md; fi
P=http://127.0.0.1:22690/api/v1
DC="docker compose --profile wl"
q() { curl -s --get "$P/query" --data-urlencode "query=$1" | python3 -I -c '
import json, sys
d = json.load(sys.stdin)
if d.get("status") != "success": print("ERROR", d.get("error")); sys.exit()
out = []
for x in d["data"]["result"]:
    m = x["metric"]; m.pop("job", None); m.pop("flavour", None)
    lab = ",".join(f"{k}={v}" for k, v in sorted(m.items()))
    v = x["value"][1]
    try: v = f"{float(v):.6g}"
    except ValueError: pass
    out.append(f"{{{lab}}} {v}")
print("empty" if not out else "; ".join(sorted(out)))'; }
log() { echo "$*" | tee -a "$OUT"; }
show() { log "- \`$1\` → $(q "$1")"; }
alerts() { log "- ALERTS firing → $(q 'count by (alertname, instance) (ALERTS{alertstate="firing"})')"; log "- ALERTS pending → $(q 'count by (alertname, instance) (ALERTS{alertstate="pending"})')"; }
sql() { local b=mysql; [ "$1" = mdb ] && b=mariadb; $DC exec -T "$1" $b -uroot -prootpw -N -e "$2" 2>&1 | grep -v 'Using a password'; }
sqlv() { local b=mysql; [ "$1" = mdb ] && b=mariadb; $DC exec -T "$1" $b -uroot -prootpw -t -e "$2" 2>&1 | grep -v 'Using a password'; }
bg() { local b=mysql; [ "$1" = mdb ] && b=mariadb; $DC exec -d -T "$1" sh -c "$b -h127.0.0.1 -uroot -prootpw -e \"$2\" >/tmp/bg.\$\$.log 2>&1"; }
wl() { local n=$1; shift; $DC run --rm -T --no-deps "wl-$n" "$@" >> "$RAW/$1-$n.log" 2>&1; }
T0=0
begin() { T0=$(date +%s); CUR=$1; log ""; log "## $1  ($(date +%T), t=$T0)"; }
finish() { printf '%s\t%s\t%s\n' "$CUR" "$T0" "$(date +%s)" >> "$RAW/windows.tsv"; }
at() { local s=$((T0 + $1 - $(date +%s))); [ $s -gt 0 ] && sleep $s; }   # 等到情境開始後第 $1 秒
both() { local c=$1; shift; for n in mdb my; do $c "$n" "$@"; done; }
BIGUPD="SET SESSION innodb_lock_wait_timeout=100; UPDATE app.big SET pad=MD5(RAND()), c=MD5(RAND()); UPDATE app.big SET pad=MD5(RAND()), c=MD5(RAND()); UPDATE app.big SET pad=MD5(RAND()), c=MD5(RAND());"
DBL="INSERT INTO app.oom (pad) SELECT pad FROM app.oom;"

trap '$DC down -v >/dev/null 2>&1' EXIT
mkdir -p "$RAW"; : > "$OUT"
if [ "$MODE" = focus ]; then rm -f results/raw-focus/*.log results/raw-focus/windows.tsv results/raw-focus/series.csv results/raw-focus/events-*.txt
else rm -f results/raw/*.log results/raw/windows.tsv results/raw/series.csv results/raw/events-*.txt; fi
$DC down -v >/dev/null 2>&1
docker compose up -d --wait >/dev/null 2>&1
sleep 30   # MySQL 映像的 healthcheck 在暫時 server 上就會過，等 init 跑完
for n in mdb my; do sql $n "ANALYZE TABLE app.sb; CREATE TABLE app.big LIKE app.sb; INSERT INTO app.big SELECT * FROM app.sb WHERE id <= 60000;" >/dev/null; done
$DC up -d wl-mdb wl-my >/dev/null 2>&1
log "# MySQL／MariaDB 效能與故障情境 $D（模式 $MODE）"
log ""
log "- 映像：mariadb:11.4（$(sql mdb 'select @@version')）、mysql:8.4.7、mysqld-exporter v0.21.0、prometheus v3.13.5"
log "- 每台資料庫：cpus=2、mem_limit=1g（無 swap）、buffer pool 128M、max_connections=100、innodb_lock_wait_timeout=10、long_query_time=0.5"
log "- 工作負載：每台 8 thread × 每秒最多 40 筆交易（點查 + 單列 UPDATE + COMMIT），表 20 萬列"
log "- scrape_interval 5s；rate 用 [20s]；時間是 host wall clock（容器時鐘約慢 30%）"
sleep 45

begin "s0 基線"
show 'rate(mysql_global_status_queries[20s])'
show 'rate(mysql_global_status_commands_total{command="commit"}[20s])'
show 'sum by (instance) (rate(mysql_global_status_innodb_row_ops_total[20s]))'
show 'mysql_global_status_threads_running'
show 'mysql_global_variables_performance_schema'
show 'count by (instance) (mysql_perf_schema_events_statements_total)'
show 'count by (instance) ({__name__=~"mysql_perf_schema_events_statements.*"})'
show 'scrape_duration_seconds'
alerts
at 40; finish

if [ "$MODE" = all ]; then
begin "s1 慢查詢、digest 與 cardinality"
for n in mdb my; do wl $n slow 40 & done
for n in mdb my; do wl $n digests 300; done
at 30
show 'increase(mysql_global_status_slow_queries[30s])'
show 'count by (instance) (mysql_perf_schema_events_statements_total)'
show 'count by (instance) ({__name__=~"mysql_perf_schema_events_statements.*"})'
show 'mysql_global_variables_performance_schema_digests_size'
show 'mysql_global_status_performance_schema_lost_total{instrumentation="digest"}'
show 'topk(3, sum by (instance, digest_text) (rate(mysql_perf_schema_events_statements_seconds_total[30s])))'
show 'topk(2, sum by (instance, digest_text) (rate(mysql_perf_schema_events_statements_rows_examined_total[30s])))'
show 'max by (instance, digest_text) (mysql_perf_schema_events_statements_latency{quantile="99"} > 0.3)'
show 'max by (instance) (mysql_perf_schema_events_statements_lock_time_seconds_total)'
show 'sum by (instance) (rate(mysql_perf_schema_events_statements_errors_total[30s]))'
show 'max by (instance) (mysql_exporter_collector_duration_seconds{collector="collect.perf_schema.eventsstatements"})'
show 'scrape_duration_seconds'
log '- sys.statement_analysis（MySQL）前 3：'
log '```'; sqlv my "SELECT LEFT(query,60) q, exec_count, total_latency, max_latency, rows_examined_avg, full_scan FROM sys.statement_analysis WHERE db='app' LIMIT 3" | tee -a "$OUT" >/dev/null; log '```'
log '- MariaDB 的 sys schema：'
log '```'; sqlv mdb "SELECT LEFT(query,60) q, exec_count, total_latency, rows_examined_avg FROM sys.statement_analysis WHERE db='app' LIMIT 3" | tee -a "$OUT" >/dev/null; log '```'
alerts
wait; at 45; finish

begin "s2 row lock：長交易鎖住 25% 的列"
both bg "BEGIN; UPDATE app.sb SET k=k WHERE id <= 50000; DO SLEEP(35); COMMIT;"
at 20
show 'mysql_global_status_innodb_row_lock_current_waits'
show 'rate(mysql_global_status_innodb_row_lock_time[20s])'
show 'rate(mysql_global_status_commands_total{command="commit"}[20s])'
show 'mysql_global_status_threads_running'
show 'sum by (instance, state) (mysql_info_schema_processlist_threads{state=~".*lock.*"})'
show 'max by (instance, state) (mysql_info_schema_innodb_trx_oldest_transaction_age_seconds)'
show 'mysql_info_schema_innodb_trx_transactions'
alerts
at 50; finish
fi

[ "$MODE" = focus ] && sleep 40   # 前一個情境完全恢復
begin "s3 metadata lock：ALTER TABLE 排在長交易後面"
both bg "BEGIN; SELECT c FROM app.sb WHERE id=1; DO SLEEP(40); COMMIT;"
sleep 3
both bg "ALTER TABLE app.sb ADD COLUMN x1 INT"
at 25
show 'rate(mysql_global_status_queries[20s])'
show 'rate(mysql_global_status_commands_total{command="commit"}[20s])'
show 'mysql_global_status_threads_running'
show 'mysql_global_status_innodb_row_lock_current_waits'
show 'sum by (instance, state) (mysql_info_schema_processlist_threads{state=~".*metadata.*"})'
show 'max by (instance, state) (mysql_info_schema_processlist_seconds{state=~".*metadata.*"})'
show 'max by (instance, state) (mysql_info_schema_innodb_trx_oldest_transaction_age_seconds)'
show 'mysql_perf_schema_events_waits_total{event_name="wait/lock/metadata/sql/mdl"}'
log '- performance_schema.metadata_locks（PENDING）：'
for n in mdb my; do log "  - $n: $(sql $n "SELECT COUNT(*), GROUP_CONCAT(DISTINCT LOCK_TYPE) FROM performance_schema.metadata_locks WHERE LOCK_STATUS='PENDING'" | tr '\t\n' '  ')"; done
log "  - my sys.schema_table_lock_waits: $(sql my "SELECT waiting_query, blocking_pid, sql_kill_blocking_connection FROM sys.schema_table_lock_waits LIMIT 1" | tr '\t\n' '  ')"
alerts
at 60
show 'mysql_perf_schema_events_waits_total{event_name="wait/lock/metadata/sql/mdl"}'
show 'increase(mysql_perf_schema_events_waits_seconds_total{event_name="wait/lock/metadata/sql/mdl"}[1m])'
at 75; finish

if [ "$MODE" = all ]; then

begin "s4 purge 被 consistent snapshot 擋住約 60 秒"
both bg "START TRANSACTION WITH CONSISTENT SNAPSHOT; SELECT COUNT(*) FROM app.d; DO SLEEP(55); COMMIT;"
at 50
show 'mysql_global_status_innodb_history_list_length'
show 'mysql_info_schema_innodb_metrics_transaction_trx_rseg_history_len'
show 'max by (instance) (mysql_info_schema_innodb_trx_oldest_transaction_age_seconds)'
show 'rate(mysql_global_status_commands_total{command="commit"}[20s])'
alerts
at 90
show 'mysql_global_status_innodb_history_list_length'
show 'mysql_info_schema_innodb_metrics_transaction_trx_rseg_history_len'
finish

begin "s5 CPU 限制 2 → 0.1"
for n in mdb my; do docker update --cpus 0.1 "at-myperf-$n-1" >/dev/null; done
at 30
show 'rate(mysql_global_status_queries[20s])'
show 'mysql_global_status_threads_running'
show 'scrape_duration_seconds'
show 'mysql_exporter_collector_duration_seconds{collector="collect.perf_schema.eventsstatements"}'
alerts
at 45
for n in mdb my; do docker update --cpus 2 "at-myperf-$n-1" >/dev/null; done
at 65; finish

begin "s6 buffer pool 128M → 16M"
both sql "SET GLOBAL innodb_buffer_pool_size=16777216" >/dev/null
at 40
show 'mysql_global_variables_innodb_buffer_pool_size'
show 'rate(mysql_global_status_innodb_buffer_pool_reads[20s])'
show 'rate(mysql_global_status_innodb_buffer_pool_read_requests[20s])'
show 'rate(mysql_global_status_innodb_buffer_pool_wait_free[20s])'
show 'rate(mysql_global_status_commands_total{command="commit"}[20s])'
alerts
at 60
both sql "SET GLOBAL innodb_buffer_pool_size=134217728" >/dev/null
at 80; finish

begin "s7 暫存表落到磁碟（tmp_table_size=64K）"
for n in mdb my; do wl $n tmp 40 & done
at 30
show 'rate(mysql_global_status_created_tmp_disk_tables[20s])'
show 'rate(mysql_global_status_created_tmp_tables[20s])'
show 'rate(mysql_global_status_created_tmp_disk_tables[20s]) / rate(mysql_global_status_created_tmp_tables[20s])'
show 'sum by (instance) (rate(mysql_perf_schema_events_statements_tmp_disk_tables_total[20s]))'
show 'rate(mysql_global_status_commands_total{command="commit"}[20s])'
wait; at 45; finish

begin "s8a 大量寫入（redo 預設大小，對照組）"
both bg "$BIGUPD"
at 35
show 'rate(mysql_global_status_innodb_log_waits[20s])'
show 'mysql_global_status_innodb_checkpoint_age'
show 'mysql_global_status_innodb_redo_log_current_lsn - mysql_global_status_innodb_redo_log_checkpoint_lsn'
show 'rate(mysql_global_status_commands_total{command="commit"}[20s])'
at 50; finish
begin "s8b 大量寫入（redo 縮到最小：MariaDB innodb_log_file_size=16M、MySQL innodb_redo_log_capacity=8M）"
sql mdb "SET GLOBAL innodb_log_file_size=16777216" >/dev/null
sql my "SET GLOBAL innodb_redo_log_capacity=8388608" >/dev/null
sleep 3
both bg "$BIGUPD"
at 35
show 'mysql_global_variables_innodb_log_file_size'
show 'mysql_global_variables_innodb_redo_log_capacity'
show 'rate(mysql_global_status_innodb_log_waits[20s])'
show 'mysql_global_status_innodb_checkpoint_age'
show 'mysql_global_status_innodb_redo_log_current_lsn - mysql_global_status_innodb_redo_log_checkpoint_lsn'
show 'rate(mysql_global_status_commands_total{command="commit"}[20s])'
alerts
at 55
sql mdb "SET GLOBAL innodb_log_file_size=100663296" >/dev/null
sql my "SET GLOBAL innodb_redo_log_capacity=104857600" >/dev/null
at 70; finish

begin "s9 連線風暴：150 條連線反覆連線＋全表掃描 30 秒（max_connections=100）"
for n in mdb my; do wl $n storm 150 30 & done
at 20
show 'mysql_up'
show 'mysql_global_status_threads_connected'
show 'mysql_global_status_threads_running'
show 'rate(mysql_global_status_commands_total{command="commit"}[20s])'
show 'scrape_duration_seconds'
alerts
wait; sleep 10
show 'increase(mysql_global_status_connection_errors_total{error="max_connections"}[1m])'
show 'increase(mysql_global_status_aborted_connects[1m])'
show 'mysql_global_status_max_used_connections'
at 60; finish

begin "s10 磁碟滿：DATA DIRECTORY 在 24 MB tmpfs 的表寫到滿"
for n in mdb my; do wl $n fill & done
wait
show 'mysql_up'
show 'rate(mysql_global_status_commands_total{command="commit"}[20s])'
log "- error log（最後 3 行含 full/space）："
for n in mdb my; do log "  - $n: $(docker logs at-myperf-$n-1 2>&1 | grep -iE 'full|space|ENOSPC' | tail -3 | tr '\n' ' ' | cut -c1-400)"; done
# tmpfs 在容器重啟後是空的；不刪掉這張表，之後的 crash recovery 會因找不到 .ibd 而失敗（第一次執行時 MariaDB 因此重啟迴圈）
both sql "DROP TABLE app.fill" >/dev/null
at 30; finish
fi

oom() {
  local n=$1 c=at-myperf-$1-1
  local use; use=$(docker stats --no-stream --format '{{.MemUsage}}' "$c" | awk '{print $1}')
  local mib; mib=$(python3 -I -c "import re,sys; v,u=re.match(r'([\d.]+)(\w+)',sys.argv[1]).groups(); print(int(float(v)*{'KiB':1/1024,'MiB':1,'GiB':1024}[u]))" "$use")
  local lim=$((mib + 150))
  begin "s11-$n OOM kill：mem_limit 1g → ${lim}m（目前用量 ${mib}MiB + 150）、buffer pool 加到 1G、寫入大表"
  timeout 240 docker events --filter container=$c --format '{{.Time}} {{.Action}} {{.Actor.Attributes.exitCode}}' > "$RAW/events-$n.txt" 2>&1 &
  local evpid=$!
  docker update --memory ${lim}m --memory-swap ${lim}m "$c" >/dev/null
  sql $n "SET GLOBAL innodb_buffer_pool_size=1073741824; CREATE TABLE app.oom (id INT AUTO_INCREMENT PRIMARY KEY, pad VARCHAR(4000)); INSERT INTO app.oom (pad) SELECT REPEAT('z',4000) FROM app.d;" >/dev/null
  bg $n "$DBL $DBL $DBL $DBL $DBL $DBL $DBL $DBL $DBL $DBL $DBL $DBL $DBL $DBL $DBL $DBL"
  local killed=""
  for i in $(seq 1 120); do
    sleep 1
    if grep -q ' die' "$RAW/events-$n.txt"; then killed=$(date +%s); break; fi
  done
  if [ -z "$killed" ]; then log "- 120 秒內沒有被 kill"; else
    log "- die 事件：情境開始後 $((killed - T0)) 秒；事件：$(grep -v exec_ "$RAW/events-$n.txt" | head -4 | tr '\n' ';')"
    local up=""
    for i in $(seq 1 180); do
      if [ "$(q "mysql_up{instance=\"$n\"}" | awk '{print $NF}')" = 1 ] && [ "$(q "mysql_global_status_uptime{instance=\"$n\"} < 300" )" != empty ]; then up=$(date +%s); break; fi
      sleep 1
    done
    if [ -n "$up" ]; then log "- mysql_up 回到 1（且 uptime < 300）：die 後 $((up - killed)) 秒"; else log "- die 後 180 秒內 mysql_up 沒有回到 1"; fi
    sleep 5
    show "mysql_global_status_uptime{instance=\"$n\"}"
    show "min_over_time(mysql_up{instance=\"$n\"}[3m])"
    show "count_over_time((mysql_up{instance=\"$n\"} == 0)[3m:5s])"
    show "resets(mysql_global_status_uptime{instance=\"$n\"}[5m])"
    show "rate(mysql_global_status_commands_total{instance=\"$n\",command=\"commit\"}[20s])"
    log "- error log 的 recovery 行："
    log '```'; docker logs --since "$T0" "$c" 2>&1 | grep -iE 'recover|crash|ready for connections|starting as process|Starting MariaDB|apply|redo|rollback|Aborting' | head -14 | cut -c1-200 | tee -a "$OUT" >/dev/null; log '```'
  fi
  docker update --memory 1g --memory-swap 1g "$c" >/dev/null
  sleep 15
  log "- 事件總數：oom $(grep -c ' oom' "$RAW/events-$n.txt")、die $(grep -c ' die' "$RAW/events-$n.txt")、start $(grep -c ' start' "$RAW/events-$n.txt")"
  kill $evpid 2>/dev/null
  alerts
  finish
}
oom mdb
oom my
sleep 10

# 整段時間序列與工作負載紀錄
python3 -I analyze.py dump "$P" "$RAW"
for n in mdb my; do $DC logs --no-log-prefix "wl-$n" > "$RAW/run-$n.log" 2>&1; done
python3 -I analyze.py summary "$RAW" > "$SUM"
log ""; log "（逐情境的指標變化見 $SUM）"
