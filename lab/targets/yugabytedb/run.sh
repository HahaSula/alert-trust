#!/usr/bin/env bash
# YugabyteDB v2026.1.3.0（master ×3、tserver ×3，RF=3）的三方交叉、master quorum、YSQL 連線上限與 postgres_exporter 實驗，約 25 分鐘。
# 原始輸出寫到 results/run-<日期>.txt，結束會 docker compose down -v。
# 注意：這台機器的 Docker 容器單調時鐘比牆鐘慢約 30%，牆鐘也會被 WSL 往回調（YugabyteDB 會把這當成 clock skew，見 compose.yml）。
# Prometheus 內的時間（for、scrape_duration_seconds）只當參考；q.py 與 SQL 的耗時用主機的 monotonic 時鐘量。
set -u
cd "$(dirname "$0")"
OUT=results/run-$(date +%Y%m%d).txt
mkdir -p results
K() { python3 -I q.py "$@"; }
H() { echo; echo "## $1（$(date -u +%T)Z）"; }
mono() { python3 -I -c 'import time; print(f"{time.monotonic():.2f}")'; }
since() { python3 -I -c "import time,sys; print(f'{time.monotonic()-float(sys.argv[1]):.0f}')" "$1"; }
MPORT() { case $1 in yb-master-1) echo 30001;; yb-master-2) echo 30002;; yb-master-3) echo 30003;; esac; }
# Y <在哪個容器執行> <連哪台 tserver> <SQL>：新連線執行一次，印 rc、耗時（主機 monotonic）、輸出
Y() { local t0 rc out; t0=$(mono); out=$(docker compose exec -T "$1" timeout 25 /home/yugabyte/bin/ysqlsh -h "$2" -At -v ON_ERROR_STOP=1 \
        -c "set statement_timeout=20000" -c "$3" 2>&1); rc=$?
      printf '  SQL[%s→%s] %-60s rc=%s %ss %s\n' "${1#yb-}" "${2#yb-}" "$3" "$rc" "$(since "$t0")" "$(echo "$out" | grep -v '^SET$' | tr '\n' ' ' | cut -c1-170)"; }
