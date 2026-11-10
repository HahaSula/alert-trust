# 資料庫的資源：池、執行緒與主機

每個資料庫 instance 都有自己管理的資源（連線上限、執行緒或行程、記憶體池、背景工作），也吃主機的 CPU、記憶體、磁碟與 fd。這頁回答兩件事：**這些資源用到多少、上限多少，現有的卡片看不看得到**；以及**資料庫內部的資源和主機層的關係**，只寫會影響告警的部分。

依據是各 target 卡已釘的版本（MySQL 8.4.7、PostgreSQL 17.11、Valkey 9.1.3、MongoDB 8.0.34、etcd v3.7.3、TiDB／TiKV v8.5.8、YugabyteDB v2026.1.3.0、ClickHouse v26.10.2）與它們的 exporter 原始碼；主機層是 node_exporter v1.12.1、cAdvisor v0.60.6、kubelet v1.37.2、kube-state-metrics v2.20.0。本頁新增的部分全部是讀碼，沒有做實驗。<span class="at-chip at-ev-code">讀碼</span>

主機層的訊號集中在 [主機與容器資源](../components/host.md) 卡。

## 先講結論

1. **資料庫自己管的池大多看得到「用量 vs 上限」**：連線、buffer pool／WiredTiger cache／maxmemory、ClickHouse 的 merge pool、TiKV／YugabyteDB 的記憶體配額，target 卡都有訊號。
2. **IO 路徑是最大的缺口。**只有 etcd（WAL fsync、backend commit）和 YugabyteDB（log sync）有原生的 fsync 延遲；MySQL、PostgreSQL、MongoDB、Valkey、ClickHouse 只看得到「等待次數」或什麼都沒有，fsync 慢只能從主機的磁碟延遲看。
3. **CPU 幾乎都要靠主機層。**有自己 CPU metric 的只有 Valkey（主執行緒）、TiKV（每個 thread pool）、YugabyteDB、ClickHouse；MySQL、PostgreSQL、MongoDB 的 exporter 沒有行程 CPU。容器的 CPU 節流在資料庫裡完全看不到。
4. **複寫的 worker 與背景工作的「池滿了」大多看不到。**MySQL 的 applier worker collector 預設關；PostgreSQL 的 autovacuum worker 數要自己用 `backend_type` label 算；MongoDB 的 oplog applier 沒有對應訊號。
5. **主機層目前每張 target 卡都把它寫成 blind spot。**磁碟空間、OOM、swap、fd、時鐘同步都可以由 node_exporter／cAdvisor 補上，見[主機層怎麼補](#host-layer)。

## 對照表 {#matrix}

每格依序是：上限由什麼決定（設定，附原始碼）、用量在哪個 metric（連到卡片訊號，或附 exporter 原始碼）。「主機層」表示資料庫與 exporter 都沒有，要看 [host](../components/host.md) 卡；「看不到」表示主機層也只能間接推。

| | 連線／session | 工作執行緒或行程 | 記憶體池 | IO／fsync 路徑 | 複寫的 worker | 背景工作 | CPU |
|---|---|---|---|---|---|---|---|
| **MySQL／MariaDB（InnoDB）** | `max_connections`[^my-asp-my-max-connections]；[connections](../components/mysql.md#connections)、[connection-headroom](../components/mysql.md#connection-headroom) | 每條連線一條 thread[^dbr-my-thread-per-conn]，`thread_cache_size`[^res-my-thread-cache]；`Threads_running` 在 [connections](../components/mysql.md#connections)，`Threads_created`[^res-my-threads-created] 由 global_status 輸出[^res-mx-default-gs]但沒有訊號；MariaDB thread pool[^dbr-mdb-thread-handling] 沒有訊號 | `innodb_buffer_pool_size`[^res-my-bp-size]；[buffer-pool-misses](../components/mysql.md#buffer-pool-misses)、[innodb-pressure](../components/mysql.md#innodb-pressure)（wait_free、log_waits） | `innodb_io_capacity`[^res-my-io-capacity]；`Innodb_data_pending_fsyncs`[^res-my-pending-fsyncs] 有輸出、沒有訊號；redo 用量（checkpoint age）[^my-perf-my-redo-lsn][^my-perf-mdb-checkpoint-age] 沒有訊號；fsync 延遲**主機層** | `replica_parallel_workers`（預設 4）[^res-my-replica-workers]；每個 worker 的狀態 collector 預設關[^res-mx-default-applier]，只看得到整體 [replication-lag](../components/mysql.md#replication-lag) | `innodb_purge_threads`[^res-my-purge-threads]；結果看 [undo-history](../components/mysql.md#undo-history)，擋住的人看 [long-transactions](../components/mysql.md#long-transactions) | **主機層**；statement 層的 CPU_TIME 在 performance_schema[^my-asp-my-esms-cpu]，exporter 只到 [slow-sql-visibility](../components/mysql.md#slow-sql-visibility) |
| **PostgreSQL** | `max_connections`[^pg-trace-pg17-guc-maxconn]；[connection-headroom](../components/postgres_exporter.md#connection-headroom) | 每條連線一個 backend process[^dbr-pg-backend-fork]；`max_worker_processes`[^res-pg-worker-procs] 由 pg_settings 輸出[^pg-asp-c-settings-vartype]，用量要從 `pg_stat_activity_count` 的 `backend_type` 算[^res-pe-activity-labels]，沒有訊號 | `shared_buffers`[^res-pg-shared-buffers]；[cache-and-temp](../components/postgres_exporter.md#cache-and-temp)（`blks_read` 不等於讀磁碟，見下）；pg_buffercache collector 預設關[^res-pe-buffercache-off] | commit 時 WAL fsync[^dbr-pg-xlogflush]；[wal-io-sources](../components/postgres_exporter.md#wal-io-sources)（query-exporter 的 `pg_stat_io`）、[checkpoint-pressure](../components/postgres_exporter.md#checkpoint-pressure)；PG 16 以前有 `buffers_backend_fsync`[^res-pe-backend-fsync]，17 起移除[^res-pe-bgwriter-17]；fsync 延遲**主機層** | `max_wal_senders`[^res-pg-wal-senders]；看 [replication-lag](../components/postgres_exporter.md#replication-lag)、[standby-conflicts](../components/postgres_exporter.md#standby-conflicts) | `autovacuum_max_workers`[^res-pg-av-workers]；[autovacuum](../components/postgres_exporter.md#autovacuum) 看結果，同時在跑的 worker 數沒有訊號；checkpointer collector 預設關[^res-pe-checkpointer-off] | **主機層**（postgres_exporter 沒有行程 CPU） |
| **Valkey／Redis** | `maxclients`[^vk-cfg-maxclients]；[connections](../components/redis_exporter.md#connections) | 單一主執行緒＋`io-threads`[^vk-cfg-io-threads]；[main-thread](../components/redis_exporter.md#main-thread) | `maxmemory`[^vk-info-maxmemory]；[maxmemory-eviction](../components/redis_exporter.md#maxmemory-eviction)、[fragmentation](../components/redis_exporter.md#fragmentation)；client output buffer 佔的記憶體 `redis_mem_clients_slaves`[^res-rx-mem-replicas] 沒有訊號 | AOF；[aof-persistence](../components/redis_exporter.md#aof-persistence)（`aof_delayed_fsync`）；fork 在 [fork-latency](../components/redis_exporter.md#fork-latency) | 複本由主執行緒送出，上限是 `client-output-buffer-limit`[^res-vk-cfg-cobl]；超過被斷線的次數 `redis_client_output_buffer_limit_disconnections_total`[^res-vk-cobl-stat][^res-rx-cobl] 沒有訊號（Redis 7.2 沒有這個欄位） | expire／lazyfree 在 [expire-cycle](../components/redis_exporter.md#expire-cycle)；RDB／AOF rewrite 在 [rdb-persistence](../components/redis_exporter.md#rdb-persistence) | [main-thread](../components/redis_exporter.md#main-thread)；整個行程 `redis_cpu_sys_seconds_total`[^res-rx-cpu-sys] |
| **MongoDB** | `net.maxIncomingConnections`[^res-mg-max-incoming]；[connections](../components/mongodb_exporter.md#connections) | 執行 ticket（8.0 動態調整）[^mg-ticket-algo]；[tickets](../components/mongodb_exporter.md#tickets) | WiredTiger cache，預設 (RAM − 1GB) × 50%[^mg-wt-cache-default]；[wt-cache](../components/mongodb_exporter.md#wt-cache) | journal 與 checkpoint；[checkpoint](../components/mongodb_exporter.md#checkpoint) 只看成功與耗時；fsync 延遲**主機層** | `replWriterThreadCount`（預設 16）[^res-mg-repl-writers]；oplog buffer 的 `sizeBytes`／`maxSizeBytes`[^res-mg-repl-buffer] 經 `mongodb_ss_*` 攤平[^res-mgx-ss-prefix]，但欄位位置隨 feature flag 改變[^res-mg-repl-buffer-flag]，沒有訊號 | eviction thread 固定 4 個[^mg-wt-eviction-threads]，app thread 被拉去 evict 在 [wt-cache](../components/mongodb_exporter.md#wt-cache)；TTL 在 [ttl-monitor](../components/mongodb_exporter.md#ttl-monitor) | serverStatus `extra_info.user_time_us`[^res-mg-cpu-time] 有輸出、沒有訊號；實際以**主機層**為準 |
| **etcd** | 沒有連線上限設定；打滿的是 fd：[fd-headroom](../components/etcd.md#fd-headroom) | Go runtime，沒有固定的池；`go_goroutines` 等由預設 registry 輸出[^res-etcd-default-reg][^res-cg-process] | bbolt 檔案與 quota（預設 2GiB）[^etcd-quota-default]；[db-quota](../components/etcd.md#db-quota) | WAL fsync[^etcd-wal-fsync-observe]、backend commit[^etcd-be-batch-limit]；[wal-fsync](../components/etcd.md#wal-fsync)、[backend-commit](../components/etcd.md#backend-commit) | Raft 由同一個行程處理；[proposals-pending](../components/etcd.md#proposals-pending)、[slow-apply](../components/etcd.md#slow-apply) | 自動 compaction 預設關[^etcd-embed-autocompact]；[compaction](../components/etcd.md#compaction)、[defrag](../components/etcd.md#defrag) | `process_cpu_seconds_total`（預設 registry）[^res-cg-process]，沒有訊號；容器節流**主機層** |
| **TiDB／TiKV** | TiDB `max-server-connections`（預設 0 不限制）[^tidb-cfg-max-conn]、`token-limit`[^tidb-cfg-token-limit]；[connections](../components/tidb.md#connections) | TiKV 每個 thread pool 有名稱[^tikv-batch-thread-name]，CPU 在 [raftstore-latency](../components/tidb.md#raftstore-latency)；unified read pool 的執行中 task[^res-tikv-read-pool]、`tikv_worker_pending_task_total`[^res-tikv-pending-task] 沒有訊號 | TiDB `tidb_server_memory_limit`[^tidb-var-memlimit]、TiKV memory quota[^tikv-m-memquota]；[memory-quota](../components/tidb.md#memory-quota)；block cache 大小[^res-tikv-block-cache] 沒有訊號 | raft log append 在 [raftstore-latency](../components/tidb.md#raftstore-latency)；RocksDB 的 write stall[^tikv-m-write-stall] 在 [write-stall](../components/tidb.md#write-stall) | raftstore／apply pool；channel 滿[^res-tikv-channel-full]（官方規則有[^res-tikv-rule-channel-full]）沒有訊號 | compaction 待處理量[^res-tikv-pending-compaction] 與上限 `soft_pending_compaction_bytes_limit`[^res-tikv-soft-pending] 都有輸出，沒有訊號；GC 在 [gc-progress](../components/tidb.md#gc-progress) | 每個 thread 的 CPU 在 [raftstore-latency](../components/tidb.md#raftstore-latency)；CPU quota `tikv_server_cpu_cores_quota`[^res-tikv-cpu-quota]（官方規則 TiKV_cpu_quota[^res-tikv-rule-cpu-quota]）沒有訊號 |
| **YugabyteDB** | `ysql_max_connections`[^yb-ysql-maxconn]；[connections](../components/yugabytedb.md#connections) | `threads_running`[^res-yb-threads]；RPC 佇列 `generic_svc_queue_length`[^res-yb-svc-queue]，溢出 `rpcs_queue_overflow`[^res-yb-queue-overflow] 沒有訊號 | soft／hard memory limit[^yb-mem-soft][^yb-mem-hard]；[memory](../components/yugabytedb.md#memory)；block cache 比例[^res-yb-block-cache-pct]、用量[^res-yb-block-cache] 沒有訊號 | WAL sync；[wal-fsync](../components/yugabytedb.md#wal-fsync) | Raft follower；[replication-lag](../components/yugabytedb.md#replication-lag) | RocksDB compaction；[write-stall](../components/yugabytedb.md#write-stall)（SST 太多被拒） | `cpu_utime`／`cpu_stime`[^res-yb-cpu-utime] 有輸出、沒有訊號 |
| **ClickHouse** | `max_connections`（預設 4096）[^res-ch-max-connections]、`TCPConnection`[^res-ch-tcpconn]；同時查詢數 `max_concurrent_queries`[^ch-cfg-max-conc] 在 [query-admission](../components/clickhouse.md#query-admission) | global thread pool，`max_thread_pool_size`（預設 10000）[^res-ch-max-thread-pool]；`GlobalThread`／`GlobalThreadActive`[^res-ch-globalthread][^res-ch-globalthread-active] 沒有訊號 | `max_server_memory_usage`（預設 RAM × 0.9）[^ch-ss-max-mem][^ch-ss-mem-ratio]；[memory](../components/clickhouse.md#memory) | 沒有 fsync 延遲；只有 `OSIOWaitTimeNormalized`[^res-ch-iowait]；**主機層** | replication queue；[replication-queue](../components/clickhouse.md#replication-queue) | merge／mutation pool[^ch-cm-poolsize]；[merge-pool](../components/clickhouse.md#merge-pool)、[parts-pressure](../components/clickhouse.md#parts-pressure) | `OSUserTimeNormalized`[^res-ch-os-user]、`CGroupMaxCPU`[^res-ch-cgroup-cpu] 有輸出、沒有訊號 |

### 最大的缺口 {#gaps}

| 缺口 | 影響哪些資料庫 | 補法 |
|---|---|---|
| fsync／磁碟延遲 | MySQL、PostgreSQL、MongoDB、Valkey、ClickHouse | 主機層 [disk-latency](../components/host.md#disk-latency)；PostgreSQL 16 以前可加 `buffers_backend_fsync` |
| 行程 CPU、容器節流 | 全部（MySQL、PostgreSQL、MongoDB 連行程 CPU 都沒有） | [cpu-saturation](../components/host.md#cpu-saturation)、[container-cpu-throttling](../components/host.md#container-cpu-throttling) |
| 背景工作的池滿了 | PostgreSQL autovacuum、TiKV compaction、MySQL purge | 有 metric 的先加訊號（見[提議的訊號](#proposed)）；MySQL purge 只能看 history list 的結果 |
| 複寫 worker 個別狀態 | MySQL（collector 預設關）、MongoDB（oplog buffer 沒訊號） | 開 collector 或加訊號 |
| 記憶體被核心或 cgroup 拿走 | 全部 | [memory-available](../components/host.md#memory-available)、[swap-activity](../components/host.md#swap-activity)、[container-memory-limit](../components/host.md#container-memory-limit)、[oom-kills](../components/host.md#oom-kills) |

## 各類資源：飽和長什麼樣 {#classes}

### 連線 {#connections}

**飽和時。**新連線被拒（Valkey 見[^vk-maxclients-reject]），exporter 也進不來，比例規則在最需要時回傳空。機制與實測見[關聯式架構：連線](db-relational.md#connections)。

**誰看得到。**每張卡都有：[mysql#connections](../components/mysql.md#connections)、[postgres_exporter#connection-headroom](../components/postgres_exporter.md#connection-headroom)、[redis_exporter#connections](../components/redis_exporter.md#connections)、[mongodb_exporter#connections](../components/mongodb_exporter.md#connections)、[tidb#connections](../components/tidb.md#connections)、[yugabytedb#connections](../components/yugabytedb.md#connections)、[clickhouse#query-admission](../components/clickhouse.md#query-admission)。etcd 沒有連線上限，打滿的是 fd（[etcd#fd-headroom](../components/etcd.md#fd-headroom)）。

**缺口。**行程的 fd 上限（`RLIMIT_NOFILE`）通常比連線上限先到，只有 etcd 有訊號；其他用 [host#fd-headroom](../components/host.md#fd-headroom)。ClickHouse 的 `TCPConnection` 對 `max_connections` 沒有訊號。

### 執行緒與行程 {#threads}

**飽和時。**分兩種。一種是「每條連線一個執行單位」（MySQL thread、PostgreSQL backend），上限就是連線上限，執行中的數量（`Threads_running`、`state="active"`）超過 CPU 核數時大家一起變慢。另一種是固定大小的池（MongoDB ticket、TiKV 的 read pool／raftstore、YugabyteDB 的 RPC 佇列、ClickHouse 的 global thread pool），池滿時請求排隊或被拒。

**誰看得到。**[mysql#connections](../components/mysql.md#connections)（`Threads_running`）、[mongodb_exporter#tickets](../components/mongodb_exporter.md#tickets)、[redis_exporter#main-thread](../components/redis_exporter.md#main-thread)、[tidb#raftstore-latency](../components/tidb.md#raftstore-latency)、[tidb#server-busy](../components/tidb.md#server-busy)。

**缺口。**YugabyteDB 的 `rpcs_queue_overflow`、ClickHouse 的 `GlobalThreadActive`、TiKV 的 `tikv_worker_pending_task_total` 都有輸出但卡片沒有訊號。

### 記憶體池 {#memory}

**飽和時。**池用滿後不是馬上出錯，而是改走慢路：InnoDB 找不到空頁要等（`Innodb_buffer_pool_wait_free`）、WiredTiger 讓 app thread 去做 eviction、Valkey 開始 evict 或拒絕寫入、TiKV／YugabyteDB 拒絕請求、ClickHouse 讓查詢失敗。

**誰看得到。**[mysql#buffer-pool-misses](../components/mysql.md#buffer-pool-misses)、[mysql#innodb-pressure](../components/mysql.md#innodb-pressure)、[mongodb_exporter#wt-cache](../components/mongodb_exporter.md#wt-cache)、[redis_exporter#maxmemory-eviction](../components/redis_exporter.md#maxmemory-eviction)、[tidb#memory-quota](../components/tidb.md#memory-quota)、[yugabytedb#memory](../components/yugabytedb.md#memory)、[clickhouse#memory](../components/clickhouse.md#memory)。

**缺口。**池以外的記憶體（每條連線的 sort／join buffer、PostgreSQL 的 `work_mem`、Valkey 的 client buffer）沒有上限，加起來超過主機或 cgroup 時是 OOM kill，資料庫只看得到重啟（[host#oom-kills](../components/host.md#oom-kills)）。

### IO 與 fsync {#io}

**飽和時。**commit 要等 log fsync（InnoDB redo、PostgreSQL WAL[^dbr-pg-xlogflush]、etcd WAL、YugabyteDB log），磁碟慢時寫入延遲一起上升；另一種是背景寫不夠快，前景被迫自己寫（InnoDB `log_waits`、PostgreSQL backend 自己寫 buffer、RocksDB write stall）。

**誰看得到。**直接量 fsync 延遲的只有 [etcd#wal-fsync](../components/etcd.md#wal-fsync)、[etcd#backend-commit](../components/etcd.md#backend-commit)、[yugabytedb#wal-fsync](../components/yugabytedb.md#wal-fsync)。間接的：[mysql#innodb-pressure](../components/mysql.md#innodb-pressure)、[postgres_exporter#wal-io-sources](../components/postgres_exporter.md#wal-io-sources)、[postgres_exporter#checkpoint-pressure](../components/postgres_exporter.md#checkpoint-pressure)、[tidb#write-stall](../components/tidb.md#write-stall)、[yugabytedb#write-stall](../components/yugabytedb.md#write-stall)、[redis_exporter#aof-persistence](../components/redis_exporter.md#aof-persistence)。

**缺口。**MySQL、PostgreSQL、MongoDB、ClickHouse 沒有 fsync 延遲；用 [host#disk-latency](../components/host.md#disk-latency)。磁碟滿與唯讀也在主機層（[host#disk-space](../components/host.md#disk-space)、[host#filesystem-readonly](../components/host.md#filesystem-readonly)）。

### 複寫的 worker {#replication-workers}

**飽和時。**apply 跟不上，lag 持續增加。MySQL 預設依來源的 commit 順序提交（`replica_preserve_commit_order` 預設 ON[^res-my-commit-order]），一個 worker 卡住時後面的交易等它，整體 lag 才反映。Valkey 複本送不完時 output buffer 超過上限，複本被斷線、重新全量同步。

**誰看得到。**都只看結果：[mysql#replication-lag](../components/mysql.md#replication-lag)、[postgres_exporter#replication-lag](../components/postgres_exporter.md#replication-lag)、[mongodb_exporter#replication-lag](../components/mongodb_exporter.md#replication-lag)、[redis_exporter#full-resync](../components/redis_exporter.md#full-resync)、[tidb#replication-lag](../components/tidb.md#replication-lag)。

**缺口。**MySQL 每個 worker 的狀態要開 `perf_schema.replication_applier_status_by_worker`[^res-mx-default-applier]；Valkey 的 `client_output_buffer_limit_disconnections` 沒有訊號；TiKV 的 `tikv_channel_full_total` 沒有訊號。

### 背景工作 {#background}

**飽和時。**清理跟不上，結果慢慢累積：InnoDB history list、PostgreSQL dead tuple 與 wraparound、ClickHouse parts、TiKV／YugabyteDB 的 compaction 待處理量與 write stall。

**誰看得到。**[mysql#undo-history](../components/mysql.md#undo-history)、[postgres_exporter#autovacuum](../components/postgres_exporter.md#autovacuum)、[postgres_exporter#wraparound](../components/postgres_exporter.md#wraparound)、[clickhouse#merge-pool](../components/clickhouse.md#merge-pool)、[clickhouse#parts-pressure](../components/clickhouse.md#parts-pressure)、[tidb#write-stall](../components/tidb.md#write-stall)、[etcd#compaction](../components/etcd.md#compaction)。

**缺口。**「worker 全滿」本身：PostgreSQL 的 autovacuum worker 數可以用 `pg_stat_activity_count{backend_type="autovacuum worker"}` 對 `pg_settings_autovacuum_max_workers` 算（`pg_stat_activity` collector 預設開，且只算有 datname 與 state 的行程，autovacuum worker 在內[^res-pe-activity-join]）；TiKV 的 pending compaction 對 soft limit 可以在 write stall 之前先響。

### CPU {#cpu}

**飽和時。**資料庫通常不會報錯，只是延遲上升；容器被 CFS quota 節流時，平均使用率看起來不高也會發生。單執行緒的部分（Valkey 主執行緒、TiKV raftstore）會先到上限。

**誰看得到。**[redis_exporter#main-thread](../components/redis_exporter.md#main-thread)、[tidb#raftstore-latency](../components/tidb.md#raftstore-latency)（每個 thread 的 CPU）。其他都要看 [host#cpu-saturation](../components/host.md#cpu-saturation)、[host#container-cpu-throttling](../components/host.md#container-cpu-throttling)，或 process-exporter 依 thread 名稱分的 CPU（[host#process-threads-cpu](../components/host.md#process-threads-cpu)）。

## 資料庫內部和主機層的關係 {#relation}

只列會改變告警寫法的地方。

| 資料庫內部 | 主機層 | 對告警的影響 |
|---|---|---|
| InnoDB buffer pool（MySQL 8.4 沒設 `innodb_flush_method` 時用 O_DIRECT[^res-my-flush-odirect]，資料檔不經 page cache） | page cache | 這時 `Innodb_buffer_pool_reads` 大致等於讀磁碟；buffer pool 以外的 page cache 對 InnoDB 資料檔幫助不大 |
| PostgreSQL shared_buffers | page cache（雙層快取） | `blks_read` 只表示不在 shared_buffers，可能是 page cache 命中；cache hit 低不一定有磁碟讀，要對照 [host#disk-latency](../components/host.md#disk-latency) 的讀取量 |
| WiredTiger cache、Valkey maxmemory、ClickHouse 記憶體上限 | 行程 RSS ≠ 池大小 | 池以外的記憶體（連線 buffer、fragmentation、fork 的 copy-on-write）讓 RSS 超過池；OOM 的是 RSS，所以要另外看 [host#memory-available](../components/host.md#memory-available) 或 [host#container-memory-limit](../components/host.md#container-memory-limit) |
| 預設池大小依「可用記憶體」計算（WiredTiger (RAM − 1GB) × 50%[^mg-wt-cache-default]、ClickHouse RAM × 0.9[^ch-ss-mem-ratio]） | cgroup memory limit | MongoDB 與 ClickHouse 都把 cgroup 上限當成 RAM[^res-mg-memlimit][^res-mg-cgroup-limit][^res-ch-mem-cgroup]，池會跟著 limit 縮小；InnoDB、shared_buffers、maxmemory 是固定設定，不會。固定設定加上池外記憶體超過 limit 時是 cgroup OOM kill，資料庫的「用量 vs 上限」訊號看起來仍正常 |
| 池被換出（swap） | swap | hit ratio 不變、延遲上升；資料庫的 metric 看不出來，只有 [host#swap-activity](../components/host.md#swap-activity) |
| 連線上限、thread pool | CPU 核數、cgroup CPU quota | 執行中的 thread 多於核數時，`Threads_running`、ticket queue 變高是結果；原因要看 [host#cpu-saturation](../components/host.md#cpu-saturation) 與節流 |
| fsync 延遲（etcd、YugabyteDB 的直方圖） | 磁碟每次 IO 的時間 | 兩邊同時變慢 → 磁碟；只有資料庫變慢 → 資料庫內部（鎖、batch）。etcd、YugabyteDB 可以交叉判斷，其他資料庫只有主機這一邊 |
| 連線數、開檔數 | 行程的 `RLIMIT_NOFILE`、整機 `fs.file-max` | 先到的通常是行程 ulimit；node-mixin 只看整機，見 [host#fd-headroom](../components/host.md#fd-headroom) |
| 磁碟滿 | 檔案系統唯讀 | 兩者都讓寫入停止；node-mixin 的空間規則排除唯讀檔案系統[^res-nm-fs-readonly]，要另外有唯讀規則 |
| hybrid clock、TSO | 主機時鐘同步 | YugabyteDB 與 TiDB 卡片把「時鐘同步本身」列為盲點；用 [host#clock-sync](../components/host.md#clock-sync) 補 |

## 主機層怎麼補 {#host-layer}

[host](../components/host.md) 卡的訊號依級別：

| 級 | 訊號 | 資料來源 |
|---|---|---|
| 1 | [exporter-up](../components/host.md#exporter-up)、[disk-space](../components/host.md#disk-space)、[filesystem-readonly](../components/host.md#filesystem-readonly)、[oom-kills](../components/host.md#oom-kills)、[container-memory-limit](../components/host.md#container-memory-limit) | node_exporter、cAdvisor、kube-state-metrics |
| 2 | [collector-success](../components/host.md#collector-success)、[memory-available](../components/host.md#memory-available)、[swap-activity](../components/host.md#swap-activity)、[cpu-saturation](../components/host.md#cpu-saturation)、[container-cpu-throttling](../components/host.md#container-cpu-throttling)、[disk-latency](../components/host.md#disk-latency)、[fd-headroom](../components/host.md#fd-headroom)、[clock-sync](../components/host.md#clock-sync) | node_exporter（含 PSI）、cAdvisor、行程的 process_* 或 process-exporter |
| 3 | [disk-busy](../components/host.md#disk-busy)、[network-errors](../components/host.md#network-errors)、[process-threads-cpu](../components/host.md#process-threads-cpu) | node_exporter、process-exporter |

和上游規則比：

| | node-mixin（node_exporter v1.12.1） | kubernetes-mixin version-1.5.6 | 本卡 |
|---|---|---|---|
| exporter 停掉 | 沒有 | — | 有 |
| 磁碟空間、inode | 有[^res-nm-fs-filling][^res-nm-files] | PVC 有 | 有 |
| 檔案系統唯讀 | 排除，不告警[^res-nm-fs-readonly] | — | 有 |
| 磁碟延遲 | 只有佇列長度[^res-nm-diskio] | — | 每次 IO 的平均時間、PSI io |
| 記憶體 | MemAvailable 90%[^res-nm-mem]、major fault[^res-nm-majfault] | — | MemAvailable、PSI memory、swap |
| OOM kill | 沒有 | 沒有 | 主機、容器、K8s 三條 |
| CPU | 使用率（info）[^res-nm-cpu]、load per core[^res-nm-load] | 節流 25%[^res-km-throttling] | PSI cpu、節流 |
| 容器記憶體 vs limit | — | 沒有 | 有 |
| fd | 只看整機[^res-nm-fd] | — | 整機＋行程 |
| 時鐘 | 有[^res-nm-clock] | — | 同式 |

規則範本在 `rules/host/meta.yml`。所有 job selector 與門檻都是例子；node-mixin 的預設 selector 是 `job="node"`[^res-nm-selector]，名稱不同時整組回傳空。

## 提議的訊號 {#proposed}

有原始碼可證明 metric 存在、但 target 卡還沒有訊號的項目。都還沒有在 lab 看過數值，加進卡片時證據標 `code`。

| 卡片 | 提議 | metric |
|---|---|---|
| mysql、mariadb | thread cache 失效（`Threads_created` 速率高）；已加成 [mysql#thread-cache](../components/mysql.md#thread-cache)、[mariadb#thread-cache](../components/mariadb.md#thread-cache) | `mysql_global_status_threads_created`[^res-my-threads-created]、`mysql_global_variables_thread_cache_size` |
| mysql、mariadb | fsync 在排隊；已加成 [mysql#pending-fsyncs](../components/mysql.md#pending-fsyncs)、[mariadb#pending-fsyncs](../components/mariadb.md#pending-fsyncs)（MariaDB 10.11 起不含 redo log） | `mysql_global_status_innodb_data_pending_fsyncs`[^res-my-pending-fsyncs] |
| postgres_exporter | autovacuum worker 全滿 | `pg_stat_activity_count{backend_type="autovacuum worker"}`[^res-pe-activity-labels]、`pg_settings_autovacuum_max_workers`[^res-pg-av-workers] |
| postgres_exporter | backend 自己做 fsync（PG 16 以前） | `pg_stat_bgwriter_buffers_backend_fsync_total`[^res-pe-backend-fsync]；checkpoint 的 sync 時間 `pg_stat_bgwriter_checkpoint_sync_time_total`[^res-pe-ckpt-sync] |
| redis_exporter | 複本因 output buffer 被斷線 | `redis_client_output_buffer_limit_disconnections_total`[^res-rx-cobl]（Valkey、Redis 8） |
| tidb | compaction 待處理量接近 soft limit | `tikv_engine_pending_compaction_bytes`[^res-tikv-pending-compaction]、`tikv_config_rocksdb{name="soft_pending_compaction_bytes_limit"}`[^res-tikv-config-rocksdb][^res-tikv-soft-pending] |
| tidb | CPU 接近 quota | `process_cpu_seconds_total`、`tikv_server_cpu_cores_quota`[^res-tikv-cpu-quota]（同官方 TiKV_cpu_quota[^res-tikv-rule-cpu-quota]） |
| tidb | raftstore／apply channel 滿 | `tikv_channel_full_total`[^res-tikv-channel-full] |
| yugabytedb | RPC 佇列溢出 | `rpcs_queue_overflow`[^res-yb-queue-overflow] |
| clickhouse | global thread pool 接近上限 | `ClickHouseMetrics_GlobalThreadActive`[^res-ch-globalthread-active]、`max_thread_pool_size`[^res-ch-max-thread-pool]（設定值要用 query-exporter 查 system.server_settings） |
