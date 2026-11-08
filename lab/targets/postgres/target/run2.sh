#!/usr/bin/env bash
# 補跑：run.sh 的 G 與 D 把 ALTER SYSTEM 和其他語句放在同一個 psql -c，被包成一個交易而失敗。
# 這裡每個語句分開送，並補上 F 的 XID horizon（backend_xid）觀察。只啟動 PG16 的主庫、s16a 與它們的 exporter，約 5 分鐘。
# 注意：G 段的 ALTER SYSTEM SET archive_command 沒有生效（compose 命令列 -c 的優先權較高），archive 失敗以 run3.sh 為準。
set -u
cd "$(dirname "$0")"
P=http://127.0.0.1:23090
OUT=results/run2-$(date +%Y%m%d).txt
q() { for e in "$@"; do echo "  > $e"; curl -s --get "$P/api/v1/query" --data-urlencode "query=$e" | python3 -I fmt.py; done; }
# 每個參數各自一個 -c，各自一個交易
sql() { local svc=$1; shift; local args=(); for s in "$@"; do echo "  \$ [$svc] $s"; args+=(-c "$s"); done
        docker compose exec -T "$svc" psql -U postgres -qAtX "${args[@]}" 2>&1 | sed 's/^/    /'; }
sqlq() { docker compose exec -T "$1" psql -U postgres -qAtX -c "$2" >/dev/null 2>&1; }
hold() { ( echo "$3"; sleep "$2" ) | docker compose exec -T "$1" psql -U postgres -qX >/dev/null 2>&1 & }
wal() { sqlq "$1" "INSERT INTO t SELECT g, 0, repeat('x', 900) FROM generate_series($2 * 100000 + 1, $2 * 100000 + 30000) g; SELECT pg_switch_wal();"; }
H() { echo; echo "## $1（$(date +%T)）"; }

docker compose up -d p16 s16a e-p16 e-s16a prometheus >/dev/null 2>&1
sleep 40
exec > >(tee "$OUT") 2>&1
echo "補跑：postgres 16.15-alpine、postgres-exporter v0.20.1；scrape 5s"

H "F2 idle in transaction 持有的是 backend_xid（不是 backend_xmin）"
hold p16 60 "BEGIN; SELECT txid_current();"
sleep 3
sqlq p16 "DO \$\$ BEGIN FOR i IN 1..3000 LOOP PERFORM txid_current(); COMMIT; END LOOP; END \$\$;"
sleep 12
sql p16 "select state, age(backend_xid) as xid_age, age(backend_xmin) as xmin_age, now() - xact_start as xact_age from pg_stat_activity where backend_type = 'client backend' and pid <> pg_backend_pid()"
q 'pg_stat_activity_max_tx_duration{instance="e-p16",datname="postgres",state="idle in transaction"}' \
  'pg_database_wraparound_age_datfrozenxid_seconds{instance="e-p16",datname="postgres"}'

H "G archive_command 失敗"
q 'pg_stat_archiver_failed_count{instance="e-p16"}' 'pg_stat_archiver_archived_count{instance="e-p16"}' 'pg_stat_archiver_last_archive_age{instance="e-p16"}'
sql p16 "ALTER SYSTEM SET archive_command = '/bin/false'" "SELECT pg_reload_conf()"
wal p16 1; wal p16 2
sleep 70
q 'pg_stat_archiver_failed_count{instance="e-p16"}' 'pg_stat_archiver_archived_count{instance="e-p16"}' \
  'pg_stat_archiver_last_archive_age{instance="e-p16"}' 'pg_wal_segments{instance="e-p16"}' 'pg_wal_size_bytes{instance="e-p16"}' \
  'increase(pg_stat_archiver_failed_count{instance="e-p16"}[1m])'
sql p16 "select archived_count, failed_count, last_failed_wal, now() - last_failed_time as since_fail, now() - last_archived_time as since_ok from pg_stat_archiver"
docker compose exec -T p16 sh -c 'ls /var/lib/postgresql/data/pg_wal/archive_status | grep -c ready' | sed 's/^/    .ready 檔數：/'
sql p16 "ALTER SYSTEM RESET archive_command" "SELECT pg_reload_conf()"
sleep 15
echo "  恢復 archive_command 之後："
q 'pg_stat_archiver_archived_count{instance="e-p16"}' 'pg_stat_archiver_last_archive_age{instance="e-p16"}' 'pg_wal_segments{instance="e-p16"}'

