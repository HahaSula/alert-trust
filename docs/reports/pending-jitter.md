# Alert Pending 抖動分析

`rate(M[5m]) > A` 在事後查詢時明顯成立，`ALERTS{alertname=…}` 卻一直在 pending 出現、消失，無法轉為 firing。這頁說明成因、各參數的時間關係、rate 與 irate 的差別、通知送出路徑、rule group 的執行模型，以及要監控什麼。

!!! info "範圍與限制"
    以 Prometheus v2.55.1（6d75691）與 v3.15.1（bae3ece）原始碼為依據，並在 2.51.2 重新確認刪除邏輯與 notifier 結構；真實 2.51.2 也重現了這個現象。Rule group 執行模型的實驗是把查詢換成固定耗時的假 `QueryFunc`，沒有真實 CPU 與 I/O 競爭。**數字來自單機小型實驗與公式推算，用來說明機制。**

<span class="at-chip at-ev-tested">實測</span> 實驗量到　<span class="at-chip at-ev-code">讀碼</span> 由原始碼確認　<span class="at-chip at-ev-inferred">推算</span> 由公式算出，未實測

!!! note "2.51 沒有 `query_offset`"
    `query_offset` 從 2.53 起才有。[^p255-query-offset]本文提到 `query_offset` 的地方，在 2.51 請改成表達式內的 offset，例如 `rate(M[10m] offset 2m)`，實測同樣有效。[^exp-prom-silent]

## 結論

**Scrape 一次比 `scrape_interval` 還久時，超過的 tick 被丟掉，實際間隔變長；sample 的時間戳記是 scrape 開始的時間，卻要等整次 scrape 結束才寫入 TSDB。**[^p-scrape-run][^p-scrape-report]

Rule 在 `ts` 查 `(ts−5m, ts]`。例如實際間隔是 120 秒、scrape 耗時 100 秒時，約三分之一的 evaluation 只看得到 1 個已寫入的 sample，`rate()` 回傳空結果。只要有一次空結果，pending 狀態就被整筆刪掉，`for` 的計時從頭開始。[^p-alert-delete]`for: 3m` 需要連續 7 次都成立，在這種情況下幾乎不可能。實測一條永遠成立的 alert 跑 25 分鐘沒有 firing，rule 失敗與跳過次數都是 0。[^exp-prom-silent]

事後再查同一個時間點時，所有 sample 都已寫入，結果自然 > A，所以看起來「應該要觸發」。

## 範例參數

下文的計算都用這組數字，是重現這個現象的實驗條件，不是建議值。

| 參數 | 例子 |
|---|---|
| `scrape_interval` S | 30s |
| Scrape 實際耗時 d | 約 100s |
| 實際 scrape 間隔 I | 120s |
| Rule interval E | 30s |
| Range R | 5m |
| `query_offset` O | 0 |
| `for` F | 3m |
| `resend-delay`（預設） | 1m |

## 1. 全貌：一個 alert 從 scrape 到 Alertmanager 經過哪些階段

每個階段都有可能讓 alert 失準。Pending 抖動出在第一段，但通知送出那一段也有自己的 queue 和逾時機制，同樣需要監控。

<!-- figure: alert-pipeline -->

**圖 1**　Alert 管線與每段的失效模式。Pending 抖動的根因在 scrape 與 rule 之間。
{: .at-caption }

## 2. 根因：scrape 比 interval 慢，rule 看到的資料不完整

`scrape_timeout` 必須 ≤ `scrape_interval`，設定檔載入時會檢查。[^p-cfg-timeout-check]但這個 timeout 只套用在 HTTP 抓取與讀取 body 的階段。之後的 parse、append、`Commit()` 不受 timeout 限制，target 很大或 Prometheus 忙碌時，整段可以遠超過 interval。[^p-scrape-report]

Scrape loop 用 Go 的 `time.Ticker`，處理不完的 tick 會被直接丟掉，所以實際間隔 I 約等於 ⌈d / S⌉ × S。[^p-scrape-run]每個 sample 的時間戳記是 scrape **開始**的時間，但整批 sample 要到 commit 時才查得到。

<!-- figure: scrape-window-timeline -->

**圖 2**　實際間隔 120 秒、scrape 耗時約 100 秒時，ts 落在 scrape 開始後 0–60 秒的 evaluation（A）看得到 2 點，落在 60–100 秒的（B）只看得到 1 點。綠點是 rule 執行時已 commit 的 sample，空心紅點是時間戳在窗口內但還沒寫入的 sample。加上 2 分鐘的 offset（C）後，窗口整個往前移到已 commit 的區段。[^p-scrape-report][^exp-prom-silent]
{: .at-caption }

下表是 S = 30s、R = 5m、沒有 offset 時，不同 scrape 耗時下窗口內看得到的 sample 數。<span class="at-chip at-ev-inferred">推算</span>

| Scrape 耗時 d | 實際間隔 I | 窗口內可見 sample 數 | `rate` 回傳空的比例 |
|---|---|---|---|
| ≤ 60s | 60s | 4–5 | 0 |
| 61–90s | 90s | 2–3 | 0，但只有 2 點時外推誤差大 |
| 91–120s | 120s | 1–2 | (d − 60) ÷ 120，約 26%–50%；d = 100s 時約三分之一 |

## 3. 狀態機：一次空結果就會讓 pending 從頭開始

每次 evaluation 結束時，結果中沒有出現的 alert 會被處理：如果它還在 pending，就從 `r.active` 刪除；下一次條件成立時會建立新的 alert，`ActiveAt` 用新的時間。[^p-alert-delete]同時，上一輪有、這一輪沒有的 `ALERTS` series 會被寫入 stale marker，所以在圖表上看起來就是 pending 斷斷續續。v2.55.1 與 v3.15.1 只有這一個刪除點，邏輯相同。

<!-- figure: pending-reset -->

**圖 3**　實際間隔 120 秒、rule interval 30 秒時，可見 sample 數依「成立、成立、空、空」循環。每次空結果都把 pending 刪掉，`ActiveAt` 跟著重設。d 在真實環境會浮動，循環不會這麼整齊，但只要連續 7 次裡有一次落空，結果就一樣。實驗中這條 alert 跑 25 分鐘重置 9 次，從未 firing。[^p-alert-delete][^exp-prom-silent]
{: .at-caption }

