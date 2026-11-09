# etcd lab 摘要（2026-11-09）

etcd v3.7.3 ×3、Prometheus v3.13.5、blackbox_exporter v0.27.0。數字取自 `run-20261109.txt`（第二次執行），第一次執行（`run-20261109-v1.txt`）不同的地方另外標出。

## 三方交叉

| 情境 | 對象 | up | /readyz | /livez | /health | has_leader |
|---|---|---|---|---|---|---|
| 正常 | 三台 | 1 | 1 | 1 | 1 | 1 |
| B 停 leader（20 秒後） | 停掉的 | 0 | 0 | 0 | 0 | 無 |
| | 另外兩台 | 1 | 1 | 1 | 1 | 1（leader_changes 1 → 2） |
| C 停 2 台（15 秒與 85 秒後） | 停掉的兩台 | 0 | 0 | 0 | 0 | 無 |
| | 存活那台 | 1 | 0 | 1 | 0 | 0 |
| D sample_limit 100 | 三台 | 0 | 1 | 1 | 1 | 無（v1：一台停在 1） |
| F NOSPACE | 三台 | 1 | 1 | 1 | 0 | 1 |

## 其他觀察

- C：存活那台 `/metrics` 200、185,233 bytes、0.02 秒；`/livez` 200；`/health` 立刻 503 `RAFT NO LEADER`；`/readyz` 一次 10 秒逾時、一次 10.00 秒 503（`linearizable_read failed: etcdserver: request timed out`；v1 是 7.02 秒）；寫入 `context deadline exceeded`，`proposals_failed_total` = 1。firing：EtcdServingMetricsButNotReady、EtcdNoLeader、EtcdQuorumAtRisk、EtcdReadyzProbeFailing ×3、EtcdHealthProbeFailing ×3。
- D：v1 的規則中 EtcdQuorumAtRisk 與 EtcdLeaderCountWrong 都 firing（誤報）；改版後兩條都沒有觸發，EtcdScrapeFailingButReady 與 EtcdEndpointDown pending。
- 重啟的 member 的 `leader_changes_seen_total` 從 1 開始（B：e2；C：e1、e2）。
- A：每台約 1,600 個 sample、193 個 metric 名稱；`grpc_server_handling_seconds_bucket` 不存在，`etcd_server_request_duration_seconds_bucket` 有 105 條；`etcd_server_feature_enabled` 的 CompactHashCheck、InitialCorruptCheck 都是 0；known_peers 每台 3 條、值 1。
- E：`/metrics` 中位 4ms（無負載）、11ms（讀取中，20 秒 901 次、每次 8.5MB）、5ms（刪除 3,000 個 key）；沒有非 200。
- F：第 94 次寫入失敗（DB / quota 1.03–1.04；v1 是第 98 次、1.06–1.08）；`/readyz` 200、`/health` 503 `ALARM NOSPACE`；Put 被拒、Delete 成功；compact 到 3097＋defrag 後 DB / quota 0.001，Put 仍被拒；重啟 e2 後 e2 沒有 `etcd_debugging_server_alarms`；disarm 後 Put 成功，e1、e3 的 gauge 變 0、e2 變 -1。v1：重啟後 e1 的一筆變 2、disarm 後停在 1。
- 最後 EtcdSlowApplies 在 e3 firing（C 之後一直 pending，沒有追原因）；EtcdGrpcErrorRate 在 C 之後於 e3 pending。
