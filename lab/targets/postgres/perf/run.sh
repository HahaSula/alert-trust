#!/usr/bin/env bash
# PostgreSQL 效能與故障情境 S0–S9，約 25 分鐘（牆鐘）；S10（OOM）在 run-s10.sh。原始輸出寫到 results/run-<日期>.txt，pgbench 進度寫到 results/pgbench-*.txt，
# 結束會 docker compose down -v。
# 每個情境：pgbench 背景負載（-R 限速，固定 8 個 client）-> 中途製造狀況 -> 結束後用 query_range 印出 exporter 看到的時間序列，
# 並在狀況發生中用 psql 直接查 pg_stat_activity／pg_locks／pg_stat_statements 對照。
# 時間：t+秒 是主機牆鐘。容器內的 monotonic clock 比牆鐘慢約 30%，pgbench 的 -T、-P、-R 都用容器時鐘，
# 所以 -T 60 實際約 80 秒、-R 200 實際約 150 tps；比較時看同一次的前後，不看絕對值。
set -u
cd "$(dirname "$0")"
P=http://127.0.0.1:23690
OUT=results/run-$(date +%Y%m%d).txt
mkdir -p results
DC="docker compose"

q() { for e in "$@"; do echo "  > $e"; curl -s --get "$P/api/v1/query" --data-urlencode "query=$e" | python3 -I fmt_instant.py; done; }
# qr 起點 終點 expr...：每 5 秒一個值
qr() { local s=$1 e=$2; shift 2; for x in "$@"; do echo "  >> $x"; curl -s --get "$P/api/v1/query_range" \
  --data-urlencode "query=$x" --data-urlencode "start=$s" --data-urlencode "end=$e" --data-urlencode "step=5" | python3 -I fmt_range.py "$s"; done; }
sql() { echo "  \$ $*"; $DC exec -T pg psql -U postgres -d bench -qAtX -F ' | ' -c "$*" 2>&1 | sed 's/^/    /'; }
sqlq() { $DC exec -T pg psql -U postgres -d bench -qAtX -c "$*" >/dev/null 2>&1; }
# hold 秒數 SQL：開一個 session 送出 SQL 後保持連線（持鎖或 idle in transaction）
hold() { ( echo "$2"; sleep "$1" ) | $DC exec -T pg psql -U postgres -d bench -qX >/dev/null 2>&1 & }
# bench 名稱 秒數(容器時鐘) [額外參數]：背景 pgbench，8 個 client、限速 200 tps
bench() { local n=$1 t=$2; shift 2; $DC exec -T pg pgbench -U postgres -n -c 8 -j 2 -R 200 -P 5 -T "$t" "$@" bench \
  > "results/pgbench-$n.txt" 2>&1 & BPID=$!; }
H() { echo; echo "## $1（$(date +%T)）"; }
now() { date +%s; }
pglog() { $DC logs --since "$1" pg 2>&1 | sed -E 's/^[^|]*\| //' | grep -E "$2" | cut -c1-220 | head -"${3:-8}"; }
TPS='sum(rate(pg_stat_database_xact_commit{instance="e-lt",datname="bench"}[15s]))'
UPD='sum(rate(pg_stat_database_tup_updated{instance="e-lt",datname="bench"}[15s]))'
CALLS='sum(rate(pg_stat_statements_calls_total{instance="e-lt",datname="bench"}[15s]))'
ACT='sum(pg_stat_activity_count{instance="e-lt",datname="bench",state="active"})'
LOCKW='sum(pg_stat_activity_count{instance="e-lt",datname="bench",wait_event_type="Lock"})'
BASE=('up' "$TPS" "$UPD" "$CALLS" "$ACT" "$LOCKW")

