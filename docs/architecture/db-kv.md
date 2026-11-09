# 記憶體 KV 架構（Valkey／Redis）

Valkey 與 Redis 把所有資料放在記憶體，**所有指令在同一條主執行緒上依序執行**。這一點決定了大部分監控問題：一個慢指令、一次 fork、exporter 的一次 key 掃描，都會讓其他所有客戶端（包括 exporter 的 `INFO` 與 Sentinel 的 `PING`）一起等。這頁只寫和監控有關的五個機制，每一節是「機制 → 壞了會怎樣 → 哪個訊號看得到」。

以 Valkey 9.1.3 原始碼為準，Redis 7.2.17（最後的 BSD 分支）與 8.10.3 有差異時另外註明。<span class="at-chip at-ev-code">讀碼</span> 實驗數字來自 `lab/targets/valkey`，這台機器的容器時鐘比牆鐘慢約 30%，秒數只當量級看。[^vk-exp]

<!-- figure: db-kv-paths -->

<p class="at-caption">機制（原始碼）：客戶端、exporter、Sentinel、複本的請求都排進同一條主執行緒；fork 也在主執行緒上做，之後的寫檔由子程序負責。紅色是會讓監控失準或看不到的位置。</p>

## 單執行緒指令迴圈與 io-threads {#main-thread}

**機制。**指令的解析可以交給 io-threads，**執行一律在主執行緒**；`io-threads` 預設 1（不開）。Valkey 可以在執行中用 `CONFIG SET` 調整，Redis 7.2／8.10 只能在啟動時設定。[^vk-cfg-io-threads][^r72-cfg-io-threads][^r8-cfg-io-threads] redis_exporter 每次 scrape 依序送 `INFO ALL`、`CONFIG GET *`、`LATENCY LATEST`、`LATENCY HISTOGRAM`、`SLOWLOG`、`COMMANDLOG`，叢集再加 `CLUSTER INFO`，全部排在同一條執行緒上。[^rx-info-all][^rx-config-get][^rx-latency-latest][^rx-slowlog-len][^rx-cmdlog][^rx-cluster-info]

**壞了會怎樣。**

- 一個 7 秒的指令（`DEBUG SLEEP 7`）讓 exporter 的 `INFO` 等到指令結束。exporter 的 `--connection-timeout` 預設 15 秒，比一般的 `scrape_timeout` 長，所以是 Prometheus 先放棄：`up` 變 0、`redis_up` 跟著消失。把 `--connection-timeout` 設成 2 秒的 exporter 則在 2.2 秒回應，`up=1`、`redis_up=0`、`redis_exporter_last_scrape_error` 帶著錯誤訊息。<span class="at-chip at-ev-tested">實測</span>[^vk-exp][^rx-flag-timeout][^rx-dial-timeouts]
- 同一時間，同一個 multi-target exporter 抓的另一台（複本）不受影響：`/scrape` 每個請求建立自己的 exporter 物件。`/metrics` 模式則是整個 Collect 加鎖，同一個 exporter 的 scrape 會排隊。<span class="at-chip at-ev-tested">實測</span>[^vk-exp][^rx-scrape-handler-new][^rx-collect-lock]
- `--check-key-groups` 用 Lua script 一批一批 `SCAN` 加 `MEMORY USAGE`，每一批都是一個在主執行緒上不可中斷的 `EVAL`。[^rx-kg-scan][^rx-kg-memusage][^rx-kg-loop]

