# 附錄：從原始碼到 metric（Valkey／Redis）

[主頁](valkey.md)列出每個訊號該看哪個 metric。這頁回答另一個問題：**這個 metric 真的是從那個 INFO 欄位來的嗎？欄位不存在、是字串、或是 -1 時會怎樣？**每一列追完整條鏈：

1. **伺服器端**：Valkey 9.1.3 原始碼裡產生欄位或遞增計數的那一行（Redis 7.2.17／8.10.3 有差異時另列）。
2. **INFO 欄位**：`INFO ALL` 輸出的名稱與格式。
3. **Exporter 端**：redis_exporter v1.97.0 在哪裡讀、怎麼改名、怎麼轉值。
4. **Metric 與 lab 觀察**。

每個原始碼參考都在 tag 的 clone 上用關鍵字找出行號。Lab 欄的「實測」只用在親眼看到的情況。[^vk-exp][^vk-exp2]

## 先講結論

1. **exporter 只做兩種轉換。**數字欄位依對照表改名（`metricMapGauges`／`metricMapCounters`），字串 `ok`／`true` 轉 1、`err`／`fail`／`false` 轉 0；其他字串欄位（`maxmemory_policy`、`role`、`redis_version`）只出現在 `redis_instance_info` 的 label。對照表沒有的欄位直接丟掉。<span class="at-chip at-ev-code">讀碼</span>[^rx-okerr][^rx-instance-info]
2. **欄位不存在就沒有 series。**`master_link_down_since_seconds` 只在斷線時出現、`errorstat_*` 只在出錯後出現、`min_slaves_good_slaves` 只在設了 min-replicas 時出現，Keyspace 段只列有 key 的 db。<span class="at-chip at-ev-tested">實測</span>[^vk-info-link-down-since][^vk-info-errorstat][^vk-info-min-good]
3. **-1 會原樣輸出。**`master_last_io_seconds_ago` 在沒有主節點連線時是 -1，exporter 用 `Atoi` 直接輸出。<span class="at-chip at-ev-tested">實測</span>[^vk-info-last-io][^rx-last-io]
4. **單位換算只有少數幾個。**`latest_fork_usec` → `redis_latest_fork_seconds`（÷1e6）、per-command usec → 秒、LATENCY 的毫秒 → 秒；其他保持原單位（`eviction_exceeded_time_ms_total` 仍是毫秒）。<span class="at-chip at-ev-code">讀碼</span>[^rx-fork-seconds][^rx-latency-ms]
5. **一次 scrape 送出的指令都在主執行緒上排隊。**INFO ALL、CONFIG GET *、LATENCY LATEST、LATENCY HISTOGRAM、SLOWLOG LEN／GET 1、COMMAND INFO COMMANDLOG、COMMANDLOG LEN ×3、CONFIG GET commandlog-*，再依設定加 CLUSTER INFO、SENTINEL *、EVAL、SCAN。實測 Valkey 的 commandstats 裡每一個都出現。<span class="at-chip at-ev-tested">實測</span>[^vk-exp]

## 一次 scrape 的流程 {#scrape}

| 步驟 | exporter 原始碼 | 失敗時 |
|---|---|---|
| 連線 | `connectToRedis()`，連線、讀、寫逾時都是 `--connection-timeout`（預設 15s）[^rx-dial-timeouts][^rx-flag-timeout] | 回傳錯誤 → `redis_up` 0 |
| `INFO ALL`（失敗再試 `INFO`） | `exporter.go`[^rx-info-all] | 回傳錯誤 → `redis_up` 0 |
| `CONFIG GET *` | `exporter.go`[^rx-config-get] | **只記 debug log**，`redis_config_*` 消失，`redis_up` 仍是 1[^rx-config-get-silent] |
| `CLUSTER INFO`（`cluster_enabled:1` 時） | `exporter.go`[^rx-cluster-info] | 記 error log，cluster metric 消失 |
| `LATENCY LATEST／HISTOGRAM`、`SLOWLOG`、`COMMANDLOG` | `latency.go`、`slowlog.go`、`commandlog.go`[^rx-latency-latest][^rx-slowlog-len][^rx-cmdlog] | 對應 metric 消失 |
| check-keys、count-keys、key group | `keys.go`、`key_groups.go`[^rx-checkkeys-scan][^rx-kg-loop] | 記 error log |
| `SENTINEL *`（INFO 有 `# Sentinel` 時） | `sentinels.go`[^rx-sen-section] | 對應 metric 消失 |
| `--script` | `lua.go`；錯誤時 `script_result` 0 並**回傳錯誤**[^rx-lua-result0][^rx-lua-err-up] | `redis_up` 0（其他 metric 已經送出） |
| 結果 | `Collect()` 設 `redis_up` 與 `redis_exporter_last_scrape_error{err}`；整個 Collect 加鎖[^rx-collect-up][^rx-scrape-err][^rx-collect-lock] | — |

