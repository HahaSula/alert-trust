# 關聯式資料庫架構

這頁只寫**監控訊號或盲點依賴的機制**：MySQL／MariaDB（InnoDB）與 PostgreSQL 並排。每一節依序是機制、什麼會壞、哪個訊號看得到。訊號連到元件卡（[mysqld_exporter](../components/mysqld_exporter.md)、[postgres_exporter](../components/postgres_exporter.md)）；資料庫該監控什麼的完整清單在 [MySQL／MariaDB 本體怎麼監控](../targets/mysql.md) 與 [PostgreSQL 本體怎麼監控](../targets/postgres.md)。

以 MySQL 8.4.7、MariaDB 11.4.13、PostgreSQL 17.11（部分 16.15）原始碼與 `lab/targets/` 的實驗為準。<span class="at-chip at-ev-code">讀碼</span> <span class="at-chip at-ev-tested">實測</span>

## 先講結論

1. **連線打滿時 exporter 也進不來。**兩邊都是每條連線一個執行單位，上限擋的是所有人；要讓 exporter 在打滿時仍看得到，得走保留連線或獨立的埠。
2. **鎖可以讓監控整個消失。**PostgreSQL exporter 只有一條連線、沒設 `lock_timeout`，一個 ACCESS EXCLUSIVE 就讓整次 scrape 超時；mysqld_exporter 把自己的 `lock_wait_timeout` 設成 2 秒，沒有這個問題。
3. **MVCC 清理被擋時，exporter 看得到「結果」，看不到「誰擋的」。**InnoDB 看 history list length，PostgreSQL 看 dead tuple；擋住的交易、slot、prepared transaction 要另外查。
4. **每個「lag」量的東西不一樣。**MySQL 的 `Seconds_Behind_Source` 與 PostgreSQL 的 `pg_replication_lag_seconds` 在複本斷線時都可能是 0 或消失。
5. **兩邊的核心都不自己 failover。**MySQL 的共識只在 Group Replication／Galera；PostgreSQL 要靠 Patroni 這類工具加 DCS。

## 對照 {#compare}

