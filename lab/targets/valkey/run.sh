#!/usr/bin/env bash
# Valkey／Redis 監控實驗，約 12 分鐘。原始輸出寫到 results/run-<日期>.txt，結束會 docker compose down -v。
# 每個階段：製造狀況 -> 等幾次 scrape（5s）-> 同時印出 exporter 看到的（Prometheus 查詢）與伺服器直接回答的（valkey-cli）。
# 時間類數字：curl 的耗時用主機牆鐘量；容器內的單調時鐘在這台機器上比牆鐘慢約 30%，秒數只當量級看。
set -u
cd "$(dirname "$0")"
P=http://127.0.0.1:27090
OUT=results/run-$(date +%Y%m%d).txt
mkdir -p results

q() { for e in "$@"; do echo "  > $e"; curl -s --get "$P/api/v1/query" --data-urlencode "query=$e" | python3 -I fmt.py; done; }
bin() { case "$1" in r72|r8) echo redis-cli;; *) echo valkey-cli;; esac; }
cli() { local svc=$1; shift; echo "  \$ [$svc] $*"; docker compose exec -T "$svc" $(bin "$svc") "$@" 2>&1 | sed 's/^/    /'; }
clin() { local svc=$1; shift; docker compose exec -T "$svc" $(bin "$svc") "$@" 2>&1; }
# 背景工作：pid 記在 BG，用 waitbg 等（不用裸 wait，否則會等到 tee 的 process substitution）
BG=()
waitbg() { for p in "${BG[@]}"; do wait "$p"; done; BG=(); }
info() { local svc=$1 sec=$2; shift 2; echo "  \$ [$svc] INFO $sec | $*"; clin "$svc" INFO "$sec" | tr -d '\r' | grep -E "$@" | sed 's/^/    /'; }
sen() { echo "  \$ [s$1] SENTINEL $2"; docker compose exec -T "s$1" valkey-cli -p 26379 SENTINEL $2 2>&1 | tr '\n' ' ' | sed 's/^/    /'; echo; }
master() { docker compose exec -T s1 valkey-cli -p 26379 SENTINEL get-master-addr-by-name mymaster 2>/dev/null | head -1 | tr -d '\r'; }
H() { echo; echo "## $1（$(date +%T)）"; }

docker compose up -d >/dev/null 2>&1
sleep 30
exec > >(tee "$OUT") 2>&1
echo "環境：valkey 9.1.3-alpine（vk1 主、vk2 複本、s1-s3 Sentinel、kg）、redis 7.2.17-alpine、redis 8.10.3-alpine、redis_exporter v1.97.0、prometheus v3.13.5；scrape 5s、timeout 4s"
for s in vk1 r72 r8; do clin $s INFO server | tr -d '\r' | grep -E '^(redis_version|valkey_version|server_name):' | sed "s/^/  $s /"; done

H "A1 基線：up、redis_up、角色、Sentinel"
q 'up' 'redis_up' 'redis_instance_info' 'redis_exporter_last_scrape_error' 'redis_sentinel_master_status' \
  'redis_sentinel_master_ckquorum_status' 'redis_sentinel_master_ok_sentinels' 'redis_sentinel_master_ok_slaves'
H "A2 版本差異：metric 名稱（Valkey 9.1 vs Redis 7.2 vs Redis 8.10，都是預設設定）"
for inst in e-vk1 e-r72 e-r8 e-kg; do
  curl -s --get "$P/api/v1/label/__name__/values" --data-urlencode "match[]={instance=\"$inst\",__name__=~\"redis_.+\"}" \
    | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin)["data"]))' > "results/names-$inst.txt"
done
echo "  只在 Valkey（kg，單機預設）有："; comm -23 results/names-e-kg.txt results/names-e-r72.txt | grep -v key_group | sed 's/^/    /'
echo "  只在 Redis 7.2 有："; comm -13 results/names-e-kg.txt results/names-e-r72.txt | sed 's/^/    /'
echo "  只在 Redis 8.10 有（相對 7.2）："; comm -13 results/names-e-r72.txt results/names-e-r8.txt | sed 's/^/    /'
echo "  mixin RedisOutOfMemory 依賴的 redis_total_system_memory_bytes（預設不輸出）："
q 'redis_total_system_memory_bytes' 'redis_config_maxmemory' 'redis_memory_max_bytes'
for s in kg r72 r8; do echo "  [$s] $(clin $s CONFIG GET repl-backlog-size | tr '\n' ' ') $(clin $s CONFIG GET io-threads | tr '\n' ' ')"; done
q 'scrape_duration_seconds' 'scrape_samples_scraped'

