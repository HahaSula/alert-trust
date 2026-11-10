# 分散式 SQL 架構

這頁只寫**監控訊號或盲點依賴的分散式 SQL 機制**。每一節依序是機制、什麼會壞、哪個訊號看得到。目前只有 TiDB（v8.5.8：TiDB server、TiKV、PD）；YugabyteDB（YSQL／YCQL、yb-master、yb-tserver）之後在每一節加一段。訊號連到 [TiDB 元件卡](../components/tidb.md)，完整清單在 [TiDB 怎麼監控](../targets/tidb.md)；Region 層的 Raft 在[共識（Raft）架構](db-raft.md#tikv)。

以原始碼與 `lab/targets/tidb` 的實驗為準。<span class="at-chip at-ev-code">讀碼</span> <span class="at-chip at-ev-tested">實測</span>

## 先講結論

1. **每一層各自有「活著但不能服務」的狀態。**SQL 層的 `/status` 不看 PD 與 TiKV；TiKV 的 `/status` 永遠回空的 200；PD 的 `/pd/api/v1/health` 永遠回 200。三者的 `up` 都只代表 HTTP。[^tidb-h-status-health][^tikv-h-status][^pd-h-health]
2. **叢集狀態只有一個地方算：PD leader。**store 與 region 的健康數字由 PD leader 每 10 秒算一次；follower 沒有 `pd_cluster_status`，`pd_regions_status` 是 0。PD leader 抓不到時，叢集狀態整組看不到，而不是「變成 0」。[^pd-sched-metrics-collect][^pd-metrics-interval]
3. **TSO 是所有交易的單一入口。**每筆交易開始與提交都向 PD leader 拿時間戳；PD 失去 quorum 時 SQL 層照常接受連線，交易在等 TSO 後失敗。[^pdc-m-wait][^pdc-m-failed-tso]
4. **儲存層的故障單位是 Region。**一台 TiKV 停掉，壞的是「那台上有副本的每個 Region」；PD 用 store 狀態與 region 健康計數兩個角度描述同一件事。[^pd-store-status-order][^pd-region-down-label]
5. **舊版本要靠 GC safe point 才清得掉。**safe point 被長交易或其他工具的 service safe point 擋住時，資料一直累積、讀取變慢，沒有任何東西「壞掉」。[^pd-gc-safepoint][^tidb-gc-lifetime-default]

## 對照 {#compare}

| 機制 | TiDB | 什麼會壞 | 看得到的訊號 | 本頁 |
|---|---|---|---|---|
| SQL 層無狀態 | TiDB server 只有連線與快取；schema 以 lease 同步 | 連得上、交易失敗；schema lease 過期 | health-second-path、tso-available、query-errors | [SQL 層](#sql-layer) |
| 儲存層分片 | TiKV 把資料切成 Region，每個 Region 一組 Raft | store 停機、副本不足、follower 落後 | store-state、region-health、replication-lag | [儲存層](#storage-layer) |
| 中央排程 | PD leader 收 heartbeat、算叢集狀態、下排程 | PD leader 抓不到時叢集狀態看不到 | role-detection、pd-quorum、component-count | [PD](#pd) |
| 時間戳 | PD leader 發 TSO | PD 換 leader 或失去 quorum 時交易停 | tso-available、tso-wait、leader-changes | [TSO](#tso) |
| 熱點 | 寫入集中在少數 Region | 單台 TiKV 忙、ServerIsBusy | server-busy、raftstore-latency | [熱點](#hotspot) |
| MVCC 與 GC | 舊版本保留到 safe point 之後才清 | safe point 不前進 | gc-progress | [GC](#gc) |

## SQL 層無狀態 {#sql-layer}

**機制。**TiDB server 不存資料，連線進來後把 SQL 轉成對 TiKV 的 KV 或 coprocessor 請求。表結構（schema）放在 TiKV，透過 PD 內嵌的 etcd 通知變更；每台 TiDB 以 lease（預設 45 秒）確認自己的 schema 還是新的，每次確認時把到期時間寫進 `tidb_domain_lease_expire_time`。[^tidb-lease-expire-set][^tidb-lease-expired-check]

**什麼會壞。**`/status` 只看一個 health 旗標：listener 啟動後設成 true，開始關機時設成 false，其他時候不變。[^tidb-h-status-health][^tidb-srv-health-true][^tidb-srv-health-false]所以 PD 或 TiKV 不能用時 `/status` 仍是 200（lab 實測，見[TiDB 頁](../targets/tidb.md#health-second-path)）。

**哪個訊號看得到。**[health-second-path](../components/tidb.md#health-second-path)（`/status` 200 而 schema lease 過期）、[tso-available](../components/tidb.md#tso-available)。

## 儲存層：Region 與 store {#storage-layer}

**機制。**TiKV 把 key 範圍切成 Region，每個 Region 預設三個副本，各自是一組 Raft（見[共識架構](db-raft.md#tikv)）。每台 TiKV 是一個 store：store 每 10 秒向 PD 送一次 heartbeat，每個 Region 的 leader 另外送 region heartbeat（含 down peer 與 pending peer）。

**PD 怎麼判定 store 狀態**（依序，第一個成立的算）：[^pd-store-status-order][^pd-store-disconnect][^pd-store-unhealthy][^pd-store-down-default]

| 最後一次 heartbeat 距今 | 狀態 | `pd_cluster_status{type=...}` |
|---|---|---|
| ≥ `max-store-down-time`（預設 30 分鐘） | Down；PD 開始在別處補副本 | `store_down_count` |
| > 10 分鐘 | Unhealthy | `store_unhealth_count` |
| > 20 秒 | Disconnected | `store_disconnected_count` |
| 回報自己慢 | Slow | `store_slow_count` |
| 其他 | Up | `store_up_count` |

Offline（下線中）與 Tombstone（已移除）是另一個維度，有各自的計數。

**Region 健康。**Region leader 回報副本超過 `max-peer-down-duration`（預設 10 分鐘）沒回應時算 down peer；follower 的 raft log 跟不上算 pending peer；副本數少於設定是 miss peer。[^tikv-max-peer-down][^pd-region-miss-label][^pd-region-down-label]

**哪個訊號看得到。**[store-state](../components/tidb.md#store-state)、[region-health](../components/tidb.md#region-health)、[replication-lag](../components/tidb.md#replication-lag)。

## PD：中央排程與叢集狀態 {#pd}

**機制。**PD 是三台（或五台）組成的叢集，內嵌 etcd（v3.5.15）存 metadata；PD 自己的 leader 用 etcd 的 lease 選出，和 etcd 的 raft leader 是兩件事，PD 會依 priority 把 etcd leader 搬到 PD leader 那台。[^pd-etcd-version][^pd-etcd-leader-priority]只有 PD leader 跑排程與統計：每 10 秒算一次 store 與 region 的狀態寫進 metric，失去 leader 時把這些 metric reset。[^pd-sched-metrics-collect][^pd-sched-metrics-reset][^pd-metrics-interval]成為 leader 時 `service_member_role{service="PD"}` 設 1。[^pd-service-role-1]

**什麼會壞。**

- PD leader 那台抓不到：叢集狀態整組看不到，follower 的 `pd_regions_status` 是 0。官方規則用 `etcd_server_is_leader` 當前提，這時全部安靜（lab 實測）。
- PD 失去 quorum：存活那台照常回應 `/metrics`；`/pd/api/v1/health` 用非 linearizable 的成員清單、對每台打一個空的 `/ping`，所以仍回 200。[^pd-memberlist-nonlinear][^pd-checkhealth-url][^pd-h-ping]要探內嵌 etcd 的 `/readyz`（linearizable read）。[^pd-etcd-readyz-linear]

**哪個訊號看得到。**[role-detection](../components/tidb.md#role-detection)（含「沒有任何一台以 leader 身分被抓到」）、[pd-quorum](../components/tidb.md#pd-quorum)、[component-count](../components/tidb.md#component-count)。

## TSO：全域時間戳 {#tso}

**機制。**每筆交易開始時拿 start_ts、提交時拿 commit_ts，都由 PD leader 發出。TiDB 的 PD client 把同時的請求批次送出；`pd_client_cmd_handle_cmds_duration_seconds{type="wait"}` 是等結果的時間，失敗計入 `pd_client_cmd_handle_failed_cmds_duration_seconds{type="tso"}`。[^pdc-m-cmd-duration][^pdc-m-wait][^pdc-m-failed-tso]

**什麼會壞。**PD 換 leader 時 TSO 中斷到新 leader 就緒；PD 失去 quorum 時 TSO 一直拿不到。PD leader 的系統時間往回跳時會記 `pd_tso_events{type="system_time_slow"}`（這台 WSL2 機器的牆鐘會往回跳，lab 一直有這個事件）。

**哪個訊號看得到。**[tso-available](../components/tidb.md#tso-available)、[tso-wait](../components/tidb.md#tso-wait)、[leader-changes](../components/tidb.md#leader-changes)。

## 熱點 {#hotspot}

**機制。**連續遞增的主鍵把寫入集中在最後一個 Region，那個 Region 的 leader 所在的 TiKV 承受全部寫入。TiKV 的 scheduler 待寫入量超過門檻時回 `SchedTooBusy`，TiDB 端看到的是 ServerIsBusy region error 與 backoff。[^tikv-sched-too-busy][^cg-server-busy-label]

**哪個訊號看得到。**[server-busy](../components/tidb.md#server-busy)（TiDB 端的 `tidb_tikvclient_region_err_total{type="server_is_busy"}`）、[raftstore-latency](../components/tidb.md#raftstore-latency)。PD 的 hot region 統計（`pd_hotspot_*`）沒有做成規則，見盲點。

## MVCC 與 GC safe point {#gc}

**機制。**TiKV 每次寫入都留一個新版本；TiDB 的 GC leader 每 `tidb_gc_run_interval`（預設 10 分鐘）把 safe point 推到「現在 − `tidb_gc_life_time`（預設 10 分鐘）」與最舊的執行中交易、各工具 service safe point 之中最小的那個，PD 收到時寫 `pd_gc_gc_safepoint`。[^tidb-gc-lifetime-default][^tidb-gc-run-job][^pd-gc-safepoint]

**什麼會壞。**一個沒有結束的交易、停掉但沒移除的 TiCDC changefeed 或 BR 任務，都會讓 safe point 停住。資料一直累積，讀取要跳過越來越多舊版本，沒有錯誤。

**哪個訊號看得到。**[gc-progress](../components/tidb.md#gc-progress)（safe point 多久沒前進、長交易、官方 TiKV_GC_can_not_work）。

## YugabyteDB：YSQL／YCQL、yb-master、tablet {#yugabytedb}

YugabyteDB v2026.1.3.0 的同一組機制。以原始碼與 `lab/targets/yugabytedb`（master ×3、tserver ×3，RF=3）為準；完整清單在 [YugabyteDB 怎麼監控](../targets/yugabytedb.md)，訊號連到 [YugabyteDB 元件卡](../components/yugabytedb.md)。

| 機制 | TiDB | YugabyteDB | 對監控的差別 |
|---|---|---|---|
| SQL 層 | 獨立的 TiDB server，無狀態 | YSQL（PostgreSQL 程序）與 YCQL 跑在每台 yb-tserver 裡，和儲存同一台 | 沒有「SQL 層活著、儲存層死了」的分開部署；但 YSQL 有自己的 port（5433）與 metric port（13000），會各自失敗 |
| 儲存層分片 | Region，每個一組 Raft | tablet，每個一組 Raft（DocDB，底層 RocksDB） | 故障單位都是「那台上有副本的每個分片」 |
| 中央角色 | PD（內嵌 etcd）：排程、TSO、store 狀態 | yb-master：catalog、tablet 位置、balancer；自己一組 Raft（sys catalog tablet） | 兩者的叢集狀態都只在 leader 上；master 不發時間戳 |
| 時間戳 | PD leader 發 TSO，所有交易經過它 | 每台自己的 hybrid clock（實體時鐘 + 邏輯計數），沒有中央 | 改成依賴時鐘同步；但 YSQL 仍依賴 master leader 續約的 operation lease，master 失去 quorum 約 30 秒後 YSQL 全停（lab） |
| 健康判定 | PD 依 store heartbeat 分 Disconnected（20 秒）／Down（30 分鐘） | master 依 tserver heartbeat 分 live／dead（`tserver_unresponsive_timeout_ms` 60 秒）；tablet leader 另外在 `follower_unavailable_considered_failed_sec`（900 秒）後把 follower 踢出 Raft 設定 | YugabyteDB 有兩個獨立的時間：master 的看法與 tablet Raft 的看法 |

**SQL 層。**YSQL 是 PostgreSQL 程序，連線上限 `ysql_max_connections` 是每台 tserver 各自的。`/status` 不看任何狀態，一律回 `{}`。[^yb-h-status][^yb-ysql-maxconn]PostgreSQL 的監控能沿用多少，見 [YugabyteDB 頁的對照](../targets/yugabytedb.md#postgres)。

**master 與 PD。**兩者都是「只有 leader 在算叢集狀態」：master leader 的背景執行緒寫 `num_tablet_servers_live`、`num_tablet_servers_dead`、`is_load_balancing_enabled`，follower 一直是 0；失去 leader 時歸 0。[^yb-m-bg-report][^yb-m-report-metrics][^yb-m-bg-reset]lab：follower 的 `num_tablet_servers_live` 是 0、leader 是 3。所以和 TiDB 一樣，規則要先挑出 leader 那台（`is_raft_leader{table_name="sys.catalog"} == 1`），並在「抓不到 leader」時告警。<span class="at-chip at-ev-tested">實測</span>[^yb-exp-raw]

不同的是 master 的 JSON API：follower 會把 `/api/v1/health-check` 等請求轉給 leader，失敗時仍回 200、內容是錯誤訊息。[^yb-m-h-leader-or-redirect][^yb-m-h-redirect-nolist]用 blackbox 探這些端點時要比對 body。

**tablet 與 Region。**tablet 的健康不在 `/prometheus-metrics` 裡：leaderless（沒有 valid leader 超過 120 秒）與 under-replicated 只在 master 的 JSON API 算。[^yb-m-h-leaderless][^yb-m-h-underrep-running]TiDB 的 PD 把 down／pending／miss peer 做成 `pd_regions_status`，YugabyteDB 沒有對應的 metric；YBA 的告警範本用的 `yb_node_leaderless_tablet_count` 是 YBA 平台自己產生的。[^yba-leaderless]

**時間戳與時鐘。**沒有 TSO，交易時間來自每台的 hybrid clock：收到別台較新的 hybrid time 時把自己的往前推；本機實體時鐘落後最後的 hybrid time 超過 `max_clock_skew_usec`（500ms）時，有交易表的 tserver 直接 FATAL。[^yb-clk-skew-def][^yb-clk-fatal][^yb-clk-enable]lab：牆鐘往回跳 7 秒時三台 tserver 同時 crash。<span class="at-chip at-ev-tested">實測</span>[^yb-exp-clock]機制見[共識架構的 YugabyteDB 段](db-raft.md#yugabytedb)。

**哪個訊號看得到。**[role-detection](../components/yugabytedb.md#role-detection)、[master-quorum](../components/yugabytedb.md#master-quorum)、[tablet-health](../components/yugabytedb.md#tablet-health)、[tserver-liveness](../components/yugabytedb.md#tserver-liveness)、[clock-skew](../components/yugabytedb.md#clock-skew)、[ysql-available](../components/yugabytedb.md#ysql-available)。

## 之後加入的資料庫

| 資料庫 | SQL 層 | 儲存與分片 | 中央角色 | 要補的段落 |
|---|---|---|---|---|
| YugabyteDB | YSQL（PostgreSQL 相容）、YCQL | tablet，每個一組 Raft | yb-master（自己一組 Raft） | 已加在 [YugabyteDB](#yugabytedb)；熱點與 MVCC 歷史保留（`timestamp_history_retention_interval_sec`）尚未補 |
