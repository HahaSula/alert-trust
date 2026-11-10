# 附錄：從原始碼到 metric（ClickHouse）

[主頁](clickhouse.md)列出每個訊號該看哪個 metric。這頁回答另一個問題：**這個 metric 或健康端點是在哪一行產生的？什麼時候更新、誰會輸出？**ClickHouse 的鏈有兩條：

1. **原生 `/metrics`**：ClickHouse v26.10.2.103-stable（Apache-2.0）的 Prometheus handler，直接輸出 process 內的計數器。
2. **SQL 路徑**：query-exporter 5.2.2 對 `system.*` 表的查詢（`queries/clickhouse/clickhouse.yaml`），補每張表的狀態。

每個原始碼參考都在 tag 上依關鍵字找到行號。「Lab」欄只寫在 `lab/targets/clickhouse` 親眼看到的情況。

## 先講結論

1. **`/metrics` 預設是關的，打開後也只有全伺服器的數字。**預設 `config.xml` 的 `<prometheus>` 在註解裡；打開後輸出 `system.events`（全部，含 0）、`system.metrics`、`system.asynchronous_metrics`、`system.errors`（每個錯誤碼一條，含 0），沒有任何「每張表」的 series。<span class="at-chip at-ev-tested">實測</span>[^ch-cfg-prom-commented][^ch-prom-handler][^ch-prom-events-all][^ch-prom-errors-local][^ch-exp-raw]
2. **handler 不拿資料庫的鎖。**CurrentMetrics 與 ProfileEvents 是 atomic，async metric 讀背景執行緒算好的快照（共享鎖）。scrape 不會觸發計算，所以也不會被表鎖、merge 或 Keeper 卡住。<span class="at-chip at-ev-code">讀碼</span>[^ch-prom-metric-atomic][^ch-prom-async-snapshot][^ch-async-getvalues][^ch-async-swap]
3. **async metric 有兩種更新頻率。**一般的每 1 秒；heavy 的（detached parts、pending mutations）預設**不算**，要 `asynchronous_metrics_enable_heavy_metrics` 打開，打開後每 120 秒。<span class="at-chip at-ev-tested">實測</span>[^ch-ss-async-period][^ch-ss-heavy-enable][^ch-ss-heavy-period][^ch-sam-heavy-gate][^ch-exp-raw]
4. **26.8 起帶 key 的 async metric 用 label。**`DiskAvailable{disk="default"}`，不再是 `DiskAvailable_default`；舊名稱要 `asynchronous_metrics_key_values_mode` 設成 `legacy_names` 或 `both`。<span class="at-chip at-ev-tested">實測</span>[^ch-ss-kv-mode][^ch-prom-keyvalue-label][^ch-exp-raw]
5. **`/ping` 與 `/replicas_status` 都不反映唯讀。**`/ping` 是靜態字串；`/replicas_status` 跳過唯讀的表，只在 verbose 內容寫一行。<span class="at-chip at-ev-tested">實測</span>[^ch-ping-static][^ch-rs-readonly-skip][^ch-exp-raw]

## 原生 /metrics {#prometheus}

| 項目 | 說明 | 依據 |
|---|---|---|
| 註冊條件 | 設定裡有 `<prometheus>` 才註冊；有 `<port>` 時開獨立 port，沒有時掛在 `http_port` 的 `/metrics` | [^ch-cfg-prom-commented][^ch-prom-http-default] |
| 子項 | `info`、`metrics`、`asynchronous_metrics`、`events`、`errors`、`histograms`、`dimensional_metrics`，預設都是 true | [^ch-prom-expose-defaults][^ch-prom-handler] |
| 名稱 | `ClickHouseProfileEvents_*`（counter）、`ClickHouseMetrics_*`（gauge）、`ClickHouseAsyncMetrics_*`（gauge）、`ClickHouseErrorMetric_*`（counter）、`ClickHouse_Info` | [^ch-prom-prefixes] |
| ProfileEvents | 全部輸出，含 0（`system.events` 預設只列非 0 的） | [^ch-prom-events-all][^ch-events-zero][^ch-set-events-zero] |
| errors | 本機發生的次數（`error.local.count`）；另有 `ClickHouseErrorMetric_ALL` 是總和（和 `ALL_REPLICAS_ARE_STALE` 這類錯誤碼共用前綴） | [^ch-prom-errors-local][^ch-prom-errors-all] |
| Keeper | Keeper 自己的設定檔也有 `<prometheus>`；只輸出 Keeper 的 ProfileEvents、CurrentMetrics、async metric，不輸出 errors | [^ch-prom-keeper-factory][^ch-prom-keeper-events][^ch-prom-keeper-noerrors] |