| 機制 | MySQL／MariaDB（InnoDB） | PostgreSQL | 對監控的影響 | 本頁 |
|---|---|---|---|---|
| 連線 | 每條連線一條 thread；MariaDB 可改 thread pool | 每條連線一個 backend process | 打滿時 exporter 也被拒 | [連線](#connections) |
| 保留連線 | max_connections + 1，給 CONNECTION_ADMIN；另有 admin port／extra_port | `superuser_reserved_connections`、16 起 `reserved_connections` | 決定 exporter 打滿時能不能進來 | [連線](#connections) |
| 快取 | buffer pool | shared_buffers（再加 OS page cache） | PG 的 cache hit 只算 shared_buffers | [快取與 checkpoint](#buffer-checkpoint) |
| checkpoint 壓力 | redo 用到快滿時寫入的 thread 要等 | WAL 量達 `max_wal_size` 就提前 checkpoint | MySQL 卡片沒有 checkpoint age 訊號 | [快取與 checkpoint](#buffer-checkpoint) |
| 交易日誌 | redo log（InnoDB）＋ binlog（server），兩份 | 一份 WAL，同時給 recovery、複寫、archive | PG 的 WAL 被 slot 或 archive 卡住時會寫滿磁碟 | [日誌](#redo-wal) |
| MVCC 清理 | purge，受最舊的 read view 限制 | vacuum，受 xmin horizon 限制 | 擋住的人 exporter 看不到 | [MVCC 清理](#mvcc) |
| 交易 ID 回捲 | 卡片沒有對應訊號 | 有：2^31 前開始警告，再近就拒絕新 XID | PG 要長期看 datfrozenxid age | [MVCC 清理](#mvcc) |
| 鎖 | row lock（InnoDB）、metadata lock（server） | heavyweight lock（含表鎖），新請求排在等待中的請求後面 | PG 上一個 DDL 可以擋住 exporter | [鎖](#locks) |
| 複寫 | binlog → relay log → SQL thread；GTID | WAL streaming；replication slot | lag 的定義不同 | [複寫](#replication) |
| 同步複寫 | semi-sync，ACK 逾時**退回 async** | `synchronous_standby_names`，standby 不夠時 **commit 等待** | 一個是悄悄降級，一個是寫入卡住 | [複寫](#replication) |
| 自動 failover | 只有 Group Replication／Galera | 沒有；靠 Patroni 等工具 | 角色要從 metric 推，不能用靜態 label | [HA 與角色](#ha) |

## 連線 {#connections}

**機制。**MySQL 每條新連線在沒有閒置 thread 時建立一條 thread[^dbr-my-thread-per-conn]；MariaDB 的 `thread_handling` 可以是 one-thread-per-connection 或 pool-of-threads[^dbr-mdb-thread-handling]。PostgreSQL 每條連線由 postmaster 啟動一個 backend process[^dbr-pg-backend-fork]。兩邊的 `max_connections` 都是「同時存在的執行單位」上限，exporter 也算在內。exporter 自己用的連線數：mysqld_exporter 每次 scrape 最多 2 條[^my-pool-default]，postgres_exporter 的 collector 只有 1 條[^pg-maxopen]。

| | MySQL／MariaDB | PostgreSQL |
|---|---|---|
| 保留給誰 | 第 max_connections + 1 條給 CONNECTION_ADMIN（或 SUPER）[^mysql-conn-reserved] | `superuser_reserved_connections` 給 superuser；16 起 `reserved_connections` 給 `pg_use_reserved_connections`[^pg-src-reserved][^pg-trace-pg17-guc-reserved] |
| 不受上限限制的路 | MySQL admin port（要 SERVICE_CONNECTION_ADMIN）[^my-q-my-admin-bypass][^my-q-my-admin-priv]；MariaDB `extra_port`，有自己的 `extra_max_connections`（預設 1）[^my-q-mdb-extra-scheduler][^my-q-mdb-extra-max-default] | 沒有獨立的埠，只有保留名額 |

**什麼會壞。**

- MySQL 打滿時 exporter 收到 1040，`mysql_up` 變 0、`threads_connected` 消失，比例規則在最需要時回傳空。給 CONNECTION_ADMIN 只救得回一條連線，exporter 的第二條仍被拒，7 個 collector 失敗。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha]
- 讓 query-exporter 走 admin port／extra_port，打滿時兩台都還讀得到連線數。<span class="at-chip at-ev-tested">實測</span>[^my-q-exp]
- PostgreSQL 打滿時 `pg_up` 仍是 1（舊架構的連線早就建好），新架構每次 scrape 開新連線失敗，collector 結果整組消失。<span class="at-chip at-ev-tested">實測</span>[^pg-exp-target]連線風暴實驗中有 35 秒沒有資料。<span class="at-chip at-ev-tested">實測</span>[^pg-perf-exp]

**哪個訊號看得到。**

| | MySQL／MariaDB | PostgreSQL |
|---|---|---|
| 打滿之前 | [connections](../components/mysqld_exporter.md#connections)、[connection-headroom](../components/mysqld_exporter.md#connection-headroom) | [connection-headroom](../components/postgres_exporter.md#connection-headroom)、[connection-trend](../components/postgres_exporter.md#connection-trend) |
| 打滿當下 | [db-up](../components/mysqld_exporter.md#db-up)、[exporter-query-timeout](../components/mysqld_exporter.md#exporter-query-timeout)、[custom-queries](../components/mysqld_exporter.md#custom-queries) | [collectors-present](../components/postgres_exporter.md#collectors-present)（`pg_up` 不會變） |

## 快取與 checkpoint {#buffer-checkpoint}

**機制。**

- InnoDB 的頁都經過 buffer pool；要到磁碟讀時 `Innodb_buffer_pool_reads` 加 1，找不到空頁要等時 `Innodb_buffer_pool_wait_free` 加 1。[^my-asp-my-bp-reads][^my-asp-my-bp-wait-free][^my-asp-mdb-bp-wait-free]Redo 可用空間不夠時，寫入的 thread 在 `log_free_check_wait` 等 checkpoint 推進。[^dbr-my-log-free-check-wait]離 checkpoint 多遠：MariaDB 是 `Innodb_checkpoint_age`，MySQL 是 `Innodb_redo_log_current_lsn − Innodb_redo_log_checkpoint_lsn`。[^my-perf-mdb-checkpoint-age][^my-perf-my-redo-lsn]
- PostgreSQL 的 shared_buffers 之外還有 OS page cache；`blks_read` 只表示不在 shared_buffers，不一定碰到磁碟。自上次 checkpoint 起的 WAL 量達到由 `max_wal_size` 算出的 segment 數，就要求一次 checkpoint（記在 requested，不是 timed）。[^dbr-pg-ckpt-needed][^pg-trace-pg17-ckpt-req]17 把這些計數從 `pg_stat_bgwriter` 搬到 `pg_stat_checkpointer`。[^pg-src-bgwriter16][^pg-src-checkpointer-view]

**什麼會壞。**

- Buffer pool 縮到 16M：`buffer_pool_reads` 從 0–18/s 升到 1,946／1,437/s，TPS 約降 10%；`wait_free` 只有 MySQL 動，MariaDB 一直是 0。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-perf]
- Redo 縮到最小時 checkpoint age 一直很低（InnoDB 一直在刷頁），`Innodb_log_waits` 仍是 0，沒有重現吞吐量下降。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-perf]
- PostgreSQL `max_wal_size=32MB`：60 秒內 `checkpoints_req` 3 → 9，log 出現「checkpoints are occurring too frequently」，限速負載下 TPS 不變。<span class="at-chip at-ev-tested">實測</span>[^pg-perf-exp][^pg-perf-src-ckpt-frequent]
- PostgreSQL 17 上 stat_checkpointer collector 預設關閉，舊名稱消失，checkpoint 規則回傳空。<span class="at-chip at-ev-tested">實測</span>[^pg-exp-target][^pg-c-checkpointer-default]

**哪個訊號看得到。**

| | MySQL／MariaDB | PostgreSQL |
|---|---|---|
| 快取不夠 | [buffer-pool-misses](../components/mysqld_exporter.md#buffer-pool-misses)、[innodb-pressure](../components/mysqld_exporter.md#innodb-pressure) | [cache-and-temp](../components/postgres_exporter.md#cache-and-temp)、[resource-pressure](../components/postgres_exporter.md#resource-pressure) |
| checkpoint 壓力 | 卡片沒有訊號；global status 的數值都會輸出[^my-global-status-re]，實驗用這些 metric 算出 checkpoint age[^exp-mysql-perf] | [checkpoint-pressure](../components/postgres_exporter.md#checkpoint-pressure)、[version-semantics](../components/postgres_exporter.md#version-semantics) |

## 交易日誌：redo／binlog 與 WAL {#redo-wal}

**機制。**

- InnoDB 寫 redo log，server 另外寫 binlog（給複寫與 PITR）。commit 時要不要 fsync 由兩個設定決定：`innodb_flush_log_at_trx_commit` 與 `sync_binlog`。[^my-asp-my-flush-log-trx][^my-asp-my-sync-binlog]`Innodb_log_waits` 量的是 **log buffer** 不夠，不是 redo 檔太小。[^my-perf-my-log-waits][^my-perf-mdb-log-waits]binlog 依 `binlog_expire_logs_seconds` 刪除；MariaDB 還可以設成要等 replica 連上才刪。[^my-asp-my-binlog-expire][^my-asp-mdb-purge-needs-slave]
- PostgreSQL 只有一份 WAL。commit 呼叫 `XLogFlush` 把 WAL 寫到 commit 位置並 fsync，`synchronous_commit` 預設 on。[^dbr-pg-xlogflush][^dbr-pg-sync-commit-default]舊的 WAL segment 只有在已 archive（開了 archive 時）、而且沒有 slot 需要時才會刪。[^dbr-pg-remove-archived][^pg-src-wal-avail]

**什麼會壞。**

- MySQL 的耐久性設定被調鬆時沒有任何錯誤，只有變數值不同。
- PostgreSQL 的 archive 失敗或 slot 沒人用時，WAL 在主庫累積；pg_wal 寫滿時 backend PANIC、資料庫停機，exporter 只剩 `pg_up=0`，之前的 `pg_wal_size_bytes` 是唯一的預警。<span class="at-chip at-ev-tested">實測</span>[^pg-exp-target-raw]
- archive 失敗時 `failed_count` 加 1，`archive_command` 依序重試。[^pg-trace-pg17-arch-failinc][^pg-trace-pg17-arch-retries]

**哪個訊號看得到。**

| | MySQL／MariaDB | PostgreSQL |
|---|---|---|
| 耐久性 | [durability-settings](../components/mysqld_exporter.md#durability-settings) | 卡片沒有 `synchronous_commit` 的訊號 |
| 日誌等待 | [innodb-pressure](../components/mysqld_exporter.md#innodb-pressure) | [wal-io-sources](../components/postgres_exporter.md#wal-io-sources)（自訂查詢） |
| 日誌堆積 | [binlog-growth](../components/mysqld_exporter.md#binlog-growth)、[disk-space](../components/mysqld_exporter.md#disk-space) | [wal-disk](../components/postgres_exporter.md#wal-disk)、[replication-slots](../components/postgres_exporter.md#replication-slots) |
| 日誌保存 | [binlog-pitr](../components/mysqld_exporter.md#binlog-pitr) | [archiver](../components/postgres_exporter.md#archiver)、[archive-freshness](../components/postgres_exporter.md#archive-freshness)、[archive-config](../components/postgres_exporter.md#archive-config) |

## MVCC 清理：purge 與 vacuum {#mvcc}

<!-- figure: db-relational-horizon -->

**圖**　誰擋住 MVCC 清理。機制：InnoDB 的 purge 以最舊的 read view 為界[^dbr-my-purge-view]；PostgreSQL 的 horizon 取 replication slot 的 xmin 與每個 proc 的 xid／xmin 的最小值[^dbr-pg-horizon-slots][^dbr-pg-horizon-procs]，prepared transaction 也算在內[^pg-q-pg17-view-prepared]。
{: .at-caption }

**機制。**

- InnoDB 的舊版本放在 undo。purge 複製最舊的 read view，只清得到比它舊的 undo[^dbr-my-purge-view]；還沒清的長度是 history list length（MariaDB 是 status 變數，MySQL 在 `INNODB_METRICS`）。[^my-trace-mdb-histlen-var][^my-trace-my-histlen-mon]
- PostgreSQL 的舊版本留在表裡。vacuum 只能移除比 xmin horizon 舊的 dead tuple；horizon 由 slot 的 xmin 和每個 backend 的 xid／xmin 決定。[^dbr-pg-horizon-slots][^dbr-pg-horizon-procs]另外每個 XID 要在回捲前被凍結：離回捲 4000 萬時開始警告，300 萬時拒絕新的 XID。[^pg-src-xid-warn][^pg-src-xid-stop]

**什麼會壞。**

- InnoDB：consistent snapshot 開 60 秒、每秒約 300 筆 UPDATE，history list 到 13,663（MariaDB）／13,474（MySQL），交易結束後 40 秒內降回來，吞吐量不變。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-perf]
- PostgreSQL：一個 idle in transaction 的 session 讓 VACUUM 回報「10000 are dead but not yet removable」；exporter 看得到交易時間與 `n_dead_tup`，看不到卡住幾個 XID。<span class="at-chip at-ev-tested">實測</span>[^pg-exp-target-raw]
- 兩邊的 exporter 都不輸出「誰擋住的」：MySQL 的 `innodb_trx` 在沒有交易時不輸出 series[^my-innodb-trx-query]；PostgreSQL 的 slot xmin、prepared transaction 要靠自訂查詢。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha][^pg-q-exp]

**哪個訊號看得到。**

| | MySQL／MariaDB | PostgreSQL |
|---|---|---|
| 擋住的人 | [long-transactions](../components/mysqld_exporter.md#long-transactions) | [xmin-horizon](../components/postgres_exporter.md#xmin-horizon)（含 query-exporter 的 slot、prepared） |
| 清不掉的量 | [undo-history](../components/mysqld_exporter.md#undo-history) | [autovacuum](../components/postgres_exporter.md#autovacuum) |
| 長期風險 | — | [wraparound](../components/postgres_exporter.md#wraparound)（`database_wraparound` 預設關[^pg-c-wraparound-default]，名稱是 seconds，值是交易數） |

## 鎖 {#locks}

**機制。**

- InnoDB 的 row lock 由儲存引擎管，等太久依 `innodb_lock_wait_timeout` 回 1205；server 層另有 metadata lock（MDL），DDL 要排他的 MDL，排在開著的交易後面，之後所有用到這張表的語句排在 DDL 後面。DDL 等 MDL 的上限是 `lock_wait_timeout`，預設 MySQL 一年、MariaDB 一天。[^my-perf-my-lock-wait-timeout][^my-perf-mdb-lock-wait-timeout]
- PostgreSQL 的表鎖與列鎖都是 heavyweight lock。新的請求只要和**等待中**的請求衝突就要排隊。[^pg-perf-src-lock-waitmask]

**為什麼鎖會擋住 exporter。**

| | mysqld_exporter | postgres_exporter |
|---|---|---|
| 會等表鎖的查詢 | — | stat_user_tables（預設開）對每張表呼叫 `pg_table_size()`，要 AccessShareLock[^pg-c-user-tables-size] |
| 自我保護 | 每條連線 `SET lock_wait_timeout=2`[^my-trace-ex-lockwait] | 不設 `lock_timeout`；只有一條連線[^pg-maxopen] |
| 一個 DDL 卡住時 | exporter 不受影響；MDL 實驗中 9 個 session 在等，exporter 照常回報 | 整次 scrape 超時，`up=0` 共 40 秒；每次 scrape 再多一條等鎖的連線 |
| 證據 | <span class="at-chip at-ev-tested">實測</span>[^exp-mysql-perf] | <span class="at-chip at-ev-tested">實測</span>[^pg-perf-exp][^pg-exp-target-raw] |

給 PostgreSQL 的 exporter 帳號設 `lock_timeout=1s` 後，只有 stat_user_tables 失敗，`up` 維持 1。<span class="at-chip at-ev-tested">實測</span>[^pg-perf-exp]

**哪個訊號看得到。**

| | MySQL／MariaDB | PostgreSQL |
|---|---|---|
| 列鎖等待 | [lock-waits](../components/mysqld_exporter.md#lock-waits)（等 row lock 的 session 在 processlist 是 `updating`） | [sessions-and-locks](../components/postgres_exporter.md#sessions-and-locks)（`pg_locks_count` 不分已授予與等待中[^pg-perf-c-locks-nogranted]） |
| DDL 鎖佇列 | [metadata-locks](../components/mysqld_exporter.md#metadata-locks) | [lock-queue](../components/postgres_exporter.md#lock-queue) |
| 監控被鎖住 | [exporter-query-timeout](../components/mysqld_exporter.md#exporter-query-timeout) | [lock-blocks-exporter](../components/postgres_exporter.md#lock-blocks-exporter) |
| deadlock | [deadlocks](../components/mysqld_exporter.md#deadlocks) | [deadlocks-rollbacks](../components/postgres_exporter.md#deadlocks-rollbacks) |

## 複寫 {#replication}

**機制。**

- MySQL：primary 寫 binlog，replica 的 IO thread 收進 relay log，SQL thread（或 applier worker）套用。GTID 讓 replica 依交易而不是檔案位置接續；`gtid_executed` 是字串，exporter 不輸出。[^my-q-my-gtid-executed][^my-global-variables-parse]Semi-sync 等 replica ACK 逾時時，計數加 1 並退回 async，commit 照常成功。[^dbr-my-semisync-timeout]<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha]
- PostgreSQL：walsender 把 WAL 傳給 standby 的 walreceiver。replication slot 讓主庫保留 standby 還沒收的 WAL；設了 `max_slot_wal_keep_size` 時，超過的 slot 被標成 lost。[^pg-src-wal-avail][^pg-trace-pg17-slot-invalidate]同步複寫時，同步 standby 不夠就讓 commit 等待，不退回 async。[^pg-asp-src17-syncrep-wait]

**每個「lag」量的是什麼。**

| metric | 定義 | 會騙人的時候 | 證據 |
|---|---|---|---|
| `mysql_slave_status_seconds_behind_source` | 現在減去 SQL thread 正在套用的事件在 primary 上的時間戳[^mysql-sbs-compute] | SQL thread 停、或 IO thread 停且已追完：NULL，exporter 不輸出[^mysql-sbs-null]；MariaDB 只要 IO thread 不是 Yes 就是 NULL[^my-trace-mdb-sbm-null] | <span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha] |
| heartbeat（`mysql_heartbeat_*`） | primary 寫入的時間戳到 replica 的端到端延遲[^my-heartbeat-query] | 寫入程式停了，所有 replica 一起變大 | <span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha] |
| `pg_stat_replication_pg_wal_lsn_diff` | 主庫上，目前 WAL 位置減去 standby 的 replay 位置（位元組）[^pg-c-statrep-query] | standby 斷線時那一列消失 | <span class="at-chip at-ev-tested">實測</span>[^pg-exp-target] |
| `pg_replication_lag_seconds` | 複本上，receive 等於 replay 就回 0，否則現在減最後套用的交易時間[^pg-c-replication-lag] | receiver 斷線重連、或主庫沒寫入：0；複本重啟後 receiver 第一次啟動：變成時間差 | <span class="at-chip at-ev-tested">實測</span>[^pg-exp-target] |
| `write_lag`／`flush_lag`／`replay_lag` | 主庫算的時間延遲；exporter 不輸出 | 閒置時被清成 NULL[^pg-src-lag-clear] | <span class="at-chip at-ev-tested">實測</span>[^pg-exp-target] |

**哪個訊號看得到。**

| | MySQL／MariaDB | PostgreSQL |
|---|---|---|
| 複寫在跑 | [replication-running](../components/mysqld_exporter.md#replication-running)、[replica-status-present](../components/mysqld_exporter.md#replica-status-present) | [standby-streaming](../components/postgres_exporter.md#standby-streaming)、[wal-receiver](../components/postgres_exporter.md#wal-receiver) |
| 延遲 | [replication-lag](../components/mysqld_exporter.md#replication-lag) | [replication-lag](../components/postgres_exporter.md#replication-lag)、[replication-semantics](../components/postgres_exporter.md#replication-semantics) |
| 同步複寫 | [semi-sync](../components/mysqld_exporter.md#semi-sync) | [sync-replication](../components/postgres_exporter.md#sync-replication) |
| 交易一致 | [gtid-consistency](../components/mysqld_exporter.md#gtid-consistency) | — |
| 保留的日誌 | [binlog-growth](../components/mysqld_exporter.md#binlog-growth) | [replication-slots](../components/postgres_exporter.md#replication-slots) |

## HA 與角色 {#ha}

**機制。**

- MySQL／MariaDB 的 async 複寫不選主。primary 被 kill 時 replica 只是 `io_running=0`、`last_io_errno=2003`，primary 回來後自動重連；實驗中的 failover 是手動做的（`RESET REPLICA ALL`、關 `read_only`）。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha]多數決只在 Group Replication（成員自己判斷有沒有過半）與 Galera（不在 Primary component 的節點拒絕服務）。[^my-asp-my-gr-quorum][^my-asp-mdb-wsrep-nonprimary]
- PostgreSQL 核心沒有偵測主庫故障的機制，`pg_promote()` 只是寫一個 promote 信號檔，由人或外部工具決定。[^pg-asp-src17-doc-nofailover][^pg-asp-src17-promote]所以 postgres_exporter 卡的 cluster 面向記在「不適用的面向」，理由見[為什麼核心 PostgreSQL 沒有共識](../targets/postgres.md#cluster-why)。用 Patroni 時，主庫是 DCS（例如 etcd）裡拿得到 leader key 的節點。[^pg-asp-patroni-doc-dcs]

**什麼會壞。**

- 靜態的 `role="primary"` label 在 failover 後指向舊節點；舊 primary 回來時兩台都可寫。MySQL 與 PostgreSQL 的實驗都出現兩個「主庫」。<span class="at-chip at-ev-tested">實測</span>[^exp-mysql-ha][^pg-exp-target]
- 只看角色變化的規則不知道舊主庫還在接受寫入：PostgreSQL 被提升的那台 `changes(pg_replication_is_replica)` 是 1，舊主庫是 0。<span class="at-chip at-ev-tested">實測</span>[^pg-exp-target]

**哪個訊號看得到。**

| | MySQL／MariaDB | PostgreSQL |
|---|---|---|
| 從 metric 推角色 | [role-detection](../components/mysqld_exporter.md#role-detection)、[primary-writable](../components/mysqld_exporter.md#primary-writable)、[replica-read-only](../components/mysqld_exporter.md#replica-read-only) | [promotion](../components/postgres_exporter.md#promotion) |
| 有共識時 | [gr-quorum](../components/mysqld_exporter.md#gr-quorum)、[galera-quorum](../components/mysqld_exporter.md#galera-quorum) | Patroni：[patroni-leader](../components/postgres_exporter.md#patroni-leader)、[patroni-dcs](../components/postgres_exporter.md#patroni-dcs) |
