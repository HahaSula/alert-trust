# 實驗證據

環境：單機 Docker，受測 Prometheus 依情境 15 萬或 30 萬 series、15 秒 scrape 與 evaluation，由外部 meta Prometheus 和通知接收端量測。原始結果在 [`results/`](results/)，重現方式見 [`README.md`](README.md)。

**這些實驗驗證機制和順序。時間與門檻來自這個小規模環境，不能直接套用。**

## 1. Rule 可能不報錯地失效

2 分鐘間隔、100 秒延遲、`rate[5m]`、`for: 3m`，條件永遠成立，跑 25 分鐘：

| 版本 | 沒加 offset | 表達式 `offset 2m` | `query_offset: 2m` |
|---|---|---|---|
| 2.51.2 | 從未 firing，重置 9 次 | 540s 開始 firing | 不支援 |
| 2.55.1 | 從未 firing，重置 9 次 | 555s | 435s |
| 3.13.5 | 從未 firing，重置 10 次 | 510s | 375s |
| 3.15.1 | 從未 firing，重置 9 次 | 555s | 420s |

全程 `rule_evaluation_failures_total` 和 `iterations_missed_total` 都是 0。

## 2. Range 的公式

60 秒 interval、`for: 3m`，共 24 種組合，跑 24 分鐘（2.51.2、3.13.5）：

| Range | Offset | d = 5s | d = 20s | d = 40s | d = 55s |
|---|---|---|---|---|---|
| 2I | 0 | 2.51 失敗／3.13 正常 | 從未 firing | 從未 firing | 從未 firing |
| 2I | 1m | 正常 | 正常 | 正常 | 正常 |
| 3I、5I | 0 或 1m | 正常 | 正常 | 正常 | 正常 |

結論：沒有足夠 offset 時 `R ≥ 2I + d`；`O ≥ d` 時 `R ≥ 2I`。d = 5s 的差異應來自 scrape 與 evaluation 的相位，不能當作版本差異。矩陣只涵蓋 d < I。

## 3. Heartbeat 的盲點

通知接收端的最大間隔，正常約 35 秒：

| 故障 | Watchdog | 單一 target heartbeat | 帶 `for` 的 canary | 第一個抓到的訊號 |
|---|---|---|---|---|
| Canary 的 target 停機 | 35s | 240s | 213s | `up == 0`，70s |
| 其他 target 停機 | 35s | 35s | 35s | `up == 0`，80s |
| 查詢飽和 | 35s | 35s | 35s | 查詢 slot 比值，65s |
| CPU 降到 0.3 核 | 30s | 35s | 35s | throttling，80s |
| OOM crash loop（重啟 11 次） | 35s | 35s | 284s | `process_start_time_seconds`，10s |
| 磁碟滿 10 分鐘 | 35s | 558s | 583s | `wal_writes_failed`，15s |
| Alertmanager 停機 | 240s | 240s | 240s | `notifications_dropped`，15s |
| Prometheus 停機 3 分鐘 | 35s | 35s | 35s | 外部 `up == 0`，35s |

Prometheus 停機後，Alertmanager 會繼續 repeat 到 `EndsAt`，所以外部接收端仍持續收到。

## 4. CPU 邊際

事件翻轉到收到通知的延遲（`for: 30s`），每階段 1 次事件，只能看趨勢：

| CPU | Throttling | 重 group 耗時 ÷ interval | 大 target scrape | 獨立小 group | 重 group |
|---|---|---|---|---|---|
| 1.0 | 35% | 0.47 | 0.7s | 49.5s | 42s |
| 0.8 | 78% | 0.69 | 0.9s | 49.5s | 47s |
| 0.6 | 90% | 1.3 | 2.7s | 49.5s | 漏掉 |
| 0.5 | 99% | 1.6 | 3.1s | 49.5s | 90s |
| 0.4 | 99% | 2.8 | 7.0s | 49.5s | 84s |
| 0.25 | 100% | 3.0 | 5.9s | 49.5s | 漏掉 |
| 0.15 | 100% | 7.7 | 11.1s | 49.5s | 90s |

`auto-gomaxprocs`（1 核限制）：throttling 37% → 3%，大 target scrape 2.8s → 0.7s。

## 5. 記憶體邊際

| Limit ÷ working set | 執行中重啟 | 非正常重啟後再重啟 | 帶 `for` canary 最大中斷 |
|---|---|---|---|
| 1.5 | 0 | 0 | 30s |
| 1.3 | 0 | 0 | 35s |
| 1.2 | 0 | 0 | 30s |
| 1.1 | 1 | 2 | 35s |
| 1.0 | 4 | 6 | 240s |

