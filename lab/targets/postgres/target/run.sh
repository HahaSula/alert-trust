#!/usr/bin/env bash
# PostgreSQL 本體監控實驗，約 20 分鐘。原始輸出寫到 results/run-<日期>.txt，結束會 docker compose down -v。
# 注意：G 與 D 兩段把 ALTER SYSTEM 和其他語句放在同一個 psql -c（會被包成一個交易而失敗），且 archive_command 由命令列 -c 設定，
# ALTER SYSTEM 改不動它。這兩段以 run2.sh（slot）與 run3.sh（archive）的結果為準；保留原樣以對應 results/run-20261108.txt。
# 每個階段：用 SQL 製造狀況 -> 等幾次 scrape -> 同時印出 exporter 看到的（Prometheus 查詢）與 PostgreSQL 直接查到的（psql）。
set -u
cd "$(dirname "$0")"
P=http://127.0.0.1:23090
OUT=results/run-$(date +%Y%m%d).txt
mkdir -p results

q() { for e in "$@"; do echo "  > $e"; curl -s --get "$P/api/v1/query" --data-urlencode "query=$e" | python3 -I fmt.py; done; }
sql() { local svc=$1; shift; echo "  \$ [$svc] $*"; docker compose exec -T "$svc" psql -U postgres -qAtX -c "$*" 2>&1 | sed 's/^/    /'; }
sqlq() { docker compose exec -T "$1" psql -U postgres -qAtX -c "$2" >/dev/null 2>&1; }
# 背景 session：$1 服務、$2 秒數、$3 SQL（送出後保持連線不動，模擬 idle in transaction 或持鎖）
hold() { ( echo "$3"; sleep "$2" ) | docker compose exec -T "$1" psql -U postgres -qX >/dev/null 2>&1 & }
# 背景寫入：每秒一筆 commit，共 $2 秒；pid 記在 WPIDS，用 waitw 等它結束（不用裸 wait，避免等到 tee）
WPIDS=()
writes() { sqlq "$1" "DO \$\$ BEGIN FOR i IN 1..$2 LOOP UPDATE t SET v = v + 1 WHERE id = 1; COMMIT; PERFORM pg_sleep(1); END LOOP; END \$\$;" & WPIDS+=($!); }
waitw() { for p in "${WPIDS[@]}"; do wait "$p"; done; WPIDS=(); }
# 產生約 30MB WAL：$2 是批次編號（決定 id 範圍）
wal() { sqlq "$1" "INSERT INTO t SELECT g, 0, repeat('x', 900) FROM generate_series($2 * 100000 + 1, $2 * 100000 + 30000) g; SELECT pg_switch_wal();"; }
H() { echo; echo "## $1（$(date +%T)）"; }

docker compose up -d >/dev/null 2>&1
sleep 40
exec > >(tee "$OUT") 2>&1
echo "環境：postgres 16.15-alpine / 17.11-alpine、postgres-exporter v0.20.1、prometheus v3.13.5；scrape 5s、timeout 4s"
docker compose exec -T p16 psql -U postgres -qAtX -c 'select version()'
docker compose exec -T p17 psql -U postgres -qAtX -c 'select version()'

H "A1 基線：角色、collector 失敗、複寫"
q 'up' 'pg_replication_is_replica' 'pg_scrape_collector_success == 0' \
  'pg_stat_replication_pg_wal_lsn_diff' 'pg_stat_wal_receiver_last_msg_receipt_time' 'pg_replication_slots_active' \
  'pg_replication_slots_pg_wal_lsn_diff'
sql p16 "select application_name, state, sync_state, write_lag, flush_lag, replay_lag from pg_stat_replication"
sql s16b "select status, slot_name, sender_host from pg_stat_wal_receiver"
echo "  沒有 pg_monitor 的帳號看 pg_stat_replication／pg_stat_wal_receiver："
docker compose exec -T p16 psql -U nomon -d postgres -qAtX -c "select application_name, coalesce(state,'NULL'), coalesce(replay_lsn::text,'NULL') from pg_stat_replication" | sed 's/^/    /'
docker compose exec -T s16a psql -U nomon -d postgres -qAtX -c "select coalesce(status,'NULL'), coalesce(slot_name,'NULL') from pg_stat_wal_receiver" | sed 's/^/    /'

