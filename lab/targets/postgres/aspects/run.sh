#!/usr/bin/env bash
# 九個面向補洞實驗，約 4 分鐘。原始輸出寫到 results/run-<日期>.txt，結束會 docker compose down -v。
# 每個階段：用 SQL 製造狀況 -> 等 exporter 下一次被 curl -> 同時印出 exporter 輸出（grep）與 psql 直接查到的值。
set -u
cd "$(dirname "$0")"
OUT=results/run-$(date +%Y%m%d).txt
mkdir -p results
m() { local port=$1; shift; echo "  > [:$port] grep -E '$*'"; curl -s "http://127.0.0.1:$port/metrics" | grep -v '^#' | grep -E "$*" | sed 's/^/    /' || echo "    （沒有符合的 series）"; }
sql() { local svc=$1; shift; echo "  \$ [$svc] $*"; docker compose exec -T "$svc" psql -U postgres -qAtX -c "$*" 2>&1 | sed 's/^/    /'; }
sqlq() { docker compose exec -T "$1" psql -U postgres -qAtX -c "$2" >/dev/null 2>&1; }
H() { echo; echo "## $1（$(date +%T)）"; }

docker compose up -d >/dev/null 2>&1
sleep 20
exec > >(tee "$OUT") 2>&1
echo "環境：postgres 16.15-alpine / 17.11-alpine、postgres-exporter v0.20.1（預設 collector）；archive_mode=on、archive_timeout=30"
for s in p16 p17; do docker compose exec -T $s psql -U postgres -qAtX -c 'select version()'; done

H "A 基線：archive 設定與 archiver 在 exporter 裡的樣子（還沒有任何 WAL 被 archive）"
for p in 23801 23802; do m $p '^pg_settings_archive|^pg_stat_archiver|^pg_database_size_bytes|^pg_stat_database_sessions|^pg_settings_synchronous'; done
sql p16 "select name, setting, vartype from pg_settings where name in ('archive_mode','archive_timeout','archive_command','synchronous_standby_names') order by 1"
sql p16 "select archived_count, failed_count, coalesce(last_archived_time::text,'NULL') from pg_stat_archiver"

H "B 容量：寫入約 40MB，看 pg_database_size_bytes 與 table_size"
sql p16 "select pg_database_size('postgres')"
sqlq p16 "create table grow(id int, v text); insert into grow select g, repeat('x', 900) from generate_series(1, 40000) g;"
sqlq p17 "create table grow(id int, v text); insert into grow select g, repeat('x', 900) from generate_series(1, 40000) g;"
sleep 2
sql p16 "select pg_database_size('postgres'), pg_table_size('grow')"
for p in 23801 23802; do m $p '^pg_database_size_bytes\{datname="postgres"|^pg_stat_user_tables_table_size_bytes\{.*relname="grow"'; done

H "C 保護：archive 成功（pg_switch_wal）"
sqlq p16 "select pg_switch_wal()"; sqlq p17 "select pg_switch_wal()"
sleep 5
for p in 23801 23802; do m $p '^pg_stat_archiver'; done

H "D 保護：archive 失敗（touch /tmp/archive_fail），持續寫入 100 秒"
for s in p16 p17; do docker compose exec -T $s touch /tmp/archive_fail; done
for i in $(seq 1 10); do
  for s in p16 p17; do sqlq $s "insert into grow select g, repeat('y', 900) from generate_series(1, 2000) g"; done
  sleep 10
done
for p in 23801 23802; do m $p '^pg_stat_archiver|^pg_settings_archive_timeout|^pg_wal_segments'; done
sql p16 "select archived_count, failed_count, last_archived_wal, last_failed_wal, round(extract(epoch from now()-last_archived_time)) age from pg_stat_archiver"
sql p16 "select count(*) from pg_ls_archive_statusdir() where name like '%.ready'"
echo "  規則 PostgresArchiveStale 的條件：last_archive_age > 3 × archive_timeout（90 秒）且這段時間有寫入"

H "E 工作量：session 被 terminate 與 FATAL（PG 14 起 pg_stat_database 有 sessions_*）"
( echo "select pg_sleep(60);"; sleep 60 ) | docker compose exec -T p16 psql -U postgres -qX >/dev/null 2>&1 &
sleep 3
sql p16 "select pg_terminate_backend(pid) from pg_stat_activity where query like 'select pg_sleep(60)%' and pid <> pg_backend_pid()"
docker compose exec -T p16 psql -U postgres -d nosuchdb -c 'select 1' 2>&1 | sed 's/^/    /'
sleep 3
sql p16 "select datname, sessions, sessions_abandoned, sessions_fatal, sessions_killed from pg_stat_database where datname='postgres'"
m 23801 '^pg_stat_database_sessions|^pg_stat_database_xact_rollback\{.*datname="postgres"'

H "F 收尾"
for s in p16 p17; do docker compose exec -T $s rm -f /tmp/archive_fail; done
docker compose down -v >/dev/null 2>&1
docker ps -a --filter name=at-pgasp --format '{{.Names}}' | sed 's/^/  殘留：/'
echo "結束（$(date +%T)）"
