# PostgreSQL 自訂查詢（query-exporter 5.2.2）

回答一個問題：`queries/query-exporter/postgres.yaml` 補上的 postgres_exporter 盲點，metric 真的會出現、而且會隨資料庫狀態改變嗎？結果用在 [PostgreSQL 本體怎麼監控](../../../../docs/targets/postgres.md#custom-queries) 的「用自訂查詢補上」一節，以及元件卡 `blind_spots` 的 `covered_by`。

## 環境

| 元件 | 版本／設定 |
|---|---|
| 主庫 `p` | `postgres:17.11-alpine`，`--data-checksums`，`max_prepared_transactions=5`；`init.sh` 用 ALTER SYSTEM 設 `archive_mode=on`、`archive_command=/archive.sh`、`synchronous_standby_names='FIRST 1 (s1)'` |
| 複本 `s` | `postgres:17.11-alpine`，`pg_basebackup` 用 physical slot `s1slot`，`application_name=s1`、`hot_standby_feedback=on` |
| archive 替身 | `archive.sh`：`/tmp/archive_fail` 存在時回 1，否則回 0（不保存檔案） |
| query-exporter | `adonato/query-exporter:5.2.2`，帳號 `qe_monitor` 只有 `pg_monitor`；三個 DSN：`primary`（p/postgres）、`primary_app`（p/app）、`standby`（s/postgres）；只開 `127.0.0.1:23901` |

設定檔就是 repo 的 `queries/query-exporter/postgres.yaml`，`run.sh` 只用 sed 把 `interval` 全改成 5s、`expiration` 全改成 20s（寫到 `.config.yaml`，結束時刪除）。沒有 Prometheus：腳本直接 curl `/metrics`。

這台機器的 Docker 容器時鐘不可靠，結果只比較數值與 series 有沒有出現，不解讀秒數。

compose project 是 `at-pgq`，`./run.sh` 約 6 分鐘，結束時 `docker compose down -v` 並列出殘留容器（應為空）。

## 情境

| 代號 | 做法 | 看什麼 |
|---|---|---|
| A | 基線 | 所有 `pgq_*` 與內建 metric 的名稱（counter 加 `_total`） |
| B | 寫入 15 批，寫入中與停止後各看一次 | `write_lag` 等在閒置後變 NULL，metric 變 0 |
| C | 主庫持有 XID 的交易、`PREPARE TRANSACTION`、複本上的 repeatable read snapshot，再消耗 3000 個 XID | 五種 XID age 與 prepared 數 |
| D | terminate C 的 session | `sessions_killed` |
| E | archive 失敗＋`pg_switch_wal()` 三次；再把 `archive_command` 清空 | `pgq_archive_failing`、`.ready` 數、`pgq_archive_command_set` |
| F | 在 app 資料庫做失敗的 `CREATE UNIQUE INDEX CONCURRENTLY` 與 UPDATE | 只有 `primary_app` 的 series 變動 |
| G | checkpoint、`pg_buffercache_evict()`、用 dd 改壞一頁再讀 | `pgq_checksum_failures_total` |
| H | `REVOKE pg_monitor` | 查詢失敗時 metric 保留舊值；`queries_total{status="error"}`、`query_timestamp` |
| I | 停掉複本 | 同步 standby 數 < 要求數；每台 standby 的 series 在 expiration 後消失 |
| J | — | `pg_stat_wal`、`pg_stat_io` counter |
| K | 啟動複本後 `pg_promote()`、checkpoint | `pgq_checkpoint_timeline_id`、`pgq_in_recovery` |

結果摘要在 `results/summary.txt`，原始輸出在 `results/run-20261109.txt`。
