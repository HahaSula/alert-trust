# Valkey／Redis 怎麼監控

**要監控的是：資料還寫得進去、讀得到最新的、斷掉時有人接手。**對記憶體 KV 來說，這三件事最常壞在五個地方：記憶體碰到 `maxmemory`、持久化（fork）失敗、複本斷線後 full resync、failover 沒發生或發生兩次、主執行緒被一個慢指令卡住。這頁依九個面向從大到小排列（叢集 → 複寫 → 可用性 → 資源 → 工作量 → 維護 → 保護 → 容量），最後是工具設定與缺口。

[元件卡](../components/redis_exporter.md)列出每個訊號與規則；[原始碼到 metric](valkey-appendix.md)追每個 INFO 欄位；機制見[記憶體 KV 架構](../architecture/db-kv.md)。

版本：Valkey 9.1.3（主要）、Redis 7.2.17（最後的 BSD 授權分支）與 8.10.3（看差異）、redis_exporter v1.97.0。依據是原始碼，加上兩輪小實驗：Valkey 主從加 3 個 Sentinel、Redis 7.2 與 8.10 各一台、每台一個 exporter，製造慢指令、阻塞、maxmemory、exporter 逾時、連線額滿、複本斷線、BGSAVE 失敗、failover、Sentinel 失去多數、script 失敗、key group 掃描。<span class="at-chip at-ev-tested">實測</span>[^vk-exp][^vk-exp2] 這台機器的容器時鐘比牆鐘慢約 30%、而且會跳動：Sentinel 每 25～30 秒進一次 TILT，**failover 與 sdown 在這裡測不到**，相關結論以原始碼為準並標「讀碼」。

## 先講結論

1. **只看 `redis_up` 的規則在最需要時不響。**mixin 與 awesome 的 RedisDown 都是 `redis_up == 0`。exporter 停機時 `redis_up` 消失；伺服器忙到 `INFO` 回不來時，exporter 的 `--connection-timeout` 預設 15 秒比 `scrape_timeout` 長，Prometheus 先放棄，`redis_up` 也消失。要加 `up == 0` 與 `absent()`，並把 `--connection-timeout` 設得比 `scrape_timeout` 短。<span class="at-chip at-ev-tested">實測</span>[^vk-exp][^rx-flag-timeout]
2. **`redis_up == 0` 不一定是資料庫的問題。**`--script` 出錯時 exporter 把 `redis_up` 設成 0，其他 metric 照常輸出；maxclients 額滿時 exporter 自己也被拒。先看 `redis_exporter_last_scrape_error` 的 `err` label。<span class="at-chip at-ev-tested">實測</span>[^vk-exp][^rx-lua-err-up]
3. **寫入被拒時伺服器看起來完全正常。**noeviction 下碰到 `maxmemory`，`SET` 回 OOM、`GET` 照常，`redis_up` 是 1、`evicted_keys` 是 0；`volatile-*` 在沒有 TTL key 時一樣。BGSAVE 失敗後寫入與 `PING` 回 MISCONF，exporter 不送 PING 所以照樣成功。要看 `redis_errors_total{err=~"OOM|MISCONF"}`，而且它在第一次出錯時才出現。<span class="at-chip at-ev-tested">實測</span>[^vk-exp][^vk-oom-reject][^vk-misconf-ping]
4. **複本斷線時延遲 metric 不是變大，而是消失或變 -1。**複本端 `master_last_io_seconds_ago` 是 -1，主節點端那台複本的 lag 與 offset 整條消失。斷了多久在 INFO 的 `master_link_down_since_seconds`，exporter 不輸出（用 `--script` 補）。Valkey／Redis 沒有「以秒計的資料延遲」，要靠 heartbeat key。<span class="at-chip at-ev-tested">實測</span>[^vk-exp][^vk-exp2][^vk-info-link-down-since]
5. **斷線期間寫入超過 backlog 就 full resync。**實測 25 秒斷線、寫 3MB、backlog 1MB：`sync_full` 1 → 2。Valkey 的 `repl-backlog-size` 預設 10MB，Redis 7.2／8.10 是 1MB。<span class="at-chip at-ev-tested">實測</span>[^vk-exp][^vk-cfg-backlog][^r72-cfg-backlog]
6. **Sentinel 在 TILT 時不會 failover，而它回報的主節點狀態仍是 ok。**主節點被 pause 45 秒，`redis_sentinel_master_status` 一直是 1。要另外看 `redis_sentinel_tilt` 與 `SENTINEL CKQUORUM`。<span class="at-chip at-ev-tested">實測</span>[^vk-exp2][^vk-sen-tilt]
7. **exporter 的 key 掃描在伺服器主執行緒上跑。**`--check-key-groups` 在 40 萬個 key 時讓一次 scrape 從 0.01 秒變 1.1 秒，每 1000 個 key 一個 1.2～2ms 的 EVALSHA，slowlog 128 筆全被它佔滿。<span class="at-chip at-ev-tested">實測</span>[^vk-exp][^rx-kg-scan]
8. **上游規則有三個用不到的地方。**mixin 與 awesome 的 OutOfMemory 用 `redis_total_system_memory_bytes`（要 `--include-system-metrics`，預設不輸出）；awesome 的主節點計數不依 cluster 分組；兩者都沒有持久化失敗、Sentinel、full resync 的規則。<span class="at-chip at-ev-tested">實測</span>[^vk-exp][^rx-mixin-oom][^awe-missing-master]

