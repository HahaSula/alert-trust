#!/usr/bin/env bash
# 用 general_log 記下 mysqld_exporter v0.21.0 每次 scrape 送出的 SQL。約 2 分鐘，結束時 down -v。
set -euo pipefail
cd "$(dirname "$0")"
OUT=results/general-log-$(date +%Y%m%d).md
docker compose up -d
for svc in mdb my; do
  for i in $(seq 1 60); do
    docker compose exec -T $svc sh -c 'mysql -uroot -prootpass -e "SELECT 1 FROM heartbeat.heartbeat" 2>/dev/null || mariadb -uroot -prootpass -e "SELECT 1 FROM heartbeat.heartbeat" 2>/dev/null' >/dev/null && break
    sleep 2
  done
done
q() { docker compose exec -T "$1" sh -c "command -v mariadb >/dev/null && exec mariadb -uroot -prootpass -N -e \"\$0\" || exec mysql -uroot -prootpass -N -e \"\$0\"" "$2" 2>/dev/null; }
for svc in mdb my; do q $svc "TRUNCATE mysql.general_log"; done
E0=$(q my "SELECT IFNULL(SUM(SUM_ERROR_RAISED),0) FROM performance_schema.events_errors_summary_by_user_by_error WHERE USER='exporter' AND ERROR_NUMBER=1064")
curl -s 127.0.0.1:22501/metrics >/dev/null; curl -s 127.0.0.1:22502/metrics >/dev/null   # 一次完整 scrape
{
  echo "# mysqld_exporter v0.21.0 送出的 SQL（general_log，一次 scrape）"
  echo
  echo "環境：mariadb:11.4、mysql:8.4.7（單機、非 replica），prom/mysqld-exporter:v0.21.0；"
  echo "collector：global_status、global_variables、slave_status、info_schema.innodb_metrics、info_schema.innodb_trx、info_schema.processlist、heartbeat。"
  for svc in mdb my; do
    echo; echo "## $svc：$(q $svc 'SELECT @@version')"; echo; echo '```text'
    q $svc "SELECT command_type, REPLACE(REPLACE(CONVERT(argument USING utf8mb4), '\n', ' '), '\t', ' ') FROM mysql.general_log WHERE user_host LIKE 'exporter%' ORDER BY event_time" | sed -E 's/ +/ /g'
    echo '```'
  done
  echo; echo "## my：general_log 沒記到的語句（performance_schema 依 digest 統計，含失敗的語句）"; echo
  echo "MySQL 的 general_log 不記語法錯誤的語句，所以 slave_status 的退回順序改用 digest 表看。"; echo; echo '```text'
  q my "SELECT COUNT_STAR, SUM_ERRORS, DIGEST_TEXT FROM performance_schema.events_statements_summary_by_digest WHERE DIGEST_TEXT LIKE 'SHOW%' ORDER BY FIRST_SEEN" | sed -E 's/ +/ /g'
  echo '```'
  E1=$(q my "SELECT IFNULL(SUM(SUM_ERROR_RAISED),0) FROM performance_schema.events_errors_summary_by_user_by_error WHERE USER='exporter' AND ERROR_NUMBER=1064")
  echo; echo "exporter 帳號的 ER_PARSE_ERROR（1064）次數（performance_schema.events_errors_summary_by_user_by_error）：scrape 前 $E0，一次 scrape 後 $E1。"
  echo; echo "## 對應的 metric（節錄）"; echo
  for p in 22501 22502; do
    echo "### 127.0.0.1:$p"; echo; echo '```text'
    curl -s 127.0.0.1:$p/metrics | grep -E '^mysql_(up|global_status_(threads_running|innodb_row_lock_current_waits|innodb_history_list_length|innodb_deadlocks|max_used_connections|uptime|wsrep_ready|connection_errors_total\{error="max_connections"\})|global_variables_(read_only|super_read_only|wsrep_on|gtid_mode)|info_schema_innodb_metrics_(transaction_trx_rseg_history_len|lock_lock_deadlocks(_total)?)|heartbeat_[a-z_]+|exporter_collector_success)[ {]' || true
    echo '```'; echo
  done
  echo "## 沒有輸出的字串變數（SHOW GLOBAL VARIABLES 有值，exporter 沒有 metric）"; echo
  for p in 22501 22502; do
    echo "- 127.0.0.1:$p：gtid_executed／gtid_binlog_pos／version 的 mysql_global_variables_* 數量 = $(curl -s 127.0.0.1:$p/metrics | grep -cE '^mysql_global_variables_(gtid_executed|gtid_binlog_pos|gtid_current_pos|version)[ {]' || true)"
  done
} > "$OUT"
docker compose down -v
echo "wrote $OUT"