$DC up -d >/dev/null 2>&1
sleep 25
exec > >(tee "$OUT") 2>&1
echo "環境：postgres 16.15-alpine（cpus 1.0、mem 768m）、postgres-exporter v0.20.1 ×2、prometheus v3.13.5；scrape 5s、timeout 4s"
sql "select version()"
echo "  容器時鐘對照（主機 date 前後 vs 容器內 sleep 10）："
h0=$(date +%s.%N); $DC exec -T pg sh -c 'sleep 10'; h1=$(date +%s.%N)
echo "    容器 sleep 10 = 主機 $(python3 -c "print(round($h1-$h0,2))") 秒"
$DC exec -T pg pgbench -U postgres -i -s 10 -q bench 2>&1 | tail -2 | sed 's/^/    /'
sqlq "select pg_stat_statements_reset(); select pg_stat_reset();"
sleep 10

# ---------------------------------------------------------------- S0
H "S0 基線：pgbench -R 200 -c 8，60 秒（容器時鐘）"
s=$(now); bench s0 60; wait $BPID; e=$(now)
qr $s $e "${BASE[@]}" 'scrape_duration_seconds' \
  'sum(rate(pg_stat_database_blks_read{instance="e-lt",datname="bench"}[15s]))' \
  'sum(rate(pg_stat_database_blks_hit{instance="e-lt",datname="bench"}[15s])) / (sum(rate(pg_stat_database_blks_hit{instance="e-lt",datname="bench"}[15s])) + sum(rate(pg_stat_database_blks_read{instance="e-lt",datname="bench"}[15s])))'
tail -3 results/pgbench-s0.txt | sed 's/^/    /'
echo "  不限速的容量（-c 4，20 秒，容器時鐘）："
$DC exec -T pg pgbench -U postgres -n -c 4 -j 2 -T 20 bench 2>&1 | grep -E "^tps|latency average" | sed 's/^/    /'

# ---------------------------------------------------------------- S1
H "S1 慢 SQL：背景 pgbench + 每秒一次全表掃描 + 一個 40 秒的 pg_sleep"
s=$(now); bench s1 60
$DC exec -T pg pgbench -U postgres -n -c 1 -R 1 -T 50 -f /sql/slow.sql bench > results/pgbench-s1-slow.txt 2>&1 &
SL=$!
sleep 15; hold 45 "SELECT pg_sleep(40) /* report */;"
sleep 20
echo "  [t+35 直接查] pg_stat_activity 執行中最久的查詢："
sql "select pid, state, wait_event_type, round(extract(epoch from now()-query_start)::numeric,1) as q_sec, left(query, 60) from pg_stat_activity where datname='bench' and state='active' and pid<>pg_backend_pid() order by query_start limit 3"
q 'pg_stat_activity_max_tx_duration{instance="e-lt",datname="bench",state="active"}' 'pg_long_running_transactions_oldest_timestamp_seconds{instance="e-lt"}'
wait $BPID $SL; e=$(now)
qr $s $e "$TPS" "$ACT" \
  'topk(3, rate(pg_stat_statements_seconds_total{instance="e-lt",datname="bench"}[30s]) / rate(pg_stat_statements_calls_total{instance="e-lt",datname="bench"}[30s]))'
echo "  exporter 的 per-query 平均（最後 1 分鐘，前 5 名）與 query_id 對照（include_query，query_length 預設 120）："
q 'topk(5, rate(pg_stat_statements_seconds_total{instance="e-lt",datname="bench"}[1m]) / rate(pg_stat_statements_calls_total{instance="e-lt",datname="bench"}[1m]))'
curl -s --get "$P/api/v1/query" --data-urlencode 'query=pg_stat_statements_query_id{instance="e-lt"}' \
  | python3 -c 'import json,sys; r=json.load(sys.stdin)["data"]["result"]; [print("    ", x["metric"]["queryid"], len(x["metric"]["query"]), repr(x["metric"]["query"][:90])) for x in r[:6]]; print("     series 數：", len(r))'
q 'count(pg_stat_statements_calls_total{instance="e-lt"})' 'count(pg_stat_statements_calls_total{instance="e-plain"})' \
  'pg_stat_statements_block_read_seconds_total{instance="e-lt",datname="bench"} > 0'
