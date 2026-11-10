# 附錄：從原始碼到 metric（MongoDB）

[主頁](mongodb.md)列出每個訊號該看哪個 metric。這頁回答另一個問題：**這個 metric 真的是從那個欄位來的嗎？欄位不存在、指令被拒、或沒有 primary 時會怎樣？**每一列追完整條鏈：

1. **伺服器端**：MongoDB 8.0.34 原始碼裡產生欄位的那一行（WiredTiger 的統計在內附的 `src/third_party/wiredtiger`）。
2. **指令與欄位**：`getDiagnosticData`（內含 serverStatus、replSetGetStatus、oplog 統計）或 exporter 自己送的指令。
3. **Exporter 端**：Percona mongodb_exporter v0.54.0 在哪裡讀、怎麼命名。
4. **Metric 與 lab 觀察**。

每個原始碼參考都在 tag 的 clone 上用關鍵字找出行號。Lab 欄的「實測」只用在親眼看到的情況。[^mg-exp][^mg-exp-a]

## 先講結論

1. **名稱是從 BSON 路徑自動產生的。**exporter 沒有對照表：serverStatus 的每個數字欄位依路徑變成 `mongodb_ss_<路徑>`（`serverStatus.wiredTiger` 縮成 `ss_wt`），replSetGetStatus 變成 `mongodb_rs_<路徑>`，少數子文件的 key 變成 label（`conn_type`、`assert_type`、`csr_type`、`txn_rw`）。MongoDB 改了欄位名稱，metric 名稱就跟著改。<span class="at-chip at-ev-code">讀碼</span>[^mx-prefix-ss][^mx-prefix-rs][^mx-txn-rw]
2. **字串與陣列不輸出，日期輸出成毫秒。**`stateStr`、`syncSourceHost` 之類只出現在 label（`member_state`）；`optimeDate`、`lastCommittedWallTime` 是 epoch 毫秒。<span class="at-chip at-ev-code">讀碼</span>[^mx-asfloat-date][^mx-member-state-label]
3. **一次 scrape 約 6100 個 sample。**每 member 一個 exporter、加三個旗標時 5592 個名稱；WiredTiger 統計佔 1400 多個。`--collect-all` 再加 collStats、indexStats、dbStats、top、currentOp、profile 約 600 個（lab 的資料很少，正式環境依 collection 與 index 數成長）。<span class="at-chip at-ev-tested">實測</span>[^mg-exp]
4. **舊名 metric 只在 `--compatible-mode`。**`mongodb_mongod_replset_member_replication_lag`、`mongodb_mongod_replset_oplog_head_timestamp`、`mongodb_mongod_replset_my_state`、`mongodb_instance_uptime_seconds`、`mongodb_mongod_wiredtiger_concurrent_transactions_available_tickets` 都是 v1 的名稱，由 exporter 另外計算或改名。PMM 的規則用的是這一組。<span class="at-chip at-ev-code">讀碼</span>[^mx-diag-special][^mx-compat-tickets]

## 一次 scrape 的流程 {#scrape}

