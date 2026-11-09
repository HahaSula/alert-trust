# query-exporter 補 mysqld_exporter 盲點

驗證 [`queries/query-exporter/mysql.yaml`](../../../../queries/query-exporter/mysql.yaml)（query-exporter 5.2.2）輸出的 metric 會出現、會在情境中改變，以及 `rules/targets/mysql.yml` 的 `mysql-custom-queries` 群組會進入 pending／firing。說明在 [docs/targets/mysql.md#custom-queries](../../../../docs/targets/mysql.md)。

## 組成

| 服務 | 映像 | 說明 |
|---|---|---|
| my1 → my2 | `mysql:8.4.7` | GTID replication；`--admin-address=<自己>`、`--admin-port=33062`；my1 `max_connections=10` |
| mdb1 → mdb2 | `mariadb:11.4`（11.4.13） | GTID replication、`log_slave_updates=ON`、`--extra-port=3307 --extra-max-connections=3`；mdb1 `max_connections=10`、`performance_schema=ON`、裝 [ASH 取樣器](../ash/ash-sampler.sql) |
| qe | `adonato/query-exporter:5.2.2` | 原樣掛載 repo 的設定檔；DSN 指到 admin port／extra_port |
| ex-my1、ex-mdb1 | `prom/mysqld-exporter:v0.21.0` | 走一般 port，對照 |
| prom | `prom/prometheus:v3.13.5` | scrape／evaluation 5s，載入 `rules/targets/mysql.yml` |

Compose project `at-myq`，只開 `127.0.0.1:22960`（query-exporter）與 `127.0.0.1:22990`（Prometheus）。

## 執行

```bash
./run.sh          # 約 14 分鐘；結果寫到 results/run-YYYYMMDD.md，結束時 docker compose down -v
./run.sh ash      # 只跑 s0 與 s5（約 4 分鐘），設定檔刪掉 interval、改成每次 scrape 執行；結果寫到 results/run-YYYYMMDD-ash.md
```

## 情境

| 情境 | 做法 | 看什麼 |
|---|---|---|
| s0 基線 | — | 每個查詢都成功；自訂 series 數 |
| s1 連線打滿 | my1、mdb1 各開 15 條 `SELECT SLEEP(75)` | 一般 port 新連線被拒、admin port／extra_port 可以；`mysql_up` 與 `mysql_q_threads_connected` |
| s2 metadata lock | 閒置交易讀 `app.t` → `ALTER TABLE` → `SELECT` | `mysql_q_mdl_*`、`MySQLMetadataLockBlocker` |
| s3 replica 錯誤碼 | replica 先有 id=100，primary 再寫 → 1062 | `mysql_q_replica_last_error_timestamp_seconds{errno,error_name}`；修好後 expiration 讓 series 消失 |
| s4 errant GTID | read_only 的 replica 上直接寫一筆 | `mysql_q_gtid_executed_transactions`、`mysql_q_gtid_binlog_state_seq`；`MySQLErrantGtid`、`MariaDBErrantGtid` |
| s5 MariaDB ASH | 一個交易持有 row lock，3 個 UPDATE 等它；之後停用取樣 EVENT | `mysql_q_ash_active_sessions{wait_class="lock_row"}`、`MariaDBAshSamplerStopped` |
| s6 資料庫停機 | 停掉 mdb2 | 自訂 metric 在 expiration 後消失、`database_errors_total` 增加 |

## 結果

- `results/run-20261109.md`：本輪（修正後的設定與規則；s5 的 ASH metric 沒有動，原因見下一項）。
- `results/run-20261109-ash-interval.md`：只重做 ASH、設定原樣。整段約 3 分鐘內 30 秒 interval 的查詢只成功 2 次、被 APScheduler 跳過 3 次（log 的 `was missed by`），15 秒的成功 5 次、跳過 4 次；`mysql_q_ash_active_sessions` 一直停在開頭的值。APScheduler 3.11 的 `misfire_grace_time` 預設 1 秒，這台機器容器的單調時鐘比牆鐘慢，每次排程都晚到。
- `results/run-20261109-ash.md`：同上，但刪掉 interval（每次 scrape 執行）。`mysql_q_ash_active_sessions{wait_class="lock_row"}` 1.75，和同一時間直接查 `ash.aas_60s` 相同；停用 EVENT 後 `mysql_q_ash_sampler_enabled` 0、`MariaDBAshSamplerStopped` pending；每個查詢成功 23 次、沒有跳過。
- `results/run-20261109-1.md`：第一輪，保留當作兩個發現的依據：
    1. MariaDB `extra_max_connections` 預設 1，query-exporter 的常駐連線佔掉之後，同一個 port 的第二條連線（CLI 測試）也被拒（`ERROR 1040`）。query-exporter 在 timeout 時丟掉連線並重連（讀碼），舊連線在伺服器端還沒釋放時重連會被拒（推論，未單獨重現），所以設定檔建議至少 2。
    2. 第一版 `MySQLCustomQueryStale` 用 `query_timestamp`，但查詢回 0 列時 query-exporter 不更新它（原始碼 `executor.py` L205），正常情況下沒有錯誤的 `mysql_replica_errors` 也被判成停滯；規則改用 `queries_total{status="success"}` 的增加量。第一輪 s6 也用錯了 counter 名稱（輸出時是 `queries_total`、`database_errors_total`）。

這台機器的 Docker 容器時鐘不穩，只看數值與告警狀態，不看秒數長短。