echo "  PostgreSQL 有、exporter 沒輸出的欄位（mean/max/stddev、shared_blks_read、temp_blks_written）："
sql "select queryid, calls, round(mean_exec_time::numeric,1) mean_ms, round(max_exec_time::numeric,1) max_ms, round(stddev_exec_time::numeric,1) sd_ms, shared_blks_read, temp_blks_written, left(query,50) from pg_stat_statements order by total_exec_time desc limit 4"
echo "  log_min_duration_statement=500ms 寫到 log（exporter 不讀 log）："
pglog 120s "duration: [0-9.]+ ms" 4
sql "select count(*) as track_activity_query_size from pg_settings where name='track_activity_query_size' and setting='1024'"

# ---------------------------------------------------------------- S2
H "S2 列鎖等待：一個交易更新 pgbench_branches bid=1 後不 commit，持有 30 秒"
s=$(now); bench s2 70
sleep 15; hold 30 "BEGIN; UPDATE pgbench_branches SET bbalance = bbalance WHERE bid = 1;"
sleep 15
echo "  [t+30 直接查] 等鎖的 session 與擋住它們的 pid："
sql "select count(*) filter (where wait_event_type='Lock') as waiting, count(*) filter (where state='idle in transaction') as idle_in_tx, string_agg(distinct wait_event, ',') from pg_stat_activity where datname='bench'"
sql "select pid, pg_blocking_pids(pid), wait_event, round(extract(epoch from now()-state_change)::numeric,1) wait_sec, left(query,45) from pg_stat_activity where wait_event_type='Lock' order by state_change limit 3"
sql "select locktype, mode, granted, count(*) from pg_locks where locktype in ('transactionid','tuple','relation') and not granted group by 1,2,3"
q "$LOCKW" 'pg_stat_activity_max_tx_duration{instance="e-lt",datname="bench",wait_event_type="Lock"}' \
  'pg_locks_count{instance="e-lt",datname="bench"} > 0'
wait $BPID; e=$(now)
qr $s $e "${BASE[@]}" 'max(pg_stat_activity_max_tx_duration{instance="e-lt",datname="bench",wait_event_type="Lock"})' \
  'max(pg_stat_activity_max_tx_duration{instance="e-lt",datname="bench",state="idle in transaction"})'
grep -E "progress: (1[0-9]|[2-5][0-9])\." results/pgbench-s2.txt | head -12 | sed 's/^/    /'
pglog 100s "still waiting|acquired" 4

# ---------------------------------------------------------------- S3
H "S3 DDL 排在長交易後面：A 讀 pgbench_accounts 不結束（45 秒），B 執行 ALTER TABLE（沒設 lock_timeout）"
s=$(now); bench s3 90
sleep 10; hold 45 "BEGIN; SELECT count(*) FROM pgbench_accounts;"
sleep 5
$DC exec -T pg psql -U postgres -d bench -qAtX -c "ALTER TABLE pgbench_accounts ADD COLUMN c1 int" > results/s3-ddl.txt 2>&1 &
DDL=$!
sleep 20
echo "  [t+35 直接查] 等鎖的 session：誰擋 DDL、誰被 DDL 擋"
sql "select count(*) filter (where wait_event_type='Lock') waiting, count(*) filter (where wait_event_type='Lock' and pg_blocking_pids(pid) <> '{}') blocked from pg_stat_activity where datname='bench'"
sql "select pid, pg_blocking_pids(pid) blockers, wait_event, left(query,40) from pg_stat_activity where wait_event_type='Lock' order by state_change limit 4"
sql "select mode, granted, count(*) from pg_locks where relation='pgbench_accounts'::regclass group by 1,2 order by 1,2"
q 'up' 'scrape_duration_seconds' 'pg_scrape_collector_success{collector="stat_user_tables"}' "$LOCKW" \
  'pg_locks_count{instance="e-lt",datname="bench",mode="accessexclusivelock"}'
