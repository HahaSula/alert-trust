# 附錄：從原始碼到 metric（TiDB）

[主頁](tidb.md)列出每個訊號該看哪個 metric。這頁回答另一個問題：**這個 metric 或健康端點是在哪一行產生的？什麼時候更新、誰會輸出？**TiDB 沒有 exporter，鏈只有兩段：

1. **元件端**：TiDB、TiKV、PD v8.5.8（commit `8b857ef`、`a1fdd6a`、`4ecadf2`）；TiDB 用的 PD client 與 client-go 釘在 TiDB v8.5.8 `go.mod` 的 commit（`85a975a5ca78`、`c3b50791b9fb`）；PD 內嵌的 etcd 釘在 v3.5.15。
2. **Metric**：名稱、型別、更新時機。

每個原始碼參考都在 tag 上依關鍵字找到行號。「Lab」欄只寫在 `lab/targets/tidb` 親眼看到的情況。

## 先講結論

1. **三種元件的健康端點都不看服務能力。**TiDB `/status` 只看 health 旗標（啟動後 true、關機時 false）；TiKV `/status` 一律 `Response::default()`；PD `/pd/api/v1/health` 用非 linearizable 的成員清單、對每台打一個空的 `/pd/api/v1/ping`。<span class="at-chip at-ev-tested">實測</span>[^tidb-h-status-health][^tikv-h-status][^pd-memberlist-nonlinear][^pd-h-ping][^tidb-exp-raw]
2. **PD 的叢集 metric 只有 leader 在算。**schedulingController 每 10 秒寫一次 store 與 region 狀態，停止時 reset；`pd_regions_status` 的幾個 child 在套件初始化時就建立，所以 follower 也輸出 0。<span class="at-chip at-ev-tested">實測</span>[^pd-sched-metrics-collect][^pd-sched-metrics-reset][^pd-region-miss-label][^tidb-exp-raw]
3. **TiKV 的 metric 多數是 lazy_static。**第一次用到才註冊，健康時可能完全沒有 series。<span class="at-chip at-ev-tested">實測</span>[^tikv-m-loglag][^tikv-m-critical][^tidb-exp-names]
4. **label 的寫法要照原始碼。**`pd_regions_status` 用連字號；TiKV 的執行緒名稱把 `-` 換成 `_`。官方規則兩處都寫錯。<span class="at-chip at-ev-tested">實測</span>[^pd-region-miss-label][^tikv-thread-sanitize][^pd-rule-miss][^tikv-rule-apply]

## 健康端點 {#health}

| 元件 | 端點 | 檢查 | PD 失去 quorum 時（lab） | 依據 |
|---|---|---|---|---|
| TiDB | `/status`（10080） | `s.health`：listener 啟動後 true、`startShutdown` 時 false；內容是連線數、版本 | 200 | [^tidb-h-status-route][^tidb-h-status-health][^tidb-srv-health-true][^tidb-srv-health-false] |
| TiKV | `/status`（20180） | 無，回空的 200；`/status` 與 `/metrics` 不檢查 client 憑證的 role | 200 | [^tikv-h-status] |
| PD | `/pd/api/v1/health` | 成員清單（`Linearizable: false`）＋每台 `/pd/api/v1/ping`（3 秒逾時、200 就算健康） | 200，停掉的兩台 `health: false` | [^pd-h-health][^pd-memberlist-nonlinear][^pd-checkhealth-url][^pd-checkhealth] |
| PD | `/pd/api/v1/ping` | 空的 handler | 200 | [^pd-h-ping] |
| PD | `/readyz`、`/health`、`/livez`（內嵌 etcd） | etcd v3.5.15 的檢查：`/readyz` 有 linearizable read；`/health` 看 alarm 與 leader | `/readyz` 12 秒逾時；`/health` 503 `RAFT NO LEADER`；`/livez` 200 | [^pd-etcd-readyz-linear][^pd-etcd-health-noleader][^pd-userhandlers] |

根目錄的 `/health` 在 PD 的 router 裡標成 deprecated，但 lab 打到的回應是 etcd 格式的 `{"health":"true","reason":""}`。[^pd-h-health-route-deprecated][^tidb-exp-raw]

