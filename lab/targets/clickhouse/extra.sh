#!/usr/bin/env bash
# run.sh 的補充，約 12 分鐘：(J) CHPartsNearInsertDelay 用 query-exporter 查到的 parts_to_delay_insert；
# (K) broken part：MergeTree 表的資料檔被截短後重啟，part 被移到 detached（reason broken-on-start），看 SQL 與 heavy metric；
# (L) 使用者 DETACH PARTITION（reason 空字串）對照；(M) 備份成功與失敗後 query-exporter 的值；
# (N) Keeper 失去 quorum 時 query-exporter 的 SQL 路徑與 ZooKeeperSession*，恢復後多久回到正常。
# 原始輸出寫到 results/extra-<日期>.txt，結束會 docker compose down -v。
set -u
cd "$(dirname "$0")"
OUT=results/extra-$(date +%Y%m%d).txt
mkdir -p results
K() { python3 -I q.py "$@"; }
S1() { K sql 31001 "$1"; }
S2() { K sql 31002 "$1"; }
H() { echo; echo "## $1（$(date -u +%T)Z）"; }
waitping() { for i in $(seq 1 40); do [ "$(curl -s -m2 127.0.0.1:$1/ping)" = "Ok." ] && return; sleep 2; done; }

docker compose up -d >/dev/null 2>&1
waitping 31001; waitping 31002
exec > >(tee "$OUT") 2>&1
echo "環境：同 run.sh（ClickHouse v26.10.2.103-stable ×2、Keeper ×3、Prometheus v3.13.5、query-exporter 5.2.2）"

H "J part 數對 parts_to_delay_insert（query-exporter 的 merge_tree_settings 查詢每 1 分鐘）"
S1 "CREATE TABLE default.events ON CLUSTER lab (ts DateTime DEFAULT now(), k UInt32, v String) ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/events', '{replica}') ORDER BY k"
S1 "SYSTEM STOP MERGES default.events"
S2 "SYSTEM STOP MERGES default.events"
for i in $(seq 1 15); do K sql 31001 "INSERT INTO default.events (k, v) SETTINGS async_insert = 0 VALUES ($i, 'j')" >/dev/null; done
S1 "SELECT count() FROM system.parts WHERE table = 'events' AND active"
for i in $(seq 1 30); do [ "$(python3 -I q.py q 'chq_merge_tree_setting{name="parts_to_delay_insert"}' | grep -c '20$')" -ge 2 ] && break; sleep 5; done
sleep 15
K q 'chq_merge_tree_setting'
K q 'max by (instance) (ClickHouseAsyncMetrics_MaxPartCountForPartition{job="clickhouse-server"}) / on (instance) max by (instance) (chq_merge_tree_setting{job="clickhouse-sql", name="parts_to_delay_insert"})'
K alerts '^CHParts'
echo "  query-exporter log 裡被跳過的執行（Run time of job ... was missed）：$(docker compose logs qe 2>&1 | grep -c 'was missed')"
S1 "SYSTEM START MERGES default.events"
S2 "SYSTEM START MERGES default.events"

H "K broken part：截短 ch2 本機 MergeTree 表的一個資料檔後重啟"
S2 "CREATE TABLE default.local_t (k UInt32, v String) ENGINE = MergeTree ORDER BY k"
S2 "INSERT INTO default.local_t SELECT number, toString(number) FROM numbers(100000)"
D=$(curl -s --data-binary "SELECT path FROM system.parts WHERE table = 'local_t' AND active LIMIT 1" 127.0.0.1:31002/)
echo "  part 路徑：$D"
docker compose exec -T ch2 sh -c "ls -l '$D' | head -20; f=\$(ls '$D' | grep -E '^(v|data)\.(bin|cmrk2|mrk2)$' | head -1); echo \"  截短 \$f\"; truncate -s 10 '$D'/\$f"
docker compose restart ch2 >/dev/null 2>&1
waitping 31002
sleep 5
S2 "SELECT count() FROM default.local_t"
S2 "SELECT name, reason FROM system.detached_parts WHERE table = 'local_t' FORMAT TSV"
docker compose logs ch2 2>&1 | grep -i -E "broken|suspicious|detach" | grep -v -i "warning.*memory" | tail -5 | cut -c1-260
sleep 30
K q 'chq_detached_parts'
K q 'ClickHouseAsyncMetrics_NumberOfDetachedParts'
K q 'ClickHouseAsyncMetrics_NumberOfDetachedByUserParts'
K alerts '^CH(Broken|Detached)'