| 步驟 | exporter 原始碼 | 失敗時 |
|---|---|---|
| 期限 | `X-Prometheus-Scrape-Timeout-Seconds`（沒有就 10 秒）減 `--web.timeout-offset`（1 秒）[^mx-handler-header][^mx-handler-default10][^mx-handler-offset] | — |
| 連線 | 預設每次 scrape 新建一個 client，`--mongodb.connect-timeout-ms` 5000 同時是 server selection timeout[^mx-new-client][^mx-flag-connect-timeout] | client 是 nil，只註冊 general collector → `mongodb_up 0` |
| 拓撲 label | `isMaster`、`getCmdLineOpts`、`replSetGetConfig`、`replSetGetStatus` 決定 `cl_role`、`rs_nm`、`cl_id`、`rs_state`[^mx-topo-rsnm][^mx-topo-rsstate][^mx-topo-state] | 該 label 不出現 |
| general | 一次 `ping`（primaryPreferred）[^mx-up-ping] | `mongodb_up 0` |
| diagnosticdata（旗標） | `getDiagnosticData`；8.0 的回應分段，exporter 先合併[^mx-register-diag][^mx-diag-merge8] | **只記 warn**（預設 log level 是 error，看不到），整組 `mongodb_ss_*`、`mongodb_rs_*`（來自 FTDC 的那份）、`mongodb_oplog_stats_*` 消失[^mx-diag-warn][^mx-flag-loglevel] |
| compatible mode | 從同一份資料算舊名 metric；再對 `local.oplog.rs` 做兩次 `findOne`（`$natural` 排序）取 head／tail[^mx-diag-special][^mx-oplog-find] | oplog 讀不到只記 warn，head／tail 消失[^mx-oplog-warn] |
| replicasetstatus（旗標） | 自己送 `replSetGetStatus`[^mx-rs-error] | 記 error，`mongodb_rs_members_*` 等消失 |
| 註冊即查詢 | 每個 collector 在 `Describe` 時就執行查詢；期限到了以後的 collector 直接跳過[^mx-describe-collect][^mx-describe-ctx] | 跳過的那一組 metric 消失，`up` 仍是 1 |
| 回應 | `promhttp` 用 `ContinueOnError`，有錯也回 200[^mx-continue-on-error] | — |

## 欄位到 metric {#fields}

