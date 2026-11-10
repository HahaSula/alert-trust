# TiDB lab 摘要（2026-11-10）

TiDB v8.5.8（PD ×3、TiKV ×3、TiDB ×1）、Prometheus v3.13.5（scrape 5 秒）、blackbox_exporter v0.27.0。數字取自 `run-20261110.txt`。
秒數用主機時鐘量（SQL、HTTP 耗時）；Prometheus 內的時間只當參考（容器單調時鐘慢約 30%，牆鐘也會被 WSL 校時往回跳）。

## 三方交叉

健康 metric：PD 用 `etcd_server_has_leader` 與 `service_member_role`；TiKV 用 PD 看到的 store 狀態；TiDB 用 `time() − tidb_domain_lease_expire_time`（> 0 表示 schema lease 過期）。

| 情境 | 對象 | up | probe | 健康 metric | 判讀 |
|---|---|---|---|---|---|
| 正常 | 全部 | 1 | 1（PD /readyz、TiKV /status、TiDB /status） | has_leader 1；store Up；lease −30 秒左右 | — |
| C 停 tikv3，+33 秒 | tikv3 | 0 | 0 | store 1 Disconnected | process 停了 |
| C +85 秒 | tikv3 | 0 | 0 | store 1 Down（max-store-down-time 1m） | |
| C +142 秒 | 叢集 | — | — | down-peer 5、pending-peer 5、miss-peer 0、undersized 5 | 只有 3 台，PD 沒地方補副本 |
| D PD leader（pd2）抓不到 | pd2 | 0 | /readyz 1 | service_member_role 整條不見；pd_cluster_status 0 條；follower 的 pd_regions_status 全 0 | scrape 本身的問題；叢集狀態看不到 |
| E 停 PD leader，+29 秒 | pd2 | 0 | 0 | 新 leader pd3（service_member_role、etcd_server_is_leader 都在 pd3） | |
| F 停 2 台 PD，+16 秒 | pd1（存活） | 1 | /readyz 12 秒逾時；/health 503 RAFT NO LEADER；/livez 200；/pd/api/v1/health 200（列出 pd2、pd3 health false）；/pd/api/v1/ping 200 | has_leader 0；沒有 service_member_role | PD 不能服務，/metrics 照常 |
| F +16 秒 | tidb | 1 | /status 200 | lease 過期 48 秒 | SQL 層活著、交易不行 |
| F +16 秒 | tikv ×3 | 1 | /status 200 | store 狀態看不到（PD 沒有 leader） | |

## SQL（主機時鐘）

| 時間點 | `select 1` | `select count(*)` | `insert` |
|---|---|---|---|
| C TiKV 停著 | — | 0.17 秒 | 0.17 秒 |
| E 停 PD leader +5 秒 | — | — | 8.71 秒（成功） |
| E +20 秒 | — | — | 0.18 秒 |
| F PD 失去 quorum +16 秒 | 0.16 秒成功 | 25 秒 client 逾時 | 25 秒 client 逾時 |
| F +150 秒 | 0.18 秒成功 | 25 秒逾時 | — |
| PD 恢復 45 秒後 | — | — | 0.17 秒 |

F 時 TiDB 的 `pd_client_cmd_handle_failed_cmds_duration_seconds_count` rate 每秒 3.6；`tidb_tikvclient_backoff_seconds_count{type="pdRPC"}` 2 分鐘增加 394；`tidb_session_schema_lease_error_total` 沒有 series（官方 TiDB_schema_error 沒有響）。

## 官方規則

| 問題 | 結果 |
|---|---|
| (a) `name="apply_.*"` | 0 條；`name=~"apply_.*"` 每台 2 條（apply_0、apply_low_0）。官方 expr 回傳 0 條，修正後 3 條 |
| (b) `job="tidb"` 寫死 | 同一台 TiDB 用 job `tidb-other-name` 再抓一次：`go_memstats_heap_inuse_bytes{job="tidb"}` 只對到 1 條，另一個 job 的那條規則看不到；沒有任何官方規則會因此響 |
| (c) 以 `etcd_server_is_leader` 為前提 | C 時 PD_cluster_down_tikv_nums firing；D 讓 PD leader 抓不到後，store 仍是 Down，兩條官方 expr 都回傳 0 條，`sum(etcd_server_is_leader)` 是 0；alerts 清單裡 PD_cluster_down_tikv_nums 消失。我們的 TiDBPDLeaderMetricsMissing firing |
| (d) PD_miss_peer_region_count 用 `type="miss_peer_region_count"` | 0 條；PD 輸出的是 `miss-peer-region-count`（3 條） |
| selector 稽核 | 18 條官方規則有 selector 對到 0 條：其中 `tidb_monitor_keep_alive_total`、`tikv_pd_heartbeat_tick_total` 在 v8.5.8 原始碼不存在；其他是事件發生才註冊的 counter（例如 tikv_critical_error_total）或沒開的功能 |

## 其他觀察

- 每台 `/metrics`：TiKV 約 1.1MB、13,119 個 sample（region leader 那台 27,249）；TiDB 約 0.93MB、9,182；PD 約 0.36MB、3,309（leader 4,524）。名稱：PD 331、TiKV 437、TiDB 622（`names-*.txt`）。
- `/metrics` 耗時 0.02–0.03 秒；TiKV 有一次 9.68 秒（主機時鐘，只出現一次，沒有追原因；可能是 WSL 校時）。
- TiKV v8.5.8 映像：未去掉 debug info 時，啟動後 jemalloc allocated 約 2.35GB，heap profile 的三筆大配置（805MB、537MB、470MB）都在 backtrace-loader 執行緒解壓 DWARF；`objcopy --strip-debug` 後約 160MB。mem_limit 1g 時 RSS 一直貼著上限，`TiKV_memory_quota`（RSS / quota > 0.8）會一直響。
- 這台機器的環境雜訊：`PD_node_restart`、`TiDB_node_restart` 一直 firing（WSL2 上 `process_start_time_seconds` 會跳）；`PD_system_time_slow`、`TiDB_monitor_time_jump_back_error` firing（牆鐘被往回調，TiKV log 每約 33 秒一次 "system time jumped back" 約 6 秒）。
- tidb-sql（blackbox tcp 等 MySQL greeting 裡的 "TiDB"）在正常時也時有時無，沒有用在規則裡。
- TiDBGcSafePointStale 整段 pending：lab 約 11 分鐘，`pd_gc_gc_safepoint` 一直沒出現（第一次 GC 在 `tidb_gc_run_interval` 10 分鐘之後；規則 for 1h，沒有觸發）。
