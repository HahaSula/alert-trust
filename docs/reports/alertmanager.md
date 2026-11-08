# Alertmanager 監控指南

Alertmanager 的架構、失敗時哪些 counter 會動、`group_interval` 對告警視窗的影響、需要設定的告警，以及到 0.34.2 的版本 survey 與升級注意事項。

!!! info "範圍與限制"
    以 0.27.0、0.29.0 原始碼為準，比對到 0.34.2（2026-10-29）。實測用官方映像檔，每項只跑一次，規模很小、`group_interval` 多半設 15s，未測高負載。Email 用假 SMTP，真實伺服器的行為可能不同。**數字來自單機小型實驗，用來說明機制，不能直接當門檻。**

<span class="at-chip at-ev-tested">實測</span> 用官方映像檔或 promtool 驗證　<span class="at-chip at-ev-code">讀碼</span> 由原始碼確認，未實測

## 結論

**Alertmanager 活著，不代表通知送得到。**三種 receiver 持續失敗時，`/-/healthy` 和 `/-/ready` 都回 200，這兩個端點是寫死的。[^am-healthy][^exp-am-delivery]要順著 alert 的路徑，分段看接收、抑制、送達和狀態。

**0.27.0 的 email 可能失敗卻記成成功。**假 SMTP 在 DATA 結尾回 554 時，0.27.0 的失敗 counter 全是 0、log 沒有錯誤，觀察期間也沒有重試。0.29.0 則正確計入失敗並持續重試。[^exp-am-delivery]這在 0.27.0 無法靠 metric 發現，要升級，或用外部檢查信箱的 canary。

