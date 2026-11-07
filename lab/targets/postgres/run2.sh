#!/usr/bin/env bash
# 補充實驗：DB 以不同方式壞掉時 up/pg_up 的差異，以及沒有 pg_monitor 時看不看得到別人的長交易。約 3 分鐘。
set -u
cd "$(dirname "$0")"
P=http://127.0.0.1:19190
OUT=results
q() { curl -s --get "$P/api/v1/query" --data-urlencode "query=$1" | python3 -I fmt.py; }
check() { for e in "$@"; do printf '  %-75s -> %s\n' "$e" "$(q "$e")"; done; }
psql_su() { docker compose exec -T postgres psql -U postgres -qAt -c "$1"; }
scrape_time() { curl -s -o /dev/null -w '%{http_code} %{time_total}s' --max-time 30 http://127.0.0.1:19187/metrics; }
EXPRS=('up{job=~"postgres.*"}' 'pg_up' 'pg_up == 0' 'pg_exporter_last_scrape_error' 'scrape_duration_seconds{job=~"postgres.*"}' 'count by (job) (pg_scrape_collector_success)')
docker compose up -d
sleep 25
{
echo "## d2) 連線立即被拒（exporter-refused 指向 5433）"
check "${EXPRS[@]}"

echo; echo "## e) 長交易可見性（superuser 開一個 idle in transaction 的交易）"
docker compose exec -T postgres psql -U postgres -c "BEGIN; SELECT txid_current(); SELECT pg_sleep(60);" >/dev/null 2>&1 &
sleep 20
check 'max by (job) (pg_stat_activity_max_tx_duration)' 'sum by (job) (pg_stat_activity_count{state="active"})'

echo; echo "## f) exporter 使用者被改成 NOLOGIN（認證失敗，連線很快被拒）"
psql_su "ALTER ROLE exporter NOLOGIN;"
sleep 15
echo "  直接抓 exporter /metrics: $(scrape_time)"
check "${EXPRS[@]}"
echo "  中斷 exporter 既有的連線（pg_terminate_backend）後："
psql_su "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE usename='exporter';"
sleep 12
check "${EXPRS[@]}"
psql_su "ALTER ROLE exporter LOGIN;"
sleep 10

echo; echo "## g) docker pause postgres（TCP 不回應）"
docker compose pause postgres >/dev/null
sleep 15
echo "  直接抓 exporter /metrics: $(scrape_time)"
check "${EXPRS[@]}"
docker compose unpause postgres >/dev/null
sleep 10

echo; echo "## h) docker stop postgres（主機名稱解析失敗）"
docker compose stop postgres >/dev/null
sleep 15
echo "  直接抓 exporter /metrics: $(scrape_time)"
check "${EXPRS[@]}"
} | tee "$OUT/run2.txt"
docker compose down -v
