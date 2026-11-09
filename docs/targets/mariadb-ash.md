# MariaDB 的 ASH 缺口與取樣器建議

ASH（Active Session History）是「每秒記下每個在做事的 session 正在跑哪個語句、在等什麼」，事後可以回答「昨天 14:03 那 40 秒卡在哪裡」。這頁回答三個問題：MariaDB 11.4 有哪些相近的工具、跟 MySQL 8.4 比少了什麼、怎麼用一個輕量取樣器補上，並接到 alert-trust 的訊號。

依據是 MariaDB 11.4.13、MySQL 8.4.7、mysqld_exporter v0.21.0 的原始碼，加上一組小實驗（[`lab/targets/mysql/ash/`](https://github.com/HahaSula/alert-trust/tree/main/lab/targets/mysql/ash)）。<span class="at-chip at-ev-tested">實測</span>[^my-asp-exp][^my-asp-exp-ash]

## 先講結論

1. **MariaDB 和 MySQL 都沒有內建的 ASH。**兩邊都有 performance_schema 的 `events_*_current`／`_history`／`_history_long` 與 sys schema，但那是「每個 thread 最近 N 筆事件」和「累計值」，不是依時間取樣的 active session 紀錄；Oracle ASH、PostgreSQL 的 pg_wait_sampling、pgsentinel 這類每秒取樣的歷史表，兩邊都沒有。<span class="at-chip at-ev-code">讀碼</span>
2. **MariaDB 比 MySQL 少的是「事後分析」的料。**digest 摘要表沒有 `QUANTILE_95/99/999`、`SUM_CPU_TIME`、`QUERY_SAMPLE_*`，沒有 `events_statements_histogram_by_digest`、`events_errors_summary_*`、`data_locks`／`data_lock_waits`。實驗中查 `information_schema` 確認這幾張表與欄位在 MariaDB 11.4 不存在、在 MySQL 8.4 存在。<span class="at-chip at-ev-tested">實測</span>[^my-asp-exp][^my-asp-my-esms-quantile][^my-asp-my-esms-cpu][^my-asp-my-esmh]exporter 對 MariaDB 輸出的 quantile、`lock_time`、`cpu_time` 一直是 0，原因在[主頁](mysql.md#perf-slow-sql)。[^my-perf-ex-stmt-8028]
3. **MariaDB 的 performance_schema 預設全關，開了以後 `events_*_current` 的 consumer 也全關。**實驗中 `performance_schema=ON` 的 MariaDB，`setup_consumers` 只有 `global_instrumentation`、`thread_instrumentation`、`statements_digest` 是 YES，`events_statements_current`、`events_waits_current` 都是 NO。<span class="at-chip at-ev-tested">實測</span>[^my-asp-exp][^my-perf-mdb-pfs-default]
4. **MariaDB 執行中的語句沒有 digest。**MariaDB 只在語句結束時把 digest 複製進 `events_statements_current`；MySQL 在 parse 完就複製。實驗：開 consumer 後 MariaDB 取樣 216 列、有 digest 的 0 列；MySQL 執行中的 `SELECT SLEEP(8)` 已有 `SELECT SLEEP (?)`。所以 MariaDB 的取樣器不能直接用 digest 把執行中的語句分組。<span class="at-chip at-ev-tested">實測</span>[^my-asp-mdb-pfs-digest-at-end][^my-asp-my-pfs-digest-at-parse][^my-asp-exp-ash]
5. **一個 SQL 寫的取樣器就夠用。**每秒一次的 EVENT 把 `PROCESSLIST`、`performance_schema.threads`、`events_statements_current`、`events_waits_current`、`INNODB_TRX` join 起來存一列。實驗中 row lock、metadata lock、重查詢、`SLEEP` 同時發生的 30 秒內，它正確分出「等 row lock 57 秒、等 MDL 50＋26 秒、重查詢 27 秒」；同一時間 exporter 的 processlist collector 只看得到 `updating` 3、`waiting_for_table_metadata_lock` 3。一次取樣約 3–4 ms（含 client 啟動，是上限）。<span class="at-chip at-ev-tested">實測</span>[^my-asp-exp-ash]

## MariaDB 11.4 有什麼

| 工具 | 看得到 | 有歷史嗎 | 預設 | 證據 |
|---|---|---|---|---|
| `information_schema.PROCESSLIST` | 每個連線的 command、state、`INFO`；MariaDB 另有 `TIME_MS`、`STAGE`／`MAX_STAGE`／`PROGRESS`、`MEMORY_USED`、`EXAMINED_ROWS`、`SENT_ROWS`、`QUERY_ID` | 沒有，只有當下 | 開 | 讀碼[^my-asp-mdb-pl-time-ms][^my-asp-mdb-pl-progress][^my-asp-mdb-pl-examined] |
| `performance_schema.processlist` | 和上面相近的資訊，從 performance_schema 讀 | 沒有 | 要開 performance_schema | 讀碼[^my-asp-mdb-pfs-processlist] |
| `events_statements_current`／`_history`／`_history_long` | 每個 thread 目前與最近 N 筆語句（`SQL_TEXT`、耗時、`ROWS_EXAMINED`；digest 要語句結束才有） | 每個 thread 最近幾筆、全域最近 N 筆（`*_history_size`、`*_history_long_size`），滿了就覆蓋 | 要開 performance_schema，consumer 預設 NO | 實測[^my-asp-exp]、讀碼[^my-asp-mdb-pfs-digest-at-end] |
| `events_waits_current`／`_history` | 每個 thread 目前在等的事件（`wait/lock/metadata/sql/mdl`、`wait/io/table/sql/handler`…） | 同上 | consumer 預設 NO，多數 instrument 預設關 | 實測[^my-asp-exp-ash] |
| `events_statements_summary_by_digest` | 每個 digest 的次數、總耗時、`SUM_LOCK_TIME`、rows、暫存表、全表掃描 | 累計值，沒有時間維度 | 要開 performance_schema | 讀碼[^my-asp-mdb-esms-locktime] |
| sys schema | `sys.statement_analysis`、`sys.processlist`、`sys.schema_table_lock_waits` 等，資料來自 performance_schema | 同 performance_schema | 10.6 起內建；實驗中 `sys` 存在 | 實測[^my-asp-exp]、讀碼[^my-asp-mdb-sys-statement-analysis] |
| `SHOW EXPLAIN FOR <id>`、`SHOW ANALYZE FOR <id>` | 執行中查詢的計畫；ANALYZE 另有到目前為止的 `r_rows`、`r_filtered` | 沒有 | 開 | 實測：對執行中的三表 BNL join 拿到計畫與 `r_rows` 2459、9995[^my-asp-exp-ash]、讀碼[^my-asp-mdb-show-explain][^my-asp-mdb-show-analyze] |
| slow log ＋ `log_slow_verbosity` | 慢語句原文；`query_plan`、`innodb`、`explain`、`engine`、`warnings` 等擴充欄位 | 有（檔案或表），只有超過門檻的語句 | slow log 關 | 讀碼[^my-asp-mdb-slow-verbosity] |
| `optimizer_trace` | 單一 session 的最佳化過程 | 只有最後一筆 | 關 | 讀碼[^my-asp-mdb-optimizer-trace] |
| userstat（`USER_STATISTICS` 等） | 每個帳號、client、表、索引的累計 `BUSY_TIME`、`CPU_TIME`、rows | 累計值 | `userstat=OFF` | 讀碼[^my-asp-mdb-userstat-var][^my-asp-mdb-userstat-cpu]；exporter 有 `info_schema.userstats`（預設關）[^my-asp-ex-userstats-cpu][^my-asp-ex-def-userstats] |

## 和 MySQL 8.4 比少了什麼

| MySQL 8.4 有、MariaDB 11.4 沒有 | 影響 | 證據 |
|---|---|---|
| digest 摘要的 `QUANTILE_95/99/999` 與 `events_statements_histogram_by_digest` | 只能算平均耗時，看不出長尾 | 實測[^my-asp-exp]、讀碼[^my-asp-my-esms-quantile][^my-asp-my-esmh] |
| digest 摘要的 `SUM_CPU_TIME` | 分不出「慢是因為吃 CPU 還是在等」 | 實測、讀碼[^my-asp-my-esms-cpu] |
| `QUERY_SAMPLE_TEXT`、`QUERY_SAMPLE_TIMER_WAIT` | digest 沒有帶參數的範例，要另外從 slow log 或 history 找 | 實測、讀碼[^my-asp-my-esms-sample] |
| `events_errors_summary_*` | 錯誤碼分布要從 slow log 或應用端看 | 實測[^my-asp-exp] |
| `data_locks`、`data_lock_waits`（sys.innodb_lock_waits 的來源） | 「誰擋住誰」要用 `INNODB_TRX` 與 `INNODB_LOCK_WAITS` 拼（後者未實測） | 實測[^my-asp-exp] |
| 執行中語句的 digest | 取樣器不能直接依 digest 分組（見下一節的補法） | 實測、讀碼[^my-asp-mdb-pfs-digest-at-end][^my-asp-my-pfs-digest-at-parse] |

MariaDB 有、MySQL 沒有的：`SHOW ANALYZE FOR`（MySQL 有 `EXPLAIN FOR CONNECTION`，沒有對執行中查詢的 ANALYZE；未實測）、`PROCESSLIST` 的 `PROGRESS`、`MEMORY_USED`、`EXAMINED_ROWS`、userstat 的每帳號 `CPU_TIME`。

## 實驗

MariaDB 11.4（`performance_schema=ON`、`event_scheduler=ON`），開 `events_statements_current`、`events_waits_current` 兩個 consumer 與 MDL、table I/O、InnoDB file I/O 的 instrument，裝上取樣器後同時做四件事：一個交易持有 `app.hot` 的一列 20 秒、三個 UPDATE 等它；一個交易讀 `app.d` 後持有 15 秒、一個 `ALTER TABLE app.d` 和兩個 SELECT 排在後面；一個三表 BNL join；兩個 `SLEEP(15)`、一個 `SLEEP(20)`。<span class="at-chip at-ev-tested">實測</span>[^my-asp-exp-ash]

| 取樣結果（30 次、212 列、平均 7.07 個 active session） | active 秒數 |
|---|---|
| `Updating`、`trx_state=LOCK WAIT`、`wait/io/table/sql/handler`：`UPDATE app.hot ...` | 57 |
| `Waiting for table metadata lock`、`wait/lock/metadata/sql/mdl`：`SELECT * FROM app.d ...` | 50 |
| `Sending data`、`wait/io/table/sql/handler`：三表 join | 27 |
| `Waiting for table metadata lock`：`ALTER TABLE app.d ...` | 26 |
| `User sleep`、`idle`：`SLEEP(...)` | 20＋15＋15 |

- **等 row lock 要靠 `INNODB_TRX` 才分得出來。**PROCESSLIST 的 state 是 `Updating`，等待事件是 `wait/io/table/sql/handler`（和正常讀寫一樣），只有 `INNODB_TRX.trx_state = 'LOCK WAIT'` 說明它在等鎖。exporter 的 processlist collector 在同一時間是 `state="updating"` 3，看不出是等鎖。
- **digest 全部是 NULL**：216 列中有等待事件的 216 列、有 digest 的 0 列。同一個查詢在 MySQL 8.4 執行中就有 digest。原因是 MariaDB 只在 `pfs_end_statement_v1` 複製 digest。[^my-asp-mdb-pfs-digest-at-end]第一次執行時只開了 `events_waits_current`，digest 也是 0；兩次結果一致。[^my-asp-exp]
- **成本**：閒置時一次 `CALL ash.take_sample()` 約 2.9–3.0 ms，20 個 session 在跑時約 3.9–4.1 ms；這是 200 次呼叫（分 4 次 client 啟動）的平均，含 client 啟動，所以是上限。每秒一次約佔一個核心的 0.4%。這台機器的容器時鐘不準，數字只當量級。<span class="at-chip at-ev-tested">實測</span>[^my-asp-exp][^my-asp-exp-ash]

## 建議：輕量 ASH 取樣器 {#sampler}

```mermaid
flowchart LR
  subgraph DB[MariaDB]
    PL["information_schema.PROCESSLIST"]
    TH["performance_schema.threads"]
    SC["events_statements_current<br/>（digest 要等語句結束）"]
    WC["events_waits_current"]
    TRX["information_schema.INNODB_TRX"]
    EV["EVENT 每秒一次<br/>ash.take_sample()"]
    S[("ash.sample<br/>保留 1 小時")]
    V["ash.aas_60s<br/>7 種等待類別 × 前 10 digest"]
  end
  PL & TH & SC & WC & TRX --> EV --> S --> V
  V -->|"query-exporter<br/>或 textfile"| P[(Prometheus)]
  S -->|事後查詢| DBA[DBA]
```

### 設計

- **取樣**：MariaDB 的 EVENT 最短每 1 秒一次，不用額外程序。取樣的 SQL 在 [`ash-sampler.sql`](https://github.com/HahaSula/alert-trust/blob/main/lab/targets/mysql/ash/ash-sampler.sql)：只取 `COMMAND` 不是 `Sleep`、`Daemon`、`Binlog Dump` 的連線，記下時間、帳號、db、state、`TIME_MS`、等待事件、`trx_state`、SQL 前 80 字。
- **等待類別**：固定 7 種，`lock_mdl`（state 含 metadata lock）、`lock_row`（`trx_state = 'LOCK WAIT'`）、`table_io_lock`、`file_io`、`synch`、`cpu`（Updating／Sending data 且沒有等待事件）、`other`。類別要先判斷 `lock_row`，因為等 row lock 的等待事件和正常讀寫一樣。
- **digest 的補法**：MariaDB 執行中拿不到 digest，有三種做法，依成本排序。
    1. 取樣時另外記 `events_statements_current` 的 `THREAD_ID`、`EVENT_ID`；語句結束後同一組 `(THREAD_ID, EVENT_ID)` 會出現在 `events_statements_history_long`，由每分鐘的整理 EVENT 回填 digest。前提是 `history_long` 夠大、整理得夠快（未實測）。
    2. 只對已結束的語句分組：直接讀 `events_statements_history_long`，等於「最近 N 筆語句的 digest 分布」，但看不到還在跑的長語句。
    3. 用 `MD5(LEFT(INFO, n))` 當分組鍵：沒有正規化，帶參數的語句每筆都不同，只適合當事後查詢的線索，不能當 label。
- **保留與大小**：每分鐘刪掉一小時前的列。實驗中約 6,000 列時表約 80 KiB（`information_schema.TABLES` 的估計值）。尖峰時列數 = active session 數 × 3600，要依連線數估。
- **權限**：EVENT 用 definer 的權限執行，definer 要有 `PROCESS`（看別人的連線）與 `SELECT` on `performance_schema`。
- **開 performance_schema 的代價**：要重啟；記憶體依 `performance_schema_*_size` 配置；consumer 與 instrument 在重啟後回到預設（實驗中要在每次啟動後用 `UPDATE performance_schema.setup_*` 重新打開，或寫進設定檔的 `performance-schema-consumer-*`、`performance-schema-instrument`）。

### 輸出成 metric

`ash.aas_60s` 每列是「最近 60 秒，這個等待類別 × digest 平均有幾個 active session」。label 上限 7 × 11（前 10 個 digest 加 `other`），可以直接當 gauge。mysqld_exporter 沒有自訂查詢，要用 query-exporter、sql_exporter 這類工具，或由 cron 把結果寫成 textfile。以下是 query-exporter 的設定例子（未實測，格式依它的 README）：[^my-asp-exp-ash]

```yaml
databases:
  mdb: { dsn: "mysql://ash_reader:...@127.0.0.1:3306/ash" }
metrics:
  mysql_ash_active_sessions:
    type: gauge
    description: 最近 60 秒平均 active session 數，依等待類別與 digest
    labels: [wait_class, digest]
queries:
  aas:
    interval: 30
    databases: [mdb]
    metrics: [mysql_ash_active_sessions]
    sql: SELECT aas AS mysql_ash_active_sessions, wait_class, digest_label AS digest FROM ash.aas_60s
```

### 接到 alert-trust 的訊號

| 訊號（[元件卡](../components/mysqld_exporter.md)） | 現在用的 metric | 取樣器補上的 | 規則例子（未實測） |
|---|---|---|---|
| [active-sessions](../components/mysqld_exporter.md#active-sessions) | `mysql_info_schema_processlist_threads`（只有 scrape 那一刻） | 每秒取樣的平均，scrape 之間的尖峰也算進去 | `sum by (instance) (mysql_ash_active_sessions) > 2 * avg_over_time(sum by (instance) (mysql_ash_active_sessions)[1d:5m])` |
| [lock-waits](../components/mysqld_exporter.md#lock-waits) | `innodb_row_lock_current_waits`、`innodb_row_lock_time` | 哪個語句在等 row lock、等了多久 | `sum by (instance) (mysql_ash_active_sessions{wait_class="lock_row"}) > 2` |
| [metadata-locks](../components/mysqld_exporter.md#metadata-locks) | processlist 的 `waiting_for_table_metadata_lock` | 事後知道 ALTER 與被擋的語句是誰 | 和現有 `MySQLMetadataLockWaits` 相同，改用 `wait_class="lock_mdl"` |
| [slow-sql-visibility](../components/mysqld_exporter.md#slow-sql-visibility) | eventsstatements 的累計耗時（MariaDB 沒有 quantile） | 依時間的 digest 分布（要用上面的 digest 補法） | `topk(3, mysql_ash_active_sessions{digest!="other"})` 放在告警 annotation 或 dashboard |

規則要搭配取樣器自己的存活訊號：`absent(mysql_ash_active_sessions)` 或 query-exporter 的 `up`，以及 `ash.sample` 最新一列的時間（EVENT 被停用、`event_scheduler=OFF` 時取樣會靜默停止）。

### 其他做法

| 做法 | 優點 | 缺點 |
|---|---|---|
| 外部程式每秒查一次（Python、Go），直接輸出 metric | 不寫進資料庫；可以順便查 `SHOW EXPLAIN` | 多一個要監控的程序；連線斷時沒有取樣 |
| slow log（`long_query_time` 調低）＋ `log_slow_verbosity`，送到 log 系統 | 有完整 SQL 與計畫 | 只有已結束而且超過門檻的語句，看不到正在等的 session |
| 只用 exporter 的 processlist collector | 不用改資料庫 | 解析度是 scrape 間隔；沒有語句與等待事件的維度 |

## 證據等級

| 說法 | 等級 |
|---|---|
| MariaDB 缺 quantile、CPU、sample、histogram、errors summary、data_locks | 實測＋讀碼 |
| MariaDB 執行中語句沒有 digest | 實測＋讀碼 |
| MariaDB 預設 consumer 全關 | 實測 |
| 取樣器能分出 row lock、MDL、重查詢 | 實測（一組情境） |
| 每次取樣 3–4 ms | 實測（上限，時鐘不穩） |
| digest 回填、query-exporter 設定、規則例子 | 推論（未實測） |
| MySQL、MariaDB 都沒有內建 ASH | 讀碼（兩邊的 performance_schema 表清單） |
