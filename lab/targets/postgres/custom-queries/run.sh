#!/usr/bin/env bash
# query-exporter 5.2.2 自訂查詢實驗，約 6 分鐘。原始輸出寫到 results/run-<日期>.txt，結束會 docker compose down -v。
# 每個階段：用 SQL 製造狀況 -> 等 query-exporter 下一輪查詢（interval 5s）-> 同時印出 /metrics（grep）與 psql 直接查到的值。
# 這台機器的容器時鐘不可靠：只比較數值與 series 有沒有出現，不解讀秒數。
set -u
cd "$(dirname "$0")"
OUT=results/run-$(date +%Y%m%d).txt
mkdir -p results
# 用 repo 裡的設定，只縮短 interval 與 expiration
sed -E -e 's/^(    interval:) .*/\1 5s/' -e 's/^(    expiration:) .*/\1 20s/' ../../../../queries/query-exporter/postgres.yaml > .config.yaml
m() { echo "  > /metrics | grep -E '$*'"; curl -s http://127.0.0.1:23901/metrics | grep -v '^#' | grep -E "$*" | sed 's/^/    /' || echo "    （沒有符合的 series）"; }
sql() { local svc=$1 db=$2; shift 2; echo "  \$ [$svc/$db] $*"; docker compose exec -T "$svc" psql -U postgres -d "$db" -qAtX -c "$*" 2>&1 | sed 's/^/    /'; }
sqlq() { docker compose exec -T "$1" psql -U postgres -d "$2" -qAtX -c "$3" >/dev/null 2>&1; }
H() { echo; echo "## $1"; }
W=8

docker compose up -d >/dev/null 2>&1
for i in $(seq 1 60); do docker compose exec -T s pg_isready -U postgres >/dev/null 2>&1 && break; sleep 2; done
docker compose exec -T s pg_isready -U postgres >/dev/null 2>&1 || { echo 'standby 沒有起來'; docker compose logs s | tail -5; docker compose down -v; exit 1; }
sleep 12
exec > >(tee "$OUT") 2>&1
echo "環境：postgres 17.11-alpine 主庫 p＋複本 s（slot s1slot、application_name=s1、hot_standby_feedback=on）、"
echo "synchronous_standby_names='FIRST 1 (s1)'、--data-checksums、archive_mode=on；query-exporter 5.2.2（adonato/query-exporter:5.2.2），帳號 qe_monitor 只有 pg_monitor。"
echo "設定：queries/query-exporter/postgres.yaml，interval 全改 5s、expiration 全改 20s。"
sql p postgres "select version()"
docker compose exec -T qe query-exporter --version 2>&1 | tail -1 | sed 's/^/  qe: /'

H "A 基線：所有自訂 metric 與內建 metric"
m '^pgq_|^queries_total|^database_errors_total'
sql p postgres "select application_name, state, sync_state, write_lag, flush_lag, replay_lag from pg_stat_replication"
sql p postgres "show synchronous_standby_names"

H "B 時間延遲：寫入中與閒置後（NULL 變 0）"
( for i in $(seq 1 15); do docker compose exec -T p psql -U postgres -d app -qAtX -c "insert into orders select g % 1000, 'w' from generate_series(1, 20000) g" >/dev/null 2>&1; done ) &
LOAD=$!
sleep 6
sql p postgres "select application_name, write_lag, flush_lag, replay_lag from pg_stat_replication"
m '^pgq_replication_(write|flush|replay)_lag_seconds'
wait $LOAD
sleep 30
echo "  （寫入停止 30 秒後）"
sql p postgres "select application_name, coalesce(write_lag::text,'NULL'), coalesce(flush_lag::text,'NULL'), coalesce(replay_lag::text,'NULL') from pg_stat_replication"
m '^pgq_replication_(write|flush|replay)_lag_seconds'

H "C 誰擋住 vacuum：持有 XID 的交易、prepared xact、複本上的 snapshot（經 hot_standby_feedback 到 slot xmin）"
m '^pgq_oldest|^pgq_prepared'
( echo "begin; select txid_current(); select pg_sleep(300);" | docker compose exec -T p psql -U postgres -d postgres -qX >/dev/null 2>&1 ) &
sqlq p app "begin; insert into orders values (1, 'prepared'); prepare transaction 'pq1';"
( echo "begin isolation level repeatable read; select count(*) from pg_class; select pg_sleep(300);" | docker compose exec -T s psql -U postgres -d postgres -qX >/dev/null 2>&1 ) &
sleep 5
echo "  消耗 3000 個 XID（每次 COMMIT）"
sqlq p postgres 'do $$ begin for i in 1..3000 loop perform txid_current(); commit; end loop; end $$;'
sleep $W
sql p postgres "select max(age(backend_xid)), max(age(backend_xmin)) from pg_stat_activity"
sql p postgres "select slot_name, xmin, age(xmin), catalog_xmin from pg_replication_slots"
sql p postgres "select gid, age(transaction) from pg_prepared_xacts"
sql s postgres "select max(age(backend_xmin)) from pg_stat_activity"
m '^pgq_oldest|^pgq_prepared'

H "D sessions_killed：把 C 的兩個 session terminate 掉；prepared xact 收掉"
m '^pgq_db_sessions_killed_total\{.*datname="postgres"'
sql p postgres "select count(pg_terminate_backend(pid)) from pg_stat_activity where query like 'select pg_sleep(300)%' and pid <> pg_backend_pid()"
sql s postgres "select count(pg_terminate_backend(pid)) from pg_stat_activity where query like 'select pg_sleep(300)%' and pid <> pg_backend_pid()"
sqlq p app "commit prepared 'pq1'"
sleep $W
sql p postgres "select datname, sessions_killed, sessions_fatal, sessions_abandoned from pg_stat_database where datname='postgres'"
m '^pgq_db_sessions_killed_total\{.*datname="postgres"|^pgq_oldest|^pgq_prepared'

H "E archive：失敗、.ready 積壓、archive_command 清空"
m '^pgq_archive'
docker compose exec -T p touch /tmp/archive_fail
for i in 1 2 3; do sqlq p app "insert into orders values ($i, 'a')"; sqlq p postgres "select pg_switch_wal()"; done
sleep $W
sql p postgres "select archived_count, failed_count, last_archived_wal, last_failed_wal from pg_stat_archiver"
sql p postgres "select count(*) from pg_ls_archive_statusdir() where name like '%.ready'"
m '^pgq_archive'
docker compose exec -T p rm -f /tmp/archive_fail
sqlq p postgres "alter system set archive_command = ''"; sqlq p postgres "select pg_reload_conf()"
sleep $W
sql p postgres "select coalesce(nullif(current_setting('archive_command'),''),'<empty>'), current_setting('archive_mode')"
m '^pgq_archive_(mode|command_set)'
sqlq p postgres "alter system set archive_command = '/archive.sh'"; sqlq p postgres "select pg_reload_conf()"

H "F 其他資料庫：app 的失效索引與 dead tuple（primary DSN 是 postgres，primary_app DSN 是 app）"
m '^pgq_invalid_indexes|^pgq_tables_over|^pgq_table_dead'
sql p app "create unique index concurrently orders_id_uq on orders (id)"
sql p app "update orders set v = 'u' where id < 500"
sleep $W
sql p app "select indexrelid::regclass, indisvalid from pg_index where not indisvalid"
sql p app "select relname, n_live_tup, n_dead_tup from pg_stat_user_tables order by n_dead_tup desc limit 3"
m '^pgq_invalid_indexes|^pgq_tables_over|^pgq_table_dead'

H "G checksum 失敗：破壞 app.victim 的一個資料頁（先 checkpoint、清掉 shared buffer）"
m '^pgq_checksum|^pgq_data_checksums'
F=$(docker compose exec -T p psql -U postgres -d app -qAtX -c "select current_setting('data_directory') || '/' || pg_relation_filepath('victim')" | tr -d '\r')
sqlq p app "checkpoint"
sql p app "select count(pg_buffercache_evict(bufferid)) from pg_buffercache where relfilenode = pg_relation_filenode('victim')"
docker compose exec -T -u postgres p sh -c "printf 'CORRUPTCORRUPT' | dd of='$F' bs=1 seek=4000 conv=notrunc 2>/dev/null"
sql p app "select count(*) from victim"
sleep $W
sql p app "select datname, checksum_failures from pg_stat_database where datname='app'"
m '^pgq_checksum|^pgq_data_checksums'

H "H 自檢：拿掉 pg_monitor，archive 查詢失敗後 metric 還在（舊值），只有錯誤計數與 query_timestamp 透露"
m '^query_timestamp\{.*query="archive"|^queries_total\{.*query="archive"|^pgq_archive_mode'
sqlq p postgres "revoke pg_monitor from qe_monitor"
sql p postgres "select pg_terminate_backend(pid) is not null from pg_stat_activity where usename='qe_monitor' limit 0"
sleep 12
m '^query_timestamp\{.*query="archive"|^queries_total\{.*query="archive"|^pgq_archive_mode'
docker compose logs qe 2>&1 | grep -m1 -i "permission denied" | cut -c1-220 | sed 's/^/  log: /'
sqlq p postgres "grant pg_monitor to qe_monitor"

H "I 同步 standby 消失：停掉複本"
docker compose stop s >/dev/null 2>&1
sleep 6
sql p postgres "select count(*) from pg_stat_replication"
m '^pgq_replication'
echo "  （再等 expiration 20s 之後）"
sleep 22
m '^pgq_replication'

H "J WAL 與 I/O 計數（A 之後一直有寫入）"
m '^pgq_wal_|^pgq_io_writes_total\{.*database="primary"|^pgq_io_fsyncs_total\{.*database="primary"'

H "K timeline：複本提升後"
docker compose start s >/dev/null 2>&1
for i in $(seq 1 30); do docker compose exec -T s pg_isready -U postgres >/dev/null 2>&1 && break; sleep 2; done
sleep $W
m '^pgq_checkpoint_timeline_id|^pgq_in_recovery'
sql s postgres "select pg_promote()"
sqlq s postgres "checkpoint"
sleep $W
sql s postgres "select timeline_id from pg_control_checkpoint()"
m '^pgq_checkpoint_timeline_id|^pgq_in_recovery'

H "L 收尾"
m '^queries_total\{.*status="(error|timeout|invalid-value)"|^database_errors_total'
kill $(jobs -p) 2>/dev/null
docker compose down -v >/dev/null 2>&1
rm -f .config.yaml
docker ps -a --filter name=at-pgq --format '{{.Names}}' | sed 's/^/  殘留：/'
echo "結束"