masterleader() { for m in yb-master-1 yb-master-2 yb-master-3; do
    [ "$(curl -s -m2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$(MPORT $m)/api/v1/is-leader")" = 200 ] && { echo "$m"; return; }; done; echo none; }
endpoints() {  # endpoints <master port>：master 的 JSON API 與 /status
  for p in /status /api/v1/is-leader /api/v1/health-check /api/v1/tablet-replication /api/v1/tablet-under-replication; do K get "$1" "$p" 6; done; }
threeway() {
  echo "  三方交叉（up／probe／健康 metric）："
  K q 'up{job=~"yb-master|yb-tserver|yb-ysql|yb-ycql|yb-pgexp"}'
  K q 'probe_success'
  K q 'is_raft_leader{job="yb-master", table_name="sys.catalog"}'
  K q 'raft_term{job="yb-master", table_name="sys.catalog"}'
  K q 'num_tablet_servers_live{job="yb-master"}'
  K q 'num_tablet_servers_dead{job="yb-master"}'
  K q 'max_follower_heartbeat_delay{job="yb-master"}'
  K q 'ts_live_tablet_peers{job="yb-tserver"}'
  K q 'max by (instance) (follower_lag_ms{job="yb-tserver"})'
  K q 'yb_ysqlserver_connection_total{job="yb-ysql"}'
}
ALERTS() { K alerts '^YB'; }

free -m | head -2
docker compose up -d >/dev/null 2>&1
# master 選出 leader、YSQL initdb 完成約需 1–2 分鐘
for i in $(seq 1 40); do docker compose exec -T yb-tserver-1 /home/yugabyte/bin/ysqlsh -h yb-tserver-1 -At -c 'select 1' >/dev/null 2>&1 && break; sleep 5; done
sleep 20
exec > >(tee "$OUT") 2>&1
echo "環境：YugabyteDB v2026.1.3.0-b100（master ×3、tserver ×3，RF=3）、Prometheus v3.13.5（scrape 5s）、blackbox_exporter v0.27.0（timeout 4s）、postgres_exporter v0.20.1"
echo "tserver：follower_unavailable_considered_failed_sec=120（預設 900）、ysql_max_connections=12、memory_limit_hard_bytes=384MB；master 256MB；全部 fail_on_out_of_range_clock_skew=false。主機記憶體："
free -m | head -2
docker stats --no-stream --format '  {{.Name}} {{.MemUsage}}' | grep at-yb

H "A 基線：端點、metric 由誰輸出"
L=$(masterleader); echo "  master leader（/api/v1/is-leader 回 200 的那台）：$L"
Y yb-tserver-1 yb-tserver-1 "create table if not exists t (k int primary key, v text)"
Y yb-tserver-1 yb-tserver-1 "insert into t select g, 'v' from generate_series(1, 1000) g on conflict do nothing"
Y yb-tserver-1 yb-tserver-1 "select count(*) from t"
Y yb-tserver-1 yb-tserver-1 "select count(*) from yb_table_properties('t'::regclass)"
Y yb-tserver-1 yb-tserver-1 "show max_connections"
Y yb-tserver-1 yb-tserver-1 "show superuser_reserved_connections"
K q 'scrape_samples_scraped{job=~"yb-master|yb-tserver|yb-ysql|yb-ycql|yb-pgexp"}'
K q 'scrape_response_size_bytes{job=~"yb-master|yb-tserver|yb-ysql|yb-ycql|yb-pgexp"}'
K q 'scrape_duration_seconds{job=~"yb-master|yb-tserver|yb-ysql|yb-ycql|yb-pgexp"}'
for c in "master 30001" "tserver 30011" "ysql 30021" "ycql 30031"; do set -- $c
  curl -s localhost:$2/prometheus-metrics | grep -v '^#' | sed 's/[{ ].*//' | sort -u > results/names-$1-2026.1.3.0.txt
  echo "  $1 /prometheus-metrics 的 metric 名稱 $(wc -l < results/names-$1-2026.1.3.0.txt) 個；metric_type：$(curl -s localhost:$2/prometheus-metrics | grep -o 'metric_type="[a-z]*"' | sort | uniq -c | tr '\n' ' ')"; done
echo "  每台 master 的端點："
for m in yb-master-1 yb-master-2 yb-master-3; do echo "  [$m]"; endpoints "$(MPORT $m)"; done
echo "  tserver 的端點："
K get 30011 /status; K get 30011 /api/v1/health-check; K get 30021 /prometheus-metrics 6
echo "  叢集 metric 由哪台 master 輸出（follower 是 0）："
K q 'num_tablet_servers_live{job="yb-master"}'
K q 'is_load_balancing_enabled{job="yb-master"}'
K q 'is_raft_leader{job="yb-master", table_name="sys.catalog"}'
echo "  tablet 層級 metric 是加總到表（同一台 tserver 上同一張表的 tablet 加起來）："
K q 'is_raft_leader{job="yb-tserver", table_name="t"}'
K q 'sum by (table_name) (is_raft_leader{job="yb-tserver", table_name=~"t|transactions"})'
K q 'server_memory_hard_limit'
K q 'server_memory_soft_limit{job="yb-tserver"}'
K q 'mem_tracker_server{job=~"yb-master|yb-tserver"}'
K q 'ts_supportable_tablet_peers'
K q 'yb_ysqlserver_max_connection_total'
threeway
ALERTS

H "B postgres_exporter 預設 collector 對 YSQL"
echo "  直接執行 exporter 用到、可能出問題的查詢（statement_timeout 8 秒）："
for q in "select count(*) from pg_stat_replication" "select count(*) from pg_replication_slots" "select pg_is_in_recovery()" \
         "select pg_current_wal_lsn()" "select name, count(*) from pg_settings group by 1 having count(*) > 1" \
         "select count(*) from pg_stat_bgwriter" "select pg_database_size('yugabyte')" "select count(*) from pg_stat_archiver" "select count(*) from pg_ls_waldir()"; do
  Y yb-tserver-1 yb-tserver-1 "set statement_timeout=8000; $q"; done
docker compose --profile pgexp-default up -d pgexp-default >/dev/null 2>&1
T0=$(mono)
for w in 20 50; do sleep $((w - $(since "$T0"))) 2>/dev/null; echo "  +$(since "$T0") 秒："
  K q 'up{job="yb-pgexp-default"}'
  K q 'yb_ysqlserver_connection_total{job="yb-ysql", instance="yb-tserver-1:13000"}'
  K q 'increase(yb_ysqlserver_connection_over_limit_total{job="yb-ysql", instance="yb-tserver-1:13000"}[1m])'
  K get 30093 /metrics 30
  Y yb-tserver-1 yb-tserver-1 "select count(*) from pg_stat_activity where query like '%FROM pg_stat_replication%' and state = 'active'"
done
docker compose logs --no-log-prefix pgexp-default 2>&1 | sed 's/time=[^ ]* //; s/duration_seconds=[0-9.]*/duration_seconds=…/' | grep -E 'level=(ERROR|WARN)' | sort | uniq -c | sort -rn | head -12
docker compose --profile pgexp-default stop pgexp-default >/dev/null 2>&1; docker compose --profile pgexp-default rm -f pgexp-default >/dev/null 2>&1
sleep 5
echo "  exporter 停掉 5 秒後，server 端卡住的 backend 還在（客戶端斷線不會結束它們）："
Y yb-tserver-2 yb-tserver-1 "select count(*) from pg_stat_activity where query like '%FROM pg_stat_replication%' and state = 'active'"
Y yb-tserver-2 yb-tserver-1 "select count(pg_terminate_backend(pid)) from pg_stat_activity where query like '%FROM pg_stat_replication%' and pid <> pg_backend_pid()"
sleep 5
echo "  關掉 stat_replication 與 settings 的 exporter（job yb-pgexp）："
K q 'pg_scrape_collector_success{job="yb-pgexp"}'
K q 'up{job="yb-pgexp"}'
K q 'pg_up{job="yb-pgexp"}'
K q 'count by (__name__) ({job="yb-pgexp", __name__=~"pg_.*"})'
K q 'pg_stat_database_xact_commit{job="yb-pgexp", datname="yugabyte"}'
K q 'pg_stat_database_tup_inserted{job="yb-pgexp", datname="yugabyte"}'
K q 'pg_database_size_bytes{job="yb-pgexp"}'
K q 'pg_stat_bgwriter_buffers_alloc_total{job="yb-pgexp"}'
K q 'pg_stat_user_tables_n_live_tup{job="yb-pgexp"}'
K q 'pg_replication_lag_seconds{job="yb-pgexp"}'
K q 'pg_replication_is_replica{job="yb-pgexp"}'
K q 'pg_stat_activity_count{job="yb-pgexp", state="active"}'
K q 'pg_locks_count{job="yb-pgexp", mode="accessexclusivelock"}'

H "C1 停 tserver-3（docker kill）：dead 判定與 under-replicated"
docker compose kill yb-tserver-3 >/dev/null 2>&1; T0=$(mono)
for w in 20 75 150; do sleep $((w - $(since "$T0"))) 2>/dev/null
  echo; echo "  ---- tserver-3 停掉 +$(since "$T0") 秒（tserver_unresponsive_timeout_ms 60 秒；follower_unavailable_considered_failed_sec 120 秒）----"
  threeway; endpoints "$(MPORT "$(masterleader)")"; K get 30011 /api/v1/health-check 6
  Y yb-tserver-1 yb-tserver-1 "insert into t values ($((2000 + w)), 'c1') on conflict do nothing"
  Y yb-tserver-1 yb-tserver-1 "select count(*) from t"
  ALERTS
done

H "C2 再停 tserver-2：tablet 沒有過半副本（leaderless）"
docker compose kill yb-tserver-2 >/dev/null 2>&1; T0=$(mono)
for w in 30 150; do sleep $((w - $(since "$T0"))) 2>/dev/null
  echo; echo "  ---- tserver-2 也停掉 +$(since "$T0") 秒（leaderless_tablet_alert_delay_secs 120 秒）----"
  threeway; endpoints "$(MPORT "$(masterleader)")"
  Y yb-tserver-1 yb-tserver-1 "select count(*) from t"
  ALERTS
done
docker compose start yb-tserver-2 yb-tserver-3 >/dev/null 2>&1; T0=$(mono)
for i in $(seq 1 30); do n=$(curl -s -m2 "http://127.0.0.1:$(MPORT "$(masterleader)")/api/v1/health-check" | grep -c '"dead_nodes":\[\]'); [ "$n" = 1 ] && break; sleep 4; done
echo "  兩台 tserver 啟動後 $(since "$T0") 秒 dead_nodes 清空"
sleep 30
endpoints "$(MPORT "$(masterleader)")"; ALERTS

H "D 停 master leader：重新選舉"
L=$(masterleader); echo "  master leader：$L"
docker compose kill "$L" >/dev/null 2>&1; T0=$(mono); NL=none
for i in $(seq 1 60); do NL=$(masterleader); [ "$NL" != none ] && [ "$NL" != "$L" ] && break; sleep 1; done
echo "  新 leader $NL，kill 後 $(since "$T0") 秒 /api/v1/is-leader 回 200（主機 monotonic）"
Y yb-tserver-1 yb-tserver-1 "insert into t values (3001, 'd') on conflict do nothing"
sleep 10; echo "  +$(since "$T0") 秒："; threeway; ALERTS
docker compose start "$L" >/dev/null 2>&1; sleep 25

H "E 停 3 台 master 中的 2 台：master 失去 quorum"
L=$(masterleader); F=$(for m in yb-master-1 yb-master-2 yb-master-3; do [ "$m" != "$L" ] && echo "$m"; done | head -1)
S=$(for m in yb-master-1 yb-master-2 yb-master-3; do [ "$m" != "$L" ] && [ "$m" != "$F" ] && echo "$m"; done)
echo "  停 $L（leader）與 $F，留下 $S"
# 先開一條長連線：每 10 秒讀一次、寫一次，看已經建立的 session 在 master 失去 quorum 時還能不能用
printf "set statement_timeout=15000;\n" > results/.sess.sql
for i in $(seq 1 16); do printf "select 'sess $i', clock_timestamp(), count(*) from t;\ninsert into t values (40%02d, 'e') on conflict do nothing;\nselect pg_sleep(10);\n" "$i" >> results/.sess.sql; done
docker compose cp results/.sess.sql yb-tserver-2:/tmp/sess.sql >/dev/null 2>&1
docker compose exec -T -d yb-tserver-2 bash -c '/home/yugabyte/bin/ysqlsh -h yb-tserver-2 -At -f /tmp/sess.sql > /tmp/sess.log 2>&1'
sleep 12
docker compose kill "$L" "$F" >/dev/null 2>&1; T0=$(mono)
for w in 20 60 120; do sleep $((w - $(since "$T0"))) 2>/dev/null
  echo; echo "  ---- 停 2 台 master +$(since "$T0") 秒 ----"
  threeway; endpoints "$(MPORT "$S")"; K get "$(MPORT "$S")" /prometheus-metrics 6
  K get 30011 /api/v1/health-check 6; K get 30011 /status 6
  Y yb-tserver-1 yb-tserver-1 "select count(*) from t"
  Y yb-tserver-1 yb-tserver-1 "insert into t values ($((5000 + w)), 'e') on conflict do nothing"
  Y yb-tserver-1 yb-tserver-1 "select 1"
  ALERTS
done
docker compose start "$L" "$F" >/dev/null 2>&1; sleep 40
echo "  長連線（建立在 master 停掉之前）的輸出："
docker compose exec -T yb-tserver-2 cat /tmp/sess.log | sed 's/^/    /' | head -60
rm -f results/.sess.sql
threeway; ALERTS

H "F YSQL 連線上限（ysql_max_connections=12）"
for i in $(seq 1 12); do docker compose exec -T -d yb-tserver-2 /home/yugabyte/bin/ysqlsh -h yb-tserver-1 -At -c "select pg_sleep(70)"; done
sleep 15
Y yb-tserver-3 yb-tserver-1 "select 1"
Y yb-tserver-3 yb-tserver-2 "select 1"
sleep 10
K q 'yb_ysqlserver_connection_total{job="yb-ysql"}'
K q 'yb_ysqlserver_max_connection_total{job="yb-ysql", instance="yb-tserver-1:13000"}'
K q 'increase(yb_ysqlserver_connection_over_limit_total{job="yb-ysql"}[1m])'
K q 'probe_success{job="yb-ysql-tcp"}'
K q 'up{job="yb-pgexp"}'
K q 'pg_up{job="yb-pgexp"}'
K q 'pg_stat_activity_count{job="yb-pgexp", state="active"}'
ALERTS
sleep 50

H "G 整段：clock skew、scrape 時間、記憶體"
K q 'max_over_time(hybrid_clock_skew{job=~"yb-master|yb-tserver"}[1h])'
K q 'max_over_time(scrape_duration_seconds{job=~"yb-master|yb-tserver|yb-ysql|yb-ycql|yb-pgexp"}[1h])'
K q 'max_over_time(prometheus_target_scrapes_sample_out_of_bounds_total[1h])'
K q 'max_over_time(prometheus_target_scrapes_sample_out_of_order_total[1h])'
for s in yb-master-1 yb-master-2 yb-master-3 yb-tserver-1 yb-tserver-2 yb-tserver-3; do
  echo "  $s 的 clock skew ERROR log 行數：$(docker compose logs --no-log-prefix "$s" 2>&1 | grep -c 'Too big clock skew')"; done
docker stats --no-stream --format '  {{.Name}} {{.MemUsage}}' | grep at-yb

H "結束"
docker compose --profile pgexp-default down -v >/dev/null 2>&1
echo "殘留容器：$(docker ps -a --filter name=at-yb -q | wc -l)；殘留 volume：$(docker volume ls -q --filter name=at-yb | wc -l)"
