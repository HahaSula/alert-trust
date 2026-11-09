# 摘要（2026-11-09）

原始輸出：`run-20261109.txt`（A～L）、`run2-20261109.txt`（R0～R5）。版本：Valkey 9.1.3、Redis 7.2.17／8.10.3、redis_exporter v1.97.0、Prometheus v3.13.5（scrape 5s、timeout 4s）。

| 情境 | 觀察 |
|---|---|
| A | Valkey 的 `redis_version` 是 7.2.4（另有 `valkey_version` 9.1.3）。預設設定下 Valkey 比 Redis 7.2 多 36 個 metric 名稱，Redis 8.10 比 7.2 多 26 個。`redis_total_system_memory_bytes` 四台都沒有。Valkey `repl-backlog-size` 預設 10485760 |
| B | Valkey 與 Redis 7.2 都是 slowlog last_id 3、length 4、last 0.30 秒、latency spike `command` 0.3 秒；Valkey 的 LATENCY LATEST 回 6 欄、Redis 7.2 回 4 欄 |
| C | `redis_blocked_clients` 1、`redis_total_blocking_keys` 1 |
| D | noeviction：SET、INCR 回 OOM，GET 正常，`errors_total{err="OOM"}` 2、evicted 0、redis_up 1；volatile-lru（沒有 TTL key）：OOM 3、evicted 0；allkeys-lru：SET 成功、evicted 32487、eviction_exceeded_time 20322ms |
| E | 預設 exporter：HTTP 回應 9.5 秒（主機牆鐘），Prometheus `up` 0、`scrape_duration` 4.0；`--connection-timeout=2s`：2.2 秒回應、`up` 1、`redis_up` 0、err「use of closed network connection」；multi-target 的複本與 exporter 本身 `up` 1 |
| F | e-vk1 `up` 1、`redis_up` 0、err「use of closed network connection」；`redis_connected_clients`、`redis_rejected_connections_total` 額滿期間沒有資料；結束後 rejected 21 |
| G | 斷線中：複本 `master_link_up` 0、`master_last_io_seconds_ago` -1、script 的 `master_link_down_since_seconds` 14（INFO 18）；主節點 `connected_slaves` 0、slave lag／offset 消失；Sentinel ok_slaves 仍 1。接回：主節點 log「Unable to partial resync … for lack of backlog」，`sync_full` 1→2、`sync_partial_err` 0→1 |
| H | BGSAVE 後 `rdb_last_bgsave_status` err（metric 0）；SET 與 PING 回 MISCONF；`errors_total{err="MISCONF"}` 29；redis_up 1；Sentinel 30 秒內 status 一直 ok。恢復後 status 1、fork 0.000445 秒、COW 413696 bytes、total_forks 4 |
| I | pause 主節點 25 秒：Sentinel 一直回報 vk1、status ok、config-epoch 0；未依 job 去重的 master 計數是 3（vk1 被 3 個 job 抓） |
| J | 停 s2、s3 25 秒：CKQUORUM 仍「OK 3 usable Sentinels」 |
| K | 壞 script：`up` 1、`redis_up` 0、err「ERR Unknown command called from script」、其他 450 個 sample 照常 |
| L | 40 萬個 key：scrape 0.012 → 1.095 秒、key group 928ms、key_group_count user／sess 各 200000、slowlog 128 筆全是 exporter 的 EVALSHA（每筆 1.2～2ms）；commandstats evalsha 2648 次、平均 1231µs，memory\|usage 160 萬次 |
| R0 | 開始時三台 Sentinel `sentinel_tilt` 0、`total_tilt` 0 |
| R1 | heartbeat 延遲平常 2.5 秒、vk2 斷線 20 秒時 25.1 秒、接回 3.2 秒；同時 `master_last_io_seconds_ago` -1、slave lag 消失 |
| R2 | ACL -config：`redis_up` 1、`last_scrape_error` 0、`redis_config_*` 全部消失、sample 554 → 530；`redis_up == 1 unless redis_config_maxclients` 回傳 e-noconf |
| R3 | MISCONF 期間 `last-ok-ping-reply` 11.6 → 44.0 秒、flags 一直是 master；cmdstat_ping rejected_calls 145、errorstat MISCONF 181 |
| R5 | pause 主節點 45 秒：每次讀到 `sentinel_tilt:1`，flags 一直是 master、config-epoch 0；s1 log 5 次 `+tilt`；`redis_sentinel_master_status` 1。依 run_id 去重的 master 計數 1 |
| R4 | 複本重啟：載入自己的 RDB，partial resync 成功，主節點 sync_full 不變、partial_ok 3→4；`changes(redis_start_time_seconds)` 主節點 0、複本 1 |
