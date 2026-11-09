# 附錄：從原始碼到 metric（etcd）

[主頁](etcd.md)列出每個訊號該看哪個 metric。這頁回答另一個問題：**這個 metric 是在 etcd 的哪一行產生的？什麼時候更新、什麼時候不更新？**etcd 沒有 exporter，所以鏈只有兩段：

1. **etcd 端**：值在哪裡產生（etcd v3.7.3，commit `0a173d0`；raft 函式庫 go.etcd.io/raft v3.7.0，是 v3.7.3 的 `go.mod` 所用版本）。
2. **Metric**：名稱、型別、更新時機。

每個原始碼參考都在 tag 上逐行核對過關鍵字。Lab 欄的「實測」只用在 `lab/targets/etcd` 親眼看到的情況。

## 先講結論

1. **`/metrics` 不經過 raft、不經過 apply。**handler 就是 `promhttp.Handler()`；scrape 時才算的值只有六個 GaugeFunc，其中兩個要拿 mvcc 的 revMu 讀鎖，最多等一次 backend commit。<span class="at-chip at-ev-code">讀碼</span>[^etcd-h-metrics-handler][^etcd-mvcc-report-rev][^etcd-mvcc-txn-revmu]
2. **狀態類 gauge 只在事件發生時更新。**`has_leader`、`is_leader` 只在 raft SoftState 改變時寫；`etcd_debugging_server_alarms` 只在 apply alarm 請求時 Inc／Dec，重啟不重建。<span class="at-chip at-ev-tested">實測</span>[^etcd-raft-hasleader][^etcd-alarm-gauge-inc][^etcd-alarm-store-restore][^etcd-exp-raw]
3. **名字有 `_total` 的不一定是 counter。**`etcd_server_proposals_committed_total`、`etcd_server_proposals_applied_total` 是 gauge，值是 raft index。<span class="at-chip at-ev-code">讀碼</span>[^etcd-raft-committed][^etcd-srv-applied]
4. **三個健康端點檢查的東西不同。**`/livez` 只做本機讀；`/readyz` 加上 linearizable read、CORRUPT alarm、learner；`/health` 看所有 alarm、leader、再做一次讀。<span class="at-chip at-ev-tested">實測</span>[^etcd-h-livez][^etcd-h-readyz-corrupt][^etcd-h-health-alarms][^etcd-exp-raw]

## 健康端點 {#health}

| 端點 | 檢查（依序） | 失敗時 | NOSPACE | 沒有 leader | learner | 依據 |
|---|---|---|---|---|---|---|
| `/livez` | serializable read（本機） | 503 | 200 | 200（實測） | 200 | [^etcd-h-livez] |
| `/readyz` | data_corruption（只看 CORRUPT alarm）、serializable read、linearizable read、non_learner | 503，內容列出每個檢查 | 200（實測） | 等到 ReqTimeout（預設 7 秒；實測 7–10 秒以上）後 503 | 一定 503 | [^etcd-h-readyz-corrupt][^etcd-h-readyz-linear][^etcd-h-readyz-learner][^etcd-read-timeout] |
| `/health` | 所有 alarm → leader → range（用 ReqTimeout） | 503，JSON 的 reason | 503 `ALARM NOSPACE`（實測） | 立刻 503 `RAFT NO LEADER`（實測） | 依 leader | [^etcd-h-health-alarms][^etcd-h-health-nospace][^etcd-h-health-noleader][^etcd-h-health-reqtimeout] |

`/readyz`、`/livez` 的 read check 用請求本身的 context，沒有自己的逾時；只有 linearizable read 內部的 ReadIndex 會在 ReqTimeout 回錯。[^etcd-h-readcheck-ctx][^etcd-read-timeout][^etcd-srv-reqtimeout]`?exclude=<check>` 可以排除某個檢查，`?verbose` 回每個檢查的結果。每次有人打 `/livez`、`/readyz`，`etcd_server_healthcheck{type,name}` 才更新，沒有人探測時它是舊值。[^etcd-h-healthcheck-gauge]`--listen-metrics-urls` 的 listener 也掛這三個端點。[^etcd-embed-metrics-mux]

## 共識 {#consensus}