## 監控模型與官方建議 {#models}

Valkey 與 Redis OSS 都**沒有內建的 Prometheus endpoint**（9.1.3 與 8.10.3 的 `src/` 裡沒有 HTTP 或 Prometheus 輸出）；伺服器自己提供的是一組文字指令（INFO 的每一段由 `server.c` 組出來），所有外部工具都是把這些指令的輸出轉成 metric。<span class="at-chip at-ev-code">讀碼</span>[^vk-info-errorstat]

| 模型 | 來源 | 看得到 | 看不到 | 在這頁的角色 |
|---|---|---|---|---|
| redis_exporter（v1.97.0） | `INFO ALL`、`CONFIG GET`、`LATENCY`、`SLOWLOG`／`COMMANDLOG`、`CLUSTER INFO`、`SENTINEL MASTERS`／`CKQUORUM`[^rx-info-all][^rx-config-get][^rx-sen-ckquorum] | INFO 幾乎全部欄位、持久化狀態、複寫連線與 offset、Sentinel 與 Cluster 彙總、per-command 延遲直方圖 | 少數 INFO 欄位（斷線多久、min-replicas、total_forks）、per-node cluster 狀態、Sentinel 的 TILT 次數 | **基礎模型**：唯一同時涵蓋持久化、複寫、Sentinel、Cluster 的 Prometheus 工具，有 multi-target 與 `--script` 擴充 |
| OpenTelemetry Collector redisreceiver（v0.165.0） | `INFO all`、`CLUSTER INFO`[^otel-info-all] | 記憶體、連線、命中、fork 時間、複本數[^otel-latest-fork][^otel-slaves] | `rdb_last_bgsave_status`、`aof_last_write_status`、`master_link_status` 都沒有對應的 metric；role 與 cluster 預設關閉[^otel-role][^otel-cluster-state] | 已經用 OTel 管線時的替代；持久化與複寫要另外補 |
| 伺服器原生指令 | `INFO`、`LATENCY DOCTOR／LATEST`（`latency-monitor-threshold` 預設 0）、`SLOWLOG`／`COMMANDLOG`、`INFO commandstats`／`latencystats`／`errorstats`[^vk-cfg-latency-threshold][^vk-lat-latest] | 一切的原始資料 | 歷史（slowlog 只留最近 128 筆）、跨節點比較 | exporter 讀的就是它；排查時直接看 |
| Sentinel 自報 | `SENTINEL MASTER`、`CKQUORUM`、`INFO sentinel`、pub/sub 事件（`+sdown`、`+switch-master`、`+tilt`）[^vk-sen-ckquorum][^vk-sen-switch] | 主節點狀態、quorum、failover 次數（config-epoch） | 自己在 TILT 時不判斷，status 不變 | exporter 指向每台 Sentinel 的 26379 抓；事件流補 TILT 次數 |
| Cluster 自報 | `CLUSTER INFO`、`CLUSTER NODES`、`CLUSTER SLOT-STATS`（Valkey；exporter 的 `--include-cluster-slot-stats`）[^vk-cl-info-state][^rx-slot-stats] | slot 覆蓋、節點 PFAIL／FAIL | 每個節點只看得到自己的觀點 | exporter 抓每個節點的 `CLUSTER INFO`；per-node 狀態是缺口 |
| 外部探測 | blackbox 的 TCP／redis 模組、應用端延遲 | 從客戶端角度的可連線性 | 內部狀態 | exporter 停機或被額滿擋住時的第二條路 |

**官方建議監控什麼**（valkey-doc 9.1.3、Redis 7.2.17 的 redis.conf）：