Lab：server 的 `/metrics` 約 3,200 個名稱（每個名稱一個 `# TYPE`）、約 10,300 行；Keeper 約 630 個名稱。名稱清單在 `results/names-server-26.10.2.103.txt`、`names-keeper-26.10.2.103.txt`。<span class="at-chip at-ev-tested">實測</span>[^ch-exp-names]

## async metric：誰算、多久算一次 {#async}

| Metric | 產生的地方 | 更新 | 依據 |
|---|---|---|---|
| `MaxPartCountForPartition` | 所有 MergeTree 表單一 partition 的最大 active part 數 | 每 1 秒 | [^ch-sam-maxparts][^ch-ss-async-period] |
| `ReplicasMaxAbsoluteDelay`、`ReplicasMaxQueueSize`、`ReplicasMaxInsertsInQueue` | 所有 Replicated 表的最大值；**唯讀的表不算 delay** | 每 1 秒 | [^ch-sam-absdelay][^ch-sam-skip-readonly][^ch-sam-queue][^ch-sam-inserts-queue] |
| `DiskAvailable`、`DiskTotal`（label `disk`） | 每個 disk；遠端 disk 的 total 可能是 16 EiB | 每 1 秒 | [^ch-sam-disk-available][^ch-sam-disk-total] |
| `NumberOfDetachedParts`、`NumberOfDetachedByUserParts` | heavy metric | 只有打開 heavy 時，每 120 秒 | [^ch-sam-detached][^ch-sam-detached-user][^ch-sam-heavy-gate] |
| `NumberOfPendingMutations`、`NumberOfPendingMutationsOverExecutionTime` | heavy metric | 同上 | [^ch-sam-mutations][^ch-sam-mutations-over] |
| `CGroupMemoryTotal`、`OSMemoryTotal` | 記憶體上限的分母；cgroup 沒設上限時 0 | 每 1 秒 | （lab 名稱清單）[^ch-exp-names] |

## 健康端點 {#health}

| 元件 | 端點 | 檢查 | 依據 |
|---|---|---|---|
| server | `/ping`（http_port） | 無，回 `Ok.` | [^ch-ping-path][^ch-ping-static] |
| server | `/replicas_status` | 每張 Replicated 表：不是唯讀時，absolute delay ≥ `min_absolute_delay_to_close`（預設 0＝不看）或 relative delay ≥ `min_relative_delay_to_close`（300 秒）就 503；唯讀的表只寫 `is readonly`，不讓結果失敗 | [^ch-rs-path][^ch-rs-readonly-skip][^ch-rs-close][^ch-rs-readonly-msg][^ch-rs-503][^ch-mt-min-rel-close][^ch-mt-min-abs-close] |
| Keeper | `/ready`（`keeper_server.http_control`） | 自己是 leader，或 follower／observer 而且有 leader 才 200，否則 503 | [^ch-k-ready-path][^ch-k-ready][^ch-k-ready-503] |
| Keeper | 4lw `ruok` | 一律回 `imok` | [^ch-k-ruok] |
| Keeper | 4lw `mntr` | 沒在服務時回 `This instance is not currently serving requests`；leader 才輸出 `zk_followers`、`zk_synced_followers` | [^ch-k-mntr-inactive][^ch-k-mntr-state][^ch-k-mntr-synced] |
| Keeper | 4lw 允許清單 | `four_letter_word_allow_list` 預設含 `ruok`、`mntr`、`srvr`、`stat` 等 | [^ch-k-4lw-default] |

`max_replica_delay_for_distributed_queries`（預設 300 秒）是 Distributed 查詢挑 replica 用的，和 `/replicas_status` 無關。[^ch-set-max-replica-delay]

## MergeTree：part、insert、mutation {#mergetree}

| Metric／設定 | 說明 | 依據 |
|---|---|---|
| `parts_to_delay_insert`、`parts_to_throw_insert`、`max_parts_in_total` | 1000、3000、100000 | [^ch-mt-delay][^ch-mt-throw][^ch-mt-max-total] |
| `ClickHouseProfileEvents_DelayedInserts`、`RejectedInserts` | `delayInsertOrThrowIfNeeded` 延遲或拒絕時加 1；拒絕時丟 `TOO_MANY_PARTS` | [^ch-mtd-delay-fn][^ch-mtd-delayed][^ch-mtd-rejected][^ch-pe-delayed][^ch-pe-rejected] |
| `ClickHouseMetrics_DelayedInserts` | 目前正被延遲中的 insert 數（gauge，瞬間值） | [^ch-cm-delayed] |
| `max_delay_to_insert` | 每次延遲最多 1 秒 | [^ch-mt-max-delay] |
| `async_insert` | 26.2 起預設 true | [^ch-set-async-insert] |
| `number_of_mutations_to_delay`、`number_of_mutations_to_throw` | 500、1000：未完成的 mutation 太多時延遲或拒絕新 mutation | [^ch-mt-mut-delay][^ch-mt-mut-throw] |
| `system.mutations.latest_fail_reason` | 最近一次 part mutation 失敗的錯誤訊息；`/metrics` 沒有對應的 metric | [^ch-sys-mut-fail] |
| `BackgroundMergesAndMutationsPoolTask`／`PoolSize` | merge 與 mutation 共用的背景池 | [^ch-cm-pooltask][^ch-cm-poolsize] |

