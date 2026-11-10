# etcd 怎麼監控

**目標：知道 etcd 叢集現在能不能寫入、能不能做 linearizable read，以及離不能還有多遠。**etcd 自己提供 `/metrics`，沒有 exporter；所以「抓得到 `/metrics`」只代表 process 回應了 HTTP，不代表叢集健康。這頁依九個面向從大到小排列：叢集與共識 → 可用性 → 複寫 → 資源 → 工作量 → 維護 → 保護 → 容量，最前面是監測自檢。

依據是 etcd v3.7.3 的原始碼（raft 函式庫 v3.7.0），加上一組三台叢集的小實驗：停 leader、停兩台失去 quorum、讓 scrape 失敗、讀取與刪除時量 `/metrics`、寫到 quota<span class="at-chip at-ev-tested">實測</span>[^etcd-exp]。慢磁碟、TLS、learner 沒有實驗，只讀原始碼。這台機器的 Docker 容器時鐘比牆鐘慢約 30%，秒數類的實驗結果以主機時鐘量的為準，其餘只看狀態。訊號與規則在[元件卡](../components/etcd.md)，原始碼位置在[附錄](etcd-appendix.md)，機制在[共識（Raft）架構](../architecture/db-raft.md)。3.4、3.5、3.6 與 3.7 之間的 metric 差異見 [etcd 各版本的監控差異](versions/etcd.md)。

## 先講結論

