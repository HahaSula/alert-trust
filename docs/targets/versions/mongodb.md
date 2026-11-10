# MongoDB 各版本的監控差異

這頁列出 MongoDB 6.0、7.0、8.0 與 8.2（rapid release）之間會影響監控的差異：serverStatus 欄位搬家或改名、ticket 機制改變、mongodb_exporter v0.54.0 有沒有處理。目前的卡片核對過 MongoDB 8.0.34（8.2.12、9.0.4 只查過 OTel metrics）。

依據是各版本線最新 tag 的原始碼（`r6.0.29`、`r7.0.45`、`r8.0.34`、`r8.2.12`，含內附的 WiredTiger）與 Percona mongodb_exporter v0.54.0。沒有做實驗，全部是<span class="at-chip at-ev-code">讀碼</span>；metric 名稱是依 exporter 的命名規則從 BSON 路徑推出的，標「推論」。

## 結論 {#summary}

1. **ticket 從 `serverStatus.wiredTiger.concurrentTransactions`（6.0、7.0）搬到 `serverStatus.queues.execution`（8.0 起）。**exporter 把新路徑改回舊前綴 `mongodb_ss_wt_concurrentTransactions_*`，`available`／`out`／`totalTickets` 的名稱三個版本相同。
2. **`tickets` 用的排隊數在 6.0 不存在、在 7.0 名稱不同。**6.0 預設是 `SemaphoreTicketHolder`，沒有 `queueLength`；7.0 的 `queueLength` 直接在 `read`／`write` 下；8.0 起多一層 `normalPriority`。卡片的 `mongodb_ss_wt_concurrentTransactions_read_normalPriority_queueLength` 只在 8.0 以上有，6.0、7.0 規則回傳空。
3. **7.0 起 ticket 數會動態調整（`throughputProbing`）。**6.0 固定 128，`available` 的固定門檻有意義；7.0 起 `totalTickets` 會變，門檻要用比例或改看排隊數。使用者手動設了 ticket 數時自動改回固定。
4. **WiredTiger 的 checkpoint 統計在 8.0 換分類。**6.0、7.0 是 `transaction: transaction checkpoint most recent time (msecs)`，8.0 起是 `checkpoint: most recent time (msecs)`；`checkpoint: total failed number of checkpoints` 只在 8.0 有。卡片的 `mongodb_ss_wt_checkpoint_*` 兩條規則在 6.0、7.0 回傳空。
5. **8.0 的 getDiagnosticData 分段回傳。**exporter v0.54.0 會合併；更舊的 exporter 在 8.0 上 `mongodb_ss_*` 會整組換路徑。

## 選的版本 {#versions}

