# ClickHouse 三方交叉、Too many parts、mutation、複寫與 Keeper quorum

回答六個問題：原生 `/metrics` 有什麼、沒有什麼（要靠 SQL 補哪些）；insert 太碎時哪些 metric 先動；mutation 失敗時 `/metrics` 看不看得到；replica 落後或停掉時兩台各自怎麼描述；Keeper 失去 quorum 時 server 還回應什麼（`up`、`/ping`、`/replicas_status`）；`<prometheus>` 關掉時 scrape 看到什麼。另外驗證 query-exporter 5.2.2 能不能連 ClickHouse，以及 Altinity clickhouse-operator release-0.27.4 規則用到的名稱在 v26.10.2.103 有沒有來源。結果用在 [ClickHouse 怎麼監控](../../../docs/targets/clickhouse.md) 與 [ClickHouse 元件卡](../../../catalog/targets/clickhouse.yaml)。

## 環境

| 元件 | 版本／設定 |
|---|---|
| ClickHouse server | `clickhouse/clickhouse-server:26.10.2.103` ×2（ch1、ch2：1 shard、2 replicas），容器 `mem_limit` 900m、`max_server_memory_usage` 600MB；`conf/server/lab.xml` 打開 `<prometheus>`（port 9363）、`parts_to_delay_insert` 20、`parts_to_throw_insert` 40、`min_absolute_delay_to_close` 30 秒；ch2 另外打開 `asynchronous_metrics_enable_heavy_metrics`（ch1 用預設 false，對照用） |
| ClickHouse Keeper | `clickhouse/clickhouse-keeper:26.10.2.103` ×3，`mem_limit` 256m；`session_timeout_ms` 30 秒；`http_control` 開 `/ready`（9182）；`<prometheus>`（9363） |
| Prometheus | `prom/prometheus:v3.13.5`，scrape 5 秒、timeout 4 秒，載入 `rules/targets/clickhouse.yml` 與 `prom/altinity/chkeeper.yml`（Altinity 的 Keeper 規則原文） |
| blackbox_exporter | `prom/blackbox-exporter:v0.27.0`，timeout 4 秒（`bb/blackbox.yml`）：`/ping`、`/replicas_status?verbose=1`（body 有 `is readonly` 也算失敗）、HTTP SQL 探測、Keeper `/ready`、4lw `ruok`、`mntr` |
| query-exporter | `adonato/query-exporter:5.2.2`（映像內建 clickhouse-sqlalchemy 0.3.2、clickhouse-driver 0.2.11），native 協定 9000，帳號 `monitor`（`readonly=2`），設定 `queries/clickhouse/clickhouse.yaml` |

只開 `127.0.0.1` 的 31001–31002（HTTP 8123）、31011–31012（server `/metrics`）、31021–31023（Keeper 4lw）、31031–31033（Keeper `/ready`）、31041–31043（Keeper `/metrics`）、31090（Prometheus）、31091（blackbox）、31092（query-exporter）。compose project 是 `at-ch`；容器記憶體上限加起來約 2.8GB，實際用量約 0.9GB。`./run.sh` 約 15 分鐘，結束時 `docker compose down -v` 並列出殘留容器與 volume。

這台機器的 Docker 容器單調時鐘比牆鐘慢約 30%。SQL 與 HTTP 的耗時用主機的 monotonic 時鐘量；Prometheus 的 `for` 與 `scrape_duration_seconds` 只當參考。

## 情境

| 代號 | 做法 |
|---|---|
| A | 基線：`ON CLUSTER` 建 ReplicatedMergeTree 表、寫 1,000 列；設定值；端點；名稱清單；Altinity 規則的名稱有沒有來源（`altinity_names.py`） |
| B | 兩台 `SYSTEM STOP MERGES`，逐筆 insert 45 次（`async_insert=0`）；再用預設 `async_insert` 逐筆 20 次 |
| C | `ALTER TABLE ... UPDATE v = toString(throwIf(...))`（一定失敗的 mutation），40 秒後看 `system.mutations` 與 `/metrics`；`KILL MUTATION`；在 ch2 `DETACH PART` |
| D | `max_memory_usage` 30MB 的 GROUP BY；`max_concurrent_queries_for_user` 2 時同時送 3 個 `sleep(2)` |
| E | Distributed 表指向不存在的主機，非同步 insert 5 次 |
| F | `BACKUP TABLE ... TO Disk('backups', 'b1.zip')` 兩次（第二次同名，失敗） |
| G1 | ch2 `SYSTEM STOP FETCHES`，ch1 每 8 秒寫一次、共 8 次；看 ch2 的延遲與 `/replicas_status` |
| G2 | `docker compose stop ch2`，ch1 繼續寫；看 ch1 怎麼描述；啟動 ch2 |
| H | 停 Keeper leader 與一台 follower（失去 quorum），+10、+50 秒看 server 與 Keeper 的端點、SQL、三方交叉；啟動 Keeper，量 ch1 恢復可寫的時間 |
| I | ch2 的 `<prometheus>` 關掉 `asynchronous_metrics`；再整段拿掉（`<prometheus remove="remove"/>`） |

server 的慢磁碟、磁碟滿、broken part（crash 後）、server 層級的 `max_server_memory_usage`、Keeper 慢、多 shard 的 Distributed 查詢沒有做。

## 結果

