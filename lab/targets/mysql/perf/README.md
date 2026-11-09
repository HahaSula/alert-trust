# MySQL／MariaDB 效能與故障情境

MariaDB 11.4.13 與 MySQL 8.4.7 各一台，同時跑同一組情境，觀察吞吐量（QPS／TPS）掉下來時哪些指標先動、mysqld_exporter v0.21.0 看得到什麼、看不到什麼，以及哪條規則會響。

## 怎麼跑

```bash
./run.sh      # 約 25 分鐘；結束時自動 docker compose down -v
```

- compose project `at-myperf`，只開 `127.0.0.1:22690`（Prometheus）。
- 每台資料庫 `cpus=2`、`mem_limit=1g`（不給 swap）、buffer pool 128M、`max_connections=100`、`innodb_lock_wait_timeout=10`、`long_query_time=0.5`；MariaDB 另外開 `performance_schema=ON`（預設 OFF）。
- 工作負載 `workload.py`（python:3.12.7-alpine + PyMySQL 1.1.1）：每台 8 個 thread，每個每秒最多 40 筆交易（點查 + 單列 UPDATE + COMMIT），落後時不補送，所以是「限速」負載，不是壓力測試。每秒印一行 JSON（成功數、錯誤碼、p99）。
- exporter 額外開 `info_schema.processlist`、`info_schema.innodb_trx`、`info_schema.innodb_metrics`、`perf_schema.eventsstatements`、`perf_schema.eventsstatementssum`、`perf_schema.eventswaits`。
- Prometheus v3.13.5 每 5 秒抓一次，載入 `rules/targets/mysql.yml`。lab 沒有 `cluster` label，所以 `MySQLMultipleWritable` 會把兩台獨立的資料庫算成同一個叢集而響，這是 lab 的假象。

## 情境

| 代號 | 做什麼 |
|---|---|
| s0 | 基線 |
| s1 | 兩個 client 反覆跑 filesort 全表掃描與 `SLEEP(1)`；再送 300 種不同形狀的查詢（每種一個 digest） |
| s2 | 一個交易 `UPDATE ... WHERE id <= 50000` 後不 commit，持有 25% 的列 35 秒 |
| s3 | 一個交易讀一列後不 commit，3 秒後 `ALTER TABLE ... ADD COLUMN`，ALTER 等 metadata lock，後面所有查詢一起排隊 |
| s4 | `START TRANSACTION WITH CONSISTENT SNAPSHOT` 開著約 60 秒，擋住 purge |
| s5 | `docker update --cpus 0.1`，45 秒後改回 2 |
| s6 | buffer pool 128M → 16M（表約 44／102 MB），60 秒後改回 |
| s7 | 兩個 client 用 `tmp_table_size=64K` 反覆 GROUP BY |
| s8a／s8b | 對 6 萬列的表連續三次全表 UPDATE；8a 用預設 redo，8b 先把 redo 縮到最小（MariaDB `innodb_log_file_size=16M`、MySQL `innodb_redo_log_capacity=8M`） |
| s9 | 150 條連線反覆「連線 → 全表掃描 → 斷線」30 秒（max_connections=100） |
| s10 | 一張 `DATA DIRECTORY` 在 24 MB tmpfs 的表一直寫到滿 |
| s11 | `docker update --memory 400m`，buffer pool 加到 1G，再把一張大表倍增到被 OOM kill；`restart: on-failure` 讓它自己起來 |

## 結果檔

- `results/run-YYYYMMDD.md`：每個情境中的 Prometheus 查詢快照、`sys.statement_analysis`、`performance_schema.metadata_locks`、error log 摘錄、ALERTS。
- `results/summary-YYYYMMDD.md`：`analyze.py` 依時間窗算出的客戶端 TPS、錯誤，以及每個指標的基線、極值、「先動」秒數與缺樣本數。
- `results/raw/`：`run-<db>.log`（工作負載逐秒紀錄）、`slow-*.log` 等其他模式的輸出、`windows.tsv`（情境時間窗）、`series.csv`（Prometheus 時間序列，step 5s）。

## 注意