sql "select usename, wait_event_type, wait_event, count(*) from pg_stat_activity where usename like 'exporter%' group by 1,2,3 order by 1"
wait $DDL; wait $BPID; e=$(now)
qr $s $e "${BASE[@]}" 'scrape_duration_seconds' 'sum by (instance) (pg_locks_count{datname="bench",mode="accessexclusivelock"})'
grep -E "progress: (1[0-9]|[2-6][0-9])\." results/pgbench-s3.txt | head -14 | sed 's/^/    /'
pglog 140s "still waiting|acquired|canceling" 6
sqlq "ALTER TABLE pgbench_accounts DROP COLUMN c1"

H "S3b 同上，但 DDL session 先 SET lock_timeout = '2s'"
s=$(now); bench s3b 60
sleep 10; hold 30 "BEGIN; SELECT count(*) FROM pgbench_accounts;"
sleep 5
$DC exec -T pg psql -U postgres -d bench -qAtX -c "SET lock_timeout = '2s'" -c "ALTER TABLE pgbench_accounts ADD COLUMN c1 int" 2>&1 | sed 's/^/    DDL: /'
wait $BPID; e=$(now)
qr $s $e "$TPS" "$LOCKW" 'up'
grep -E "progress: (1[0-9]|2[0-9])\." results/pgbench-s3b.txt | head -5 | sed 's/^/    /'
sqlq "ALTER TABLE pgbench_accounts DROP COLUMN IF EXISTS c1"

# ---------------------------------------------------------------- S4
H "S4 CPU 限制：pgbench 中途把 pg 的 cpus 從 1.0 改成 0.1，40 秒後改回"
s=$(now); bench s4 80
sleep 20; docker update --cpus 0.1 at-pgperf-lab-pg-1 >/dev/null; echo "  t+20 cpus=0.1"
sleep 40; docker update --cpus 1.0 at-pgperf-lab-pg-1 >/dev/null; echo "  t+60 cpus=1.0"
wait $BPID; e=$(now)
qr $s $e "${BASE[@]}" 'scrape_duration_seconds' 'sum(pg_stat_activity_count{instance="e-lt",datname="bench",state="active",wait_event_type=""})' \
  'sum by (wait_event_type) (pg_stat_activity_count{instance="e-lt",datname="bench",state="active"})'
grep -E "progress:" results/pgbench-s4.txt | sed 's/^/    /' | head -20

# ---------------------------------------------------------------- S5
H "S5 work_mem：DISTINCT 10 萬列的查詢（2 client、-R 4），work_mem 64MB 與 64kB 各 30 秒"
$DC exec -T pg psql -U postgres -d bench -qAtX -c "ALTER SYSTEM SET work_mem = '64MB'" -c "SELECT pg_reload_conf()" >/dev/null
s=$(now)
$DC exec -T pg pgbench -U postgres -n -c 2 -R 4 -P 10 -T 30 -f /sql/sort.sql bench 2>&1 | grep -E "latency average|^tps" | sed 's/^/    64MB: /'
m=$(now)
$DC exec -T pg psql -U postgres -d bench -qAtX -c "ALTER SYSTEM SET work_mem = '64kB'" -c "SELECT pg_reload_conf()" >/dev/null
$DC exec -T pg pgbench -U postgres -n -c 2 -R 4 -P 10 -T 30 -f /sql/sort.sql bench 2>&1 | grep -E "latency average|^tps" | sed 's/^/    64kB: /'
e=$(now); echo "  切換點 t+$((m - s))"
sleep 10; e=$(now)
qr $s $e "$TPS" 'sum(rate(pg_stat_database_temp_bytes{instance="e-lt",datname="bench"}[15s]))' \
  'sum(rate(pg_stat_database_temp_files{instance="e-lt",datname="bench"}[15s]))' \
  'topk(1, rate(pg_stat_statements_seconds_total{instance="e-lt",datname="bench"}[20s]) / rate(pg_stat_statements_calls_total{instance="e-lt",datname="bench"}[20s]))'
