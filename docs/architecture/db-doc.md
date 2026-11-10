# 文件型架構（MongoDB）

MongoDB 的資料在 WiredTiger 儲存引擎裡，複寫靠 **oplog**（一個有大小上限的 collection），高可用靠 replica set 的選舉。監控問題大多出在這幾個機制的「邊界」：cache 滿了要應用執行緒自己清、oplog 被釘住不能截斷、primary 看不到多數就自己降級、w:majority 沒有 wtimeout 時一直等。這頁只寫和監控有關的六個機制，每一節是「機制 → 壞了會怎樣 → 哪個訊號看得到」。

以 MongoDB 8.0.34（SSPL）原始碼與內附的 WiredTiger 為準。<span class="at-chip at-ev-code">讀碼</span> 實驗數字來自 `lab/targets/mongodb`（3 member replica set、mongodb_exporter v0.54.0），這台機器的容器單調時鐘比牆鐘慢約 30%，選舉與 checkpoint 的秒數只當量級看。[^mg-exp] 訊號連到 [mongodb_exporter 元件卡](../components/mongodb_exporter.md)；該監控什麼的完整清單在 [MongoDB 怎麼監控](../targets/mongodb.md)。

## 先講結論

1. **WiredTiger cache 的問題是「應用執行緒被迫做 eviction」。**cache 用到 80% 是常態（eviction_target）；dirty 到 20% 或用量到 95% 時，進來的請求要自己幫忙清 cache，延遲直接變長。[^wt-eviction-defaults][^wt-stat-app-evict]
2. **8.0 的 ticket 數是動態的。**預設 throughputProbing 從 CPU 核心數開始、在 4～128 之間調；照舊版固定 128 寫的「available < 50」規則在 16 核的 lab 一開機就響。[^mg-ticket-algo][^mg-tp-initial][^mg-exp]
3. **oplog 超過大小也不一定會被截斷。**只有沒被 stable timestamp（跟著 majority commit point）釘住的部分能刪；majority 停住時 oplog 一直長。[^mg-oplog-pinned][^mg-pinned-rollback][^mg-pinned-stable]
4. **primary 看不到多數就自己降級，剩下的節點都是 SECONDARY，`mongodb_up` 仍是 1。**[^mg-stepdown-majority][^mx-up-ping][^mg-exp]
5. **w:majority 預設沒有逾時。**沒有 arbiter 時預設 write concern 就是 majority，wtimeout 0 代表一直等；`getLastError.wtimeouts` 只數有設 wtimeout 的寫入。[^mg-wc-implicit][^mg-wc-notimeout][^mg-exp]
6. **flow control 只限流，不讓寫入失敗。**commit point 落後超過 5 秒時 primary 把寫入 ticket 壓到每秒最少 100 個；lab 兩個 secondary 都卡住時 `isLagged` 1、`targetRateLimit` 100，每秒 20 筆的寫入完全沒被擋。<span class="at-chip at-ev-tested">實測</span>[^mg-fc-min-tickets][^mg-exp]

## WiredTiger cache 與 eviction {#wt-cache}

**機制。**cache 預設是 (RAM − 1GB) × 50%，至少 256MB（`wiredTigerCacheSizeGB` 可以改）。[^mg-wt-cache-default] MongoDB 固定開 4 個背景 eviction thread。[^mg-wt-eviction-threads] WiredTiger 的預設門檻：用量 80%（eviction_target）開始背景清、95%（eviction_trigger）時應用執行緒也要清；dirty 5%（eviction_dirty_target）開始背景寫出、20%（eviction_dirty_trigger）時應用執行緒也要寫。[^wt-eviction-defaults]

**壞了會怎樣。**寫入量超過磁碟能寫出的速度時，dirty 比例上升到 20%，每個寫入請求先花時間做 eviction；讀取量超過 cache 時，用量貼著 95%，讀取請求要先清出空間。從外面看是延遲上升、ticket 排隊，CPU 不一定滿。

