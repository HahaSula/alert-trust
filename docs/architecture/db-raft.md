# 共識（Raft）架構

這頁只寫**監控訊號或盲點依賴的共識機制**。每一節依序是機制、什麼會壞、哪個訊號看得到。目前只有 etcd（v3.7.3，raft 函式庫 go.etcd.io/raft v3.7.0）；TiKV（每個 Region 一組 Raft）、YugabyteDB（每個 tablet 一組 Raft）、MongoDB（自己的 Raft 變體）之後在每一節加一段。訊號連到 [etcd 元件卡](../components/etcd.md)；etcd 該監控什麼的完整清單在 [etcd 怎麼監控](../targets/etcd.md)。

以原始碼與 `lab/targets/etcd` 的實驗為準。<span class="at-chip at-ev-code">讀碼</span> <span class="at-chip at-ev-tested">實測</span>

## 先講結論

1. **失去 quorum 的節點還活著，而且照常回應 `/metrics`。**Raft 的失敗是「彼此連不到過半」，不是 process 停掉；`up` 看不出來，要看節點自己的「有沒有 leader」。etcd 實測：3 台停 2 台，存活那台 `up=1`、`etcd_server_has_leader=0`。[^etcd-exp-raw]
2. **「有沒有 leader」是每個節點自己的看法，只在狀態改變時更新。**follower 要等 election timeout 才發現 leader 不見，leader 要等 CheckQuorum 才退位；這段時間內舊的值仍是 1。[^etcd-raft-hasleader][^etcd-raft-checkquorum-stepdown]
3. **fsync 在每次 commit 的關鍵路徑上。**慢的磁碟先讓 heartbeat 送不出去，再讓 follower 以為 leader 不見而發起選舉；leader 變動次數是磁碟問題的下游訊號。[^etcd-raft-save-before-apply][^etcd-raft-heartbeat-fail]
4. **成員數要問叢集自己，不要用 scrape target 數。**target 數取決於服務發現；etcd 的 `etcd_network_known_peers` 是成員表本身。[^etcd-member-knownpeers]
5. **快照與 compaction 是兩件事。**raft snapshot 截短 log、讓落後的節點追上；資料的 compaction 清舊版本。兩者都不是備份。

## 對照 {#compare}