| Metric | 型別 | 產生的地方 | 更新時機 | Lab | 依據 |
|---|---|---|---|---|---|
| `etcd_server_has_leader` | gauge | raft loop | SoftState 改變：`Lead == None` 設 0 | 失去 quorum 時存活那台 0 | [^etcd-raft-hasleader] |
| `etcd_server_is_leader` | gauge | raft loop | 同上 | 停 leader 後另一台變 1 | [^etcd-raft-hasleader] |
| `etcd_server_leader_changes_seen_total` | counter | raft loop | 看到新的、不是 None 的 leader | 重啟歸零 | [^etcd-raft-leaderchanges] |
| `etcd_server_heartbeat_send_failures_total` | counter | 送 raft 訊息前 | heartbeat 超過 2 倍 interval 才送出 | — | [^etcd-raft-heartbeat-fail] |
| `etcd_server_proposals_committed_total` | **gauge** | raft loop 寫完 WAL 後 | `HardState.Commit` | — | [^etcd-raft-committed] |
| `etcd_server_proposals_applied_total` | **gauge** | apply 之後 | 已 apply 的 index | — | [^etcd-srv-applied] |
| `etcd_server_proposals_pending` | gauge | 提案送出後 | Inc／defer Dec | — | [^etcd-srv-proposal-pending] |
| `etcd_server_proposals_failed_total` | counter | 提案 | Propose 失敗、等結果逾時 | 失去 quorum 時寫一次後是 1 | [^etcd-srv-proposal-failed][^etcd-srv-proposal-timeout] |
| `etcd_network_known_peers{Local,Remote}` | gauge | 成員表 | 每個成員（含自己、learner）1，移除的 0 | 3 台時每台 3 條 1 | [^etcd-member-knownpeers] |
| `etcd_server_is_learner` | gauge | 成員表 | 自己是不是 learner | 都是 0 | [^etcd-member-islearner] |
| `etcd_network_peer_round_trip_time_seconds` | histogram | rafthttp prober | 健康時每 30 秒、不健康時 5 秒觀察一次平滑 RTT | — | [^etcd-rafthttp-rtt][^etcd-rafthttp-probe-interval] |
| `etcd_network_peer_sent_failures_total{To}` | counter | rafthttp | 送不出的訊息 | — | [^etcd-rafthttp-sentfail] |
| `etcd_network_snapshot_send_failures` | counter | rafthttp | snapshot 傳送失敗 | — | [^etcd-rafthttp-snapfail] |

raft 端：pre-candidate 把 `lead` 設成 None；leader 的 CheckQuorum 失敗時以 `lead = None` 退成 follower；etcd 一律開 CheckQuorum。[^etcd-raft-precandidate-lead][^etcd-raft-checkquorum-stepdown][^etcd-raft-becomefollower][^etcd-boot-checkquorum]

## 磁碟與 apply {#disk}

| Metric | 型別 | 產生的地方 | 依據 |
|---|---|---|---|
| `etcd_disk_wal_fsync_duration_seconds` | histogram | 每次 WAL fsync；超過 1 秒另寫 warning log | [^etcd-wal-fsync-observe][^etcd-wal-warn-sync] |
| `etcd_disk_backend_commit_duration_seconds` | histogram | bbolt commit（含 fsync） | [^etcd-be-commit-observe] |
| `etcd_disk_backend_defrag_duration_seconds` | histogram | defrag | [^etcd-be-defrag-metric] |
| `etcd_server_slow_apply_total` | counter | apply 超過 `--warning-apply-duration`（預設 100ms） | [^etcd-txn-slow-apply][^etcd-embed-warn-apply] |
| `etcd_server_request_duration_seconds{type,success}` | histogram | Range、ReadonlyTxn 與每種寫入；`--metrics=basic` 就有 | [^etcd-srv-request-duration] |
| `grpc_server_handling_seconds` | histogram | 只在 `--metrics=extensive` 註冊（lab：basic 下沒有這個名字） | [^etcd-grpc-histogram-extensive][^etcd-exp-raw] |

## 儲存、quota 與 alarm {#storage}

