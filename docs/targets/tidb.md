# TiDB 怎麼監控

**目標：知道 TiDB 叢集現在能不能執行交易（拿得到 TSO、region 有多數副本），以及哪一層、哪一台離不能還有多遠。**TiDB 叢集有三種 process：PD（排程與 TSO）、TiKV（儲存）、TiDB server（SQL），各自在 status port 提供 `/metrics`，沒有 exporter。三種元件的「健康端點」都不看自己能不能服務，所以 `up` 加上 `/status` 都還不夠。這頁依九個面向從大到小排列：叢集 → 可用性 → 複寫 → 資源 → 工作量 → 維護 → 保護 → 容量，最前面是監測自檢。

依據是 TiDB、TiKV、PD v8.5.8 的原始碼（PD 內嵌 etcd v3.5.15），加上一組小實驗：PD ×3、TiKV ×3、TiDB ×1，停 TiKV、讓 PD leader 抓不到、停 PD leader、停兩台 PD 失去 quorum，並稽核官方規則<span class="at-chip at-ev-tested">實測</span>[^tidb-exp]。`gh api .../releases/latest` 回的是 v7.5.8（最後發布的 LTS 修補版），版本號最高的穩定版是 v8.5.8，以它為準。這台機器的 Docker 容器時鐘比牆鐘慢約 30%，牆鐘還會被 WSL 往回調；秒數以主機時鐘量的 SQL、HTTP 耗時為準，Prometheus 內的時間只當參考。訊號與規則在[元件卡](../components/tidb.md)，原始碼位置在[附錄](tidb-appendix.md)，機制在[分散式 SQL 架構](../architecture/db-distsql.md)與[共識（Raft）架構](../architecture/db-raft.md#tikv)。7.5、8.1、8.5 之間的 metric 與官方規則差異見 [TiDB 各版本的監控差異](versions/tidb.md)。

## 先講結論

1. **三種元件的健康端點都不看自己能不能服務。**PD 失去 quorum 時，存活那台 `/pd/api/v1/health` 仍回 200（只在 JSON 裡把停掉的成員標 `health: false`）；TiDB 的 `/status` 200、`select 1` 成功，讀表與寫入 25 秒逾時；TiKV 的 `/status` 一律是空的 200。<span class="at-chip at-ev-tested">實測</span>[^tidb-exp-raw][^pd-memberlist-nonlinear][^tidb-h-status-health][^tikv-h-status]
2. **PD 要探內嵌 etcd 的 `/readyz`，TiDB 要看 schema lease。**PD 失去 quorum 時 `/readyz` 逾時（12 秒）、`/health` 503 `RAFT NO LEADER`；TiDB 的 `time() − tidb_domain_lease_expire_time` 在 16 秒內變正（lease 過期）。見[三方交叉](#health-second-path)。<span class="at-chip at-ev-tested">實測</span>[^tidb-exp-raw][^pd-etcd-readyz-linear][^tidb-lease-expire-set]
3. **PD leader 抓不到時，官方叢集規則全部安靜。**官方 PD 規則以 `etcd_server_is_leader` 為前提；lab 讓 PD leader 的 scrape 失敗（PD 正常），store 仍是 Down，PD_cluster_down_tikv_nums 從 firing 變成回傳空。follower 沒有 `pd_cluster_status`、`pd_regions_status` 是 0，拿掉前提也只會得到 0。我們改成「沒有任何一台以 leader 身分被抓到」本身告警。<span class="at-chip at-ev-tested">實測</span>[^tidb-exp-raw][^pd-rule-down][^pd-sched-metrics-collect]
4. **官方規則有四條永遠不會響。**`name="apply_.*"`（`=` 不是 `=~`）對到 0 條；`type="miss_peer_region_count"`（PD 輸出連字號）0 條；`tikv_pd_heartbeat_tick_total` 與 `tidb_monitor_keep_alive_total` 在 v8.5.8 不存在。<span class="at-chip at-ev-tested">實測</span>[^tidb-exp-raw][^tikv-rule-apply][^pd-rule-miss][^tikv-rule-leader-drops][^tidb-rule-keepalive]
5. **job 寫死時沒有任何規則會響。**官方規則用 `job="tidb"`、`job="pd"`、`job="tikv"`；換一個 job 名稱，規則就只看到原名稱那一條。我們每個 job 都加 `absent(up{job=...})`。<span class="at-chip at-ev-tested">實測</span>[^tidb-exp-raw][^tidb-rule-memabnormal]
6. **store 停掉的前 30 分鐘，官方 emergency 規則不響。**PD 依序判定 Disconnected（20 秒）、Unhealthy（10 分鐘）、Down（`max-store-down-time`，預設 30 分鐘）；PD_cluster_down_tikv_nums 只看 Down。lab 把它縮成 1 分鐘：+33 秒 Disconnected、+85 秒 Down。<span class="at-chip at-ev-tested">實測</span>[^tidb-exp-raw][^pd-store-status-order][^pd-store-down-default]
7. **TiKV 的 metric 很多要第一次用到才出現。**`tikv_critical_error_total`、`tikv_channel_full_total` 在健康的叢集是 0 條；「metric 消失」的檢查只能挑啟動就有的。<span class="at-chip at-ev-tested">實測</span>[^tidb-exp-names][^tikv-m-loglag]
8. **TiKV v8.5.8 映像啟動時多用約 1.9GB 記憶體。**binary 帶壓縮的 debug info，backtrace-loader 執行緒把它解壓進記憶體（heap profile）；記憶體上限小時 RSS 一直貼著上限，TiKV_memory_quota 會一直響。lab 用 `objcopy --strip-debug` 後約 160MB。<span class="at-chip at-ev-tested">實測</span>[^tidb-exp-heap][^tikv-backtrace-loader]

### 九個面向一覽 {#aspects}

| 面向 | 這頁的重點 | 元件卡的訊號 | 證據 |
|---|---|---|---|
| [監測自檢](#exporter) | 三種元件各自的三方交叉；job 名稱錯了要會響；metric 消失；scrape 預算 | endpoint-up、health-second-path、metric-families-present、scrape-budget、endpoint-blocking | <span class="at-chip at-ev-tested">實測</span> |
| [叢集](#cluster) | PD leader 抓不抓得到；PD quorum；元件數；leader 變動；region 健康 | role-detection、pd-quorum、component-count、leader-changes、region-health | <span class="at-chip at-ev-tested">實測</span> |
| [可用性](#availability) | store 狀態；TSO 拿不拿得到 | store-state、tso-available | <span class="at-chip at-ev-tested">實測</span> |
| [複寫](#replication) | pending peer、raft log lag | replication-lag | <span class="at-chip at-ev-tested">實測</span> |
| [資源與飽和](#saturation) | raftstore 延遲、write stall、ServerIsBusy、記憶體、TSO 等待 | raftstore-latency、write-stall、server-busy、memory-quota、tso-wait | <span class="at-chip at-ev-tested">實測</span>／<span class="at-chip at-ev-code">讀碼</span> |
| [工作量](#workload) | 錯誤、延遲、慢查詢、連線、衝突與 backoff、panic | query-errors、query-latency、slow-queries、connections、txn-conflicts、critical-errors | <span class="at-chip at-ev-tested">實測</span>／<span class="at-chip at-ev-code">讀碼</span> |
| [資料維護](#maintenance) | GC safe point、長交易、DDL 與統計 | gc-progress、ddl-and-stats | <span class="at-chip at-ev-code">讀碼</span> |
| [資料保護](#protection) | log backup checkpoint；全量 BR 沒有 metric | log-backup | <span class="at-chip at-ev-code">讀碼</span> |
| [容量](#capacity) | 每台 TiKV 與叢集的空間 | storage-space | <span class="at-chip at-ev-upstream">上游</span> |

## 監控模型與官方建議 {#model}

TiDB 屬於「資料庫內建端點」模型（見[資料庫監控模型](models.md#tidb)）。

| 項目 | 內容 |
|---|---|
| 抓什麼 | PD client port（2379）、TiKV status port（20180）、TiDB status port（10080）的 `/metrics`；PD 的回應含內嵌 etcd 的 `etcd_*`[^dbm-tidb-status-metrics][^pd-userhandlers] |
| 健康端點 | PD：內嵌 etcd 的 `/readyz`、`/health`、`/livez`，與 PD 自己的 `/pd/api/v1/health`（永遠 200）；TiKV：`/status`（空的 200）；TiDB：`/status`（只看關機旗標） |
| 官方建議 | TiUP 部署 Prometheus＋Grafana＋Alertmanager＋blackbox，規則來自三個 repo 的 rules.yml（[Deploy Monitoring Services](https://docs.pingcap.com/tidb/stable/deploy-monitoring-services/)） |
| 官方沒做的 | Prometheus、Alertmanager 本身的監控與 Watchdog（見 [models](models.md#tidb)） |

**和官方規則（v8.5.8）的差異：**

| 官方規則 | 我們 | 差在哪 |
|---|---|---|
| PD 的叢集規則：`... and (sum(etcd_server_is_leader) by (instance) > 0)`[^pd-rule-down] | 用 `max by (job)` 取叢集值，另加 TiDBPDLeaderMetricsMissing | PD leader 抓不到時官方全部回傳空（實測） |
| PD_miss_peer_region_count：`type="miss_peer_region_count"`[^pd-rule-miss] | TiDBRegionMissPeers：`miss-peer-region-count` | 官方 selector 對到 0 條（實測） |
| TiKV_thread_apply_worker_cpu_seconds：`name="apply_.*"`[^tikv-rule-apply] | TiKVApplyThreadBusy：`name=~"apply_.*"` | 官方 0 條，修正後每台 2 條（實測） |
| TiKV_leader_drops：`tikv_pd_heartbeat_tick_total`[^tikv-rule-leader-drops] | （不用） | metric 不存在（讀碼＋lab 名稱清單） |
| TiDB_monitor_keep_alive：`tidb_monitor_keep_alive_total{job="tidb"}`[^tidb-rule-keepalive] | TiDBMetricFamiliesMissing | metric 只剩 metrics_schema 的定義，沒有註冊[^tidb-keepalive-infoschema] |
| `job="tidb"`、`job="pd"`、`job="tikv"` 寫死[^tidb-rule-restart][^pd-rule-restart][^tikv-rule-restart] | 同樣用佔位名稱，但每個 job 有 absent | 名稱不同時官方安靜、我們響 |
| PD_cluster_down_tikv_nums（只看 Down） | TiDBStoreDisconnected、TiDBStoreDownOrUnhealthy | 官方前 30 分鐘只有 warning |
| tidb_tikvclient_backoff_seconds_count（不分 type）[^tidb-rule-backoff] | TiDBTxnBackoffHigh（by type）、TiDBTsoRequestsFailing | 分得出鎖衝突、region 搬動、PD 連不到 |
| PD_leader_change：數存過 TSO 的 PD 台數[^pd-rule-leaderchange] | TiDBPDLeaderChanges（內嵌 etcd 的 leader_changes） | — |
| 其他（write stall、raft append／apply、latch、空間、memory quota、DDL、analyze） | 相同門檻 | 拿掉 job 寫死 |
| （沒有） | 三方交叉、PD quorum、元件數、TSO 失敗、GC safe point、log backup | — |

**這台機器的雜訊。**PD_node_restart、TiDB_node_restart 一直 firing（WSL2 上 `process_start_time_seconds` 會跳），PD_system_time_slow、TiDB_monitor_time_jump_back_error 也 firing（牆鐘被往回調）；這些是環境造成的，不是規則的問題。[^tidb-exp-raw]

## 監測自檢 {#exporter}

### 三方交叉 {#health-second-path}

blackbox_exporter 從另一條路徑探測每台元件，probe job 把 `instance` relabel 成和 `/metrics` target 相同的 `host:port`（設定見 `lab/targets/tidb/prom/prometheus.base.yml`）。每種元件的健康 metric 不同：

| 元件 | probe | 健康 metric | 為什麼 |
|---|---|---|---|
| PD | 內嵌 etcd 的 `/readyz`（linearizable read） | `etcd_server_has_leader`、`service_member_role` | `/pd/api/v1/health` 永遠 200[^pd-h-health][^pd-checkhealth-url] |
| TiKV | `/status`（只代表 HTTP） | PD 看到的 store 狀態 | TiKV 沒有自己的健康端點[^tikv-h-status] |
| TiDB | `/status` | `time() − tidb_domain_lease_expire_time > 0` | `/status` 只看關機旗標[^tidb-h-status-health][^tidb-lease-expired-check] |

lab 的結果（「健康 metric」欄是同一時間點的值）：<span class="at-chip at-ev-tested">實測</span>[^tidb-exp-raw]

| 情境 | 對象 | `up` | probe | 健康 metric | 判讀 | 規則（firing 或 pending） |
|---|---|---|---|---|---|---|
| 正常 | 全部 | 1 | 1 | has_leader 1；store Up；lease −30 秒左右 | — | — |
| 停 tikv3，+33 秒 | tikv3 | 0 | 0 | store Disconnected | process 停了 | TiDBComponentEndpointDown、TiDBTiKVStatusProbeFailing |
| 停 tikv3，+85／+142 秒 | tikv3 | 0 | 0 | store Down；down-peer 5 | | TiDBStoreDownOrUnhealthy；TiDBRegionDownPeers、TiDBTiKVNotScraped pending |
| PD leader（pd2）抓不到 | pd2 | **0** | **1** | `service_member_role` 整條不見 | scrape 本身；叢集狀態看不到 | TiDBPDLeaderMetricsMissing；官方 PD_cluster_down_tikv_nums 消失 |
| 停 PD leader，+29 秒 | pd2 | 0 | 0 | 新 leader pd3 | | TiDBPDReadyzProbeFailing、TiDBTsoRequestsFailing |
| 停 2 台 PD：存活那台 | pd1 | **1** | **0**（逾時） | has_leader 0；沒有 leader | PD 不能服務 | TiDBPDServingMetricsButNotReady、TiDBPDQuorumAtRisk、TiDBPDLeaderMetricsMissing |
| 停 2 台 PD：TiDB | tidb | **1** | **1** | lease 過期 48 秒（+16 秒）、111 秒（+150 秒） | SQL 層活著、交易不行 | TiDBStatusOkButSchemaLeaseExpired、TiDBTsoRequestsFailing |
| 停 2 台 PD：TiKV | ×3 | 1 | 1 | store 狀態看不到 | PD 沒有 leader | （TiDBTiKVRunningButStoreNotUp 不評估，由 TiDBPDLeaderMetricsMissing 補位） |

PD 失去 quorum 時存活那台的端點（主機時鐘）：`/metrics` 200 357KB 0.02 秒；`/health` 503 `{"health":"false","reason":"RAFT NO LEADER"}`；`/readyz` 12 秒逾時；`/livez` 200；`/pd/api/v1/health` 200（2.21 秒，pd2、pd3 標 `health: false`）；`/pd/api/v1/leader` 500 `redirect failed`；`/pd/api/v1/ping` 200。<span class="at-chip at-ev-tested">實測</span>[^tidb-exp-raw]

同一時間 TiDB 的 SQL（主機時鐘）：`select 1` 0.16 秒成功（不需要 TSO）；`select count(*)` 與 `insert` 25 秒 client 逾時。所以「連得上、`select 1` 成功」的 SQL probe 也不夠，要做一次讀表或寫入，見[盲點](#blind-spots)。官方 TiDB_schema_error（`tidb_session_schema_lease_error_total{type="outdated"}`）這時沒有 series。<span class="at-chip at-ev-tested">實測</span>[^tidb-exp-raw][^tidb-rule-schema]

lease 過期的秒數比經過的時間大（+16 秒時已過期 48 秒）：lease 到期時間由 TSO 的時間戳算出，這台機器的牆鐘會被往回調，數字只當「已過期」看。<span class="at-chip at-ev-tested">實測</span>[^tidb-lease-expire-set]

### metric 消失、job 名稱、scrape 預算 {#exporter-scrape}

| 問題 | 做法 | 證據 |
|---|---|---|
| job 名稱錯了 | 每個 job 一條 `absent(up{job=...})`；官方沒有 | <span class="at-chip at-ev-tested">實測</span>：另一個 job 名稱的 TiDB 不在官方規則裡[^tidb-exp-raw] |
| 該有的 metric 還在嗎 | 每種元件挑啟動就有的兩個 metric（例如 TiKV 的 `tikv_raftstore_region_count`、`tikv_engine_write_stall`） | <span class="at-chip at-ev-tested">實測</span>：名稱 PD 331、TiKV 437、TiDB 622[^tidb-exp-names] |
| scrape 預算 | TiKV 約 1.1MB、13,119 個 sample，region leader 那台 27,249；TiDB 0.93MB；PD 0.36MB | <span class="at-chip at-ev-tested">實測</span>（約 5 個 region） |
| `/metrics` 會不會被卡 | TiDB、PD 是 promhttp；TiKV 從 registry 輸出、不碰 raftstore 或 RocksDB；lab 0.02–0.03 秒，TiKV 有一次 9.68 秒（只一次，原因未查） | <span class="at-chip at-ev-tested">實測</span>[^tikv-h-metrics-dump] |

## 叢集 {#cluster}

| 問題 | 訊號 | 證據 |
|---|---|---|
| 抓得到 PD leader 嗎 | `absent(service_member_role{service="PD"} == 1)`；> 1 是兩個 leader | <span class="at-chip at-ev-tested">實測</span>[^pd-service-role-1] |
| PD 還有過半嗎 | `/readyz` 成功數 < floor(預期 / 2) + 1；預期數寫在規則檔的 `tidb-params`（內嵌 etcd 3.5.15 沒有 `etcd_network_known_peers`） | <span class="at-chip at-ev-tested">實測</span>[^tidb-exp-names] |
| 每種元件的台數 | PD、TiDB 對 `tidb-params`；TiKV 對 PD 記得的 store 數（不必寫死） | <span class="at-chip at-ev-tested">實測</span> |
| PD leader 換太多次 | 內嵌 etcd 的 `leader_changes_seen_total` 15 分鐘 ≥ 3（例子）；WAL fsync p99 > 1 秒 | <span class="at-chip at-ev-tested">實測</span> |
| region 少了副本嗎 | `pd_regions_status` 的 down-peer、miss-peer、offline-peer（連字號） | <span class="at-chip at-ev-tested">實測</span> |

**為什麼用 `service_member_role` 而不是 `etcd_server_is_leader`。**PD leader 用 etcd 的 lease 選出，和 etcd 的 raft leader 是兩件事；PD 會依 priority 把 etcd leader 搬過來，但兩者可以暫時不同。叢集 metric 是 PD leader 的 schedulingController 算的，`service_member_role{service="PD"}` 就是它。lab 裡兩者一直在同一台。<span class="at-chip at-ev-code">讀碼</span>[^pd-etcd-leader-priority][^pd-sched-metrics-collect][^pd-service-role-1]

**停一台 TiKV 時的 region。**3 台、`max-replicas` 3、`max-peer-down-duration` 40 秒（lab）：+142 秒 down-peer 5、pending-peer 5、undersized 5、miss-peer 0。只有 3 台時 PD 沒有地方補副本，miss-peer 不會出現；`pd_checker_event_count` 的 replica_checker 在 5 分鐘內沒有增加。TiKV 回來後全部歸 0。<span class="at-chip at-ev-tested">實測</span>[^tidb-exp-raw]

**停 PD leader。**+5 秒的寫入花 8.71 秒（主機時鐘）成功，+20 秒的寫入 0.18 秒；+29 秒新 leader 是 pd3，`service_member_role` 與 `etcd_server_is_leader` 都在 pd3。<span class="at-chip at-ev-tested">實測</span>[^tidb-exp-raw]

## 可用性 {#availability}

| 問題 | 訊號 | 證據 |
|---|---|---|
| store 狀態 | `pd_cluster_status{type="store_disconnected_count"｜"store_unhealth_count"｜"store_down_count"｜"store_low_space_count"}`，每個 store 一條 | <span class="at-chip at-ev-tested">實測</span>[^pd-store-status-order] |
| TSO | TiDB 端 `pd_client_cmd_handle_failed_cmds_duration_seconds_count{type="tso"}` 的 rate > 0 | <span class="at-chip at-ev-tested">實測</span>：PD 失去 quorum 時每秒 3.6 次[^pdc-m-failed-tso] |

store 狀態只有 PD leader 輸出；PD 停兩台時這組 metric 整組不見，由 TiDBPDLeaderMetricsMissing 補位。`max-store-down-time` 預設 30 分鐘：Down 之後 PD 才開始在別處補副本，所以 Disconnected 與 Unhealthy 階段就要告警。

## 複寫 {#replication}

TiKV 的複寫是每個 Region 一組 Raft（見[共識架構](../architecture/db-raft.md#tikv)）：

| 問題 | 訊號 | 證據 |
|---|---|---|
| follower 落後嗎 | `pending-peer-region-count` > 100（官方）；`tikv_raftstore_log_lag` p99 > 5000（官方；lazy_static） | <span class="at-chip at-ev-tested">實測</span>：TiKV 停掉期間 pending-peer 5 |

## 資源與飽和 {#saturation}

| 問題 | 訊號（門檻） | 證據 |
|---|---|---|
| raft append／commit／apply | p99 > 1 秒；apply 執行緒 CPU > 0.9（`=~`）；raftstore 執行緒 80%（例子） | <span class="at-chip at-ev-tested">實測</span>（matcher）／<span class="at-chip at-ev-code">讀碼</span>[^tikv-m-append][^tikv-m-apply] |
| write stall | `tikv_engine_write_stall{type="write_stall_max"}` > 0 | <span class="at-chip at-ev-code">讀碼</span>[^tikv-m-write-stall] |
| ServerIsBusy | TiDB 端 `tidb_tikvclient_region_err_total{type="server_is_busy"}`；TiKV latch 等待 p99 > 1 秒 | <span class="at-chip at-ev-code">讀碼</span>[^tikv-sched-too-busy][^cg-server-busy-label] |
| 記憶體 | RSS / `tikv_server_memory_quota_bytes`（或 `tidb_server_memory_quota_bytes`）> 0.8；TiDB 超過 `tidb_server_memory_limit` 時取消最耗記憶體的 SQL，只寫 log | <span class="at-chip at-ev-tested">實測</span>（TiKV 映像）／<span class="at-chip at-ev-code">讀碼</span>[^tidb-memlimit-kill] |
| TSO 等待 | `pd_client_cmd_handle_cmds_duration_seconds{type="wait"}` p99 > 0.1 秒（例子） | <span class="at-chip at-ev-tested">實測</span>：PD 恢復後 pending[^pdc-m-wait] |
| compaction 待處理量 | `tikv_engine_pending_compaction_bytes{db="kv"}` / `tikv_config_rocksdb{name="soft_pending_compaction_bytes_limit"}` > 0.5（例子，TiKVPendingCompactionNearSoftLimit）；flow control 開著時 CF 的值不超過 flow control 的 soft limit | <span class="at-chip at-ev-code">讀碼</span>[^res-tikv-pending-compaction][^res-tikv-soft-pending-clamp] |
| CPU quota | `rate(process_cpu_seconds_total[5m])` / `tikv_server_cpu_cores_quota` > 0.8（官方門檻，TiKVCPUNearQuota）；TiKV 的 CPU counter 是整數秒 | <span class="at-chip at-ev-code">讀碼</span>[^res-tikv-cpu-quota-cgroup][^res-tikv-process-cpu-int][^res-tikv-rule-cpu-quota] |
| FSM mailbox 滿 | `rate(tikv_channel_full_total[10m])` > 0（同官方，TiKVChannelFull）；第一次發生前 metric 不存在 | <span class="at-chip at-ev-code">讀碼</span>[^res-tikv-channel-full-normal][^res-tikv-rule-channel-full] |

**版本。**記憶體 quota metric（`tidb_server_memory_quota_bytes`、`tikv_server_memory_quota_bytes`）8.1 才有，TiDBComponentMemoryNearQuota 在 7.5 回傳空；7.5 只有 CPU quota，記憶體改對 cgroup 上限[^ver-ti-tidb81-memquota][^ver-ti-tikv81-memquota][^ver-ti-tikv75-cpuquota]。

**慢磁碟、write stall、熱點沒有實驗。**這台機器做不了輕量的慢磁碟或寫入壓力；這一節的門檻取自官方規則。熱點 region（PD 的 `pd_hotspot_*`）沒有做成規則，用 ServerIsBusy 與 raftstore 延遲當下游訊號。

## 工作量 {#workload}

| 問題 | 訊號 | 證據 |
|---|---|---|
| 錯誤比例 | `tidb_server_query_total{result="Error"}` 占比 > 5%（例子）；種類看 `tidb_server_execute_error_total{type}`（RFC code） | <span class="at-chip at-ev-tested">實測</span>[^tidb-m-execute-error-label] |
| 延遲 | `tidb_server_handle_query_duration_seconds` p99 > 1 秒（官方） | <span class="at-chip at-ev-tested">實測</span> |
| 慢查詢 | `tidb_server_slow_query_process_duration_seconds_count` 的增加量；只記超過 `tidb_slow_log_threshold`（300ms） | <span class="at-chip at-ev-code">讀碼</span>[^tidb-m-slow-query] |
| 連線與 token | `tidb_server_tokens` > 900（`token-limit` 1000）；`tidb_server_connections` | <span class="at-chip at-ev-code">讀碼</span>[^tidb-cfg-token-limit] |
| 衝突與 backoff | `tidb_tikvclient_backoff_seconds_count` 依 type；write conflict（`kv:9007`） | <span class="at-chip at-ev-tested">實測</span>：PD 失去 quorum 時 pdRPC 2 分鐘 394 次[^cg-m-backoff] |
| panic、critical error | `tidb_server_panic_total`、`tidb_server_critical_error_total`、`tikv_critical_error_total`（第一次發生才出現） | <span class="at-chip at-ev-tested">實測</span> |

哪一條 SQL 造成負載，`/metrics` 沒有；要查 statements_summary、slow query、Top SQL（見 [models](models.md#tidb)）。

## 資料維護 {#maintenance}

| 問題 | 訊號 | 證據 |
|---|---|---|
| GC safe point 前進嗎 | `time() − pd_gc_gc_safepoint / 2^18 / 1000` > 3 小時（例子），或這條 gauge 不存在；執行中交易 > 1 小時；官方 TiKV_GC_can_not_work | <span class="at-chip at-ev-code">讀碼</span>[^pd-gc-safepoint][^tidb-m-ongoing-txn][^tikv-rule-gc] |
| DDL、統計 | `tidb_ddl_waiting_jobs` > 5；auto analyze 失敗（官方） | <span class="at-chip at-ev-upstream">上游</span> |

`pd_gc_gc_safepoint` 要等 PD 第一次收到 safe point 才出現（GC 預設每 10 分鐘一次）；lab 約 11 分鐘內沒有出現，TiDBGcSafePointStale 一直 pending（for 1h，沒有觸發）。safe point 的值是 TSO：右移 18 位是毫秒。`tidb_gc_life_time` 預設 10 分鐘。<span class="at-chip at-ev-tested">實測</span>[^tidb-exp-raw][^tidb-gc-lifetime-default]

## 資料保護 {#protection}

log backup（PITR）的 checkpoint 是 `tidb_log_backup_last_checkpoint{task}`（TSO），落後超過 10 分鐘（例子）告警；只在有 task 時存在。<span class="at-chip at-ev-code">讀碼</span>[^tidb-logbackup-checkpoint]全量 BR 備份是外部工具，沒有常駐的 metric：備份成功後寫 textfile metric 給 node_exporter，並定期還原演練。lab 沒有做備份。

## 容量 {#capacity}

| 問題 | 訊號 | 證據 |
|---|---|---|
| 每台 TiKV | `tikv_store_size_bytes{type="available"} / capacity` < 0.2（官方） | <span class="at-chip at-ev-upstream">上游</span>[^tikv-m-store-size] |
| 整個叢集 | PD 的 `storage_size / storage_capacity` > 0.8（官方） | <span class="at-chip at-ev-upstream">上游</span> |

## 盲點與補法 {#blind-spots}

| 盲點 | 類別 | 補法 |
|---|---|---|
| SQL 層是否真的能執行交易 | 資料庫本身沒提供 | 外部 SQL probe 定期讀寫測試表（`select 1` 不夠，lab：PD 失去 quorum 時仍成功） |
| PD leader 抓不到時的叢集狀態 | 元件有、`/metrics` 只有 leader 輸出 | TiDBPDLeaderMetricsMissing；pd-ctl 或 `/pd/api/v1/stores` |
| 全量備份新鮮度 | 要靠其他元件 | 備份工作寫 textfile metric；定期還原 |
| 哪一條 SQL 造成負載 | 元件有、`/metrics` 沒輸出 | statements_summary 經 query-exporter 彙總（未實測） |
| 熱點 region | 元件有、沒做成規則 | ServerIsBusy、raftstore 延遲；PD hot region API |
| TiFlash、TiCDC、TiProxy | 要靠其他元件 | 不在這次範圍；至少加上它們的 `up` 與 absent。TiCDC 停住的 changefeed 也會擋住 GC safe point |
| TLS 憑證到期 | 資料庫本身沒提供 | blackbox 的 `probe_ssl_earliest_cert_expiry` |
| 時鐘偏移 | 要靠其他元件（資料庫只有 PD leader 的 `system_time_slow`） | node_exporter 的 `node_timex_offset_seconds`；已由 [主機與容器資源](../components/host.md) 補上（規則只讀碼，host lab 之後排程） |

## 實驗 {#lab}

`lab/targets/tidb`：TiDB v8.5.8（PD ×3、TiKV ×3、TiDB ×1）、Prometheus v3.13.5（scrape 5 秒）、blackbox_exporter v0.27.0（timeout 4 秒），載入 `rules/targets/tidb.yml` 與官方三份規則。PD `max-store-down-time` 1 分鐘、TiKV `max-peer-down-duration` 40 秒（為了在 lab 時間內看到 Down 與 down peer）；TiKV binary 去掉 debug info 才放得進這台機器。`./run.sh` 約 13 分鐘，結束時 `docker compose down -v`（殘留容器 0）。原始輸出 `results/run-20261110.txt`，摘要 `results/summary.md`。[^tidb-exp]