Replay 只有 8–13 秒，所以這裡無法驗證大型環境 replay 時的記憶體尖峰。

## 6. 儲存

**IOPS：** 探測確認限速生效（寫 16MB 從 0 秒變 98 秒）。10 IOPS 時 WAL fsync p99 達 4.8–7.7 秒，reload 頻率約減半，scrape 和 rule 幾乎不受影響。記憶體充足，page cache 吃緊時可能不同。

**真實磁碟完全寫滿（2.51.2、2.55.1、3.13.5、3.15.1）：**

| 階段 | 結果 |
|---|---|
| 寫滿 10 分鐘 | WAL 與 rule 寫入失敗；reload 正常；compaction 失敗數為 0；block 數不變 |
| 釋放空間 | 寫入立刻恢復；下一次 compaction 的 checkpoint 失敗；WAL 83MB → 約 135MB；`wal_corruptions_total` 仍是 0 |
| 重啟 | 修復 WAL，`wal_corruptions_total` 變 1 |

2.51.2、3.13.5 印 `unexpected full record`；2.55.1、3.15.1 印 `unexpected first record`。OOM 則是 `unexpected checksum`。

**空間被 compaction 用完（2.51.2／3.13.5）：**

| 時間 | 訊號 |
|---|---|
| 145s／150s | `compactions_failed_total` 增加 |
| 360s／370s | `wal_writes_failed_total` 增加 |
| 565s／575s | `reloads_total` 5 分鐘為 0 |
| 765s／770s | 最舊資料超過 retention |

## 7. 升級

**回退：** 目前所有版本寫 index 都是 `FormatV2`。切換前後的 sample 筆數相同：

| 切換 | 前 | 後 |
|---|---|---|
| 3.13.5 → 2.51.2 | 21000 | 21000 |
| 3.13.5 → 2.55.1 | 20500 | 20500 |
| 2.51.2 → 3.13.5 | 20500 | 20500 |

500 條 gauge、正常關機。官方文件仍只支援回退到 2.55。

**Alert 恢復 3 萬個實例（debug log 時間）：** 2.51.2 1.5s、2.55.1 0.9s、3.13.5 0.7s。

**相同負載的資源比較：** 30 萬 series、每秒約 2 萬 sample、15 分鐘 block，三個版本同時跑，量測 40 分鐘，各一次。

| 指標（平均） | 2.51.2 | 2.55.1 | 3.13.5 |
|---|---|---|---|
| CPU（核） | 0.66 | 0.70 | 0.65 |
| RSS | 1.40GB | 1.25GB | 1.26GB |
| 記憶體配置速率 | 67MB/s | 52MB/s | 53MB/s |
| 大 target scrape | 0.68s | 0.71s | 0.78s |
| 重 rule group | 6.5s | 6.9s | 6.8s |
| WAL 大小 | 316MB | 317MB | 316MB |
| 非正常重啟的 replay | 6.1s | 4.7s | 6.0s |

CPU、scrape、rule 耗時沒有明顯差異，2.55 以上記憶體約少 10%。同時期機器上有其他實驗，幾個百分點內的差距不能當成版本差異。

## 8. Blackbox 探測能看到什麼

每 15 秒探測 Prometheus 的 `/-/healthy`、`/-/ready`、實際查詢，以及 Alertmanager 的 `/-/healthy`、`/-/ready`、`/api/v2/status`。

| 故障 | Prometheus 存活探測 | Prometheus 查詢 | Alertmanager 探測 | 真正抓到的訊號 |
|---|---|---|---|---|
| CPU 降到 0.1 核 | 全部成功 | 全部成功 | 全部成功 | `iterations_missed`，50s |
| 查詢飽和 | 全部成功 | 10／14 | 全部成功 | 查詢 slot 比值，70s |
| Target 停機 | 全部成功 | 全部成功 | 全部成功 | `up == 0`，85s |
| 接收端停機 | 全部成功 | 全部成功 | 全部成功 | `alertmanager_notifications_failed_total` |
| OOM crash loop（重啟 4 次） | healthy 全部成功，ready 18／19 | 18／19 | 全部成功 | `process_start_time_seconds`，10s |
| 磁碟完全寫滿 | 全部成功 | 全部成功 | 全部成功 | `wal_writes_failed_total`，15s |
| Alertmanager 停機 | 全部成功 | 全部成功 | 全部失敗 | Blackbox |
| Prometheus 停機 | 全部失敗 | 全部失敗 | 全部成功 | Blackbox |

接收端停機 170 秒時，`alertmanager_notifications_failed_total` 2 分鐘內增加約 40 次，`prometheus_notifications_errors_total` 為 0。