| Metric | 型別 | 產生的地方 | 陷阱 | 依據 |
|---|---|---|---|---|
| `etcd_mvcc_db_total_size_in_bytes` | GaugeFunc | scrape 時讀 `backend.Size()`（atomic） | 檔案大小；compaction 不會讓它變小 | [^etcd-mvcc-gaugefunc-size][^etcd-be-size-atomic] |
| `etcd_mvcc_db_total_size_in_use_in_bytes` | GaugeFunc | 同上，`SizeInUse()` | — | [^etcd-mvcc-gaugefunc-size] |
| `etcd_server_quota_backend_bytes` | gauge | 啟動時設定 | 預設 2GiB | [^etcd-quota-metric][^etcd-quota-default] |
| `etcd_debugging_server_alarms{server_id,alarm_type}` | gauge | apply alarm 請求時 Inc／Dec；alarm 已存在時 Activate 回傳既有那筆，照樣 Inc | 重啟不重建、disarm 後 -1；曾出現 2 與 disarm 後停在 1（實測） | [^etcd-mvcc-alarm-metric][^etcd-alarm-gauge-inc][^etcd-alarm-activate-existing][^etcd-alarm-store-restore] |
| `etcd_debugging_mvcc_current_revision` | GaugeFunc | scrape 時拿 revMu 讀鎖 | 最多等一次 backend commit | [^etcd-mvcc-report-rev] |
| `etcd_debugging_mvcc_compact_revision` | GaugeFunc | 同上 | 重啟時從 backend 還原 | [^etcd-compact-rev-restore] |
| `etcd_debugging_mvcc_db_compaction_last` | gauge | compaction 完成時設 unix time | 重啟歸 0 | [^etcd-compaction-last] |
| `etcd_server_feature_enabled{name,stage}` | gauge | 啟動時 | CompactHashCheck、InitialCorruptCheck 預設 0（實測） | [^etcd-feat-metric][^etcd-feat-compacthash][^etcd-feat-initialcorrupt] |

NOSPACE 的路徑：寫入的成本加上目前檔案大小超過 quota 時，apply 回 ErrNoSpace，server 提出 NOSPACE alarm；alarm 在 alarm store 裡時 applier 換成 capped 版本，Put、寫入的 Txn、LeaseGrant 一律回 ErrNoSpace，DeleteRange 不擋。啟動時依 alarm store 決定要不要 capped。[^etcd-quota-available][^etcd-srv-nospace-raise][^etcd-capped-put][^etcd-uber-restore-alarms]

## `/metrics` 會不會被卡住 {#endpoint-blocking}

| GaugeFunc | 拿的鎖 | 誰會長時間持有 | 依據 |
|---|---|---|---|
| `db_total_size_in_bytes`、`db_total_size_in_use_in_bytes`、`db_open_read_transactions` | 只有各自的 report mutex（啟動時寫一次）；值是 atomic | 沒有 | [^etcd-mvcc-gaugefunc-size][^etcd-be-size-atomic] |
| `current_revision`、`compact_revision` | mvcc `revMu` 讀鎖 | 寫入交易結束時持有寫鎖，期間 `tx.Unlock()` 在有 delete 或達到 batch limit 時做 bbolt commit；restore 時也持有 | [^etcd-mvcc-report-rev][^etcd-mvcc-txn-revmu][^etcd-be-unlock-commit] |
| `etcd_debugging_auth_revision` | auth 的 report mutex | 沒有長時間持有 | — |

所以 `/metrics` 只在 backend commit 慢時被拖慢，最多一次 commit 的時間；沒有任何 raft 或 apply 的鎖。lab 在本機 SSD 上讀取與刪除時沒有量到差別，慢磁碟沒有測。

## 沒有釘到原始碼行的部分 {#unpinned}

- `os_fd_used`、`os_fd_limit` 的更新時機（etcd 的 `runtime` 套件定期讀 `/proc`）沒有逐行核對。
- `--metrics=extensive` 多出來的 series 數沒有量。

## 實驗 {#lab}

`lab/targets/etcd`：etcd v3.7.3 三台、Prometheus v3.13.5、blackbox_exporter v0.27.0，情境是停 leader、停兩台失去 quorum、sample_limit 讓 scrape 失敗、讀取與刪除時量 `/metrics`、寫到 quota。原始輸出在 `results/run-20261109.txt`，摘要在 `results/summary.md`。[^etcd-exp]