pglog 90s "temporary file" 2
$DC exec -T pg psql -U postgres -d bench -qAtX -c "ALTER SYSTEM RESET work_mem" -c "SELECT pg_reload_conf()" >/dev/null

# ---------------------------------------------------------------- S6
H "S6 shared_buffers 16MB（重啟），同樣 pgbench -R 200，再跑 20 秒不限速"
$DC exec -T pg psql -U postgres -d bench -qAtX -c "ALTER SYSTEM SET shared_buffers = '16MB'" >/dev/null
r0=$(now); $DC restart pg >/dev/null 2>&1; sleep 8
sql "show shared_buffers"
s=$(now); bench s6 60; wait $BPID; e=$(now)
qr $r0 $e "${BASE[@]}" 'sum(rate(pg_stat_database_blks_read{instance="e-lt",datname="bench"}[15s]))' \
  'sum(rate(pg_stat_database_blks_hit{instance="e-lt",datname="bench"}[15s])) / (sum(rate(pg_stat_database_blks_hit{instance="e-lt",datname="bench"}[15s])) + sum(rate(pg_stat_database_blks_read{instance="e-lt",datname="bench"}[15s])))' \
  'pg_postmaster_start_time_seconds{instance="e-lt"}' 'resets(pg_stat_database_xact_commit{instance="e-lt",datname="bench"}[2m])'
tail -2 results/pgbench-s6.txt | sed 's/^/    /'
echo "  不限速的容量（-c 4，20 秒）："
$DC exec -T pg pgbench -U postgres -n -c 4 -j 2 -T 20 bench 2>&1 | grep -E "^tps|latency average" | sed 's/^/    /'
q 'changes(pg_postmaster_start_time_seconds{instance="e-lt"}[5m])'
$DC exec -T pg psql -U postgres -d bench -qAtX -c "ALTER SYSTEM RESET shared_buffers" >/dev/null
$DC restart pg >/dev/null 2>&1; sleep 8

# ---------------------------------------------------------------- S7
H "S7 checkpoint 太頻繁：max_wal_size 32MB（reload），pgbench -R 200，60 秒"
CK='pg_stat_bgwriter_checkpoints_req_total{instance="e-lt"}'
q "$CK" 'pg_stat_bgwriter_checkpoints_timed_total{instance="e-lt"}'
$DC exec -T pg psql -U postgres -d bench -qAtX -c "ALTER SYSTEM SET max_wal_size = '32MB'" -c "ALTER SYSTEM SET min_wal_size = '32MB'" -c "SELECT pg_reload_conf()" >/dev/null
s=$(now); bench s7 60; wait $BPID; e=$(now)
qr $s $e "$TPS" "increase(${CK}[30s])" 'rate(pg_stat_bgwriter_buffers_checkpoint_total{instance="e-lt"}[15s])' \
  'pg_settings_max_wal_size_bytes{instance="e-lt"}'
q "$CK"
tail -2 results/pgbench-s7.txt | sed 's/^/    /'
pglog 90s "checkpoint starting: wal|checkpoints are occurring too frequently" 4
$DC exec -T pg psql -U postgres -d bench -qAtX -c "ALTER SYSTEM RESET max_wal_size" -c "ALTER SYSTEM RESET min_wal_size" -c "SELECT pg_reload_conf()" >/dev/null