## INFO 欄位到 metric {#fields}

| 訊號 | 伺服器原始碼（Valkey 9.1.3） | INFO 欄位 | exporter | metric | Lab |
|---|---|---|---|---|---|
| writes-rejected | `processCommand` 拒絕 OOM[^vk-oom-reject]、MISCONF[^vk-misconf-deny] | `errorstat_OOM:count=N`、`errorstat_MISCONF` | `info.go` Errorstats[^rx-errorstats] | `redis_errors_total{err}` | <span class="at-chip at-ev-tested">實測</span> OOM 2→3、MISCONF 29 |
| writes-rejected | 同上，每個指令的 rejected_calls | `cmdstat_set:…,rejected_calls=1` | `info.go`[^rx-cmd-rejected] | `redis_commands_rejected_calls_total{cmd}` | <span class="at-chip at-ev-tested">實測</span> set 1、incr 1 |
| maxmemory-eviction | `performEvictions()`[^vk-oom-perform]；noeviction／import-mode 直接失敗[^vk-evict-noeviction]；`stat_evictedkeys++`[^vk-evict-count]（Redis 8 在 db.c[^r8-evict-count]） | `evicted_keys`[^vk-info-evicted] | 對照表[^rx-map-evicted] | `redis_evicted_keys_total` | <span class="at-chip at-ev-tested">實測</span> 0／0／32487 |
| maxmemory-eviction | `server.maxmemory` | `maxmemory`[^vk-info-maxmemory] | 對照表[^rx-map-maxmemory] | `redis_memory_max_bytes` | <span class="at-chip at-ev-tested">實測</span> |
| maxmemory-eviction | 超過上限的時間 | `current_eviction_exceeded_time`、`total_eviction_exceeded_time`（毫秒）[^vk-info-eviction-exceeded] | 對照表 | `redis_eviction_exceeded_time_ms_total` | <span class="at-chip at-ev-tested">實測</span> 20322 |
| fragmentation | `mh->total_frag` | `mem_fragmentation_ratio`[^vk-info-frag] | 對照表[^rx-map-frag] | `redis_mem_fragmentation_ratio` | <span class="at-chip at-ev-code">讀碼</span> |
| connections | `networking.c` 超過 maxclients[^vk-maxclients-reject] | `rejected_connections`[^vk-info-rejected] | 對照表[^rx-map-rejected] | `redis_rejected_connections_total` | <span class="at-chip at-ev-tested">實測</span> 額滿期間無資料，之後 21 |
| connections | `CONFIG GET maxclients` | （不是 INFO） | `extractConfigMetrics`[^rx-config-maxclients] | `redis_config_maxclients` | <span class="at-chip at-ev-tested">實測</span> 沒有 CONFIG 權限時消失 |
| blocked-clients | `server.blocked_clients` | `blocked_clients`[^vk-info-blocked] | 對照表[^rx-map-blocked] | `redis_blocked_clients` | <span class="at-chip at-ev-tested">實測</span> 1 |
| main-thread | `active_io_threads_num > 1` | `io_threads_active`[^vk-info-io-threads] | 對照表 | `redis_io_threads_active` | <span class="at-chip at-ev-code">讀碼</span> |
| keyspace-hit-ratio | `stat_keyspace_hits` | `keyspace_hits`[^vk-info-hits] | 對照表[^rx-map-hits] | `redis_keyspace_hits_total` | <span class="at-chip at-ev-code">讀碼</span> |
| replication-link | `repl_state == REPL_STATE_CONNECTED ? "up" : "down"` | `master_link_status`[^vk-info-link-status] | `up` → 1，其他 → 0[^rx-link-up] | `redis_master_link_up{master_host,master_port}` | <span class="at-chip at-ev-tested">實測</span> 0 |
| replication-link | 沒有主節點連線時 -1 | `master_last_io_seconds_ago`[^vk-info-last-io] | `Atoi`，原樣輸出[^rx-last-io] | `redis_master_last_io_seconds_ago` | <span class="at-chip at-ev-tested">實測</span> -1 |
| replication-link | 只在斷線時輸出 | `master_link_down_since_seconds`[^vk-info-link-down-since] | 不在對照表 | （`valkey-extra.lua`）`redis_script_values{key="master_link_down_since_seconds"}` | <span class="at-chip at-ev-tested">實測</span> 14、18 |
| replication-lag | 每台複本一行，`lag = 現在 − repl_ack_time`[^vk-info-slave-lag][^vk-info-slave-line] | `slave0:ip=…,port=…,state=online,offset=…,lag=…,type=replica` | `parseConnectedSlaveString`[^rx-slave-lag] | `redis_connected_slave_offset_bytes`、`redis_connected_slave_lag_seconds`（label slave_ip、slave_port、slave_state） | <span class="at-chip at-ev-tested">實測</span> 斷線時消失 |
| replication-lag | `primary_repl_offset` | `master_repl_offset` | 對照表[^rx-map-master-offset] | `redis_master_repl_offset` | <span class="at-chip at-ev-tested">實測</span> |
| full-resync | backlog 不夠[^vk-repl-no-backlog] → `stat_sync_full++`[^vk-repl-full]、`stat_sync_partial_err++`[^vk-repl-partial-err] | `sync_full`、`sync_partial_err`[^vk-info-sync-full][^vk-info-sync-partial-err] | 對照表[^rx-map-sync-full] | `redis_replica_resyncs_full`、`redis_replica_partial_resync_denied` | <span class="at-chip at-ev-tested">實測</span> 1→2、0→1 |
| full-resync | `repl_backlog->histlen` | `repl_backlog_histlen`[^vk-info-backlog] | 對照表 | `redis_repl_backlog_history_bytes` | <span class="at-chip at-ev-code">讀碼</span> |
| role-detection | `primary_host == NULL ? "master" : "slave"`[^vk-info-role] | `role` | label[^rx-instance-info] | `redis_instance_info{role}` | <span class="at-chip at-ev-tested">實測</span> |
| version-semantics | 固定的 `REDIS_VERSION`[^vk-info-redis-version] | `redis_version:7.2.4`、`valkey_version:9.1.3` | label | `redis_instance_info{redis_version, valkey_version}` | <span class="at-chip at-ev-tested">實測</span> |
| rdb-persistence | 子程序失敗 → `lastbgsave_status = C_ERR`[^vk-rdb-bgsave-err]；fork 失敗也是[^vk-rdb-fork-fail] | `rdb_last_bgsave_status:ok／err`[^vk-info-bgsave-status] | `err` → 0[^rx-map-bgsave][^rx-okerr] | `redis_rdb_last_bgsave_status` | <span class="at-chip at-ev-tested">實測</span> 0 → 1 |
| aof-persistence | 寫入與背景 fsync 都成功才 ok | `aof_last_write_status`[^vk-info-aof-write-status]、`aof_last_bgrewrite_status`[^vk-info-aof-rewrite-status] | 對照表[^rx-map-aof-write] | `redis_aof_last_write_status`、`redis_aof_last_bgrewrite_status` | <span class="at-chip at-ev-code">讀碼</span> |
| fork-latency | `stat_fork_time = ustime() − start`[^vk-fork-time] | `latest_fork_usec`[^vk-info-fork-usec] | 改名並 ÷1e6[^rx-map-fork][^rx-fork-seconds] | `redis_latest_fork_seconds` | <span class="at-chip at-ev-tested">實測</span> 0.000445 |
| fork-latency | `stat_total_forks++` | `total_forks`[^vk-info-total-forks] | 不在對照表 | （`valkey-extra.lua`）`redis_script_values{key="total_forks"}` | <span class="at-chip at-ev-tested">實測</span> 4 |
| slow-commands | latency monitor 存成毫秒[^vk-lat-ms]；Valkey 回 6 欄[^vk-lat-latest] | `LATENCY LATEST`（不是 INFO） | 讀前 4 欄，毫秒 ÷1e3[^rx-latency-ms] | `redis_latency_spike_duration_seconds{event_name}` | <span class="at-chip at-ev-tested">實測</span> 0.3 |
| slow-commands | — | `SLOWLOG LEN`、`SLOWLOG GET 1` | `slowlog.go`[^rx-slowlog-len][^rx-slowlog-lastid] | `redis_slowlog_length`、`redis_slowlog_last_id`、`redis_last_slow_execution_duration_seconds` | <span class="at-chip at-ev-tested">實測</span> 4、3、0.30 |
| sentinel-quorum | `sentinelIsQuorumReachable()`[^vk-sen-quorum] | `SENTINEL CKQUORUM` 的回覆[^vk-sen-ckquorum] | 錯誤回覆 → 0，訊息放 label[^rx-sen-ckquorum] | `redis_sentinel_master_ckquorum_status{message}` | <span class="at-chip at-ev-tested">實測</span> 1 |
| sentinel-quorum | TILT 判斷[^vk-sen-tilt] | `sentinel_tilt`、`sentinel_total_tilt`[^vk-sen-info-tilt] | 只取 `sentinel_tilt` | `redis_sentinel_tilt` | <span class="at-chip at-ev-tested">實測</span> 1 |
| sentinel-quorum | INFO sentinel 的 `master0:…status=ok` | `status` | `ok` → 1[^rx-sen-status] | `redis_sentinel_master_status{master_status}` | <span class="at-chip at-ev-tested">實測</span> TILT 期間一直是 1 |
| cluster-state | full coverage 檢查[^vk-cl-full-coverage]、少數派[^vk-cl-minority] | `CLUSTER INFO` 的 `cluster_state:ok／fail`[^vk-cl-info-state]、`cluster_slots_fail`[^vk-cl-info-slots-fail] | `cluster_` 前綴原名輸出[^rx-cluster-prefix] | `redis_cluster_state`、`redis_cluster_slots_fail` | <span class="at-chip at-ev-code">讀碼</span> |

