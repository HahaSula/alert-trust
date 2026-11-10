# MongoDB 怎麼監控

**要監控的是：replica set 裡剛好有一台寫得進去的 primary、secondary 跟得上而且 oplog 夠它追、w:majority 的寫入不會無限等。**對文件型資料庫來說，這三件事最常壞在五個地方：選舉中或失去多數時沒有 primary、多數 secondary 卡住讓 majority commit point 停住、oplog 太短（或被釘住而一直長）、WiredTiger cache 逼得請求自己做 eviction、exporter 本身只輸出了一小部分。這頁依九個面向從大到小排列（叢集 → 複寫 → 可用性 → 資源 → 工作量 → 維護 → 保護 → 容量 → 監測自檢），最後是工具設定與缺口。

[元件卡](../components/mongodb_exporter.md)列出每個訊號與規則；[原始碼到 metric](mongodb-appendix.md)追每個欄位；機制見[文件型架構](../architecture/db-doc.md)與[共識架構的 MongoDB 段](../architecture/db-raft.md#mongodb)；6.0、7.0、8.0、8.2 之間的欄位差異見 [MongoDB 各版本的監控差異](versions/mongodb.md)。

版本：MongoDB 8.0.34（目前的 LTS 系列；SSPL）、Percona mongodb_exporter v0.54.0。8.2.12 與 9.0.4（2026-10 釋出）只查了 OTel metrics 的程式碼。依據是原始碼，加上一個 3 member replica set 的小實驗（`wiredTigerCacheSizeGB` 0.25、oplog 2MB），每個 member 一個 exporter，另有預設旗標、沒有 clusterMonitor、`--collect-all`、multi-target 四種 exporter，並把 PMM v3.10.0 的範本與 awesome-prometheus-alerts 的規則照抄進 Prometheus 看會不會響。<span class="at-chip at-ev-tested">實測</span>[^mg-exp] 這台機器的容器單調時鐘比牆鐘慢約 30%：選舉與降級的秒數比真實環境長，只當量級看，機制以原始碼為準。sharded cluster 沒有做實驗。

## 先講結論

1. **mongodb_exporter 預設什麼都不收。**不加旗標時只有 `mongodb_up` 與 go／process metric（lab 46 個 sample）；要 `--collector.diagnosticdata --collector.replicasetstatus --compatible-mode` 才有約 6100 個 sample，PMM 的規則用的舊名 metric 也要 `--compatible-mode`。<span class="at-chip at-ev-tested">實測</span>[^mg-exp][^mx-flag-diag][^mx-flag-compat]
2. **權限不足時 exporter 安靜地只剩 `mongodb_up`。**帳號沒有 clusterMonitor：`up=1`、`mongodb_up=1`、sample 6108 → 49；`getDiagnosticData` 被拒只記 warn，預設 `--log.level=error` 看不到。規則 `MongoDBDiagnosticDataMissing` 用 `mongodb_up == 1 unless mongodb_ss_uptime` 抓。<span class="at-chip at-ev-tested">實測</span>[^mg-exp][^mx-diag-warn][^mx-flag-loglevel]
3. **伺服器忙或卡住時，`mongodb_up` 是消失，不是變 0。**primary 持有 global 鎖 12 秒：exporter 在期限（scrape_timeout 減 1 秒）回應，`up=1`，sample 只剩 44 個，`mongodb_up` 本身也不在；只看 `mongodb_up == 0` 的 MongoDBDown 不響。卡住（pause）的 member 則回 `mongodb_up 0`。<span class="at-chip at-ev-tested">實測</span>[^mg-exp][^mx-describe-ctx][^mx-handler-offset]
4. **沒有 primary 時，延遲 series 整組消失，`mongodb_up` 仍是 1。**3 台停 2 台：存活那台 12 秒後（牆鐘）自己降級成 SECONDARY，寫入回 `NotWritablePrimary`，它的 exporter `up=1`、`mongodb_up=1`，`mongodb_mongod_replset_member_replication_lag` 一條都沒有（前一次執行）。要用「依 cluster 計數 isWritablePrimary」與「SECONDARY 卻沒有延遲數字」兩條規則。<span class="at-chip at-ev-tested">實測</span>[^mg-exp][^mg-exp-a][^mx-compat-gotprimary][^mg-stepdown-majority]
5. **預設 write concern 是 majority、沒有逾時。**兩個 secondary 都卡住時，不指定 write concern 的 insert 在 8 秒後仍沒回應；有 wtimeout 的回 `WriteConcernFailed` 並讓 `getLastError.wtimeouts` 加 1。commit point 落後要從 `lastAppliedWallTime − lastCommittedWallTime` 看（lab 70 秒）。<span class="at-chip at-ev-tested">實測</span>[^mg-exp][^mg-wc-implicit][^mg-wc-notimeout]
6. **上游規則有一半在這個版本上用不了。**PMM 的 tickets 範本「available < 50」在 8.0 的動態 ticket 下一直響（lab 16 核，total 16）；PMM 的 replication lag 在 Prometheus 是語法錯誤（MetricsQL 才能跑）；awesome 的 tickets 用錯 label（`txn_rw_type`，實際是 `txn_rw`），lag 與 headroom 用 `on (set)` 對不到任何 series。<span class="at-chip at-ev-tested">實測</span>[^mg-exp][^pmm-read-tickets][^pmm-lag][^awe-mg-read-tickets][^awe-mg-lag]
7. **`/scrapeall` 兩個 member 卡住，三個 member 一起消失。**它在一個請求裡依序抓每個 URI，每個都給完整的期限（scrape_timeout 減 1 秒）：一台卡住時 3.2 秒還來得及、只少那一台；一台已停、再 pause 一台時整個請求 4 秒逾時、`up=0`。`/scrape?target=` 只有那一台受影響。<span class="at-chip at-ev-tested">實測</span>[^mg-exp][^mg-exp-a][^mx-scrapeall-loop][^mx-scrapeall-timeout]
8. **每個 metric 都帶 member 自己的狀態（`rs_state`）。**角色一換，同一台的所有 series 換一組 label：`changes()` 要先依 cluster 彙總，`mongodb_rs_term` 這類計數要用 `max by (cluster)`。<span class="at-chip at-ev-tested">實測</span>[^mg-exp][^mx-topo-rsstate]

## 監控模型與官方建議 {#models}

MongoDB 8.0 與 8.2 **沒有可以 scrape 的 Prometheus endpoint**。8.2.12 有 OTel metrics 的 OTLP 檔案與 HTTP 推送初始化，9.0.4 再加了 Prometheus 格式的「檔案」輸出（`openTelemetryPrometheusMetricsDirectory`）與約 160 個 metric 名稱，但沒有 replica set、oplog、WiredTiger cache 這些；而且檔案輸出連續失敗達上限（預設 10 次）時 process 會結束。<span class="at-chip at-ev-code">讀碼</span>[^mg90-otel-prom-dir][^mg90-otel-maxfail][^mg90-otel-names] 所以基礎仍是 exporter。

| 模型 | 來源 | 看得到 | 看不到 | 在這頁的角色 |
|---|---|---|---|---|
| Percona mongodb_exporter（v0.54.0） | `getDiagnosticData`、`replSetGetStatus`、`local.oplog.rs`，選配 `$collStats`、`$indexStats`、`dbStats`、`top`、`currentOp`、`system.profile`、PBM[^mx-register-diag][^mx-oplog-find] | serverStatus 全部數字欄位、replica set 每個 member、oplog window、選舉計數 | 逐查詢資料、FTDC 歷史、主機層 | **基礎模型** |
| OTel Collector mongodbreceiver | serverStatus、dbStats 等（metrics beta）[^dbm-otel-mongodb] | 連線、操作數、cache、儲存 | 名稱不同；replica set 狀態較少 | 已有 OTel 管線時的替代 |
| MongoDB 9.0 的 OTel metrics | process 內建，寫檔或推送[^mg90-otel-prom-dir] | 約 160 個 server／process metric | 複寫、oplog、cache；不是 scrape 端點 | 未實測；之後再評估 |
| FTDC（`diagnostic.data`） | 每秒取樣 serverStatus 等，250MB 循環覆寫[^mg-ftdc-period][^mg-ftdc-dir-size] | 事件前後每秒的資料 | 格式不公開 | 事後分析；確認在持久磁碟 |
| 伺服器指令 | `serverStatus`、`rs.status()`、`db.getReplicationInfo()`、`currentOp`、profiler | 一切的原始資料 | 歷史 | 排查時直接看 |
| 廠商平台 | Atlas、Ops Manager、Cloud Manager、PMM | 告警與自動化 | — | 門檻的來源 |

**官方建議監控什麼**（[Atlas 的監控與告警指引](https://www.mongodb.com/docs/atlas/architecture/current/monitoring-alerts/)，2026-11-10 讀取）：

| Atlas 的建議（低優先／高優先） | 對應的訊號 | 這裡的規則 |
|---|---|---|
| Oplog Window < 24h（5 分鐘）／< 1h（10 分鐘） | [oplog-window](../components/mongodb_exporter.md#oplog-window) | `MongoDBOplogWindowShort`（1h） |
| Replication lag > 240 秒／> 1 小時（5 分鐘） | [replication-lag](../components/mongodb_exporter.md#replication-lag) | `MongoDBReplicationLag`（240 秒） |
| No primary 5 分鐘；Primary election 任何一次；Election events > 3／> 30 次一小時 | [role-detection](../components/mongodb_exporter.md#role-detection)、[elections](../components/mongodb_exporter.md#elections) | `MongoDBNoPrimary`（30 秒）、`MongoDBElectionHappened`、`MongoDBFrequentElections`（3 次） |
| Queued Reads／Writes > 0～10／10+ | [tickets](../components/mongodb_exporter.md#tickets) | `MongoDBTicketQueue`（10） |
| Disk partition usage > 90%／> 95% | [disk-usage](../components/mongodb_exporter.md#disk-usage) | `MongoDBDiskSpaceLow`（90%，要 dbstats） |
| Restarts last hour > 2；Host down 15 分鐘 | [restart](../components/mongodb_exporter.md#restart)、[db-up](../components/mongodb_exporter.md#db-up) | `MongoDBRestarted`、`MongoDBDown` |
| Read／Write latency > 20ms／> 50ms | [op-latency](../components/mongodb_exporter.md#op-latency) | `MongoDBSlowOperations`（100ms，例子） |
| Page faults、IOPS、swap、CPU steal | 主機層 | node_exporter（[盲點](#blind-spots)） |

**為什麼以 mongodb_exporter 為基礎。**它是唯一同時涵蓋 serverStatus、replica set 每個 member 的看法、oplog window 的 Prometheus 工具，PMM 的告警範本也建立在它上面；缺點是名稱依 BSON 路徑自動產生、預設不收、失敗時不報錯，這頁的監測自檢面向就是在補這三件事。<span class="at-chip at-ev-code">讀碼</span>[^mx-prefix-ss][^mx-general-always]

### 九個面向一覽 {#aspects}

| 面向 | 這頁的重點 | 元件卡的訊號 | 證據 |
|---|---|---|---|
| [叢集與共識](#cluster) | member 健康、選舉、sharding（只讀碼） | member-health、elections、sharding-balancer | <span class="at-chip at-ev-tested">實測</span>／<span class="at-chip at-ev-code">讀碼</span> |
| [複寫與 HA](#replication) | 角色、延遲、oplog window、commit point | role-detection、replication-lag、oplog-window、majority-commit-lag | <span class="at-chip at-ev-tested">實測</span> |
| [可用性](#availability) | 連得上、重啟、w:majority 等待 | db-up、restart、majority-writes | <span class="at-chip at-ev-tested">實測</span> |
| [資源與飽和](#saturation) | cache、ticket、連線 | wt-cache、tickets、connections | <span class="at-chip at-ev-code">讀碼</span>／<span class="at-chip at-ev-tested">實測</span> |
| [工作量](#workload) | 延遲、cursor、assert | op-latency、cursors、asserts | <span class="at-chip at-ev-inferred">推論</span>／<span class="at-chip at-ev-code">讀碼</span> |
| [資料維護](#maintenance) | TTL monitor | ttl-monitor | <span class="at-chip at-ev-inferred">推論</span> |
| [資料保護](#protection) | checkpoint、備份、FTDC | checkpoint | <span class="at-chip at-ev-code">讀碼</span> |
| [容量](#capacity) | oplog 大小、磁碟 | oplog-growth、disk-usage | <span class="at-chip at-ev-tested">實測</span> |
| [監測自檢](#exporter) | 旗標、權限、期限、multi-target、名稱與 label | exporter-up、collectors-present、collector-success、scrape-budget、exporter-query-timeout、probe-blast-radius、version-semantics | <span class="at-chip at-ev-tested">實測</span> |

## 叢集與共識 {#cluster}

**結論：每個 exporter 都報整個 replica set 的看法；停掉的 member 自己的 exporter 只剩 `mongodb_up=0`，它的健康要從其他 member 看。**

| 要看什麼 | MongoDB 的來源 | metric | 證據 |
|---|---|---|---|
| member 在別人眼中健康嗎 | `replSetGetStatus.members[].health`；超過 `electionTimeoutMillis` 沒消息就是 0、state 8、stateStr「(not reachable/healthy)」[^mg-rs-health][^mg-member-timeout][^mg-rs-notreachable] | `mongodb_rs_members_health{member_idx, member_state}` | <span class="at-chip at-ev-tested">實測</span> |
| member 卡在 RECOVERING、STARTUP2、ROLLBACK | `members[].state` | `mongodb_rs_members_state` | <span class="at-chip at-ev-code">讀碼</span> |
| 選舉 | 每次選舉 term + 1[^mg-term-increment] | `mongodb_rs_term`（`max by (cluster)`）、`mongodb_ss_electionMetrics_*_called` | <span class="at-chip at-ev-tested">實測</span> |
| sharding | mongos 上 `config.changelog`、balancer 設定[^mx-changelog][^mx-balancer-enabled] | `mongodb_mongos_sharding_changelog_10min_total{event}` | <span class="at-chip at-ev-code">讀碼</span> |

實驗：

- **kill primary：**另外兩台的 `mongodb_rs_members_health{member_idx="<被 kill 的>"}` 是 0、`member_state="(not reachable/healthy)"`；被 kill 那台的 exporter `up=1`、`mongodb_up=0`、只剩 46 個 sample。`max by (cluster) (mongodb_rs_term)` 2 → 3。<span class="at-chip at-ev-tested">實測</span>[^mg-exp]
- **選舉規則要先彙總。**角色一換，每個 metric 的 `rs_state` label 跟著換：awesome 的 `changes(mongodb_rs_myState[10m]) > 0 and mongodb_rs_myState == 1` 在整次實驗（3 次選舉）從沒響過，因為每條 series 本身沒變。`max by (cluster) (mongodb_rs_term) − min by (cluster) (min_over_time(...[15m]))` 與 PMM 的 `changes(mongodb_mongod_replset_my_state)`（compatible mode 的 metric 沒有 `rs_state`）都看得到。<span class="at-chip at-ev-tested">實測</span>[^mg-exp][^awe-mg-primarychanged][^pmm-primary-changed]
- **鎖住 primary 也會造成選舉。**primary 持有 global X 鎖 12 秒（`sleep` 測試指令）後約 25 秒，m2 發起選舉、m1 log「Stepping down from primary, because a new term has begun」。原因推測是 heartbeat 在鎖期間回不來；log 沒有保留，未確認。<span class="at-chip at-ev-tested">實測</span>[^mg-exp][^mg-sleep-global]
- **sharding** 沒有實驗。exporter 只在連到 mongos 時輸出 `mongodb_mongos_*`，要另外部署（見[盲點](#blind-spots)）。<span class="at-chip at-ev-code">讀碼</span>[^mx-mongos-compat]

## 複寫與 HA {#replication}

| 要看什麼 | MongoDB 的來源 | exporter metric | 沒有 primary 時 | 證據 |
|---|---|---|---|---|
| 誰是 primary | `serverStatus.repl.isWritablePrimary`[^mg-writable-primary] | `mongodb_ss_repl_isWritablePrimary`、`mongodb_rs_myState` | 全部 0 | <span class="at-chip at-ev-tested">實測</span> |
| 延遲（秒） | `members[].optimeDate`（秒為單位，取自 heartbeat）[^mg-rs-optimedate] | `mongodb_mongod_replset_member_replication_lag{self="1"}`（compatible mode） | **整組消失**[^mx-compat-gotprimary] | <span class="at-chip at-ev-tested">實測</span> |
| 延遲（不用 compatible mode） | 同上 | `mongodb_rs_members_optimeDate`（毫秒）在同一個 instance 內相減 | 只剩 secondary 的 | <span class="at-chip at-ev-tested">實測</span> |
| oplog window | `local.oplog.rs` 第一筆與最後一筆 | `mongodb_mongod_replset_oplog_head_timestamp − ..._tail_timestamp` | 照常 | <span class="at-chip at-ev-tested">實測</span> |
| majority commit point | `optimes.lastCommittedWallTime`[^mg-rs-lastcommitted] | `mongodb_rs_optimes_lastAppliedWallTime − mongodb_rs_optimes_lastCommittedWallTime`（毫秒） | 照常 | <span class="at-chip at-ev-tested">實測</span> |
| flow control | `serverStatus.flowControl`[^mg-fc-ss-islagged] | `mongodb_ss_flowControl_isLagged`、`..._targetRateLimit` | 不作用（只在 primary） | <span class="at-chip at-ev-tested">實測</span> |

- **延遲是每個 member 各自的看法。**m3 `fsyncLock` 35 秒：m3 自報 26、primary 看 31、m2 看 35；用 `self="1"` 取自報的那條，或取 `max`。閒置時 primary 每 10 秒寫一筆 noop，延遲在 0 與 10 之間跳，門檻不要設在 10 秒以下。<span class="at-chip at-ev-tested">實測</span>[^mg-exp][^mg-noop-interval][^mx-compat-self]
- **沒有 primary 時延遲消失。**exporter 只在 replSetGetStatus 裡找得到 PRIMARY 時才算；DOWN 的 member optime 被清成 0，它的延遲也不輸出。kill 兩個 secondary 後延遲 series 0 條（前一次執行）；`MongoDBReplicationLagUnknown`（`rs_myState == 2 unless` 延遲）在這時 pending。<span class="at-chip at-ev-tested">實測</span>[^mg-exp-a][^mg-exp][^mx-compat-lag][^mg-set-down-optime]
- **awesome 的 lag 與 headroom 永遠空。**兩條都用 `on (set)` 對 `mongodb_rs_members_optimeDate`，這個 metric 的 label 是 `rs_nm`、`member_idx`、`member_state`，沒有 `set`。PMM 的 lag 範本寫成 `max(<m>[1m]) by (...)`，promtool 拒絕（PMM 的 TSDB 是 VictoriaMetrics，MetricsQL 會自動補 rollup）。<span class="at-chip at-ev-tested">實測</span>[^mg-exp][^awe-mg-lag][^awe-mg-headroom][^pmm-lag][^pmm-vm]
- **oplog window。**2MB 的 oplog 寫 10MB 後 window 95 秒；PMM 的「< 24h」與我們的「< 1h」在新叢集上一定成立，所以規則加了 uptime 條件。<span class="at-chip at-ev-tested">實測</span>[^mg-exp][^pmm-oplog]
- **majority commit point。**m2、m3 都 `fsyncLock`：primary 的 `lastAppliedWallTime − lastCommittedWallTime` 70 秒（secondary 上是 0，所以規則只在 `isWritablePrimary == 1` 上算）；flow control `isLagged` 1、`targetRateLimit` 降到下限 100，每秒 20 筆的 w:1 寫入沒有被擋。只鎖一台時 commit point 跟著另一台走，`isLagged` 0。<span class="at-chip at-ev-tested">實測</span>[^mg-exp][^mg-fc-min-tickets][^mg-fc-target-lag]

## 可用性 {#availability}

- **`mongodb_up` 只代表 ping 成功。**它是一次 ping（primaryPreferred、direct connection），label 只有 `cluster_role`。3 台停 2 台時存活的 SECONDARY `mongodb_up=1`；被 kill 的那台 `mongodb_up=0`（`cluster_role` 是空的）；primary 被 global 鎖住時 `mongodb_up` **不存在**（exporter 期限內沒做到 ping）。所以 MongoDBDown 要配 `MongoDBExporterDown`、`MongoDBScrapeSamplesDropped` 與角色規則。<span class="at-chip at-ev-tested">實測</span>[^mg-exp][^mx-up-ping][^mx-up-desc]
- **寫得進去嗎**由[角色](#replication)回答：kill 兩個 secondary 後 primary 12.3 秒（牆鐘）自己降級，寫入回 `NotWritablePrimary`；`MongoDBNoPrimary`、PMM 與 awesome 的 no primary 都響。kill primary 時 9.5 秒後選出新 primary，`for: 30s` 的 NoPrimary 沒響（Atlas 建議 5 分鐘）。<span class="at-chip at-ev-tested">實測</span>[^mg-exp][^mg-stepdown-majority][^mg-election-timeout]
- **w:majority 會一直等。**沒有 arbiter 時預設 write concern 是 `{w: "majority", wtimeout: 0}`（`getDefaultRWConcern` 的 source 是 implicit）。m2、m3 都卡住時：wtimeout 5000 的寫入回 `WriteConcernFailed`、`mongodb_ss_metrics_getLastError_wtimeouts` 0 → 1；不指定 write concern 的 insert 8 秒（主機時鐘）沒回應，計數不變。第一次執行的背景 writer 就是這樣卡住的。<span class="at-chip at-ev-tested">實測</span>[^mg-exp][^mg-wc-implicit][^mg-wc-notimeout]
- **重啟：**`mongodb_ss_uptime < 300`（PMM 與 awesome 都有）。被 kill 的 member 重新啟動後回來當 SECONDARY，`MongoDBRestarted` 響。<span class="at-chip at-ev-tested">實測</span>[^mg-exp]

## 資源與飽和 {#saturation}

- **WiredTiger cache。**dirty 比例到 20%、用量到 95% 時應用執行緒要自己做 eviction；看 `mongodb_ss_wt_cache_tracked_dirty_bytes_in_the_cache`、`mongodb_ss_wt_cache_bytes_currently_in_the_cache` 對 `mongodb_ss_wt_cache_maximum_bytes_configured`，與 `mongodb_ss_wt_cache_application_thread_time_evicting_usecs` 的速率。cache 預設 (RAM − 1GB) × 50%，容器裡要明確設 `--wiredTigerCacheSizeGB`（lab 0.25GB，maximum_bytes 268435456）。lab 的寫入量太小，沒有重現壓力。<span class="at-chip at-ev-code">讀碼</span>[^wt-eviction-defaults][^mg-wt-cache-default][^wt-stat-app-evict]
- **ticket。**8.0 預設 throughputProbing：初始 ticket 數是 CPU 核心數、在 4～128 間調整。lab（16 核）`mongodb_ss_wt_concurrentTransactions_totalTickets{txn_rw}` 是 16，PMM 的「available < 50」一開機就 firing。要看的是排隊：`mongodb_ss_wt_concurrentTransactions_{read,write}_normalPriority_queueLength`（Atlas 的 Queued Reads／Writes）。<span class="at-chip at-ev-tested">實測</span>[^mg-exp][^mg-ticket-algo][^mg-tp-initial]
  **版本：**`normalPriority` 這層 8.0 才有；7.0 的排隊數直接在 `read`／`write` 下（`..._read_queueLength`，推論），MongoDBTicketQueue 的 regex 兩種都接受；6.0 預設沒有排隊數、ticket 固定 128，只剩 `globalLock_currentQueue`。<span class="at-chip at-ev-code">讀碼</span>[^ver-mg60-semaphore-stats][^ver-mg70-semaphore-stats][^ver-mg80-semaphore-normal][^ver-mg60-ticket-128]
- **連線。**`mongodb_ss_connections{conn_type="current"} / (current + available)`；上限是 maxIncomingConnections 與 ulimit 的較小者（lab available 8168）。exporter 預設每次 scrape 建新連線，額滿時它也連不進去。<span class="at-chip at-ev-code">讀碼</span>[^mg-conn-refused][^mx-new-client]
- **主機層**（swap、IOPS、page fault、CPU steal）不在 serverStatus 裡，用 node_exporter。

## 工作量 {#workload}

- **延遲：**`rate(mongodb_ss_opLatencies_latency) / rate(mongodb_ss_opLatencies_ops)`，label `op_type`，單位微秒。<span class="at-chip at-ev-inferred">推論</span>
- **cursor：**`mongodb_ss_metrics_cursor_open{csr_type="total"}` 與 `mongodb_ss_metrics_cursor_timedOut`（閒置 10 分鐘逾時）。<span class="at-chip at-ev-code">讀碼</span>[^mg-cursor-timeout][^mg-cursor-timedout]
- **assert：**只看 regular／warning／msg。user assert 包含權限不足：lab 那個沒有 clusterMonitor 的 exporter 讓 primary 的 user assert 每分鐘多約 400。<span class="at-chip at-ev-tested">實測</span>[^mg-exp]
- 逐查詢的慢查詢與執行計畫不在 exporter 的預設範圍（[盲點](#blind-spots)）。

## 資料維護 {#maintenance}

TTL monitor 預設每 60 秒一輪（`ttlMonitorSleepSecs`），`mongodb_ss_metrics_ttl_passes` 十分鐘不動代表過期文件不會被刪。只在 primary 上刪，secondary 透過 oplog 複寫。<span class="at-chip at-ev-inferred">推論</span>[^mg-ttl-sleep]

## 資料保護 {#protection}

- **checkpoint：**預設每 60 秒（`syncdelay`），`mongodb_ss_wt_checkpoint_most_recent_time_msecs` 一直很長代表磁碟跟不上；`mongodb_ss_wt_checkpoint_total_failed_number_of_checkpoints` 增加要立刻處理。<span class="at-chip at-ev-code">讀碼</span>[^mg-syncdelay]
  **版本：**6.0、7.0 的 checkpoint 時間在 `transaction` 分類（`mongodb_ss_wt_txn_transaction_checkpoint_most_recent_time_msecs`，推論），MongoDBCheckpointSlow 用 `or` 兩種都接受；失敗次數只在 8.0 以上有，MongoDBCheckpointFailed 在 6.0、7.0 回傳空。<span class="at-chip at-ev-code">讀碼</span>[^ver-mg60-wt-ckpt][^ver-mg70-wt-ckpt][^ver-mg80-wt-ckpt][^ver-mg80-wt-ckpt-failed]
- **備份：**MongoDB 沒有內建排程備份；Percona Backup for MongoDB 要 `--collector.pbm`（PMM 有 PBM 範本），其他工具要自己的 metric。
- **FTDC：**每秒取樣、目錄 250MB 循環覆寫（shard server 在新格式下加倍）。事件後要分析時才拿得到，要確認在持久磁碟。<span class="at-chip at-ev-code">讀碼</span>[^mg-ftdc-dir-size][^mg-ftdc-shard-double]

## 容量 {#capacity}

- **oplog 大小。**截斷是背景執行緒整段刪，條件是超過上限而且最舊那段沒被 stable timestamp（跟著 majority commit point）或 backup 釘住。lab 的 2MB oplog 寫 10MB 後 150 秒內一直是 10.6MB；m2、m3 都卡住時長到 5.4MB、解鎖 20 秒後 6.7MB。這組數據分不出「延後截斷」與「被釘住」，釘住只讀碼；規則 `MongoDBOplogOverMaxSize` 用 1.5 倍加 `for: 5m`。<span class="at-chip at-ev-tested">實測</span>[^mg-exp] <span class="at-chip at-ev-code">讀碼</span>[^mg-oplog-capacity][^mg-oplog-pinned][^mg-pinned-rollback]
- **磁碟。**`mongodb_dbstats_fsUsedSize / mongodb_dbstats_fsTotalSize` 要 `--collector.dbstats`（lab 只有 `--collect-all` 的 exporter 有，每個 database 一份）。沒開就用 node_exporter。<span class="at-chip at-ev-tested">實測</span>[^mg-exp][^mx-flag-dbstats]

## 監測自檢 {#exporter}

| exporter 設定 | sample 數 | `mongodb_up` | 看得到什麼 | 證據 |
|---|---|---|---|---|
| 不加旗標 | 46 | 1 | 只有 `mongodb_up`、go、process | <span class="at-chip at-ev-tested">實測</span>[^mg-exp] |
| 三個旗標，帳號沒有 clusterMonitor | 49 | 1 | 同上，加 `mongodb_version_info`；log 只有 replSetGetStatus 的 ERROR | <span class="at-chip at-ev-tested">實測</span>[^mg-exp] |
| `--collector.diagnosticdata --collector.replicasetstatus --compatible-mode` | 6108 | 1 | serverStatus、replSetGetStatus、oplog、舊名 metric | <span class="at-chip at-ev-tested">實測</span>[^mg-exp] |
| `--collect-all` | 6729 | 1 | 再加 dbStats、collStats、indexStats、top、currentOp（lab 幾乎沒有 collection） | <span class="at-chip at-ev-tested">實測</span>[^mg-exp] |

- **沒有 collector 成功指標。**`collector_scrape_time_ms{collector}` 在 collector 被拒時照樣輸出（0ms）。用 presence：`mongodb_up == 1 unless mongodb_ss_uptime`（diagnosticdata）、`mongodb_ss_uptime unless mongodb_rs_ok`（replicasetstatus）、`mongodb_ss_uptime unless mongodb_mongod_replset_oplog_head_timestamp`（oplog）。<span class="at-chip at-ev-tested">實測</span>[^mg-exp][^mx-measure]
- **期限。**exporter 用 Prometheus 送來的 `X-Prometheus-Scrape-Timeout-Seconds` 減 1 秒；查詢在註冊 collector 時依序執行，期限過了就跳過剩下的。被鎖住或卡住的 member 的 scrape 都是 3.0 秒（4 − 1）、`up=1`。<span class="at-chip at-ev-tested">實測</span>[^mg-exp][^mx-handler-offset][^mx-describe-ctx]
- **名稱與 label。**名稱依 BSON 路徑產生；8.0 的 ticket 在 `queues.execution`，exporter 改回舊名、label `txn_rw`。`rs_state`、`rs_nm`、`cl_id` 加在 serverStatus 與 replSetGetStatus 的每個 metric 上，`mongodb_up` 沒有。<span class="at-chip at-ev-tested">實測</span>[^mg-exp][^mx-prefix-queues][^mx-txn-rw][^mx-topo-rsstate]
- **multi-target。**`/scrape?target=` 只碰那一台；`/scrapeall` 一個請求依序抓全部，`instance` 變成 `exported_instance`（Prometheus 的 honor_labels 預設 false）。每個 member 一個 exporter 最單純。<span class="at-chip at-ev-tested">實測</span>[^mg-exp][^mg-exp-a][^mx-scrapeall-instance]

## 建議設定 {#recommended}

| 設定 | 值 | 為什麼 |
|---|---|---|
| 部署方式 | 每個 member 一個 exporter（`--mongodb.uri` 指向那一台，預設 direct connection） | 一台卡住只影響自己；`/scrapeall` 會連帶全部 |
| collector | `--collector.diagnosticdata --collector.replicasetstatus --compatible-mode` | 少了任何一個，這頁一半的規則回傳空 |
| 帳號 | admin 的 `clusterMonitor`＋local 的 `read` | 少了前者 serverStatus 全部消失、少了後者 oplog window 消失 |
| `--log.level` | `warn` | `getDiagnosticData` 與 oplog 的失敗是 warn |
| collStats／indexStats | 只列重要的 namespace，或另開長間隔的 job；`--collector.collstats-limit` | 每次 scrape 對每個 collection 跑一次 |
| target label | `cluster=<replica set 名稱>` | role-detection 與選舉規則依它分組；`rs_nm` 在連不上時不存在 |
| `scrape_timeout` | 讓 exporter 的期限（timeout − 1 秒）大於一次正常 scrape | 期限到了以後的 collector 直接跳過 |
| 伺服器 | 明確設 `--wiredTigerCacheSizeGB`；確認 `diagnostic.data` 在持久磁碟 | 容器裡依主機 RAM 算的預設值太大 |

## 盲點 {#blind-spots}

元件卡的 `blind_spots` 有完整清單；重點：

| 看不到什麼 | 類別 | 補法 | 狀態 |
|---|---|---|---|
| 逐查詢的慢查詢、執行計畫 | 資料庫沒提供（指標形式） | profiler、slow query log、PMM Query Analytics | 未補 |
| collection／index 層級的大小與延遲 | exporter 預設不收 | `--collector.collstats`／`indexstats`＋namespace 清單 | 有成本，未量 |
| sharded cluster（mongos、balancer、chunk） | 要靠其他元件／未驗證 | 每個 shard member 與 mongos 各一個 exporter | 只讀碼 |
| FTDC 涵蓋多久、在不在持久磁碟 | 資料庫沒提供 | 盤點 | 盤點 |
| 備份成敗 | 要靠其他元件 | `--collector.pbm` 或備份工具 | 未補 |
| 主機記憶體、swap、磁碟延遲、CPU、OOM kill | 要靠其他元件 | node_exporter、cAdvisor | 已由 [主機與容器資源](../components/host.md) 補上（規則只讀碼，host lab 之後排程） |
| 雲端磁碟的 IOPS 配額、CPU steal | 要靠其他元件 | 雲端供應商的 metric；steal 只畫圖 | 未補 |
| 憑證到期、版本漏洞 | 要靠其他元件 | blackbox TLS、版本盤點 | 其他元件 |