## 9. K8s 驗證

Minikube、kube-prometheus-stack 57.2、Prometheus 2.51.2、2 個各 15 萬 series 的 target。

| 驗證項目 | 結果 |
|---|---|
| 24 條規則載入 | 全部 `health=ok`；無資料的只有 `interval="30s"`（這裡 15s）和 `ALERTS_FOR_STATE`（當時沒有帶 `for` 的 pending alert） |
| CPU 限制 150m | Throttling 約 70%；`PrometheusCPUThrottled` 限制後約 14 分鐘觸發，內建 `CPUThrottlingHigh` 約 18 分鐘（含 pod 重建） |
| 記憶體接近 limit | 比值到 0.9 但回落，當時 `for: 10m` 未觸發，改為 `for: 5m`，並以 `promtool test rules` 驗證邏輯，K8s 上未再次觸發 |
| OOM | 重啟 6 次、`OOMKilled`、`CrashLoopBackOff`；Prometheus 自己查不到原因，因為當時沒有在抓資料 |
| Alertmanager 不存在 | `NoAlertmanagerDiscovered`、`NotificationsDropped` 有觸發 |
| PVC 用量 | Minikube 的 hostPath 沒有 `kubelet_volume_stats_*`，未驗證 |

**真實 K8s 抓到的問題（都已修正）：**
- `job="prometheus"` 對不上：ServiceMonitor 的 job 是 service 名稱。
- `container_spec_memory_limit_bytes` 被 chart 丟棄，要用 `kube_pod_container_resource_limits`。
- 腳本把 `by (...)` 的 label 當成 metric，並混淆 scrape pool 和 `job` label。

**預設規則的覆蓋（只算 chart 內建 126 條 alert）：** 涵蓋約一半訊號，沒有資料 heartbeat、`for` 重置、scrape 與 rule group 耗時比值、WAL 寫入與 checkpoint 失敗、Head 未落成 block、retention 落後、記憶體接近 limit。這是比對 metric 名稱的結果。

## 10. 每一層怎麼執行：單序還是並行

依原始碼整理（Prometheus 2.51.2、Alertmanager 0.27.0），依資料流排列。瓶頸和連鎖問題都出在單序的環節。除非另外註明，本節是讀碼結果。

| 層級 | 同一個對象內 | 不同對象之間 | 併行上限 | 卡住時影響誰 |
|---|---|---|---|---|
| 服務發現 | 每個 provider 一條 goroutine | 並行 | 無 | Target 清單更新變慢，最快 5 秒同步一次 |
| Scrape（target） | **單序**：抓取、parse、relabel、append、commit。沒跑完下一輪不會開始，tick 被丟掉 | 並行：每個 target 一條 goroutine，用 offset 錯開 | 無，受 CPU 限制 | 只影響該 target，除非搶 CPU、記憶體或 WAL |
| WAL 寫入（全域） | **單序**：所有 scrape 和 rule 的 commit 都經過同一把鎖（`WL.Log`）；切換 segment 的 fsync 由另一條 goroutine 非同步執行 | – | 1 | 全部寫入；平常只寫 page cache 所以很快 |
| Head（series） | **單序**：每條 series 一把鎖 | 並行：16384 個 stripe；chunk 落盤是單一背景佇列 | 依 stripe | 碰到相同 stripe 或 series 的操作 |
| Rule group（group） | **單序**：rule 依定義順序跑，相鄰兩輪不重疊 | 並行：每個 group 一條 goroutine | Group 數無上限；查詢受查詢引擎限制 | 同 group 的所有 alert |
| 查詢引擎（全域） | **單序**：一個查詢的求值在同一條 goroutine，合併多個 querier 時才另開 | 有上限並行：rule 和 API 搶同一組 slot，解析與執行各搶一次 | `--query.max-concurrency`，預設 20 | Rule 和 API 都排隊 |
| Alert 狀態（rule） | **單序**：在 group 的 goroutine 裡更新 | 跟隨 rule group | 跟隨 rule group | Group 慢時跟著延後 |
| Notifier（全域） | **單序**：一條 queue、一條送出迴圈，每批最多 64 筆。同一批同時送給所有 Alertmanager，等全部回應才送下一批 | – | 同時 1 批 | 全部通知；逾時預設 10 秒 |
| Alertmanager（alert group） | **單序**：每個 alert group 依 `group_interval` 依序送出 | 並行：每個 alert group 一條 goroutine，同 group 的多個 receiver 也並行，失敗會重試 | 無 | 用到該接收端的 group |
| TSDB 維護（全域） | **單序**：reload、retention、head compaction、block compaction、WAL 截斷都在同一條 goroutine（`DB.run`） | – | 1 | 全部維護工作；compaction 慢或失敗時 retention 延後或停止 |

