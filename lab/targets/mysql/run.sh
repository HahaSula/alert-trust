#!/usr/bin/env bash
# 四個情境：a 正常、b 停 MariaDB、c 停 exporter、d 收回權限。結果寫到 results/。
set -u
cd "$(dirname "$0")"
OUT=results/run-$(date +%Y%m%d).md
P=http://127.0.0.1:19090/api/v1
q() { curl -s --get "$P/query" --data-urlencode "query=$1" | python3 -I -c 'import json,sys; d=json.load(sys.stdin)["data"]["result"]; print("empty" if not d else "; ".join(f"{ {k:v for k,v in r["metric"].items() if k in ("collector","__name__","job")} } = {r["value"][1]}" for r in d))'; }
names() { curl -s --get "$P/query" --data-urlencode 'query=count by (__name__) ({job="mysql",__name__=~"mysql_.*"})' | python3 -I -c 'import json,sys; print(sorted(r["metric"]["__name__"] for r in json.load(sys.stdin)["data"]["result"]))'; }
sqlroot() { docker compose exec -T mariadb mariadb -uroot -prootpw -e "$1"; }
log() { echo "$*" | tee -a "$OUT"; }
show() { log "- \`$1\` → $(q "$1")"; }

trap 'docker compose down -v >/dev/null 2>&1' EXIT
: > "$OUT"
docker compose up -d --wait >/dev/null 2>&1 || docker compose up -d
sleep 20

log "## a) 正常"
show 'up{job="mysql"}'; show 'mysql_up'; show 'mysql_exporter_collector_success'
show 'count({job="mysql",__name__=~"mysql_.*"})'
names > results/metric-names-normal.txt
show 'mysql_exporter_collector_duration_seconds'

log "## b) 停 MariaDB"
docker compose stop mariadb >/dev/null; sleep 15
show 'up{job="mysql"}'; show 'mysql_up'; show 'mysql_exporter_collector_success'
show 'count({job="mysql",__name__=~"mysql_.*"})'
show 'scrape_duration_seconds{job="mysql"}'
docker compose start mariadb >/dev/null; sleep 20
show 'mysql_up'

log "## c) 停 exporter"
docker compose stop exporter >/dev/null; sleep 15
show 'up{job="mysql"} == 0'; show 'mysql_up != 1'; show 'mysql_up == 0'; show 'absent(mysql_up)'
show 'count({job="mysql",__name__=~"mysql_.*"})'
docker compose start exporter >/dev/null; sleep 15
show 'mysql_up'

log "## d1) REVOKE SLAVE MONITOR"
sqlroot "REVOKE SLAVE MONITOR ON *.* FROM 'exporter'@'%';"; sleep 12
show 'mysql_exporter_collector_success'
show 'count({job="mysql",__name__=~"mysql_.*"})'
docker compose logs --since 15s exporter 2>&1 | grep -i 'error from scraper' | tail -2 | tee -a "$OUT"

log "## d2) 再 REVOKE PROCESS"
sqlroot "REVOKE PROCESS ON *.* FROM 'exporter'@'%';"; sleep 12
show 'mysql_exporter_collector_success'
show 'count({job="mysql",__name__=~"mysql_.*"})'
names > results/metric-names-noprocess.txt
docker compose logs --since 15s exporter 2>&1 | grep -i 'error from scraper' | sort -u | tail -4 | tee -a "$OUT"
