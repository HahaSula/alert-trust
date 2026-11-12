# Prometheus 資源邊際

從 `prometheus` pack 進來。數字標「量測／使用者說／推算」，門檻都是實驗值（30 萬 series 單機），規模不同要重新確認。證據在 `$AT/catalog/components/prometheus.yaml` 的 `silent_failures` 與訊號 `refs`。

先確認 Prometheus 在做什麼，再談要不要加資源。Scrape、rule、查詢和 TSDB 維護共用同一個行程，但瓶頸各不相同。

讀快照 `<id>/peaks.json`（`cpu_cores_peak`、`throttling_ratio_peak`、`mem_ratio_peak`、`wal_size`、`blocks_size`、`lowest_ts_age_days`、`retention_*`、`reloads_min_5m`、`compactions_failed`、`checkpoint_failed`、`wal_fsync_p99_peak`、`query_slots_peak`、`queue_time_p90_peak`）、`runtimeinfo.json`、`flags.json`。缺資料時請使用者執行下方查詢。

## CPU

```promql
rate(process_cpu_seconds_total{job=~".*prometheus"}[5m])
rate(container_cpu_cfs_throttled_periods_total{container="prometheus"}[5m])
  / rate(container_cpu_cfs_periods_total{container="prometheus"}[5m])
prometheus_rule_group_last_duration_seconds / prometheus_rule_group_interval_seconds
```

| 判斷 | 說明 |
|---|---|
| **Rule 失準的轉折點是 group 耗時比值超過 1** | 實測 throttling 已 78% 時延遲幾乎沒變；比值超過 1 才開始跳過 |
| `GOMAXPROCS` 大於 CPU limit | 看 `runtimeinfo`。2.x 預設用節點核心數，要加 `--enable-feature=auto-gomaxprocs`；3.0 起預設對齊。實測 1 核限制下 throttling 從 37% 降到 3% |
| 加 CPU 不一定有用 | 每個 target 只有一條 scrape goroutine。Scrape 超過 timeout 仍慢，代表時間花在 parse、relabel、append |

## Scrape 慢：先分辨原因

`scrape_timeout` 只管 HTTP，而且不能大於 interval。所以 `scrape_duration_seconds` 超過 timeout 又成功時，多出來的時間都在 Prometheus 內部。

```promql
topk(10, scrape_duration_seconds)
topk(10, scrape_samples_scraped)
```

- **少數大 target 慢：** 用 `metric_relabel_configs` 丟不用的 series、拆 target、或把這個 job 的 interval 改成符合實際。
- **所有 target 一起慢：** 資源競爭。看 throttling、GC（`go_gc_duration_seconds`）、head series 是否暴增。

修好後實際寫入量會上升，因為之前 tick 被丟掉了。先確認資源有餘裕。

## 記憶體

```promql
container_memory_working_set_bytes{container="prometheus"}
  / on (namespace, pod, container) group_left
kube_pod_container_resource_limits{container="prometheus",resource="memory"}
prometheus_tsdb_head_series
prometheus_tsdb_data_replay_duration_seconds
```

- kube-prometheus-stack 預設丟棄 `container_spec_*`，所以 limit 要用 kube-state-metrics 的 `kube_pod_container_resource_limits`。
- Prometheus 被 OOM 時自己抓不到資料，`OOMKilled` 要由外部 Prometheus 或 `kubectl describe` 看。
- 實測 working set 超過 limit 的 83% 開始 OOM，所以建議告警門檻 80%。這是 30 萬 series 的結果，規模不同要重新確認。
- **Replay 是盲區。** Replay 期間 rule 不會執行，重啟後 `for` 小於 10m 的 alert 還要重新計時。實驗環境只有 8–13 秒，大型環境可能很久。問使用者：上次重啟花多久？
- 上游回報大型環境 replay 的記憶體可達平時 2–3 倍，實驗環境無法驗證。用實際重啟時的尖峰判斷。
- OOM 會損毀 WAL：實測讀到 `unexpected checksum` 並修復。

## 磁碟與 retention

```promql
prometheus_tsdb_wal_storage_size_bytes
prometheus_tsdb_storage_blocks_bytes
(time() - prometheus_tsdb_lowest_timestamp_seconds) / 86400
prometheus_tsdb_retention_limit_seconds / 86400
increase(prometheus_tsdb_reloads_total[15m])
```

**Retention 卡住的兩條路徑：**

| 路徑 | `reloads_total` | `compactions_failed_total` | 原因 |
|---|---|---|---|
| 磁碟突然完全寫滿 | 正常 | 不增加 | Commit 全失敗，沒有新 block。Time retention 以最新 block 為基準，所以凍結 |
| 空間被 compaction 用完 | **停止** | **持續增加** | Head 維持可 compact，commit 不斷送訊號，1 分鐘的 reload timer 被重置 |

**WAL 半筆寫入：** 磁碟滿時跨頁紀錄可能只寫一半，空間恢復後寫入看似正常，下一次 checkpoint 才失敗，WAL 開始增長。這在 3.x 也會發生。訊息可能是 `unexpected full record` 或 `unexpected first record`。

| 看到 | 代表 | 處置 |
|---|---|---|
| `compactions_failed` 增加，checkpoint 正常 | 空間不足 | 釋放空間，實驗中不必重啟就恢復 |
| `checkpoint_creations_failed` 增加 | WAL 有損毀 | 重啟修復，修復點之後的資料會遺失 |
| `wal_corruptions_total > 0` | 本次啟動修復過 | 查 log 起因；舊版 runtime 損毀時這個值還是 0 |

**不要**手動只刪單一 WAL segment（上游回報會導致無法啟動）。`retention.size` 不要貼近磁碟容量，compaction 和 WAL 都是先增加用量才釋放。只設 `retention.size` 時，time retention 是停用的。

## IOPS

```promql
prometheus_tsdb_wal_fsync_duration_seconds{quantile="0.99"}
prometheus_tsdb_compaction_duration_seconds_sum / prometheus_tsdb_compaction_duration_seconds_count
```

實測寫入限制到 10 IOPS 時，scrape 和 rule 幾乎不受影響，因為 WAL 先寫 page cache；受影響的是 fsync、compaction，reload 頻率約減半，retention 延後。那次記憶體充足，page cache 吃緊時可能不同。

## 查詢

```promql
prometheus_engine_queries / prometheus_engine_queries_concurrent_max
prometheus_engine_query_duration_seconds{slice="queue_time",quantile="0.9"}
```

Rule 和 API 共用查詢 slot（`--query.max-concurrency`，預設 20）。Grafana 的重查詢會讓 rule 排隊。

## 回報格式

| 資源 | 現況 | 上限 | 比值 | 狀態 | 第一個會失效的東西 |
|---|---|---|---|---|---|

標明每個數字是量測、使用者說或推算，並提醒實驗門檻是經驗值。完成後回到 `prometheus` pack。
