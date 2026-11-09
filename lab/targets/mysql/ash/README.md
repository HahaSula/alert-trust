# MySQL／MariaDB：叢集、資料保護、容量與 ASH 取樣器

補 `catalog/targets/mysqld_exporter.yaml` 的 cluster、protection、capacity、maintenance 面向，以及 [MariaDB 的 ASH 缺口](../../../../docs/targets/mariadb-ash.md)。

## 怎麼跑

```bash
./run.sh       # 全部情境，約 6 分鐘
./run.sh ash   # 只跑 ASH 取樣器（E），約 1 分鐘
```

compose project 是 `at-myasp`，只開 127.0.0.1:22801–22804（四台 mysqld_exporter），結束時 `docker compose down -v`。不起 Prometheus，直接 curl `/metrics`。

| 服務 | 映像 | 用途 |
|---|---|---|
| mdb | mariadb:11.4（11.4.13） | binlog 過期、auto_increment、ASH 取樣器（`performance_schema=ON`、`event_scheduler=ON`） |
| gal1–gal3 | mariadb:11.4＋galera-4 26.4.27 | Galera 三台；gal1 用 `--wsrep-new-cluster` 起叢集，其他兩台用 rsync SST 加入 |
| gr1–gr3 | mysql:8.4.7 | Group Replication single-primary；GR 參數要加 `loose-` 前綴，否則 `--initialize` 時 plugin 還沒載入會失敗 |
| ex-mdb、ex-gr1、ex-gal1、ex-gal2 | prom/mysqld-exporter:v0.21.0 | 各自開需要的 collector（見 `compose.yml`） |

## 情境

| 代號 | 做什麼 |
|---|---|
| A1／A2 | GR 三台 ONLINE；`docker pause` gr2、gr3 後看 gr1 的成員表、exporter、寫入；恢復 |
| B1–B4 | 沒開 Galera 的 MariaDB 輸出哪些 `wsrep_*`；三台正常；gal2 限 0.05 核＋`gcs.fc_limit=1` 嘗試觸發 flow control；暫停兩台看 non-Primary |
| C1／C2 | binlog、保留、`sync_binlog` 的預設值；MariaDB 沒有 replica 時 binlog 會不會依 `binlog_expire_logs_seconds` 刪除 |
| D1 | auto_increment 與 schema 大小；MySQL 的 information_schema 統計快取 |
| E1–E3 | MariaDB 有哪些診斷表；`ash-sampler.sql` 的取樣器在 row lock、metadata lock、重查詢、SLEEP 同時發生時記到什麼；一次取樣的成本 |

## 結果（2026-11-09）

`results/run-20261109.md`（全部）、`results/run-ash-20261109.md`（開 `events_statements_current` 後重跑 E）。

- GR：暫停兩台 24 秒後 gr1 看到 2 台 UNREACHABLE、自己 ONLINE／PRIMARY，`mysql_up` 1；INSERT 卡到 5 秒逾時，恢復後那筆其實 commit 了。
- Galera：沒開 Galera 的 MariaDB 也輸出 `wsrep_cluster_status` 0、`wsrep_cluster_size` 0、`wsrep_ready` 0。暫停兩台 29 秒後 gal1 是 non-Primary（exporter 0）、size 1，SELECT 回 ERROR 1047。
- Flow control **沒有重現**：`flow_control_paused_ns`、`flow_control_sent` 都是 0，限 CPU 與不限 CPU 寫 3000 筆都約 43–44 秒。
- MariaDB 預設 `slave_connections_needed_for_purge=1`：expire 20 秒、等 30 秒再 FLUSH，binlog 從 5 個變 6 個（沒刪）；改成 0 後變 3 個。
- 預設值：MySQL 8.4 `log_bin` 1、`binlog_expire_logs_seconds` 2592000、`sync_binlog` 1；MariaDB 11.4 `sync_binlog` 0。
- MySQL 新表的 auto_increment 在 `ANALYZE TABLE` 之前一直沒出現在 exporter 輸出（`information_schema_stats_expiry` 86400）；MariaDB 馬上是 128／127。
- MariaDB `performance_schema=ON` 時 `events_*_current` consumer 全是 NO；開了以後執行中語句的 digest 仍是 NULL（216 列中 0 列有），MySQL 執行中就有。
- 取樣器把 30 秒分成 row lock 57、MDL 50＋26、重查詢 27 個 active 秒；每次取樣約 3–4 ms（含 client 啟動，是上限）。

這台機器的容器時鐘不準，秒數只當量級；成員狀態、值與有無是可靠的。
