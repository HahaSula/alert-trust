# Valkey／Redis 各版本的監控差異

這頁列出 Valkey 7.2、8.0、8.1、9.0、9.1 與 Redis 7.2、8.x 之間會影響監控的差異：INFO 欄位的增減、預設值改變、slowlog 與 commandlog、redis_exporter v1.97.0 有沒有處理。目前的卡片核對過 Valkey 9.1.3、Redis 7.2.17 與 8.10.3。

依據是各版本線最新 tag 的 `src/server.c`（INFO）、`src/config.c`（預設值）與 `src/version.h`，加上 redis_exporter v1.97.0。INFO 欄位的增減是把每個版本 `server.c` 裡所有 `欄位:%` 字串列出來逐版比對。沒有做實驗，全部是<span class="at-chip at-ev-code">讀碼</span>；標「推論」的是從原始碼推出、沒有單獨驗證的結果。

## 結論 {#summary}

1. **Valkey 的 `redis_version` 永遠是 7.2.4。**7.2.15 到 9.1.3 都一樣；真正的版本在 `valkey_version`。用 `redis_instance_info{redis_version=...}` 判斷版本的規則或 dashboard 在 Valkey 上一律看到 7.2.4。exporter 會把 `valkey_version` 加成 label。
2. **Valkey 8.0 起 INFO 輸出 `server_mode`，不是 `redis_mode`**（除非開 `extended-redis-compatibility`）。exporter 會改讀 `server_mode`，`redis_instance_info` 的 `redis_mode` label 照樣有值。
3. **Valkey 8.0 改了兩組預設值：`repl-backlog-size` 從 1mb 變 10mb，`lazyfree-lazy-*` 從 no 變 yes。**Redis 8.10 仍是 1mb、no。同樣的寫入量下 Valkey 8 以上的 `full-resync` 比較少；淘汰與刪除改在背景釋放，`redis_memory_used_bytes` 降得比較慢（推論）。
4. **Valkey 8.1 起 slowlog 是 commandlog 的一部分。**`slowlog-*` 設定變成 `commandlog-*` 的別名，`SLOWLOG` 指令仍在；exporter 兩種都抓，先用 `COMMAND INFO COMMANDLOG` 判斷有沒有。
5. **INFO 欄位只增不減**（除了上面的 `redis_mode`、`atomicvar_api`）。抽查卡片用到的 21 個欄位（`master_link_status`、`sync_full`、`rejected_connections`、`total_error_replies`、`lazyfree_pending_objects` 等），在 11 個 Valkey／Redis tag 都存在。

## 選的版本 {#versions}

| 版本線 | 這頁用的 tag | 備註 |
|---|---|---|
| Valkey 7.2 | `7.2.15` | 從 Redis 7.2.4 分出 |
| Valkey 8.0 | `8.0.12` | |
| Valkey 8.1 | `8.1.11` | |
| Valkey 9.0 | `9.0.7` | |
| Valkey 9.1 | `9.1.3` | 卡片核對過 |
| Redis 7.2 | `7.2.17` | 卡片核對過；最後的 BSD 授權分支 |
| Redis 8.x | `8.2.11`、`8.10.3` | 8.10.3 卡片核對過；7.4.12、8.0.6、8.4.8 只做 INFO 欄位比對 |
| redis_exporter | `v1.97.0` | 最新 release |

## 版本差異表 {#diff}