## PD：leader、store、region {#pd}

| Metric | 型別 | 產生的地方 | 更新時機 | Lab | 依據 |
|---|---|---|---|---|---|
| `service_member_role{service="PD"}` | gauge | 成為 PD leader | 設 1；失去 leader 設 0 | 只在 leader 那台有 | [^pd-service-role-1][^pd-service-role-0][^pd-m-service-role] |
| `etcd_server_is_leader` | gauge | 內嵌 etcd | etcd 的 raft leader；和 PD leader 是兩件事，PD 依 priority 搬動 | lab 一直和 PD leader 同一台 | [^pd-etcd-leader-priority] |
| `pd_cluster_status{type,engine,store}` | gauge | PD leader 的 schedulingController | 每 10 秒；store 狀態依序判定 Down → Unhealthy → Disconnected → Slow → Up | 只有 leader 輸出（54 條）；停 tikv3 後 +33 秒 disconnected、+85 秒 down | [^pd-m-cluster-status][^pd-store-status-order][^pd-metrics-interval] |
| `pd_regions_status{type}` | gauge | 同上 | 每 10 秒；type 用連字號 | follower 輸出 0；停 tikv3 後 down-peer 5 | [^pd-m-regions-status][^pd-region-miss-label][^pd-region-down-label] |
| `pd_gc_gc_safepoint{type="gc_safepoint"}` | gauge | 收到新的 GC safe point | 第一次更新才出現 | 約 11 分鐘內沒有出現 | [^pd-gc-safepoint] |

store 狀態的時間：Disconnected > 20 秒、Unhealthy > 10 分鐘、Down ≥ `max-store-down-time`（預設 30 分鐘）。[^pd-store-disconnect][^pd-store-unhealthy][^pd-store-down-default]

## TiKV {#tikv}

| Metric | 型別 | 產生的地方 | 陷阱 | 依據 |
|---|---|---|---|---|
| `tikv_thread_cpu_seconds_total{name}` | counter | 讀 `/proc` 的每條執行緒 | 名稱 `-` 換成 `_`：apply pool 是 `apply_0`、`apply_low_0` | [^tikv-thread-sanitize][^tikv-apply-spawn][^tikv-batch-thread-name] |
| `tikv_raftstore_append_log_duration_seconds`、`commit_log`、`apply_log` | histogram | raftstore | — | [^tikv-m-append][^tikv-m-commit][^tikv-m-apply] |
| `tikv_raftstore_log_lag` | histogram | raftstore | lazy_static | [^tikv-m-loglag] |
| `tikv_engine_write_stall{db,type}` | gauge | RocksDB 統計 | type 是 write_stall_max、percentile99 等 | [^tikv-m-write-stall] |
| `tikv_store_size_bytes{type}` | gauge | PD client 的 store heartbeat | — | [^tikv-m-store-size] |
| `tikv_server_memory_quota_bytes` | gauge | 啟動時 | lab：cgroup 640MB 時 671,088,640 | [^tikv-m-memquota] |
| `tikv_critical_error_total` | counter | — | lazy_static，lab 0 條 | [^tikv-m-critical] |
| `tikv_gcworker_autogc_safe_point` | gauge | gc_manager | lazy_static | [^tikv-m-autogc-sp] |

`tikv_pd_heartbeat_tick_total`（官方 TiKV_leader_drops 用的）在 v8.5.8 原始碼找不到註冊的地方，lab 的名稱清單也沒有。[^tikv-rule-leader-drops][^tidb-exp-names]

region 的 down peer：leader 判定副本超過 `max-peer-down-duration`（預設 10 分鐘）沒回應；`hibernate-regions` 開著時會被調成至少 2 × `peer-stale-state-check-interval`。[^tikv-max-peer-down][^tikv-peer-down-hibernate]

**啟動時的記憶體。**`set_panic_hook` 開一條 backtrace-loader 執行緒預先載入 debug info；v8.5.8 映像的 binary 帶壓縮的 debug section，lab 的 heap profile 裡三筆大配置（805MB、537MB、470MB）都在這條執行緒解壓 DWARF。<span class="at-chip at-ev-tested">實測</span>[^tikv-backtrace-loader][^tidb-exp-heap]