| 訊號 | 伺服器原始碼（8.0.34） | 欄位 | exporter | metric | Lab |
|---|---|---|---|---|---|
| role-detection | `replication_info.cpp` 的 repl 段[^mg-writable-primary] | `serverStatus.repl.isWritablePrimary`（bool） | bool 轉 1／0 | `mongodb_ss_repl_isWritablePrimary` | <span class="at-chip at-ev-tested">實測</span> kill 兩個 secondary 後全部 0 |
| replication-lag | heartbeat 的 applied optime，秒為單位[^mg-rs-optimedate]；DOWN 時清成 0[^mg-set-down-optime] | `replSetGetStatus.members[].optimeDate` | 找到 PRIMARY 才算、取絕對值[^mx-compat-gotprimary][^mx-compat-lag][^mx-compat-abs] | `mongodb_mongod_replset_member_replication_lag{name,self,state}` | <span class="at-chip at-ev-tested">實測</span> 自報 26／primary 看 31；沒有 primary 時 0 條 |
| replication-lag | 同上 | 同上 | members 陣列加 `member_idx`、`member_state`[^mx-member-state-label] | `mongodb_rs_members_optimeDate`（毫秒） | <span class="at-chip at-ev-tested">實測</span> 沒有 `set` label |
| member-health | `bb.append("health", h)`[^mg-rs-health] | `members[].health`、`state`、`stateStr`（不可達時是 `(not reachable/healthy)`）[^mg-rs-notreachable] | 同上 | `mongodb_rs_members_health`、`mongodb_rs_members_state` | <span class="at-chip at-ev-tested">實測</span> kill 的那台 0、`(not reachable/healthy)` |
| elections | 每次選舉 term + 1[^mg-term-increment] | `replSetGetStatus.term`、`electionCandidateMetrics`[^mg-rs-candidate]；serverStatus `electionMetrics`[^mg-election-sss] | 自動命名 | `mongodb_rs_term`、`mongodb_ss_electionMetrics_electionTimeout_called` | <span class="at-chip at-ev-tested">實測</span> 每次選舉 +1；換角色時 series 換 label |
| majority-commit-lag | `optimes.lastCommittedWallTime`[^mg-rs-lastcommitted] | `replSetGetStatus.optimes.*` | 日期轉毫秒 | `mongodb_rs_optimes_lastAppliedWallTime − mongodb_rs_optimes_lastCommittedWallTime` | <span class="at-chip at-ev-tested">實測</span> 兩台卡住 70 秒 |
| majority-commit-lag | `flowControl` 段[^mg-fc-ss-islagged][^mg-fc-ss-target] | `serverStatus.flowControl.isLagged`、`targetRateLimit` | 自動命名 | `mongodb_ss_flowControl_isLagged`、`..._targetRateLimit` | <span class="at-chip at-ev-tested">實測</span> isLagged 1、targetRateLimit 100 |
| oplog-window | oplog 是 capped collection[^mg-oplog-capacity] | `local.oplog.rs` 第一筆與最後一筆的 `ts` | compatible mode 的 `oplogStatus`[^mx-oplog-find][^mx-oplog-head] | `mongodb_mongod_replset_oplog_head_timestamp − ..._tail_timestamp`（秒） | <span class="at-chip at-ev-tested">實測</span> 2MB 寫 10MB 後 95 秒 |
| oplog-growth | 被釘住的 oplog 不能截斷[^mg-oplog-pinned][^mg-pinned-rollback] | getDiagnosticData 的 `local.oplog.rs.stats` | 前綴 `oplog_stats` | `mongodb_oplog_stats_storageStats_size`、`..._maxSize` | <span class="at-chip at-ev-tested">實測</span> 5.4MB → 6.7MB（上限 2MB） |
| tickets | `queues.execution`[^mg-queues-section]；throughputProbing[^mg-ticket-algo] | `serverStatus.queues.execution.{read,write}` | 改名成舊的 `concurrentTransactions`，label `txn_rw`[^mx-prefix-queues][^mx-txn-rw] | `mongodb_ss_wt_concurrentTransactions_available{txn_rw}`、`..._read_normalPriority_queueLength` | <span class="at-chip at-ev-tested">實測</span> total 16＝nproc |
| wt-cache | WT 統計字串[^wt-stat-dirty][^wt-stat-max][^wt-stat-app-evict] | `serverStatus.wiredTiger.cache["tracked dirty bytes in the cache"]` 等 | 空白與括號換成底線 | `mongodb_ss_wt_cache_tracked_dirty_bytes_in_the_cache`、`..._application_thread_time_evicting_usecs` | <span class="at-chip at-ev-tested">實測</span> 名稱存在 |
| connections | 額滿時拒絕[^mg-conn-refused] | `serverStatus.connections.{current,available}` | key 變 `conn_type` | `mongodb_ss_connections{conn_type}` | <span class="at-chip at-ev-tested">實測</span> available 8168 |
| cursors | `metrics.cursor.timedOut`[^mg-cursor-timedout]；10 分鐘逾時[^mg-cursor-timeout] | `serverStatus.metrics.cursor` | key 變 `csr_type` | `mongodb_ss_metrics_cursor_open{csr_type}`、`mongodb_ss_metrics_cursor_timedOut` | <span class="at-chip at-ev-tested">實測</span> 名稱存在 |
| majority-writes | wtimeout 0＝無限[^mg-wc-notimeout] | `serverStatus.metrics.getLastError.wtimeouts` | 自動命名 | `mongodb_ss_metrics_getLastError_wtimeouts` | <span class="at-chip at-ev-tested">實測</span> wtimeout 5000 的寫入 0 → 1 |
| ttl-monitor | 60 秒一輪[^mg-ttl-sleep] | `serverStatus.metrics.ttl.passes` | 自動命名 | `mongodb_ss_metrics_ttl_passes` | <span class="at-chip at-ev-code">讀碼</span> |
| checkpoint | 60 秒一次[^mg-syncdelay] | `wiredTiger.checkpoint["most recent time (msecs)"]` | 自動命名 | `mongodb_ss_wt_checkpoint_most_recent_time_msecs` | <span class="at-chip at-ev-code">讀碼</span> |
| sharding-balancer | 10 秒一輪[^mg-balancer-round] | mongos 上 `config.changelog`、`config.settings` | compatible mode 的 mongos 專用查詢[^mx-changelog][^mx-balancer-enabled][^mx-mongos-compat] | `mongodb_mongos_sharding_changelog_10min_total{event}` | <span class="at-chip at-ev-code">讀碼</span>（lab 沒有 mongos） |
