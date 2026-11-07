# Exporter 架構

資料庫 exporter 都是 pull 模型：**Prometheus 來抓的那一刻，exporter 才去查資料庫**，沒有快取。所以 scrape 有多慢，取決於資料庫當下回得多快，以及 exporter 怎麼安排這些查詢。

兩個 exporter 的 collector 都各開一條 goroutine，看起來是並行，**實際上被連線數卡住**。

以 mysqld_exporter v0.21.0、postgres_exporter v0.20.1 原始碼為準。<span class="at-chip at-ev-code">讀碼</span>

## 對照

| 項目 | mysqld_exporter | postgres_exporter |
|---|---|---|
| 每次 scrape 的連線 | 新建一個連線池，上限 `--exporter.max_open_connections`（預設 2）[^my-newinstance][^my-pool-default] | Collector 系統每次新建連線，**上限 1**[^pg-collect][^pg-maxopen] |
| Collector 執行 | 每個一條 goroutine，最多 2 個查詢同時跑[^my-scrapers-go] | 每個一條 goroutine，**實際上排隊**[^pg-goroutines][^pg-maxopen] |
| 查詢期限 | Prometheus 送來的 scrape timeout 減 `--timeout-offset`（預設 0.25s）；單一 collector 可設 `--exporter.query_timeout`[^my-offset][^my-querytimeout] | `--collection-timeout`，**預設 1m**，比一般的 scrape_timeout 長[^pg-timeout-flag][^pg-timeout] |
| 錯誤指標 | `mysql_up`、`mysql_exporter_collector_success` | 兩套：舊架構的 `pg_up`、`pg_exporter_last_scrape_error`，新架構的 `pg_scrape_collector_success`，互不相通 |
| 多 target（`/probe`） | 支援；連線池依每個 scrape 請求建立，同時抓的 target 越多連線越多 | 支援；`/probe` 沒有逾時[^pg-probe] |

## mysqld_exporter {#mysqld-exporter}

- 每次 scrape 先連資料庫。連不上時 `mysql_up` 變 0，**所有 collector 的結果整組消失**，不是變成 0。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql][^my-up]
- 期限跟著 Prometheus 的 scrape timeout 走，所以 exporter 會比 Prometheus 早 0.25 秒放棄，`up` 仍是 1。資料庫停機時 scrape 耗時正好是 timeout 減 0.25s。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql]
- 連線池只有 2 條，一個慢 collector 會佔住一條，其他 collector 只剩一條可用。[^my-pool-default]
- 權限不足時只有對應的 collector 失敗，其他照常；有些 collector（如 processlist）權限不足也回報成功，只是數字偏低。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql]

## postgres_exporter {#postgres-exporter}

- Collector 系統每次 scrape 新建**一條**連線，所有 collector 的 goroutine 共用它，實際上一個接一個執行。**一個慢查詢會拖住全部**，總耗時是所有查詢相加。[^pg-maxopen][^pg-goroutines]
- `--collection-timeout` 預設 1m。資料庫主機不回應時，一次 scrape 要 17–30 秒以上，Prometheus 先放棄，`up` 變 0、所有 `pg_*` 消失。<span class="at-chip at-ev-tested">實測</span>[^exp-pg]
- 舊架構（`pg_up`、自訂 queries）會重用既有連線。把 exporter 帳號改成 NOLOGIN 時，`pg_up` 仍是 1，新架構的 collector 結果卻整組消失。<span class="at-chip at-ev-tested">實測</span>[^exp-pg]
- 沒有 `pg_monitor` 時多數 collector 仍回報成功，但看不到別人的 session：跑了 20 秒的交易顯示成 0。<span class="at-chip at-ev-tested">實測</span>[^exp-pg]

## 這對 rule 代表什麼

1. **`*_up == 0` 不夠。**Exporter 停機或卡住時 `*_up` 會消失，要搭配 `up == 0` 與 `absent`。
2. **Collector 結果可能整組消失。**要有 `up == 1 unless on(job, instance) <collector_success>`。
3. **Exporter 的期限要比 `scrape_timeout` 短**，否則 Prometheus 先放棄，看起來像 exporter 掛了。見[契約](../contracts.md#exporter-deadline)。
4. **成功不代表看得到資料。**權限、版本、extension 不對時，collector 回報成功卻沒有資料或數字偏低。

這四點對任何 exporter 都成立，已整理成每張 target 卡都要回答的問題，見[元件卡總覽](../components/index.md)。
