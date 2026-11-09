#!/usr/bin/env bash
# 第二輪（約 5 分鐘）：heartbeat key 量真正的複寫延遲、ACL 拒絕 CONFIG 時 exporter 怎麼表現、
# BGSAVE 失敗時 Sentinel 實際收到什麼、複本重啟。原始輸出寫到 results/run2-<日期>.txt，結束會 docker compose down -v。
set -u
cd "$(dirname "$0")"
P=http://127.0.0.1:27090
OUT=results/run2-$(date +%Y%m%d).txt
mkdir -p results
export PROM_CONFIG=prometheus-run2.yml
q() { for e in "$@"; do echo "  > $e"; curl -s --get "$P/api/v1/query" --data-urlencode "query=$e" | python3 -I fmt.py; done; }
cli() { local svc=$1; shift; echo "  \$ [$svc] $*"; docker compose exec -T "$svc" valkey-cli "$@" 2>&1 | sed 's/^/    /'; }
clin() { local svc=$1; shift; docker compose exec -T "$svc" valkey-cli "$@" 2>&1; }
sfield() { docker compose exec -T s1 valkey-cli -p 26379 SENTINEL MASTER mymaster | tr -d '\r' | paste - - | grep -E "^($1)	" | tr '\t\n' '= '; }
BG=()
waitbg() { for p in "${BG[@]}"; do wait "$p"; done; BG=(); }
H() { echo; echo "## $1（$(date +%T)）"; }

docker compose --profile run2 up -d vk1 vk2 s1 s2 s3 e-vk1 e-vk2 e-s1 e-hb e-noconf prometheus >/dev/null 2>&1
sleep 20
exec > >(tee "$OUT") 2>&1
echo "環境：valkey 9.1.3-alpine（vk1 主、vk2 複本、s1-s3）、redis_exporter v1.97.0、prometheus v3.13.5；scrape 5s、timeout 4s"

H "R0 Sentinel 是否在 TILT（第一輪 Sentinel 完全沒有動作）"
for n in 1 2 3; do echo "  s$n $(docker compose exec -T s$n valkey-cli -p 26379 INFO sentinel | tr -d '\r' | grep -E '^sentinel_(tilt|tilt_since_seconds|total_tilt):' | tr '\n' ' ')"; done
docker compose logs s1 2>&1 | grep -cE '\+tilt' | sed 's/^/  s1 log 的 +tilt 次數：/'
docker compose logs s1 2>&1 | grep -E '\+tilt|-tilt' | tail -4 | sed 's/^/    /'
q 'redis_sentinel_tilt'

H "R1 heartbeat：vk1 每秒 SET at:heartbeat <伺服器時間>，複本端 exporter 用 --check-single-keys 讀"
docker compose exec -T vk1 valkey-cli -r 150 -i 1 EVAL "return redis.call('SET','at:heartbeat',redis.call('TIME')[1])" 0 >/dev/null 2>&1 & BG+=($!)
sleep 15
q 'time() - redis_key_value{key="at:heartbeat"}' 'redis_master_link_up{instance="e-vk2"}'
docker network disconnect at-vk-lab_default at-vk-lab-vk2-1
sleep 20
echo "  -- vk2 斷線 20 秒"
q 'time() - redis_key_value{key="at:heartbeat"}' 'redis_master_link_up{instance="e-vk2"}' 'redis_master_last_io_seconds_ago{instance="e-vk2"}' \
  'redis_connected_slave_lag_seconds' 'redis_script_values{key="master_link_down_since_seconds",instance="e-vk2"}'
docker network connect at-vk-lab_default at-vk-lab-vk2-1
sleep 15
echo "  -- 接回 15 秒後"
q 'time() - redis_key_value{key="at:heartbeat"}' 'redis_master_link_up{instance="e-vk2"}'

H "R2 ACL 不給 CONFIG：ACL SETUSER mon on >monpw +@all -config ~* &*"
cli vk1 ACL SETUSER mon on '>monpw' +@all -config '~*' '&*'
sleep 12
q 'up{instance="e-noconf"}' 'redis_up{instance="e-noconf"}' 'redis_exporter_last_scrape_error{instance="e-noconf"}' \
  'redis_config_maxclients' 'redis_config_maxmemory' 'scrape_samples_scraped{instance=~"e-noconf|e-vk1"}'
