#!/usr/bin/env bash
# TiDB v8.5.8（PD ×3、TiKV ×3、TiDB ×1）的健康第二路徑與官方規則實驗，約 25 分鐘。原始輸出寫到 results/run-<日期>.txt，結束會 docker compose down -v。
# 注意：這台機器的 Docker 容器單調時鐘比牆鐘慢約 30%，牆鐘也會被 WSL 校時往回跳（TiKV log 有 "system time jumped back"）；
# Prometheus 內的時間（for、scrape_duration_seconds）只當參考。q.py 量的耗時與 SQL 的耗時是主機時鐘。
set -u
cd "$(dirname "$0")"
OUT=results/run-$(date +%Y%m%d).txt
mkdir -p results official .bin
K() { python3 -I q.py "$@"; }
H() { echo; echo "## $1（$(date +%T)）"; }
PORT() { case $1 in pd1) echo 29001;; pd2) echo 29002;; pd3) echo 29003;; esac; }
pdleader() { for p in pd1 pd2 pd3; do n=$(curl -s -m3 "http://127.0.0.1:$(PORT $p)/pd/api/v1/leader" | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["name"])' 2>/dev/null) && [ -n "$n" ] && { echo "$n"; return; }; done; }
sql() { local t0 t1 rc; t0=$(date +%s.%N); out=$(docker compose exec -T client timeout 25 mysql -h tidb -P 4000 -u root --connect-timeout=5 -N -e "$1" 2>&1); rc=$?; t1=$(date +%s.%N);
        printf '  SQL %-55s rc=%s %.2fs %s\n' "$1" "$rc" "$(echo "$t1 - $t0" | bc)" "$(echo "$out" | tr '\n' ' ' | cut -c1-160)"; }
reload() { curl -s -X POST http://127.0.0.1:29090/-/reload; }
promcfg() { sed -e "s|#PD_LEADER_RELABEL#|$1|" prom/prometheus.base.yml > prom/prometheus.yml.new && cat prom/prometheus.yml.new > prom/prometheus.yml && rm prom/prometheus.yml.new; }
threeway() {
  echo "  三方交叉（up／probe／健康 metric）："
  K q 'up{job=~"pd|tikv|tidb"}'
  K q 'probe_success{job=~"pd-readyz|pd-health|pd-ping|pd-leader-api|tikv-status|tidb-status|tidb-sql"}'
  K q 'service_member_role{job="pd", service="PD"}'
  K q 'etcd_server_has_leader{job="pd"}'
  K q 'max by (store, type) (pd_cluster_status{job="pd", type=~"store_(up|disconnected|slow|unhealth|down)_count"}) > 0'
  K q 'max by (type) (pd_regions_status{job="pd", type=~"(miss|down|pending|offline)-peer-region-count"})'
  K q 'time() - tidb_domain_lease_expire_time{job="tidb"}'
}

# 官方規則：tag v8.5.8 的原檔（Apache-2.0），不修改
curl -sf -o official/tidb.rules.yml https://raw.githubusercontent.com/pingcap/tidb/v8.5.8/pkg/metrics/alertmanager/tidb.rules.yml
curl -sf -o official/tikv.rules.yml https://raw.githubusercontent.com/tikv/tikv/v8.5.8/metrics/alertmanager/tikv.rules.yml
curl -sf -o official/pd.rules.yml https://raw.githubusercontent.com/tikv/pd/v8.5.8/metrics/alertmanager/pd.rules.yml
# 映像內的 tikv-server 帶壓縮的 debug info，啟動時會解壓進記憶體（約 1.9GB）；複製出來去掉 debug section 再掛進去
if [ ! -x .bin/tikv-server ]; then
  cid=$(docker create pingcap/tikv:v8.5.8); docker cp "$cid":/tikv-server .bin/tikv-server.full >/dev/null; docker rm "$cid" >/dev/null
  objcopy --strip-debug .bin/tikv-server.full .bin/tikv-server && rm .bin/tikv-server.full
fi
promcfg ""
free -m | head -2
docker compose up -d >/dev/null 2>&1
sleep 90
exec > >(tee "$OUT") 2>&1
echo "環境：TiDB v8.5.8（PD ×3、TiKV ×3、TiDB ×1）、Prometheus v3.13.5（scrape 5s）、blackbox_exporter v0.27.0（timeout 4s）"
echo "PD schedule.max-store-down-time=1m（預設 30m）；TiKV raftstore.max-peer-down-duration=40s（預設 10m）；其餘預設。主機記憶體："
free -m | head -2
docker stats --no-stream --format '  {{.Name}} {{.MemUsage}}' | grep at-tidb

H "A 基線：端點、metric 由誰輸出"
L=$(pdleader); echo "  PD leader（/pd/api/v1/leader）：$L"
K q 'scrape_samples_scraped{job=~"pd|tikv|tidb"}'
K q 'scrape_response_size_bytes{job=~"pd|tikv|tidb"}'
K q 'scrape_duration_seconds{job=~"pd|tikv|tidb"}'
for c in "pd 29001" "tikv 29011" "tidb 29080"; do set -- $c; curl -s localhost:$2/metrics | grep -v '^#' | sed 's/[{ ].*//' | sort -u > results/names-$1-8.5.8.txt; echo "  $1 /metrics 的 metric 名稱 $(wc -l < results/names-$1-8.5.8.txt) 個"; done
for p in /health /readyz /livez /pd/api/v1/health /pd/api/v1/ping /pd/api/v1/leader; do K get "$(PORT "$L")" "$p"; done
K get 29011 /status; K get 29011 /health
K get 29080 /status; K get 29080 /health
echo "  PD 的叢集 metric 由哪台輸出："
K q 'count by (instance) (pd_cluster_status{job="pd"})'
K q 'count by (instance) (pd_regions_status{job="pd"})'
K q 'etcd_server_is_leader{job="pd"}'
K stores 29001
K q 'tidb_server_memory_quota_bytes{job="tidb"}'
K q 'tikv_server_memory_quota_bytes{job="tikv"}'
K q 'process_resident_memory_bytes{job=~"tikv|tidb|pd"}'

H "A2 官方規則：三個問題與 selector 稽核"
echo "  (a) TiKV_thread_apply_worker_cpu_seconds：name=\"apply_.*\"（=）對 name=~\"apply_.*\"（=~）"
K q 'tikv_thread_cpu_seconds_total{name="apply_.*"}'
K q 'count by (instance) (tikv_thread_cpu_seconds_total{name=~"apply_.*"})'
K q 'max(rate(tikv_thread_cpu_seconds_total{name="apply_.*"}[1m])) by (instance)'
K q 'max(rate(tikv_thread_cpu_seconds_total{name=~"apply_.*"}[1m])) by (instance)'
echo "  (b) job=\"tidb\" 寫死：同一個 TiDB 用另一個 job 名稱（tidb-other-name）抓"
K q 'up{job=~"tidb.*"}'
K q 'changes(process_start_time_seconds{job="tidb"}[5m]) >= 0'
K q 'go_memstats_heap_inuse_bytes{job="tidb"}'
K q 'go_memstats_heap_inuse_bytes'
echo "  (d) PD_miss_peer_region_count 的 type：底線對連字號"
K q 'count(pd_regions_status{type="miss_peer_region_count"})'
K q 'count(pd_regions_status{type="miss-peer-region-count"})'
echo "  官方規則每個 selector 對到的 series 數（0 代表那條規則一定回傳空；TiKV 有些 metric 第一次用到才註冊）："
for f in pd tikv tidb; do echo "  [$f.rules.yml]"; K audit official/$f.rules.yml; done

H "B 輕量寫入與 /metrics 耗時"
sql "create table if not exists test.t (id int primary key auto_increment, v varchar(200))"
sql "insert into test.t (v) values (repeat('x',200))"
for i in 1 2 3 4 5 6 7 8 9 10; do sql "insert into test.t (v) select repeat('y',200) from test.t limit 2000" >/dev/null; done
sql "select count(*) from test.t"
for i in 1 2 3; do K get 29011 /metrics 10 | cut -c1-60; K get 29080 /metrics 10 | cut -c1-60; K get 29001 /metrics 10 | cut -c1-60; done

H "C 停掉 1 台 TiKV（docker kill tikv3）"
docker compose kill tikv3 >/dev/null 2>&1; T0=$(date +%s)
sleep 30; echo "  +$(( $(date +%s) - T0 ))s"
K stores 29001
threeway
sql "select count(*) from test.t"
sleep 60; echo "  +$(( $(date +%s) - T0 ))s"
K stores 29001
threeway
sql "insert into test.t (v) values ('during-tikv-down')"
sleep 45; echo "  +$(( $(date +%s) - T0 ))s"
threeway
K q 'max by (type) (pd_regions_status{job="pd"}) > 0'
K q 'increase(pd_checker_event_count{type="replica_checker"}[5m]) > 0'
K alerts

H "D TiKV 仍停著；讓 PD leader 的 scrape 失敗（改去沒有在聽的 port，instance 不變）"
L=$(pdleader); echo "  PD leader：$L"
promcfg "- { source_labels: [__address__], regex: '$L:2379', target_label: __address__, replacement: '$L:2399' }"; reload
sleep 90; echo "  reload 後 90 秒（PD leader 仍是 $(pdleader)）"
threeway
echo "  官方規則用 etcd_server_is_leader 當前提：leader 抓不到時對到幾條"
K q '(sum(pd_cluster_status{type="store_down_count"}) by (instance) > 0) and (sum(etcd_server_is_leader) by (instance) > 0)'
K q '(sum(pd_regions_status{type="down-peer-region-count"}) by (instance)  > 0) and (sum(etcd_server_is_leader) by (instance) > 0)'
K q 'sum(etcd_server_is_leader)'
echo "  不加前提的寫法（follower 的 pd_regions_status 是 0，pd_cluster_status 沒有）"
K q 'max(pd_regions_status{type="down-peer-region-count"})'
K q 'max(pd_cluster_status{type="store_down_count"})'
K alerts
promcfg ""; reload
docker compose start tikv3 >/dev/null 2>&1
sleep 60; echo "  config 還原、tikv3 重新啟動 60 秒後"
threeway
K alerts

H "E 停掉 PD leader（docker kill）"
L=$(pdleader); echo "  PD leader：$L"
docker compose kill "$L" >/dev/null 2>&1; T0=$(date +%s)
sleep 5; sql "insert into test.t (v) values ('pd-leader-killed+5s')"
sleep 15; echo "  +$(( $(date +%s) - T0 ))s，新 leader：$(pdleader)"
threeway
K q 'etcd_server_is_leader{job="pd"}'
K q 'max by (instance) (etcd_server_leader_changes_seen_total{job="pd"})'
sql "insert into test.t (v) values ('pd-leader-killed+20s')"
sleep 60; echo "  +$(( $(date +%s) - T0 ))s"
K alerts
docker compose start "$L" >/dev/null 2>&1
sleep 30; echo "  $L 重新啟動 30 秒後，leader：$(pdleader)"
K q 'service_member_role{job="pd", service="PD"}'

H "F 停掉 3 台 PD 中的 2 台（失去 quorum）"
L=$(pdleader); S=""; for p in pd1 pd2 pd3; do [ "$p" != "$L" ] && [ -z "$S" ] && S=$p; done
STOP=$(for p in pd1 pd2 pd3; do [ "$p" != "$S" ] && printf '%s ' $p; done)
echo "  存活：$S（host port $(PORT $S)）；停掉：$STOP"
docker compose kill $STOP >/dev/null 2>&1; T0=$(date +%s)
sleep 15; echo "  +$(( $(date +%s) - T0 ))s"
for p in /metrics /health /readyz /livez /pd/api/v1/health /pd/api/v1/leader /pd/api/v1/ping; do K get "$(PORT $S)" "$p" 12 | cut -c1-230; done
K get 29080 /status
K get 29011 /status
sql "select 1"
sql "select count(*) from test.t"
sql "insert into test.t (v) values ('pd-quorum-lost')"
threeway
sleep 60; echo "  +$(( $(date +%s) - T0 ))s"
threeway
sql "select 1"
sql "select count(*) from test.t"
K q 'sum by (instance) (rate(pd_client_cmd_handle_failed_cmds_duration_seconds_count{job="tidb"}[1m])) > 0'
K q 'sum by (type) (increase(tidb_tikvclient_backoff_seconds_count{job="tidb"}[2m])) > 0'
K q 'increase(tidb_session_schema_lease_error_total[5m])'
K alerts
docker compose start $STOP >/dev/null 2>&1
sleep 45; echo "  PD 重新啟動 45 秒後"
threeway
sql "insert into test.t (v) values ('pd-restored')"
K alerts

H "結束"
docker stats --no-stream --format '  {{.Name}} {{.MemUsage}}' | grep at-tidb
docker compose down -v >/dev/null 2>&1
rm -rf .bin
echo "  殘留容器：$(docker ps -a --filter name=at-tidb -q | wc -l)"
