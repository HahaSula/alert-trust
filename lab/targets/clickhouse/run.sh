#!/usr/bin/env bash
# ClickHouse v26.10.2.103-stable（server ×2：1 shard、2 replicas；Keeper ×3）的三方交叉、Too many parts、mutation 失敗、記憶體上限、
# Distributed 送出、備份、replica 落後與停機、Keeper 失去 quorum、<prometheus> 關掉的實驗，約 25 分鐘。
# 原始輸出寫到 results/run-<日期>.txt，結束會 docker compose down -v。
# 注意：這台機器的 Docker 容器單調時鐘比牆鐘慢約 30%；Prometheus 內的時間（for、scrape_duration_seconds）只當參考，
# 耗時用主機的 monotonic 時鐘（q.py、since）量。
set -u
cd "$(dirname "$0")"
OUT=results/run-$(date +%Y%m%d).txt
mkdir -p results
K() { python3 -I q.py "$@"; }
S1() { K sql 31001 "$1"; }
S2() { K sql 31002 "$1"; }
H() { echo; echo "## $1（$(date -u +%T)Z）"; }
mono() { python3 -I -c 'import time; print(f"{time.monotonic():.2f}")'; }
since() { python3 -I -c "import time,sys; print(f'{time.monotonic()-float(sys.argv[1]):.0f}')" "$1"; }
KPORT4() { case $1 in keeper1) echo 31021;; keeper2) echo 31022;; keeper3) echo 31023;; esac; }
keeperleader() { for k in keeper1 keeper2 keeper3; do
    K tcp "$(KPORT4 $k)" mntr 2>/dev/null | grep -q 'zk_server_state	leader' && { echo "$k"; return; }; done; echo none; }
endpoints() {
  for p in 31001 31002; do K get $p /ping 4; K get $p '/replicas_status?verbose=1' 4; done
  for p in 31031 31032 31033; do K get $p /ready 4; done
  for p in 31021 31022 31023; do K tcp $p ruok; K tcp $p mntr; done
}
threeway() {
  echo "  三方交叉（up／probe／健康 metric）："
  K q 'up{job=~"clickhouse-server|clickhouse-keeper|clickhouse-sql"}'
  K q 'probe_success'
  K q 'ClickHouseAsyncMetrics_KeeperIsLeader'
  K q 'ClickHouseAsyncMetrics_KeeperSyncedFollowers'
  K q 'ClickHouseMetrics_ZooKeeperSession'
  K q 'ClickHouseMetrics_ReadonlyReplica'
  K q 'ClickHouseAsyncMetrics_ReplicasMaxAbsoluteDelay'
  K q 'ClickHouseAsyncMetrics_ReplicasMaxQueueSize'
  K q 'chq_replica_readonly{table="default.events"}'
  K q 'chq_replica_absolute_delay_seconds{table="default.events"}'
  K q 'chq_replica_active_replicas{table="default.events"}'
  K q 'chq_keeper_session_expired'
  K q 'scrape_duration_seconds{job=~"clickhouse-server|clickhouse-keeper|clickhouse-sql"}'
}
ALERTS() { K alerts '^(CH|ClickHouse)'; }

free -m | head -2
docker compose up -d >/dev/null 2>&1
for i in $(seq 1 30); do [ "$(curl -s -m2 127.0.0.1:31001/ping)" = "Ok." ] && [ "$(curl -s -m2 127.0.0.1:31002/ping)" = "Ok." ] && break; sleep 3; done
sleep 15
exec > >(tee "$OUT") 2>&1
echo "環境：ClickHouse v26.10.2.103-stable（映像 clickhouse/clickhouse-server:26.10.2.103 ×2、clickhouse/clickhouse-keeper:26.10.2.103 ×3）、"
echo "Prometheus v3.13.5（scrape 5s）、blackbox_exporter v0.27.0（timeout 4s）、query-exporter 5.2.2（interval 15s）"
echo "lab 設定：parts_to_delay_insert=20、parts_to_throw_insert=40、min_absolute_delay_to_close=30、session_timeout_ms=30000、max_server_memory_usage=600MB；"
echo "ch2 打開 asynchronous_metrics_enable_heavy_metrics（ch1 用預設 false）。主機記憶體："
free -m | head -2
docker stats --no-stream --format '  {{.Name}} {{.MemUsage}}' | grep at-ch