**看得到它的訊號。**[exporter-query-timeout](../components/redis_exporter.md#exporter-query-timeout)、[scrape-budget](../components/redis_exporter.md#scrape-budget)、[key-scan-cost](../components/redis_exporter.md#key-scan-cost)、[slow-commands](../components/redis_exporter.md#slow-commands)、[main-thread](../components/redis_exporter.md#main-thread)。

## maxmemory 與淘汰政策 {#maxmemory}

**機制。**每個指令執行前呼叫 `performEvictions()`；用量超過 `maxmemory` 時依 `maxmemory-policy` 挑 key 淘汰。預設 `noeviction`：直接回 `-OOM`，只拒絕會增加記憶體的指令（`GET` 照常）。`volatile-*` 只從有 TTL 的 key 挑，沒有這種 key 時和 noeviction 一樣。[^vk-oom-perform][^vk-oom-reject][^vk-evict-noeviction][^vk-evict-nothing][^vdoc-lru-volatile] 複本的輸出緩衝與 AOF 緩衝不算進用量（`mem_not_counted_for_evict`）。[^vk-evict-notcounted] 複本預設不淘汰，用量可以超過主節點。[^vdoc-repl-monitor] Valkey 另有 `import-mode`，主節點開著時不淘汰。[^vk-cfg-import-mode]

**壞了會怎樣。**實測 maxmemory 設得比用量少 2MB：

| 政策 | 寫入 | `redis_errors_total{err="OOM"}` | `redis_evicted_keys_total` |
|---|---|---|---|
| noeviction | `SET`、`INCR` 回 OOM，`GET` 正常 | 0 → 2（series 在第一次出錯時才出現） | 0 |
| volatile-lru（沒有任何 key 有 TTL） | 仍回 OOM | 3 | 0 |
| allkeys-lru | 成功 | 不變 | 0 → 32487 |

<span class="at-chip at-ev-tested">實測</span>[^vk-exp] 只看 `evicted_keys` 的規則在 noeviction 與 volatile-* 下永遠不響；只看「用量／上限」的規則在 `maxmemory` 是 0（預設）時除以 0。

**看得到它的訊號。**[maxmemory-eviction](../components/redis_exporter.md#maxmemory-eviction)、[writes-rejected](../components/redis_exporter.md#writes-rejected)、[memory-growth](../components/redis_exporter.md#memory-growth)。

## fork 持久化與 copy-on-write {#fork}

**機制。**`BGSAVE`、AOF rewrite、full resync 都靠 `fork()`。fork 本身在主執行緒上做，耗時記在 `latest_fork_usec`；之後父程序每改一頁，kernel 就複製一頁（copy-on-write），子程序結束時回報複製量 `rdb_last_cow_size`。[^vk-fork-time][^vk-info-fork-usec][^vdoc-latency-fork] 子程序寫檔失敗（目錄不能寫、磁碟滿）時 `rdb_last_bgsave_status` 變 `err`；fork 本身失敗（記憶體不夠、`vm.overcommit_memory` 沒設 1）也是。[^vk-rdb-bgsave-err][^vk-rdb-fork-fail][^vdoc-admin-overcommit]

**壞了會怎樣。**`stop-writes-on-bgsave-error` 預設 yes：最後一次 BGSAVE 失敗、又有設 `save` 時，**所有寫入指令與 `PING`** 都回 `-MISCONF`。[^vk-misconf-cond][^vk-misconf-ping][^vk-cfg-stop-writes] Redis 7.2 與 8.10 同樣把 `PING` 算進去。[^r72-misconf-ping][^r8-misconf-ping] 實測把 `/data` 改成唯讀後 `BGSAVE`：`rdb_last_bgsave_status` 0、`SET` 與 `PING` 都回 MISCONF、`redis_up` 仍是 1（exporter 不送 PING）。<span class="at-chip at-ev-tested">實測</span>[^vk-exp] Sentinel 的 PING 回覆檢查只接受 PONG、LOADING、MASTERDOWN[^vk-sen-ping-valid]；這次實驗 30 秒內 Sentinel 沒有把主節點標成 sdown，原因見[元件卡](../components/redis_exporter.md#rdb-persistence)。

latency monitor 有一個 `fork` 事件，但 `latency-monitor-threshold` 預設 0（關閉）。Valkey 用微秒比較門檻、存成毫秒；Redis 7.2 直接用毫秒。[^vk-fork-latency][^vk-lat-addsample][^vk-lat-ms][^r72-fork-latency][^r72-lat-addsample]

**看得到它的訊號。**[rdb-persistence](../components/redis_exporter.md#rdb-persistence)、[aof-persistence](../components/redis_exporter.md#aof-persistence)、[fork-latency](../components/redis_exporter.md#fork-latency)、[writes-rejected](../components/redis_exporter.md#writes-rejected)。

## 複寫 backlog 與 full resync {#replication}

**機制。**主節點把寫入串流同時送給複本並存進一個環狀 backlog（`repl-backlog-size`：Valkey 預設 10MB，Redis 7.2／8.10 預設 1MB）。[^vk-cfg-backlog][^r72-cfg-backlog][^r8-cfg-backlog] 複本斷線重連時帶著自己的 offset 送 `PSYNC`；offset 還在 backlog 範圍內就補差額（partial），不在就 full resync：主節點 fork 產生 RDB、整份傳過去，`sync_full` 與 `sync_partial_err` 各加 1。[^vk-repl-no-backlog][^vk-repl-full][^vk-repl-partial-err] 雙方各自用 `repl-timeout`（預設 60 秒）判斷對方沒回應。[^vk-repl-timeout][^vk-cfg-repl-timeout] Valkey 有 `dual-channel-replication-enabled`（預設關），Redis 8 有 `repl-rdb-channel`：full sync 時 RDB 走另一條連線。[^vk-cfg-dual-channel][^r8-cfg-rdb-channel]

**壞了會怎樣。**實測把複本從網路拔掉 25 秒、主節點期間寫 3MB（backlog 1MB）：

| 時間點 | 複本端 | 主節點端 |
|---|---|---|
| 斷線中 | `redis_master_link_up` 0、`redis_master_last_io_seconds_ago` **-1**、INFO 的 `master_link_down_since_seconds` 18（exporter 不輸出） | `redis_connected_slaves` 0，`redis_connected_slave_lag_seconds` 與 offset 整條消失 |
| 接回 | 「Trying a partial resynchronization」→「Full resync from primary」 | 「Unable to partial resync … for lack of backlog」；`sync_full` 1 → 2、`sync_partial_err` 0 → 1 |

<span class="at-chip at-ev-tested">實測</span>[^vk-exp] 斷線時延遲類的 series 消失或變 -1，「延遲 > N」的規則回傳空。INFO 裡每台複本那一行的 `lag` 是「距上次 REPLCONF ACK 幾秒」，不是資料落後多久。[^vk-info-slave-lag][^rx-slave-lag]

**看得到它的訊號。**[replication-link](../components/redis_exporter.md#replication-link)、[replication-lag](../components/redis_exporter.md#replication-lag)、[full-resync](../components/redis_exporter.md#full-resync)。

## Sentinel 與 Cluster 的 failover {#failover}

**Sentinel。**每個 Sentinel 每秒 PING 主節點；超過 `down-after-milliseconds` 沒有合格回覆就標 sdown，同意的 Sentinel 數達到 `quorum` 時變 odown，再由多數 Sentinel 授權其中一個執行 failover，`config-epoch` 加 1。[^vk-sen-sdown][^vk-sen-odown][^vk-sen-quorum][^vk-sen-switch] `SENTINEL CKQUORUM` 同時檢查兩個門檻（quorum 與多數），官方文件建議監控系統用它。[^vk-sen-ckquorum][^vdoc-sentinel-ckquorum] 程序被暫停或時鐘跳動超過 2 秒時進入 TILT：照常收資料，但不做任何動作。[^vk-sen-tilt][^vdoc-sentinel-tilt]

**Cluster。**沒有外部協調者：節點之間用 gossip 互相標 PFAIL、FAIL，`cluster-require-full-coverage` 預設 yes 時只要有一個 slot 沒人負責，整個節點回報 `cluster_state:fail` 並拒絕服務；節點發現自己在少數派時也是 fail。[^vk-cl-full-coverage][^vk-cl-minority][^vk-cl-info-state][^vk-cfg-full-coverage] Valkey 9.1 的 `INFO ALL` 多了 `# Cluster Info` 段，redis_exporter 跳過它，照樣另送 `CLUSTER INFO`。[^vk-info-cluster-info][^rx-skip-cluster-section]

**壞了會怎樣。**failover 之後，靜態的 role label 指錯節點；舊主節點回來時，在 Sentinel 把它降級之前兩台都回報 `role:master`。Sentinel 失去多數時主節點壞掉也不會 failover，而主節點與複本本身都正常。實測見元件卡的 [role-detection](../components/redis_exporter.md#role-detection) 與 [sentinel-quorum](../components/redis_exporter.md#sentinel-quorum)。

**看得到它的訊號。**[role-detection](../components/redis_exporter.md#role-detection)、[sentinel-quorum](../components/redis_exporter.md#sentinel-quorum)、[cluster-state](../components/redis_exporter.md#cluster-state)。