| 版本 | 變更 | 對監控的影響（哪個 metric／collector／規則） | exporter 或 endpoint 有沒有處理 | 依據 |
|---|---|---|---|---|
| Valkey 7.2+ | `REDIS_VERSION` 固定 `"7.2.4"`；另有 `valkey_version`、`server_name` | `redis_instance_info{redis_version}` 在 Valkey 上不能用來分版本 | 有：有 `valkey_version` 就加成 label | [^ver-vk72-redis-version][^ver-vk91-redis-version][^ver-rx-valkey-version] |
| Valkey 8.0 | `redis_mode` 改名 `server_mode`（`extended-redis-compatibility yes` 時才輸出 `redis_mode`） | 直接解析 INFO 的腳本要改；`redis_instance_info{redis_mode}` 不受影響 | 有：`redis_mode` 沒有就讀 `server_mode` | [^ver-vk72-redis-mode][^ver-vk80-server-mode][^ver-rx-server-mode] |
| Valkey 8.0 | `repl-backlog-size` 預設 1mb → 10mb | `full-resync` 的頻率與 `redis_config_*` 看到的 backlog 不同；從 7.2 升上來的基準要重取 | 不需要 | [^ver-vk72-backlog][^ver-vk80-backlog] |
| Valkey 8.0 | `lazyfree-lazy-eviction`、`-expire`、`-server-del`、`-user-del`、`replica-lazy-flush` 預設 yes | `maxmemory-eviction` 看到的 `used_memory` 在淘汰後慢一點才降，背景釋放量看 `redis_lazyfree_pending_objects`（推論） | 有輸出 `lazyfree_pending_objects` | [^ver-vk72-lazyfree][^ver-vk80-lazyfree] |
| Valkey 8.0 | 新 I/O 執行緒架構，INFO 多 `io_threaded_*` 計數；`io-threads` 預設仍是 1 | `main-thread` 的 `redis_io_threads_active` 意義不變；多了可看的 `io_threaded_*` | 部分：`io_threads_active` 有對應，其他欄位照 INFO 名稱輸出 | [^ver-vk80-iothreads] |
| Valkey 8.0 | INFO 新增 `client_query_buffer_limit_disconnections`、`client_output_buffer_limit_disconnections`、`evicted_scripts`、`replicas_repl_buffer_*` | `connections` 可以多看「因 buffer 上限被斷線」的次數 | 照 INFO 名稱輸出 | 本頁的 INFO 欄位比對（[^ver-vk80-server-mode] 同檔） |
| Valkey 8.1 | slowlog 併入 commandlog：`commandlog-execution-slower-than`（別名 `slowlog-log-slower-than`）等；另有 large-request、large-reply 兩種 log | `slow-commands` 的 `redis_slowlog_*` 照常；另有 `redis_commandlog_*` | 有：先 `COMMAND INFO COMMANDLOG`，有才抓；`SLOWLOG LEN` 一律送 | [^ver-vk81-commandlog][^ver-vk91-slowlog-len][^ver-rx-commandlog-probe][^ver-rx-slowlog] |
| Valkey 8.1 | INFO 新增 `paused_reason`、`paused_actions`、`paused_timeout_milliseconds` | `CLIENT PAUSE`／failover 時寫入暫停可以直接看 | 照 INFO 名稱輸出 | 本頁的 INFO 欄位比對 |
| Valkey 9.0 | `io-threads` 可用 `CONFIG SET` 動態改；INFO 新增 cluster slot 搬遷的 `*_slot_export`／`*_slot_import` | `main-thread` 的 `redis_io_threads_active` 可能在執行中改變 | 不需要 | [^ver-vk80-iothreads][^ver-vk90-iothreads] |
| Valkey 9.1 | INFO 新增 `tls_*_cert_expires_in_seconds`、`used_active_time_main_thread` | 可以加「TLS 憑證快到期」規則（卡片目前沒有） | 照 INFO 名稱輸出（推論，沒查對照表） | [^ver-vk91-tls-expire] |
| Redis 8.0–8.10 | `repl-backlog-size` 仍 1mb；`lazyfree-*` 仍 no | 和 Valkey 8+ 的預設不同，兩者的基準不能共用 | 不需要 | [^ver-rd810-backlog][^ver-rd810-lazyfree] |
| Redis 8.2 | INFO 新增 `master_total_sync_attempts`、`master_current_sync_attempts`、`master_link_up_since_seconds` | `replication-link` 可以看重連次數與連線持續時間 | 照 INFO 名稱輸出（推論） | [^ver-rd82-sync-attempts] |
| Redis 8.10 | INFO 新增 `slowlog_commands_count`、`slowlog_commands_time_ms_sum`／`_max`、`active_clients`、`avg_pipeline_length` | `slow-commands` 可以用累計次數，不必算 `slowlog_last_id` 的差 | 照 INFO 名稱輸出（推論） | [^ver-rd810-slowlog-info] |

## redis_exporter 怎麼處理版本 {#exporter}

| 項目 | 做法 | 會怎麼壞 | 依據 |
|---|---|---|---|
| 版本 label | `redis_instance_info` 帶 `redis_version`，Valkey 另帶 `valkey_version` | 只看 `redis_version` 會把所有 Valkey 當成 7.2.4 | [^ver-rx-valkey-version] |
| mode | `redis_mode` 沒有時用 `server_mode` | 已處理 | [^ver-rx-server-mode] |
| commandlog | `COMMAND INFO COMMANDLOG` 有結果才抓 | Redis 與 Valkey 8.0 以下沒有 `redis_commandlog_*`，規則寫成 `or` 才不會空 | [^ver-rx-commandlog-probe] |

## 各版本要注意的地方 {#notes}

- **Valkey 7.2、Redis 7.2**：INFO 幾乎相同（只差 `server_name`、`valkey_version`）；預設 backlog 1mb、lazyfree 關。
- **Valkey 8.0**：backlog 10mb、lazyfree 開；`server_mode` 取代 `redis_mode`。
- **Valkey 8.1**：有 commandlog；slowlog 設定是別名。
- **Valkey 9.0、9.1**：`io-threads` 可動態改；9.1 有 TLS 憑證到期欄位。
- **Redis 8.x**：預設值維持 7.2 的；8.2、8.10 多了複寫重連與 slowlog 累計欄位。

## 卡片上會隨版本變的訊號 {#signals}

| 訊號 | 隨版本變什麼 | 建議 |
|---|---|---|
| `version-semantics`、`role-detection` | Valkey 的 `redis_version` 固定 7.2.4；8.0 起 `server_mode` | 版本判斷改用 `valkey_version` label（沒有時才用 `redis_version`） |
| `full-resync`、`replication-lag` | Valkey 8.0 起 backlog 10mb | 門檻與基準依實際 `repl-backlog-size` 設，不要沿用 7.2 的經驗值 |
| `maxmemory-eviction`、`memory-growth` | Valkey 8.0 起 lazyfree 預設開 | runbook 加看 `redis_lazyfree_pending_objects` |
| `slow-commands` | Valkey 8.1 起有 commandlog；Redis 8.10 有 `slowlog_commands_count` | 規則保持 `redis_slowlog_*` 為主，commandlog 用 `or` |
| `main-thread` | Valkey 9.0 起 `io-threads` 可動態改 | 比較 `redis_io_threads_active` 前後要注意設定是否改過 |
| 其他（`connections`、`rdb-persistence`、`aof-persistence`、`expire-cycle`、`cluster-state`、`sentinel-quorum`） | 抽查用到的 INFO 欄位，所有版本都有 | 不用改 |

Valkey／Redis 本體的監控見 [Valkey／Redis 怎麼監控](../valkey.md)。