# ---------------------------------------------------------------- S8
H "S8 idle in transaction 擋住 vacuum：拿到 XID 後閒置 100 秒，autovacuum_naptime 5s，pgbench -R 200 90 秒"
$DC exec -T pg psql -U postgres -d bench -qAtX -c "ALTER SYSTEM SET autovacuum_naptime = '5s'" -c "SELECT pg_reload_conf()" >/dev/null
sqlq "VACUUM pgbench_branches, pgbench_tellers"
s=$(now); hold 100 "BEGIN; SELECT txid_current();"
bench s8 90
sleep 60
echo "  [t+60 直接查] 擋住 vacuum 的 backend 與它的 XID age："
sql "select pid, state, age(backend_xid) xid_age, age(backend_xmin) xmin_age, round(extract(epoch from now()-xact_start)::numeric) tx_sec from pg_stat_activity where backend_xid is not null and state like 'idle in%'"
sql "select relname, n_live_tup, n_dead_tup, autovacuum_count, pg_size_pretty(pg_table_size(relid)) from pg_stat_user_tables where relname in ('pgbench_branches','pgbench_tellers') order by 1"
wait $BPID; e=$(now)
qr $s $e "$TPS" 'pg_stat_user_tables_n_dead_tup{instance="e-lt",relname=~"pgbench_(branches|tellers)"}' \
  'pg_stat_user_tables_table_size_bytes{instance="e-lt",relname="pgbench_branches"}' \
  'increase(pg_stat_user_tables_autovacuum_count{instance="e-lt",relname="pgbench_branches"}[30s])' \
  'max(pg_stat_activity_max_tx_duration{instance="e-lt",datname="bench",state="idle in transaction"})'
grep -E "progress:" results/pgbench-s8.txt | awk 'NR%3==1' | sed 's/^/    /'
pglog 120s "dead but not yet removable|removable cutoff" 4
sleep 15
sql "select relname, n_dead_tup, pg_size_pretty(pg_table_size(relid)) from pg_stat_user_tables where relname in ('pgbench_branches','pgbench_tellers') order by 1"
$DC exec -T pg psql -U postgres -d bench -qAtX -c "ALTER SYSTEM RESET autovacuum_naptime" -c "SELECT pg_reload_conf()" >/dev/null

# ---------------------------------------------------------------- S9
H "S9 連線風暴：pgbench 已連 8 條；另開 36 條 pg_sleep(40)；再跑每筆交易重新連線的 pgbench -C"
s=$(now); bench s9 70
sleep 10
SPIDS=()
for i in $(seq 1 36); do $DC exec -T pg psql -U postgres -d bench -qAtX -c "select pg_sleep(40)" >/dev/null 2>>results/s9-storm-err.txt & SPIDS+=($!); done
sleep 8
$DC exec -T pg pgbench -U postgres -n -C -c 2 -R 20 -T 20 bench > results/pgbench-s9-connect.txt 2>&1
sql "select count(*) from pg_stat_activity where backend_type='client backend'"
q 'up' 'pg_up' 'count by (instance) (pg_scrape_collector_success)' 'sum by (instance) (pg_stat_activity_count)'
wait $BPID; sleep 5; e=$(now)
qr $s $e "${BASE[@]}" 'sum(pg_stat_database_numbackends{instance="e-lt"})' 'count by (instance) (pg_scrape_collector_success)'
echo "  psql 連線錯誤（去重）："; sort results/s9-storm-err.txt | uniq -c | head -3 | sed 's/^/    /'
grep -E "too many|reserved|aborted|^tps|failed" results/pgbench-s9-connect.txt | sort | uniq -c | head -5 | sed 's/^/    /'
# 不要用裸 wait：bash 會連 process substitution 的 tee 一起等，永遠不會結束（20261109 那次就卡在這裡）
wait "${SPIDS[@]}"

echo; echo "## 結束（$(date +%T)）"
exec >&- 2>&-
$DC logs e-plain e-lt 2>&1 | grep 'level=ERROR' | sed -E 's/^([^|]*)\|.*msg=/\1 msg=/; s/duration_seconds=[0-9.]+ //; s/ts=[^ ]+ //' | cut -c1-200 | sort | uniq -c | sort -rn | head -40 > results/exporter-errors.txt
$DC logs pg > results/pg-log.txt 2>&1
$DC down -v >/dev/null 2>&1