## 複寫與 Keeper session {#replication}

| Metric／設定 | 說明 | 依據 |
|---|---|---|
| `ClickHouseMetrics_ReadonlyReplica` | restarting thread 在 session 過期或初始化失敗時 `setReadonly`，加 1 | [^ch-cm-readonly][^ch-rt-setreadonly][^ch-rt-expired] |
| `TABLE_IS_READ_ONLY` | 寫入唯讀表時丟出；`ClickHouseErrorMetric_TABLE_IS_READ_ONLY` 計數 | [^ch-rmt-assert-ro] |
| `ClickHouseMetrics_ZooKeeperSession` | 到 Keeper 的 session 數，應該是 1 | [^ch-cm-zksession] |
| `zookeeper_session_expiration_check_period` | 60 秒 | [^ch-mt-zk-check] |
| `system.replicas.absolute_delay`、`active_replicas` | 每張表；query-exporter 的 `chq_replica_*` | [^ch-sys-rep-absdelay][^ch-sys-rep-active] |
| `ClickHouseMetrics_DistributedFilesToInsert`、`BrokenDistributedFilesToInsert`、`ProfileEvents_DistributedAsyncInsertionFailures` | Distributed 非同步送出的佇列、broken 目錄、失敗次數 | [^ch-cm-distfiles][^ch-cm-brokendist][^ch-dist-broken][^ch-pe-dist-fail] |

## Keeper {#keeper}

| Metric | 說明 | 依據 |
|---|---|---|
| `ClickHouseAsyncMetrics_KeeperIsLeader` | 1 代表 leader | [^ch-k-isleader] |
| `ClickHouseAsyncMetrics_KeeperSyncedFollowers` | leader 上的同步 follower 數；follower 上是 0 | [^ch-k-synced] |
| `ClickHouseAsyncMetrics_KeeperZnodeCount` | znode 數 | [^ch-k-znode] |

## 記憶體與並行 {#memory}

| Metric／設定 | 說明 | 依據 |
|---|---|---|
| `max_server_memory_usage`、`max_server_memory_usage_to_ram_ratio` | 0、0.9 | [^ch-ss-max-mem][^ch-ss-mem-ratio] |
| `max_memory_usage` | 單一查詢，預設 0 | [^ch-set-max-mem] |
| `MEMORY_LIMIT_EXCEEDED`、`QueryMemoryLimitExceeded` | MemoryTracker 超過上限時 | [^ch-mem-exceeded][^ch-pe-qmle] |
| `ClickHouseMetrics_MemoryTracking` | 追蹤的記憶體；預設由背景執行緒依實際 RSS 修正 | [^ch-cm-memtracking] |
| `max_concurrent_queries` | 程式預設 0，預設 `config.xml` 設 1000 | [^ch-ss-max-conc][^ch-cfg-max-conc] |
| `TOO_MANY_SIMULTANEOUS_QUERIES` | ProcessList 超過並行上限時 | [^ch-pl-toomany] |
| `ClickHouseMetrics_QueryPreempted` | 因 `priority` 設定暫停的查詢，不是被拒絕的查詢 | [^ch-cm-querypreempted] |

## 備份 {#backups}

`system.backups` 的內容是 BackupsWorker 記憶體裡的 map，server 重啟後清空；`status` 是 `BACKUP_CREATED`、`BACKUP_FAILED` 等。[^ch-backups-infos][^ch-sys-backups-status]原生 `/metrics` 沒有備份新鮮度。Altinity 的備份規則用 clickhouse-backup 工具自己的 metric。[^alt-backup-failed]

## Altinity metrics-exporter 的來源 {#altinity}

| 項目 | 說明 | 依據 |
|---|---|---|
| 部署方式 | 從 Kubernetes 的 ClickHouseInstallation 物件找 target，不能單獨對一般部署使用 | [^alt-exporter-kube] |
| 名稱 | `chi_clickhouse_metric_*`（system.metrics＋asynchronous_metrics）、`chi_clickhouse_event_*`（system.events）、`chi_clickhouse_table_*` | [^alt-writer-namespace][^alt-fetch-events] |
| system.events | 不加 `system_events_show_zero_values`，所以事件發生前沒有那條 series | [^alt-fetch-events][^ch-events-zero] |
| 自己的 SQL | `DiskFreeBytes`（system.disks）、`DetachedParts`（system.detached_parts）、`LongestRunningQuery`、mutation 數 | [^alt-fetch-disks][^alt-fetch-detached] |