H "A2 版本差異：PG16 與 PG17 的 checkpoint 與 settings metric"
for inst in e-p16 e-p17; do
  curl -s --get "$P/api/v1/label/__name__/values" --data-urlencode "match[]={instance=\"$inst\",__name__=~\"pg_.+\"}" \
    | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin)["data"]))' > "results/names-$inst.txt"
done
echo "  只在 PG16 有的 metric（不含 settings）："; comm -23 results/names-e-p16.txt results/names-e-p17.txt | grep -v pg_settings | sed 's/^/    /'
echo "  只在 PG17 有的 metric（不含 settings）："; comm -13 results/names-e-p16.txt results/names-e-p17.txt | grep -v pg_settings | sed 's/^/    /'
q 'pg_settings_reserved_connections' 'pg_settings_superuser_reserved_connections' 'pg_settings_max_connections{instance=~"e-p1.*"}'
echo "  字串型設定（synchronous_standby_names、archive_mode）有沒有 metric："
grep -E 'synchronous|archive_mode|wal_level|archive_command' results/names-e-p16.txt | sed 's/^/    /' || true
echo "    (若上面只有 archive_timeout 之類，表示字串與 enum 設定都不會輸出)"

H "B 沒有寫入時的延遲（閒置 60 秒）"
sleep 60
q 'pg_replication_lag_seconds{instance=~"e-s.*"}' 'pg_replication_last_replay_seconds{instance=~"e-s.*"}' \
  'pg_stat_replication_pg_wal_lsn_diff{job="postgres"}'
sql p16 "select application_name, replay_lag from pg_stat_replication"

H "C 持續寫入（每秒一筆 commit，40 秒）"
writes p16 40; writes p17 40
sleep 30
q 'pg_replication_lag_seconds{instance=~"e-s.*"}' 'pg_replication_last_replay_seconds{instance=~"e-s.*"}' \
  'pg_stat_replication_pg_wal_lsn_diff{job="postgres"}'
sql p16 "select application_name, write_lag, flush_lag, replay_lag from pg_stat_replication"
waitw

H "E 切斷 WAL receiver（repl 改 NOLOGIN 並中斷 walsender），主庫繼續寫入 40 秒"
sql p16 "ALTER ROLE repl NOLOGIN; SELECT count(pg_terminate_backend(pid)) FROM pg_stat_replication;"
writes p16 40
sleep 35
q 'pg_replication_lag_seconds{cluster="pg16"}' 'pg_replication_last_replay_seconds{cluster="pg16"}' \
  'pg_stat_wal_receiver_last_msg_receipt_time' 'time() - pg_stat_wal_receiver_last_msg_receipt_time' \
  'pg_stat_replication_pg_wal_lsn_diff{cluster="pg16"}' 'pg_replication_slots_active{cluster="pg16",job="postgres"}' \
  'pg_replication_slots_pg_wal_lsn_diff{cluster="pg16",job="postgres"}'
sql s16a "select pg_last_wal_receive_lsn() = pg_last_wal_replay_lsn() as receive_eq_replay, now() - pg_last_xact_replay_timestamp() as replay_age, (select count(*) from pg_stat_wal_receiver) as receivers"
waitw
sql p16 "ALTER ROLE repl LOGIN"
sleep 15
q 'pg_stat_replication_pg_wal_lsn_diff{cluster="pg16",job="postgres"}'

H "F idle in transaction 持有 xmin：燒 XID、製造 dead tuple、VACUUM"
hold p16 100 "BEGIN; SELECT txid_current(); SELECT count(*) FROM t;"
sleep 3
sqlq p16 "DO \$\$ BEGIN FOR i IN 1..3000 LOOP PERFORM txid_current(); COMMIT; END LOOP; END \$\$;"
sqlq p16 "UPDATE t SET v = v + 1"
sqlq p16 "VACUUM t"
sleep 30
q 'pg_stat_activity_max_tx_duration{instance="e-p16",datname="postgres",state=~"idle in transaction|active"} > 0' \
  'pg_long_running_transactions{instance="e-p16"}' 'pg_long_running_transactions_oldest_timestamp_seconds{instance="e-p16"}' \
  'pg_database_wraparound_age_datfrozenxid_seconds{instance="e-p16",datname="postgres"}' \
  'pg_stat_user_tables_n_dead_tup{instance="e-p16",relname="t"}' 'pg_stat_user_tables_n_live_tup{instance="e-p16",relname="t"}' \
  'pg_stat_activity_count{instance="e-p16-nomon",datname="postgres",state="idle in transaction"}'