| 機制 | etcd | 什麼會壞 | 看得到的訊號 | 本頁 |
|---|---|---|---|---|
| 選主與 term | 一個叢集一組 Raft；PreVote、CheckQuorum 開著 | 選舉期間沒有 leader；頻繁換 leader | has-leader、leader-changes、role-detection | [選主](#election) |
| quorum | 過半投票成員（learner 不算） | 寫入與 linearizable read 全部停 | quorum、health-second-path | [quorum](#quorum) |
| log 複寫與 commit | leader 送 entry，過半寫入 WAL 才 commit | follower 落後；提案逾時 | replication-lag、proposals-pending、proposals-failed | [複寫](#replication) |
| 持久化 | 每批 entry 先 fsync 到 WAL，再 apply 到 bbolt | 磁碟慢拖住整個叢集 | wal-fsync、backend-commit、slow-apply | [fsync](#fsync) |
| snapshot 與 compaction | 每 10,000 筆 apply 做 raft snapshot；MVCC compaction 另外設定 | follower 只能收整份 snapshot；DB 長到 quota | snapshot-transfer、compaction、defrag、db-quota | [snapshot](#snapshot) |
| learner | 只收 log、不投票 | 卡著沒升級；健康探測一直失敗 | learner | [learner](#learner) |

## 選主與 term {#election}

**機制。**每個節點有一個 term 與它認定的 leader（`lead`）。follower 在 election timeout 內沒有收到 heartbeat，先成為 pre-candidate（PreVote）：此時 `lead` 被設成 None。[^etcd-raft-precandidate-lead]拿到過半的 pre-vote 才真的加 term、成為 candidate。leader 開著 CheckQuorum：一段時間內收不到過半回應就退成 follower，`lead` 也是 None。[^etcd-raft-checkquorum-stepdown][^etcd-boot-checkquorum]

**etcd 怎麼變成 metric。**raft loop 只在 `Ready` 帶著 SoftState（狀態改變）時更新：`lead == None` 時 `etcd_server_has_leader` 設 0，否則 1；看到一個「和上次不同、不是 None」的 leader 時 `etcd_server_leader_changes_seen_total` 加 1；自己是 leader 時 `etcd_server_is_leader` 設 1。[^etcd-raft-hasleader][^etcd-raft-leaderchanges]

**什麼會壞。**

- leader 停機：follower 等 election timeout 後選出新 leader。lab 停掉 leader，20 秒後另外兩台的 `leader_changes` 從 1 變 2、`sum(is_leader)` 是 1。[^etcd-exp-raw]
- leader 被分割到少數那邊：在 CheckQuorum 讓它退位之前，它和多數那邊的新 leader 同時是 `is_leader=1`。持續不久，但 `sum(is_leader) > 1` 不是「不可能」。
- counter 是每個節點自己的：重啟歸零（lab：重啟的 member 從 2 回到 1）。[^etcd-exp-raw]

**哪個訊號看得到。**[has-leader](../components/etcd.md#has-leader)、[leader-changes](../components/etcd.md#leader-changes)（取叢集內最大值再算 increase）、[role-detection](../components/etcd.md#role-detection)。

## quorum 失去 {#quorum}

**機制。**commit 要過半投票成員把 entry 寫進 WAL；linearizable read 要 leader 用 ReadIndex 向過半確認自己仍是 leader。少於過半時沒有 leader，兩者都停。etcd 的 linearizable read 等到 `ReqTimeout`（5 秒 + 2 × election timeout，預設 7 秒）才回逾時。[^etcd-read-timeout][^etcd-srv-reqtimeout]

**什麼會壞。**process 都還在，`/metrics` 照常回應。lab 停掉 3 台中的 2 台：[^etcd-exp-raw]

| 存活的 member | 值 |
|---|---|
| `up` | 1（`/metrics` 200，約 185KB） |
| `etcd_server_has_leader` | 0 |
| `/livez` | 200（只做本機的 serializable read） |
| `/readyz` | 503，7–10 秒後（`linearizable_read failed: request timed out`；主機時鐘，容器時鐘較慢）；也有超過 10 秒的 |
| `/health` | 立刻 503 `RAFT NO LEADER` |
| 寫入 | `context deadline exceeded` |

**哪個訊號看得到。**[quorum](../components/etcd.md#quorum)：看得到 leader 的節點數對成員數過半；[health-second-path](../components/etcd.md#health-second-path)：`up=1` 而 `/readyz` 探測失敗。用 `up` 算過半（官方 mixin 的 etcdInsufficientMembers）在這個情境下只算到停掉的那兩台，對「process 在、網路分割」的情況不響。[^dbm-etcd-mixin-insufficient]

## log 複寫與 commit index {#replication}

**機制。**client 的寫入變成提案（proposal）送進 raft；leader 附加到自己的 log、送給 follower，過半寫入後推進 commit index，每個節點再依序 apply 到狀態機。etcd 把 commit index 放進 `etcd_server_proposals_committed_total`、已 apply 的 index 放進 `etcd_server_proposals_applied_total`，兩個都是 gauge（值是 index）。[^etcd-raft-committed][^etcd-srv-applied]送出後等結果的提案數是 `etcd_server_proposals_pending`；Propose 失敗或等結果逾時，`etcd_server_proposals_failed_total` 加 1。[^etcd-srv-proposal-pending][^etcd-srv-proposal-failed][^etcd-srv-proposal-timeout]

**什麼會壞。**follower 落後（磁碟慢、網路慢）時 applied 跟不上；leader 端每個 follower 的進度（match index）在 raft tracker 裡，etcd 沒有輸出成 metric，只能比較各節點的 applied。quorum 失去時提案逾時：lab 存活那台在一次寫入嘗試後 `proposals_failed_total` 是 1。[^etcd-exp-raw]

**哪個訊號看得到。**[replication-lag](../components/etcd.md#replication-lag)（叢集最大 committed − 每台 applied）、[proposals-pending](../components/etcd.md#proposals-pending)、[proposals-failed](../components/etcd.md#proposals-failed)、[peer-network](../components/etcd.md#peer-network)。

## fsync 在關鍵路徑上 {#fsync}

**機制。**etcd 的 raft loop 每次拿到一批 `Ready`，先把 HardState 與 entry 寫進 WAL（含 fsync）才往下走；leader 先把訊息送出去再寫自己的 WAL（並行），follower 寫完才回應。[^etcd-raft-save-before-apply][^etcd-raft-leader-parallel]apply 之後資料進 bbolt，backend 預設每 100ms 或 10,000 筆 commit 一次，有 delete 時當場 commit。[^etcd-be-batch-limit][^etcd-be-unlock-commit]

**什麼會壞。**WAL fsync 慢讓 commit 慢；leader 的 raft loop 卡住時 heartbeat 送不出去（`etcd_server_heartbeat_send_failures_total`，程式註解「likely overloaded from slow disk」），follower 收不到 heartbeat 就發起選舉。[^etcd-raft-heartbeat-fail]apply 超過 `--warning-apply-duration`（預設 100ms）時 `etcd_server_slow_apply_total` 加 1。[^etcd-txn-slow-apply][^etcd-embed-warn-apply]

**對 `/metrics` 的影響。**寫入交易結束時先拿 mvcc 的 revMu 寫鎖、再呼叫 `tx.Unlock()`，而 Unlock 在有 delete 時當場做 bbolt commit；`etcd_debugging_mvcc_current_revision` 是 scrape 時才讀的 GaugeFunc，要拿 revMu 讀鎖。所以一次 scrape 最多等一次 backend commit。[^etcd-mvcc-txn-revmu][^etcd-be-unlock-commit][^etcd-mvcc-report-rev]lab 在本機 SSD 上刪 3,000 個 key 時沒有量到差別；慢磁碟沒有測（這台機器做不了輕量的慢磁碟模擬）。

**哪個訊號看得到。**[wal-fsync](../components/etcd.md#wal-fsync)（p99 > 0.5／1 秒）、[backend-commit](../components/etcd.md#backend-commit)（p99 > 0.25 秒）、[slow-apply](../components/etcd.md#slow-apply)、[endpoint-blocking](../components/etcd.md#endpoint-blocking)。門檻取自官方 mixin。[^dbm-etcd-mixin-fsync][^etcd-mixin-commit]

## snapshot 與 compaction {#snapshot}

**機制。**兩件不同的事：

| | raft snapshot | MVCC compaction |
|---|---|---|
| 做什麼 | 把狀態機存一份，截短 raft log | 刪掉指定 revision 以前的舊版本 |
| 何時 | 每 10,000 筆 apply（`--snapshot-count`）；之後只保留 5,000 筆 entry 給慢的 follower[^etcd-srv-snapshot-count][^etcd-srv-snapshot-catchup] | `--auto-compaction-retention` 預設 0（關閉），要自己排程或設定[^etcd-embed-autocompact] |
| 失敗時 | follower 落後超過保留的 entry，只能收整份 snapshot；傳送失敗計入 `etcd_network_snapshot_send_failures`[^etcd-rafthttp-snapfail] | revision 一直累積，DB 長到 quota |

compaction 之後檔案不會縮小，空出來的頁面要 defrag 才還給檔案系統；quota 比的是檔案大小，不是使用中的大小。[^etcd-quota-available]到 quota 時發 NOSPACE alarm，寫入被拒，alarm 要手動 disarm。lab：compact＋defrag 後 DB / quota 從 1.03 降到 0.001，寫入仍被拒，直到 `etcdctl alarm disarm`。[^etcd-exp-raw]

**哪個訊號看得到。**[snapshot-transfer](../components/etcd.md#snapshot-transfer)、[compaction](../components/etcd.md#compaction)、[defrag](../components/etcd.md#defrag)、[db-quota](../components/etcd.md#db-quota)、[alarms](../components/etcd.md#alarms)。

## learner {#learner}

**機制。**learner 收 log 但不投票，不算 quorum；用來讓新成員先追上再升級。etcd 的 `/readyz` 有一個 `non_learner` 檢查，learner 一定失敗。[^etcd-h-readyz-learner]

**什麼會壞。**learner 追不上就一直不能升級；對 learner 做 `/readyz` 探測會一直告警。成員表（`etcd_network_known_peers`）包含 learner，用它算過半時要扣掉 learner。[^etcd-member-knownpeers]

**哪個訊號看得到。**[learner](../components/etcd.md#learner)（`etcd_server_is_learner`）。[^etcd-member-islearner]

## TiKV：每個 Region 一組 Raft {#tikv}

以下是 TiKV v8.5.8 的對應；訊號連到 [TiDB 元件卡](../components/tidb.md)，完整清單在 [TiDB 怎麼監控](../targets/tidb.md)。<span class="at-chip at-ev-code">讀碼</span> <span class="at-chip at-ev-tested">實測</span>

**multi-raft。**TiKV 把 key 範圍切成 Region，每個 Region 預設三個副本、各自是一組 Raft；一台 TiKV（store）上有很多組，每組有自己的 leader（lab 只有 5 個 Region）。raft tick 預設 1 秒、election timeout 10 個 tick（10 秒）；閒置的 Region 會休眠（hibernate-regions 預設開），不送 heartbeat。[^tikv-raft-tick][^tikv-raft-election][^tikv-hibernate]所以沒有「這台有沒有 leader」這種單一值，etcd 的 `has_leader` 在 TiKV 沒有對應。

**誰知道哪裡壞了。**每台 TiKV 每 10 秒送 store heartbeat 給 PD；每個 Region 的 leader 另外送 region heartbeat，帶著 down peer（超過 `max-peer-down-duration`，預設 10 分鐘沒回應的副本）與 pending peer（log 落後的副本）。[^tikv-store-heartbeat][^tikv-max-peer-down]PD leader 把這些彙總成 `pd_cluster_status`（每個 store 的狀態）與 `pd_regions_status`（各類 Region 的數量），只有 PD leader 輸出。[^pd-sched-metrics-collect][^pd-store-status-order]

| etcd 的機制 | TiKV 的對應 | 什麼會壞 | 看得到的訊號 |
|---|---|---|---|
| [選主](#election) | 每個 Region 各自選 leader；store 停掉時它上面的 leader 在 election timeout 後移走 | 那台上的 Region 短暫不能寫 | [store-state](../components/tidb.md#store-state)、[server-busy](../components/tidb.md#server-busy) |
| [quorum](#quorum) | 每個 Region 的過半副本；一台停掉時三副本的 Region 剩兩個，仍有過半 | 再停一台，兩台都有副本的 Region 失去過半 | [region-health](../components/tidb.md#region-health)（down peer） |
| [log 複寫](#replication) | follower 落後成為 pending peer；落後太多改送 snapshot | 副本長時間落後 | [replication-lag](../components/tidb.md#replication-lag) |
| [fsync](#fsync) | raft log 先寫 raft engine 再 apply 到 RocksDB；append、commit、apply 各有 histogram | 磁碟慢時 append 慢、write stall | [raftstore-latency](../components/tidb.md#raftstore-latency)、[write-stall](../components/tidb.md#write-stall) |
| 成員數 | PD 記得的 store 數（非 Tombstone） | scrape 漏掉某台 | [component-count](../components/tidb.md#component-count) |
| PD 自己的 Raft | PD 內嵌 etcd v3.5.15：上面 etcd 的每一節都適用；但 PD leader（etcd lease 選出）不等於 etcd 的 raft leader | PD 失去 quorum 時 TSO 停止 | [pd-quorum](../components/tidb.md#pd-quorum)、[role-detection](../components/tidb.md#role-detection) |

**lab 看到的。**3 台停 1 台（`max-peer-down-duration` 40 秒、`max-store-down-time` 1 分鐘）：+33 秒 store Disconnected、+85 秒 Down、+142 秒 down-peer 5 與 pending-peer 5；SQL 讀寫照常（每個 Region 仍有兩個副本）。只有 3 台時 PD 沒有地方補副本，miss-peer 一直是 0。[^tidb-exp-raw]

**和 etcd 不同的陷阱。**PD 的 store／region 狀態只在 PD leader 上有；PD leader 那台抓不到時，用 `etcd_server_is_leader` 當前提的官方規則全部安靜（lab 實測）。etcd 的 quorum 計數可以用 `etcd_network_known_peers` 當成員數，PD 內嵌的 etcd 3.5.15 沒有這個 metric。[^pd-rule-down][^pd-etcd-version]

## 之後加入的資料庫

| 資料庫 | Raft 的單位 | 要補的段落 |
|---|---|---|
| TiKV（TiDB） | 每個 Region 一組；PD 內嵌 etcd | 已加在 [TiKV](#tikv)；snapshot 傳送與 Region 層級的 leader 平衡尚未補 |
| YugabyteDB | 每個 tablet 一組；master 另一組 | leaderless tablet、follower lag |
| MongoDB | replica set 的選舉協定（Raft 變體） | term、election、oplog 落後 |
