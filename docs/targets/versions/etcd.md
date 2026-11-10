# etcd 各版本的監控差異

這頁列出 etcd 3.4、3.5、3.6、3.7 之間會影響監控的差異：metric 的新增與搬家、健康端點、v2 API、`--metrics` 的等級。目前的卡片核對過 etcd v3.7.3。

依據是各版本線最新 tag 的原始碼（`v3.4.45`、`v3.5.35`、`v3.6.16`、`v3.7.3`）與 etcd 的 CHANGELOG（main 分支）。etcd 只維護最新兩個 stable minor[^ver-et-two-stable]，目前是 3.7 與 3.6；3.4、3.5 仍被 TiDB 的 PD 內嵌[^ver-ti-pd75-etcd][^ver-ti-pd85-etcd]，所以一起比。沒有做實驗，全部是<span class="at-chip at-ev-code">讀碼</span>。

## 結論 {#summary}

1. **`etcd_network_known_peers` 3.6 才有。**卡片的 `quorum`、`component-count` 用它算成員數，在 3.4、3.5 回傳空；舊版只有 `etcd_network_active_peers`。
2. **`etcd_server_request_duration_seconds` 3.7 才有。**卡片 `request-latency` 的第一個 metric 在 3.6 以下不存在，只剩 `grpc_server_handling_seconds`，而那個要 `--metrics=extensive` 才有。
3. **`etcd_debugging_server_alarms` 與 `etcd_server_feature_enabled` 3.6 才有。**`alarms` 與 `corruption-check` 在 3.4、3.5 要改用 `/health` 或 `etcdctl alarm list`。
4. **`/livez`、`/readyz` 在 3.4.29、3.5.11 才回補。**這頁用的 3.4.45、3.5.35 都有；更舊的 patch 版只有 `/health`，`health-second-path` 的 blackbox 探測會 404。
5. **v2 API：3.4 預設關、3.5 標 deprecated、3.6 移除 `--enable-v2`。**3.6 以上帶著 `--enable-v2` 啟動會因為不認得 flag 而失敗（推論），升版時 `up` 會直接變 0。

## 選的版本 {#versions}

| 版本線 | 這頁用的 tag | 狀態 |
|---|---|---|
| 3.4 | `v3.4.45` | 不在維護範圍（只維護兩個 stable minor） |
| 3.5 | `v3.5.35` | 同上；TiDB PD 8.5 內嵌 v3.5.15[^ver-ti-pd85-etcd] |
| 3.6 | `v3.6.16` | 維護中 |
| 3.7 | `v3.7.3` | 維護中；卡片核對過 |

## 版本差異表 {#diff}

| 版本 | 變更 | 對監控的影響（哪個 metric／collector／規則） | exporter 或 endpoint 有沒有處理 | 依據 |
|---|---|---|---|---|
| 3.4.29、3.5.11 | 回補 `/livez`、`/readyz` | 更早的 patch 版只有 `/health`；`health-second-path`、`quorum` 的 blackbox 探測在舊 patch 版 404 | 內建 endpoint | [^ver-et-cl34-livez][^ver-et-cl35-livez][^ver-et34-livez][^ver-et35-livez] |
| 3.4、3.5 | 沒有 `etcd_network_known_peers`，peer 數只有 `etcd_network_active_peers` | `quorum`、`component-count` 用 `known_peers` 的規則回傳空 | — | [^ver-et35-active-peers][^ver-et36-known-peers] |
| 3.6 | 新增 `etcd_network_known_peers`（`etcd_server_is_learner` 移到 membership 套件，名稱不變） | 卡片的成員數規則從 3.6 起有資料 | — | [^ver-et36-known-peers] |
| 3.6 | 新增 `etcd_debugging_server_alarms{alarm_type}` | `alarms` 規則在 3.4、3.5 回傳空 | — | [^ver-et36-alarms][^ver-et-cl36-alarms] |
| 3.6 | 新增 `etcd_server_feature_enabled` | `corruption-check` 在 3.4、3.5 回傳空；舊版只能看啟動參數 | — | [^ver-et36-feature] |
| 3.6 | 移除 `--enable-v2`、`--experimental-enable-v2v3`；v2store 預設 write-only | 帶舊 flag 的設定升版後啟動失敗，`up` 0（推論） | — | [^ver-et35-enable-v2][^ver-et-cl36-v2-removed][^ver-et36-v2depr] |
| 3.6 | gRPC metric 改用 go-grpc-middleware；`--metrics=extensive` 才加 handling histogram | `grpc_server_handling_seconds_bucket` 名稱不變，仍要 `extensive` | — | [^ver-et35-grpc-hist][^ver-et36-grpc-hist][^ver-et37-metrics-flag] |
| 3.7 | 新增 `etcd_server_request_duration_seconds` | `request-latency` 的主 metric 只在 3.7 有；3.6 以下要 `--metrics=extensive` 並改用 `grpc_server_handling_seconds` | — | [^ver-et37-request-duration][^ver-et-cl37-reqdur] |
| 3.4–3.7 | `etcd_server_has_leader`、`is_leader`、`leader_changes_seen_total`、`proposals_*`、`etcd_disk_wal_fsync_duration_seconds`、`etcd_disk_backend_commit_duration_seconds`、`etcd_mvcc_db_total_size_*`、`etcd_debugging_mvcc_*_revision`、`etcd_server_quota_backend_bytes` 四個版本都有（檔案位置在 3.5、3.6 搬過） | 卡片其他訊號不受影響 | — | 這頁對四個 tag 逐一 grep metric 名稱；3.4 另有已 deprecated 的 `etcd_debugging_mvcc_db_total_size_in_bytes` |

## 各版本要注意的地方 {#notes}

- **3.4**：確認 patch ≥ 3.4.29 才有 `/readyz`；沒有 `known_peers`、`alarms`、`feature_enabled`、`request_duration_seconds`。成員數改用 `count(up{job="etcd"})` 或 `etcd_network_active_peers + 1`。
- **3.5**：確認 patch ≥ 3.5.11；其他同 3.4。`--enable-v2` 仍可用但已 deprecated。
- **3.6**：卡片除了 `request-latency` 以外都對得上；`--enable-v2` 會讓啟動失敗。
- **3.7**：卡片核對的版本。

## 卡片上會隨版本變的訊號 {#signals}

| 訊號 | 隨版本變什麼 | 建議 |
|---|---|---|
| `quorum`、`component-count` | `etcd_network_known_peers` 3.6 起 | `applies_when: etcd 3.6 以上`；3.4／3.5 用 `etcd_network_active_peers` 或 `up` 計數的替代規則 |
| `alarms` | `etcd_debugging_server_alarms` 3.6 起 | `applies_when: etcd 3.6 以上`；舊版靠 `/health`（先檢查 alarm）的 blackbox 探測[^etcd-h-health-alarms] |
| `corruption-check` | `etcd_server_feature_enabled` 3.6 起 | `applies_when: etcd 3.6 以上` |
| `request-latency` | `etcd_server_request_duration_seconds` 3.7 起 | 規則 `or` 上 `grpc_server_handling_seconds_bucket`，並註明要 `--metrics=extensive` |
| `health-second-path` | `/readyz` 在 3.4.29、3.5.11 起 | `applies_when` 寫 patch 版本下限；更舊的改探 `/health` |
| 其他 | 名稱四個版本相同 | 不用改 |

etcd 的監控見 [etcd 怎麼監控](../etcd.md)。TiDB PD 內嵌的 etcd 版本見 [TiDB 各版本的監控差異](tidb.md)。