## TiDB server {#tidb}

| Metric | 型別 | 產生的地方 | 陷阱 | 依據 |
|---|---|---|---|---|
| `tidb_domain_lease_expire_time` | gauge | schema validator 更新 lease 時 | lease 授予時間（TSO）＋ lease；PD 不能用時停止更新 | [^tidb-m-lease-expire][^tidb-lease-expire-set][^tidb-lease-expired-check] |
| `tidb_server_connections` | gauge | — | — | [^tidb-m-connections] |
| `tidb_server_query_total{type,result}` | counter | 每個命令 | — | [^tidb-m-query-total] |
| `tidb_server_execute_error_total{type}` | counter | 執行失敗 | type 是 RFC code，非 terror 的錯誤是 unknown | [^tidb-m-execute-error][^tidb-m-execute-error-label] |
| `tidb_server_handle_query_duration_seconds` | histogram | — | — | [^tidb-m-handle-query] |
| `tidb_server_slow_query_process_duration_seconds` | histogram | 慢查詢 | 只記超過門檻的 | [^tidb-m-slow-query] |
| `tidb_server_tokens` | gauge | 拿 token 時 | token-limit 預設 1000 | [^tidb-m-tokens][^tidb-cfg-token-limit] |
| `tidb_server_memory_quota_bytes` | gauge | 啟動時 | — | [^tidb-m-memquota] |
| `tidb_executor_ongoing_txn_duration_seconds` | histogram | 執行中的交易 | — | [^tidb-m-ongoing-txn] |
| `tidb_tikvclient_gc_config{type}` | gauge | GC worker | `tikv_gc_life_time` 預設 600 | [^tidb-gc-config-gauge][^tidb-gc-lifetime-default] |
| `tidb_log_backup_last_checkpoint{task}` | gauge | log backup advancer | TSO；只在有 task 時 | [^tidb-logbackup-checkpoint] |
| `pd_client_cmd_handle_cmds_duration_seconds{type}` | histogram | PD client | `wait` 是等 TSO 結果 | [^pdc-m-cmd-duration][^pdc-m-wait] |
| `pd_client_cmd_handle_failed_cmds_duration_seconds{type}` | histogram | PD client | `tso` 是取 TSO 失敗 | [^pdc-m-failed-tso] |
| `tidb_tikvclient_backoff_seconds{type}` | histogram | client-go | type 區分鎖、region、RPC | [^cg-m-backoff] |
| `tidb_tikvclient_region_err_total{type}` | counter | client-go | `server_is_busy` | [^cg-m-region-err][^cg-server-busy-label] |

`tidb_monitor_keep_alive_total`（官方 TiDB_monitor_keep_alive 用的）只剩 `metrics_schema` 的 PromQL 定義，原始碼沒有註冊。[^tidb-keepalive-infoschema]

記憶體：`tidb_server_memory_limit` 預設 80%，超過時 global memory controller 取消最耗記憶體的 SQL，只寫 log；單條 SQL 的上限是 `tidb_mem_quota_query`，超過時依 `tidb_mem_oom_action`（預設 CANCEL）。[^tidb-var-memlimit][^tidb-memlimit-kill][^tidb-var-oom-action]

## 沒有釘到原始碼行的部分 {#unpinned}

- `tidb_statistics_auto_analyze_total`、`tidb_ddl_waiting_jobs` 只引用官方規則，沒有逐行核對產生的地方。
- `pd_hotspot_*`、`pd_checker_event_count` 的更新時機沒有核對。
- TiKV `/metrics` 那一次 9.68 秒的原因沒有追。

## 實驗 {#lab}

`lab/targets/tidb`：TiDB v8.5.8（PD ×3、TiKV ×3、TiDB ×1）、Prometheus v3.13.5、blackbox_exporter v0.27.0。原始輸出在 `results/run-20261110.txt`，摘要在 `results/summary.md`。[^tidb-exp]
