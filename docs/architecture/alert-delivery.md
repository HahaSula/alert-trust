# 從觸發到通知：alert 怎麼送到 Alertmanager

條件成立到有人收到通知，中間有兩段等待：ruler（Prometheus、vmalert）的偵測，和 Alertmanager 的分組。**偵測的時間由 `scrape_interval`、`evaluation_interval`、`for` 決定；通知的時間由 alert 落在新 group 還是已在響的 group 決定**：新 group 等 `group_wait`，已在響的 group 等下一個 `group_interval` tick。`repeat_interval` 只在這些 tick 上比對，所以實際重送間隔是 `group_interval` 的整數倍。

以 Prometheus v3.13.5、vmalert v1.155.0、Alertmanager v0.28.1 原始碼為準（0.29.0 的計時邏輯相同，0.32.0 有差異的地方另外註明）；lab 用 Prometheus 3.13.5 與 Alertmanager 0.29.0。<span class="at-chip at-ev-code">讀碼</span> <span class="at-chip at-ev-tested">實測</span>

## 結論 {#summary}

| 這一段 | 誰在等 | 決定延遲的設定（預設） | 證據 |
|---|---|---|---|
| 抓到變化 | scrape | `scrape_interval`（1m）＋ scrape 耗時 | 見[信任契約](../contracts.md) |
| 評估看到 | rule group | `evaluation_interval`（1m）；Prometheus 加 `query_offset`，vmalert 加 `max(L, D)` | 見[信任契約](../contracts.md) |
| pending → firing | alert 狀態 | `for`，只在評估時檢查，⌈`for` ÷ E⌉ × E[^ad-p-firing] | 讀碼 |
| 送到 Alertmanager | notifier | 送出逾時 10s；失敗整批丟、不重試[^ad-p-am-timeout][^ad-p-drop] | 讀碼 |
| 新 group 的第一次通知 | aggregation group | `group_wait`（30s）[^ad-am-group-wait] | 實測[^exp-ad-group-wait] |
| 已在響的 group 有新 alert、resolved | aggregation group | 下一個 `group_interval` tick（5m）[^ad-am-group-interval] | 實測[^exp-ad-group-interval] |
| 沒變化時重送 | 通知流程 dedup | `repeat_interval`（4h），只在 tick 上比對[^ad-am-repeat] | 實測[^exp-ad-repeat] |
| ruler 停了，alert 自動 resolved | Alertmanager | EndsAt（4 × max(resend, E)）＋ 下一個 tick[^ad-p-validuntil][^ad-am-flush-resolved] | 實測[^exp-ad-endsat] |
| HA 第 N 台 | 通知流程 wait | N × `--cluster.peer-timeout`（15s）[^ad-am-peer-timeout] | 讀碼 |

<!-- figure: delivery-timeline -->

**圖**　上兩列是**機制**：ruler 依序經過 scrape、評估、`for`、notifier queue，再 POST 到 Alertmanager；Alertmanager 存進 alert store、依 route 分到 aggregation group，flush 時跑通知流程，最後送到 receiver。紅框的 aggregation group 是三個 interval 起作用的地方。[^ad-p-send-after-eval][^ad-am-mem-merge][^ad-am-route-match][^ad-am-pipeline] 下面是**這組數據**（lab 情境 a）：`scrape_interval` 與 `evaluation_interval` 5s、`for: 10s`、`group_wait: 10s`；時間是接收端容器的牆鐘與 Alertmanager 的 `updatedAt`。[^exp-ad-group-wait]
{: .at-caption }

## 每一段 {#hops}