sql p16 "select state, age(backend_xmin) as xmin_age, now() - xact_start as xact_age from pg_stat_activity where backend_xmin is not null and backend_type = 'client backend' order by 2 desc limit 3"
sql p16 "select n_dead_tup, last_vacuum is not null as vacuumed from pg_stat_user_tables where relname = 't'"
docker compose exec -T p16 psql -U postgres -qAtX -c "VACUUM (VERBOSE) t" 2>&1 | grep -iE 'dead but not yet removable|removable cutoff' | sed 's/^/    /'
echo "  （idle in transaction session 在背景，100 秒後結束）"

H "G archive_command 失敗"
sql p16 "ALTER SYSTEM SET archive_command = '/bin/false'; SELECT pg_reload_conf();"
wal p16 1
sleep 25
q 'pg_stat_archiver_failed_count{instance="e-p16"}' 'pg_stat_archiver_archived_count{instance="e-p16"}' \
  'pg_stat_archiver_last_archive_age{instance="e-p16"}' 'pg_wal_segments{instance="e-p16"}' 'pg_wal_size_bytes{instance="e-p16"}'
sql p16 "select archived_count, failed_count, last_failed_wal, now() - last_failed_time as since_fail, now() - last_archived_time as since_ok from pg_stat_archiver"
docker compose exec -T p16 sh -c 'ls /var/lib/postgresql/data/pg_wal/archive_status | grep -c ready' | sed 's/^/    .ready 檔數：/'
sql p16 "ALTER SYSTEM RESET archive_command; SELECT pg_reload_conf();"

H "H deadlock"
hold p16 4 "BEGIN; UPDATE t SET v = 1 WHERE id = 1; SELECT pg_sleep(1); UPDATE t SET v = 1 WHERE id = 2; COMMIT;"
hold p16 4 "BEGIN; UPDATE t SET v = 2 WHERE id = 2; SELECT pg_sleep(1); UPDATE t SET v = 2 WHERE id = 1; COMMIT;"
sleep 15
q 'pg_stat_database_deadlocks{instance="e-p16",datname="postgres"}'

H "I 鎖等待：一個 session 持有 ACCESS EXCLUSIVE 45 秒，另一個在等"
hold p16 45 "BEGIN; LOCK t IN ACCESS EXCLUSIVE MODE;"
sleep 2
( docker compose exec -T p16 psql -U postgres -qAtX -c "SELECT count(*) FROM t" >/dev/null 2>&1 & )
sleep 25
q 'up{instance=~"e-p16.*"}' 'scrape_duration_seconds{instance=~"e-p16.*"}' 'pg_up{instance=~"e-p16.*"}' \
  'sum by (instance) (pg_stat_activity_count{instance=~"e-p16.*",wait_event_type="Lock"})' \
  'pg_locks_count{instance=~"e-p16.*",datname="postgres",mode="accessexclusivelock"}'
sql p16 "select backend_type, state, wait_event_type, wait_event, left(query, 60) from pg_stat_activity where usename in ('exporter','nomon') or wait_event_type = 'Lock'"
sleep 25

H "J temp file"
sqlq p16 "SET work_mem = '64kB'; SELECT count(*) FROM (SELECT g FROM generate_series(1, 500000) g ORDER BY g DESC) s;"
sleep 12
q 'pg_stat_database_temp_files{instance="e-p16",datname="postgres"}' 'pg_stat_database_temp_bytes{instance="e-p16",datname="postgres"}' \
  'pg_stat_database_blks_hit{instance="e-p16",datname="postgres"} / (pg_stat_database_blks_hit{instance="e-p16",datname="postgres"} + pg_stat_database_blks_read{instance="e-p16",datname="postgres"})'