H "A 基線：端點、metric 名稱、Altinity 規則的名稱"
S1 "SELECT version()"
S1 "CREATE TABLE default.events ON CLUSTER lab (ts DateTime DEFAULT now(), k UInt32, v String) ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/events', '{replica}') ORDER BY k"
S1 "INSERT INTO default.events (k, v) SELECT number, 'v' FROM numbers(1000)"
sleep 3
S2 "SELECT count() FROM default.events"
S1 "SELECT name, value FROM system.server_settings WHERE name IN ('max_server_memory_usage', 'max_concurrent_queries', 'background_pool_size', 'asynchronous_metrics_enable_heavy_metrics', 'asynchronous_metrics_key_values_mode') FORMAT TSV"
S1 "SELECT name, value FROM system.settings WHERE name IN ('async_insert', 'wait_for_async_insert', 'max_memory_usage', 'max_replica_delay_for_distributed_queries') FORMAT TSV"
S1 "SELECT name, value FROM system.merge_tree_settings WHERE name IN ('parts_to_delay_insert', 'parts_to_throw_insert', 'max_parts_in_total', 'min_relative_delay_to_close', 'min_absolute_delay_to_close') FORMAT TSV"
endpoints
sleep 20
K q 'scrape_samples_scraped{job=~"clickhouse-server|clickhouse-keeper|clickhouse-sql"}'
K q 'scrape_response_size_bytes{job=~"clickhouse-server|clickhouse-keeper|clickhouse-sql"}'
K q 'count by (instance) ({job="clickhouse-server", __name__=~"ClickHouseAsyncMetrics_NumberOf(Detached|Pending).*"})'
K q 'ClickHouseAsyncMetrics_DiskAvailable'
K q 'count by (job) ({__name__=~"ClickHouseAsyncMetrics_DiskAvailable_.*"})'
for p in 31011 31041; do echo "  :$p 的 metric 名稱數：$(curl -s 127.0.0.1:$p/metrics | grep -v '^#' | sed 's/[{ ].*//' | sort -u | wc -l)"; done
curl -s 127.0.0.1:31011/metrics | grep -v '^#' | sed 's/[{ ].*//' | sort -u > results/names-server-26.10.2.103.txt
curl -s 127.0.0.1:31041/metrics | grep -v '^#' | sed 's/[{ ].*//' | sort -u > results/names-keeper-26.10.2.103.txt
S1 "SELECT count() FROM system.events WHERE event IN ('RejectedInserts', 'DelayedInserts')"
S1 "SELECT count() FROM system.events SETTINGS system_events_show_zero_values = 1"
echo "  Altinity clickhouse-operator release-0.27.4 規則的名稱是否有來源："
AR=$(mktemp -d)
curl -s -o "$AR/ch.yaml" https://raw.githubusercontent.com/Altinity/clickhouse-operator/release-0.27.4/deploy/prometheus/prometheus-alert-rules-clickhouse.yaml
curl -s -o "$AR/chkeeper.yaml" https://raw.githubusercontent.com/Altinity/clickhouse-operator/release-0.27.4/deploy/prometheus/prometheus-alert-rules-chkeeper.yaml
python3 -I altinity_names.py "$AR/ch.yaml" 31001 31041 "$AR/chkeeper.yaml"
rm -rf "$AR"
threeway
ALERTS

H "B Too many parts：停 merge，逐筆小 insert（async_insert=0）"
S1 "SYSTEM STOP MERGES default.events"
S2 "SYSTEM STOP MERGES default.events"
t0=$(mono)
for i in $(seq 1 45); do
  r=$(python3 -I q.py sql 31001 "INSERT INTO default.events (k, v) SETTINGS async_insert = 0 VALUES ($i, 'p')")
  case "$r" in *" 200 "*) printf '.';; *) printf '\n  insert %s: %s\n' "$i" "$(echo "$r" | cut -c1-220)";; esac
