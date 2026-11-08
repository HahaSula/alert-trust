#!/usr/bin/env bash
# 用 log_statement=all 記下 postgres_exporter v0.20.1 兩次 scrape 送出的 SQL。約 1 分鐘，結束時 down -v。
set -euo pipefail
cd "$(dirname "$0")"
OUT=results/statements-$(date +%Y%m%d).md
docker compose up -d
for svc in p16 p17; do
  for i in $(seq 1 60); do
    docker compose exec -T $svc psql -U exporter -d postgres -Atc 'select 1' >/dev/null 2>&1 && break
    sleep 2
  done
done
sleep 3
# 只取 scrape 期間的 log：記下目前行數
declare -A N0
for svc in p16 p17; do N0[$svc]=$(docker compose logs --no-log-prefix $svc 2>&1 | wc -l); done
for p in 23501 23502; do curl -s 127.0.0.1:$p/metrics > /tmp/at-pgtrace-$p.1; done   # 第一次 scrape
sleep 2
for p in 23501 23502; do curl -s 127.0.0.1:$p/metrics > /tmp/at-pgtrace-$p.2; done   # 第二次 scrape
sleep 2
{
  echo "# postgres_exporter v0.20.1 送出的 SQL（log_statement=all，連續兩次 scrape）"
  echo
  echo "環境：postgres:16.15-alpine、postgres:17.11-alpine（單機、非 replica），quay.io/prometheuscommunity/postgres-exporter:v0.20.1；"
  echo "帳號 exporter（pg_monitor）；另開 collector：stat_wal_receiver、database_wraparound、postmaster、stat_checkpointer、long_running_transactions。"
  echo "log_line_prefix 是 '%m [%p] %u@%d app=%a '：[pid] 不同代表不同的連線。多行 SQL 已壓成一行。"
  for svc in p16 p17; do
    echo; echo "## $svc：$(docker compose exec -T $svc psql -U postgres -Atc 'select version()')"; echo; echo '```text'
    docker compose logs --no-log-prefix $svc 2>&1 | tail -n +$((N0[$svc]+1)) \
      | awk '/^[0-9]{4}-/{if(l!="")print l; l=$0; next}{gsub(/^[ \t]+/," "); l=l $0}END{if(l!="")print l}' \
      | grep -E 'exporter@|connection (received|authorized)' | grep -v 'connection received' \
      | sed -E 's/^[0-9-]+ ([0-9:.]+) UTC/\1/; s/[ \t]+/ /g'
    echo '```'
  done
  echo; echo "## exporter log（collector 失敗或版本跳過）"; echo; echo '```text'
  docker compose logs --no-log-prefix e-p16 e-p17 2>&1 | grep -E 'level=(WARN|ERROR)' | sed -E 's/^time=[^ ]+ //' | sort | uniq -c || true
  echo '```'
  echo; echo "## 對應的 metric（第二次 scrape，節錄）"
  for p in 23501 23502; do
    echo; echo "### 127.0.0.1:$p"; echo; echo '```text'
    grep -E '^pg_(up|postmaster_start_time_seconds|replication_(is_replica|lag_seconds|last_replay_seconds)|replication_slots_[a-z_]+|stat_archiver_[a-z_]+|stat_bgwriter_checkpoints_req_total|stat_checkpointer_num_requested_total|database_wraparound_age_datfrozenxid_seconds|long_running_transactions[a-z_]*|wal_size_bytes|settings_(max_connections|superuser_reserved_connections|reserved_connections|max_slot_wal_keep_size_bytes)|scrape_collector_success)[ {]|^pg_stat_database_(xact_commit|deadlocks|stats_reset|temp_bytes)\{[^}]*datname="postgres"|^pg_stat_activity_(count|max_tx_duration)\{[^}]*datname="postgres",[^}]*state="active"|^pg_locks_count\{datname="postgres",mode="(accessexclusivelock|sharelock)"' /tmp/at-pgtrace-$p.2 || true
    echo '```'
  done
  echo; echo "## 沒有 datname 的那一列（pg_stat_database 的 datid=0）"; echo
  for p in 23501 23502; do
    echo "- 127.0.0.1:$p：pg_stat_database_xact_commit 的 datid=\"0\" series 數 = $(grep -c '^pg_stat_database_xact_commit{datid="0"' /tmp/at-pgtrace-$p.2 || true)；直接查 pg_stat_database 的列數 = $(docker compose exec -T p$([ $p = 23501 ] && echo 16 || echo 17) psql -U postgres -Atc 'select count(*) from pg_stat_database')、datname 是 NULL 的列數 = $(docker compose exec -T p$([ $p = 23501 ] && echo 16 || echo 17) psql -U postgres -Atc 'select count(*) from pg_stat_database where datname is null')"
  done
} > "$OUT"
rm -f /tmp/at-pgtrace-*
docker compose down -v
echo "wrote $OUT"