| 事件 | 對 pending 的影響 | 依據 |
|---|---|---|
| Evaluation 結果中沒有這個 alert（空結果或 ≤ A） | 刪除，計時歸零 | `rules/alerting.go:465`（v3：`:511`）[^p-alert-delete] |
| Evaluation 因為 group 太慢被跳過（iterations_missed） | 不影響，`ts − ActiveAt` 依牆鐘時間計算，只會讓 firing 延後 | `rules/group.go:262`[^p-group-missed] |
| Evaluation 查詢失敗（timeout、超過 max samples） | 不影響，rule 直接 return，不更新狀態 | `rules/group.go:517-529`[^p-eval-fail-return] |
| Series label 改變（pod 重啟、重新排程） | 舊 alert 被刪，新 alert 從頭計時 | fingerprint 依 label set 計算 |
| `for: 0s` | 不會出現 pending，第一次成立就直接 firing；改成 firing 與 resolved 交替 | `rules/alerting.go:480` |

表中行號以 v2.55.1 為準；腳註連到 2.51.2 的對應位置。

**關於「連續兩次 eval 被延遲就會取消 pending」：**兩個版本的原始碼裡都沒有這段邏輯。延遲和跳過都不會清除 pending，唯一會清除的情況是「evaluation 有跑完而且結果裡沒有這個 alert」。這個說法可能來自相關性：Prometheus 忙的時候 scrape 和 rule 會一起變慢，真正清掉 pending 的是 scrape 變慢造成的空結果。

## 4. 參數關係：要讓 rule 穩定觸發，各參數之間的條件