q 'topk(3, pg_stat_statements_seconds_total{instance="e-p16"})' 'count by (instance) (pg_stat_statements_calls_total)'

H "D 停掉 s16a，主庫限制 max_slot_wal_keep_size=64MB，再產生 WAL"
docker compose stop s16a >/dev/null
sql p16 "ALTER SYSTEM SET max_slot_wal_keep_size = '64MB'; SELECT pg_reload_conf(); SELECT pg_create_physical_replication_slot('orphan', true);"
wal p16 2
sleep 12
q 'pg_replication_slots_active{instance="e-p16"}' 'pg_replication_slots_pg_wal_lsn_diff{instance="e-p16"}' \
  'pg_replication_slots_safe_wal_size_bytes{instance="e-p16"}' 'pg_replication_slots_wal_status{instance="e-p16"}' \
  'pg_stat_replication_pg_wal_lsn_diff{instance="e-p16"}' 'pg_wal_size_bytes{instance="e-p16"}'
wal p16 3; wal p16 4
sql p16 "CHECKPOINT"
sleep 12
echo "  產生更多 WAL 並 CHECKPOINT 之後："
q 'pg_replication_slots_pg_wal_lsn_diff{instance="e-p16"}' 'pg_replication_slots_safe_wal_size_bytes{instance="e-p16"}' \
  'pg_replication_slots_wal_status{instance="e-p16"}' 'pg_wal_size_bytes{instance="e-p16"}'
sql p16 "select slot_name, active, wal_status, safe_wal_size, pg_size_pretty(pg_current_wal_lsn() - restart_lsn) from pg_replication_slots"
docker compose start s16a >/dev/null
sleep 20
echo "  s16a 重新啟動之後（slot 已 lost）："
q 'pg_stat_wal_receiver_last_msg_receipt_time{instance="e-s16a"}' 'pg_replication_lag_seconds{instance="e-s16a"}' \
  'pg_replication_last_replay_seconds{instance="e-s16a"}' 'pg_up{instance="e-s16a"}' \
  'pg_stat_replication_pg_wal_lsn_diff{instance="e-p16"}'
docker compose logs --since 30s s16a 2>&1 | grep -E 'FATAL|ERROR' | sed -E 's/^[^|]*\| //' | sort | uniq -c | head -5

H "M checkpoint：PG16 與 PG17"
q 'pg_stat_bgwriter_checkpoints_req_total' 'pg_stat_checkpointer_num_requested_total' 'pg_stat_checkpointer_restartpoints_done_total'
sql p16 "CHECKPOINT"; sql p17 "CHECKPOINT"; sql s17 "CHECKPOINT"
sleep 12
q 'pg_stat_bgwriter_checkpoints_req_total' 'pg_stat_checkpointer_num_requested_total' 'pg_stat_checkpointer_restartpoints_done_total'

H "K 連線用完（p17：max_connections=20、superuser_reserved=3、reserved=2）"
q 'sum by (instance) (pg_stat_activity_count{instance=~"e-p17.*"})' 'pg_stat_database_numbackends{instance="e-p17",datname="postgres"}'
for i in $(seq 1 16); do
  ( ( sleep 60 ) | docker compose exec -T -e PGPASSWORD=app p17 psql -h 127.0.0.1 -U app -d postgres -qX >/dev/null 2>&1 & )
done
sleep 25
sql p17 "select usename, count(*) from pg_stat_activity where backend_type = 'client backend' group by 1 order by 1"
docker compose exec -T -e PGPASSWORD=app p17 psql -h 127.0.0.1 -U app -d postgres -qAtX -c 'select 1' 2>&1 | sed 's/^/    app 再連一次：/'
q 'up{cluster="pg17"}' 'pg_up{cluster="pg17"}' 'count by (instance) (pg_scrape_collector_success{cluster="pg17"})' \
  'sum by (instance) (pg_stat_activity_count{instance=~"e-p17.*"})'
