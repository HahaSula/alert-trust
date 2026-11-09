#!/usr/bin/env bash
# PostgreSQL 效能與故障情境 S10：cgroup 記憶體上限下一個 backend 被 OOM killer 殺掉。約 4 分鐘，自己起環境、結束 down -v。原始輸出寫到 results/run-s10-<日期>.txt，pgbench 進度寫到 results/pgbench-*.txt，
# 結束會 docker compose down -v。
# 每個情境：pgbench 背景負載（-R 限速，固定 8 個 client）-> 中途製造狀況 -> 結束後用 query_range 印出 exporter 看到的時間序列，
# 並在狀況發生中用 psql 直接查 pg_stat_activity／pg_locks／pg_stat_statements 對照。
# 時間：t+秒 是主機牆鐘。容器內的 monotonic clock 比牆鐘慢約 30%，pgbench 的 -T、-P、-R 都用容器時鐘，
# 所以 -T 60 實際約 80 秒、-R 200 實際約 150 tps；比較時看同一次的前後，不看絕對值。
set -u
cd "$(dirname "$0")"
P=http://127.0.0.1:23690
OUT=results/run-s10-$(date +%Y%m%d).txt
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
echo "環境：同 run.sh；scrape 5s"
$DC exec -T pg pgbench -U postgres -i -s 10 -q bench 2>&1 | tail -1 | sed 's/^/    /'
sleep 15
H "S10 基線 20 秒後開始"
# ---------------------------------------------------------------- S10
H "S10 OOM：pg 記憶體上限改成 400MB，pgbench 中一個 backend 用 array_agg 吃記憶體"
docker update --memory 400m --memory-swap 400m at-pgperf-lab-pg-1 >/dev/null
s=$(now); bench s10 70
sleep 20
$DC exec -T pg psql -U postgres -d bench -qAtX -c "SELECT array_length(array_agg(g),1) FROM generate_series(1, 150000000) g" 2>&1 | head -3 | sed 's/^/    OOM 查詢: /'
echo "  OOM 查詢結束 t+$(( $(now) - s ))"
sleep 15
q 'up' 'pg_up' 'pg_postmaster_start_time_seconds' 'resets(pg_stat_database_xact_commit{datname="bench"}[2m])'
wait $BPID; e=$(now)
qr $s $e "${BASE[@]}" 'pg_up' 'pg_postmaster_start_time_seconds{instance="e-lt"}' \
  'pg_stat_database_xact_commit{instance="e-lt",datname="bench"}' \
  'resets(pg_stat_database_xact_commit{instance="e-lt",datname="bench"}[1m]) > 0 unless on (job, instance) changes(pg_postmaster_start_time_seconds{instance="e-lt"}[1m]) > 0'
grep -E "aborted|progress: (1[5-9]|2[0-9]|3[0-9])\.|^tps|failed" results/pgbench-s10.txt | head -12 | sed 's/^/    /'
pglog 120s "terminated by signal|all server processes terminated|database system was interrupted|ready to accept|Killed|out of memory|redo" 8
docker inspect at-pgperf-lab-pg-1 --format '    容器 OOMKilled={{.State.OOMKilled}} RestartCount={{.RestartCount}} Status={{.State.Status}}'
sql "select count(*) as xact_commit_after from pg_stat_database where datname='bench' and stats_reset is null"

echo; echo "## 結束（$(date +%T)）"
exec >&- 2>&-
$DC logs e-plain e-lt 2>&1 | grep 'level=ERROR' | sed -E 's/^([^|]*)\|.*msg=/\1 msg=/; s/duration_seconds=[0-9.]+ //; s/ts=[^ ]+ //' | cut -c1-200 | sort | uniq -c | sort -rn | head -40 > results/exporter-errors-s10.txt
$DC logs pg > results/pg-log-s10.txt 2>&1
$DC down -v >/dev/null 2>&1
