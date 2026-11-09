# PostgreSQL 九個面向補洞

回答三個問題：資料保護（WAL archive）、容量（資料庫與表的大小）、工作量（session 錯誤計數）在 postgres_exporter v0.20.1 的預設 collector 裡看得到什麼、看不到什麼。結果用在 [PostgreSQL 本體怎麼監控](../../../../docs/targets/postgres.md) 的 protection、capacity、workload 三節。

## 環境

| 元件 | 版本／設定 |
|---|---|
| PostgreSQL | `postgres:16.15-alpine` 與 `postgres:17.11-alpine` 各一台，`archive_mode=on`、`archive_command=/archive.sh`、`archive_timeout=30` |
| archive 替身 | `archive.sh`：`/tmp/archive_fail` 存在時回 1，否則回 0（不保存檔案） |
| exporter | `postgres-exporter:v0.20.1`，預設 collector，superuser DSN；只開 `127.0.0.1:23801`（PG16）、`127.0.0.1:23802`（PG17） |

沒有 Prometheus：腳本直接 curl exporter 的 `/metrics`。compose project 是 `at-pgasp`，`./run.sh` 約 3 分鐘，結束時 `docker compose down -v` 並列出殘留容器（應為空）。

## 情境

| 代號 | 做法 |
|---|---|
| A | 基線：exporter 有沒有 `archive_mode`、`archive_timeout`、`pg_stat_archiver_*`、`pg_database_size_bytes`、`sessions_*` |
| B | 寫入約 40MB，比較 `pg_database_size()` 與 exporter 的 `pg_database_size_bytes`、`pg_stat_user_tables_table_size_bytes` |
| C | `pg_switch_wal()`，archive 成功 |
| D | `touch /tmp/archive_fail` 後每 10 秒寫 1.8MB、共 100 秒 |
| E | `pg_terminate_backend()` 一個 session、連一個不存在的資料庫 |

結果摘要在 `results/summary.txt`，原始輸出在 `results/run-20261109.txt`。