docker compose logs --since 30s e-p17-norsv 2>&1 | grep -o 'remaining connection slots[^"]*' | sort | uniq -c | head -3
sleep 40

H "L 一個 backend 被 kill -9（crash recovery，postmaster 沒有重啟）"
q 'pg_postmaster_start_time_seconds{instance="e-p17"}' 'pg_stat_database_xact_commit{instance="e-p17",datname="postgres"}' \
  'pg_stat_database_stats_reset{instance="e-p17",datname="postgres"}'
hold p17 30 "SELECT pg_sleep(30);"
sleep 3
PID=$(docker compose exec -T p17 psql -U postgres -qAtX -c "select pid from pg_stat_activity where query like 'SELECT pg_sleep(30)%' and pid <> pg_backend_pid() limit 1")
echo "  kill -9 $PID"
docker compose exec -T p17 kill -9 "$PID"
sleep 3
q 'pg_up{instance="e-p17"}' 'count(pg_scrape_collector_success{instance="e-p17"})'
sleep 20
q 'pg_up{instance="e-p17"}' 'pg_postmaster_start_time_seconds{instance="e-p17"}' \
  'pg_stat_database_xact_commit{instance="e-p17",datname="postgres"}' 'pg_stat_database_stats_reset{instance="e-p17",datname="postgres"}' \
  'resets(pg_stat_database_xact_commit{instance="e-p17",datname="postgres"}[2m])' \
  'changes(pg_postmaster_start_time_seconds{instance="e-p17"}[2m])' 'pg_replication_is_replica{cluster="pg17"}'
docker compose logs --since 40s p17 2>&1 | grep -E 'terminated by signal|all server processes terminated|database system was interrupted|ready to accept' | sed -E 's/^[^|]*\| //' | head -5

H "N 提升 s17（p17 仍在跑：split brain）"
sql s17 "SELECT pg_promote()"
writes p17 15
sleep 20
q 'pg_replication_is_replica{cluster="pg17"}' 'count by (cluster) (pg_replication_is_replica{job="postgres"} == 0)' \
  'changes(pg_replication_is_replica{cluster="pg17"}[2m])' 'pg_stat_replication_pg_wal_lsn_diff{cluster="pg17"}' \
  'pg_stat_wal_receiver_last_msg_receipt_time{cluster="pg17"}' 'pg_replication_lag_seconds{cluster="pg17"}'
sql s17 "select pg_is_in_recovery(), timeline_id from pg_control_checkpoint()"
sql p17 "select pg_is_in_recovery(), timeline_id from pg_control_checkpoint()"
waitw

H "O WAL 磁碟寫滿（p17 的 pg_wal 是 160MB tmpfs，用 slot 擋住回收）"
sql p17 "SELECT pg_create_physical_replication_slot('hold', true)"
q 'pg_wal_size_bytes{instance="e-p17"}'
for i in 1 2 3 4 5 6 7 8; do wal p17 $((10 + i)) ; done
docker compose exec -T p17 df -h /pgwal | sed 's/^/    /'
sleep 15
q 'up{instance="e-p17"}' 'pg_up{instance="e-p17"}' 'pg_wal_size_bytes{instance="e-p17"}' 'count(pg_scrape_collector_success{instance="e-p17"})'
docker compose ps p17 --format '    p17 狀態：{{.State}} {{.Status}}'
docker compose logs --since 60s p17 2>&1 | grep -E 'PANIC|No space|terminated|shut down|ready' | sed -E 's/^[^|]*\| //' | sort | uniq -c | head -8
sleep 20
q 'up{instance="e-p17"}' 'pg_up{instance="e-p17"}' 'count(pg_scrape_collector_success{instance="e-p17"})'
docker compose ps p17 --format '    p17 狀態：{{.State}} {{.Status}}'

echo; echo "## 結束（$(date +%T)）"
exec >&- 2>&-
docker compose logs e-p16-nomon e-s16a-nomon 2>&1 | grep 'level=ERROR' | sed -E 's/^([^|]*)\|.*msg=/\1 msg=/; s/duration_seconds=[0-9.]+ //' | sort | uniq -c > results/nomon-errors.txt
docker compose down -v >/dev/null 2>&1
