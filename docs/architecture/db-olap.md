# 分析型架構

這頁只寫**監控訊號或盲點依賴的分析型（欄式）資料庫機制**。每一節依序是機制、什麼會壞、哪個訊號看得到。目前只有 ClickHouse（v26.10.2.103-stable：server 與 ClickHouse Keeper）。訊號連到 [ClickHouse 元件卡](../components/clickhouse.md)，完整清單在 [ClickHouse 怎麼監控](../targets/clickhouse.md)；Keeper 的 Raft 在[共識（Raft）架構](db-raft.md#clickhouse-keeper)。

以原始碼與 `lab/targets/clickhouse` 的實驗為準。<span class="at-chip at-ev-code">讀碼</span> <span class="at-chip at-ev-tested">實測</span>

## 先講結論

1. **每次 insert 都是一個新的 part，靠背景 merge 收斂。**insert 快過 merge 時 part 數上升，到門檻先被延遲、再被拒絕（Too many parts）。原生 `/metrics` 只有全伺服器的最大值 `MaxPartCountForPartition`，哪一張表要用 SQL 查。[^ch-mtd-delay-fn][^ch-sam-maxparts]
2. **mutation（`ALTER ... UPDATE/DELETE`）是背景重寫，失敗時不會回報給下指令的人。**`ALTER` 立刻成功，之後一直重試；失敗原因只在 `system.mutations.latest_fail_reason`。[^ch-sys-mut-fail]
3. **複寫靠 Keeper 上的共享 log。**server 失去 Keeper session 時 Replicated 表變唯讀，但 server 自己的 HTTP、`/ping`、`/metrics` 照常；`/replicas_status` 也回 200。[^ch-rt-setreadonly][^ch-rs-readonly-skip][^ch-ping-static]
4. **Distributed 表預設先寫本機檔案、再非同步送到各 shard。**遠端 shard 不通時 insert 仍成功，檔案在本機累積。[^ch-cm-distfiles][^ch-pe-dist-fail]
5. **查詢受記憶體上限保護，代價是查詢失敗。**單一查詢 `max_memory_usage`、整台 `max_server_memory_usage`；超過時丟 `MEMORY_LIMIT_EXCEEDED`，不是 OOM。[^ch-mem-exceeded][^ch-ss-mem-ratio]

## 對照 {#compare}

| 機制 | ClickHouse | 什麼會壞 | 看得到的訊號 | 本頁 |
|---|---|---|---|---|
| 欄式 part 與背景 merge | MergeTree：每次 insert 一個 part，背景合併 | merge 跟不上、part 數上升 | parts-pressure、merge-pool | [part 與 merge](#parts) |
| insert 批次 | 少量大 insert；`async_insert` 在伺服器端合併 | 小 insert 太多 → Too many parts | writes-rejected、parts-pressure | [insert 批次](#insert-batching) |
| mutation | 背景重寫 part | 失敗後無限重試、後面的 mutation 排隊 | mutations | [mutation](#mutations) |
| ReplicatedMergeTree | Keeper 上的複寫 log＋每台的 queue | Keeper 不在時唯讀；fetch 失敗時落後 | replica-readonly、replication-lag、replication-queue、keeper-session | [複寫](#replication) |
| Distributed 表 | 本機暫存檔＋非同步送出 | 遠端 shard 不通時檔案累積、broken 檔案 | distributed-queue | [Distributed](#distributed) |
| 記憶體上限 | 查詢與伺服器各一個 MemoryTracker | 大查詢失敗；整台接近上限時所有查詢都可能失敗 | memory | [記憶體](#memory) |

## 欄式 part 與背景 merge {#parts}

**機制。**MergeTree 表的資料以 part 為單位：每個 part 是一個目錄，裡面每個欄位一個檔案，依 `ORDER BY` 排序。insert 不修改既有 part，而是寫一個新的；背景執行緒把同一個 partition 的小 part 合併成大 part。查詢要讀每個 active part 的 index，part 越多越慢。merge 由背景池執行，池的大小是 `background_pool_size`。[^ch-cm-pooltask][^ch-cm-poolsize]

**什麼會壞。**merge 跟不上（磁碟慢、池滿、或 partition key 太細）時，單一 partition 的 active part 數一直上升；merge 需要預留磁碟空間，空間不夠時也會停。part 太多的後果在下一節。

**看得到的訊號。**`ClickHouseAsyncMetrics_MaxPartCountForPartition`（背景每秒算一次，全伺服器最大值）；merge 池用 `ClickHouseMetrics_BackgroundMergesAndMutationsPoolTask` 對 `PoolSize`。哪一張表要查 `system.parts`（query-exporter 的 `chq_parts_max_per_partition`）。[^ch-sam-maxparts][^ch-ss-async-period]見 [parts-pressure](../components/clickhouse.md#parts-pressure)、[merge-pool](../components/clickhouse.md#merge-pool)。

## insert 批次與 Too many parts {#insert-batching}

**機制。**每次 insert 前檢查目標 partition 的 active part 數：超過 `parts_to_delay_insert`（預設 1000）時 sleep，超過 `parts_to_throw_insert`（預設 3000）或整張表超過 `max_parts_in_total`（100000）時丟 `TOO_MANY_PARTS`。延遲時 `DelayedInserts` 加 1，拒絕時 `RejectedInserts` 加 1。[^ch-mtd-delay-fn][^ch-mtd-delayed][^ch-mtd-rejected][^ch-mt-delay][^ch-mt-throw][^ch-mt-max-total]26.2 起 `async_insert` 預設開：伺服器把多個小 insert 先放進佇列再一起寫成一個 part。[^ch-set-async-insert]

**什麼會壞。**大量小 insert（每秒很多次、每次幾列）。延遲階段 insert 變慢但成功；到了拒絕門檻 insert 直接失敗。`async_insert` 只合併同時到達的 insert：lab 用同一個 client 一筆接一筆送 21 次，`AsyncInsertFlush` 也是 21 次，沒有合併；而且 part 數已經在門檻時，flush 一樣被拒（`RejectedInserts` 從 6 增加到 26）。<span class="at-chip at-ev-tested">實測</span>[^ch-exp-raw]

**看得到的訊號。**lab（門檻調成 20／40、停掉 merge、逐筆 insert 45 次）：part 數停在 40，`DelayedInserts` 20、`RejectedInserts` 6、`ClickHouseErrorMetric_TOO_MANY_PARTS` 6；CHInsertsDelayed、CHInsertsRejected 在 25 秒內 firing。<span class="at-chip at-ev-tested">實測</span>[^ch-exp-raw]見 [writes-rejected](../components/clickhouse.md#writes-rejected)。

## mutation 是非同步重寫 {#mutations}

**機制。**`ALTER TABLE ... UPDATE` 或 `DELETE` 在 Replicated 表上寫一筆 mutation 到 Keeper，立刻回傳成功；每台 replica 在背景把受影響的 part 重寫成新 part。同一張表的 mutation 依序執行。[^ch-sys-mut-fail]

**什麼會壞。**重寫時出錯（型別轉換、`throwIf` 之類的運算錯誤、記憶體）不會讓 `ALTER` 失敗，而是一直重試：`is_done` 停在 0、`latest_fail_reason` 是錯誤訊息，後面的 mutation 排在它後面。未完成的 mutation 太多時，新的 mutation 也會被延遲或拒絕（`number_of_mutations_to_delay` 500、`number_of_mutations_to_throw` 1000）。[^ch-mt-mut-delay][^ch-mt-mut-throw]

**看得到的訊號。**原生 `/metrics` 沒有「mutation 失敗」：lab 40 秒內兩台的 `latest_fail_reason` 都是 `Code: 395 ... lab: mutation fails on purpose`，`/metrics` 只看得到錯誤碼計數 `ClickHouseErrorMetric_FUNCTION_THROW_IF_VALUE_IS_NON_ZERO` 增加到 14（不知道是哪張表、是不是 mutation）；`NumberOfPendingMutations` 要打開 heavy metric 才有（只有 ch2 有，值 1）。query-exporter 的 `chq_mutations_failing` 兩台都是 1。<span class="at-chip at-ev-tested">實測</span>[^ch-exp-raw][^ch-sam-heavy-gate][^ch-ss-heavy-enable]見 [mutations](../components/clickhouse.md#mutations)。

## ReplicatedMergeTree：Keeper 上的複寫 log {#replication}

**機制。**每張 Replicated 表在 Keeper 上有一個目錄：共享的 log（insert、merge、mutation 的紀錄）與每台 replica 自己的 queue。replica 把 log 讀進自己的 queue，依序執行：insert 的 part 從其他 replica 下載（fetch）、merge 自己做或下載結果。`absolute_delay` 是「最舊的、還沒拿到的 part」距今多久；`active_replicas` 是 Keeper 上有 `is_active` 節點的 replica 數。[^ch-sys-rep-absdelay][^ch-sys-rep-active]

**什麼會壞。**

- **失去 Keeper session**：restarting thread 發現 session 過期時把表設成唯讀、`ReadonlyReplica` 加 1；寫入丟 `TABLE_IS_READ_ONLY`。讀取不受影響。[^ch-rt-setreadonly][^ch-rt-expired][^ch-rmt-assert-ro]
- **fetch 停了或失敗**：queue 變長、`absolute_delay` 上升。
- **一台 replica 停了**：倖存那台的 `absolute_delay` 仍是 0（它有全部資料），只有 `active_replicas` 少一。

**看得到的訊號，和它們的盲點。**

| 訊號 | 從哪來 | 盲點 |
|---|---|---|
| `ClickHouseMetrics_ReadonlyReplica` | 唯讀的 Replicated 表數 | 是數量，不知道哪張表 |
| `ClickHouseAsyncMetrics_ReplicasMaxAbsoluteDelay` | 所有 Replicated 表的最大 absolute_delay | **唯讀的表不算**：Keeper 不在時看起來像沒有延遲[^ch-sam-skip-readonly] |
| `/replicas_status` | 延遲超過 `min_relative_delay_to_close`（300 秒）或 `min_absolute_delay_to_close`（預設 0＝不看）時 503 | **唯讀的表不算失敗**：只在 verbose 內容寫 `is readonly`，狀態碼仍是 200[^ch-rs-readonly-skip][^ch-rs-close] |
| `system.replicas`（query-exporter） | 每張表的 is_readonly、absolute_delay、queue_size、active_replicas | 要 SQL 路徑本身活著 |

見 [replica-readonly](../components/clickhouse.md#replica-readonly)、[replication-lag](../components/clickhouse.md#replication-lag)、[keeper-session](../components/clickhouse.md#keeper-session)。lab 的三方交叉見 [ClickHouse 頁](../targets/clickhouse.md#health-second-path)。

## Distributed 表與非同步送出 {#distributed}

**機制。**Distributed 表本身不存資料，insert 時依 sharding key 把資料分給各 shard。預設（`distributed_foreground_insert` 0）先在本機寫一個檔案、立刻回傳成功，背景再送到遠端 shard；送失敗就留著重試，無法送出的檔案移到 `broken` 目錄。[^ch-dist-broken]

**什麼會壞。**遠端 shard 不通或拒絕寫入時，insert 端看不到錯誤，檔案在本機累積、佔磁碟；broken 的檔案不會再送，資料沒有進遠端 shard。

**看得到的訊號。**`ClickHouseMetrics_DistributedFilesToInsert`、`BrokenDistributedFilesToInsert`、`ClickHouseProfileEvents_DistributedAsyncInsertionFailures`；原因在 `system.distribution_queue.last_exception`。[^ch-cm-distfiles][^ch-cm-brokendist][^ch-pe-dist-fail]見 [distributed-queue](../components/clickhouse.md#distributed-queue)。

## 記憶體上限 {#memory}

**機制。**每個查詢有自己的 MemoryTracker，上層是使用者與整台伺服器。單一查詢的上限是 `max_memory_usage`（預設 0＝不限制），整台的上限是 `max_server_memory_usage`（0 時用 RAM × `max_server_memory_usage_to_ram_ratio` 0.9，在容器裡是 cgroup 的上限）。超過時丟 `MEMORY_LIMIT_EXCEEDED` 並增加 `QueryMemoryLimitExceeded`；整台接近上限時 overcommit tracker 挑一個查詢結束。[^ch-mem-exceeded][^ch-set-max-mem][^ch-ss-max-mem][^ch-ss-mem-ratio]

**什麼會壞。**大的 GROUP BY、JOIN、排序在記憶體不夠時失敗，而不是讓 process 被 OOM killer 殺掉；伺服器整體接近上限時，小查詢也可能失敗。

**看得到的訊號。**`ClickHouseMetrics_MemoryTracking` 對 `CGroupMemoryTotal`（或 `OSMemoryTotal`）；`ClickHouseErrorMetric_MEMORY_LIMIT_EXCEEDED` 的增加量。`/metrics` 沒有「目前生效的上限」，設定值要查 `system.server_settings`。lab（`max_memory_usage` 30MB 的 GROUP BY）：查詢在 0.06 秒失敗 `Query memory limit exceeded`。<span class="at-chip at-ev-tested">實測</span>[^ch-exp-raw]見 [memory](../components/clickhouse.md#memory)。