H "L 使用者 DETACH PARTITION（對照）"
S1 "CREATE TABLE default.local_u (k UInt32) ENGINE = MergeTree ORDER BY k"
S1 "INSERT INTO default.local_u SELECT number FROM numbers(10)"
S1 "ALTER TABLE default.local_u DETACH PARTITION tuple()"
sleep 25
S1 "SELECT name, reason FROM system.detached_parts WHERE table = 'local_u' FORMAT TSV"
K q 'chq_detached_parts'
K alerts '^CH(Broken|Detached)'

H "M 備份：成功一次、同名再做一次（失敗），等 query-exporter 更新（run.sh 的 70 秒不夠：容器時鐘慢，1m 的 interval 實際更長）"
S1 "BACKUP TABLE default.events TO Disk('backups', 'm1.zip')"
S1 "BACKUP TABLE default.events TO Disk('backups', 'm1.zip')"
t0=$(python3 -I -c 'import time; print(time.monotonic())')
for i in $(seq 1 40); do [ "$(curl -s 127.0.0.1:31092/metrics | grep -c '^chq_backups_failed{database="ch1"} 1')" = 1 ] && break; sleep 5; done
echo "  query-exporter 更新用了 $(python3 -I -c "import time,sys; print(round(time.monotonic()-float(sys.argv[1])))" "$t0") 秒（主機）"
sleep 10
K q 'chq_backup_last_success_timestamp'
K q 'chq_backups_failed'
K q 'time() - max(chq_backup_last_success_timestamp{job="clickhouse-sql"})'
K alerts '^CHBackup'

H "N Keeper 失去 quorum 時 SQL 路徑與 ZooKeeperSession* 的變化（keeper_connection 查詢已從設定拿掉）"
snap() {
  echo "  -- +$(python3 -I -c "import time,sys; print(round(time.monotonic()-float(sys.argv[1])))" "$t0") 秒（主機）"
  K q 'ClickHouseMetrics_ZooKeeperSession{job="clickhouse-server"}'
  K q 'ClickHouseMetrics_ZooKeeperSessionExpired{job="clickhouse-server"}'
  K q 'ClickHouseMetrics_ReadonlyReplica{job="clickhouse-server"}'
  K q 'chq_replica_readonly{table="default.events"}'
  K q 'time() - query_timestamp{job="clickhouse-sql", query="replicas"}'
  K q 'sum by (database, status) (queries_total{job="clickhouse-sql", query="replicas"})'
}
L=""; for k in keeper1 keeper2 keeper3; do p=$(case $k in keeper1) echo 31021;; keeper2) echo 31022;; keeper3) echo 31023;; esac)
  K tcp $p mntr | grep -q 'zk_server_state	leader' && L=$k; done
STOP=$L; for k in keeper1 keeper2 keeper3; do [ "$k" != "$L" ] && { STOP="$STOP $k"; break; }; done
echo "  停止：$STOP"
docker compose stop $STOP >/dev/null 2>&1; t0=$(python3 -I -c 'import time; print(time.monotonic())')
sleep 30; snap
sleep 40; snap
docker compose start $STOP >/dev/null 2>&1; t0=$(python3 -I -c 'import time; print(time.monotonic())')
echo "  Keeper 啟動"
sleep 30; snap
sleep 60; snap
sleep 60; snap
K alerts '^CH(Keeper|Sql|Replica)'

echo "  query-exporter log 裡被跳過的執行：$(docker compose logs qe 2>&1 | grep -c 'was missed')"
K q 'chq_merge_tree_setting'

H "結束"
docker compose down -v >/dev/null 2>&1
docker ps -a --format '{{.Names}}' | grep at-ch || echo "  沒有 at-ch 容器"
docker volume ls --format '{{.Name}}' | grep at-ch || echo "  沒有 at-ch volume"
