# TiDB 各版本的監控差異

這頁列出 TiDB 7.5、8.1、8.5 三條 LTS 線之間會影響監控的差異：TiDB／TiKV／PD 的 metric 新增、官方 alert 規則的改動、PD 內嵌 etcd 的版本。目前的卡片核對過 v8.5.8。

依據是三個 repo 各線最新 tag（`v7.5.8`、`v8.1.2`、`v8.5.8`）的 metric 定義，以及官方規則檔（`pkg/metrics/alertmanager/tidb.rules.yml`、`metrics/alertmanager/tikv.rules.yml`、`tikv.accelerate.rules.yml`、`pd.rules.yml`）逐檔 diff。沒有做實驗，全部是<span class="at-chip at-ev-code">讀碼</span>。

## 結論 {#summary}

1. **記憶體 quota metric 8.1 才有。**`tidb_server_memory_quota_bytes`、`tikv_server_memory_quota_bytes`、`pd_service_memory_quota_bytes` 在 7.5 都不存在；卡片 `memory-quota` 的比例規則在 7.5 回傳空。7.5 只有 CPU quota（`tikv_server_cpu_cores_quota`、`*_maxprocs`）。
2. **官方規則 8.1 新增六條 quota 告警**（TiDB／TiKV／PD 各一組 CPU 與記憶體），8.5 再加 `TiDB_auto_analyze_failed`。PD 與 TiKV 的規則檔在 8.1.2 與 8.5.8 完全相同。
3. **TiKV 的 coprocessor 等待規則 8.1 起只取 `type="all"`。**7.5 的規則沒有這個條件，在 8.1 以上的 TiKV 上會把各分項一起算進 histogram（推論）；拿 7.5 的規則檔套 8.x 叢集要改。
4. **PD 7.5、8.1 內嵌 etcd v3.4.31，8.5 內嵌 v3.5.15。**兩者都有 `/readyz`；但 PD 7.5、8.1 上沒有 etcd 3.6 才加的 metric（`etcd_network_known_peers` 等）。
5. **卡片其他訊號用的 metric 三條線都有**：`pd_regions_status`、`pd_cluster_status`、`service_member_role`、`tikv_engine_write_stall`、`tikv_raftstore_log_lag`、`tidb_server_handle_query_duration_seconds`、`tidb_server_critical_error_total`、`tidb_log_backup_last_checkpoint` 等。

## 選的版本 {#versions}

| 版本線 | 這頁用的 tag（TiDB、TiKV、PD 相同） | 備註 |
|---|---|---|
| 7.5 LTS | `v7.5.8` | |
| 8.1 LTS | `v8.1.2` | |
| 8.5 LTS | `v8.5.8` | 卡片核對過、lab 實測 |

## 版本差異表 {#diff}

| 版本 | 變更 | 對監控的影響（哪個 metric／collector／規則） | exporter 或 endpoint 有沒有處理 | 依據 |
|---|---|---|---|---|
| 8.1 | TiDB 新增 `tidb_server_memory_quota_bytes`；TiKV 新增 `tikv_server_memory_quota_bytes`；PD 新增 `pd_service_memory_quota_bytes`（移到 `pkg/basicserver`） | `memory-quota` 規則在 7.5 回傳空 | 內建 `/metrics` | [^ver-ti-tidb81-memquota][^ver-ti-tikv81-memquota][^ver-ti-pd81-memquota][^ver-ti-tikv75-cpuquota][^ver-ti-pd75-maxprocs] |
| 8.1 | 官方規則加 `TiDB_cpu_quota`、`TiDB_memory_quota`、`TiKV_cpu_quota`、`TiKV_memory_quota`、`PD_cpu_quota`、`PD_memory_quota` | 7.5 叢集套 8.1 規則檔：記憶體那三條永遠不會響（metric 不存在） | — | [^ver-ti-rules81-tidb-mem][^ver-ti-rules81-tikv-mem][^ver-ti-rules81-pd-mem] |
| 8.1 | TiKV 規則 `tikv_coprocessor_request_wait_seconds_bucket` 加 `type="all"`；recording rule 的 futurepool pending 從 `rate` 改成 `avg_over_time` | 不同版本的規則檔不能混用 | — | [^ver-ti-rules75-cop][^ver-ti-rules81-cop-type][^ver-ti-rules81-futurepool] |
| 8.5 | 官方規則加 `TiDB_auto_analyze_failed`（`tidb_statistics_auto_analyze_total{type="failed"}`） | `ddl-and-stats` 的 metric 在三條線都有，8.5 才有官方規則 | — | [^ver-ti-rules85-analyze] |
| 7.5、8.1 | PD 內嵌 etcd 是 commit `950cd5f`（etcd v3.4.31） | `/readyz` 可用；etcd 3.6 以後的 metric 沒有 | 內建 endpoint | [^ver-ti-pd75-etcd][^ver-ti-pd81-etcd] |
| 8.5 | PD 內嵌 etcd v3.5.15 | 同上（3.5 也沒有 `known_peers`） | 內建 endpoint | [^ver-ti-pd85-etcd] |

## 各版本要注意的地方 {#notes}

- **7.5**：沒有記憶體 quota metric；記憶體告警只能用 `process_resident_memory_bytes` 對固定值或 cgroup 上限（例如 cAdvisor）。官方規則檔不要拿 8.1 以後的。
- **8.1**：有 quota metric 與對應官方規則；PD 內嵌 etcd 仍是 3.4。
- **8.5**：卡片核對的版本；多一條 auto analyze 失敗規則。

## 卡片上會隨版本變的訊號 {#signals}

| 訊號 | 隨版本變什麼 | 建議 |
|---|---|---|
| `memory-quota` | quota metric 8.1 起 | `applies_when: TiDB 8.1 以上`；7.5 改用 cgroup 上限 |
| `ddl-and-stats` | 官方規則 8.5 才有 auto analyze 失敗 | 卡片的規則三條線都能用，不用改 |
| `server-busy`、`tso-wait` | 官方規則與 recording rule 在 8.1 改過 | 引用官方規則時寫明是哪個版本的檔案 |
| `pd-quorum`、`health-second-path`、`leader-changes` | PD 內嵌 etcd 3.4（7.5、8.1）／3.5（8.5） | 只用 `etcd_server_has_leader`、`is_leader`、`leader_changes_seen_total`、`/readyz`（三個都有）；不要用 etcd 3.6 的 metric |
| 其他 | metric 三條線都有 | 不用改 |

TiDB 的監控見 [TiDB 怎麼監控](../tidb.md)，PD 內嵌 etcd 各版本的差異見 [etcd 各版本的監控差異](etcd.md)。