**單序環節造成過的實際問題：**

| 單序環節 | 結果 | 證據 |
|---|---|---|
| 單一 target 內 | Scrape 超過 interval 時 tick 被丟掉，alert 無法 firing，其他 target 不受影響 | 實測 |
| 單一 rule group 內 | 同一條 alert 放在重 group，延遲變近兩倍還會漏事件；獨立小 group 不受影響 | 實測 |
| 查詢 slot | 重查詢佔滿後，rule group 從幾秒變 40 秒，scrape 不受影響 | 實測 |
| WAL 全域寫入 | 10 IOPS 時 scrape 和 rule 不受影響；磁碟寫滿時兩者同時失敗 | 實測；page cache 吃緊的情況未測 |
| TSDB 維護 | Compaction 用完空間時 reload 歸零；長查詢卡住 Compact 時 reload 也停止 | 實測 |
| Notifier 送出迴圈 | 一台 Alertmanager 慢會拖累全部 | 讀碼；實驗只測過 Alertmanager 完全停機 |

依據（v2.51.2）：`scrape/scrape.go`（`sync`、`scrapeLoop.run`、`scrapeAndReport`）、`tsdb/wlog/wlog.go`（`Log`、`nextSegment`）、`tsdb/head.go`、`tsdb/chunks/chunk_write_queue.go`、`rules/manager.go`、`rules/group.go`（`run`、`Eval`）、`promql/engine.go`（`queueActive`）、`notifier/notifier.go`、`tsdb/db.go`（`run`）。Alertmanager 0.27.0：`dispatch/dispatch.go`（`aggrGroup.run`）、`notify/notify.go`（`FanoutStage`、`RetryStage`）。

## 11. 執行模型在各版本的差異

2.55.1 的單序結構和 2.51.2 完全相同；3.x 只改了 notifier，以及選用的 rule 並行。比對 v2.51.2、v2.55.1、v3.0.0、v3.5.0、v3.13.5、v3.15.1 的原始碼，起始版本取自 repo tag，截至 2026-11。

| 層級 | 2.51.2、2.55.1 | 3.5 LTS（已結束支援） | 3.13 LTS、3.15 |
|---|---|---|---|
| Scrape、WAL、Head、查詢引擎 | 相同 | 相同 | 相同 |
| Rule group | 預設單序；`concurrent-rule-eval` 無法判斷依賴時順序可能失效 | 預設單序；開 flag 時，3.1 起無法判斷依賴會退回依序，3.2 起分三段：無上游併行 → 上下游都有依序 → 無下游併行 | 同左 |
| Notifier | 一條 queue，每批 64 筆，等所有 Alertmanager 回應 | 結構相同；3.4 起批次預設 256 筆，可用 `--alertmanager.notification-batch-size` 調整 | **3.10 起每台各一條 queue**，一台慢不再拖累其他台 |
| TSDB 維護 | 單一 goroutine；2.55 起可選 `delayed-compaction` | 單一 goroutine | 單一 goroutine；3.9 起 reload 間隔可設定，3.10 起有可選的 stale series compaction。Reload timer 被重置的問題仍在，實測 3.13.5 同樣歸零 |
| `GOMAXPROCS`、`GOMEMLIMIT` | 要開 feature flag | 3.0 起預設對齊 | 3.0 起預設對齊 |

Reload ticker 修正只在 main，還沒有正式版。Alertmanager 本身的版本差異不在此表。

## 上游參考

- [#13027](https://github.com/prometheus/prometheus/issues/13027)、[#13152](https://github.com/prometheus/prometheus/issues/13152) WAL 損毀後無法截斷
- [#19909](https://github.com/prometheus/prometheus/issues/19909) Lookback 邊界
- [PR #20161](https://github.com/prometheus/prometheus/pull/20161) Reload ticker
- [#11088](https://github.com/prometheus/prometheus/discussions/11088)、[#11807](https://github.com/prometheus/prometheus/issues/11807) Rule 與延遲資料
- [Grafana Labs：metamonitoring 架構](https://grafana.com/blog/2021/04/08/how-we-use-metamonitoring-prometheus-servers-to-monitor-all-other-prometheus-servers-at-grafana-labs/)
- [PromLabs：End-to-End Watchdog Alerts](https://training.promlabs.com/training/monitoring-and-debugging-prometheus/metrics-based-meta-monitoring/end-to-end-watchdog-alerts/)