| 建議 | 出處 | 對應的訊號 |
|---|---|---|
| 明確設定 `maxmemory`，留給碎片與 overhead 的空間 | admin.md[^vdoc-admin-maxmemory] | [maxmemory-eviction](../components/redis_exporter.md#maxmemory-eviction)、`ValkeyMaxmemoryUnset` |
| `vm.overcommit_memory = 1`，否則 BGSAVE 的 fork 可能失敗 | admin.md[^vdoc-admin-overcommit] | [rdb-persistence](../components/redis_exporter.md#rdb-persistence)；設定本身是盤點項目 |
| 主節點要有持久化，或不要自動重啟（否則複本被清空） | admin.md[^vdoc-admin-persistence] | [restart](../components/redis_exporter.md#restart)；設定是[盲點](#blind-spots) |
| 監控複本的記憶體：複本預設不淘汰 | replication.md[^vdoc-repl-monitor] | [maxmemory-eviction](../components/redis_exporter.md#maxmemory-eviction) |
| 用 `SENTINEL CKQUORUM` 監控 Sentinel 部署 | sentinel.md[^vdoc-sentinel-ckquorum] | [sentinel-quorum](../components/redis_exporter.md#sentinel-quorum) |
| 需要時打開 latency monitor（`CONFIG SET latency-monitor-threshold 100`）；fork 時間看 `latest_fork_usec` | latency-monitor.md、latency.md[^vdoc-latmon-enable][^vdoc-latency-fork] | [slow-commands](../components/redis_exporter.md#slow-commands)、[fork-latency](../components/redis_exporter.md#fork-latency) |
| `stop-writes-on-bgsave-error yes` 讓持久化失敗「被看見」 | redis.conf（7.2.17）[^r72-conf-stop-writes] | [writes-rejected](../components/redis_exporter.md#writes-rejected) |

**為什麼以 redis_exporter 為基礎。**它讀的指令最完整（INFO 全部段落加上 CONFIG、LATENCY、SLOWLOG、Sentinel、Cluster），把 `ok`／`err` 轉成 1／0，能用 `--script` 補 INFO 裡缺的欄位，也能用 `--check-single-keys` 做 heartbeat；OTel receiver 缺持久化與複寫連線狀態，正好是這頁最重要的兩個面向。<span class="at-chip at-ev-code">讀碼</span>[^rx-okerr][^rx-readme-script] 其他模型補的缺口：Sentinel 的事件流補 TILT 次數，blackbox 補「exporter 本身被擋住」，node_exporter 補 overcommit、swap、磁碟。

### 九個面向一覽 {#aspects}

| 面向 | 這頁的重點 | 元件卡的訊號 | 證據 |
|---|---|---|---|
| [叢集與共識](#cluster) | Sentinel 的 quorum 與 TILT、Cluster 的 slot | sentinel-quorum、cluster-state（`applies_when`） | <span class="at-chip at-ev-tested">實測</span>／<span class="at-chip at-ev-code">讀碼</span> |
| [複寫與 HA](#replication) | 斷線、延遲、full resync、角色 | replication-link、replication-lag、full-resync、role-detection | <span class="at-chip at-ev-tested">實測</span> |
| [可用性](#availability) | 連得上、寫得進去、重啟 | db-up、writes-rejected、restart | <span class="at-chip at-ev-tested">實測</span> |
| [資源與飽和](#saturation) | maxmemory、碎片、連線、阻塞、主執行緒 | maxmemory-eviction、fragmentation、connections、blocked-clients、main-thread | <span class="at-chip at-ev-tested">實測</span>／<span class="at-chip at-ev-code">讀碼</span> |
| [工作量](#workload) | 慢指令、延遲尖峰、命中率 | slow-commands、keyspace-hit-ratio | <span class="at-chip at-ev-tested">實測</span> |
| [資料維護](#maintenance) | 過期清理跟不上 | expire-cycle | <span class="at-chip at-ev-inferred">推論</span> |
| [資料保護](#protection) | RDB、AOF、fork | rdb-persistence、aof-persistence、fork-latency | <span class="at-chip at-ev-tested">實測</span>／<span class="at-chip at-ev-code">讀碼</span> |
| [容量](#capacity) | 記憶體與 key 的成長 | memory-growth | <span class="at-chip at-ev-inferred">推論</span> |
| [監測自檢](#exporter) | exporter 逾時、CONFIG 消失、script、key 掃描成本 | exporter-up、collectors-present、collector-success、scrape-budget、probe-blast-radius、exporter-query-timeout、key-scan-cost、version-semantics | <span class="at-chip at-ev-tested">實測</span> |

## 叢集與共識 {#cluster}

**結論：只有 Sentinel 或 Cluster 模式才有「誰決定主節點」可以監控。**單機或手動主從沒有任何故障偵測，元件卡把 cluster 面向記在 `not_applicable_aspects`，主節點問題由[複寫](#replication)的 role-detection 回答。

### Sentinel {#sentinel}

每台 Sentinel 一個 exporter（`--redis.addr=redis://sentinel:26379`）；exporter 看到 INFO 裡有 `# Sentinel` 段才送 `SENTINEL MASTERS`、`CKQUORUM`、`SENTINELS`、`SLAVES`。[^rx-sen-section][^rx-sen-ckquorum]

| 要看什麼 | Sentinel 的來源 | metric | 證據 |
|---|---|---|---|
| failover 還做得到嗎 | `SENTINEL CKQUORUM`：可用的 Sentinel 數同時要 ≥ quorum 與 ≥ 多數[^vk-sen-quorum][^vk-sen-ckquorum] | `redis_sentinel_master_ckquorum_status`（`message` label 是回覆內容） | <span class="at-chip at-ev-code">讀碼</span> |
| 健康的 Sentinel 數 | `SENTINEL SENTINELS` 中沒有 s_down／o_down 的，加上自己 | `redis_sentinel_master_ok_sentinels`（從 1 開始數）[^rx-sen-oksentinels-init] | <span class="at-chip at-ev-code">讀碼</span> |
| 主節點狀態 | INFO sentinel 的 `status=ok／sdown／odown` | `redis_sentinel_master_status` | <span class="at-chip at-ev-tested">實測</span> |
| 發生過 failover | `config-epoch` 每次 failover 加 1 | `redis_sentinel_master_config_epoch` | <span class="at-chip at-ev-code">讀碼</span> |
| TILT | 兩次 timer 間隔為負或超過 2 秒[^vk-sen-tilt] | `redis_sentinel_tilt`；`sentinel_total_tilt` 只在 INFO[^vk-sen-info-tilt] | <span class="at-chip at-ev-tested">實測</span> |

實驗：這台機器上 Sentinel 每 25～30 秒進一次 TILT（log 有 5 次 `+tilt`）。主節點被 pause 45 秒期間 `redis_sentinel_tilt` 是 1、`master_status` 一直是 ok、config-epoch 0、沒有 failover；停掉 3 台中的 2 台 25 秒，CKQUORUM 仍回「OK 3 usable Sentinels」。這證明了一件事——**TILT 中的 Sentinel 不更新 status**——但 sdown、odown、failover 本身在這裡測不到。<span class="at-chip at-ev-tested">實測</span>[^vk-exp][^vk-exp2]

### Valkey Cluster {#valkey-cluster}

exporter 在 INFO 有 `cluster_enabled:1` 時送 `CLUSTER INFO`，`cluster_` 開頭的欄位原名輸出（`cluster_state:ok` → 1）。[^rx-cluster-info][^rx-cluster-prefix][^rx-okerr] `cluster-require-full-coverage` 預設 yes：任一 slot 沒人負責或自己在少數派時，節點回報 fail 並拒絕服務。[^vk-cl-full-coverage][^vk-cl-minority] 每個節點只看得到自己的觀點，所以每個節點都要抓（`--is-cluster` 的 `/discover-cluster-nodes` 可產生 target）。這組只讀碼，沒有實驗。<span class="at-chip at-ev-code">讀碼</span>

## 複寫與 HA {#replication}

| 要看什麼 | 伺服器的來源 | exporter metric | 斷線時 | 證據 |
|---|---|---|---|---|
| 連線狀態 | 複本 INFO `master_link_status`[^vk-info-link-status] | `redis_master_link_up{master_host, master_port}`[^rx-link-up] | 0 | <span class="at-chip at-ev-tested">實測</span> |
| 最後收到資料 | 複本 INFO `master_last_io_seconds_ago`[^vk-info-last-io] | `redis_master_last_io_seconds_ago` | **-1** | <span class="at-chip at-ev-tested">實測</span> |
| 斷了多久 | 複本 INFO `master_link_down_since_seconds`（只在斷線時出現）[^vk-info-link-down-since] | 不輸出；`valkey-extra.lua` → `redis_script_values{key="master_link_down_since_seconds"}` | 14～18 | <span class="at-chip at-ev-tested">實測</span> |
| 位元組延遲 | 主節點 `master_repl_offset` − `slaveN:offset` | `redis_master_repl_offset`、`redis_connected_slave_offset_bytes{slave_ip}` | 那一列消失 | <span class="at-chip at-ev-tested">實測</span> |
| ACK 間隔 | `slaveN:lag` ＝ 現在 − 上次 REPLCONF ACK[^vk-info-slave-lag] | `redis_connected_slave_lag_seconds` | 那一列消失 | <span class="at-chip at-ev-tested">實測</span> |
| 秒數延遲 | 沒有；用 heartbeat key | `time() - redis_key_value{key="at:heartbeat"}`（複本端 `--check-single-keys`） | 25 秒（斷 20 秒） | <span class="at-chip at-ev-tested">實測</span> |
| full resync | `sync_full`、`sync_partial_err`[^vk-repl-full][^vk-repl-partial-err] | `redis_replica_resyncs_full`、`redis_replica_partial_resync_denied` | 接回後 +1 | <span class="at-chip at-ev-tested">實測</span> |
| backlog | `repl_backlog_size`、`repl_backlog_histlen` | `redis_replication_backlog_bytes`、`redis_repl_backlog_history_bytes` | — | <span class="at-chip at-ev-code">讀碼</span> |
| 角色 | INFO `role`（`primary_host` 為空就是 master）[^vk-info-role] | `redis_instance_info{role}` | — | <span class="at-chip at-ev-tested">實測</span> |

- **斷線與 full resync。**複本從網路拔掉 25 秒、主節點寫 3MB：複本 log「PRIMARY timeout: no data nor PING received」→ 接回後「Trying a partial resynchronization」→「Full resync from primary」；主節點 log「Unable to partial resync … for lack of backlog」。`redis_connected_slaves` 0 → 1、`sync_full` 1 → 2、`sync_partial_err` 0 → 1。同一段時間 Sentinel 的 `ok_slaves` 仍是 1（TILT 干擾，見上）。<span class="at-chip at-ev-tested">實測</span>[^vk-exp][^vk-repl-no-backlog][^vk-repl-timeout]
- **複本重啟不一定 full resync。**複本載入自己的 RDB（帶著 replid）後送 PSYNC，partial 成功，主節點 `sync_full` 不變。<span class="at-chip at-ev-tested">實測</span>[^vk-exp2]
- **heartbeat 是唯一的秒數延遲。**主節點每秒 `SET at:heartbeat <伺服器時間>`，複本端 exporter 讀它：平常 2.5～3.2 秒（含最多一個 `scrape_interval` 的誤差），複本斷線 20 秒時 25 秒，接回後 3.2 秒。<span class="at-chip at-ev-tested">實測</span>[^vk-exp2]
- **角色計數要分組、要去重。**依 `cluster` label 計數 `role="master"`，再依 `run_id` 去重：實驗裡 vk1 被三個 job 抓，沒去重的 count 是 3。<span class="at-chip at-ev-tested">實測</span>[^vk-exp]

## 可用性 {#availability}

- **連得上：**`redis_up == 0` 搭配 `up == 0` 與 `absent()`。`redis_up` 的原因看 `err` label（見[監測自檢](#exporter)）。<span class="at-chip at-ev-tested">實測</span>[^vk-exp]
- **寫得進去：**`redis_errors_total{err=~"OOM|MISCONF|NOREPLICAS|READONLY"}`。來源是 INFO errorstats，只列出現過的錯誤前綴；series 在第一次出錯時才建立，`increase()` 看不到第一筆，規則 `ValkeyWritesRejected` 用 `unless … offset 5m` 補。每個指令另有 `redis_commands_rejected_calls_total{cmd}`（實測 OOM 時 `set` 與 `incr` 各 1）。<span class="at-chip at-ev-tested">實測</span>[^vk-exp][^vk-info-errorstat][^rx-errorstats][^rx-cmd-rejected]
- **重啟：**`redis_uptime_in_seconds < 300`；載入期間 `redis_loading_dump_file == 1`。`redis_start_time_seconds` 是 exporter 用 now − uptime 算的，實測 2 分鐘內沒有跳動。<span class="at-chip at-ev-tested">實測</span>[^vk-exp2]

## 資源與飽和 {#saturation}

### maxmemory 與淘汰

實測 maxmemory 設得比用量少 2MB：<span class="at-chip at-ev-tested">實測</span>[^vk-exp]

| 政策 | 寫入 | `redis_errors_total{err="OOM"}` | `redis_evicted_keys_total` | 只看 evicted 的規則 |
|---|---|---|---|---|
| noeviction（預設） | `SET`、`INCR` 回 OOM，`GET` 正常 | 0 → 2 | 0 | 不響 |
| volatile-lru，沒有任何 key 有 TTL | 仍回 OOM | 3 | 0 | 不響 |
| allkeys-lru | 成功 | 不變 | 32487 | 響 |

- 比例 `redis_memory_used_bytes / redis_memory_max_bytes` 只在 `maxmemory > 0` 時有意義；預設 0。<span class="at-chip at-ev-tested">實測</span>[^vk-exp]
- 複本預設不淘汰（`replica-ignore-maxmemory` 預設 yes），用量可以超過主節點；要分開監控複本的記憶體。實驗的填充資料用 `DEBUG POPULATE`（不會複寫），所以複本端的用量沒有對照意義。<span class="at-chip at-ev-code">讀碼</span>[^vk-cfg-replica-ignore-maxmemory][^vdoc-repl-monitor]
- 淘汰在每個指令前進行；超過上限的累積時間在 `redis_eviction_exceeded_time_ms_total`（實測 20 秒）。<span class="at-chip at-ev-tested">實測</span>[^vk-info-eviction-exceeded]

### 碎片、連線、阻塞、主執行緒

| 要看什麼 | metric | 注意 | 證據 |
|---|---|---|---|
| 碎片 | `redis_mem_fragmentation_ratio`（RSS／used） | 資料量小時比例沒意義，同時看絕對量；< 1 是 swap | <span class="at-chip at-ev-code">讀碼</span>[^vk-info-frag] |
| 連線 | `redis_connected_clients` ／ `redis_config_maxclients`、`redis_rejected_connections_total` | 額滿時 exporter 也被拒；實測額滿 15 秒期間兩個 metric 都沒有資料，結束後 rejected 一次跳到 21 | <span class="at-chip at-ev-tested">實測</span>[^vk-exp][^vk-maxclients-reject] |
| 阻塞 | `redis_blocked_clients`、`redis_total_blocking_keys` | 一個 BLPOP：兩者都是 1；佇列型應用平常就有 | <span class="at-chip at-ev-tested">實測</span>[^vk-exp] |
| 主執行緒 | `rate(redis_cpu_user_main_thread_seconds_total + …sys…)` | 接近 1 核就是上限；io-threads 只分擔網路 | <span class="at-chip at-ev-code">讀碼</span>[^vk-cfg-io-threads] |
| output buffer 斷線 | `increase(redis_client_output_buffer_limit_disconnections_total[10m]) > 0`（ValkeyOutputBufferDisconnects） | 不分 client 類型，對照 `redis_mem_clients_slaves`；Redis 7.2 沒有這個欄位 | <span class="at-chip at-ev-code">讀碼</span>[^res-vk-cobl-incr][^res-rx-cobl] |

swap、RSS 對 cgroup 上限與 OOM kill 不在 INFO 裡，已由 [主機與容器資源](../components/host.md) 補上（規則只讀碼，host lab 之後排程）；overcommit 與 THP 設定仍要盤點。

## 工作量 {#workload}

| 要看什麼 | Valkey 9.1 | Redis 7.2 | Redis 8.10 | metric |
|---|---|---|---|---|
| slowlog | commandlog（`commandlog-execution-slower-than`，`slowlog-*` 是別名），另有 large-request／large-reply[^vk-cfg-commandlog-slower] | `slowlog-log-slower-than`[^r72-cfg-slowlog] | 同 7.2，INFO 另有 `slowlog_commands_*` | `redis_slowlog_last_id`、`redis_slowlog_length`、`redis_commandlog_*`、`redis_slowlog_commands_total`（8.8 起） |
| latency monitor | 預設關；用微秒比較門檻、存成毫秒[^vk-lat-addsample][^vk-lat-ms] | 預設關；毫秒[^r72-lat-addsample] | 同 7.2 | `redis_latency_spike_duration_seconds{event_name}` |
| per-command 延遲 | `latency-tracking` 預設開[^vk-cfg-latency-tracking] | 同 | 同 | `redis_commands_latencies_usec`（histogram）、`redis_latency_percentiles_usec` |
| 命中率 | `keyspace_hits`／`misses`[^vk-info-hits] | 同 | 同 | `redis_keyspace_hits_total`、`redis_keyspace_misses_total` |

實測 `DEBUG SLEEP 0.3`：Valkey 與 Redis 7.2 都是 slowlog last_id 3、`redis_last_slow_execution_duration_seconds` 0.30、`redis_latency_spike_duration_seconds{event_name="command"}` 0.3。Valkey 的 `LATENCY LATEST` 每個事件回 6 欄（多了 sum、cnt），Redis 7.2 回 4 欄，exporter 只讀前 4 欄，兩邊結果相同。<span class="at-chip at-ev-tested">實測</span>[^vk-exp][^vk-lat-latest][^rx-latency-ms]

slowlog 只記執行時間，不含排隊；exporter 自己的 key group EVALSHA 也會進 slowlog，把應用的條目擠掉（見[監測自檢](#exporter)）。

## 資料維護 {#maintenance}

記憶體 KV 沒有 vacuum；最接近的是過期清理。主動過期每輪有時間上限，`redis_expired_time_cap_reached_total` 增加且 `redis_expired_stale_percentage` 偏高時，過期 key 還佔著記憶體，會提早觸發淘汰或 OOM。只讀 exporter 的對照表，沒有實驗。<span class="at-chip at-ev-inferred">推論</span>

## 資料保護 {#protection}

| 要看什麼 | 伺服器的來源 | metric | 證據 |
|---|---|---|---|
| RDB 成敗 | `rdb_last_bgsave_status`：子程序寫檔失敗或 fork 失敗時 err[^vk-rdb-bgsave-err][^vk-rdb-fork-fail][^vk-info-bgsave-status] | `redis_rdb_last_bgsave_status`（ok → 1）[^rx-map-bgsave] | <span class="at-chip at-ev-tested">實測</span> |
| 最後成功時間 | `rdb_last_save_time`（只在成功時更新） | `redis_rdb_last_save_timestamp_seconds` | <span class="at-chip at-ev-tested">實測</span> |
| AOF | `aof_last_write_status`（寫入與背景 fsync 都成功才是 ok）、`aof_last_bgrewrite_status`[^vk-info-aof-write-status][^vk-info-aof-rewrite-status] | `redis_aof_last_write_status`、`redis_aof_last_bgrewrite_status`、`redis_aof_delayed_fsync` | <span class="at-chip at-ev-code">讀碼</span> |
| fork | `latest_fork_usec`、`rdb_last_cow_size`、`total_forks`[^vk-info-fork-usec][^vk-info-total-forks] | `redis_latest_fork_seconds`（除以 1e6）[^rx-fork-seconds]、`redis_rdb_last_cow_size_bytes`；`total_forks` 用 script 補 | <span class="at-chip at-ev-tested">實測</span> |

- **BGSAVE 失敗會停掉寫入。**把 `/data` 改成唯讀後 `BGSAVE`：`redis_rdb_last_bgsave_status` 0、`SET` 與 `PING` 都回「MISCONF Valkey is configured to save RDB snapshots…」、`redis_errors_total{err="MISCONF"}` 11 秒內 29 次（Sentinel 每秒的 PING）、`redis_up` 仍是 1。恢復權限再 BGSAVE 後 status 回到 1、fork 0.000445 秒、COW 404KB。<span class="at-chip at-ev-tested">實測</span>[^vk-exp][^vk-misconf-cond][^vk-misconf-ping]
- **MISCONF 會不會觸發 failover：讀碼是會，這裡沒驗證到。**Sentinel 只把 PONG、LOADING、MASTERDOWN 當成正常回覆[^vk-sen-ping-valid]，持續回 MISCONF 超過 `down-after-milliseconds` 應該標 sdown[^vk-sen-sdown]。實驗中 30 秒內 `last-ok-ping-reply` 長到 44 秒而 flags 一直是 master，同一段時間附近 Sentinel 反覆 TILT，無法排除干擾。<span class="at-chip at-ev-code">讀碼</span>[^vk-exp2]
- **`save ""` 的實例**：status 永遠是初始值 ok，timestamp 是啟動時間；`ValkeyRdbStale` 要排除這類實例。主節點沒有持久化又會自動重啟時，重啟後的空資料會同步到所有複本。<span class="at-chip at-ev-code">讀碼</span>[^vdoc-admin-persistence]

## 容量 {#capacity}

`predict_linear(redis_memory_used_bytes[6h], 24h)` 對照 `redis_memory_max_bytes`；key 數看 `redis_db_keys`（只輸出有 key 的 db）。哪一類 key 在長，要靠 key group（成本見下一節）或離線分析 RDB。<span class="at-chip at-ev-inferred">推論</span>

## 監測自檢 {#exporter}

exporter 本身的訊號都在[元件卡](../components/redis_exporter.md)。和 Valkey 直接相關的四點：

1. **伺服器忙，exporter 就等。**`DEBUG SLEEP 7` 期間：預設 exporter 的 HTTP 回應花 9.5 秒（主機牆鐘），Prometheus 在 `scrape_timeout` 4 秒放棄（`up=0`）；`--connection-timeout=2s` 的 exporter 2.2 秒回應，`up=1`、`redis_up=0`、`err="… use of closed network connection"`。同一個 multi-target exporter 抓的複本不受影響。<span class="at-chip at-ev-tested">實測</span>[^vk-exp][^rx-scrape-handler-new]
2. **CONFIG 沒權限時不會報錯。**ACL 拿掉 `config`：`redis_up=1`、`last_scrape_error=0`，所有 `redis_config_*` 消失（sample 554 → 530）。`ValkeyConfigMetricsMissing` 實測會響。<span class="at-chip at-ev-tested">實測</span>[^vk-exp2][^rx-config-get-silent]
3. **`--script` 出錯讓 `redis_up` 變 0。**`redis_script_result` 0、err 是 script 的錯誤訊息，其他 450 個 sample 照常。<span class="at-chip at-ev-tested">實測</span>[^vk-exp][^rx-lua-err-up]
4. **key 掃描的成本。**`--check-key-groups` 在 40 萬個 key 時：scrape 0.01 → 1.1 秒、`redis_last_key_groups_scrape_duration_milliseconds` 928、2648 次 EVALSHA 平均 1.2ms、160 萬次 MEMORY USAGE、slowlog（門檻 1ms）128 筆全是 exporter 的 EVALSHA。`--skip-checkkeys-for-role-master` 不管 key group。<span class="at-chip at-ev-tested">實測</span>[^vk-exp][^rx-kg-loop][^rx-flag-skip-master]

### 版本差異：Valkey 9.1 與 Redis 7.2／8.10 {#divergence}

| 項目 | Valkey 9.1.3 | Redis 7.2.17 | Redis 8.10.3 | 對監控的影響 | 證據 |
|---|---|---|---|---|---|
| 版本欄位 | `redis_version:7.2.4`（固定）＋`valkey_version:9.1.3` | `redis_version:7.2.17` | `redis_version:8.10.3` | 依版本分支的規則用 `valkey_version` label | <span class="at-chip at-ev-tested">實測</span>[^vk-info-redis-version] |
| `repl-backlog-size` 預設 | 10MB | 1MB | 1MB | Redis 的 full resync 門檻低 10 倍 | <span class="at-chip at-ev-tested">實測</span>[^vk-cfg-backlog][^r72-cfg-backlog][^r8-cfg-backlog] |
| slowlog | commandlog（`slowlog-*` 是別名）＋large-request／reply | slowlog | slowlog＋INFO `slowlog_commands_*`（8.8 起） | 名稱不同：`redis_commandlog_*` 只在 Valkey | <span class="at-chip at-ev-tested">實測</span>[^vk-cfg-commandlog-slower][^rx-cmdlog] |
| `io-threads` | 執行中可改 | 只能啟動時設 | 只能啟動時設，另有 per-thread 統計 | `redis_io_thread_*` 只在 Redis 8 | <span class="at-chip at-ev-code">讀碼</span>[^vk-cfg-io-threads][^r72-cfg-io-threads][^r8-cfg-io-threads] |
| full sync 另開連線 | `dual-channel-replication-enabled`（預設關） | 無 | `repl-rdb-channel`（預設開，隱藏設定） | 複本行數多一種 `type=rdb-channel` | <span class="at-chip at-ev-code">讀碼</span>[^vk-cfg-dual-channel][^r8-cfg-rdb-channel][^vk-info-slave-line] |
| key 大小分布 | 無 | 無 | INFO KEYSIZES（`--include-keysizes-metrics`） | Valkey 只能用 key group 掃 | <span class="at-chip at-ev-code">讀碼</span>[^r8-info-keysizes][^rx-flag-keysizes] |
| INFO ALL 內的 Cluster 段 | 有（9.1 起） | 無 | 無 | exporter 跳過它，另送 CLUSTER INFO | <span class="at-chip at-ev-code">讀碼</span>[^vk-info-cluster-info][^rx-skip-cluster-section] |
| 淘汰計數的位置 | evict.c | evict.c | db.c | 同一個 INFO 欄位 | <span class="at-chip at-ev-code">讀碼</span>[^vk-evict-count][^r8-evict-count] |
| import-mode | 有（主節點不淘汰） | 無 | 無 | 開著時 noeviction 以外的政策也不淘汰 | <span class="at-chip at-ev-code">讀碼</span>[^vk-cfg-import-mode] |
| 預設設定下多出的 metric 名稱 | 比 7.2 多 36 個（errors_total、tls_*、scripting_engine_*…） | — | 比 7.2 多 26 個（active_clients、io_thread_*…） | 跨版本的 dashboard 要容許缺 metric | <span class="at-chip at-ev-tested">實測</span>[^vk-exp] |

## 建議設定 {#recommended}

`queries/redis_exporter/exporter-flags.env` 是可直接用的環境變數檔：

| 設定 | 值 | 為什麼 |
|---|---|---|
| `--connection-timeout` | 比 `scrape_timeout` 短（例如 5s） | 伺服器忙時由 exporter 自己回 `redis_up=0` 與錯誤訊息 |
| `--script` ＋ `--lua-script-read-only` | `queries/redis_exporter/valkey-extra.lua` | 補 `master_link_down_since_seconds`、`min_slaves_good_slaves`、`total_forks`；配 `ValkeyExporterScriptFailed` |
| `--check-single-keys` | `db0=at:heartbeat`（複本端） | 秒數延遲；主節點要有每秒寫入 heartbeat 的程式 |
| `--check-key-groups` | 只在需要時、獨立的長間隔 job | 每次 scrape 掃整個 keyspace |
| target label | `cluster=<主從組名>` | role-detection 依它分組 |
| Sentinel | 每台一個 exporter，`job="sentinel"` | `ValkeySentinel*` 規則 |
| 伺服器 | `latency-monitor-threshold 100`（例子） | 不打開就沒有 `redis_latency_spike_*` |

## 盲點 {#blind-spots}

元件卡的 `blind_spots` 有完整清單；重點：

| 看不到什麼 | 類別 | 補法 | 狀態 |
|---|---|---|---|
| 斷線多久、min-replicas、total_forks | exporter 沒輸出 | `valkey-extra.lua`（`--script`） | 已補，實測 |
| 秒數的複寫延遲 | 資料庫沒提供 | heartbeat key ＋ `--check-single-keys` | 已補，實測 |
| 哪類 key 佔記憶體 | 資料庫沒提供（Valkey） | `--check-key-groups`，有成本 | 已補，實測 |
| CONFIG 成敗 | exporter 沒輸出 | `ValkeyConfigMetricsMissing` | 規則，實測 |
| Sentinel TILT 次數、per-replica 狀態 | exporter 沒輸出 | Sentinel log 事件 | 未補 |
| Cluster 每個節點的 flags | exporter 沒輸出 | 每個節點都抓；或 script 跑 CLUSTER NODES | 未補，未實測 |
| 熱 key | 資料庫沒提供 | `valkey-cli --hotkeys` 離峰時跑 | 未補 |
| 主節點沒持久化又自動重啟 | 字串設定不輸出 | 盤點；`--include-config-metrics` | 盤點 |
| swap、RSS、OOM kill、磁碟空間 | 要靠其他元件 | node_exporter、cAdvisor | 已由 [主機與容器資源](../components/host.md) 補上（規則只讀碼，host lab 之後排程） |
| overcommit、THP 設定、OOM 的原因、備份 | 要靠其他元件 | 盤點；備份工具 | 未補 |