H "D 停掉 s16a，主庫限制 max_slot_wal_keep_size=64MB，再產生 WAL"
docker compose stop s16a >/dev/null
sql p16 "ALTER SYSTEM SET max_slot_wal_keep_size = '64MB'" "SELECT pg_reload_conf()" "SELECT pg_create_physical_replication_slot('orphan', true)"
wal p16 3
sleep 12
q 'pg_replication_slots_active{instance="e-p16"}' 'pg_replication_slots_pg_wal_lsn_diff{instance="e-p16"}' \
  'pg_replication_slots_safe_wal_size_bytes{instance="e-p16"}' 'pg_replication_slots_wal_status{instance="e-p16"}' \
  'pg_wal_size_bytes{instance="e-p16"}'
wal p16 4; wal p16 5; wal p16 6
sql p16 "CHECKPOINT"
sleep 12
echo "  再產生約 90MB WAL 並 CHECKPOINT 之後："
q 'pg_replication_slots_active{instance="e-p16"}' 'pg_replication_slots_pg_wal_lsn_diff{instance="e-p16"}' \
  'pg_replication_slots_safe_wal_size_bytes{instance="e-p16"}' 'pg_replication_slots_wal_status{instance="e-p16"}' \
  'pg_wal_size_bytes{instance="e-p16"}'
sql p16 "select slot_name, active, wal_status, safe_wal_size, restart_lsn, pg_size_pretty(pg_current_wal_lsn() - restart_lsn) from pg_replication_slots"
docker compose start s16a >/dev/null
sleep 25
echo "  s16a 重新啟動之後："
q 'up{instance="e-s16a"}' 'pg_up{instance="e-s16a"}' 'pg_replication_is_replica{instance="e-s16a"}' \
  'pg_stat_wal_receiver_last_msg_receipt_time{instance="e-s16a"}' 'pg_replication_lag_seconds{instance="e-s16a"}' \
  'pg_replication_last_replay_seconds{instance="e-s16a"}' 'pg_stat_replication_pg_wal_lsn_diff{instance="e-p16"}'
docker compose logs --since 30s s16a 2>&1 | grep -E 'FATAL|ERROR' | sed -E 's/^[^|]*\| //; s/^[0-9-]+ [0-9:.]+ UTC \[[0-9]+\] //' | sort | uniq -c | head -5

H "I2 鎖等待對照：預設 vs exporter 帳號設 lock_timeout=1s"
echo "  [預設] 持有 ACCESS EXCLUSIVE 30 秒："
hold p16 30 "BEGIN; LOCK t IN ACCESS EXCLUSIVE MODE;"
sleep 25
q 'up{instance="e-p16"}' 'count(pg_scrape_collector_success{instance="e-p16"})'
sql p16 "select usename, count(*) from pg_stat_activity where wait_event_type = 'Lock' group by 1"
sleep 10
sql p16 "ALTER ROLE exporter SET lock_timeout = '1s'"
sleep 10
echo "  [lock_timeout=1s] 持有 ACCESS EXCLUSIVE 30 秒："
hold p16 30 "BEGIN; LOCK t IN ACCESS EXCLUSIVE MODE;"
sleep 25
q 'up{instance="e-p16"}' 'scrape_duration_seconds{instance="e-p16"}' 'pg_scrape_collector_success{instance="e-p16"} == 0' \
  'sum(pg_stat_activity_count{instance="e-p16",wait_event_type="Lock"})' 'pg_locks_count{instance="e-p16",datname="postgres",mode="accessexclusivelock"}'
sql p16 "select usename, count(*) from pg_stat_activity where wait_event_type = 'Lock' group by 1"
sleep 8

echo; echo "## 結束（$(date +%T)）"
exec >&- 2>&-
docker compose down -v >/dev/null 2>&1