1. **失去 quorum 的 member 照樣回應 `/metrics`。**3 台停 2 台，存活那台 `up=1`、`etcd_server_has_leader=0`、`/livez` 200、`/readyz` 503、寫入逾時。只看 `up` 的規則看不出來。<span class="at-chip at-ev-tested">實測</span>[^etcd-exp-raw]
2. **要三條路交叉：`up`、blackbox 探 `/readyz`、`etcd_server_has_leader`。**`up` 0、探測 1 是 scrape 本身（實測：`sample_limit`）；兩者都 0 是 process 或主機；`up` 1、探測 0 是 etcd 不能服務。見[三方交叉](#health-second-path)。<span class="at-chip at-ev-tested">實測</span>[^etcd-exp-raw]
3. **`/readyz` 不看 NOSPACE。**寫到 quota 後寫入全部被拒，`/readyz` 仍是 200；只有 `/health` 是 503 `ALARM NOSPACE`。要探 `/readyz` 也要探 `/health`。<span class="at-chip at-ev-tested">實測</span>[^etcd-exp-raw][^etcd-h-readyz-corrupt][^etcd-h-health-nospace]
4. **`etcd_debugging_server_alarms` 不能單獨用。**它是「activate 套用過幾次」的累計：重啟過的 member 沒有這條、disarm 後變 -1（兩次執行都是）；第一次執行還出現重複提出後是 2、disarm 後停在 1。<span class="at-chip at-ev-tested">實測</span>[^etcd-exp-raw][^etcd-alarm-activate-existing][^etcd-alarm-store-restore]
5. **NOSPACE 不會自己解除。**compact＋defrag 後 DB 只剩 quota 的 0.1%，官方 mixin 的 95% 規則早就不響，寫入仍被拒，直到 `etcdctl alarm disarm`。mixin 沒有 alarm 規則。<span class="at-chip at-ev-tested">實測</span>[^etcd-exp-raw]
6. **quorum 不要用 etcd 自己的 metric 計數。**scrape 失敗時 `has_leader` 會消失或停在舊值；我們的第一版規則（數 `has_leader == 1`）在 `sample_limit` 情境誤報。改成數 `/readyz` 探測成功的 member，對成員數（etcd 自己的 `etcd_network_known_peers`）過半。<span class="at-chip at-ev-tested">實測</span>[^etcd-exp-raw-v1][^etcd-exp-raw]
7. **`/metrics` 不會被 raft 或 apply 卡住**，最多等一次 backend commit（`current_revision` 的 GaugeFunc 要拿 revMu 讀鎖）。讀 8.5MB 的 prefix（4 條執行緒）與刪 3,000 個 key 時 `/metrics` 中位耗時都在 11ms 以內。<span class="at-chip at-ev-tested">實測</span>（本機 SSD；慢磁碟未測）[^etcd-exp-raw][^etcd-mvcc-report-rev][^etcd-mvcc-txn-revmu]
8. **預設 `--metrics=basic` 沒有 server 端 gRPC histogram**，mixin 的 etcdGRPCRequestsSlow 回傳空；`etcd_server_request_duration_seconds` 在 basic 就有。<span class="at-chip at-ev-tested">實測</span>[^etcd-exp-raw][^etcd-grpc-histogram-extensive]
9. **備份、憑證到期、資料損毀檢查都不在 `/metrics` 裡。**CompactHashCheck 與 InitialCorruptCheck 預設關。見[盲點](#blind-spots)。<span class="at-chip at-ev-tested">實測</span>[^etcd-exp-raw][^etcd-feat-compacthash]

### 九個面向一覽 {#aspects}

| 面向 | 這頁的重點 | 元件卡的訊號 | 證據 |
|---|---|---|---|
| [監測自檢](#exporter) | up 只代表 HTTP；三方交叉；metric 消失；scrape 預算；`/metrics` 會不會被卡 | endpoint-up、health-second-path、metric-families-present、scrape-budget、endpoint-blocking | <span class="at-chip at-ev-tested">實測</span> |
| [叢集與共識](#cluster) | quorum、leader 數、leader 變動、成員數、peer 網路、learner | quorum、role-detection、leader-changes、component-count、peer-network、learner | <span class="at-chip at-ev-tested">實測</span>／<span class="at-chip at-ev-code">讀碼</span> |
| [可用性](#availability) | has_leader；alarm（NOSPACE、CORRUPT） | has-leader、alarms | <span class="at-chip at-ev-tested">實測</span> |
| [複寫](#replication) | apply 進度、snapshot 傳送 | replication-lag、snapshot-transfer | <span class="at-chip at-ev-code">讀碼</span> |
| [資源與飽和](#saturation) | WAL fsync、backend commit、slow apply、pending、fd | wal-fsync、backend-commit、slow-apply、proposals-pending、fd-headroom | <span class="at-chip at-ev-code">讀碼</span> |
| [工作量](#workload) | gRPC 錯誤、提案失敗、請求延遲 | grpc-errors、proposals-failed、request-latency | <span class="at-chip at-ev-tested">實測</span>／<span class="at-chip at-ev-upstream">上游</span> |
| [資料維護](#maintenance) | compaction、defrag、損毀檢查 | compaction、defrag、corruption-check | <span class="at-chip at-ev-tested">實測</span> |
| [資料保護](#protection) | etcd 沒有內建備份 | 不適用（見盲點） | <span class="at-chip at-ev-code">讀碼</span> |
| [容量](#capacity) | DB 對 quota、成長預測 | db-quota、db-growth | <span class="at-chip at-ev-tested">實測</span>／<span class="at-chip at-ev-upstream">上游</span> |

## 監控模型與官方建議 {#model}

etcd 屬於「資料庫內建端點」模型（見[資料庫監控模型](models.md#etcd)）。

| 項目 | 內容 |
|---|---|
| 抓什麼 | 每個 member 的 `/metrics`（client port，或 `--listen-metrics-urls` 指定的獨立 listener；那個 listener 也有健康端點，不需要 client mTLS）[^etcd-embed-metrics-mux][^dbm-etcd-listen-metrics] |
| 細節等級 | `--metrics=basic`（預設）或 `extensive`（多 server 端 gRPC histogram）[^etcd-embed-metrics-flag][^etcd-grpc-histogram-extensive] |
| 健康端點 | `/livez`（本機讀）、`/readyz`（再加 linearizable read、CORRUPT、learner）、`/health`（所有 alarm、leader、讀）[^etcd-h-livez][^etcd-h-readyz-linear][^etcd-h-health-alarms] |
| 官方建議 | 用 Prometheus 抓 `/metrics`，告警用 repo 的 `contrib/mixin`，調整 job selector、每個叢集用唯一 label[^etcd-doc-monitoring] |
| mixin 預設 | `etcd_selector: job=~".*etcd.*"`，`clusterLabel: job`[^etcd-mixin-selector][^etcd-mixin-clusterlabel] |

**和官方 mixin（v3.7.3）的差異：**

| mixin 規則 | 我們 | 差在哪 |
|---|---|---|
| etcdMembersDown（up == 0 或 peer 送出失敗，for 20m）[^etcd-mixin-membersdown] | EtcdEndpointDown＋EtcdEndpointMissing＋三方交叉 | mixin 沒有 absent，也分不出 scrape 與 process |
| etcdInsufficientMembers（up 過半）[^dbm-etcd-mixin-insufficient] | EtcdQuorumAtRisk（`/readyz` 成功數對 known_peers 過半） | `up` 在失去 quorum 時是 1（實測：3 台停 2 台）；網路分割時 mixin 這條不響是由式子推得（推論，沒有做分割實驗） |
| etcdNoLeader[^dbm-etcd-mixin-noleader] | EtcdNoLeader（相同）＋absence 補位 | — |
| etcdHighNumberOfLeaderChanges[^etcd-mixin-leaderchanges] | EtcdFrequentLeaderChanges（相同門檻） | — |
| etcdHighNumberOfFailedGRPCRequests[^etcd-mixin-grpcfail] | EtcdGrpcErrorRate（5%） | — |
| etcdGRPCRequestsSlow[^etcd-mixin-grpcslow] | EtcdRequestSlow（`etcd_server_request_duration_seconds`） | mixin 需要 extensive，basic 下回傳空（實測） |
| etcdMemberCommunicationSlow[^etcd-mixin-peerslow] | EtcdPeerRttHigh（相同） | — |
| etcdHighNumberOfFailedProposals（`rate[15m] > 5`）[^etcd-mixin-proposals] | EtcdProposalsFailing（`increase[15m] > 5`，例子） | mixin 的 rate 是每秒，等於 15 分鐘 4,500 次（讀碼） |
| etcdHighFsyncDurations、etcdHighCommitDurations[^dbm-etcd-mixin-fsync][^etcd-mixin-commit] | 相同門檻 | — |
| etcdDatabaseQuotaLowSpace、etcdExcessiveDatabaseGrowth、etcdDatabaseHighFragmentationRatio[^dbm-etcd-mixin-quota][^etcd-mixin-growth][^etcd-mixin-frag] | 相同，另加 80% 的 warning | 比例在 compact＋defrag 後降回低值，alarm 仍在（實測） |
| （沒有） | EtcdAlarmActive、EtcdHealthProbeFailing | mixin 沒有 alarm 規則 |
| （沒有） | EtcdLeaderCountWrong、EtcdMembersNotScraped、EtcdApplyLagging、EtcdSlowApplies、EtcdHeartbeatSendFailures、EtcdCompactionNotRunning、EtcdCorruptionCheckDisabled | — |

## 監測自檢 {#exporter}

### 三方交叉 {#health-second-path}

回應 `/metrics` 的是 etcd process 本身，所以要從另一條路徑問 etcd 能不能服務。blackbox_exporter 的 http probe 探每個 member 的 `/readyz`，probe job 把 `instance` relabel 成和 `job="etcd"` 相同的 `host:port`，規則用 `on (instance)` 對上（設定見 `lab/targets/etcd/prometheus.base.yml`）。

| 情境（lab） | `up` | `/readyz` 探測 | `has_leader` | 判讀 | 規則（firing 或 pending） |
|---|---|---|---|---|---|
| 正常 | 1 | 1 | 1 | — | — |
| 停掉 leader，20 秒後：停掉的那台 | 0 | 0 | （無） | process 停了 | EtcdEndpointDown、EtcdReadyzProbeFailing、EtcdMembersNotScraped（pending） |
| 停掉 leader，20 秒後：另外兩台 | 1 | 1 | 1 | 已選出新 leader，`leader_changes` 1 → 2 | — |
| 停 2 台（失去 quorum）：停掉的兩台 | 0 | 0 | （無） | process 停了 | EtcdEndpointDown、EtcdReadyzProbeFailing |
| 停 2 台：存活那台 | **1** | **0** | **0** | etcd 不能服務；`/livez` 仍 200 | firing：EtcdServingMetricsButNotReady、EtcdNoLeader、EtcdQuorumAtRisk |
| `sample_limit: 100`（只有 scrape 失敗） | **0** | **1** | （無；第一版那次有一台停在舊值 1） | scrape 本身的問題 | pending：EtcdScrapeFailingButReady、EtcdEndpointDown（for 2m 沒等滿）；EtcdQuorumAtRisk、EtcdLeaderCountWrong 沒有觸發 |
| NOSPACE | 1 | 1 | 1 | `/readyz` 看不到；`/health` 0 | pending：EtcdHealthProbeFailing、EtcdAlarmActive（for 1m 沒等滿） |

<span class="at-chip at-ev-tested">實測</span>[^etcd-exp-raw]

**`/readyz` 在沒有 leader 時要等。**linearizable read 等到 ReqTimeout（5 秒 + 2 × election timeout，預設 7 秒）才回 503。直接打存活那台（主機時鐘）：第一次執行 7.02 秒、第二次 10.00 秒回 503（內容 `[-]linearizable_read failed: etcdserver: request timed out`），兩次都另有一次超過 curl 的 10 秒逾時；容器時鐘比牆鐘慢約 30%，可能是 7 秒變成 10 秒的原因。blackbox timeout 4 秒時 probe 以逾時失敗。`/health` 先看 leader，立刻回 503 `RAFT NO LEADER`。<span class="at-chip at-ev-tested">實測</span>[^etcd-exp-raw][^etcd-read-timeout][^etcd-srv-reqtimeout][^etcd-h-health-noleader]

**learner 的 `/readyz` 一定失敗**（`non_learner` 檢查）；探測清單要排除 learner，或用 `/readyz?exclude=non_learner`。<span class="at-chip at-ev-code">讀碼</span>[^etcd-h-readyz-learner]

### metric 消失、scrape 預算、`/metrics` 會不會被卡 {#exporter-scrape}

| 問題 | 做法 | 證據 |
|---|---|---|
| 該有的 metric 還在嗎 | `up == 1 unless` `etcd_server_has_leader`、`etcd_mvcc_db_total_size_in_bytes`、`etcd_disk_wal_fsync_duration_seconds_count`；再看 `scrape_samples_scraped` 比一小時前少一半 | <span class="at-chip at-ev-tested">實測</span>：每台約 1,600 個 sample、193 個名稱[^etcd-exp-raw] |
| scrape 預算 | 回應約 185KB（basic、3 台）；`scrape_duration_seconds` 對 `scrape_timeout` 80%；`sample_limit` 設太小時整個 target 消失 | <span class="at-chip at-ev-tested">實測</span>[^etcd-exp-raw] |
| `/metrics` 會不會被卡 | handler 是 `promhttp.Handler()`；六個 GaugeFunc 中兩個要 revMu 讀鎖，寫入交易結束時持有寫鎖，期間可能做一次 bbolt commit | <span class="at-chip at-ev-code">讀碼</span>[^etcd-h-metrics-handler][^etcd-mvcc-report-rev][^etcd-mvcc-txn-revmu][^etcd-be-unlock-commit] |

`/metrics` 耗時（主機時鐘，每 0.5 秒量一次）：<span class="at-chip at-ev-tested">實測</span>[^etcd-exp-raw]

| 條件 | 次數 | 中位 | 最大 |
|---|---|---|---|
| 無負載 | 10 | 4ms | 20ms |
| 4 條執行緒反覆讀整個 prefix（3,000 個 key、每次回應 8.5MB），20 秒共 901 次 | 39 | 11ms | 26ms |
| 一次刪除 3,000 個 key | 5 | 5ms | 20ms |

## 叢集與共識 {#cluster}

| 問題 | 訊號 | 證據 |
|---|---|---|
| 還有 quorum 嗎 | `/readyz` 探測成功數 < floor(成員數 / 2) + 1；成員數來自 `etcd_network_known_peers`（每個成員含自己一條；含 learner）[^etcd-member-knownpeers] | <span class="at-chip at-ev-tested">實測</span> |
| 剛好一個 leader 嗎 | `sum by (job) (etcd_server_is_leader) != 1`，有 member `up == 0` 時不評估 | <span class="at-chip at-ev-tested">實測</span> |
| leader 換太多次嗎 | 叢集內 `leader_changes_seen_total` 最大值 15 分鐘增加 ≥ 4（counter 重啟歸零：實測 2 → 1） | <span class="at-chip at-ev-tested">實測</span> |
| 抓得到的 member 數對嗎 | `count(up == 1)` < known_peers 的成員數；不必寫死預期數 | <span class="at-chip at-ev-tested">實測</span> |
| peer 網路 | RTT p99 > 0.15 秒；`peer_sent_failures_total` 的 rate。RTT 健康時每 30 秒才觀察一次[^etcd-rafthttp-probe-interval] | <span class="at-chip at-ev-code">讀碼</span> |
| learner 卡住 | `etcd_server_is_learner == 1` 超過 1 小時（例子） | <span class="at-chip at-ev-code">讀碼</span> |

**版本。**`etcd_network_known_peers` 3.6 才有，EtcdQuorumAtRisk 與 EtcdMembersNotScraped 在 3.4、3.5 回傳空；舊版成員數改用 `etcd_network_active_peers` + 1 或 `up` 計數[^ver-et35-active-peers][^ver-et36-known-peers]。

**為什麼 quorum 用探測計數。**第一版規則數 `has_leader == 1`：`sample_limit` 讓三台 `up=0`、`/readyz` 都正常時，EtcdQuorumAtRisk 與 EtcdLeaderCountWrong 都觸發（一台的 `has_leader` 停在舊值，其他消失）。改版後重跑同樣情境。<span class="at-chip at-ev-tested">實測</span>[^etcd-exp-raw-v1][^etcd-exp-raw]Prometheus 與 blackbox 一起和 etcd 斷開時，改版的規則仍會響，這時 EtcdEndpointDown 也會一起響，代表「看不到」而不是「確定沒有 quorum」。

**Patroni 的 DCS。**PostgreSQL 卡片的 `patroni-dcs` 訊號說「DCS 本身的 quorum 要用它自己的 metric 監控」：用 etcd 當 DCS 時，就是這一節的 quorum、has-leader 與 alarms（NOSPACE 時 Patroni 寫不進 leader key）。

## 可用性 {#availability}

| 問題 | 訊號 | 證據 |
|---|---|---|
| 這台看得到 leader 嗎 | `etcd_server_has_leader == 0` for 1m；只在 raft 狀態改變時更新 | <span class="at-chip at-ev-tested">實測</span>[^etcd-raft-hasleader] |
| 有 alarm 嗎 | 探 `/health`（任何 alarm 都 503）；`etcd_debugging_server_alarms` 只標示種類，要和 `/health` 失敗同時成立 | <span class="at-chip at-ev-tested">實測</span> |

**版本。**`etcd_debugging_server_alarms` 3.6 才有；3.4、3.5 只剩 `/health` 探測，看不出 alarm 種類[^ver-et36-alarms][^ver-et-cl36-alarms]。

NOSPACE 情境（quota 32MiB，每次寫 200KB；第二次執行）：<span class="at-chip at-ev-tested">實測</span>[^etcd-exp-raw]

| 步驟 | DB / quota | 寫入 | 刪除 | `/readyz` | `/health` | `etcd_debugging_server_alarms` |
|---|---|---|---|---|---|---|
| 第 94 次寫入失敗 | 1.03–1.04 | `database space exceeded`（HTTP 429、gRPC ResourceExhausted） | 成功 | 200 | 503 `ALARM NOSPACE`（三台） | 三台都有一筆（e3 提出）＝1 |
| compact＋defrag | 0.001 | 仍被拒 | 成功 | 200 | 503 | e1 也提出了一筆（alarm list 兩筆） |
| 重啟 e2 | — | 仍被拒 | — | 200 | 503（e2 也是） | e1、e3 各兩筆＝1；**e2 沒有這條** |
| `alarm disarm` | — | 成功 | — | 200 | 200 | e1、e3 變 0；**e2 變 -1** |

第一次執行的差別：兩個 member 在第 98 次寫入時各提出一次；重啟 e2 後 e1 的其中一筆變 2，disarm 後停在 1。也就是 alarm 已解除時 gauge 仍可能 > 0，所以 EtcdAlarmActive 要求 `/health` 同時失敗；第二次執行 disarm 後 70 秒內它沒有觸發，但那次 gauge 已回到 0，「停在 1 而 `/health` 正常」這個條件本身沒有被實驗驗證。<span class="at-chip at-ev-tested">實測</span>[^etcd-exp-raw-v1][^etcd-exp-raw]

依據：quota 比的是檔案大小；alarm 已存在時 Activate 回傳既有的那一筆，applier 照樣 Inc；重啟時 alarm store 從 backend 還原、不碰 gauge；capped applier 擋 Put、寫入的 Txn、LeaseGrant，不擋 DeleteRange。<span class="at-chip at-ev-code">讀碼</span>[^etcd-quota-available][^etcd-alarm-activate-existing][^etcd-alarm-gauge-inc][^etcd-alarm-store-restore][^etcd-capped-put]

## 複寫 {#replication}

etcd 的「複寫」是 raft log：leader 送 entry、過半寫入 WAL 才 commit、每台再 apply。etcd 沒有輸出每個 follower 的 match index，只能比較各 member：

| 問題 | 訊號 | 證據 |
|---|---|---|
| 這台 apply 跟得上嗎 | 叢集最大 `etcd_server_proposals_committed_total` − 這台的 `etcd_server_proposals_applied_total` > 5000（例子；兩者都是 gauge，值是 raft index）[^etcd-raft-committed][^etcd-srv-applied] | <span class="at-chip at-ev-code">讀碼</span> |
| snapshot 傳得過去嗎 | `etcd_network_snapshot_send_failures`、`snapshot_receive_failures`；每 10,000 筆 apply 做一次 snapshot，保留 5,000 筆 entry[^etcd-srv-snapshot-count][^etcd-srv-snapshot-catchup] | <span class="at-chip at-ev-code">讀碼</span> |

沒有寫入時 committed 與 applied 一起停、差值是 0，這是真的沒有落後，不是 PostgreSQL 那種「閒置顯示 0」的假象。

## 資源與飽和 {#saturation}

| 問題 | 訊號（門檻） | 證據 |
|---|---|---|
| WAL fsync | p99 > 0.5 秒 warning、> 1 秒 critical（mixin）；超過 1 秒另有 log[^etcd-wal-fsync-observe][^etcd-wal-warn-sync] | <span class="at-chip at-ev-code">讀碼</span> |
| backend commit | p99 > 0.25 秒（mixin）[^etcd-be-commit-observe] | <span class="at-chip at-ev-code">讀碼</span> |
| apply 慢、heartbeat 送不出 | `slow_apply_total`（> 100ms）、`heartbeat_send_failures_total`[^etcd-txn-slow-apply][^etcd-raft-heartbeat-fail] | <span class="at-chip at-ev-code">讀碼</span> |
| 提案排隊 | `proposals_pending` > 100（例子）[^etcd-srv-proposal-pending] | <span class="at-chip at-ev-code">讀碼</span> |
| file descriptor | `os_fd_used / os_fd_limit` > 0.8 | <span class="at-chip at-ev-inferred">推論</span> |

**慢磁碟沒有實驗。**這台機器做不了輕量的慢磁碟模擬，這一節只讀原始碼：raft loop 每批 entry 先 fsync 到 WAL 才往下走，leader 卡住時 heartbeat 送不出去，follower 就發起選舉，所以 leader 變動常是磁碟問題的下游訊號。[^etcd-raft-save-before-apply][^etcd-raft-heartbeat-fail]

## 工作量 {#workload}

| 問題 | 訊號 | 證據 |
|---|---|---|
| gRPC 錯誤比例 | `grpc_server_handled_total` 依 grpc_code，> 5%（mixin）[^etcd-mixin-grpcfail] | <span class="at-chip at-ev-upstream">上游</span> |
| 提案失敗 | `increase(proposals_failed_total[15m]) > 5`（例子）；失去 quorum 時寫一次後是 1[^etcd-srv-proposal-failed] | <span class="at-chip at-ev-tested">實測</span> |
| 請求延遲 | `etcd_server_request_duration_seconds` p99（basic 就有）；`grpc_server_handling_seconds` 要 extensive | <span class="at-chip at-ev-tested">實測</span>[^etcd-srv-request-duration] |

**版本。**`etcd_server_request_duration_seconds` 3.7 才有[^ver-et37-request-duration][^ver-et-cl37-reqdur]。EtcdRequestSlow 對沒有這個 histogram 的 instance 改看 `grpc_server_handling_seconds`（unary），3.6 以下要 `--metrics=extensive` 才有資料；預設 basic 時這條在 3.6 以下回傳空[^ver-et35-grpc-hist][^ver-et36-grpc-hist][^ver-et37-metrics-flag]。

哪個 key、哪個 client 慢，`/metrics` 沒有；只有 warning log。

## 資料維護 {#maintenance}

| 問題 | 訊號 | 證據 |
|---|---|---|
| compaction 有在跑嗎 | 一天內 `compact_revision` 沒變、`current_revision` 有變；`--auto-compaction-retention` 預設 0（關閉）。`compact_revision` 重啟時還原，`db_compaction_last` 重啟歸 0（不要用它算多久沒跑）[^etcd-embed-autocompact][^etcd-compact-rev-restore][^etcd-compaction-last] | <span class="at-chip at-ev-tested">實測</span>：compact 後三台都是 3097 |
| 要 defrag 嗎 | in_use / total < 0.5 且 in_use > 100MB（mixin）；compaction 不會讓檔案變小 | <span class="at-chip at-ev-tested">實測</span>：defrag 後 DB / quota 從 1.03 降到 0.001 |
| 有損毀檢查嗎 | `etcd_server_feature_enabled{name="CompactHashCheck"}`；預設 0，InitialCorruptCheck 也是 0 | <span class="at-chip at-ev-tested">實測</span>[^etcd-feat-compacthash][^etcd-feat-initialcorrupt] |

**版本。**`etcd_server_feature_enabled` 3.6 才有，EtcdCorruptionCheckDisabled 在 3.4、3.5 回傳空，只能看啟動參數[^ver-et36-feature]。

## 資料保護 {#protection}

**etcd 本身不做備份。**`/metrics` 裡的 snapshot（`etcd_debugging_snap_save_*`、`etcd_snap_db_*`、`etcd_network_snapshot_*`）都是 raft snapshot，用來截短 log 或讓落後的 follower 追上。<span class="at-chip at-ev-code">讀碼</span>[^etcd-srv-snapshot-count][^etcd-rafthttp-snapfail]備份要外部執行 `etcdctl snapshot save`，成功時寫一個 textfile metric（例如 `etcd_backup_last_success_timestamp_seconds`）給 node_exporter，再對它的新鮮度告警；還原要定期演練。

## 容量 {#capacity}

| 問題 | 訊號 | 證據 |
|---|---|---|
| 離 quota 多遠 | `etcd_mvcc_db_total_size_in_bytes / etcd_server_quota_backend_bytes` > 0.8（例子）、> 0.95（mixin）；預設 quota 2GiB[^etcd-quota-default] | <span class="at-chip at-ev-tested">實測</span> |
| 多久會滿 | `predict_linear(...[4h], 4h)` > quota（mixin） | <span class="at-chip at-ev-upstream">上游</span> |
| 磁碟 | quota 只限 DB 檔；WAL 與 snapshot 在同一個 data-dir，要 node_exporter 的 `node_filesystem_avail_bytes` | <span class="at-chip at-ev-inferred">推論</span> |

## 盲點與補法 {#blind-spots}

| 盲點 | 類別 | 補法 |
|---|---|---|
| 備份新鮮度與可還原性 | 要靠其他元件 | 備份工作寫 textfile metric；定期還原 |
| TLS 憑證到期 | 資料庫本身沒提供（3.7.3 沒有任何憑證 metric） | blackbox 探 2379、2380 的 `probe_ssl_earliest_cert_expiry`；client-cert-auth 開著時 probe 要帶 client 憑證 |
| 每個 follower 的 raft 進度 | etcd 有、`/metrics` 沒輸出 | 比較各 member 的 applied（replication-lag） |
| member 重啟後的 alarm 狀態 | etcd 有、`/metrics` 沒輸出（gauge 不重建） | 探 `/health`；定期 `etcdctl alarm list` |
| 慢請求的 key 與 client | 資料庫本身沒提供 | log 管線計數 "apply request took too long" |
| 磁碟剩餘空間、唯讀 | 要靠其他元件 | node_exporter；已由 [主機與容器資源](../components/host.md) 補上（規則只讀碼，host lab 之後排程）（磁碟滿時 etcd 的行為未驗證） |
| 資料損毀 | 資料庫本身沒提供（檢查預設關） | 打開 CompactHashCheck |

## 實驗 {#lab}

`lab/targets/etcd`：etcd v3.7.3 三台（quota 32MiB）、Prometheus v3.13.5（scrape 5 秒）、blackbox_exporter v0.27.0（timeout 4 秒），載入 `rules/targets/etcd.yml`。`./run.sh` 約 10 分鐘，結束時 `docker compose down -v`。原始輸出 `results/run-20261109.txt`（規則改版後）與 `results/run-20261109-v1.txt`（第一版規則），摘要 `results/summary.md`。[^etcd-exp]

[^etcd-doc-monitoring]: 文件：[Monitoring etcd](https://etcd.io/docs/v3.6/op-guide/monitoring/)