done
echo; echo "  45 筆 insert 用了 $(since "$t0") 秒（主機 monotonic）"
S1 "SELECT partition_id, count() FROM system.parts WHERE table = 'events' AND active GROUP BY partition_id FORMAT TSV"
S1 "SELECT event, value FROM system.events WHERE event IN ('DelayedInserts', 'RejectedInserts', 'DelayedInsertsMilliseconds') FORMAT TSV"
sleep 25
K q 'ClickHouseAsyncMetrics_MaxPartCountForPartition'
K q 'ClickHouseProfileEvents_DelayedInserts'
K q 'ClickHouseProfileEvents_RejectedInserts'
K q 'ClickHouseProfileEvents_DelayedInsertsMilliseconds'
K q 'ClickHouseErrorMetric_TOO_MANY_PARTS'
K q 'chq_parts_max_per_partition{table="default.events"}'
K q 'chq_merge_tree_setting'
K q 'increase(ClickHouseProfileEvents_RejectedInserts{job="clickhouse-server"}[1m])'
ALERTS
echo "  預設 async_insert（26.2 起為 1，wait_for_async_insert=1）逐筆 insert 20 次，part 數的變化："
S1 "SELECT count() FROM system.parts WHERE table = 'events' AND active"
for i in $(seq 1 20); do K sql 31001 "INSERT INTO default.events (k, v) VALUES ($((1000+i)), 'a')" >/dev/null; done
S1 "SELECT count() FROM system.parts WHERE table = 'events' AND active"
S1 "SELECT event, value FROM system.events WHERE event IN ('AsyncInsertQuery', 'AsyncInsertFlush', 'RejectedInserts') FORMAT TSV"
S1 "SYSTEM START MERGES default.events"
S2 "SYSTEM START MERGES default.events"
S1 "OPTIMIZE TABLE default.events FINAL"
sleep 10
S1 "SELECT count() FROM system.parts WHERE table = 'events' AND active"

H "C 失敗的 mutation、detached part"
S1 "ALTER TABLE default.events UPDATE v = toString(throwIf(k >= 0, 'lab: mutation fails on purpose')) WHERE 1"
sleep 40
S1 "SELECT mutation_id, is_done, parts_to_do, latest_fail_reason FROM system.mutations WHERE table = 'events' FORMAT TSV"
S2 "SELECT mutation_id, is_done, parts_to_do, substring(latest_fail_reason, 1, 120) FROM system.mutations WHERE table = 'events' FORMAT TSV"
S1 "SELECT count() FROM system.replication_queue WHERE table = 'events' AND last_exception != ''"
K q 'chq_mutations_failing'
K q 'chq_mutations_pending'
K q 'ClickHouseAsyncMetrics_NumberOfPendingMutations'
K q 'ClickHouseErrorMetric_FUNCTION_THROW_IF_VALUE_IS_NON_ZERO'
K q 'chq_replication_queue_failing'
ALERTS
S1 "KILL MUTATION WHERE table = 'events' SYNC FORMAT TSV"
sleep 10
P=$(curl -s --data-binary "SELECT name FROM system.parts WHERE table = 'events' AND active ORDER BY modification_time LIMIT 1" 127.0.0.1:31002/)
S2 "ALTER TABLE default.events DETACH PART '$P'"
sleep 25
S2 "SELECT name, reason FROM system.detached_parts FORMAT TSV"
K q 'chq_detached_parts'
K q 'ClickHouseAsyncMetrics_NumberOfDetachedParts'
K q 'ClickHouseAsyncMetrics_NumberOfDetachedByUserParts'
S2 "ALTER TABLE default.events ATTACH PART '$P'"

H "D 記憶體上限與並行上限"
S1 "SELECT number % 3000000 AS k, count() FROM numbers(6000000) GROUP BY k FORMAT Null SETTINGS max_memory_usage = 30000000"
PIDS=""; for i in 1 2 3; do K sql 31001 "SELECT sleep(2) SETTINGS max_concurrent_queries_for_user = 2" & PIDS="$PIDS $!"; done; wait $PIDS
sleep 12
K q 'ClickHouseErrorMetric_MEMORY_LIMIT_EXCEEDED'
K q 'ClickHouseProfileEvents_QueryMemoryLimitExceeded'
K q 'ClickHouseProfileEvents_GlobalMemoryLimitExceeded'
K q 'ClickHouseErrorMetric_TOO_MANY_SIMULTANEOUS_QUERIES'
K q 'ClickHouseMetrics_MemoryTracking / on (job, instance) (ClickHouseAsyncMetrics_CGroupMemoryTotal > 0 or ClickHouseAsyncMetrics_OSMemoryTotal)'
K q 'ClickHouseProfileEvents_FailedQuery'
ALERTS