原始輸出 `results/run-20261110.txt`；名稱清單 `results/names-server-26.10.2.103.txt`、`names-keeper-26.10.2.103.txt`。
補充：`results/extra-20261110.txt`（`./extra.sh`：J part 數對門檻、K broken part、L 使用者 DETACH、M 備份、N Keeper 失去 quorum 時的 SQL 路徑與 `ZooKeeperSession*`）；`results/extra-first-20261110.txt` 是補充的第一次執行，query-exporter 的 1m 查詢被排程器跳過、沒有資料（保留作為證據，之後把 interval 錯開重跑）。

### 三方交叉（up／probe／健康 metric）

| 情境 | 對象 | `up` | probe | 健康 metric |
|---|---|---|---|---|
| 正常 | 全部 | 1 | 全部 1 | Keeper leader 1 台、`KeeperSyncedFollowers` 2 |
| G1：ch2 停止 fetch 65 秒 | ch2 | 1 | `/ping` 1、`/replicas_status` 503（`Absolute delay: 70`） | `ReplicasMaxAbsoluteDelay` 80 |
| G2：停 ch2 +40 秒 | ch2 | 0 | 0 | — |
| G2 | ch1 | 1 | 1 | delay 0、`active_replicas` 1／2 |
| H：停 2 台 Keeper +81 秒 | ch1、ch2 | 1 | `/ping` 1、`/replicas_status` 200（body `is readonly`）、SQL 0 | `ReadonlyReplica` 1、`ReplicasMaxAbsoluteDelay` 0、`ZooKeeperSession` 1 |
| H | keeper3 | 1 | `/ready` 503、`ruok` imok、`mntr` not serving | `KeeperIsLeader` 0 |
| I：關 asynchronous_metrics | ch2 | 1 | 1 | sample 3,884 → 3,183 |
| I：拿掉 `<prometheus>` | ch2 | 0（connection refused） | 1 | — |

### 其他

| 情境 | 結果 |
|---|---|
| A | `/metrics` 名稱 server 3225、Keeper 632；sample server 約 3,900、Keeper 1,193；`DiskAvailable{disk=...}`（舊名 `DiskAvailable_default` 0 條）；ch1（heavy 關）沒有 `NumberOfDetachedParts` 等 4 個名稱；`system.events` 預設 169–179 列、全部 1,706；Altinity ClickHouse 規則 47 個名稱缺 `chi_clickhouse_event_NetworkErrors`，Keeper 規則 10 個都有 |
| B | 第 21–39 筆延遲、第 40 筆起拒絕：`DelayedInserts` 20、`DelayedInsertsMilliseconds` 10,500、`RejectedInserts` 6；CHInsertsDelayed、CHInsertsRejected firing。預設 async_insert 逐筆 20 次：`AsyncInsertFlush` 21＝`AsyncInsertQuery` 21，flush 也被拒（`RejectedInserts` 26） |
| C | mutation 立刻成功；40 秒後兩台 `latest_fail_reason` 有錯誤、`chq_mutations_failing` 1、`/metrics` 只有 `FUNCTION_THROW_IF_VALUE_IS_NON_ZERO` 14／15；DETACH PART 失敗 `PART_IS_TEMPORARILY_LOCKED`（改在 extra.sh 做） |
| D | 30MB 上限的 GROUP BY 0.06 秒失敗（`MEMORY_LIMIT_EXCEEDED` 1、`QueryMemoryLimitExceeded` 1）；上限 2 時第 3 個查詢 `TOO_MANY_SIMULTANEOUS_QUERIES` |
| E | 5 次 insert 都成功；`DistributedFilesToInsert` 5、`DistributedAsyncInsertionFailures` 2；`DROP TABLE` 23.7 秒 |
| F | 第二次同名 BACKUP 失敗；70 秒後 query-exporter 還沒更新（1m 查詢被跳過，見 extra-first） |
| H | 寫入卡住超過 30 秒（client timeout）；`system.zookeeper_connection` 也卡住，同一個 DSN 的 query-exporter 查詢全部逾時、chq_* 消失；Keeper 啟動後 3 秒恢復可寫；第一版 CHKeeperSessionLost 恢復後 3 分鐘仍 firing |
| J（extra） | 15 個 part、門檻 20：`chq_merge_tree_setting` 取得 20／40／100000，比例 0.75，CHPartsNearInsertDelay 兩台 pending |
| K（extra） | `data.bin` 截短後重啟：表 `count()` 0（不報錯）、`broken-on-start` detached；`chq_detached_parts{reason="broken-on-start"}` 1、`NumberOfDetachedParts` 1（ch2）；CHBrokenPartsDetached、CHDetachedPartsNative pending |
| L（extra） | 使用者 `DETACH PARTITION`：reason `detached_by_user`，規則不響 |
| M（extra） | 成功一次、同名失敗一次：55 秒內 `chq_backups_failed` 1、`chq_backup_last_success_timestamp` 有值；CHBackupFailed firing |
| N（extra） | 拿掉 `keeper_connection` 查詢後，Keeper 失去 quorum 時 SQL 路徑照常（`chq_replica_readonly` 1，資料 30 秒內）；`ZooKeeperSession` 一直 1、`ZooKeeperSessionExpired` 0 → 3，恢復 151 秒後仍 3；修改後的 CHKeeperSessionLost（變化量）firing，恢復 151 秒時仍在 5 分鐘視窗內 |

query-exporter 的 interval 錯開（15、17、19、23、29、61、67、71 秒）後，extra 的 log 裡仍有 15 次 `was missed`，但每個查詢都有資料。

## 沒做的

慢磁碟、磁碟滿、Keeper 慢、server 層級的 `max_server_memory_usage`、merge 池飽和、多 shard 的 Distributed 查詢、ON CLUSTER DDL 佇列卡住、TLS。