**看得到它的訊號。**[wt-cache](../components/mongodb_exporter.md#wt-cache)：`mongodb_ss_wt_cache_tracked_dirty_bytes_in_the_cache`、`mongodb_ss_wt_cache_bytes_currently_in_the_cache` 對 `mongodb_ss_wt_cache_maximum_bytes_configured`，加上 `mongodb_ss_wt_cache_application_thread_time_evicting_usecs` 的速率（應用執行緒花在 eviction 的時間）。[^wt-stat-dirty][^wt-stat-max][^wt-stat-app-evict] lab 的寫入量太小，沒有重現 eviction 壓力。

## ticket 與並行控制 {#tickets}

**機制。**每個讀寫操作進 storage engine 前要拿一張 ticket（serverStatus 的 `queues.execution`）。[^mg-queues-section] 8.0 預設 `storageEngineConcurrencyAdjustmentAlgorithm` 是 `throughputProbing`：初始 ticket 數是 CPU 核心數，依吞吐量在 4～128 之間調整；`fixedConcurrentTransactions` 才是舊版的固定值。[^mg-ticket-algo][^mg-tp-initial][^mg-tp-min][^mg-tp-max] 另一層是鎖：`globalLock.currentQueue` 是在等鎖的操作數。[^mg-globallock-queue]

**壞了會怎樣。**cache 壓力、慢磁碟或長交易讓 ticket 一直被佔住時，新的操作排隊（`queueLength` 上升）。`fsyncLock` 或拿 global X 鎖的指令讓所有寫入在鎖上排隊。

**看得到它的訊號。**[tickets](../components/mongodb_exporter.md#tickets)：`mongodb_ss_wt_concurrentTransactions_{read,write}_normalPriority_queueLength` 與 `mongodb_ss_globalLock_currentQueue`。看 `available` 的固定門檻在動態 ticket 下沒有意義：lab（16 核）`totalTickets` 是 16，PMM 與 awesome 的「available < 50」規則永遠成立或永遠對不到 label。<span class="at-chip at-ev-tested">實測</span>[^mg-exp][^pmm-read-tickets][^awe-mg-read-tickets]

## oplog：有大小上限的 collection {#oplog}

**機制。**每個寫入在 primary 上寫成 `local.oplog.rs` 的一筆，secondary 讀 oplog 套用。大小：有 `--oplogSize` 就用它，否則是可用空間的 5%，至少 990MB、最多 50GB。[^mg-oplog-user-size][^mg-oplog-5pct][^mg-oplog-min][^mg-oplog-max] 截斷由背景執行緒依「截斷點」（至少 10 個）整段刪，條件是：總大小超過上限、最舊那段不在被釘住的範圍內、而且（有設 `oplogMinRetentionHours` 時）夠舊。[^mg-oplog-capacity][^mg-oplog-pinned][^mg-oplog-min-retention][^mg-oplog-markers-min] 被釘住的位置取 rollback 需要的 oplog，也就是依 stable timestamp（跟著 majority commit point 前進），再加上 backup 的釘選。[^mg-pinned-rollback][^mg-pinned-stable]

**oplog window。**最新與最舊一筆的時間差，等於「secondary 可以離線多久還能追上」；超過時那台要 initial sync。shell 的 `db.getReplicationInfo()` 印的 `timeDiff` 就是它。exporter 在 `--compatible-mode` 直接讀 oplog 的第一筆與最後一筆（`$natural` 排序）輸出 head／tail。[^mx-oplog-find][^mx-oplog-head]

**壞了會怎樣。**

- 寫入量突然變大時 window 縮短：lab 用 2MB 的 oplog 寫 10MB，window 只剩 95 秒。截斷不是即時的：之後 150 秒內 oplog 一直是上限的 5 倍（10.6MB）。<span class="at-chip at-ev-tested">實測</span>[^mg-exp]
- majority commit point 停住時（多數 secondary 停機或卡住）oplog 不能截斷，越長越大，最後是磁碟滿（讀碼）。lab 兩個 secondary 都卡住時 oplog 長到 5.4MB、解鎖 20 秒後 6.7MB；這組數據分不出是被釘住還是延後截斷。

**看得到它的訊號。**[oplog-window](../components/mongodb_exporter.md#oplog-window)（head − tail；Atlas 建議 < 24h 低優先、< 1h 高優先）、[oplog-growth](../components/mongodb_exporter.md#oplog-growth)（`mongodb_oplog_stats_storageStats_size` 對 `maxSize`）、[replication-lag](../components/mongodb_exporter.md#replication-lag)（延遲接近 window 時那台 secondary 快要追不上）。

## replica set 選舉與 write concern {#election}

**機制。**member 之間每 2 秒互送 heartbeat；超過 `electionTimeoutMillis`（10 秒）沒有某台的消息就把它標成 DOWN，它在 replSetGetStatus 的 optime 被清成 0。[^mg-hb-interval][^mg-election-timeout][^mg-member-timeout][^mg-set-down-optime] primary 這時若看不到多數就自己降級成 SECONDARY。[^mg-stepdown-majority] secondary 在逾時後先做 dry run，拿到多數才把 term 加 1 正式選舉；當選後先進入 catch-up（`catchUpTimeoutMillis` 預設無限）。[^mg-dryrun][^mg-term-increment][^mg-catchup][^mg-catchup-timeout] 多數是投票成員數 / 2 + 1；沒有 arbiter 時預設 write concern 是 majority，wtimeout 0 代表一直等。[^mg-majority-count][^mg-write-majority][^mg-wc-implicit][^mg-wc-notimeout] 和 Raft 的對照見[共識架構](db-raft.md#mongodb)。

**壞了會怎樣。**

- primary 停機：在新 primary 出現前（electionTimeout 加上選舉）沒有任何 member 是 `isWritablePrimary`。
- 3 台停 2 台：存活的那台不論原本是什麼都變成 SECONDARY，寫入回 `NotWritablePrimary`；它的 exporter `up=1`、`mongodb_up=1`。
- 多數 secondary 卡住（不是停機）：primary 還在，w:1 寫入成功，w:majority 寫入一直等。

**看得到它的訊號。**[role-detection](../components/mongodb_exporter.md#role-detection)（依 cluster 計數 `mongodb_ss_repl_isWritablePrimary == 1`）、[elections](../components/mongodb_exporter.md#elections)（`mongodb_rs_term` 的增加）、[member-health](../components/mongodb_exporter.md#member-health)、[majority-commit-lag](../components/mongodb_exporter.md#majority-commit-lag)（primary 的 `lastAppliedWallTime − lastCommittedWallTime`）、[majority-writes](../components/mongodb_exporter.md#majority-writes)。

## flow control {#flow-control}

**機制。**primary 上每秒依 majority commit point 的落後調整「每秒可以拿幾個寫入 ticket」：落後超過 `flowControlTargetLagSeconds`（10 秒）× `flowControlThresholdLagPercentage`（0.5）時開始限制，最低每秒 `flowControlMinTicketsPerSecond`（100）個。[^mg-fc-enabled][^mg-fc-target-lag][^mg-fc-threshold][^mg-fc-healthy][^mg-fc-min-tickets] 只在可寫入的節點上作用；判斷時看的是各 member applied 時間的中位數（sustainer）有沒有前進。[^mg-fc-primary-only][^mg-fc-sustainer][^mg-fc-median]

**壞了會怎樣。**secondary 套用得比 primary 寫入慢時，flow control 把 primary 的寫入速度壓下來，應用看到寫入變慢但沒有錯誤。下限是每秒 100 個 ticket，寫入量更小的系統完全不受影響，延遲與 oplog 照樣長。lab：m2、m3 都 `fsyncLock` 約 50 秒後，primary 的 commit point 落後 63 秒、`isLagged` 1、`isLaggedCount` 1、`targetRateLimit` 100；每 50ms 一筆的 w:1 寫入 70 秒寫了 1353 筆，沒有錯誤。只有一個 secondary 卡住時（m2 照常），commit point 跟著 m2 前進，`isLagged` 一直是 0。<span class="at-chip at-ev-tested">實測</span>[^mg-exp]

**看得到它的訊號。**[majority-commit-lag](../components/mongodb_exporter.md#majority-commit-lag)：`mongodb_ss_flowControl_isLagged`、`mongodb_ss_flowControl_targetRateLimit`、`mongodb_ss_flowControl_isLaggedCount`，加上 commit point 落後本身。[^mg-fc-ss-islagged][^mg-fc-ss-target]

## sharding 與 balancer（簡述） {#sharding}

**機制。**sharded cluster 的資料依 shard key 分成 chunk；config server 上的 balancer 預設每 10 秒一輪，把 chunk 從多的 shard 搬到少的。[^mg-balancer-round] 搬移事件記在 `config.changelog`。

**看得到它的訊號。**mongodb_exporter 只在連到 mongos、而且開 `--compatible-mode` 時輸出 `mongodb_mongos_sharding_changelog_10min_total{event}`（近 10 分鐘的事件數）與 `mongodb_mongos_sharding_balancer_enabled`。[^mx-changelog][^mx-balancer-enabled][^mx-mongos-compat] lab 沒有 sharded cluster，這一節只讀碼；見 [sharding-balancer](../components/mongodb_exporter.md#sharding-balancer)。

## FTDC 與 exporter 讀的是同一份資料 {#ftdc}

**機制。**FTDC 每秒取樣 serverStatus、replSetGetStatus、oplog 統計等，寫到 dbPath 下的 `diagnostic.data`，目錄上限 250MB 循環覆寫（shard server 在新格式下加倍）。[^mg-ftdc-period][^mg-ftdc-dir-size][^mg-ftdc-shard-double] mongodb_exporter 的 `--collector.diagnosticdata` 執行 `getDiagnosticData` 指令拿同一份最新樣本；這個指令要 cluster 的 serverStatus 與 replSetGetStatus 權限，以及 `local.oplog.rs` 的 collStats。[^mg-diag-auth][^mg-diag-auth-oplog]

**壞了會怎樣。**exporter 帳號少了這些權限時，指令被拒，serverStatus 這一大類 metric 全部不見，但 `mongodb_up` 仍是 1。FTDC 本身不是告警來源，但事件後要分析時，`diagnostic.data` 要在持久的磁碟上、而且 250MB 能涵蓋多久要先知道。

**看得到它的訊號。**[collectors-present](../components/mongodb_exporter.md#collectors-present)；FTDC 的保留是[盲點](../targets/mongodb.md#blind-spots)。
