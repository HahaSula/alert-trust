# PostgreSQL 效能與故障情境

回答三個問題：今天怎麼找慢 SQL、鎖擋住時 exporter 看到什麼、哪些內部因素會讓 TPS 掉下來或讓 PostgreSQL 當掉，以及每種情況哪個 metric 先動、哪條規則接得到。

## 環境

| 元件 | 版本／設定 |
|---|---|
| PostgreSQL | `postgres:16.15-alpine`，`cpus: 1.0`、`mem_limit: 768m`，`shared_preload_libraries=pg_stat_statements`、`max_connections=40`、`track_io_timing=on`、`log_lock_waits=on`、`deadlock_timeout=1s`、`log_min_duration_statement=500ms` |
| 負載 | 映像內的 `pgbench`，scale 10（`pgbench_accounts` 100 萬列，約 130MB），8 個 client、`-R 200` 限速 |
| exporter | `postgres-exporter:v0.20.1` 兩個：`e-plain`（帳號沒設 lock_timeout）、`e-lt`（`ALTER ROLE exporter_lt SET lock_timeout='1s'`，並打開 `--collector.stat_statements.include_query`）；兩個都打開 `stat_statements`、`long_running_transactions`、`postmaster` |
| Prometheus | `prom/prometheus:v3.13.5`，`scrape_interval` 5s、`scrape_timeout` 4s，只開 `127.0.0.1:23690` |

compose project 是 `at-pgperf-lab`。`./run.sh` 跑 S0–S9（約 25 分鐘牆鐘），`./run-s10.sh` 另起一套環境跑 S10（約 4 分鐘）；兩個都在結束時 `docker compose down -v`。

## 情境

| 代號 | 做法 |
|---|---|
| S0 | 基線：pgbench 60 秒；再跑 20 秒不限速看容量 |
| S1 | 慢 SQL：背景 pgbench，加上每秒一次全表掃描、一個 `pg_sleep(40)` |
| S2 | 列鎖：一個交易 `UPDATE pgbench_branches WHERE bid=1` 後不 commit，持有 30 秒 |
| S3 | DDL 鎖佇列：A 開交易讀 `pgbench_accounts` 不結束，B 執行 `ALTER TABLE pgbench_accounts ADD COLUMN`（沒設 lock_timeout） |
| S3b | 同 S3，B 先 `SET lock_timeout='2s'` |
| S4 | CPU：pgbench 中途 `docker update --cpus 0.1`，40 秒後改回 1.0 |
| S5 | work_mem：DISTINCT 10 萬列的查詢，`work_mem` 64MB 與 64kB 各 30 秒 |
| S6 | shared_buffers：`ALTER SYSTEM SET shared_buffers='16MB'` 後重啟，同樣 pgbench，再跑 20 秒不限速 |
| S7 | checkpoint：`max_wal_size=32MB`（reload）後 pgbench 60 秒 |
| S8 | idle in transaction：一個 session 拿到 XID 後閒置 100 秒，`autovacuum_naptime=5s`，pgbench 90 秒 |
| S9 | 連線風暴：pgbench 已連 8 條，另開 36 條 `pg_sleep(40)`，再跑每筆交易重新連線的 `pgbench -C` |
| S10 | OOM：`docker update --memory 400m`，pgbench 中一個 backend 用 `array_agg` 吃記憶體 |

pg_wal 寫滿已在 [../target](../target) 的 O 段做過（PANIC 後停機），這裡不重做。

## 時間

- `t+秒` 都是主機牆鐘，以每個情境開始時為 0。
- 這台機器上的容器 monotonic clock 有時比牆鐘慢約 30%（其他 lab 量到過）。pgbench 的 `-T`、`-P`、`-R` 用容器時鐘，所以 pgbench 進度檔裡的秒數只當參考，時間點以主機牆鐘與 Prometheus 為準。本次沒有出現明顯落差：容器內 `sleep 10` 是 10.2 秒牆鐘，pgbench `-T 60` 約 65 秒牆鐘。
- 每個情境大約每 30 秒有一次 TPS 短暫掉到 110–150（基線也有），推測是主機磁碟抖動（同時段 log 有 `END` 花 6 秒），不是情境造成的。

## 檔案

| 檔案 | 內容 |
|---|---|
| `compose.yml`、`prometheus.yml`、`init.sh`、`sql/` | 環境與 pgbench 自訂腳本 |
| `run.sh`、`run-s10.sh` | S0–S9、S10；`fmt_instant.py`、`fmt_range.py` 印 Prometheus 查詢結果 |
| `results/run-<日期>.txt`、`results/run-s10-<日期>.txt` | 主要原始輸出：每個情境的 query_range 時間序列（每 5 秒一格）與直接查詢 |
| `results/pgbench-*.txt` | 每個情境的 pgbench 進度（容器時鐘） |
| `results/pg-log*.txt`、`results/exporter-errors*.txt` | PostgreSQL log、exporter 錯誤（去重） |
| `results/summary.txt` | 摘要 |

第一次執行時 `run.sh` 在 S9 結尾的裸 `wait` 卡住（bash 會連 process substitution 的 `tee` 一起等），手動中止；之後改成只等 psql 的 pid，S10 移到 `run-s10.sh`。`run-20261109.txt` 最後一行記錄了這件事。
