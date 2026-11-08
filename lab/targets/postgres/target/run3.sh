#!/usr/bin/env bash
# 補跑 archive 失敗：run.sh 與 run2.sh 用 ALTER SYSTEM 改 archive_command，但 compose 的命令列 -c archive_command=/bin/true
# 優先權較高，設定沒有生效。這裡 p16 的 archive_command 改成 /archive.sh，用 /tmp/archive_fail 切換成敗。約 3 分鐘。
set -u
cd "$(dirname "$0")"
P=http://127.0.0.1:23090
OUT=results/run3-$(date +%Y%m%d).txt
q() { for e in "$@"; do echo "  > $e"; curl -s --get "$P/api/v1/query" --data-urlencode "query=$e" | python3 -I fmt.py; done; }
sql() { local svc=$1; shift; echo "  \$ [$svc] $1"; docker compose exec -T "$svc" psql -U postgres -qAtX -c "$1" 2>&1 | sed 's/^/    /'; }
sqlq() { docker compose exec -T "$1" psql -U postgres -qAtX -c "$2" >/dev/null 2>&1; }
wal() { sqlq "$1" "INSERT INTO t SELECT g, 0, repeat('x', 900) FROM generate_series($2 * 100000 + 1, $2 * 100000 + 30000) g; SELECT pg_switch_wal();"; }
H() { echo; echo "## $1（$(date +%T)）"; }

ARCHIVE_COMMAND=/archive.sh docker compose up -d p16 e-p16 prometheus >/dev/null 2>&1
sleep 35
exec > >(tee "$OUT") 2>&1
echo "補跑 archive：postgres 16.15-alpine、postgres-exporter v0.20.1；scrape 5s"
sql p16 "SHOW archive_command"

H "G3 archive_command 開始失敗"
q 'pg_stat_archiver_failed_count{instance="e-p16"}' 'pg_stat_archiver_archived_count{instance="e-p16"}' 'pg_stat_archiver_last_archive_age{instance="e-p16"}'
docker compose exec -T p16 touch /tmp/archive_fail
wal p16 1; wal p16 2; wal p16 3
sleep 75
q 'pg_stat_archiver_failed_count{instance="e-p16"}' 'pg_stat_archiver_archived_count{instance="e-p16"}' \
  'pg_stat_archiver_last_archive_age{instance="e-p16"}' 'pg_wal_segments{instance="e-p16"}' 'pg_wal_size_bytes{instance="e-p16"}' \
  'increase(pg_stat_archiver_failed_count{instance="e-p16"}[1m])' 'increase(pg_stat_archiver_archived_count{instance="e-p16"}[1m])'
sql p16 "select archived_count, failed_count, last_failed_wal, now() - last_failed_time as since_fail, now() - last_archived_time as since_ok from pg_stat_archiver"
docker compose exec -T p16 sh -c 'ls /var/lib/postgresql/data/pg_wal/archive_status | grep -c ready' | sed 's/^/    .ready 檔數：/'

H "G3 恢復"
docker compose exec -T p16 rm -f /tmp/archive_fail
sleep 30
q 'pg_stat_archiver_failed_count{instance="e-p16"}' 'pg_stat_archiver_archived_count{instance="e-p16"}' \
  'pg_stat_archiver_last_archive_age{instance="e-p16"}' 'pg_wal_segments{instance="e-p16"}'
docker compose exec -T p16 sh -c 'ls /var/lib/postgresql/data/pg_wal/archive_status | grep -c ready' | sed 's/^/    .ready 檔數：/'

echo; echo "## 結束（$(date +%T)）"
exec >&- 2>&-
docker compose down -v >/dev/null 2>&1