- 所有時間都是 host 的 wall clock。這台機器（WSL2）上容器內的單調時鐘比 wall clock 慢約 30%，所以 SQL 裡的 `SLEEP(35)` 在 wall clock 上比 35 秒長；情境長度以 `windows.tsv` 為準。
- 「先動」的解析度是 scrape 間隔 5 秒加上 `rate(...[20s])` 的平滑，差 5 秒以內的先後不要過度解讀。
- 磁碟滿只模擬「單一 tablespace 所在的檔案系統滿」，redo、binlog、資料目錄本身沒有放在小檔案系統上。
- OOM 情境改變的是容器的記憶體上限，不是資料庫設定；重啟後 buffer pool 回到啟動參數的 128M。

## 執行紀錄

| 執行 | 指令 | 時間（wall） | 結果 |
|---|---|---|---|
| 1 | `./run.sh` | 07:52–08:13，約 21 分鐘 | `results/run-20261109.md`、`results/summary-20261109.md`、`results/raw/` |
| 2 | `./run.sh focus` | 08:14–08:21，約 7 分鐘 | `results/run-focus-20261109.md`、`results/summary-focus-20261109.md`、`results/raw-focus/` |

第 1 次的 s11（OOM）不可用：偵測寫錯（看 RestartCount），而且 MariaDB 因為 s10 的 tmpfs 在重啟後是空的而 crash loop、MySQL 的上限 400m 比它啟動所需還低而 OOM loop。當時手動抓的容器事件存在 `results/raw/events-main-20261109.txt`。之後在 s10 結束時刪掉那張表、OOM 上限改成「目前用量 + 150 MB」、改記錄 `docker events`，用第 2 次（focus）重做 s3 與 s11。summary 裡的「先動」以 s0 為基線；s0 本身有工作負載剛起來的波動，數字要搭配 run-*.md 的快照看。

## 結果摘要

數字前面是 MariaDB、後面是 MySQL；TPS 是客戶端成功交易數 ÷ 時間窗長度（基線約 273–275）。

| 情境 | 吞吐量 | 先動的指標 | 備註 |
|---|---|---|---|
| s1 慢查詢 | 不變 | `slow_queries` 30 秒 +36／+38 | 300 種 digest → 固定 250 個、4,771 條 series；quantile label 是 `"99.0"`；MariaDB quantile、lock_time 永遠 0 |
| s2 row lock | 98／90 tx/s | `row_lock_current_waits` 8、`threads_running` 11（9 秒）→ commit 0.7／1.1/s | 1205 錯誤 24／16 次；current_waits 上限是連線數 8 |
| s3 metadata lock（focus） | 115／114 tx/s，最長停頓 42 秒 | `waiting_for_table_metadata_lock` 9、`threads_running` 12／13、commit 0（10–15 秒） | row lock 0；MySQL `metadata_locks` 9 PENDING、`sys.schema_table_lock_waits` 給出 KILL；MariaDB 查不到 |
| s4 purge | 不變 | history list 50 秒 13,663／13,474 | 40 秒內回到 152／156 |
| s5 CPU 0.1 | 155／112 tx/s | `threads_running` 10／11、scrape 耗時 0.97／0.56 秒 → commit 73／45/s | p99 0.5／3.6 秒 |
| s6 buffer pool 16M | 245／243 tx/s | `innodb_buffer_pool_reads` 1,946／1,437/s | `wait_free` 只有 MySQL 動 |
| s7 暫存表 | 不變 | `created_tmp_disk_tables` 6.5／9.5/s（44%／84%） | MySQL TempTable 溢出也計入 |
| s8 redo 太小 | 不變（未重現） | `innodb_log_waits` 一直 0 | checkpoint age 預設 77／66 MB、縮小後 ≤ 2／6 MB |
| s9 連線風暴 | 116／108 tx/s | `mysql_up` 0（10 秒） | 事後 `connection_errors_total{error="max_connections"}` +1,840／+4,040；`aborted_connects` +2,169／+298 |
| s10 磁碟滿 | 不變 | — | 立刻 `ERROR 1114`，其他表正常；重啟後 MariaDB 因 tablespace 遺失 crash loop |
| s11 OOM（focus） | 斷線 4–5 秒 | `mysql_up` 只有一次 0，下一次 uptime 7–8 | 加大 buffer pool 的當下就被 kill；recovery 3 秒；`MySQLNotReachable` 不會響 |