H "B slowlog 與 latency monitor（vk1 DEBUG SLEEP 0.05 ×3、0.3 ×1；r72 同樣）"
for s in vk1 r72; do for i in 1 2 3; do clin $s DEBUG SLEEP 0.05 >/dev/null; done; clin $s DEBUG SLEEP 0.3 >/dev/null; done
sleep 12
q 'redis_slowlog_length' 'redis_slowlog_last_id' 'redis_last_slow_execution_duration_seconds' \
  'redis_latency_spike_duration_seconds' 'redis_latency_spike_last' 'redis_commandlog_slow_length' \
  'redis_commands_latencies_usec_count{cmd="debug"}'
cli vk1 LATENCY LATEST
cli r72 LATENCY LATEST
cli vk1 SLOWLOG GET 1

H "C blocked client（vk1 上 BLPOP 25 秒）"
docker compose exec -T vk1 valkey-cli BLPOP nokey 25 >/dev/null 2>&1 & BG+=($!)
sleep 12
q 'redis_blocked_clients{instance="e-vk1"}' 'redis_total_blocking_keys{instance="e-vk1"}'
info vk1 clients 'blocked|connected_clients'
waitbg

H "D maxmemory：noeviction → volatile-lru（沒有 TTL）→ allkeys-lru"
clin vk1 DEBUG POPULATE 150000 fill 64 >/dev/null
used=$(clin vk1 INFO memory | tr -d '\r' | awk -F: '/^used_memory:/{print $2}')
lim=$((used - 2*1024*1024))
echo "  used_memory=$used，設 maxmemory=$lim（比用量少 2MB）"
clin vk1 CONFIG SET maxmemory $lim >/dev/null
cli vk1 SET d:1 v
cli vk1 INCR d:counter
cli vk1 GET fill:1
sleep 12
echo "  -- noeviction"
q 'redis_memory_used_bytes{instance="e-vk1"}' 'redis_memory_max_bytes{instance="e-vk1"}' \
  'redis_errors_total{instance="e-vk1"}' 'redis_commands_rejected_calls_total{instance="e-vk1"}' \
  'redis_evicted_keys_total{instance="e-vk1"}' 'redis_up{instance="e-vk1"}' 'redis_script_result{instance="e-vk1"}' \
  'redis_instance_info{instance="e-vk1"}'
clin vk1 CONFIG SET maxmemory-policy volatile-lru >/dev/null
cli vk1 SET d:2 v
sleep 7
echo "  -- volatile-lru，沒有任何 key 有 TTL"
q 'redis_errors_total{instance="e-vk1",err="OOM"}' 'redis_evicted_keys_total{instance="e-vk1"}' 'redis_db_keys_expiring{instance="e-vk1"}'
clin vk1 CONFIG SET maxmemory-policy allkeys-lru >/dev/null
for i in $(seq 1 20); do clin vk1 SET d:lru:$i v >/dev/null; done
cli vk1 SET d:3 v
sleep 12
echo "  -- allkeys-lru"
q 'redis_errors_total{instance="e-vk1",err="OOM"}' 'redis_evicted_keys_total' 'redis_memory_used_bytes{cluster="vk"}' \
  'redis_db_keys{cluster="vk"}' 'redis_eviction_exceeded_time_ms_total{instance="e-vk1"}'
info vk2 memory '^(used_memory|maxmemory):'
clin vk1 CONFIG SET maxmemory 0 >/dev/null; clin vk1 CONFIG SET maxmemory-policy noeviction >/dev/null; clin vk1 FLUSHALL >/dev/null