H "E Distributed 非同步送出到不存在的 shard"
S1 "CREATE TABLE default.dist_broken AS default.events ENGINE = Distributed(broken, default, events)"
for i in 1 2 3 4 5; do S1 "INSERT INTO default.dist_broken (k, v) SETTINGS distributed_foreground_insert = 0 VALUES ($i, 'd')"; done
sleep 25
S1 "SELECT data_files, error_count, broken_data_files, substring(last_exception, 1, 120) FROM system.distribution_queue FORMAT TSV"
K q 'ClickHouseMetrics_DistributedFilesToInsert'
K q 'ClickHouseMetrics_BrokenDistributedFilesToInsert'
K q 'ClickHouseProfileEvents_DistributedAsyncInsertionFailures'
K q 'chq_distribution_queue_files'
K q 'chq_distribution_queue_errors'
ALERTS
S1 "DROP TABLE default.dist_broken SYNC"

H "F 備份：成功一次、同名再做一次（失敗）"
S1 "BACKUP TABLE default.events TO Disk('backups', 'b1.zip')"
S1 "BACKUP TABLE default.events TO Disk('backups', 'b1.zip')"
S1 "SELECT name, status, substring(error, 1, 120) FROM system.backups FORMAT TSV"
sleep 70
K q 'chq_backup_last_success_timestamp'
K q 'chq_backups_failed'
K q '{__name__=~"ClickHouse.*Backup.*", job="clickhouse-server"} > 0'
ALERTS

H "G1 replica 落後：ch2 停止 fetch，ch1 繼續寫"
S2 "SYSTEM STOP FETCHES default.events"
t0=$(mono)
for i in $(seq 1 8); do S1 "INSERT INTO default.events (k, v) SETTINGS async_insert = 0 VALUES ($((2000+i)), 'g')" >/dev/null; sleep 8; done
echo "  停止 fetch 後 $(since "$t0") 秒（主機）"
S2 "SELECT is_readonly, absolute_delay, queue_size, inserts_in_queue, active_replicas, total_replicas FROM system.replicas WHERE table = 'events' FORMAT TSV"
S1 "SELECT is_readonly, absolute_delay, queue_size, inserts_in_queue, active_replicas, total_replicas FROM system.replicas WHERE table = 'events' FORMAT TSV"
K get 31002 '/replicas_status?verbose=1' 4
K get 31002 /replicas_status 4
sleep 10
K q 'ClickHouseAsyncMetrics_ReplicasMaxAbsoluteDelay'
K q 'ClickHouseAsyncMetrics_ReplicasMaxInsertsInQueue'
K q 'chq_replica_absolute_delay_seconds{table="default.events"}'
K q 'probe_success{job="clickhouse-replicas-status"}'
ALERTS
S2 "SYSTEM START FETCHES default.events"
sleep 10
S2 "SELECT absolute_delay, queue_size FROM system.replicas WHERE table = 'events' FORMAT TSV"

H "G2 停掉 ch2，ch1 繼續寫"
docker compose stop ch2 >/dev/null 2>&1; t0=$(mono)
for i in $(seq 1 5); do S1 "INSERT INTO default.events (k, v) SETTINGS async_insert = 0 VALUES ($((3000+i)), 's')" >/dev/null; sleep 5; done
sleep 15
echo "  ch2 停止後 $(since "$t0") 秒（主機）"
S1 "SELECT is_readonly, absolute_delay, queue_size, active_replicas, total_replicas FROM system.replicas WHERE table = 'events' FORMAT TSV"
K get 31001 '/replicas_status?verbose=1' 4
threeway
ALERTS
docker compose start ch2 >/dev/null 2>&1; t0=$(mono)
for i in $(seq 1 30); do [ "$(curl -s -m2 127.0.0.1:31002/ping)" = "Ok." ] && break; sleep 2; done
sleep 10
echo "  ch2 啟動後 $(since "$t0") 秒（主機）"
S2 "SELECT is_readonly, absolute_delay, queue_size, active_replicas FROM system.replicas WHERE table = 'events' FORMAT TSV"
S1 "SELECT count() FROM default.events"
S2 "SELECT count() FROM default.events"

