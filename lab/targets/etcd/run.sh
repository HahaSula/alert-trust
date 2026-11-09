#!/usr/bin/env bash
# etcd 三台叢集的健康第二路徑實驗，約 10 分鐘。原始輸出寫到 results/run-<日期>.txt，結束會 docker compose down -v。
# 注意：這台機器的 Docker 容器單調時鐘比牆鐘慢約 30%，Prometheus 內的時間（scrape_duration_seconds、for）只當參考；
# kv.py 量的耗時是主機時鐘。
set -u
cd "$(dirname "$0")"
OUT=results/run-$(date +%Y%m%d).txt
mkdir -p results
K() { python3 -I kv.py "$@"; }
ctl() { local s=$1; shift; echo "  \$ [$s] etcdctl $*"; docker compose exec -T "$s" /usr/local/bin/etcdctl "$@" 2>&1 | sed 's/^/    /'; }
H() { echo; echo "## $1（$(date +%T)）"; }
leader() { K q 'etcd_server_is_leader{job="etcd"} == 1' | grep -o 'e[123]:2379' | head -1 | cut -d: -f1; }
reload() { curl -s -X POST http://127.0.0.1:28090/-/reload; }

cp prom/prometheus.base.yml prom/prometheus.yml.tmp && sed 's/#SAMPLE_LIMIT#//' prom/prometheus.yml.tmp > prom/prometheus.yml && rm prom/prometheus.yml.tmp
docker compose up -d >/dev/null 2>&1
sleep 25
exec > >(tee "$OUT") 2>&1
echo "環境：etcd v3.7.3 ×3（--quota-backend-bytes 32MiB，其他預設）、Prometheus v3.13.5（scrape 5s）、blackbox_exporter v0.27.0（timeout 4s）"
ctl e1 endpoint status --cluster -w table

H "A 基線"
K snap
K q 'count by (instance) ({job="etcd"})'
echo "  預設 --metrics=basic：有沒有 server 端 gRPC histogram（mixin etcdGRPCRequestsSlow 用的）"
K q 'count(grpc_server_handling_seconds_bucket{job="etcd"})'
K q 'count(etcd_server_request_duration_seconds_bucket{job="etcd"})'
K q 'etcd_network_known_peers{job="etcd", instance="e1:2379"}'
K q 'etcd_server_feature_enabled{job="etcd", instance="e1:2379", name=~"CompactHashCheck|InitialCorruptCheck"}'
K q 'etcd_server_quota_backend_bytes{job="etcd"}'
K q '{job="etcd", __name__=~"etcd_network_snapshot_.*_failures|etcd_debugging_server_alarms|etcd_server_is_learner"}'
curl -s http://127.0.0.1:28001/metrics | grep -v '^#' | sed 's/[{ ].*//' | sort -u > results/names-etcd-3.7.3.txt
echo "  /metrics 的 metric 名稱 $(wc -l < results/names-etcd-3.7.3.txt) 個，存到 results/names-etcd-3.7.3.txt"
K alerts

H "B 停掉 leader（docker kill），20 秒後"
L=$(leader); echo "  leader 是 $L"
docker compose kill "$L" >/dev/null 2>&1
sleep 20
K snap
K q 'sum(etcd_server_is_leader{job="etcd"})'
K alerts
docker compose start "$L" >/dev/null 2>&1
sleep 20
echo "  $L 重新啟動 20 秒後"
K snap

H "C 停掉 3 台中的 2 台（失去 quorum）"
SURV=e3; docker compose kill e1 e2 >/dev/null 2>&1
echo "  存活：$SURV（host port 28003）；15 秒後"
sleep 15
K snap
echo "  直接打存活的 member（主機時鐘）："
K get 28003 /metrics
K get 28003 /livez
K get 28003 /readyz 10
K get 28003 /health 10
K get 28003 '/readyz?verbose' 10
echo "  寫入："
ctl e3 --command-timeout=5s put k1 v1
echo "  再等 70 秒讓 for: 1m 的規則觸發"
sleep 70
K snap
K alerts
K q 'etcd_server_proposals_failed_total{job="etcd"}'
K q 'probe_duration_seconds{job="etcd-readyz"}'
docker compose start e1 e2 >/dev/null 2>&1
sleep 25
echo "  e1、e2 重新啟動 25 秒後"
K snap

H "D 只有 scrape 失敗：job=\"etcd\" 加 sample_limit: 100（etcd 一次輸出上千個 sample）"
sed 's/#SAMPLE_LIMIT#/sample_limit: 100/' prom/prometheus.base.yml > prom/prometheus.yml; reload
sleep 75
K snap
K alerts
sed 's/#SAMPLE_LIMIT#//' prom/prometheus.base.yml > prom/prometheus.yml; reload
sleep 15

H "E /metrics 會不會被讀取或刪除卡住（endpoint-blocking）"
K put 28001 3000 2048 /load/
K load 28001 /load/ 4 20
K delprefix 28001 /load/

H "F NOSPACE：寫到 quota（32MiB）"
K put 28001 400 204800 /fill/
sleep 12
ctl e1 alarm list
K snap
K q 'etcd_debugging_server_alarms{job="etcd"}'
K q 'etcd_mvcc_db_total_size_in_bytes{job="etcd"} / etcd_server_quota_backend_bytes{job="etcd"}'
K get 28001 /readyz
K get 28001 /livez
K get 28001 /health
echo "  寫入與刪除："
ctl e1 put k2 v2
ctl e1 del /fill/000000

H "F2 compact 到目前 revision、每台 defrag 之後（alarm 還沒 disarm）"
REV=$(docker compose exec -T e1 /usr/local/bin/etcdctl endpoint status -w json | python3 -I -c 'import json,sys; print(json.load(sys.stdin)[0]["Status"]["header"]["revision"])')
ctl e1 del --prefix /fill/
REV=$((REV + 1))
ctl e1 compact "$REV" --physical
ctl e1 defrag --cluster
sleep 12
K q 'etcd_mvcc_db_total_size_in_bytes{job="etcd"} / etcd_server_quota_backend_bytes{job="etcd"}'
K q 'etcd_debugging_mvcc_compact_revision{job="etcd"}'
ctl e1 alarm list
ctl e1 put k3 v3
K alerts

H "F3 alarm 仍在時重啟 e2：e2 的 etcd_debugging_server_alarms 還在嗎"
docker compose restart e2 >/dev/null 2>&1
sleep 20
K q 'etcd_debugging_server_alarms{job="etcd"}'
ctl e2 alarm list
K get 28002 /health
K get 28002 /readyz
K snap

H "F4 disarm"
ctl e1 alarm disarm
ctl e1 put k4 v4
sleep 12
K snap
K q 'etcd_debugging_server_alarms{job="etcd"}'
echo "  disarm 後再等 70 秒：gauge 停在舊值時 EtcdAlarmActive（gauge 且 /health 失敗）還響嗎"
sleep 70
K q 'etcd_debugging_server_alarms{job="etcd"}'
K alerts

H "結束"
docker compose down -v >/dev/null 2>&1
echo "  殘留容器：$(docker ps -a --filter name=at-etcd -q | wc -l)"