H "E exporter 逾時與影響範圍（vk1 DEBUG SLEEP 7；Sentinel down-after 10s，不會 failover）"
( clin vk1 DEBUG SLEEP 7 >/dev/null ) & BG+=($!)
sleep 1
for u in e-vk1 e-short; do
  ( s=$(date +%s.%N); body=$(docker compose exec -T prometheus wget -qO- -T 20 http://$u:9121/metrics 2>&1); e=$(date +%s.%N)
    printf '  curl %s /metrics：%.1f 秒（主機牆鐘），%s\n' $u "$(echo "$e - $s" | bc)" "$(echo "$body" | grep -E '^redis_up |^redis_exporter_last_scrape_error' | tr '\n' ' ')" ) & BG+=($!)
done
waitbg
sleep 6
q 'min_over_time(up{cluster="vk"}[20s])' 'min_over_time(redis_up{cluster="vk"}[20s])' 'max_over_time(scrape_duration_seconds{cluster="vk"}[20s])' \
  'min_over_time(up{job="valkey-exporter"}[20s])' 'max_over_time(redis_exporter_last_scrape_connect_time_seconds{instance="e-short"}[20s])'

H "F 連線額滿（maxclients 40，從主機開 45 條連線 15 秒）"
python3 -I -c '
import socket, time
s = []
for i in range(45):
    try:
        c = socket.create_connection(("127.0.0.1", 27001), timeout=2); s.append(c)
    except OSError as e:
        print("  connect", i, e)
time.sleep(15)
' & BG+=($!)
sleep 11
q 'up{instance="e-vk1"}' 'redis_up{cluster="vk"}' 'redis_connected_clients{instance="e-vk1"}' 'redis_rejected_connections_total{instance="e-vk1"}' \
  'redis_exporter_last_scrape_error{instance=~"e-vk1|e-short"}' 'redis_sentinel_master_status'
waitbg
sleep 6
q 'redis_up{instance="e-vk1"}' 'redis_rejected_connections_total{instance="e-vk1"}'

H "G 複本斷線與 full resync（vk2 從 default 網路拔掉 25 秒，期間 vk1 寫 3MB；repl-backlog-size 1mb、repl-timeout 5）"
info vk1 stats '^sync_(full|partial)'
docker network disconnect at-vk-lab_default at-vk-lab-vk2-1
clin vk1 EVAL "for i=1,3000 do redis.call('SET','g:'..i,string.rep('x',1000)) end return 1" 0 >/dev/null
sleep 18
echo "  -- 斷線中"
q 'redis_master_link_up' 'redis_master_last_io_seconds_ago' 'redis_script_values{key="master_link_down_since_seconds"}' \
  'redis_connected_slaves{instance="e-vk1"}' 'redis_connected_slave_lag_seconds' 'redis_connected_slave_offset_bytes' \
  'redis_master_repl_offset{cluster="vk"}' 'redis_slave_repl_offset' 'up{instance=~"e-vk2|multi-vk2"}' 'redis_sentinel_master_ok_slaves'
info vk2 replication '^(master_link|master_last_io|master_sync)'
sleep 7
docker network connect at-vk-lab_default at-vk-lab-vk2-1
sleep 15
echo "  -- 接回 15 秒後"
q 'redis_master_link_up' 'redis_connected_slaves{instance="e-vk1"}' 'increase(redis_replica_resyncs_full{instance="e-vk1"}[1m])' \
  'redis_replica_resyncs_full{instance="e-vk1"}' 'redis_replica_partial_resync_denied{instance="e-vk1"}' 'redis_replica_partial_resync_accepted{instance="e-vk1"}' \
  'redis_master_repl_offset{instance="e-vk1"} - on(cluster) group_right redis_slave_repl_offset'
info vk1 stats '^sync_(full|partial)'
echo "  vk1 log："; docker compose logs vk1 --since 60s 2>&1 | grep -iE 'resync|backlog|psync|lost|timeout' | sed 's/^/    /' | tail -8
echo "  vk2 log："; docker compose logs vk2 --since 60s 2>&1 | grep -iE 'resync|backlog|psync|lost|timeout|MASTER <-> REPLICA sync' | sed 's/^/    /' | tail -8
clin vk1 FLUSHALL >/dev/null

H "H BGSAVE 失敗（vk1 的 /data 改成 555；stop-writes-on-bgsave-error 預設 yes）"
docker compose exec -T -u root vk1 chmod 555 /data
cli vk1 BGSAVE
sleep 3
info vk1 persistence '^rdb_(last_bgsave_status|last_save_time|bgsave_in_progress|saves)'
cli vk1 SET h:1 v
cli vk1 PING
sleep 8
q 'redis_rdb_last_bgsave_status{instance="e-vk1"}' 'redis_rdb_last_save_timestamp_seconds{instance="e-vk1"}' 'redis_errors_total{instance="e-vk1"}' \
  'redis_up{instance="e-vk1"}' 'redis_script_result{instance="e-vk1"}'
echo "  Sentinel 怎麼看這台（PING 被回 MISCONF；down-after 10s），每 5 秒看一次："
for i in 1 2 3 4 5 6; do sleep 5; echo "    +$((i*5))s master=$(master) $(docker compose exec -T s1 valkey-cli -p 26379 INFO sentinel | tr -d '\r' | grep ^master0)"; done
docker compose logs s1 --since 45s 2>&1 | grep -E '\+sdown|\+odown|\+switch-master|failover' | sed 's/^/    /' | tail -8
q 'redis_sentinel_master_status' 'redis_instance_info{cluster="vk"}'
docker compose exec -T -u root vk1 chmod 755 /data
sleep 2
cli vk1 BGSAVE
sleep 4
info vk1 persistence '^rdb_last_bgsave_status'
sleep 6
q 'redis_rdb_last_bgsave_status{instance="e-vk1"}' 'redis_latest_fork_seconds{cluster="vk"}' 'redis_rdb_last_cow_size_bytes{cluster="vk"}' \
  'redis_script_values{key="total_forks"}'

H "I Sentinel failover（docker pause 目前的主節點 25 秒）"
M=$(master); MS=""
for s in vk1 vk2; do clin $s INFO replication | tr -d '\r' | grep -q '^role:master' && MS=$s; done
echo "  Sentinel 回報的主節點：$M；自己回報 role:master 的服務：$MS"
q 'redis_sentinel_master_config_epoch'
docker compose pause $MS >/dev/null
for i in 1 2 3 4 5; do sleep 5; echo "    +$((i*5))s sentinel 回報 master=$(master)"; done
q 'redis_instance_info{cluster="vk"}' 'count by (cluster) (redis_instance_info{cluster="vk",role="master"})' 'redis_sentinel_master_config_epoch' \
  'redis_sentinel_master_status' 'up{cluster="vk"}'
docker compose unpause $MS >/dev/null
echo "  恢復後每秒看舊主節點自己的 role："
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do echo "    +${i}s $MS $(clin $MS INFO replication | tr -d '\r' | grep -E '^(role|master_host|master_link_status):' | tr '\n' ' ')"; sleep 1; done
sleep 8
q 'count by (cluster) (redis_instance_info{cluster="vk",role="master"})' 'max_over_time(count by (cluster) (redis_instance_info{cluster="vk",role="master"})[1m:5s])' \
  'changes(redis_sentinel_master_config_epoch[2m])' 'redis_master_link_up'
docker compose logs s1 --since 70s 2>&1 | grep -E '\+sdown|\+odown|\+switch-master|\+failover-end|\+convert-to-slave|elected' | sed 's/^/    /' | tail -10

H "J Sentinel quorum（停掉 s2、s3 25 秒）"
docker compose stop s2 s3 >/dev/null 2>&1
sleep 25
q 'redis_sentinel_master_ckquorum_status' 'redis_sentinel_master_ok_sentinels' 'redis_sentinel_master_sentinels' 'redis_sentinel_master_status'
sen 1 "CKQUORUM mymaster"
docker compose start s2 s3 >/dev/null 2>&1

H "K -script 失敗的影響（e-bad：script 呼叫不存在的指令）"
q 'up{instance="e-bad"}' 'redis_up{instance="e-bad"}' 'redis_exporter_last_scrape_error{instance="e-bad"}' 'scrape_samples_scraped{instance=~"e-bad|e-kg"}' \
  'redis_db_keys{instance="e-bad"}'

H "L key group 與 count-keys 的成本（kg：40 萬個 key；slowlog 門檻 1ms）"
q 'scrape_duration_seconds{instance="e-kg"}' 'redis_last_key_groups_scrape_duration_milliseconds'
clin kg DEBUG POPULATE 200000 user 32 >/dev/null; clin kg DEBUG POPULATE 200000 sess 32 >/dev/null
clin kg SLOWLOG RESET >/dev/null
sleep 20
q 'scrape_duration_seconds{instance="e-kg"}' 'redis_last_key_groups_scrape_duration_milliseconds' 'redis_key_group_count' 'redis_keys_count' \
  'redis_slowlog_length{instance="e-kg"}' 'scrape_duration_seconds{instance="e-bad"}'
echo "  kg 的 slowlog（exporter 的 EVAL 與 SCAN）："
clin kg SLOWLOG GET 6 | tr -d '\r' | grep -E '^[0-9]+$|EVAL|SCAN|evalsha|scan' | paste -sd' ' | fold -w 200 | sed 's/^/    /'
echo "  kg commandstats："; clin kg INFO commandstats | tr -d '\r' | grep -E 'evalsha|eval:|scan|memory' | sed 's/^/    /'

H "清理"
docker compose down -v >/dev/null 2>&1
docker ps -a --filter name=at-vk- --format '{{.Names}}' | sed 's/^/  殘留：/'
echo "  完成（$(date +%T)）"
