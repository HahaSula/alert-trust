# ClickHouse 怎麼監控

**目標：知道 ClickHouse 叢集現在能不能寫入與查詢（Keeper 有 leader、Replicated 表不是唯讀、part 數離 Too many parts 還遠、mutation 沒有卡住、記憶體與磁碟有餘裕），以及哪一台、哪一張表離不能還有多遠。**ClickHouse 有兩種 process：server（MergeTree 表、查詢、ReplicatedMergeTree 的複寫）與 ClickHouse Keeper（Raft，存複寫 log 與 ON CLUSTER DDL）。兩者都有原生 `/metrics`，但預設是關的；打開後只有全伺服器的數字，每張表的狀態要用 SQL 查 system 表。這頁依九個面向從大到小排列：叢集 → 複寫 → 可用性 → 資源 → 工作量 → 維護 → 保護 → 容量，最前面是監測自檢。

依據是 ClickHouse v26.10.2.103-stable 的原始碼（`gh api .../releases/latest` 回的最新 stable，2026-10-24；最新 LTS 是 v26.8.34.13-lts；Apache-2.0），加上一組小實驗：server ×2（1 shard、2 replicas）、Keeper ×3，做 Too many parts、失敗的 mutation、記憶體與並行上限、Distributed 送出、備份、replica 落後與停機、Keeper 失去 quorum、`<prometheus>` 關掉<span class="at-chip at-ev-tested">實測</span>[^ch-exp]。這台機器的 Docker 容器時鐘比牆鐘慢約 30%；秒數以主機 monotonic 時鐘量的 SQL、HTTP 耗時為準，Prometheus 的 `for` 與 query-exporter 的 interval 只當參考。訊號與規則在[元件卡](../components/clickhouse.md)，原始碼位置在[附錄](clickhouse-appendix.md)，機制在[分析型架構](../architecture/db-olap.md)與[共識（Raft）架構](../architecture/db-raft.md#clickhouse-keeper)。25.3 到 26.10 之間的 metric 差異見 [ClickHouse 各版本的監控差異](versions/clickhouse.md)。

## 先講結論

1. **原生 `/metrics` 預設是關的，卡片以「打開它」為前提。**預設 `config.xml` 的 `<prometheus>` 在註解裡；沒打開時 9363 不 listen，Prometheus 看到 connection refused，和 process 停掉一樣。打開後只有 `system.metrics`、`events`、`asynchronous_metrics`、`errors` 的全伺服器數字。<span class="at-chip at-ev-tested">實測</span>[^ch-cfg-prom-commented][^ch-prom-handler][^ch-exp-raw]
2. **Keeper 失去 quorum 時，server 的 `up`、`/ping`、`/replicas_status` 都是 200。**寫入失敗 `TABLE_IS_READ_ONLY`，讀取照常；`/replicas_status` 跳過唯讀的表，`ReplicasMaxAbsoluteDelay` 也不算唯讀的表。看得到的是 `ReadonlyReplica`、`ZooKeeperSession`、Keeper 的 `/ready` 與 SQL 探測。<span class="at-chip at-ev-tested">實測</span>[^ch-exp-raw][^ch-rs-readonly-skip][^ch-sam-skip-readonly]
3. **Altinity 的 Keeper quorum 規則在真正失去 quorum 時不會響。**`ClickHouseKeeperLostQuorum` 以 `KeeperIsLeader == 1` 為前提，沒有 leader 時回傳空；lab 停 2 台 Keeper 時它沒有 pending。4lw 的 `ruok` 這時仍回 `imok`。<span class="at-chip at-ev-tested">實測</span>[^alt-k-lostquorum][^ch-k-ruok][^ch-exp-raw]
4. **每張表的狀態要靠 SQL：query-exporter 5.2.2 可以直接用。**映像內建 clickhouse-sqlalchemy 0.3.2，`clickhouse+native://` 連 9000；lab 用它補上每張表的唯讀、延遲、複寫佇列、mutation 失敗、detached parts、Distributed 佇列與備份。Altinity 的 metrics-exporter 只能在 Kubernetes operator 下使用。<span class="at-chip at-ev-tested">實測</span>[^ch-exp-qe][^alt-exporter-kube]
5. **mutation 失敗時 `/metrics` 只看得到錯誤碼的計數。**`ALTER ... UPDATE` 立刻成功，背景一直重試；lab 40 秒內兩台的 `latest_fail_reason` 都有錯誤，`/metrics` 只有 `ClickHouseErrorMetric_FUNCTION_THROW_IF_VALUE_IS_NON_ZERO` 增加。<span class="at-chip at-ev-tested">實測</span>[^ch-sys-mut-fail][^ch-exp-raw]
6. **一些「看起來有」的 metric 在預設設定下沒有。**`NumberOfDetachedParts`、`NumberOfPendingMutations` 要打開 `asynchronous_metrics_enable_heavy_metrics`；26.8 起 `DiskAvailable_default` 改成 `DiskAvailable{disk="default"}`。照舊名稱寫的規則回傳空。<span class="at-chip at-ev-tested">實測</span>[^ch-ss-heavy-enable][^ch-ss-kv-mode][^ch-exp-raw]
7. **Too many parts 先延遲、再拒絕，原生 metric 都有。**lab（門檻 20／40、停 merge、逐筆 insert）：`DelayedInserts` 20、`RejectedInserts` 6，CHInsertsDelayed、CHInsertsRejected 在 25 秒內 firing。預設開著的 `async_insert` 不合併同一個 client 一筆接一筆的 insert（21 次 insert、21 次 flush）。<span class="at-chip at-ev-tested">實測</span>[^ch-mtd-delay-fn][^ch-set-async-insert][^ch-exp-raw]

### 九個面向一覽 {#aspects}

| 面向 | 這頁的重點 | 元件卡的訊號 | 證據 |
|---|---|---|---|
| [監測自檢](#exporter) | `<prometheus>` 要打開；三方交叉；metric 消失；SQL 路徑本身 | endpoint-up、health-second-path、metric-families-present、scrape-budget、endpoint-blocking、sql-path | <span class="at-chip at-ev-tested">實測</span> |
| [叢集](#cluster) | Keeper leader 與 quorum；server 的 Keeper session；台數 | role-detection、keeper-quorum、component-count、keeper-session、keeper-latency | <span class="at-chip at-ev-tested">實測</span> |
| [複寫](#replication) | 唯讀、延遲（量的是真的嗎）、佇列、Distributed 送出 | replica-readonly、replication-lag、replication-queue、distributed-queue | <span class="at-chip at-ev-tested">實測</span> |
| [可用性](#availability) | insert 被拒、查詢被並行上限拒 | writes-rejected、query-admission | <span class="at-chip at-ev-tested">實測</span> |
| [資源與飽和](#saturation) | part 數對門檻、記憶體 | parts-pressure、memory | <span class="at-chip at-ev-tested">實測</span> |
| [工作量](#workload) | 失敗查詢比例、資料類錯誤碼 | failed-queries、error-counters | <span class="at-chip at-ev-tested">實測</span>／<span class="at-chip at-ev-code">讀碼</span> |
| [資料維護](#maintenance) | merge 池、mutation、detached parts | merge-pool、mutations、detached-parts | <span class="at-chip at-ev-tested">實測</span>／<span class="at-chip at-ev-code">讀碼</span> |
| [資料保護](#protection) | 原生 BACKUP、資料遺失 | backups、data-integrity | <span class="at-chip at-ev-tested">實測</span>／<span class="at-chip at-ev-code">讀碼</span> |
| [容量](#capacity) | 每個 disk 的剩餘空間 | disk-space | <span class="at-chip at-ev-code">讀碼</span> |

## 監控模型與官方建議 {#model}

ClickHouse 屬於「資料庫內建端點」模型（見[資料庫監控模型](models.md#clickhouse)），但內建端點不夠，alert-trust 的基礎是**原生 `<prometheus>` 加上 SQL 查 system 表**。

| 項目 | 內容 |
|---|---|
| 抓什麼 | 每台 server 與 Keeper 的 `<prometheus>` `/metrics`（例子用 9363）；**必須在設定裡打開**，沒打開的 server 被抓時是 connection refused（[endpoint-up](#exporter)），打開但關掉子項時由 [metric-families-present](#exporter) 抓 |
| SQL 補什麼 | `system.replicas`、`replication_queue`、`parts`、`merge_tree_settings`、`mutations`、`detached_parts`、`distribution_queue`、`zookeeper_connection`、`backups`（`queries/clickhouse/clickhouse.yaml`） |
| 健康端點 | server：`/ping`（靜態 `Ok.`）、`/replicas_status`（只看延遲，不看唯讀）；Keeper：`/ready`（有 leader 才 200）、4lw `ruok`（一律 `imok`）、`mntr` |
| 官方建議 | 文件列出 system 表與 `/ping`、`/replicas_status`；ClickHouse 專案沒有規則或 mixin（v26.10.2.103-stable 的檔案樹裡沒有；[ClickHouse Monitoring](https://clickhouse.com/docs/operations/monitoring)） |
| 社群規則 | Altinity clickhouse-operator release-0.27.4 的 `prometheus-alert-rules-clickhouse.yaml`（用 operator 內 metrics-exporter 的 `chi_clickhouse_*`）、`-chkeeper.yaml`（用 Keeper 原生 `/metrics`）、`-backup.yaml`（clickhouse-backup 工具） |

**Altinity 規則對 v26.10.2.103 的檢查。**lab 用 `altinity_names.py` 把規則裡每個名稱對到 ClickHouse 的 system 表與 Keeper 的 `/metrics`；Keeper 規則的 10 個名稱都存在，ClickHouse 規則 47 個名稱有 1 個不存在。<span class="at-chip at-ev-tested">實測</span>[^ch-exp-altinity]

| Altinity 規則 | 用的名稱 | 問題 | 我們 |
|---|---|---|---|
| ClickHouseDNSErrors[^alt-dns] | `chi_clickhouse_event_DNSError` **or** `..._NetworkErrors` | `NetworkErrors` 在 v26.10.2.103 不是 ProfileEvent（system.events 沒有），後半永遠空；有 `or` 所以前半仍有效 | 不收（DNS 錯誤看 `ClickHouseErrorMetric_*`） |
| ClickHouseKeeperLostQuorum[^alt-k-lostquorum] | `KeeperSyncedFollowers < 1 and KeeperIsLeader == 1` | 失去 quorum 時沒有 leader，回傳空（lab：停 2 台 Keeper 沒有 pending） | CHKeeperQuorumLost（`/ready` 成功數）、CHKeeperNoLeader（absent） |
| ClickHouseRejectedInsert、DelayedInsertThrottling 等 `increase(chi_clickhouse_event_*[1m])`[^alt-rejected][^alt-delayed] | metrics-exporter 讀 `system.events` | `system.events` 預設只列非 0 的事件，第一次發生前沒有 series，`increase` 抓不到第一次的增加（lab：`RejectedInserts`、`DelayedInserts` 0 列）[^ch-events-zero][^alt-fetch-events] | 用原生 `/metrics`：每個 ProfileEvent 都輸出，含 0[^ch-prom-events-all] |
| ClickHouseReplicasMaxAbsoluteDelay[^alt-absdelay] | `ReplicasMaxAbsoluteDelay > 300` | 不算唯讀的表；replica 停掉時倖存那台是 0 | 加上每張表的 SQL 延遲與 active_replicas |
| ClickHouseZooKeeperSession[^alt-zksession] | `ZooKeeperSession > 1` | 只看「太多」，不看 0（session 不在） | CHKeeperSessionLost（< 1 或 SessionExpired） |
| ClickHouseQueryPreempted[^alt-querypreempted] | `QueryPreempted > 0` | 是 `priority` 設定造成的暫停，不是過載或被拒[^ch-cm-querypreempted] | CHQueriesRejectedConcurrency（`TOO_MANY_SIMULTANEOUS_QUERIES`） |
| ClickHouseTooManyRunningQueries[^alt-running] | `Query - PendingAsyncInsert > 80` | 固定門檻，和 `max_concurrent_queries`（預設 1000）無關 | 看被拒的次數 |
| ClickHouseMaxPartCountForPartition[^alt-maxparts] | `> 100` | 固定門檻 | 對 `parts_to_delay_insert`（SQL 查設定值）的比例 |
| ClickHouseTooManyMutations[^alt-mutations] | 未完成數 `> 100` | 不看失敗；一個失敗的 mutation 就能卡住後面全部 | CHMutationFailing（`latest_fail_reason`） |
| ClickHouseDetachedParts[^alt-detached] | `DetachedParts > 0` | 含使用者自己 DETACH 的 | 只看 reason 不是使用者的 |
| ClickHouseDiskUsage[^alt-disk] | `predict_linear(DiskFreeBytes[1d], 86400) < 0` | exporter 自己查 system.disks；原生是 `DiskAvailable{disk}` | CHDiskSpaceLow、CHDiskFillingUp |
| ClickHouseKeeperDown[^alt-k-down] | `up == 0` | 沒有 absent；job 名稱錯了不會響 | CHKeeperEndpointMissing |
| 備份規則[^alt-backup-failed] | clickhouse-backup 的 metric | 原生 BACKUP 沒有規則 | CHBackupFailed、CHBackupTooOld（system.backups） |
| （沒有） | — | 三方交叉、唯讀時的 `/replicas_status`、mutation 失敗、記憶體上限、SQL 路徑本身 | — |

## 監測自檢 {#exporter}

### 三方交叉 {#health-second-path}

blackbox_exporter 從另一條路徑探測每台元件，probe job 把 `instance` relabel 成 `/metrics` 的 `host:9363`（設定見 `lab/targets/clickhouse/prom/prometheus.yml`、`bb/blackbox.yml`）。

| 元件 | probe | 健康 metric | 為什麼 |
|---|---|---|---|
| server | `/ping`；`/replicas_status?verbose=1`（503 或 body 有 `is readonly`）；HTTP SQL：`SELECT count() FROM system.replicas WHERE is_readonly` 要是 0 | `ReadonlyReplica`、`ZooKeeperSession` | `/ping` 是靜態字串；`/replicas_status` 不看唯讀[^ch-ping-static][^ch-rs-readonly-skip] |
| Keeper | `/ready`（`keeper_server.http_control`） | `KeeperIsLeader` | `ruok` 一律 `imok`；`mntr` 沒在服務時只回一行訊息[^ch-k-ready][^ch-k-ruok][^ch-k-mntr-inactive] |

lab 的結果：<span class="at-chip at-ev-tested">實測</span>[^ch-exp-raw]

| 情境 | 對象 | `up` | probe | 健康 metric | 判讀 | 規則（firing 或 pending） |
|---|---|---|---|---|---|---|
| 正常 | 全部 | 1 | 全部 1 | Keeper leader 1 台、`KeeperSyncedFollowers` 2；`ReadonlyReplica` 0 | — | — |
| ch2 停止 fetch 65 秒 | ch2 | 1 | `/ping` 1；`/replicas_status` **503**（`Absolute delay: 70`，lab 的 `min_absolute_delay_to_close` 30） | `ReplicasMaxAbsoluteDelay` 80、`InsertsInQueue` 8 | 落後 | CHReplicasStatusNotOk、CHReplicationQueueFailing pending |
| 停掉 ch2，+40 秒 | ch2 | 0 | `/ping` 0、SQL 0、`/replicas_status` 0 | — | process 停了 | CHEndpointDown、CHPingFailing、CHSqlProbeFailing、CHComponentCountLow pending |
| 停掉 ch2，+40 秒 | ch1（倖存） | 1 | 全部 1；`/replicas_status` `Absolute delay: 0` | `ReplicasMaxAbsoluteDelay` 0；`active_replicas` **1**／2 | 只有 active_replicas 看得出少一台 | CHReplicaInactive pending |
| 停 2 台 Keeper，+81 秒 | ch1、ch2 | **1** | `/ping` **1**；`/replicas_status` 狀態碼 **200**（body `is readonly`，靠 body 比對才失敗）；SQL 探測 0 | `ReadonlyReplica` 1；`ReplicasMaxAbsoluteDelay` **0**；`ZooKeeperSession` 仍是 **1** | 唯讀，寫入卡住 | CHReplicaReadonly、CHKeeperSessionLost、CHSqlProbeFailing、CHReplicasStatusNotOk、CHWritesToReadonlyTable |
| 停 2 台 Keeper | keeper3（存活） | **1** | `/ready` **503**（`hasLeader:false`）；`ruok` **imok**；`mntr` `This instance is not currently serving requests` | `KeeperIsLeader` 0、`KeeperSyncedFollowers` 0 | 沒有 quorum | CHKeeperNoLeader、CHKeeperQuorumLost、CHKeeperNotReady；Altinity ClickHouseKeeperLostQuorum **沒有** |
| `<prometheus>` 關掉 asynchronous_metrics | ch2 | 1 | 全部 1 | sample 3,884 → 3,183；`MaxPartCountForPartition` 不見 | 設定問題 | CHMetricFamiliesMissing pending |
| `<prometheus>` 整段拿掉 | ch2 | **0**（`connect: connection refused`） | 全部 1 | — | 設定問題，不是 process | CHEndpointDown、CHComponentCountLow pending |

Keeper 失去 quorum 時的 server（主機時鐘）：+10 秒兩台的表已經 `is_readonly` 1、`is_session_expired` 1（不必等 `session_timeout_ms` 30 秒：連線斷了 session 就過期）；`INSERT` 不是立刻失敗，而是卡住超過 client 的 30 秒（insert 遇到 Keeper 錯誤時重試，`insert_keeper_max_retries` 20、退避最長 10 秒）；`SELECT count()` 0.02 秒正常；`SELECT ... FROM system.zookeeper_connection` 也卡住超過 30 秒。`ClickHouseErrorMetric_TABLE_IS_READ_ONLY` 32、`KEEPER_EXCEPTION` 34、`ZooKeeperHardwareExceptions` 34。server 的 `/metrics` 一直在 0.01 秒內；停掉的 Keeper 每次 scrape 約 2.8 秒才失敗。Keeper 啟動後 3 秒 ch1 的表恢復可寫。<span class="at-chip at-ev-tested">實測</span>[^ch-exp-raw][^ch-set-insert-keeper-retries][^ch-set-insert-keeper-backoff]

### metric 消失、job 名稱、scrape 預算 {#exporter-scrape}

| 問題 | 做法 | 證據 |
|---|---|---|
| `<prometheus>` 沒打開 | 被抓時 connection refused（`up` 0）；和 `/ping` 一起看：`/ping` 1、`up` 0 是設定或 scrape 路徑 | <span class="at-chip at-ev-tested">實測</span>[^ch-prom-http-default] |
| 子項被關、名稱改了 | `up == 1 unless` MaxPartCountForPartition（async）、ReadonlyReplica（metrics）、RejectedInserts（events）、ErrorMetric_ALL（errors）、DiskAvailable（`{disk}` 與 `_<disk>` 兩種名稱都接受）；Keeper 用 KeeperIsLeader 等 | <span class="at-chip at-ev-tested">實測</span>：名稱 server 3225、Keeper 632[^ch-exp-names] |
| job 名稱錯了 | 每個 job 一條 `absent(up{job=...})`；Altinity 沒有 | <span class="at-chip at-ev-code">讀碼</span>[^alt-k-down] |
| scrape 預算 | server 約 3,900 個 sample、Keeper 1,193；沒有每張表的 series，表多不會變大 | <span class="at-chip at-ev-tested">實測</span> |
| `/metrics` 會不會被卡 {#endpoint-blocking} | 讀 atomic 計數器與背景算好的 async 快照，scrape 不觸發計算、不拿表或 Keeper 的鎖；lab 各情境都在 0.01 秒內 | <span class="at-chip at-ev-tested">實測</span>[^ch-prom-metric-atomic][^ch-async-getvalues][^ch-async-swap] |

### SQL 路徑本身 {#sql-path}

query-exporter 查詢失敗時保留舊值（和 [PostgreSQL 的自訂查詢](postgres.md#custom-queries-behaviour)相同），所以要另外看它是否新鮮：`up{job="clickhouse-sql"}`、`queries_total{status!="success"}`、`time() - query_timestamp`。<span class="at-chip at-ev-code">讀碼</span>[^pg-q-qe-status][^pg-q-qe-builtin-timestamp]lab 看到三件事：<span class="at-chip at-ev-tested">實測</span>[^ch-exp-raw][^ch-exp-extra][^ch-exp-extra-first]

| 現象 | lab | 做法 |
|---|---|---|
| server 停掉時它的 series 停在舊值 | ch2 停掉後 `chq_replica_absolute_delay_seconds{ch2}` 停在 66，直到 expiration（2 分鐘） | 規則和 `up`、CHSqlStale 一起看 |
| 一個卡住的查詢拖垮同一個 DSN 的所有查詢 | Keeper 失去 quorum 時 `SELECT ... FROM system.zookeeper_connection` 卡住超過 30 秒；每個 DSN 一條連線，其他查詢一起逾時，所有 `chq_*` 消失 | 拿掉這個查詢（Keeper session 改看原生 metric）；拿掉後同一情境 `chq_replica_readonly` 30 秒內更新 |
| 排程器跳過同時到期的查詢 | 第一版的 15s 與 1m 查詢同時到期，log 寫 `Run time of job ... was missed`，1m 的查詢 5 分鐘內一次都沒有執行 | interval 錯開（15、17、19、23、29、61、67、71 秒）；之後仍偶爾跳過，但每個查詢都有資料 |

第一次執行是在啟動後一個 interval，所以最長的 interval 約 1 分鐘；容器時鐘慢，實際間隔更長。

## 用 SQL 補上 {#custom-queries}

| 項目 | 內容 |
|---|---|
| 工具 | query-exporter 5.2.2（`adonato/query-exporter:5.2.2`），映像內建 clickhouse-sqlalchemy 0.3.2、clickhouse-driver 0.2.11，不必另裝 |
| 設定 | [queries/clickhouse/clickhouse.yaml](https://github.com/HahaSula/alert-trust/blob/main/queries/clickhouse/clickhouse.yaml)：每台 server 一個 DSN（`clickhouse+native://monitor:...@host:9000/default`）；database label 在 Prometheus 轉成 `instance="host:9363"` |
| 帳號 | system 表任何使用者都讀得到；建議 `readonly=2`、`max_execution_time` 保底（lab 的 `monitor`） |
| 規則 | `rules/targets/clickhouse.yml` 中用 `chq_*` 的規則，假設 `job="clickhouse-sql"` |
| 其他做法 | Altinity metrics-exporter 只在 operator 下；blackbox 用 HTTP 介面跑一條 SQL（lab 的 `clickhouse-sql-probe`）適合一兩個關鍵檢查 |

| 盲點 | 查詢 | metric | 規則 | lab |
|---|---|---|---|---|
| 每張表的唯讀、延遲、active replica | `system.replicas` | `chq_replica_readonly`、`chq_replica_absolute_delay_seconds`、`chq_replica_active_replicas`、`chq_replica_total_replicas` | CHReplicaDelayHighSQL、CHReplicaInactive | 停 ch2 時 ch1 的 active_replicas 1／2；Keeper 失去 quorum 時兩台 `chq_replica_readonly` 1（約 30 秒內更新） |
| 複寫佇列失敗 | `system.replication_queue` | `chq_replication_queue_failing` | CHReplicationQueueFailing | 失敗的 mutation：兩台各 1 |
| 哪張表 part 多、目前的門檻 | `system.parts`、`system.merge_tree_settings` | `chq_parts_max_per_partition`、`chq_merge_tree_setting` | CHPartsNearInsertDelay | 15 個 part、門檻 20：比例 0.75，CHPartsNearInsertDelay pending |
| mutation 失敗 | `system.mutations` | `chq_mutations_failing`、`chq_mutation_oldest_pending_seconds` | CHMutationFailing、CHMutationsStuck | 兩台各 1，CHMutationFailing pending |
| 伺服器移走的 part | `system.detached_parts` | `chq_detached_parts{reason}` | CHBrokenPartsDetached | broken part：`reason="broken-on-start"` 1，CHBrokenPartsDetached pending；使用者 DETACH 是 `detached_by_user`，不響 |
| Distributed 送出 | `system.distribution_queue` | `chq_distribution_queue_files` 等 | （原生 metric 的規則） | 5 個檔案等著送 |
| 原生 BACKUP | `system.backups` | `chq_backups_failed`、`chq_backup_last_success_timestamp` | CHBackupFailed、CHBackupTooOld | 成功一次、同名失敗一次：`chq_backups_failed` 1、`chq_backup_last_success_timestamp` 有值（30 秒內） |

## 叢集 {#cluster}

| 問題 | 訊號 | 證據 |
|---|---|---|
| Keeper 有 leader 嗎 {#role-detection} | `absent(KeeperIsLeader == 1)`；`count(KeeperIsLeader == 1) > 1` | <span class="at-chip at-ev-tested">實測</span>[^ch-k-isleader] |
| Keeper 還有過半嗎 {#keeper-quorum} | `/ready` 成功數 < floor(預期 / 2) + 1（預期數寫在 `clickhouse-params`）；leader 的 `KeeperSyncedFollowers` < 預期 - 1 | <span class="at-chip at-ev-tested">實測</span>[^ch-k-ready][^ch-k-synced] |
| 台數 {#component-count} | `count by (job) (up == 1)` 對 `clickhouse-params` | <span class="at-chip at-ev-tested">實測</span> |
| server 的 Keeper session {#keeper-session} | `ZooKeeperSession` < 1 或 `ZooKeeperSessionExpired` > 0 | <span class="at-chip at-ev-tested">實測</span>[^ch-cm-zksession] |
| Keeper 慢、排隊 {#keeper-latency} | `KeeperOutstandingRequests` > 10 持續 10 分鐘（Altinity） | <span class="at-chip at-ev-upstream">上游</span>[^alt-k-outstanding] |

**ClickHouse server 沒有主從。**每台 replica 都可寫，協調靠 Keeper；所以 role-detection 看的是 Keeper 的 leader，「可寫入」等於「Keeper 有 leader 而且表不是唯讀」。

**Keeper 失去 quorum。**lab 停掉 leader 與一台 follower：存活那台 `up` 1、`KeeperIsLeader` 0、`/ready` 503、`ruok` 仍 `imok`、`mntr` 只回一行 `This instance is not currently serving requests`。所以 4lw `ruok` 不能當健康檢查；ZooKeeper 時代常見的 `ruok` probe 在 Keeper 上看不出 quorum。Altinity 的 ClickHouseKeeperLostQuorum 要求 `KeeperIsLeader == 1`，這時回傳空。Keeper 的 Raft 機制見[共識（Raft）架構](../architecture/db-raft.md#clickhouse-keeper)。<span class="at-chip at-ev-tested">實測</span>[^ch-exp-raw][^ch-k-ruok][^ch-k-mntr-inactive][^alt-k-lostquorum]

**server 端的 session 不能看 `ZooKeeperSession`。**Keeper 失去 quorum 的整段時間兩台的 `ZooKeeperSession` 都是 1（session 物件還在，只是過期）；`ZooKeeperSessionExpired` 從 0 變成 1、再到 3–4，Keeper 恢復 150 秒後仍是 3–4，不歸零。所以 `< 1` 不會響、`> 0` 恢復後一直響（第一版規則就是這樣：恢復後 3 分鐘還 firing）。改成看變化量：`delta(ZooKeeperSessionExpired[5m]) > 0` 或 `ZooKeeperHardwareExceptions` 增加；現況看 `ReadonlyReplica`。修改後的規則在 `extra.sh` 重跑：`ZooKeeperSessionExpired` 一樣從 0 變成 3，CHKeeperSessionLost firing；恢復 151 秒時仍 firing（5 分鐘視窗還包含變化，預期在最後一次變化後約 5 分鐘清掉，lab 沒有等到）。<span class="at-chip at-ev-tested">實測</span>[^ch-exp-raw][^ch-exp-extra][^ch-cm-zksession]

**版本。**`ZooKeeperSessionExpired` 26.3 才有；25.3、25.8 上 CHKeeperSessionLost 只剩 `ZooKeeperHardwareExceptions` 那半，session 過期改看自訂查詢的 `chq_replica_session_expired`（`system.replicas.is_session_expired`）[^ver-ch258-zk-session][^ver-ch263-zk-expired][^ver-ch253-replicas]。

## 複寫 {#replication}

| 問題 | 訊號 | 證據 |
|---|---|---|
| 唯讀 {#replica-readonly} | `ReadonlyReplica` > 0；每張表 `chq_replica_readonly` | <span class="at-chip at-ev-tested">實測</span>[^ch-cm-readonly][^ch-rt-setreadonly] |
| 延遲 {#replication-lag} | `ReplicasMaxAbsoluteDelay` > 300；每張表 `chq_replica_absolute_delay_seconds`；`active_replicas` < `total_replicas` | <span class="at-chip at-ev-tested">實測</span>[^ch-sam-absdelay][^ch-sam-skip-readonly] |
| 佇列 {#replication-queue} | `chq_replication_queue_failing` > 0 持續 15 分鐘；`ReplicasMaxInsertsInQueue` > 100（例子） | <span class="at-chip at-ev-tested">實測</span> |
| Distributed 送出 {#distributed-queue} | `DistributedFilesToInsert` > 50（Altinity）；`DistributedAsyncInsertionFailures` 增加；`BrokenDistributedFilesToInsert` > 0 | <span class="at-chip at-ev-tested">實測</span>[^ch-cm-distfiles][^ch-pe-dist-fail] |

**延遲量的是真的嗎。**三個情境三種答案：<span class="at-chip at-ev-tested">實測</span>[^ch-exp-raw]

| 情境 | ch1 | ch2 | 誰看得出來 |
|---|---|---|---|
| ch2 `STOP FETCHES`、ch1 繼續寫 65 秒 | delay 0 | `absolute_delay` 70、`queue_size` 9、`inserts_in_queue` 8；`ReplicasMaxAbsoluteDelay` 80；relative delay **0** | ch2 自己的 delay 與 `/replicas_status`（lab 的 `min_absolute_delay_to_close` 30） |
| 停掉 ch2 | delay 0、`active_replicas` 1／2 | 抓不到；query-exporter 的 ch2 series 停在最後的值（delay 66）直到 expiration | 倖存那台的 `active_replicas`（CHReplicaInactive） |
| Keeper 失去 quorum | `ReplicasMaxAbsoluteDelay` 0（唯讀的表不算） | 同左 | `ReadonlyReplica` |

**`/replicas_status` 的預設值幾乎不會 503。**`min_absolute_delay_to_close` 預設 0（不看絕對延遲），只看 relative delay ≥ `min_relative_delay_to_close`（300 秒）；relative delay 又只在 absolute delay ≥ `min_relative_delay_to_measure`（120 秒）時才算，lab 的 70 秒延遲 relative 是 0。所以預設設定下，replica 要落後超過 5 分鐘、而且比別台落後 5 分鐘以上才 503；唯讀永遠不會。`max_replica_delay_for_distributed_queries`（300 秒）是 Distributed 查詢挑 replica 用的，和 `/replicas_status` 無關。<span class="at-chip at-ev-code">讀碼</span>[^ch-mt-min-abs-close][^ch-mt-min-rel-close][^ch-rmt-relative-measure][^ch-mt-rel-measure][^ch-set-max-replica-delay]

**`STOP FETCHES` 也讓複寫佇列有失敗項目。**ch2 的 CHReplicationQueueFailing 在 G1 pending（佇列項目帶著「fetch 被停止」的 last_exception）；失敗的 mutation 同樣讓兩台各有 1 個。<span class="at-chip at-ev-tested">實測</span>[^ch-exp-raw]

**Distributed 送出。**Distributed 表指向不存在的主機、非同步 insert 5 次：insert 都在 0.08 秒內成功；ch1 的 `DistributedFilesToInsert` 5、`DistributedAsyncInsertionFailures` 2，`system.distribution_queue` 的 `last_exception` 是 `All connection tries failed`；CHDistributedSendFailing pending 後 firing。之後 `DROP TABLE` 等了 23.7 秒（主機）。<span class="at-chip at-ev-tested">實測</span>[^ch-exp-raw]

## 可用性 {#availability}

| 問題 | 訊號 | 證據 |
|---|---|---|
| insert 被拒 {#writes-rejected} | `RejectedInserts` 增加（Too many parts）；`ClickHouseErrorMetric_TABLE_IS_READ_ONLY` 增加 | <span class="at-chip at-ev-tested">實測</span>[^ch-mtd-rejected][^ch-rmt-assert-ro] |
| 查詢被並行上限拒 {#query-admission} | `ClickHouseErrorMetric_TOO_MANY_SIMULTANEOUS_QUERIES` 增加 | <span class="at-chip at-ev-tested">實測</span>[^ch-pl-toomany] |

lab：每個使用者上限 2 時同時送 3 個 `sleep(2)`，第 3 個 0.02 秒失敗 `TOO_MANY_SIMULTANEOUS_QUERIES`，CHQueriesRejectedConcurrency firing。`max_concurrent_queries` 的程式預設是 0（不限制），預設 `config.xml` 設成 1000。<span class="at-chip at-ev-tested">實測</span>[^ch-exp-raw][^ch-ss-max-conc][^ch-cfg-max-conc]

## 資源與飽和 {#saturation}

| 問題 | 訊號（門檻） | 證據 |
|---|---|---|
| part 數 {#parts-pressure} | `MaxPartCountForPartition / parts_to_delay_insert` > 0.5（設定值用 SQL 查）；備援 `MaxPartCountForPartition` > 300；`DelayedInserts` 增加 | <span class="at-chip at-ev-tested">實測</span>[^ch-sam-maxparts][^ch-mt-delay] |
| 記憶體 {#memory} | `MemoryTracking / (CGroupMemoryTotal 或 OSMemoryTotal)` > 0.8；`MEMORY_LIMIT_EXCEEDED` 增加 | <span class="at-chip at-ev-tested">實測</span>[^ch-cm-memtracking][^ch-mem-exceeded] |
| global thread pool | `GlobalThreadActive` / `max_thread_pool_size` 或 `GlobalThreadScheduled` / `thread_pool_queue_size` > 0.9（預設都是 10000，寫在 clickhouse-params）；`CANNOT_SCHEDULE_TASK` 增加（CHGlobalThreadPoolNearMax、CHThreadPoolScheduleFailed） | <span class="at-chip at-ev-code">讀碼</span>[^res-ch-global-pool-metrics][^res-ch-pool-queue-wait][^res-ch-cannot-schedule] |

**Too many parts。**lab 把門檻調成 20／40、兩台 `SYSTEM STOP MERGES`，逐筆 insert 45 次（`async_insert=0`），用了 14 秒（主機）：第 21–39 筆被延遲（`DelayedInserts` 20、`DelayedInsertsMilliseconds` 10,500，每筆最多 `max_delay_to_insert` 1 秒），第 40 筆起被拒（`Too many parts (40 ...)`，`RejectedInserts` 6、`TOO_MANY_PARTS` 6）。`MaxPartCountForPartition` 兩台都是 40（ch2 是複寫過去的）。CHInsertsDelayed、CHInsertsRejected 在 25 秒內 firing。<span class="at-chip at-ev-tested">實測</span>[^ch-exp-raw][^ch-mt-max-delay]

**預設的 async_insert 不一定救得了。**26.2 起 `async_insert` 預設開、`wait_for_async_insert` 也開。lab 用同一個 client 一筆接一筆送 20 次：`AsyncInsertFlush` 和 `AsyncInsertQuery` 一樣多（21），沒有合併；part 數已在門檻時 flush 一樣被拒（`RejectedInserts` 6 → 26）。合併只發生在同時到達的 insert。<span class="at-chip at-ev-tested">實測</span>[^ch-set-async-insert][^ch-exp-raw]

**對門檻的比例。**`/metrics` 沒有 `parts_to_delay_insert` 的值，CHPartsNearInsertDelay 用 query-exporter 查 `system.merge_tree_settings`。lab（門檻 20、停 merge 後 15 個 part）：比例 0.75，兩台 CHPartsNearInsertDelay pending。表層級的 `SETTINGS` 覆寫不在這個表裡。<span class="at-chip at-ev-tested">實測</span>[^ch-exp-extra]

**記憶體。**`max_memory_usage` 30MB 的 GROUP BY 在 0.06 秒失敗（`Query memory limit exceeded`），`ClickHouseErrorMetric_MEMORY_LIMIT_EXCEEDED` 與 `QueryMemoryLimitExceeded` 各加 1，`GlobalMemoryLimitExceeded` 仍是 0；CHQueriesHitMemoryLimit firing。`/metrics` 沒有目前生效的 `max_server_memory_usage`：預設是 RAM（容器裡是 cgroup 上限）× 0.9，所以規則用 `CGroupMemoryTotal`／`OSMemoryTotal` 當分母；設了明確值時改用 `system.server_settings` 的值。整台的上限沒有實驗。<span class="at-chip at-ev-tested">實測</span>[^ch-exp-raw][^ch-ss-max-mem][^ch-ss-mem-ratio]

## 工作量 {#workload}

| 問題 | 訊號 | 證據 |
|---|---|---|
| 失敗查詢比例 {#failed-queries} | `rate(FailedQuery) / rate(Query)` > 5%（例子） | <span class="at-chip at-ev-tested">實測</span>（Too many parts 時 pending）[^ch-pe-failed-query] |
| 資料或磁碟類錯誤 {#error-counters} | `ClickHouseErrorMetric_{CHECKSUM_DOESNT_MATCH,CANNOT_READ_ALL_DATA,CORRUPTED_DATA,NOT_ENOUGH_SPACE,...}` 增加 | <span class="at-chip at-ev-code">讀碼</span>[^ch-prom-errors-local] |

`/metrics` 的 errors 每個錯誤碼一條（含 0），所以 `increase` 抓得到第一次；這是原生端點比讀 `system.errors` 的 exporter 好的地方。哪一條查詢造成負載要查 `system.query_log`（見[盲點](#blind-spots)）。

## 資料維護 {#maintenance}

| 問題 | 訊號 | 證據 |
|---|---|---|
| merge 池 {#merge-pool} | `BackgroundMergesAndMutationsPoolTask / PoolSize` ≥ 1 持續 30 分鐘 | <span class="at-chip at-ev-code">讀碼</span>[^ch-cm-pooltask][^ch-cm-poolsize] |
| mutation {#mutations} | `chq_mutations_failing` > 0；最老 mutation > 6 小時（例子）；heavy 打開時 `NumberOfPendingMutationsOverExecutionTime` | <span class="at-chip at-ev-tested">實測</span>[^ch-sys-mut-fail][^ch-sam-mutations-over] |
| detached parts {#detached-parts} | `chq_detached_parts{reason!="detached_by_user"}` > 0；heavy 打開時 `NumberOfDetachedParts - NumberOfDetachedByUserParts` | <span class="at-chip at-ev-tested">實測</span>[^ch-sam-detached] |

**失敗的 mutation。**`ALTER TABLE events UPDATE v = toString(throwIf(k >= 0, ...))` 在 0.04 秒回傳成功。40 秒後兩台的 `system.mutations` 都是 `is_done` 0、`parts_to_do` 1、`latest_fail_reason` `Code: 395 ... lab: mutation fails on purpose`；`system.replication_queue` 各有 1 個 `last_exception` 非空的項目。`/metrics`：`ClickHouseErrorMetric_FUNCTION_THROW_IF_VALUE_IS_NON_ZERO` 14、15（每次重試加 1），`NumberOfPendingMutations` 只有打開 heavy metric 的 ch2 有（1）。CHMutationFailing、CHReplicationQueueFailing 兩台都 pending。`KILL MUTATION` 之後，被 mutation 鎖住的 part 仍不能 `DETACH`（`PART_IS_TEMPORARILY_LOCKED`）。<span class="at-chip at-ev-tested">實測</span>[^ch-exp-raw]

**broken part。**ch2 一張本機 MergeTree 表（10 萬列、1 個 part）的 `data.bin` 截短成 10 bytes 後重啟：server 照常啟動，log 寫 `is broken and needs manual correction`、`Detaching broken part`；那張表 `SELECT count()` 回 **0**（不是錯誤），`system.detached_parts` 有 `broken-on-start_all_1_1_0`（reason `broken-on-start`）。`chq_detached_parts{reason="broken-on-start"}` 1、打開 heavy 的 ch2 `NumberOfDetachedParts` 1；CHBrokenPartsDetached、CHDetachedPartsNative pending。使用者 `DETACH PARTITION` 的 reason 是空字串（`detached_by_user`），不觸發。資料少了、查詢卻成功，這個訊號是唯一會說話的地方。<span class="at-chip at-ev-tested">實測</span>[^ch-exp-extra][^ch-sam-detached]

lab 的 `background_pool_size` 是 1（容器 CPU 少），正式環境預設是 16。merge 池飽和沒有單獨的實驗。<span class="at-chip at-ev-tested">實測</span>（設定值）[^ch-exp-raw]

## 資料保護 {#protection}

| 問題 | 訊號 | 證據 |
|---|---|---|
| 原生 BACKUP {#backups} | `chq_backups_failed` 30 分鐘內增加；`time() - max(chq_backup_last_success_timestamp)` > 26 小時（例子） | <span class="at-chip at-ev-tested">實測</span>[^ch-sys-backups-status] |
| 所有 replica 都找不到的 part {#data-integrity} | `ReplicatedDataLoss` 增加 | <span class="at-chip at-ev-code">讀碼</span>[^ch-pe-datalost] |

**原生 BACKUP。**`BACKUP TABLE ... TO Disk('backups', 'b1.zip')` 成功一次（0.04 秒），同名再做一次失敗 `BACKUP_ALREADY_EXISTS`；`system.backups` 兩列（`BACKUP_CREATED`、`BACKUP_FAILED`）。`/metrics` 只有 `ClickHouseProfileEvents_Backup*` 這類耗時與次數，沒有成功或失敗。query-exporter 的 `chq_backups_failed` 變成 1、`chq_backup_last_success_timestamp` 有值，CHBackupFailed firing。`system.backups` 存在記憶體裡，server 重啟後清空，所以 CHBackupTooOld 在重啟後會誤報（lab 一開始沒有備份時它就是 pending）。用 clickhouse-backup 時改用它自己的 metric（Altinity 的備份規則就是這樣）。<span class="at-chip at-ev-tested">實測</span>[^ch-exp-raw][^ch-exp-extra][^ch-backups-infos][^alt-backup-failed]

## 容量 {#capacity}

| 問題 | 訊號 | 證據 |
|---|---|---|
| 每個 disk 的剩餘空間 {#disk-space} | `DiskAvailable / DiskTotal` < 10%（例子；DiskTotal 大於 1e18 的遠端 disk 排除）；`predict_linear(DiskAvailable[6h], 1 天)` < 0 | <span class="at-chip at-ev-code">讀碼</span>[^ch-sam-disk-available][^ch-sam-disk-total] |

26.8 起名稱是 `ClickHouseAsyncMetrics_DiskAvailable{disk="default"}`（lab 確認，`DiskAvailable_default` 0 條）；`asynchronous_metrics_key_values_mode` 設成 `legacy_names` 時反過來。merge 要預留空間，空間不夠時 merge 停、part 數上升。lab 沒有填滿磁碟。<span class="at-chip at-ev-tested">實測</span>（名稱）[^ch-ss-kv-mode][^ch-exp-raw]

**版本。**26.3 以前每顆磁碟一個名稱（`ClickHouseAsyncMetrics_DiskAvailable_default`）。CHDiskSpaceLow、CHDiskFillingUp 用 `or` 加上 `{__name__=~"ClickHouseAsyncMetrics_DiskAvailable_.+"}`，以 `label_replace` 把名稱後綴轉成 `disk` label，兩種名稱都能算；CHMetricFamiliesMissing 也兩種都接受。舊名稱的寫法只讀碼，沒有在 26.3 以前的版本上跑過[^ver-ch253-disk-suffix][^ver-ch263-disk-suffix][^ver-ch268-disk-label][^ver-ch268-kv-mode]。

## 盲點與補法 {#blind-spots}

| 盲點 | 類別 | 補法 |
|---|---|---|
| 每張表的唯讀、延遲、佇列 | 資料庫有、`/metrics` 沒輸出 | query-exporter（[已補](#custom-queries)） |
| mutation 失敗、detached parts 的 reason、part 數的門檻 | 資料庫有、`/metrics` 沒輸出 | query-exporter（已補） |
| Distributed 送出的原因、原生 BACKUP | 資料庫有、`/metrics` 沒輸出 | query-exporter（已補）；clickhouse-backup 用它自己的 metric |
| 端到端能不能寫入 | 資料庫本身沒提供 | 外部 SQL probe 定期寫一列到測試用的 Replicated 表並讀回 |
| 哪一條查詢造成負載 | 資料庫有、`/metrics` 沒輸出 | `system.query_log`、`system.processes`（未實測） |
| TLS 憑證到期 | 資料庫本身沒提供 | blackbox 的 `probe_ssl_earliest_cert_expiry` |
| 慢磁碟、磁碟滿、唯讀 | 要靠其他元件 | CHDiskSpaceLow；已由 [主機與容器資源](../components/host.md) 補上（規則只讀碼，host lab 之後排程） |
| Keeper 慢、ON CLUSTER DDL 佇列卡住 | 沒有實驗；主機層補不了 | CHKeeperRequestsBacklog；`system.distributed_ddl_queue`（未實測） |

## 實驗 {#lab}

`lab/targets/clickhouse`：ClickHouse v26.10.2.103-stable（server ×2：1 shard、2 replicas，`max_server_memory_usage` 600MB；Keeper ×3）、Prometheus v3.13.5（scrape 5 秒）、blackbox_exporter v0.27.0（timeout 4 秒）、query-exporter 5.2.2，載入 `rules/targets/clickhouse.yml` 與 Altinity 的 Keeper 規則原文。`parts_to_delay_insert` 20、`parts_to_throw_insert` 40、`min_absolute_delay_to_close` 30 秒、Keeper `session_timeout_ms` 30 秒；ch2 打開 heavy metric。`./run.sh` 約 15 分鐘、`./extra.sh` 約 12 分鐘，結束時 `docker compose down -v`（殘留容器、volume 0）。原始輸出見 `results/run-20261110.txt`、`results/extra-20261110.txt`，摘要見 lab 的 README。[^ch-exp][^ch-exp-summary]