**長 `group_interval` 會拉長偵測時間。**失敗是整輪 flush 結束才計數。以實驗的 30m 為例，`rate(...[5m]) > 0` 加 `for: 5m` 的告警在單一 group 持續失敗時完全不會觸發。[^exp-am-window]告警視窗要取 `group_interval` 的 2 倍，Watchdog 和 canary 要有自己的短週期路由。見[告警視窗契約](../contracts.md#am-failure-window)。

**升到 0.34.2 前要先處理 3 項。**設定檔格式相容，實測 0.27、0.29 升級再回退都正常。[^exp-am-ha]但子路由的 `group_by: []` 從 0.31 起語意改變且語法檢查抓不到，`auto-gomaxprocs` 會讓 0.34.2 啟動失敗，CORS 預設關閉。[^exp-am-compat]

## 1. 架構

Alertmanager 是單一行程，主要工作是把 alert 分組、決定要不要通知，再送到接收端。下圖是一條 alert 的路徑。更細的說明見 [Alertmanager 架構](../architecture/alertmanager.md)。

<!-- figure: alertmanager-process -->

**圖**　除了 ruler 和接收端，其餘都在同一個行程內。實線是資料流，虛線是查詢。上排紅框的 alert store 是全域瓶頸：寫入時持鎖送進每個訂閱者的 channel。[^am-mem-put][^am-mem-chan][^am-aggr-run][^am-pipeline]
{: .at-caption }

### 每個元件怎麼執行

| 元件 | 執行方式 | 卡住時影響誰 | 0.27／0.29 到 0.34.2 的變化 |
|---|---|---|---|
| API | 每個 request 一條 goroutine。GET 有併發上限，POST 沒有 | GET 超限回 503；POST 跟著 store 變慢 | 0.30 起 `/api/v2` 有延遲 metric |
| Alert store | 全域一把鎖；`Put` 持鎖寫入 dispatcher 和 inhibitor 的 channel[^am-mem-put] | 全部接收。Channel 滿了 POST 會阻塞 | 結構不變，0.30 起有 channel 寫入次數 |
| Dispatcher | 0.31 以前一條 goroutine；0.32 起 `min(max(GOMAXPROCS/2, 2), 8)` 條 worker | 新 alert 的分組；再反壓到 store | 0.32 的改動帶來過回歸，見第 6 節 |
| Aggregation group | 每個 group 一條 goroutine，flush 依序執行，逾時為 `group_interval` 加等待[^am-aggr-run] | 只影響該 group | 相同 |
| 通知流程 | Integration 之間並行；可重試的錯誤會一直重試到逾時[^am-fanout][^am-retry] | 只影響該 group 的該接收端 | 相同，階段順序不變 |
| Inhibitor | 一條 goroutine 維護 source alert 快取，`Mutes` 查詢它 | 也是 store 的訂閱者，慢了同樣反壓 | 0.30 改用索引實作；0.34.1 修掉不該解除抑制的問題 |
| Silences、nflog | 記憶體內狀態，定期 GC 並寫快照 | 查詢與寫入共用各自的鎖 | 0.32 起支援多組 matcher 的 silence |
| Cluster | memberlist gossip，只同步 silence 和 nflog | 同步中斷時可能重複通知 | 0.30 起可固定 peer 名稱 |

以上為 <span class="at-chip at-ev-code">讀碼</span>。

### 重啟與 reload 時發生什麼

| 狀態 | 存放位置 | 重啟後 |
|---|---|---|
| Alert | 只在記憶體 | 消失，靠 Prometheus 定期重送補回 |
| Aggregation group 與計時器 | 只在記憶體 | 重建，`group_wait` 重新計算；舊 alert 可能立即 flush |
| nflog | 記憶體加快照 | 從快照恢復。實測升版重啟後不會重送 <span class="at-chip at-ev-tested">實測</span>[^exp-am-ha] |
| Silence | 記憶體加快照 | 從快照恢復。單機當機時，最後一次快照之後的變更會遺失 <span class="at-chip at-ev-code">讀碼</span> |

**Reload：**設定有效時會停掉舊的 dispatcher 和 inhibitor，再從 store 重新載入 alert，進行中的通知被取消，計入 `reason="contextCanceled"`。設定無效時回 500，繼續跑舊設定。<span class="at-chip at-ev-tested">實測</span>[^am-reload][^exp-am-ha] 0.30 起會等新元件載入完成，避免 reload 時的競態（#4705）。<span class="at-chip at-ev-code">讀碼</span>

### 高可用怎麼運作

- **Prometheus 要送給每個實例。**Alert 本身不會 gossip，所以 Prometheus 和 Alertmanager 之間不要放負載平衡。
- **每個實例都跑完整流程，靠等待和 nflog 去重。**第 N 順位先等 N × `--cluster.peer-timeout`（預設 15s），再查 nflog 決定要不要送。接收端回應比這段等待慢時，就可能重複通知。<span class="at-chip at-ev-code">讀碼</span>[^am-wait][^am-peertimeout][^am-dedup]
- **它偏向重複，不偏向遺漏。**Gossip 中斷時各實例都會送，所以叢集分裂的症狀通常是重複通知，不是收不到。<span class="at-chip at-ev-code">讀碼</span>

## 2. 順著路徑看

```text
POST /api/v2/alerts → provider/mem → dispatcher → aggregation group → gossip settle → inhibit
  → 時間區間 → silence → wait → dedup → retry → 寫入 nflog
```

| 階段 | 回答的問題 | 看什麼 | 盲點 |
|---|---|---|---|
| 接收 | Prometheus 送得進來嗎 | `prometheus_notifications_errors_total`、`prometheus_notifications_dropped_total`、`prometheus_notifications_latency_seconds`、`alertmanager_alerts_received_total`、`alertmanager_alerts_invalid_total` | 0.29 以前 `/api/v2` 沒有延遲 metric，要從 Prometheus 端看；0.30 起有 <span class="at-chip at-ev-tested">實測</span> |
| 分組 | Dispatcher 跟得上嗎 | `alertmanager_dispatcher_aggregation_groups`、`alertmanager_dispatcher_alert_processing_duration_seconds` | Channel 長度 200，滿了會反壓 POST <span class="at-chip at-ev-code">讀碼</span>[^am-mem-chan]。0.30 起只有寫入次數，仍看不到佔用率 |
| 抑制 | 是不是被擋掉 | `alertmanager_notifications_suppressed_total{reason}`、`alertmanager_alerts{state}`、`alertmanager_silences{state}` | 沒有 per-route metric，路由錯誤看不到。要用 `amtool config routes test` |
| 送達 | 接收端收下了嗎 | `alertmanager_notification_requests_failed_total`（每次嘗試）、`alertmanager_notifications_failed_total{reason}`（整輪）、`alertmanager_notification_latency_seconds` | 沒有 alert 就沒有嘗試，counter 不動。2xx 或 SMTP 250 不代表有人看到 |
| HA | 叢集一致嗎 | `alertmanager_cluster_members`、`alertmanager_peer_position`、`alertmanager_config_hash` | 非第一順位實例的成功數可以是 0，屬正常 <span class="at-chip at-ev-tested">實測</span>[^exp-am-ha] |
| 狀態 | 重啟會遺失什麼 | `alertmanager_nflog_maintenance_errors_total`、`alertmanager_silences_maintenance_errors_total`、`alertmanager_config_last_reload_successful` | Alert 不落盤，重啟後靠 Prometheus 重送。nflog 和 silence 預設每 15 分鐘寫一次快照 <span class="at-chip at-ev-code">讀碼</span> |

## 3. 失敗時，哪個 counter 會動

### Webhook：三種失敗

兩個實例組成叢集，`group_interval` 15s，每種失敗各 1 個 group，觀察 75 秒。<span class="at-chip at-ev-tested">實測</span>[^exp-am-delivery]

| 接收端回應 | reason | 行為 | 接收端收到 |
|---|---|---|---|
| HTTP 400 | `clientError` | 不重試，等下一輪 flush | 10 |
| HTTP 500 | `serverError` | 整輪持續重試 | 65 |
| 連線被拒 | `other` | 整輪持續重試 | 未記錄 |

同期間 `notification_requests_failed_total` 是 79／61，`notifications_failed_total` 只有 13／11（0.27.0／0.29.0）。前者每次嘗試都加，適合早期預警；後者代表一整輪都沒送成。0.34 起 401、403 計為 `authError`，429 計為 `rateLimited`。<span class="at-chip at-ev-tested">實測</span>[^exp-am-delivery][^am-retry]

### Email：SMTP 在 DATA 結尾拒收

假 SMTP 接受 MAIL、RCPT 和 DATA，最後回 `554`。單一實例，`group_interval` 15s，觀察約 65 秒。<span class="at-chip at-ev-tested">實測</span>[^exp-am-delivery]

| 項目 | 0.27.0 | 0.29.0 |
|---|---|---|
| SMTP 拒收次數 | 1，之後沒有重試 | 35 |
| `notification_requests_failed_total` | **0** | 35 |
| `notifications_failed_total` | **0** | 4（reason=other） |
| Log | **沒有相關錯誤** | `delivery failure: 554 5.7.1 ...` |

0.27.0 用 `defer message.Close()`，忽略了 DATA 結尾的回應[^am027-email-defer]（[#4006](https://github.com/prometheus/alertmanager/pull/4006)，0.28 修正[^am029-email-close]）。通知被當成成功並寫入 nflog，所以下一輪 dedup 不會再送，要等 `repeat_interval` 才會再試 <span class="at-chip at-ev-code">讀碼</span>[^am-dedup]。0.34.2 的行為與 0.29.0 相同 <span class="at-chip at-ev-tested">實測</span>。這只涵蓋 DATA 結尾拒收；SMTP 回 250 之後的退信，兩個版本都看不到。

## 4. 長 `group_interval` 帶來什麼

下表用 G 代表 `group_interval`。實驗用 G = 30m，是刻意選的長週期，用來把效果放大。

| 情況 | 影響 | 處理 |
|---|---|---|
| 可重試的失敗（5xx、連線失敗、SMTP 暫時錯誤） | `notifications_failed_total` 最晚 G 之後才加 1 | 用 `notification_requests_failed_total` 當早期訊號 |
| 不重試的失敗（4xx） | 一輪只試一次，下一次在 G 之後，重試 counter 也是這個頻率 | 告警視窗要大於 G |
| 已有通知紀錄的 group 來了新 alert | 要等下一輪 flush，最多 G <span class="at-chip at-ev-code">讀碼</span>[^am-aggr-run] | 需要快速通知的 alert 拆到較短的路由 |
| Watchdog 繼承主路由 | 重送週期會落在 G 的倍數上 <span class="at-chip at-ev-code">讀碼</span> | 給它獨立的短週期路由 |
| `repeat_interval`（例如 4h） | 只在 flush 時判斷，實際約 4h 到 4h + G <span class="at-chip at-ev-code">讀碼</span> | Dead man's switch 的等待時間要算進去，見[等待時間契約](../contracts.md#deadman-wait) |

### 告警視窗實測

模擬單一 group 持續失敗，counter 每 30 分鐘加 1，評估 151 個時間點（90–240 分鐘，每分鐘一點）。<span class="at-chip at-ev-tested">實測</span>[^exp-am-window]

| 寫法 | Prometheus 2.51.2 | Prometheus 3.13.5 |
|---|---|---|
| `rate(...[5m]) > 0` 加 `for: 5m` | **0／151** | **0／151** |
| `increase(...[30m]) > 0` | 151／151 | 151／151 |
| `increase(...[1h]) > 0`（建議，2 × G） | 151／151 | 151／151 |

`[30m]` 剛好等於失敗週期，落在邊界上。這次只測整分鐘的時間點，實際 scrape 有抖動，在 3.x 的左開區間下也可能短暫歸零，所以建議用 2 倍的視窗。代價是恢復後告警最多再持續約 2 × G。多個 group 交錯失敗時，短視窗的寫法仍可能觸發，所以它最容易漏掉的就是只有一個 group 在失敗的時候。

## 5. 需要設定的告警

共 21 條加 2 條 canary，下表依「壞掉時你會少知道什麼」分組。放在獨立的外部 Prometheus，通知走另一條路，否則 Alertmanager 壞掉時也發不出來。

| 要防的事 | 告警 | 等級 | 故障注入 |
|---|---|---|---|
| 通知送不出去 | `AlertmanagerNotificationsFailing` | critical | counter 有動 |
| | `AlertmanagerAllInstancesFailing` | critical | counter 有動 |
| | `AlertmanagerNotificationAttemptsFailing` | warning | counter 有動 |
| Prometheus 送不進來 | `PrometheusNoAlertmanagerDiscovered` | critical | 未注入 |
| | `PrometheusAlertmanagerNotificationsFailing` | critical | 未注入 |
| | `PrometheusNotificationsDropped` | critical | 未注入 |
| | `PrometheusNotificationQueueNearFull` | warning | 未注入 |
| 行程或叢集壞了 | `AlertmanagerScrapeDown` | critical | 未注入 |
| | `AlertmanagerScrapeMissing` | critical | 未注入 |
| | `AlertmanagerClusterDown` | critical | 未注入 |
| | `AlertmanagerCrashLooping` | critical | 未注入 |
| | `AlertmanagerClusterFailedPeers` | warning | 停 peer 後為 1 |
| | `AlertmanagerMembersInconsistent` | critical | 未注入 |
| 設定或狀態出錯 | `AlertmanagerConfigReloadFailed` | critical | 壞設定後為 0 |
| | `AlertmanagerConfigInconsistent` | critical | 未注入 |
| | `AlertmanagerStateSnapshotFailing` | warning | 刪資料目錄後增加 |
| | `AlertmanagerInvalidAlerts` | warning | 送無效 alert 後增加 |
| 接收變慢（0.30+） | `AlertmanagerAlertPostSlow` | warning | 未注入 |
| Silence 異常（0.30+） | `AlertmanagerSilenceGCErrors` | warning | 未注入 |
| Alert 被丟棄（0.31+） | `AlertmanagerAlertsLimited` | critical | 未注入 |
| 分組放棄（0.33+） | `AlertmanagerGroupCreationGivenUp` | critical | 未注入 |
| 端到端，metric 看不到的部分 | `Watchdog` | 外部判斷 | 未注入 |
| | `CanaryEmail` | 外部判斷 | 0.27 假成功的唯一訊號 |

故障注入在 0.27.0、0.29.0、0.34.2 各做一次，結果相同。21 條規則都用 `promtool test rules` 在模擬資料上觸發過（Prometheus 2.51.2、3.13.5）；門檻仍要依環境調整。<span class="at-chip at-ev-tested">實測</span>[^exp-am-rules]

### 設定時要注意

- **只靠 `AlertmanagerMembersInconsistent` 抓不到 peer 停機。**實測停掉一台後，存活的實例回報 `cluster_members=1`，停掉的那台又抓不到，兩邊數字相等不會觸發。這條抓的是叢集分裂，peer 停機要靠 `AlertmanagerClusterFailedPeers` 和 `AlertmanagerScrapeDown`。<span class="at-chip at-ev-tested">實測</span>[^exp-am-rules]
- **視窗要跟著 `group_interval`。**下方範例以 G = 30m 寫成 `[1h]`；有路由的 G 更長時，視窗也要一起拉長。
- **不要用 `reason="clientError"` 篩選。**0.34 起 401、403 改成 `authError`，429 改成 `rateLimited`。<span class="at-chip at-ev-tested">實測</span>[^exp-am-delivery]
- **Email 失敗的 reason 是 `other`，**SMTP 錯誤沒有 HTTP 狀態碼。<span class="at-chip at-ev-tested">實測</span>[^exp-am-delivery]

### 主要規則（所有版本）

放在外部 Prometheus。`prometheus-to-alertmanager` 這組要抓得到每台受測 Prometheus 的 metric。

```yaml
# 範例視窗以 group_interval 30m 計算（[1h] = 2 × G）。cluster、job 的 label 請對齊實際環境。
groups:
  - name: alertmanager-delivery            # 通知有沒有送出去
    rules:
      - alert: AlertmanagerNotificationsFailing
        expr: sum by (cluster, instance, integration) (increase(alertmanager_notifications_failed_total{reason!="contextCanceled"}[1h])) > 0
        labels: { severity: critical }
        annotations: { summary: "至少一輪通知完全失敗；視窗是 group_interval 的 2 倍" }
      - alert: AlertmanagerAllInstancesFailing
        expr: |
          min by (cluster, integration) (
            sum by (cluster, instance, integration) (increase(alertmanager_notifications_failed_total{reason!="contextCanceled"}[1h]))
          ) > 0
        labels: { severity: critical }
        annotations: { summary: "叢集內每個實例都有失敗，通知很可能完全送不出去" }
      - alert: AlertmanagerNotificationAttemptsFailing
        expr: increase(alertmanager_notification_requests_failed_total[15m]) > 0
        for: 5m
        labels: { severity: warning }
        annotations: { summary: "重試中的早期訊號，不必等整輪 flush 結束" }
  - name: alertmanager-availability        # 行程與叢集有沒有活著
    rules:
      - alert: AlertmanagerScrapeDown
        expr: up{job=~".*alertmanager.*"} == 0
        for: 5m
        labels: { severity: critical }
        annotations: { summary: "/metrics 抓不到，行程可能停機或卡住" }
      - alert: AlertmanagerScrapeMissing
        expr: absent(up{job=~".*alertmanager.*"})
        for: 10m
        labels: { severity: critical }
        annotations: { summary: "完全沒有 Alertmanager 的 target，服務發現或 job 名稱可能錯了" }
      - alert: AlertmanagerClusterDown
        expr: |
          count by (cluster) (avg_over_time(up{job=~".*alertmanager.*"}[5m]) < 0.5)
            / count by (cluster) (up{job=~".*alertmanager.*"}) >= 0.5
        for: 5m
        labels: { severity: critical }
        annotations: { summary: "一半以上的實例停機" }
      - alert: AlertmanagerCrashLooping
        expr: changes(process_start_time_seconds{job=~".*alertmanager.*"}[15m]) > 2
        labels: { severity: critical }
        annotations: { summary: "15 分鐘內重啟超過 2 次；探測多半看不出來" }
      - alert: AlertmanagerClusterFailedPeers
        expr: max_over_time(alertmanager_cluster_failed_peers[5m]) > 0
        for: 15m
        labels: { severity: warning }
        annotations: { summary: "有 peer 失聯，可能重複通知或失去備援" }
      - alert: AlertmanagerMembersInconsistent
        expr: |
          max_over_time(alertmanager_cluster_members[5m])
            < on (cluster) group_left
          count by (cluster) (max_over_time(alertmanager_cluster_members[5m]))
        for: 15m
        labels: { severity: critical }
        annotations: { summary: "活著的實例彼此看不到，叢集可能已分裂" }
  - name: alertmanager-state               # 設定與落盤狀態
    rules:
      - alert: AlertmanagerConfigReloadFailed
        expr: max_over_time(alertmanager_config_last_reload_successful[5m]) == 0
        for: 10m
        labels: { severity: critical }
        annotations: { summary: "新設定沒有生效，仍在跑舊設定" }
      - alert: AlertmanagerConfigInconsistent
        expr: count by (cluster) (count_values by (cluster) ("config_hash", alertmanager_config_hash)) != 1
        for: 20m
        labels: { severity: critical }
        annotations: { summary: "同一叢集的實例設定不一致" }
      - alert: AlertmanagerStateSnapshotFailing
        expr: increase(alertmanager_nflog_maintenance_errors_total[1h]) > 0 or increase(alertmanager_silences_maintenance_errors_total[1h]) > 0
        labels: { severity: warning }
        annotations: { summary: "nflog 或 silence 落盤失敗，重啟可能遺失狀態" }
      - alert: AlertmanagerInvalidAlerts
        expr: increase(alertmanager_alerts_invalid_total[1h]) > 0
        labels: { severity: warning }
        annotations: { summary: "收到無效 alert，Prometheus 端會看到 400" }
  - name: prometheus-to-alertmanager       # 放在 Prometheus 端的訊號，每台受測 Prometheus 都要有
    rules:
      - alert: PrometheusNoAlertmanagerDiscovered
        expr: prometheus_notifications_alertmanagers_discovered < 1
        for: 5m
        labels: { severity: critical }
        annotations: { summary: "Prometheus 沒有可用的 Alertmanager" }
      - alert: PrometheusAlertmanagerNotificationsFailing
        expr: rate(prometheus_notifications_errors_total[15m]) > 0
        for: 5m
        labels: { severity: critical }
        annotations: { summary: "Prometheus 送往 Alertmanager 持續失敗" }
      - alert: PrometheusNotificationsDropped
        expr: increase(prometheus_notifications_dropped_total[15m]) > 0
        labels: { severity: critical }
        annotations: { summary: "Prometheus 已丟棄通知" }
      - alert: PrometheusNotificationQueueNearFull
        expr: prometheus_notifications_queue_length / prometheus_notifications_queue_capacity > 0.8
        for: 5m
        labels: { severity: warning }
        annotations: { summary: "通知佇列快滿，滿了會丟最舊的" }
```

### 升級後才有的規則

舊版本沒有這些 metric，不會誤報，但也不會響。

```yaml
# 升級後才有的 metric；舊版本不會有資料，加上也不會誤報，但也不會響。
groups:
  - name: alertmanager-newer-versions
    rules:
      - alert: AlertmanagerAlertPostSlow            # 0.30+
        expr: |
          histogram_quantile(0.99, sum by (cluster, instance, le) (
            rate(alertmanager_http_request_duration_seconds_bucket{handler="/api/v2/alerts",method="post"}[5m])
          )) > 1
        for: 10m
        labels: { severity: warning }
        annotations: { summary: "POST /api/v2/alerts 變慢，可能是 channel 反壓" }
      - alert: AlertmanagerSilenceGCErrors          # 0.30+
        expr: increase(alertmanager_silences_gc_errors_total[1h]) > 0
        labels: { severity: warning }
        annotations: { summary: "Silence GC 遇到錯誤，有 silence 被移除" }
      - alert: AlertmanagerAlertsLimited            # 0.31+，需設定 --alerts.per-alertname-limit
        expr: increase(alertmanager_alerts_limited_total[15m]) > 0
        labels: { severity: critical }
        annotations: { summary: "Alert 超過 per-alertname 上限，已被丟棄" }
      - alert: AlertmanagerGroupCreationGivenUp     # 0.33+
        expr: increase(alertmanager_dispatcher_aggregation_group_creation_given_up_total[1h]) > 0
        labels: { severity: critical }
        annotations: { summary: "建立 aggregation group 重試過多後放棄，alert 可能遺失" }
```

### Canary 規則與路由

路由通過 0.27.0、0.29.0、0.34.2 `amtool check-config`，沒有在正式環境跑過。

```yaml
# 放在每台受測 Prometheus。路由見下方 Alertmanager 設定。
groups:
  - name: alertmanager-canary
    rules:
      - alert: Watchdog
        expr: vector(1)
        labels: { severity: none }
        annotations: { summary: "送到外部 dead man's switch，停止抵達就要告警" }
      - alert: CanaryEmail
        expr: vector(1)
        for: 2m
        labels: { severity: none }
        annotations: { summary: "走真實 SMTP 路徑到專用信箱，由外部程式檢查最新一封的時間" }
```

Canary 路由要有自己的短週期，不要繼承主路由的長週期。下面的主路由以 G = 30m 為例。

```yaml
# 範例設定。Canary 路由各自設定短週期，不要繼承主路由的 group_interval。
global:
  smtp_smarthost: 'smtp.example.test:587'
  smtp_from: 'alertmanager@example.test'
route:
  receiver: default-email
  group_by: [alertname]
  group_wait: 30s
  group_interval: 30m
  repeat_interval: 4h
  routes:
    - matchers: ['alertname="Watchdog"']
      receiver: external-deadman
      group_wait: 0s
      group_interval: 1m
      repeat_interval: 5m
    - matchers: ['alertname="CanaryEmail"']
      receiver: canary-mailbox
      group_wait: 0s
      group_interval: 5m
      repeat_interval: 1h
receivers:
  - name: default-email
    email_configs: [{to: 'oncall@example.test'}]
  - name: external-deadman
    webhook_configs: [{url: 'https://deadman.example.test/ping'}]
  - name: canary-mailbox          # 走同一個 SMTP 路徑，由外部檢查信箱最新一封的時間
    email_configs: [{to: 'am-canary@example.test'}]
```

通知走 email 時，信箱要由 Alertmanager 以外的程式檢查，例如超過約 2 小時沒有新信就告警。這也能抓到 SMTP 收下之後才退信的情況，任何版本都建議保留。

## 6. 版本 survey 與相容性

涵蓋到 0.34.2（2026-10-29）。**設定檔格式確實相容，新版只新增欄位。但相容不等於行為相同，下面 3 項會在升級時出問題，其中 `group_by: []` 通過語法檢查卻會改變行為。**

### 設定檔格式

| 測試 | 結果 | 證據 |
|---|---|---|
| 0.27.0 與 0.29.0 的 31 個 `config/testdata` 樣本，跑 0.27.0、0.28.1、0.29.0、0.30.1、0.31.1、0.33.1、0.34.2 | 沒有任何檔案從通過變成失敗。只有 3 個從失敗變通過（Rocket.Chat 兩個、UTF-8 inhibit `equal` 一個，都在 0.28 起支援） | <span class="at-chip at-ev-tested">實測</span>[^exp-am-compat] |
| 舊寫法設定：`match`、`match_re`、`source_match`、`target_match`、`mute_time_intervals`、`time_intervals`、email、`inhibit_rules` | 同樣 7 個版本全部通過，舊欄位還沒移除 | <span class="at-chip at-ev-tested">實測</span>[^exp-am-compat] |
| 預設 email 模板 | `template/default.tmpl` 從 0.27 到 0.34.2 只有新增，既有定義沒變 | <span class="at-chip at-ev-code">讀碼</span> |
| SMTP 連線行為 | 沒設 `force_implicit_tls` 時，port 465 仍走 implicit TLS。不同的是 0.31 起不再另外要求 STARTTLS，所以 `require_tls: true` 從失敗變成功，詳見下方 Email 小節 | <span class="at-chip at-ev-tested">實測</span>[^exp-am-compat] |
| 反方向：用新欄位後回退 | 舊版不認得新欄位就會拒絕。回退時設定要一起退 | <span class="at-chip at-ev-tested">實測</span>（0.27 拒絕 Rocket.Chat 樣本）[^exp-am-compat] |

### 會出問題的 3 項

| 變更 | 從哪版開始 | 實測結果 | 怎麼處理 |
|---|---|---|---|
| **子路由 `group_by: []` 語意改變**（#4825） | 0.31 | 兩條不同 `alertname` 的 alert：0.27、0.29 繼承父路由，分成 2 組；0.34.2 **合成 1 組**。`check-config` 都通過 | 先搜尋設定。想繼承就刪掉這行 |
| **移除 `--enable-feature=auto-gomaxprocs`** | 0.33 | 0.29 正常啟動；0.34.2 **啟動失敗**，exit 1。0.27 也不認得這個值 | 先從啟動參數拿掉，0.29 起它已沒有作用 |
| **API 預設不送 CORS header**（#5664） | 0.34.2 | 0.29 回 `Access-Control-Allow-Origin: *`；0.34.2 **沒有這個 header** | 只影響從其他網域直接呼叫 API 的瀏覽器頁面，需要就設 `--web.cors.origin` |

<span class="at-chip at-ev-tested">實測</span>[^exp-am-compat]

Metric 只需注意兩項：`alertmanager_marked_alerts` 在 0.33 移除；0.34 起 `reason` 多了 `authError`、`rateLimited`。其餘都是新增，第 5 節的主要規則用到的 metric 在 3 個版本都有。<span class="at-chip at-ev-tested">實測</span> Log 格式從 0.28 起改成 `time=… level=ERROR`。<span class="at-chip at-ev-tested">實測</span>[^exp-am-compat]

### 資料與叢集

| 情境 | 結果（各跑一次） |
|---|---|
| 0.27.0 → 0.34.2 → 0.27.0，保留資料目錄 | Silence 都在，沒有重複通知，log 沒有錯誤 |
| 0.29.0 → 0.34.2 → 0.29.0，保留資料目錄 | 同上 |
| 滾動升級中的混版叢集：0.27.0 + 0.34.2、0.29.0 + 0.34.2 | 可組成叢集，通知去重正常，silence 雙向同步 |

只測了單一 regex silence。多組 matcher 或帶 annotation 的 silence（0.32 新功能）回退時的行為沒有測，升級穩定前不要使用。<span class="at-chip at-ev-tested">實測</span>[^exp-am-ha]

### 修掉的 bug

以下來自 changelog 與 commit，只挑會影響通知可信度的項目。

| 修正版本 | 問題 | 受影響的版本 |
|---|---|---|
| 0.28.0 | #4006 SMTP 收尾錯誤被忽略，失敗記成成功 <span class="at-chip at-ev-tested">實測</span> | 0.27 以前 |
| 0.28.0 | #3715 alert store 可能互鎖卡死 | 0.27 以前 |
| 0.28.0 | #3826 再次 firing 的 alert 可能被刪除 | 0.27 以前 |
| 0.28.0 | #3930 更新 silence 造成記憶體洩漏 | 0.27 以前 |
| 0.30.0 | #4705 reload 時的競態 | 0.29 以前 |
| 0.30.0 | #4438 marker 狀態從 aggregation group 洩漏 | 0.29 以前 |
| 0.30.0 | #4343 解析 peer 沒有逾時 | 0.29 以前，有用 HA 時 |
| 0.31.0 | #4825 空的 `group_by` 無法覆寫父路由 | 修正後語意改變，見上表 |
| 0.34.1 | #5542、#5559 inhibitor 不該解除抑制卻解除 | 0.30–0.34.0（0.30 改用索引，由此推論） |
| 0.34.2 | #5664 CORS；Go 1.26.10 更新憑證信任（CVE-2026-111943） | 0.34.1 以前 |

### Email：後續版本修了什麼

**只修了一部分。**0.27 的「失敗記成成功」在 0.28 修掉，port 465 的 `require_tls` 在 0.31 修掉。但 SMTP 卡住、模板錯誤仍寄出空白信，到最新的 0.34.2 都還在，升級解決不了。<span class="at-chip at-ev-tested">實測</span>[^exp-am-smtp]

| 問題 | 0.27.0 | 0.29.0 | 0.34.2 | 上游狀態（2026-11-06） |
|---|---|---|---|---|
| DATA 結尾被拒收卻記成成功 | **有** | 已修 | 已修 | 0.28 修正（#4006） |
| Port 465 加 `require_tls: true` 發送失敗（伺服器未宣告 STARTTLS） | **失敗** | **失敗** | 成功 | 0.31 修正（#4818）。`require_tls: false` 三版都成功 |
| **SMTP 不回應時，整個 group 停止通知** | **有** | **有** | **有** | 未修。PR #5695、#5690 尚未合併 |
| **模板執行失敗仍寄出空白信** | **有** | **有** | **有** | 0.34.2 仍有 |
| RCPT 回 550 永久錯誤仍持續重試（30 秒 19 次） | 有 | 有 | 有 | 0.34.2 仍有 |
| SMTP 錯誤的 `reason` 都是 `other` | 有 | 有 | 有 | 未修。PR #5533 尚未合併 |

**SMTP 不回應。**假 SMTP 收完信後不回覆，`group_interval` 5s，三個版本的結果相同：

- **同 group 全停：**第二條 alert 進來 60 秒後仍沒有通知，同一個 receiver 的 webhook 也收不到。
- **看不到異常：**卡住期間 `notifications_failed_total` 沒有增加，`/-/healthy`、`/-/ready` 都回 200。
- **其他 group 不受影響：**其他 group 照常通知。
- **Reload 能解開：**reload 立刻回 200，webhook 馬上恢復，但 email 若連到同一台 SMTP，會再卡一次。
- **連線斷開後：**0.29、0.34.2 記為失敗（`reason="contextDeadlineExceeded"`）；0.27 把沒收下的信**記成成功**。

Email notifier 建立連線後沒有設 deadline，`group_interval` 導出的 flush timeout 中斷不了它。所以對方活著卻不回應時，應該會一直卡住（讀碼推論，實測只卡了約 90 秒）。`group_interval` 再長也一樣，而且指標不會動，只有**第 5 節的外部 canary 信箱檢查抓得到**。

**模板失敗。**Email notifier 先送出 DATA 才執行模板，失敗時 deferred close 會把未完成的信送出。三個版本的結果相同：

- **標頭模板失敗：**伺服器收到完全空白的信，沒有主旨。
- **內文模板失敗：**信有主旨，但沒有內文。
- **每次 flush 再寄一封：**失敗不會寫進 nflog，也不重試，所以每個 `group_interval` 寄一封（例如 G = 30m 時約每 30 分鐘一封），同時 `notifications_failed_total{reason="other"}` 會增加。
- **語法檢查抓不到：**`amtool check-config` 對引用不存在模板的設定回 SUCCESS，要在非正式環境實際發一次。

以上皆為假 SMTP 實測，每項跑一次；真實 SMTP、STARTTLS 路徑與 TCP 對端整個消失的情況沒有測。

### 該升到哪一版

**0.34.2 包含上面所有修正，也是實測過升級和回退的版本。**中途要避開這些：

| 版本 | 原因 |
|---|---|
| `0.30.0` | Tracing client 記憶體洩漏，0.30.1 修正（#4828） |
| `0.32.0`、`0.32.1` | 新 dispatcher 的回歸：壅塞路由派送問題（0.32.1 修）、goroutine 洩漏（0.32.2 修） |
| `0.32.x` 到 `0.32.2`、`0.33.0` | Silence 快照少了舊 matcher 欄位，舊版讀不了，會妨礙回退（0.32.3、0.33.1 修）。`v0.32.3` 在 Docker Hub 和 Quay 都找不到映像檔 |
| `0.30` 到 `0.34.0` | 有依賴 inhibit 的話，抑制可能失效，直接跳過 |

### 升級檢查清單

```bash
# 1. 用目標版本的 amtool 檢查實際設定（換成實際的檔名）
docker run --rm -v "$PWD":/w -w /w --entrypoint amtool \
  prom/alertmanager:v0.34.2 check-config alertmanager.yml

# 2. 找出語意會變的空 group_by
grep -nE 'group_by:[[:space:]]*\[[[:space:]]*\]' alertmanager.yml

# 3. 啟動參數要拿掉這個值
#    --enable-feature=auto-gomaxprocs

# 4. 找出要改的 dashboard、告警與 log 解析
#    alertmanager_marked_alerts、reason="clientError"、level=error
```

1. **備份**資料目錄的 `nflog` 和 `silences`。
2. **先升非正式環境，**用真實的 SMTP 和自訂模板發一次通知。這次的實驗沒有涵蓋自訂模板和實際設定。
3. **叢集逐台滾動，**每升一台確認 `cluster_members`、`config_hash`，以及 canary 信箱有收到新信。
4. **穩定後再用新功能，**新欄位一用，設定就無法直接退回舊版。

### 升級後值得開的功能

| 功能 | 版本 | 解決什麼 |
|---|---|---|
| `--dispatch.start-delay` | 0.30 | 重啟後等 Prometheus 重送完再派送，上游建議與 `--rules.alert.resend-delay` 對齊 |
| `--alerts.per-alertname-limit` | 0.31 | 防止單一 alertname 灌爆。設了要搭配 `AlertmanagerAlertsLimited`，因為超過就丟棄 |
| `auth_secret_file` | 0.31 | SMTP 密鑰不寫在設定檔 |
| `--cluster.peer-name` | 0.30 | K8s 重建 pod 時固定順位 |
| `--log.silences` | 0.32 | 留下 silence 的 log，查誰擋了通知 |

功能本身沒有實測，版本取自 changelog 與 flag 比對。

## 7. 原始碼閱讀順序

Repo 預設在 `main`，要先切出對應版本：

```bash
git worktree add ../am-0.27 v0.27.0
git worktree add ../am-0.29 v0.29.0
```

| 順序 | 檔案（行號為 v0.29.0） | 看什麼 |
|---|---|---|
| 1 | `cmd/alertmanager/main.go:398` | 元件組裝、reload、flush 逾時的計算[^am-reload] |
| 2 | `provider/mem/mem.go:219` | `Put` 持鎖寫 channel，dispatcher 和 inhibitor 都是訂閱者[^am-mem-put] |
| 3 | `dispatch/dispatch.go:439` | `aggrGroup.run` 和 `flush`，同 group 依序 flush，逾時等於 `group_interval` 加等待[^am-aggr-run] |
| 4 | `notify/notify.go:416` | 階段順序；`DedupStage`；`RetryStage`[^am-pipeline][^am-dedup][^am-retry] |
| 5 | `doc/alertmanager-mixin/alerts.libsonnet` | 上游告警，可參考用 `min by` 判斷整個叢集失敗 |

0.34.2 把元件組裝拆到 `app/`（reload 在 `app/reloader.go`），健康檢查移到 `httpserver/`，事件紀錄在 `eventrecorder/`。比對版本時可以用 `git diff v0.29.0 v0.34.2 -- dispatch/`。

## 8. 建議順序

1. **還在 0.27 而且用 email 的，先放外部檢查的 canary 信箱。**這是 0.27 假成功唯一的訊號。
2. **上線告警：**部署第 5 節的主要規則，把 `cluster` 和 `job` 換成實際的 label，視窗換成 2 × 最長的 `group_interval`。
3. **拆短週期路由：**Watchdog 和 canary 不要繼承主路由的長週期，再檢查 dead man's switch 的等待時間。
4. **驗證告警：**在非正式環境把 SMTP 指向會拒收的目標，確認真的會響。
5. **檢視 `group_interval` 是否合適：**確認 critical alert 能接受最多一個 `group_interval` 的新增通知延遲，不能就拆路由。
6. **升到 0.34.2：**照第 6 節的清單，版本越舊越優先，再加上升級後才有的規則。

## 限制與來源

實測用官方映像檔，每項只跑一次，`group_interval` 多半 15s，規模很小，未測高負載，也沒有用實際環境的設定和自訂模板。卡死、channel 反壓都只有讀碼，未重現。視窗與告警測試是理想化的模擬序列。Email 用假 SMTP，真實伺服器的行為可能不同。版本資訊以 repo tag 與映像檔倉庫為準，`v0.32.3` 沒有映像檔所以沒有測，上游之後可能有新版本。

相關頁面：[Prometheus Rule 信任邊際](prometheus-trust.md)、[Alertmanager 架構](../architecture/alertmanager.md)、[Alertmanager 元件卡](../components/alertmanager.md)。

- [#4006 close SMTP submission correctly to handle errors](https://github.com/prometheus/alertmanager/pull/4006)
- [#3715 avoid deadlock in the alerts store](https://github.com/prometheus/alertmanager/pull/3715)
- [#3826 fix race condition in dispatch.go](https://github.com/prometheus/alertmanager/pull/3826)
- [#4825 allow empty group_by to override parent route](https://github.com/prometheus/alertmanager/pull/4825)
- [#5664 add --web.cors.origin flag](https://github.com/prometheus/alertmanager/pull/5664)
- [#5542 update inhibitor index to store all matched alerts](https://github.com/prometheus/alertmanager/pull/5542)
- [Alertmanager CHANGELOG（到 0.34.2）](https://github.com/prometheus/alertmanager/blob/v0.34.2/CHANGELOG.md)
