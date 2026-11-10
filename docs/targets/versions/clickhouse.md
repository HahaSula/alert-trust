# ClickHouse 各版本的監控差異

這頁列出 ClickHouse LTS 25.3、25.8、26.3、26.8 與最新 stable 26.10 之間會影響監控的差異：asynchronous metric 從「名稱帶後綴」改成 label、CurrentMetrics 的新增、system 表欄位。目前的卡片核對過 v26.10.2.103-stable。

依據是各線最新 tag（`v25.3.14.14-lts`、`v25.8.33.6-lts`、`v26.3.60.2-lts`、`v26.8.34.13-lts`、`v26.10.2.103-stable`）的 `src/Common/CurrentMetrics.cpp`、`ProfileEvents.cpp`、`AsynchronousMetrics.cpp`、`src/Interpreters/ServerAsynchronousMetrics.cpp`、`src/Coordination/KeeperAsynchronousMetrics.cpp`、`src/Server/PrometheusMetricsWriter.cpp` 與 `src/Storages/System/`。沒有做實驗，全部是<span class="at-chip at-ev-code">讀碼</span>。

## 結論 {#summary}

1. **26.8 起磁碟、網卡、block device 等 metric 改成 label。**25.3–26.3 是 `ClickHouseAsyncMetrics_DiskAvailable_default`（每顆磁碟一個名稱），26.8 起是 `ClickHouseAsyncMetrics_DiskAvailable{disk="default"}`。卡片 `disk-space` 的規則只寫新名稱，在 26.3 以前回傳空。26.8 的 `asynchronous_metrics_key_values_mode` 可以改回舊名。
2. **`ZooKeeperSessionExpired` 26.3 才有。**卡片 `keeper-session` 的主 metric 在 25.3、25.8 不存在；舊版只有 `ZooKeeperSession`（session 過期時仍是 1）。
3. **25.x 已不在安全更新範圍。**v26.10.2 的 SECURITY.md 列 25.* 為不支援；26.3、26.8 LTS 仍支援。
4. **其他卡片 metric 五個版本都有**：`ReadonlyReplica`、`KeeperOutstandingRequests`、`DistributedFilesToInsert`、`BrokenDistributedFilesToInsert`、`BackgroundMergesAndMutationsPoolTask`、`MemoryTracking`（CurrentMetrics），`RejectedInserts`、`DelayedInserts`、`FailedQuery`、`ReplicatedDataLoss`、`ZooKeeperHardwareExceptions`（ProfileEvents），`MaxPartCountForPartition`、`ReplicasMaxAbsoluteDelay`、`NumberOfDetachedParts`、`NumberOfPendingMutations`、`KeeperIsLeader`、`KeeperSyncedFollowers`、`CGroupMemoryTotal`（asynchronous），以及 `ClickHouseErrorMetric_*`。
5. **自訂查詢用的 system 表欄位在 25.3 都已存在**（`system.replicas`、`replication_queue`、`mutations`、`distribution_queue`、`detached_parts`、`backups`）。

## 選的版本 {#versions}

| 版本線 | 這頁用的 tag | 狀態（[SECURITY.md](https://github.com/ClickHouse/ClickHouse/blob/v26.10.2.103-stable/SECURITY.md)） |
|---|---|---|
| 25.3 LTS | `v25.3.14.14-lts` | 不支援 |
| 25.8 LTS | `v25.8.33.6-lts` | 不支援 |
| 26.3 LTS | `v26.3.60.2-lts` | 支援 |
| 26.8 LTS | `v26.8.34.13-lts` | 支援；卡片記錄的最新 LTS |
| 26.10 stable | `v26.10.2.103-stable` | 卡片核對過 |

## 版本差異表 {#diff}

| 版本 | 變更 | 對監控的影響（哪個 metric／collector／規則） | exporter 或 endpoint 有沒有處理 | 依據 |
|---|---|---|---|---|
| 25.3–26.3 | 磁碟 metric 是 `DiskAvailable_<disk>`、`DiskTotal_<disk>` 等獨立名稱 | `disk-space`、`metric-families-present` 寫 `ClickHouseAsyncMetrics_DiskAvailable` 的規則回傳空 | 內建 `/metrics`，照名稱輸出 | [^ver-ch253-disk-suffix][^ver-ch263-disk-suffix] |
| 26.8 | 改成 `DiskAvailable{disk}`；`Network*{interface}`、`Block*{device}`、`CPUFrequencyMHz{cpu}`、`Temperature{sensor}`、`EDAC*{mc}` 也一樣 | 卡片規則從 26.8 起有資料；用舊名的 dashboard 在 26.8 起變空 | `asynchronous_metrics_key_values_mode` 預設新格式，可改回舊名 | [^ver-ch268-disk-label][^ver-ch268-net-label][^ver-ch268-kv-mode] |
| 26.3 | 新增 CurrentMetric `ZooKeeperSessionExpired` | `keeper-session` 規則在 25.3、25.8 回傳空；舊版只能看 `ZooKeeperSession` 與 `system.replicas.is_session_expired` | 內建 `/metrics` | [^ver-ch258-zk-session][^ver-ch263-zk-expired] |
| 25.3–26.10 | `/metrics` 輸出 `system.errors`（`ClickHouseErrorMetric_*`） | `error-counters`、`writes-rejected` 五個版本都能用 | 內建 `/metrics` | [^ver-ch253-errors] |
| 25.3–26.10 | `system.replicas` 的 `is_readonly`、`is_session_expired`、`absolute_delay` 等；`system.backups` 的 `status`、`end_time` | 本 repo `queries/clickhouse/clickhouse.yaml` 的查詢在 25.3 起都能跑 | query-exporter 照 SQL | [^ver-ch253-replicas][^ver-ch253-backups] |

## 各版本要注意的地方 {#notes}

- **25.3、25.8**：已不在安全更新範圍。磁碟 metric 是舊名；沒有 `ZooKeeperSessionExpired`，Keeper session 改用自訂查詢的 `chq_replica_session_expired`。
- **26.3**：有 `ZooKeeperSessionExpired`；磁碟 metric 仍是舊名。
- **26.8、26.10**：磁碟與網卡等改成 label；卡片的名稱都對得上。從 26.3 升上來時，舊名的 dashboard 與規則會一起變空。

## 卡片上會隨版本變的訊號 {#signals}

| 訊號 | 隨版本變什麼 | 建議 |
|---|---|---|
| `disk-space`、`metric-families-present` | 26.8 起 `DiskAvailable{disk}`，之前是 `DiskAvailable_<disk>` | `applies_when: ClickHouse 26.8 以上（或 asynchronous_metrics_key_values_mode 為預設）`；26.3 以前用 `{__name__=~"ClickHouseAsyncMetrics_DiskAvailable_.*"}` 的替代規則 |
| `keeper-session` | `ZooKeeperSessionExpired` 26.3 起 | `applies_when: ClickHouse 26.3 以上`；25.x 用 `chq_replica_session_expired` |
| 其他 | metric 與 system 表欄位五個版本都有 | 不用改 |

ClickHouse 的監控見 [ClickHouse 怎麼監控](../clickhouse.md)。
