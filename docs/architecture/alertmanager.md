# Alertmanager 架構

Alertmanager 是單一行程，負責把 alert 分組、決定要不要通知，再送到接收端。所有 ruler（Prometheus、vmalert）共用它，所以它一壞，全部的通知一起斷。

以 0.27.0、0.29.0 原始碼為準，並比對到 0.34.2。<span class="at-chip at-ev-code">讀碼</span>

<!-- figure: alertmanager-process -->

**圖**　Alert 從 ruler 進入 API 與記憶體裡的 alert store，經 dispatcher 分組，每個 aggregation group 定期 flush 進通知流程，再送到接收端。實線是資料流，虛線是查詢。紅框的 alert store 是全域瓶頸：寫入時持鎖送進每個訂閱者的 channel。[^am-mem-put][^am-mem-chan][^am-aggr-run][^am-pipeline]
{: .at-caption }

## Alert store {#alert-store}

Alert 只存在記憶體，全域一把鎖。寫入時持鎖把 alert 送進 dispatcher 和 inhibitor 的 channel（長度 200），channel 滿了 POST 就會阻塞。[^am-mem-put][^am-mem-chan]0.30 起才有這個 channel 的寫入次數，仍看不到佔用率。

## Dispatcher {#dispatcher}

比對路由樹，把 alert 放進對應的 aggregation group。0.31 以前只有一條 goroutine；0.32 起改成多條 worker，這個改動在 0.32.0、0.32.1 帶來過回歸，升級時要避開。

## Aggregation group {#aggregation-group}

| 項目 | 行為 |
|---|---|
| Group 之間 | 並行，每個 group 一條 goroutine |
| 同一個 group | 第一次等 `group_wait`，之後每 `group_interval` flush 一次，依序執行[^am-aggr-run] |
| 一次 flush 的逾時 | `group_interval` 加上等待時間 |
| 卡住時影響誰 | 只有這個 group |

- **失敗要等整輪 flush 結束才計數。**`group_interval` 是 1h 時，單一 group 持續失敗的 `notifications_failed_total` 每 1 小時才加 1，`rate(...[5m])` 抓不到。<span class="at-chip at-ev-tested">實測</span>[^exp-am-window] 見[告警視窗契約](../contracts.md#am-failure-window)。
- **SMTP 不回應時整個 group 停止通知**，同 receiver 的 webhook 也收不到，counter 不動，到 0.34.2 仍是如此。<span class="at-chip at-ev-tested">實測</span>[^exp-am-smtp]

## 通知流程 {#pipeline}

每次 flush 依序經過：gossip settle → inhibit → 時間區間 → silence → wait（HA 順位） → dedup（查 nflog） → retry → 寫入 nflog。[^am-pipeline][^am-wait][^am-dedup][^am-retry]

- 同一個 receiver 內的多個 integration 並行送出。[^am-fanout]
- 可重試的錯誤（5xx、連線失敗）會一直重試到這次 flush 逾時；4xx 不重試，等下一輪。<span class="at-chip at-ev-tested">實測</span>[^exp-am-delivery]
- `notification_requests_failed_total` 每次嘗試都加，適合當早期訊號；`notifications_failed_total` 只在一整輪都沒送成時加。

## 健康檢查 {#health}

`/-/healthy` 和 `/-/ready` 是寫死的，receiver 全部失敗時也回 200。<span class="at-chip at-ev-tested">實測</span>[^am-healthy][^exp-am-delivery] 所以 Alertmanager 的監控要看通知失敗計數，加上外部的 dead man's switch，不能只靠存活探測。

## 重啟與 reload {#restart}

| 狀態 | 存放位置 | 重啟後 |
|---|---|---|
| Alert | 只在記憶體 | 消失，靠 ruler 定期重送補回 |
| Aggregation group 與計時器 | 只在記憶體 | 重建，`group_wait` 重新計算 |
| nflog | 記憶體加快照 | 從快照恢復，不會重送 <span class="at-chip at-ev-tested">實測</span>[^exp-am-ha] |
| Silence | 記憶體加快照 | 從快照恢復；單機當機時，最後一次快照之後的變更會遺失 |

Reload 時設定有效就停掉舊的 dispatcher 和 inhibitor，進行中的通知被取消（`reason="contextCanceled"`）；設定無效時回 500，繼續跑舊設定。<span class="at-chip at-ev-tested">實測</span>[^am-reload][^exp-am-ha]

## 高可用 {#ha}

- **每個 ruler 都要直接送給每一台 Alertmanager。**Alert 本身不會 gossip，中間不要放負載平衡。
- 每台都跑完整流程，靠等待和 nflog 去重：第 N 順位先等 N × `--cluster.peer-timeout`（預設 15s）再查 nflog。[^am-peertimeout][^am-wait]
- 它偏向重複，不偏向遺漏。Gossip 中斷時各台都會送，叢集分裂的症狀通常是重複通知。