最右欄用上面的範例參數代入。完整的契約與符號見[信任契約](../contracts.md#range-window)。

| 條件 | 目的 | 用範例參數代入 |
|---|---|---|
| `d ≤ scrape_interval` | 根本條件。讓實際間隔等於設定值 | 不成立：100s > 30s |
| `O ≥ max(d)` | 讓 rule 只查已 commit 的資料，結果與事後查詢一致 | 不成立：0 < 100s，O = 2m 即可（2.51 寫在表達式內） |
| 沒有足夠 offset：`R ≥ (2 + k) × I + d`；`O ≥ d`：`R ≥ (2 + k) × I` | 窗口內至少 2 個可見 sample，還能容忍 k 次 scrape 失敗（實驗驗證[^exp-prom-range]） | 不成立：k = 0 時已需 2 × 120 + 100 = 340s > 5m。加 2m offset 後，k = 1 需要 6m、k = 3 需要 10m |
| `E ≤ R` | 窗口之間沒有空隙，不漏掉短暫尖峰 | 成立：30s ≤ 5m |
| Group `last_duration` < E | 避免跳過 evaluation、延後 firing | 依 group 而定 |
| 連續 `⌈F / E⌉ + 1` 次都成立 | 從 pending 轉為 firing 的門檻 | `for: 3m` → 7 次；`for: 5m` → 11 次 |

**v3 的 range selector 改成左開區間。**v2.55.1 取 `t >= mint`[^p255-range-closed]，v3.15.1 取 `t > mint`[^p3-range-open]。當 R 剛好是 I 的整數倍時，v3 會比 v2 少一個 sample。升級到 v3 之後，sample 很少的窗口更容易變成空結果。

## 5. rate 與 irate：在 scrape 延遲下誰比較穩 {#rate-irate}

兩個函式都至少要 2 個 sample，所以**在回傳空結果這件事上，irate 和 rate 一樣糟**。差別在於有值的時候怎麼算：

- **rate**（`extrapolatedRate`）：用窗口內第一點到最後一點的增量，再外推到窗口兩端。如果某一端與邊界的距離超過「平均間隔 × 1.1」，只外推半個間隔。scrape 延遲讓最後一個可見 sample 離窗口終點很遠，於是 rate 被系統性地算低。[^p-extrapolated]
- **irate**（`instantValue`）：只取窗口內**最後兩個** sample，增量除以兩點的實際時間差，不做外推。所以不會因為窗口末端缺資料而算低，但整個窗口只用到一個間隔的資料，雜訊大。range 只決定「往回找多遠來湊到兩點」。[^p-instantvalue]

<!-- figure: rate-vs-irate -->

**圖 4**　同一個窗口裡兩個函式用到的範圍。外推門檻是 1.1 × 120 = 132 秒；最後一點離終點 160 秒，超過門檻，所以 rate 只往後外推 60 秒，剩下 100 秒沒有被計入。irate 不外推，所以不會低估，但也只反映最後一個間隔。[^p-extrapolated][^p-instantvalue]
{: .at-caption }

下表假設真實速率固定為 r，I = 120s、d = 100s（實驗用的延遲），ts 落在最新一次 scrape 開始後的不同時間點。數字是「算出來的值 ÷ r」，由 [`lab/jitter/rate_model.py`](https://github.com/HahaSula/alert-trust/blob/main/lab/jitter/rate_model.py) 依 `extrapolatedRate` 與 `instantValue` 的原始碼邏輯算出[^p-extrapolated][^p-instantvalue]。<span class="at-chip at-ev-inferred">推算</span>

| ts − scrape 開始 | `rate[5m]` | `irate[5m]` | `rate[10m]` | `irate[10m]` | `rate[5m]` + offset 2m |
|---|---|---|---|---|---|
| 15s | 75% | 100% | 88% | 100% | 100% |
| 45s | 65% | 100% | 82% | 100% | 100% |
| 75s | 空 | 空 | 78% | 100% | 100% |
| 105s | 100% | 100% | 100% | 100% | 100% |

ts 落在 scrape 開始後 60–100 秒時最新一點還沒寫入：`[5m]` 只剩 1 點回傳空，`[10m]` 有值但被算低。過了 100 秒最新一點寫入，數值回到正確。所以低估和空結果都集中在「scrape 進行中」那一段，加上不小於 d 的 offset 就全部消失。

### irate 的優點

- 不外推，窗口末端缺資料也不會被算低。
- 搭配較長的 range（例如 `irate(M[10m])`），幾乎不會回傳空結果。
- 除以兩點的實際間隔，所以實際 scrape 間隔在 60、90、120 秒之間跳動時，數值不受影響。

### irate 的缺點

- 只用最後一個間隔，突發流量或短暫停頓都會直接反映，值在 A 附近時更容易抖動。
- Evaluation 每 30 秒一次、sample 每 120 秒一筆時，同一對 sample 會被連續用 4 次，然後跳到下一對，數值呈階梯狀。
- 事後查詢會用到 rule 當下還看不到的最新一點，比較的是不同的時間片段，仍然對不上。
- 官方文件建議 irate 只用於畫圖，alert 用 rate。

**結論：**把 rate 換成 irate 可以消除低估，但會引入更大的雜訊，回傳空結果的問題也沒有解決。單純把 range 拉長到 10m，rate 仍然低估 12–27%，因為缺的是窗口末端的資料，拉長 range 補不到。唯一能同時解決空結果、低估、事後對不上這三件事的是查詢 offset（2.51 用 `rate(M[5m] offset 2m)`，2.53 起可用 `query_offset`）；v3.15.1 另外可以試 `rate(M[5m] anchored)`（需要 `--enable-feature=promql-extended-range-selectors`，實驗功能），它不做外推，並用窗口起點前的 sample 當左端點。

## 6. Pending 之後：alert 怎麼送到 Alertmanager

就算 alert 已經 firing，也要經過 notifier queue 才會到達 Alertmanager。這一段有自己的失效模式：queue 滿了會丟資料、Alertmanager 太慢會拖住送出，太久沒續送則會被 Alertmanager 自動 resolve。

### 哪些 alert 會被送出

- 每次 evaluation 結束後，在 rule 自己的 goroutine 裡呼叫 `sendAlerts`（`rules/group.go:536`）。
- **pending 永遠不送**（`needsSending`，`rules/alerting.go:94`）。Alertmanager 完全不知道有 pending 這回事。[^p-needs-sending]
- firing 的 alert 在 `LastSentAt + resend-delay`（預設 1m）之後才會再送一次。[^p-resenddelay]
- 剛 resolve 的 alert 會立刻送出，並保留 15 分鐘持續重送。

### EndsAt 的計算

- `ValidUntil = ts + 4 × max(resend-delay, rule interval)`，送出時填進 `EndsAt`。[^p-alert-validuntil]
- 範例參數：4 × max(1m, 30s) = **4 分鐘**。
- 如果 Prometheus 超過這段時間沒有續送（queue 丟資料、Alertmanager 連不上、rule 卡住），Alertmanager 會自己把 alert 判定為 resolved。
- 之後一旦續送成功，又會重新 firing，Alertmanager 端就會看到 firing 和 resolved 交替。契約見[續送中斷要短於 EndsAt](../contracts.md#resend-endsat)。

<!-- figure: notifier-queues -->

**圖 5**　兩個版本的 notifier 結構。batch 大小 v2 寫死為 64，v3 可用 `--alertmanager.notification-batch-size` 調整，預設 256。每台 Alertmanager 的請求逾時預設 10 秒（`alerting.alertmanagers[].timeout`）。[^p-notifier-batch][^p-notifier-wait][^p3-notifier-queue]
{: .at-caption }

<!-- figure: endsat-resend -->

**圖 6**　Prometheus 端的 alert 一直都在 firing，但只要續送中斷超過 4 分鐘，Alertmanager 就會發出 resolved 通知，之後再發一次 firing。如果 Alertmanager 端看到這種抖動，要檢查 notifier 的 dropped 與 errors。[^p-alert-validuntil]
{: .at-caption }

## 7. 內建 metric：每個階段要看什麼

以下名稱都已在兩個版本的原始碼中確認過。這些 metric 只能觀察，真正能調整行為的是第 8 節的設定。

| 階段 | Metric | 判讀 |
|---|---|---|
| Scrape | `prometheus_target_interval_length_seconds` | 實際 scrape 間隔。明顯大於設定值就是 tick 被丟掉 |
| | `scrape_duration_seconds` | 單一 target 從抓取到 append 的耗時（不含 commit） |
| | `up` | 抓取失敗時為 0，該次不產生 sample |
| | `scrape_samples_scraped`、`scrape_series_added` | target 大小與 series 變動，pod 重啟會讓 series_added 跳高 |
| | `prometheus_target_scrapes_exceeded_sample_limit_total` | 超過 sample_limit，整次 scrape 被丟棄 |
| Rule eval | `prometheus_rule_group_last_duration_seconds` ÷ `…_interval_seconds` | 大於 1 會開始跳過 evaluation |
| | `prometheus_rule_group_iterations_missed_total` | 被跳過的次數 |
| | `prometheus_rule_evaluation_failures_total` | 查詢失敗次數（以 group 計） |
| | `prometheus_rule_group_last_evaluation_timestamp_seconds` | `time() −` 它太大表示 group 卡住 |
| | `prometheus_rule_group_last_evaluation_samples` | 上一輪產生的 sample 數，alert 忽有忽無時會跳動 |
| | `prometheus_rule_group_last_rule_duration_sum_seconds`（v3） | 所有 rule 耗時加總，與 last_duration 比較可看出並行評估是否生效 |
| Alert 狀態 | `ALERTS` | pending／firing 狀態 |
| | `ALERTS_FOR_STATE` | 值為 ActiveAt。值一直在變就代表 pending 一直被重設 |
| Notifier | `prometheus_notifications_queue_length` ÷ `…_queue_capacity` | 接近 1 就會開始丟資料。v3 的 queue_length 帶 alertmanager label |
| | `prometheus_notifications_dropped_total` | 被丟掉的 alert，應為 0。v2 只有「所有 Alertmanager 都失敗」才算 |
| | `prometheus_notifications_errors_total` | 送出失敗。v2 以請求數計，v3 以 alert 數計 |
| | `prometheus_notifications_sent_total` | 送出總數，用來計算錯誤率 |
| | `prometheus_notifications_latency_seconds` | 每台 Alertmanager 的送出延遲。v3 另有 `_latency_histogram_seconds` |
| | `prometheus_notifications_alertmanagers_discovered` | 為 0 時完全不會送出任何 alert（k8s SD 失效時常見） |

### 建議的 meta-alert

```yaml
groups:
- name: meta-monitoring
  rules:
  # ── Scrape：pending 抖動的根因 ──────────────────
  - alert: ScrapeIntervalStretched
    expr: prometheus_target_interval_length_seconds{interval="30s", quantile="0.99"} > 45
    for: 10m
  - alert: ScrapeSlowerThanInterval
    expr: max by (job, instance) (scrape_duration_seconds) > 25
    for: 10m

  # ── Rule evaluation ─────────────────────────────
  - alert: RuleGroupTooSlow
    expr: prometheus_rule_group_last_duration_seconds > 0.8 * prometheus_rule_group_interval_seconds
    for: 10m
  - alert: RuleGroupMissedIterations
    expr: increase(prometheus_rule_group_iterations_missed_total[10m]) > 0
  - alert: RuleEvaluationFailures
    expr: increase(prometheus_rule_evaluation_failures_total[10m]) > 0
  - alert: RuleGroupStalled
    expr: time() - prometheus_rule_group_last_evaluation_timestamp_seconds > 3 * prometheus_rule_group_interval_seconds

  # ── Alert 狀態 ──────────────────────────────────
  - alert: AlertPendingFlapping
    expr: changes(ALERTS_FOR_STATE{alertname!="Watchdog"}[1h]) > 5

  # ── Notifier ────────────────────────────────────
  - alert: NotificationQueueFilling
    expr: |
      prometheus_notifications_queue_length
        / ignoring(alertmanager) group_left prometheus_notifications_queue_capacity > 0.5
    for: 5m
  - alert: NotificationsDropped
    expr: increase(prometheus_notifications_dropped_total[10m]) > 0
  - alert: NotificationErrorRate
    expr: |
      rate(prometheus_notifications_errors_total[5m])
        / rate(prometheus_notifications_sent_total[5m]) > 0.01
    for: 5m
  - alert: NotificationLatencyHigh
    expr: prometheus_notifications_latency_seconds{quantile="0.99"} > 5
    for: 10m
  - alert: NoAlertmanagerDiscovered
    expr: prometheus_notifications_alertmanagers_discovered < 1
    for: 5m
  # 接收端掛掉時，Prometheus 端沒有錯誤，只有這個 metric 會動
  - alert: AlertmanagerNotificationsFailing
    expr: sum by (instance, integration) (rate(alertmanager_notifications_failed_total[5m])) > 0
    for: 5m

  # ── 整條鏈路的 dead man's switch ─────────────────
  - alert: Watchdog
    expr: vector(1)
```

範例以 30s scrape interval 為基準。`Watchdog` 要搭配 Alertmanager 端或外部服務，在「沒收到」的時候告警，因為 Prometheus 掛掉時無法替自己發 alert。**但只有它不夠：**實測它在 target 停機、查詢飽和、CPU 受限、OOM crash loop、磁碟寫滿時都幾乎不受影響，還要搭配依賴真實資料且帶 `for` 的 canary。[^exp-prom-heartbeat]完整的 24 條規則見 [Prometheus Rule 信任邊際](prometheus-trust.md#rules)。

## 8. 建議的處置順序

1. **處理 scrape 本身。**用 `topk(10, scrape_duration_seconds)` 與 `scrape_samples_scraped` 找出慢的 target，再以 `metric_relabel_configs` 丟掉不需要的 series、拆分 target，或把該 job 的 `scrape_interval` 改成符合實際耗時的值，讓設定反映實際情況。
2. **加上查詢 offset。**2.51 要寫在每個 selector：`rate(M[10m] offset 2m)`。2.53 起可以在 group 設 `query_offset: 2m`，或全域設 `rule_query_offset: 2m`。Rule 看到的資料就會與事後查詢一致，代價是 alert 會晚 offset 那麼久。offset 要不小於尖峰 d。
3. **把 range 拉長。**以範例參數，`[5m]` 沒有 offset 就不夠（至少要 2I + d = 340s）；`[10m] offset 2m` 可以容忍 3 次 scrape 失敗，數值也較穩定。
4. **firing 之後再考慮 `keep_firing_for`。**它只對 firing 有效，不能防止 pending 被重設。
5. **Notifier 端。**queue 常常偏滿時加大 `--alertmanager.notification-queue-capacity`；v2 下若有一台 Alertmanager 不穩，它會拖慢所有通知，升級到 v3（3.10 起）可讓各 Alertmanager 的 queue 互不影響。

### 驗證查詢

```text
# 實際 scrape 間隔是否明顯大於設定值
prometheus_target_interval_length_seconds{interval="30s"}

# scrape 耗時遠超過 interval 的時段，應與 pending 被重設的時間點對得上
max by (job, instance) (scrape_duration_seconds{job="<job>"}) > 60

# pending 的開始時間是否一直在變
changes(ALERTS_FOR_STATE{alertname="<alertname>"}[30m])
```

事後查 `count_over_time(M[5m])` 會得到 2–3，因為那時資料都已寫入。這個查詢無法重現 rule 當下看到的狀況，要用 scrape 耗時的時段去對照，或用 [rule 當下的探針](prometheus-trust.md#probe)記錄。

## 9. 修正後的資源與行為影響

**讓 scrape 真的回到設定的 interval，負載會上升。**實際間隔被慢 scrape 拉長成設定值的 N 倍時，Prometheus 其實只處理了設定量的 1/N。修好之後，這個 job 的寫入量會變成 N 倍，讀取它的 rule 也要處理 N 倍的 sample。以範例參數（設定 30s、實際 120s）N = 4。動手前要先確認 CPU、記憶體和 IOPS 都有餘裕。

相對地，查詢 offset 幾乎不增加資源，代價是 alert 變慢，適合當第一步。

以下的方向和倍數是從原始碼與公式推算的，**沒有實測**。<span class="at-chip at-ev-inferred">推算</span>實際數字要用本節最後的查詢比對修正前後。

### 每項修正的影響

| 修正 | CPU | 記憶體 | IOPS／磁碟 | Alert 行為與其他代價 |
|---|---|---|---|---|
| offset 2m（表達式，或 2.53 起的 `query_offset`） | 不變 | 不變 | 不變 | 觸發和 resolve 都晚 2 分鐘。套用時 recording rule 與 `ALERTS` 的新時間戳會早於已寫入的資料，約 2 分鐘內的結果會被丟棄，圖上有短暫缺口 |
| `rate(M[10m])` | 此 rule 約 2× | 查詢峰值約 2× | 幾乎不變，資料還在 head | 反應變慢，短暫尖峰被平均掉，A 可能要重調。整體影響取決於 M 匹配多少 series |
| scrape 回到真正的 interval | ↑ 該 job N× | 小幅 ↑：series 數不變，查詢峰值與 page cache 增加 | ↑ N×：WAL、chunk、block 都增加，compaction 和 WAL replay 變久 | 解析度與準確度最好。磁碟用量以 N× 為上限估算，實際壓縮率要量測 |
| `metric_relabel_configs` 丟 series | ↓ append，HTTP 與 parse 不變 | ↓ 依比例，舊 series 要等 head GC 才釋放 | ↓ 依比例 | 丟掉的 metric 查不到，要先確認沒有圖表或 rule 在用。target 的回應時間和傳輸量不變 |
| 把 `scrape_interval` 改成實際耗時以上（例如 2m） | 持平或 ↓ | 持平 | 0.5–1× | 解析度變低，R 要跟著拉長，偵測較慢。d 再超過新 interval 時問題會再出現 |
| 拆分 target 或多台 Prometheus 分片 | 單台 ↓，總量 ↑ | 單台 ↓，總量 ↑ | 單台 ↓，總量持平或 ↑ | 多一個 target 就多一組 `up`、`scrape_*` series。分片後跨台查詢要另外處理，rule 要跟資料放同一台，維運成本較高 |
| `keep_firing_for` | 不變 | 極小 | 不變 | resolve 延後設定的時間，只對已 firing 的 alert 有效 |
| 加大 notifier queue | 不變 | 預先配置 8 B × 容量，積壓時再加上 alert 物件 | 不變 | alert 可能排更久才送出，治標不治本。要先看 Alertmanager 為何變慢 |
| 升級到 v3 | 需壓測 | 需壓測，queue 上限變成每台 Alertmanager 各一份 | 需壓測 | 相同負載實測 CPU、scrape、rule 耗時沒有明顯差異，2.55 以上記憶體約少 10%（30 萬 series，單次量測）[^exp-prom-upgrade]。range 左開會讓窗口少一點，門檻與 meta-alert 要重新確認 |
| 新增 13 條 meta-alert | 小 | 小 | 小 | `changes(ALERTS_FOR_STATE[1h])` 較貴，建議獨立成 group，interval 用 1m |

### 可以先算出來的倍數

- 每秒寫入 sample 數 = `series 數 ÷ I`
- 每次 evaluation 讀取 sample 數 = `匹配 series 數 × R ÷ I`

下表以「設定 30s、實際 I 120s、R 5m」為修正前的基準。

| 配置 | 每條 series 每次 eval 讀幾點 | 相對修正前的 rule 讀取量 | 相對修正前的寫入量 |
|---|---|---|---|
| 修正前：R 5m，I 120s | 2.5 | 1× | 1× |
| R 10m，I 120s（不修 scrape，或把設定改成 2m） | 5 | 2× | 1× |
| R 5m，I 30s | 10 | 4× | 4× |
| R 10m，I 30s | 20 | 8× | 4× |

如果用 relabel 丟掉比例 X 的 series，上表的寫入量再乘以 (1 − X)。單一查詢的 `series × R ÷ I` 也不能超過 `--query.max-samples`（預設 5000 萬），否則查詢失敗。

### 為什麼 IOPS 會跟著寫入量上升

- 每次 scrape commit 都會把 WAL page（32 KB）flush，所以 write 次數跟 scrape 頻率和資料量有關。
- 每寫滿 128 MB 切換 segment 時 fsync，寫入量變成 N 倍，fsync 頻率也約 N 倍。
- 每個 chunk 存 120 個 sample，間隔變短，chunk 寫到磁碟的頻率也跟著提高。

### 先確認 scrape 慢在哪裡

- HTTP 階段受 `scrape_timeout`（≤ interval）限制。如果 `scrape_duration_seconds` 量到的 d 遠超過 timeout，多出來的部分都花在 Prometheus 的 parse、relabel 與 append。例如 timeout 30s、d 100s，至少 70 秒花在 Prometheus 內部。
- CPU 未達 limit 仍可能被 throttle，要一併看 `container_cpu_cfs_throttled_periods_total`。
- 找到瓶頸前，不要先假設只要加大資源或丟 series 就能解決。

### 兩種最終配置

以範例參數為基準：

| | A：維持高解析度 | B：配合現有容量 |
|---|---|---|
| 配置 | S 30s（d < 30s）、R 5m、O 30s | S 2m（d < 2m）、R 10m、O 2m |
| 寫入量（相對修正前） | 4× | 1× |
| Rule 讀取量（相對修正前） | 4× | 2× |
| 磁碟（相對修正前） | 上限約 4× | 持平 |
| Alert 額外延遲 | 約 30 秒 | 約 2 分鐘，加上 10m 窗口的平滑 |
| 適用情況 | 資源有餘裕，或先丟掉足夠的 series | 短時間無法擴容，且可以接受較慢的偵測 |

配置 A 的 R 維持 5m 即可（I = 30s 時遠高於公式下限），不用同時拉長到 10m，否則 rule 讀取量會變成 8×。

### 建議的上線步驟

1. **記錄基準線**：用下面的查詢至少抓一個完整的 compaction 週期。
2. **先加 offset**（2.51 寫在表達式內）：資源幾乎不變，確認 `ALERTS_FOR_STATE` 不再反覆改變。
3. **找出 scrape 瓶頸並決定配置 A 或 B**：先丟掉不用的 series，再一次調整一個 job。
4. **d 穩定低於 interval 後**：把 O 降到略大於 max(d)，並確認 R ≥ (2 + k) × I，縮短 alert 延遲。

### 修正前後要比對的查詢

| 面向 | 查詢 | 來源 |
|---|---|---|
| CPU | `rate(process_cpu_seconds_total{job="prometheus"}[5m])` | Prometheus |
| CPU 限流 | `rate(container_cpu_cfs_throttled_periods_total{pod=~"prometheus.*"}[5m])` | cAdvisor |
| 記憶體 | `process_resident_memory_bytes{job="prometheus"}`、`prometheus_tsdb_head_series` | Prometheus |
| 寫入量 | `rate(prometheus_tsdb_head_samples_appended_total[5m])` | Prometheus |
| IOPS | `rate(container_fs_writes_total{pod=~"prometheus.*"}[5m])` | cAdvisor，PVC 環境可能要改看儲存層 |
| WAL | `prometheus_tsdb_wal_fsync_duration_seconds`、`prometheus_tsdb_wal_storage_size_bytes` | Prometheus |
| 磁碟與 compaction | `prometheus_tsdb_storage_blocks_bytes`、`prometheus_tsdb_compaction_duration_seconds` | Prometheus |
| 查詢負載 | `rate(prometheus_engine_query_samples_total[5m])`、`prometheus_rule_group_last_duration_seconds` | Prometheus |

## 附錄 A：Rule group 執行模型

**Prometheus 不是單執行序，但同一個 group 預設是單序列。**每個 group 有自己的 goroutine，不同 group 同時跑；同一個 group 裡的 rule 預設依定義順序一條接一條跑，上一輪沒跑完，下一輪不會開始。[^p-group-run][^p-group-eval]

要讓同一個 group 內也併行，必須開 `--enable-feature=concurrent-rule-eval`，兩個版本預設都沒開。

**這些設定都解不了 pending 抖動。**拆 group 或開併行只會縮短 T<sub>eval</sub>，空結果來自 scrape 太慢，修法仍然是第 8 節的查詢 offset 與 scrape 本身。

### 每一層是單序還是併行

| 層級 | 預設 | 開 concurrent-rule-eval |
|---|---|---|
| 同一個 group 的相鄰兩輪 | 不會重疊：跑不完就順延或跳過 | 相同 |
| 同一個 group 內的 rule | 單序列：全部在 group 的 goroutine 裡跑 | 部分併行：只有判定為無依賴的 rule |
| 不同 group 之間 | 併行：每個 group 一個 goroutine，數量沒有上限 | 相同 |
| 單一 PromQL 查詢 | 單序列：`promql/engine.go` 沒有開 goroutine，在呼叫端求值；storage 層合併多個 querier 時才會另開 | 相同 |
| goroutine 對應 OS thread | 由 Go runtime 排程，同時執行的上限是 `GOMAXPROCS`。v3.15.1 的 `--auto-gomaxprocs` 預設開啟，會對齊 container CPU quota；v2.55.1 要加 `--enable-feature=auto-gomaxprocs`，否則用 Go 的預設值（通常是節點核心數），在 K8s 設了 CPU limit 時較容易被 throttle。 | 相同 |

<!-- figure: rule-group-modes -->

**圖 7**　三種情況的時間軸。每格是一條 rule。A、B 的時間與峰值是下方實驗以每條 rule 100 ms 量到的數字；C 是示意，實驗中 5 個 group、每條 400 ms 時全域峰值為 5，每個 group 自己的峰值都是 1，也始終只用同一個 goroutine。[^exp-pend]
{: .at-caption }

### 實驗結果

用 v2.55.1 與 v3.15.1 的原始碼加上測試，把查詢換成固定耗時的假 `QueryFunc`，再記錄同時執行數與 goroutine ID。因為沒有真實的 CPU 與 I/O 競爭，**實際加速幅度會比這裡小**。<span class="at-chip at-ev-tested">實測</span>[^exp-pend]

| 情境（每條 rule 100 ms） | v2.55.1 | v3.15.1 |
|---|---|---|
| 8 條獨立 rule，預設 | 804 ms，峰值 1 | 804 ms，峰值 1 |
| 8 條獨立 rule，開 flag，K = 4 | 201 ms，峰值 5 | 201 ms，峰值 5 |
| 8 條獨立 rule，開 flag，K = 2 | 301 ms，峰值 3 | 302 ms，峰值 3 |
| 依賴鏈 a → b → alert 加 2 條獨立 rule，開 flag | 403 ms，峰值 2，依定義順序 | 301 ms，峰值 3，**獨立的先跑**，不一定照定義順序 |
| 同上的依賴鏈，group 內另有 `{job="x"}` 這種無名稱 selector，開 flag | 100 ms，峰值 4，**依賴順序失效** | 402 ms，峰值 1，退回序列 |
| 5 個 group 各 8 條同時開跑，開 flag，K = 2／4 | 全域峰值 7／9 | 全域峰值 7／9 |

**v2.51.2 與 v2.55.1 開這個 flag 要小心（實驗在 v2.55.1，v2.51.2 的程式相同，僅讀碼確認）。**Group 裡只要有無名稱 selector 或 `ALERTS`／`ALERTS_FOR_STATE`，依賴分析會回傳 nil，但 `AnalyseRules` 沒有檢查 nil，結果**所有** rule 都被當成獨立，依賴順序不再保證。[^p255-analyse-rules]上游在 2024-12 才修正（`7802ca263`，#15560），修正不在 v2.55.1。v3.15.1 遇到無名稱 selector 會退回序列；`ALERTS` 則改成比對 `alertname`，只把前面定義的相符 alert 視為依賴。

**`--rules.max-concurrent-evals`（預設 4）不是總上限。**它限制的是「另開的 goroutine」，每個 group 自己的 goroutine 還可以再跑一條，所以同時執行的 rule 最多是 **group 數 + K**。[^p255-max-concurrent][^p255-concurrency-ctrl]另外，v2.55 的 `docs/feature_flags.md` 把名稱寫成 `--rules.max-concurrent-rule-evals`，這個旗標並不存在，要以 `--help` 的名稱為準。

### 依賴怎麼判斷

| | v2.55.1 | v3.15.1 |
|---|---|---|
| 分析範圍 | 只看**同一個 group**，比對 selector 的 metric 名稱與 rule 的輸出名稱。跨 group 的依賴不會被偵測，也沒有順序保證 | 同左 |
| 可併行的條件 | 沒有上游也沒有下游的 rule，才嘗試搶 slot | 切成三段依序跑：無上游的（併行）→ 上下游都有的（逐條）→ 無下游的（併行） |
| 依賴方向 | 不分定義先後，只要名稱相符就算 | 只有定義在前面的 rule 才算上游 |
| 搶不到 slot | `TryAcquire` 不等待，直接在 group 的 goroutine 裡跑 | 同左 |
| 時間基準 | 同一輪所有 rule 都用同一個 `ts`，不論實際何時開始；實驗中每個情境都如此 | 同左 |

### T<sub>eval</sub> 超過 E 時會怎樣

同一個 group 不會疊著跑。Ticker 的 channel 只能暫存一個 tick，所以跑得慢時下一輪會在上一輪結束後馬上開始，錯過的 slot 不會補跑。`ts` 仍然落在 E 的整數倍上。以下是 E = 200 ms 的實測，兩個版本相同。<span class="at-chip at-ev-tested">實測</span>[^exp-pend][^p-group-missed]

| T<sub>eval</sub> | 實際評估的 slot（ts ÷ E） | iterations_missed | 開始時間比 ts 晚 |
|---|---|---|---|
| 0.9 E | 0 1 2 3 4 5 6 7 … | 0／30 輪 | 0 |
| 1.5 E | 0 1 3 4 6 7 9 10 … | 9（20 輪涵蓋 29 個 slot） | 0 與 0.5 E 交替 |
| 2.5 E | 0 2 5 7 10 12 15 17 … | 16（12 輪涵蓋 28 個 slot） | 0 與 0.5 E 交替 |

### 多個 group 共用的關卡

<!-- figure: query-slots -->

**圖 8**　一條 rule 的查詢要過兩次同一組 slot，解析完會先釋放，執行時再搶一次。實測 `--query.timeout` 設 300 ms、只有 1 個 slot：在 ② 被佔住時 300 ms 回傳 `context deadline exceeded`；在 ① 被佔住 2 秒時完全不受 timeout 影響，2001 ms 後成功。② 失敗屬於第 3 節的「查詢失敗」，pending 狀態不變。[^p-engine-queue][^exp-pend]
{: .at-caption }

單一查詢不跨 goroutine，所以預設模式下每個 group 最多佔一個 slot。要估算會不會塞車，可以看平均同時執行的 group 數：

```text
# 平均同時執行的 group 數（預設模式），要明顯低於 query.max-concurrency 與可用 CPU 核心數
sum(prometheus_rule_group_last_duration_seconds / prometheus_rule_group_interval_seconds)

# 查詢是否在排隊（兩個 gate 的時間都算在內）
prometheus_engine_query_duration_seconds{slice="queue_time", quantile="0.99"}

# 執行中加上等待中的查詢數，對照上限
prometheus_engine_queries / prometheus_engine_queries_concurrent_max

# v3：大於 1 代表 group 內真的有併行（v2.55.1 沒有這個 metric）
prometheus_rule_group_last_rule_duration_sum_seconds / prometheus_rule_group_last_duration_seconds
```

要看 group 內哪一條 rule 最慢，用 `/api/v1/rules` 回傳的每條 rule 的 `evaluationTime`。

### 怎麼選

| | 單一 group | 拆成多個 group | 單一 group + concurrent-rule-eval |
|---|---|---|---|
| 執行順序 | 嚴格依定義順序 | group 之間無保證 | 只保留偵測得到的依賴 |
| T<sub>eval</sub> | 所有 rule 加總，一條慢全部晚 | 各自計算，慢的 group 不會拖到別的 group | 縮短，幅度取決於 K 與依賴關係 |
| interval、query_offset、limit | 全部相同 | 每個 group 可不同 | 全部相同 |
| 查詢負載 | 同時最多 1 條 | 同時最多 group 數 | 同時最多 K + 1 條，全域最多 group 數 + K |
| 適用情況 | recording rule 與使用它的 alert | 互不相依、週期不同的 rule，**優先考慮** | 很多互不相依的 rule，又必須留在同一個 group；v3 較合適 |

Alert 依賴 recording rule 時要放在**同一個 group**，這樣同一輪就讀得到新值。放在不同 group 時，alert 可能讀到上一輪的值，約晚一個 E，再加上另一個 group 的 T<sub>eval</sub>。

### 對 pending 抖動的影響

| 做法 | 對 pending 抖動的效果 |
|---|---|
| 把慢的 rule 拆到別的 group | 不解決空結果：只改善 T<sub>eval</sub> 與 missed，讓 firing 不延後 |
| 開 `concurrent-rule-eval` | 不解決空結果：還會增加同時查詢數，在 scrape 已經很慢時可能加重負載 |
| 加大 `--query.max-concurrency` | 不解決空結果：只有 `queue_time` 真的偏高才需要 |
| `query_offset` | 有效：查詢窗口移到已 commit 的區段，與 group 怎麼拆無關 |

**調快 rule 之後，pending 可能抖得更明顯（由原始碼推論，未實測）。**`ts` 固定，但查詢實際執行的時間是 `ts + δ`，δ 是這條 rule 前面累積的耗時。查詢讀的是當下已 commit 的 sample，所以 group 越慢、rule 排得越後面，看得到的資料反而越多。拆 group 或開併行讓 δ 變小後，同一條 rule 可能更常看到不完整的窗口。上線後如果抖動變多，先不要認定是拆錯了，要回頭確認是否已經設定 offset。

## 附錄 B：符號與規則總表

| 符號 | 意義 | 範例值 | 從哪裡看 |
|---|---|---|---|
| S | 設定的 scrape_interval | 30s | scrape config |
| d | 一次 scrape 從開始到 commit 的時間 | 約 100s | `scrape_duration_seconds`（不含 commit） |
| I | 實際 scrape 間隔 | 120s | `prometheus_target_interval_length_seconds` |
| E | rule group 的 evaluation interval | 30s | `prometheus_rule_group_interval_seconds` |
| T<sub>eval</sub> | 一次 group evaluation 的耗時 | 依 group 而定 | `prometheus_rule_group_last_duration_seconds` |
| O | query_offset | 0 | rule group／global config |
| R | range selector 長度 | 5m | rule 運算式 |
| F | `for` 持續時間 | 3m | rule 定義 |
| D<sub>resend</sub> | `--rules.alert.resend-delay` | 1m（預設） | 啟動參數 |
| Q | notifier queue 容量 | 10000（預設） | `prometheus_notifications_queue_capacity` |
| T<sub>AM</sub> | 每台 Alertmanager 的請求逾時 | 10s（預設） | `alerting.alertmanagers[].timeout` |

| 規則 | 式子 | 範例代入 |
|---|---|---|
| 實際 scrape 間隔 | `d > S` 時 `I = ⌈d / S⌉ × S` | 120s |
| sample 何時查得到 | 時間戳 `t`（scrape 開始），可見時間 `t + d` | 晚約 100s |
| rule 的查詢窗口 | `(ts − O − R, ts − O]`；v2 左閉、v3 左開 | `(ts − 5m, ts]` |
| 與事後查詢一致 | `O ≥ max(d)` | 不成立 |
| rate／irate 有值 | 窗口內可見 sample ≥ 2：沒有足夠 offset 時 `R ≥ (2 + k) × I + d`；`O ≥ d` 時 `R ≥ (2 + k) × I` | 不成立 |
| rate 外推門檻 | 端點距邊界 ≥ `1.1 × 平均間隔` 時只外推半個間隔 | 低估 25–35% |
| irate 取值 | 窗口內最後兩點，`Δ / 兩點時間差`，不外推 | 空結果比例同 rate |
| 窗口不漏資料 | `E ≤ R` | 成立 |
| 不跳過 evaluation | `T_eval < E` | 依 group 而定 |
| pending → firing | 連續 `⌈F / E⌉ + 1` 次結果都 > A | 7 次 |
| pending 被清除 | 任一次 evaluation 結果裡沒有這個 alert | 約每 2 分鐘一次 |
| `for: 0s` | 沒有 pending，第一次成立就 firing | — |
| 哪些 alert 會送出 | pending 不送；firing 每 D<sub>resend</sub> 續送；resolved 立即送出並保留 15 分鐘 | — |
| Alertmanager 端到期 | `EndsAt = ts + 4 × max(D_resend, E)` | 4m |
| queue 滿了 | 超過 `Q` 時丟掉最舊的；batch 64（v2）／256（v3） | — |
| Alertmanager 慢 | v2：每批等最慢的那台（最多 T<sub>AM</sub>）；v3：只影響自己的 queue | — |

## 附錄 C：原始碼依據

行號以 v2.55.1 為準，標註 v3 的為 v3.15.1。正文的腳註連到釘住版本的行號。

- `rules/alerting.go:343`（v3 `:389`）查詢時間點為 `ts − queryOffset`
- `rules/alerting.go:464-465`（v3 `:510-511`）結果中沒有的 pending alert 直接刪除
- `rules/alerting.go:480` `ts − ActiveAt ≥ for` 時轉為 firing
- `rules/alerting.go:94-105` `needsSending`：pending 不送；依 resend-delay 續送
- `rules/alerting.go:560-570` `ValidUntil = ts + 4 × max(resendDelay, interval)`
- `rules/group.go:205-297` group 的排程迴圈與 iterations_missed 計算
- `rules/group.go:516-529` 查詢失敗時直接 return，不更新 alert 狀態
- `scrape/scrape.go:1386-1422` timeout 只包住 HTTP 抓取；append 使用 scrape 開始的時間戳
- `config/config.go:744` scrape_timeout 不可大於 scrape_interval
- `promql/engine.go:2388` 與 v3 `promql/engine.go:3103` range selector 左閉與左開
- `notifier/notifier.go:411-437, 515-628`（v2）單一 queue、滿了丟最舊的、wg.Wait 等待所有 Alertmanager
- `notifier/sendloop.go`（v3）每台 Alertmanager 一條 send loop，指標帶 alertmanager label
- `promql/functions.go:72-175`（v3 `:452`）rate 的外推邏輯：門檻 1.1 × 平均間隔，超過只外推半個間隔
- `promql/functions.go:288, instantValue`（v3 `:821`）irate 只取最後兩點，不外推
- v3 `promql/functions.go:309` `extendedRate`：`anchored`／`smoothed` 修飾子
- `rules/group.go:575-593` rule 結果若時間戳早於已寫入的 sample，會被當成 out-of-order 丟棄
- `scrape/scrape.go:1671` `metric_relabel_configs` 在 append 階段才套用
- `tsdb/wlog/wlog.go:41-42, 585, 803` WAL page 32 KB、segment 128 MB；commit 時 flush，切換 segment 時 fsync
- `tsdb/head.go:189` 每個 chunk 預設 120 個 sample
- `notifier/notifier.go:230`（v3 `notifier/sendloop.go:69`）queue 依容量預先配置
- `rules/manager.go:231-242`（v3 `:308-319`）每個 group 一個 goroutine，呼叫 `newg.run`
- `rules/group.go:279-298`（v3 `:277-296`）同步的排程迴圈，`evalIterationFunc` 回傳後才取下一個 tick；`missed` 計算
- `rules/group.go:394-417`（v3 `:422-445`）`EvalTimestamp`：每個 group 的相位來自 hash(name, file) % interval
- `rules/group.go:476-477, 624-636`（v3 `:664-709`）Eval 的序列與併行分支
- `rules/manager.go:139-145, 480-490` v2 的併行條件與 `TryAcquire`；v3 `:578-616` 改為 `SplitGroupIntoBatches`
- `rules/manager.go:338`（v3 `:424`）依賴分析在每個 group 載入時各做一次
- `rules/manager.go:448-454` v2 未檢查 nil；v3 `rules/manager.go:534-539` 有檢查
- `rules/group.go:1063-1073`（v3 `:1180-1183, 1187-1222`）indeterminate 條件與 ALERTS 處理
- `promql/engine.go:474, 621, 659`（v3 `:547, 695, 740`）兩次 `queueActive` 與 timeout 的設定位置
- `cmd/prometheus/main.go:690, 819, 845`（v3 `:960, 1051, 1080`）rule 的 `context.Background()`；rule 與 HTTP API 共用同一個 engine
- `cmd/prometheus/main.go:460, 484`（v3 `:622, 646`）`--rules.max-concurrent-evals`、`--query.max-concurrency`
- `storage/merge.go:157`（v3 `:161`）合併多個 querier 時才開 goroutine
- `docs/feature_flags.md`、`docs/configuration/recording_rules.md` 官方文件的預設行為說明，與上述一致

Rule group 實驗以 Go 1.26.10 編譯兩個版本的 `rules` 套件，一次執行，時間數字會有幾 ms 的誤差。上游的 `TestAsyncRuleEvaluation`、`TestBoundedRuleEvalConcurrency`、`TestDependenciesEdgeCases`、`TestDependencyMap`、`TestNoMetricSelector` 與 `TestQueryConcurrency` 在兩個版本都通過。

相關頁面：[Prometheus Rule 信任邊際](prometheus-trust.md)、[Prometheus 架構](../architecture/prometheus.md)、[信任契約](../contracts.md)。
