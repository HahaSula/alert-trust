#!/usr/bin/env bash
# 輕量實驗，約 3 分鐘。結果寫到 results/。
set -u
cd "$(dirname "$0")"
P=http://127.0.0.1:19190
OUT=results
mkdir -p "$OUT"
q() { curl -s --get "$P/api/v1/query" --data-urlencode "query=$1" | python3 -I fmt.py; }
check() { for e in "$@"; do printf '  %-75s -> %s\n' "$e" "$(q "$e")"; done; }
EXPRS=(
  'up{job=~"postgres.*"}'
  'pg_up'
  'pg_up == 0'
  'up{job="postgres"} == 0'
  'absent(up{job="postgres"})'
  'pg_exporter_last_scrape_error'
  'pg_scrape_collector_success == 0'
  'count by (job) (pg_scrape_collector_success)'
  'count by (job) ({__name__=~"pg_.+"})'
  'pg_replication_lag_seconds'
)
docker compose up -d
sleep 25
{
echo "## a) 正常（$(date -Is)）"
check "${EXPRS[@]}"
echo "  meta metric names (job=postgres):"
curl -s --get "$P/api/v1/label/__name__/values" --data-urlencode 'match[]={job="postgres",__name__=~"pg_(up|exporter_.*|scrape_.*)|up|scrape_.*"}' | python3 -c 'import json,sys; print("   ", " ".join(json.load(sys.stdin)["data"]))'
echo "  collectors with success per job:"
curl -s --get "$P/api/v1/query" --data-urlencode 'query=pg_scrape_collector_success' | python3 -c 'import json,sys
for x in sorted(json.load(sys.stdin)["data"]["result"], key=lambda x:(x["metric"]["job"],x["metric"]["collector"])): print("   ", x["metric"]["job"], x["metric"]["collector"], x["value"][1])'
echo "  metric count per collector prefix, postgres vs postgres-nomon:"
for job in postgres postgres-nomon; do
  curl -s --get "$P/api/v1/label/__name__/values" --data-urlencode "match[]={job=\"$job\",__name__=~\"pg_.+\"}" | python3 -c 'import json,sys; n=json.load(sys.stdin)["data"]; print("   ", sys.argv[1], len(n), "names")' "$job"
done
curl -s --get "$P/api/v1/label/__name__/values" --data-urlencode 'match[]={job="postgres",__name__=~"pg_.+"}' | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin)["data"]))' > "$OUT/names-postgres.txt"
curl -s --get "$P/api/v1/label/__name__/values" --data-urlencode 'match[]={job="postgres-nomon",__name__=~"pg_.+"}' | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin)["data"]))' > "$OUT/names-postgres-nomon.txt"
echo "  names only with pg_monitor:"; comm -23 "$OUT/names-postgres.txt" "$OUT/names-postgres-nomon.txt" | sed 's/^/    /'
echo "  pg_stat_activity_count rows (nomon sees others' state?):"
check 'count by (job) (pg_stat_activity_count)' 'count by (job) (pg_stat_activity_max_tx_duration)'

echo; echo "## b) 停 PostgreSQL（$(date -Is)）"
docker compose stop postgres >/dev/null
sleep 20
check "${EXPRS[@]}"

echo; echo "## c) PostgreSQL 恢復、停 exporter（$(date -Is)）"
docker compose start postgres >/dev/null
sleep 10
docker compose stop exporter >/dev/null
sleep 20
check "${EXPRS[@]}"
} | tee "$OUT/run.txt"
docker compose logs exporter-nomon 2>&1 | grep -i 'level=ERROR' | sed -E 's/time=[^ ]+ //' | sort | uniq -c | head -30 > "$OUT/nomon-errors.txt"
docker compose logs exporter 2>&1 | grep -i 'level=ERROR\|WARN' | sed -E 's/time=[^ ]+ //' | sort | uniq -c | head -30 > "$OUT/exporter-errors.txt"
docker compose down -v