| 版本線 | 這頁用的 tag | 狀態（[lifecycle](https://www.mongodb.com/legal/support-policy/lifecycles)，2026-11-10） |
|---|---|---|
| 6.0 | `r6.0.29` | 2025-07-31 EOL |
| 7.0 | `r7.0.45` | 支援到 2027-08-31 |
| 8.0 | `r8.0.34` | 卡片核對過、lab 實測；支援到 2029-10-31 |
| 8.2（rapid） | `r8.2.12` | 2026-07-31 EOL |
| 8.3、9.0 | `r8.3.13`、`r9.0.4` | 這頁沒有比對 serverStatus（卡片只查過 9.0 的 OTel metrics） |
| mongodb_exporter | `v0.54.0` | 最新 release |

## 版本差異表 {#diff}

| 版本 | 變更 | 對監控的影響（哪個 metric／collector／規則） | exporter 或 endpoint 有沒有處理 | 依據 |
|---|---|---|---|---|
| 6.0 | ticket 在 `wiredTiger.concurrentTransactions.{read,write}`，預設 `SemaphoreTicketHolder`，只有 `out`、`available`、`totalTickets`；ticket 數固定 128 | `tickets` 規則的 `*_queueLength` 不存在，回傳空；排隊只能看 `mongodb_ss_globalLock_currentQueue` | 不需要（照路徑命名） | [^ver-mg60-ct-section][^ver-mg60-semaphore][^ver-mg60-semaphore-stats][^ver-mg60-ticket-128] |
| 7.0 | ticket 仍在 `wiredTiger.concurrentTransactions`；預設仍是 `SemaphoreTicketHolder`（priority holder 的 feature flag 預設關），但多了 `queueLength`、`processing` 等，直接放在 `read`／`write` 下 | 排隊數的名稱是 `mongodb_ss_wt_concurrentTransactions_read_queueLength`（推論），不是卡片的 `..._read_normalPriority_queueLength` | 沒有：exporter 不做這個對應 | [^ver-mg70-ct-section][^ver-mg70-semaphore][^ver-mg70-depri-flag][^ver-mg70-semaphore-stats][^ver-mg70-queue-length] |
| 7.0 | `storageEngineConcurrencyAdjustmentAlgorithm` 預設 `throughputProbing`；使用者設了 ticket 數就改回 `fixedConcurrentTransactions` | `mongodb_ss_wt_concurrentTransactions_totalTickets` 會隨負載變；`available < N` 這類固定門檻失去意義 | 不需要 | [^ver-mg70-algo][^ver-mg70-algo-userset] |
| 8.0 | ticket 搬到 `queues.execution`；`SemaphoreTicketHolder` 也包一層 `normalPriority` | 卡片的 `*_read_normalPriority_queueLength` 從 8.0 起才有 | 有：`serverStatus.queues.execution` 改回 `ss_wt_concurrentTransactions` 前綴；`--compatible-mode` 的舊名從新名轉換 | [^ver-mg80-queues-execution][^ver-mg80-semaphore-normal][^ver-mgx-queues-prefix][^ver-mgx-compat-tickets] |
| 8.2 | `queues.execution` 結構同 8.0 | 同 8.0 | 同 8.0 | [^ver-mg82-queues-execution] |
| 8.0 | WiredTiger checkpoint 統計從 `transaction` 分類移到 `checkpoint` 分類；新增 `total failed number of checkpoints` | 6.0、7.0 的名稱是 `mongodb_ss_wt_txn_transaction_checkpoint_most_recent_time_msecs`（推論）；`checkpoint` 規則與「checkpoint 失敗」規則在 6.0、7.0 回傳空 | 沒有：exporter 照路徑命名 | [^ver-mg60-wt-ckpt][^ver-mg70-wt-ckpt][^ver-mg80-wt-ckpt][^ver-mg80-wt-ckpt-failed] |
| 8.0 | getDiagnosticData 的 `data` 分成多個區塊（exporter 以有沒有 `common` 鍵判斷） | 沒合併時 `mongodb_ss_*` 的路徑多一層 | 有：v0.54.0 看到 `common` 就把各區塊攤平 | [^ver-mgx-merge8] |
| 6.0–8.0 | `metrics.getLastError.wtime`、`wtimeouts` 在三個版本都有 | `majority-writes` 三個版本都能用 | 不需要 | [^ver-mg60-gle][^ver-mg80-gle] |
| 6.0–8.2 | `isMaster` 仍是 `hello` 的別名 | exporter 啟動時送的 `isMaster` 在 8.2 仍可用 | 不需要 | [^ver-mg82-ismaster] |

## mongodb_exporter 怎麼處理版本 {#exporter}

| 項目 | 做法 | 會怎麼壞 | 依據 |
|---|---|---|---|
| 名稱 | 依 BSON 路徑套前綴表命名 | MongoDB 換路徑時名稱跟著換；只有 `queues.execution` 有特別對應 | [^ver-mgx-queues-prefix] |
| 8.0 分段 | 有 `common` 鍵時把各區塊合併 | v0.54.0 已處理 | [^ver-mgx-merge8] |
| compatible-mode | v1 舊名（`mongodb_mongod_wiredtiger_concurrent_transactions_*`）從新名轉換 | 新名在 6.0、7.0、8.0 一致，所以舊名也一致 | [^ver-mgx-compat-tickets] |

## 各版本要注意的地方 {#notes}

- **6.0**：已 EOL。沒有 ticket 排隊數，ticket 數固定 128；`available` 接近 0 就是飽和。checkpoint 用 `transaction` 分類的名稱，沒有失敗次數。
- **7.0**：ticket 數動態調整；排隊數沒有 `normalPriority` 這層；checkpoint 名稱同 6.0。
- **8.0、8.2**：卡片的名稱都對得上；`throughputProbing` 仍是預設。

## 卡片上會隨版本變的訊號 {#signals}

| 訊號 | 隨版本變什麼 | 建議 |
|---|---|---|
| `tickets` | 6.0 沒有排隊數；7.0 名稱沒有 `normalPriority`；7.0 起 ticket 數動態 | 規則 `or` 上 `mongodb_ss_wt_concurrentTransactions_{read,write}_queueLength`；`applies_when` 寫「排隊數 7.0 以上」；6.0 改看 `globalLock_currentQueue` |
| `checkpoint` | 8.0 起才有 `mongodb_ss_wt_checkpoint_*`，失敗次數只在 8.0 以上 | 規則 `or` 上 `mongodb_ss_wt_txn_transaction_checkpoint_most_recent_time_msecs`；失敗次數那條 `applies_when: MongoDB 8.0 以上` |
| `version-semantics` | ticket 路徑 8.0 搬家（exporter 已對應） | 不用改；卡片可補「6.0、7.0 的路徑是 wiredTiger.concurrentTransactions」 |
| `collectors-present`、`collector-success` | 8.0 的分段回應要 exporter v0.54.0 這類新版本合併 | `versions_checked` 寫明 exporter 版本下限 |
| `majority-writes`、`majority-commit-lag`、`replication-lag`、`elections`、`member-health` | 用的 `getLastError.*`、`replSetGetStatus`、`flowControl` 欄位在 6.0–8.2 沒有找到改名 | 不用改 |

FTDC 的預設取樣週期與目錄大小在三個版本的 `config.h` 都用同一組常數名稱，這頁沒有逐一比對數值。

MongoDB 本體的監控見 [MongoDB 怎麼監控](../mongodb.md)。