| 段 | 誰等誰 | 多久（設定，預設） | 擋住誰 | 壞掉時的症狀 |
|---|---|---|---|---|
| 1. Rule 評估 | group 依 `evaluation_interval` 排程 | E（1m） | 同一個 group 的 rule 依序評估 | 評估太久跳過一輪，見 [Prometheus rule group](prometheus.md) |
| 2. pending → firing | `for` 從第一次看到條件成立的評估時間起算[^ad-p-firing] | ⌈`for` ÷ E⌉ × E | — | 一次 scrape 失敗就重置 `for`，見[信任契約](../contracts.md#lookback) |
| 3. 放進 notifier queue | 評估完立刻放[^ad-p-send-after-eval]；firing 之後只有在「上次送出 + resend」**之後**的評估才再放，resolved 立刻放[^ad-p-needs-sending] | Prometheus `--rules.alert.resend-delay`（1m）[^ad-p-resend-flag]；vmalert `-rule.resendDelay`（0，每次評估都送）[^ad-vma-resend-flag][^ad-vma-needs-sending] | — | 續送中斷太久，Alertmanager 自動 resolved，見[續送契約](../contracts.md#resend-endsat) |
| 4. POST `/api/v2/alerts` | Prometheus 每台 Alertmanager 一條 queue、一次一批[^ad-p-queue-add][^ad-p-batch]；vmalert 在 group 裡同步送[^ad-vma-send] | 逾時 10s（`alerting.alertmanagers[].timeout`、`-notifier.sendTimeout`）[^ad-p-send-timeout][^ad-vma-timeout] | Prometheus：同一台 Alertmanager 的下一批；vmalert：同一個 group 的下一條 rule | Prometheus 整批丟棄（`prometheus_notifications_dropped_total`）[^ad-p-drop]；細節見 [Prometheus notifier](prometheus.md#notifier)、[vmalert 送通知](vmalert.md#notify) |
| 5. API → alert store | 收到時寫 `updatedAt`[^ad-am-api-updated]；同一個 fingerprint 時間重疊就合併[^ad-am-mem-merge]；持鎖送進 dispatcher 的 channel[^ad-am-mem-notify] | 立即；沒有 EndsAt 的 alert 才用 `resolve_timeout`（5m）[^ad-am-api-resolve-timeout] | channel 滿時 POST 阻塞 | 見 [Alert store](alertmanager.md#alert-store) |
| 6. Dispatcher | 每個 alert 比對 route 樹[^ad-am-route-match]，依 `group_by` 的 label 算出 group[^ad-am-group-key] | 立即 | 0.31 以前單一 goroutine | 見 [Dispatcher](alertmanager.md#dispatcher) |
| 7. Aggregation group | 新 group 計時 `group_wait`[^ad-am-group-wait]；之後每 `group_interval` 一個 tick[^ad-am-group-interval] | `group_wait` 30s、`group_interval` 5m[^ad-am-route-defaults] | 同一個 group 的下一輪 flush | 一輪 flush 的逾時是 `group_interval`（HA 再加等待）[^ad-am-flush-timeout][^ad-am-timeout-func] |
| 8. 通知流程 | gossip settle → inhibit → 時間區間 → silence → wait → dedup → retry → 寫通知紀錄[^ad-am-pipeline] | HA 第 N 台等 N × 15s[^ad-am-wait-stage]；retry 重試到這輪逾時[^ad-am-retry] | 同一個 group 的下一輪 | 見[通知流程](alertmanager.md#pipeline)、[Alertmanager 報告 §4](../reports/alertmanager.md) |
| 9. Receiver | 第一次成功送出後寫通知紀錄（nflog），時間戳記是寫入當下[^ad-am-nflog-ts] | — | — | receiver 慢或不回應，整個 group 停住，見 [Aggregation group](alertmanager.md#aggregation-group) |

## group_wait：新 group 的第一次通知 {#group-wait}

<!-- figure: interval-group-wait -->

**圖**　**機制**：alert 依 route 的 `group_by` 算出 group label；這組 label 在這條 route 上還沒有 group，就建立一個並計時 `group_wait`，只計一次。等待期間到的同組 alert 都放進同一則通知。[^ad-am-group-key][^ad-am-group-wait] **這組數據**：`group_wait: 10s`，inst 1 與 inst 3 相隔 5s 到 Alertmanager，同一則送出。[^exp-ad-group-wait]
{: .at-caption }

**機制**（讀碼）

- **什麼算新 group**：group 的 key 是 route 加上 alert 在 `group_by` 裡的 label 值[^ad-am-group-labels][^ad-am-group-key]。同一條 route、同樣的值就進同一個 group[^ad-am-group-existing]。
- `group_by` 沒寫時繼承父路由[^ad-am-route-inherit]，根路由沒寫就是整條 route 一個 group；`group_by: ['...']` 用 alert 的全部 label，每個 alert 自己一個 group[^ad-am-groupby-all]。子路由寫 `group_by: []` 在 0.31 起語意改變，見 [Alertmanager 報告 §6](../reports/alertmanager.md)。
- **例外**：group 還沒 flush 過，而到達的 alert `startsAt` 已早於 `now − group_wait`，就立刻 flush[^ad-am-old-alert]。Prometheus 送的 `startsAt` 是轉成 firing 的評估時間[^ad-p-startsat]，所以 Alertmanager 重啟後 ruler 重送的舊 alert 不再等 `group_wait`。
- group 裡的 alert 都消失（resolved 已送出、或沒有 alert）後，每 30s 一次的維護會刪掉這個 group[^ad-am-maintenance]（0.29 起可調）；之後同樣的 label 再來，又是新 group，又等 `group_wait`。

**這組數據**[^exp-ad-group-wait]

條件：`scrape_interval`、`evaluation_interval` 5s，`for: 10s`，`group_wait: 10s`，`group_by: [alertname, scen]`。時間是秒，從把 inst 1 設成 1 起算；接收端容器牆鐘，Alertmanager 收到的時間用 API 的 `updatedAt`。

| 事件 | inst 1 | inst 3 |
|---|---|---|
| gauge 設成 1 | 0.0 | 6.0 |
| Prometheus 第一次看到（`activeAt`） | 1.8 | 6.8 |
| 轉成 firing（`startsAt`） | 11.8 | 16.8 |
| Alertmanager 收到（`updatedAt`） | 12.7 | 17.7 |
| 通知 | **22.7**（收到後 10.0s） | 同一則 |

從條件成立到通知 22.7s，對照最壞 S + E + ⌈`for` ÷ E⌉ × E + `group_wait` = 5 + 5 + 10 + 10 = 30s。inst 3 在等待期間到，跟 inst 1 同一則送出。兩台 Alertmanager（設定 A、B）時間相同。

## group_interval：已在響的 group {#group-interval}

<!-- figure: interval-group-interval -->

**圖**　**機制**：第一次 flush 之後，計時器每 `group_interval` 觸發一次（從上一次觸發起算，不是從 alert 到達起算）[^ad-am-group-interval]。新 alert 加入、alert resolved，都等到下一個 tick 才送；tick 時 dedup 看到 firing 集合變了，或 resolved 集合變了（`send_resolved` 開著），才送出[^ad-am-new-firing][^ad-am-send-resolved]。**這組數據**：`group_interval: 30s`，時間是接收端容器的牆鐘。[^exp-ad-group-interval]
{: .at-caption }

**機制**（讀碼）

- 第二個 alert 最多等一個 `group_interval`。`group_interval` 5m（預設）時，已經在響的 group 裡出現第二個問題，最多 5 分鐘後才通知。
- Resolved 一樣等 tick：flush 時把 EndsAt 已過的 alert 當成 resolved[^ad-am-flush-resolved]，送成功後才從 group 刪掉[^ad-am-flush-delete]。group 全部 resolved 時，就算 `send_resolved: false` 也會走一次通知紀錄[^ad-am-all-resolved]。
- 同一個 tick 內 firing 集合沒變、只有 annotation 或 EndsAt 變了，不算變化，不會送。

**這組數據**[^exp-ad-group-interval]

同一次執行，`group_interval: 30s`。

| 事件 | 時間 | 距第一次通知 |
|---|---|---|
| 第一次通知（tick 0） | 22.7 | 0 |
| inst 2 設成 1 | 27.5 | — |
| inst 2：Prometheus 第一次看到 | 36.8 | — |
| inst 2：Alertmanager 收到 | 48.4 | — |
| **通知（含 inst 2）**，tick 1 | **53.4** | 30.8 |
| inst 1 設成 0 | 54.3 | — |
| inst 1：Alertmanager 標成 resolved（API 不再列出，1s 輪詢） | 64.4 | — |
| **通知（inst 1 resolved）**，tick 2 | **84.0** | 61.3 |

inst 2 在 Alertmanager 等了 5.0s，inst 1 的 resolved 等了 19.6s，都落在 tick 上，不是收到就送。inst 2 從設成 1 到 Prometheus 看到花了 9.3s，接近 S + E 的最壞情況。tick 間隔以牆鐘看是 30.6–30.8s，原因見 [repeat_interval](#repeat-interval) 的時鐘說明。

## repeat_interval：沒變化時多久再送 {#repeat-interval}

<!-- figure: interval-repeat -->

**圖**　**機制**：沒變化時，每個 tick 比對「通知紀錄的時間戳記是否早於 `now − repeat_interval`」[^ad-am-repeat]，所以重送一定落在 tick 上。時間戳記是送出**之後**寫的[^ad-am-nflog-ts]，比 tick 晚 δ（送出花的時間），所以 `repeat_interval` 剛好是 `group_interval` 整數倍時，那一個 tick 還差 δ，要再等一個 tick。圖用理想時鐘，`group_interval` 30s。
{: .at-caption }

**機制**（讀碼）

實際重送間隔 = 距上一次送出超過 `repeat_interval` 的第一個 tick：

| `repeat_interval` 與 `group_interval` G | 實際間隔 |
|---|---|
| 不是 G 的整數倍 | ⌈`repeat_interval` ÷ G⌉ × G |
| 剛好是 G 的整數倍 | `repeat_interval` + G（排程誤差小於送出耗時 δ 時，一般主機如此） |
| 預設 4h、5m | 4h5m |
| 小於 G | G（每個 tick 都送） |

- 0.32.0 的 dedup 改用 tick 的時間當 now[^ad-am032-repeat]，計時器同樣在 tick 時重設[^ad-am032-group-interval]，整數倍時一樣要多等一個 tick。
- 通知紀錄保留 2 × `repeat_interval`[^ad-am-repeat]；ruler 的續送（每 resend 一次）只延長 EndsAt，**不會**觸發通知，除非 firing 集合變了。
- `repeat_interval` 依 route 繼承[^ad-am-route-inherit]。Watchdog 走主路由時，重送週期就是主路由的 `repeat_interval`（例如 4h5m），外部 dead man's switch 的等待要比它長，見[等待時間契約](../contracts.md#deadman-wait)。Watchdog 要有自己的 route：`group_wait: 0s`、`group_interval: 1m`、`repeat_interval: 1m`（實際每 2 分鐘），見 [Alertmanager 報告 §5](../reports/alertmanager.md)。

**這組數據**[^exp-ad-repeat][^exp-ad-clock]

`group_interval: 30s`，group 沒有變化時兩次通知的間隔（接收端容器牆鐘）：

| `repeat_interval` | ⌈R ÷ G⌉ × G | 原始碼預期（理想時鐘） | 量到的間隔 | 幾個 tick |
|---|---|---|---|---|
| 45s | 60s | 60s | 62.1、63.7、61.4、63.9s | 2 |
| 75s | 90s | 90s | 92.6、94.3s | 3 |
| 105s | 120s | 120s | 123.5、125.4s | 4 |
| 1m（整數倍） | 60s | **90s** | 62.1、63.7、61.4、63.9s | 2 |

45s、75s、105s 都落在 ⌈`repeat_interval` ÷ G⌉ 個 tick 上。1m 量到的是 2 個 tick，不是原始碼預期的 3 個：這台 Docker Desktop VM 的單調時鐘比牆鐘慢，每 30s 的計時器以牆鐘看是 30.6–32.7s[^exp-ad-clock]，2 個 tick 已經超過 60s，蓋過了 δ。整數倍的結論只來自原始碼，lab 量不出來；在單調時鐘正常的主機上，要多等一個 tick。

## 和 ruler 的互動 {#interaction}

<!-- figure: interval-interaction -->

**圖**　**機制**：ruler 每 resend 一次重新 POST，只把 EndsAt 往後延（`4 × max(resend, E)`）[^ad-p-validuntil][^ad-vma-resolve]；Alertmanager 只在 tick 上決定送不送。Ruler 停掉後不再續送，EndsAt 到了 alert 才算 resolved，下一個 tick 送出 resolved。[^ad-am-flush-resolved] **這組數據**：`--rules.alert.resend-delay=15s`、E 5s（EndsAt = 最後一次送出 + 60s）、`group_interval: 30s`。[^exp-ad-endsat]
{: .at-caption }

| 互動 | 機制 | 結果 |
|---|---|---|
| Ruler 續送 | Prometheus：firing 後只在「上次送出 + resend」之後的評估才送（嚴格大於）[^ad-p-needs-sending]；vmalert 預設每次評估都送[^ad-vma-needs-sending] | 只延長 EndsAt；firing 集合沒變就不通知 |
| 條件解除 | Prometheus 下一次評估就送 resolved，EndsAt = 解除的評估時間[^ad-p-endsat] | 下一個 tick 送 resolved |
| Ruler 停掉、查詢失敗、送不到 | EndsAt 到期[^ad-p-validuntil][^ad-vma-end] | EndsAt 之後的第一個 tick 送 resolved：條件還在，通知卻說解決了 |
| HA（多台 Alertmanager） | 每台都跑完整流程，第 N 順位先等 N × `--cluster.peer-timeout` 再查通知紀錄[^ad-am-wait-stage][^ad-am-peer-timeout] | 第一台照常送；第一台掛掉時，第二台晚 15s |

**這組數據**[^exp-ad-endsat]：Prometheus 的續送（inst 2 的 `updatedAt`）在 250.6、271.2、292.4、312.4、335.1，間隔 20–23s，不是 15s：resend 15s 時要等「上次 + 15s」**之後**的評估，E 5s 時是每 20s（`E × (⌊resend ÷ E⌋ + 1)`，見[續送契約](../contracts.md#resend-endsat)），以牆鐘看再略長。這段時間 Alertmanager 只在 271.2、335.1 重送（`repeat_interval` 到了），續送本身沒有觸發通知。

| 事件 | 時間 |
|---|---|
| 最後一次 POST（`updatedAt`） | 335.1 |
| kill Prometheus | 336.1 |
| 最後一次 EndsAt | 391.8（評估時間 + 60s，即 4 × max(15s, 5s)） |
| inst 2 從 API 消失（resolved，1s 輪詢） | 393.7 |
| **resolved 通知**（inst 2、3 同一則） | **400.6**（kill 後 64.5s，EndsAt 後 8.8s） |

inst 3 的 EndsAt 比 inst 2 早（最後一次 POST 在 317.4），379.1 就 resolved，也等到同一個 tick 才送。

## 最壞情況：從條件成立到通知 {#worst-case}

S 是 `scrape_interval`、d 是 scrape 耗時、E 是 `evaluation_interval`、O 是 `query_offset`（vmalert 換成 `max(L, D)`）。**推算**，不含 receiver 本身的延遲與重試；`rate` 這類要多個 sample 才超過門檻的 rule 更久。

| 情況 | 最壞時間 | 預設值、`for: 5m` 時 |
|---|---|---|
| 第一個 alert（新 group） | S + d + E + O + ⌈`for` ÷ E⌉ × E + `group_wait` | 1m + d + 1m + 5m + 30s ≈ 7.5m |
| 加入已在響的 group | S + d + E + O + ⌈`for` ÷ E⌉ × E + `group_interval` | 1m + d + 1m + 5m + 5m ≈ 12m |
| 條件解除（resolved） | S + d + E + O + `group_interval` | 1m + d + 1m + 5m ≈ 7m |
| Ruler 停掉，Alertmanager 送出假 resolved | 4 × max(resend, E) + `group_interval` | 4m + 5m = 9m |
| Ruler 停掉，有人知道 | 外部 DMS：4 × max(resend, E) + `repeat_interval` + `group_interval` + DMS 自己的等待；外部觀察者的 `up`：它自己的 S + E + `for` | 見[等待時間契約](../contracts.md#deadman-wait) |

`tools/fit_contracts.py --notify-within X` 依每條 critical rule 的 route 算出前兩行，分成「新 group」與「加入已在響的 group」兩列，標**推算**。

## 常見的坑 {#pitfalls}

| 坑 | 為什麼 | 怎麼處理 |
|---|---|---|
| Watchdog 繼承主路由的 `repeat_interval` | 重送週期變成 4h5m，DMS 要等 4 小時以上才會響[^ad-am-route-inherit][^ad-am-repeat] | 給 Watchdog 專用 route，`repeat_interval` 短，`group_interval` 也短 |
| `group_interval` 太長，第二個事件被藏住 | 同一個 group 已經在響時，新 alert 要等下一個 tick[^ad-am-group-interval] | 要快的 alert 分到 `group_interval` 短的 route；或讓 `group_by` 把不同事件分開 |
| `group_by: [alertname]` 把不同 instance 合在一起 | 同名 alert 都進一個 group，第一個 instance 之後的都要等 tick，通知內容也混在一起[^ad-am-group-labels] | `group_by` 加上 `cluster`、`instance` 這類區分事件的 label |
| `group_by: ['...']` 或每個 alert 一組 | 每個 alert 都是新 group，都等 `group_wait`，但通知數量暴增 | 只在 alert 數量少的 route 用 |
| Resolved 晚到 | resolved 也等 tick；條件解除後還要 S + E 才被評估到[^ad-am-flush-resolved] | 預期 resolved 最多晚 S + E + `group_interval` |
| 假 resolved | Ruler 停掉或續送中斷超過 EndsAt，Alertmanager 自動 resolved[^ad-p-validuntil] | 見[續送契約](../contracts.md#resend-endsat)；ruler 本身的存活靠外部觀察者 |
| `repeat_interval` 設成 `group_interval` 的整數倍 | 實際多一個 `group_interval`[^ad-am-repeat][^ad-am-nflog-ts] | 算 DMS 等待時用 ⌈`repeat_interval` ÷ G⌉ × G + G |
| 以為 `resolve_timeout` 會決定自動 resolved | Prometheus、vmalert 一定帶 EndsAt，`resolve_timeout` 只用在沒有 EndsAt 的 alert[^ad-am-api-resolve-timeout] | 改 resend 或 E，不是 `resolve_timeout` |

## 參考

- 實驗：[lab/alert-delivery](https://github.com/HahaSula/alert-trust/tree/main/lab/alert-delivery)
- 相關頁：[Prometheus notifier](prometheus.md#notifier)、[vmalert 送通知](vmalert.md#notify)、[Alertmanager 架構](alertmanager.md)、[Alertmanager 監控指南](../reports/alertmanager.md)、[續送契約](../contracts.md#resend-endsat)、[告警視窗契約](../contracts.md#am-failure-window)、[等待時間契約](../contracts.md#deadman-wait)