## 補位：`valkey-extra.lua` {#script}

`queries/redis_exporter/valkey-extra.lua` 用 `EVAL_RO`（`--lua-script-read-only`）在伺服器上執行 `INFO replication` 與 `INFO stats`，用 `string.match` 取出欄位，回傳 `{key, value, …}`；exporter 把每一對輸出成 `redis_script_values{key, filename}`。[^rx-lua-ro] 欄位不存在時輸出預設值（斷線多久 0、min-replicas -1），因為 script 任何錯誤都會讓 `redis_up` 變 0。實測主節點與複本上 `redis_script_result` 都是 1，斷線時 `master_link_down_since_seconds` 14～18、`total_forks` 4。<span class="at-chip at-ev-tested">實測</span>[^vk-exp][^vk-exp2]

## 沒有釘到原始碼行的部分 {#unpinned}

- 「src/ 裡沒有 HTTP 或 Prometheus 輸出」是對 9.1.3 與 8.10.3 整個 `src/` 做關鍵字搜尋的結果，沒有單一行可釘。
- 過期清理（expire-cycle）與容量（memory-growth）只依 exporter 的對照表，沒有追 `expire.c`，標「推論」。
- MISCONF 是否讓 Sentinel 標 sdown：原始碼路徑已釘（PING 回覆檢查、sdown 條件），但實驗受 TILT 干擾，沒有驗證。