H "H Keeper 失去 quorum：停 leader 與一台 follower"
L=$(keeperleader); echo "  Keeper leader（mntr zk_server_state=leader）：$L"
STOP=$L; for k in keeper1 keeper2 keeper3; do [ "$k" != "$L" ] && { STOP="$STOP $k"; break; }; done
LEFT=$(for k in keeper1 keeper2 keeper3; do case " $STOP " in *" $k "*) ;; *) echo $k;; esac; done)
echo "  停止：$STOP；留下：$LEFT"
docker compose stop $STOP >/dev/null 2>&1; t0=$(mono)
sleep 10
echo "  +$(since "$t0") 秒（主機）"
S1 "SELECT is_readonly, is_session_expired FROM system.replicas WHERE table = 'events' FORMAT TSV"
S1 "INSERT INTO default.events (k, v) SETTINGS async_insert = 0 VALUES (4001, 'k')"
endpoints
sleep 40
echo "  +$(since "$t0") 秒（主機；session_timeout_ms 30 秒）"
S1 "SELECT is_readonly, is_session_expired, absolute_delay FROM system.replicas WHERE table = 'events' FORMAT TSV"
S2 "SELECT is_readonly, is_session_expired, absolute_delay FROM system.replicas WHERE table = 'events' FORMAT TSV"
S1 "SELECT host, is_expired FROM system.zookeeper_connection FORMAT TSV"
S1 "INSERT INTO default.events (k, v) SETTINGS async_insert = 0 VALUES (4002, 'k')"
S1 "SELECT count() FROM default.events"
endpoints
sleep 15
threeway
K q 'ClickHouseErrorMetric_TABLE_IS_READ_ONLY'
K q 'ClickHouseErrorMetric_KEEPER_EXCEPTION'
K q 'ClickHouseProfileEvents_ZooKeeperHardwareExceptions'
K q 'sum(probe_success{job="clickhouse-keeper-ready"})'
ALERTS
docker compose start $STOP >/dev/null 2>&1; t0=$(mono)
for i in $(seq 1 40); do [ "$(curl -s --data-binary 'SELECT sum(is_readonly) FROM system.replicas' 127.0.0.1:31001/)" = "0" ] && break; sleep 3; done
echo "  Keeper 啟動後 $(since "$t0") 秒 ch1 的 Replicated 表不再唯讀（主機）"
S1 "INSERT INTO default.events (k, v) SETTINGS async_insert = 0 VALUES (4003, 'k')"
sleep 15
threeway
ALERTS

H "I <prometheus> 的子項關掉、整段拿掉"
docker compose exec -T ch2 sh -c 'cat > /etc/clickhouse-server/config.d/zz-prom.xml' <<'EOF'
<clickhouse><prometheus><asynchronous_metrics>false</asynchronous_metrics></prometheus></clickhouse>
EOF
docker compose restart ch2 >/dev/null 2>&1
for i in $(seq 1 30); do [ "$(curl -s -m2 127.0.0.1:31002/ping)" = "Ok." ] && break; sleep 2; done
sleep 25
K get 31012 /metrics 4
K q 'up{instance="ch2:9363"}'
K q 'scrape_samples_scraped{job="clickhouse-server"}'
K q 'ClickHouseAsyncMetrics_MaxPartCountForPartition'
K q 'up{job="clickhouse-server"} == 1 unless on (job, instance) (ClickHouseAsyncMetrics_MaxPartCountForPartition and on (job, instance) ClickHouseMetrics_ReadonlyReplica and on (job, instance) ClickHouseProfileEvents_RejectedInserts and on (job, instance) ClickHouseErrorMetric_ALL and on (job, instance) group by (job, instance) (ClickHouseAsyncMetrics_DiskAvailable))'
ALERTS
docker compose exec -T ch2 sh -c 'cat > /etc/clickhouse-server/config.d/zz-prom.xml' <<'EOF'
<clickhouse><prometheus remove="remove"/></clickhouse>
EOF
docker compose restart ch2 >/dev/null 2>&1
for i in $(seq 1 30); do [ "$(curl -s -m2 127.0.0.1:31002/ping)" = "Ok." ] && break; sleep 2; done
sleep 25
K get 31012 /metrics 4
K get 31002 /metrics 4
K q 'up{instance="ch2:9363"}'
K q 'probe_success{instance="ch2:9363"}'
curl -s '127.0.0.1:31090/api/v1/targets?state=active' | python3 -I -c 'import json,sys; [print("  target", t["labels"]["instance"], t["health"], t["lastError"]) for t in json.load(sys.stdin)["data"]["activeTargets"] if t["labels"]["job"]=="clickhouse-server"]'
ALERTS

H "結束"
docker stats --no-stream --format '  {{.Name}} {{.MemUsage}}' | grep at-ch
docker compose down -v >/dev/null 2>&1
docker ps -a --format '{{.Names}}' | grep at-ch || echo "  沒有 at-ch 容器"
docker volume ls --format '{{.Name}}' | grep at-ch || echo "  沒有 at-ch volume"