q 'redis_up{job="valkey-noconf"} == 1 unless on (job, instance) redis_config_maxclients{job="valkey-noconf"}'

H "R3 BGSAVE 失敗時 Sentinel 看到什麼（每 4 秒讀 SENTINEL MASTER；down-after 10s）"
echo "  前：$(sfield 'flags|last-ping-reply|last-ok-ping-reply|down-after-milliseconds')"
docker compose exec -T -u root vk1 chmod 555 /data
clin vk1 BGSAVE >/dev/null; sleep 3
cli vk1 PING
for i in 1 2 3 4 5 6 7 8; do sleep 4; echo "    +$((i*4))s $(sfield 'flags|last-ping-sent|last-ping-reply|last-ok-ping-reply')"; done
echo "  vk1 commandstats／errorstats："
clin vk1 INFO commandstats | tr -d '\r' | grep -E '^cmdstat_ping' | sed 's/^/    /'
clin vk1 INFO errorstats | tr -d '\r' | grep -E 'MISCONF' | sed 's/^/    /'
docker compose logs s1 --since 40s 2>&1 | grep -E 'sdown|odown|failover' | sed 's/^/    /' | tail -5
docker compose exec -T -u root vk1 chmod 755 /data
clin vk1 BGSAVE >/dev/null; sleep 4
cli vk1 INFO persistence | grep -a bgsave_status

H "R5 failover：docker pause vk1 45 秒"
echo "  前：$(sfield 'flags|config-epoch')"
docker compose pause vk1 >/dev/null
for i in 1 2 3 4 5 6 7 8 9; do sleep 5; echo "    +$((i*5))s $(sfield 'flags|ip|config-epoch|num-other-sentinels') tilt=$(docker compose exec -T s1 valkey-cli -p 26379 INFO sentinel | tr -d '\r' | grep ^sentinel_tilt: )"; done
q 'redis_instance_info{job="valkey"}' 'redis_sentinel_master_config_epoch' 'redis_sentinel_master_status'
docker compose unpause vk1 >/dev/null
echo "  恢復後每 2 秒看 vk1、vk2 自己的 role："
for i in 1 2 3 4 5 6 7 8; do echo "    +$((i*2))s vk1 $(clin vk1 INFO replication | tr -d '\r' | grep -E '^role:' ) vk2 $(clin vk2 INFO replication | tr -d '\r' | grep -E '^role:')"; sleep 2; done
sleep 5
q 'max_over_time(count by (cluster) (count by (cluster, run_id) (redis_instance_info{job="valkey", role="master"}))[1m:5s])' \
  'changes(redis_sentinel_master_config_epoch[3m])' 'redis_master_link_up'
docker compose logs s1 --since 90s 2>&1 | grep -E '\+sdown|\+odown|\+switch-master|\+failover-end|\+convert-to-slave|tilt' | sed 's/^/    /' | tail -10

H "R4 複本重啟（docker compose restart vk2）"
q 'changes(redis_start_time_seconds{instance="e-vk1"}[2m])' 'redis_uptime_in_seconds{cluster="vk"}'
clin vk1 INFO stats | tr -d '\r' | grep -E '^sync_(full|partial)' | sed 's/^/    /'
docker compose restart vk2 >/dev/null 2>&1
sleep 15
q 'redis_uptime_in_seconds{cluster="vk"}' 'changes(redis_start_time_seconds{instance="e-vk2"}[1m])' 'redis_master_link_up'
clin vk1 INFO stats | tr -d '\r' | grep -E '^sync_(full|partial)' | sed 's/^/    /'
docker compose logs vk2 --since 20s 2>&1 | grep -iE 'resync|psync|Loading|DB loaded' | sed 's/^/    /' | tail -5
waitbg

H "清理"
docker compose --profile run2 down -v >/dev/null 2>&1
docker ps -a --filter name=at-vk- --format '{{.Names}}' | sed 's/^/  殘留：/'
echo "  完成（$(date +%T)）"
