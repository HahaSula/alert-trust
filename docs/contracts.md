# 信任契約

Rule 能不能信，取決於一組時間不等式。不等式兩邊的比值就是**邊際**：大於 1 才成立；小於 1 時 rule 照常執行、不報錯，但結果不可信。

可以用[契約計算器](tools/calculator.md)代入自己的數字。

## 兩個體系都適用嗎

**概念相同，公式不完全相同。**Prometheus 和 VictoriaMetrics 都會遇到「scrape 太慢」「視窗太短」「group 跑不完」「續送中斷造成假 resolved」，但查詢語意和預設值不同，所以有幾條要分開寫。

| 契約 | Prometheus | VictoriaMetrics 體系 | 差在哪裡 |
|---|---|---|---|
| [Scrape 在 interval 內完成](#scrape-within-interval) | 適用 | 適用（vmagent） | 相同：兩者都是每個 target 一條迴圈，跑不完就丟 tick |
| [視窗內要有足夠樣本](#range-window) | `R ≥ (2+k)·I + d` | [`R ≥ I + L − D`](#vm-range-window) | vmalert 預設往前移 30s 查詢，`rate` 的取樣方式也不同 |
| [Group 在 interval 內跑完](#group-within-interval) | 適用 | 適用（vmalert） | vmalert 查詢飽和時 `iteration_missed` 可能不動，要看錯誤比率 |
| [沒有 range 的查詢](#lookback) | 回看上限 5m，太慢會**回傳空** | 回看 `queryStep` 5m，資料停了**還會繼續成立** | 風險方向相反 |
| [續送中斷短於 EndsAt](#resend-endsat) | `4 × max(resend-delay, E)` | `4 × max(E, -rule.resendDelay)` | 公式相同；vmalert 查詢失敗就不續送 |
| [Log rule 的延遲](#vl-log-delay) | 不適用 | 適用（VictoriaLogs） | |
| [Alertmanager 告警視窗](#am-failure-window) | 共用 | 共用 | Alertmanager 兩邊共用 |
| [Dead man's switch 等待時間](#deadman-wait) | 共用 | 共用 | |
| [Exporter 的查詢期限](#exporter-deadline) | 共用 | 共用 | 跟著 exporter 走，和 ruler 無關 |

## 符號

每個符號都對應一個明確的設定或 metric，點名稱可以看設定位置、預設值和原始碼依據。

| 符號 | 意思 | 設定在哪裡 | 實際值從哪裡看 |
|---|---|---|---|
| `S` | Scrape interval（設定值） | `scrape_interval` | `/api/v1/targets` 的 `scrapeInterval` |
| `d` | Scrape 耗時的尖峰（不含寫入） | — | `max_over_time(scrape_duration_seconds[7d])`；名稱見 `scrape_duration_seconds` |
| `I` | 實際的資料間隔 | — | `prometheus_target_interval_length_seconds`；`d > S` 時約 `⌈d/S⌉ × S` |
| `E` | Rule group 的 evaluation interval | `evaluation_interval`，group 的 `interval` 覆寫 | `prometheus_rule_group_interval_seconds` |
| `T` | Rule group 耗時的尖峰 | — | Prometheus：`prometheus_rule_group_last_duration_seconds`；vmalert：`vmalert_iteration_duration_seconds` |
| `R` | Range selector 長度 | 表達式裡的 `[5m]` | 讀 rule 檔 |
| `O` | 查詢 offset | `query_offset`，或表達式的 `offset` | `/api/v1/rules` 的 `queryOffset`（2.53+） |
| `F` | Alert 的 for | `for` | `/api/v1/rules` 的 `duration` |
| `k` | 要容忍的 scrape 失敗次數 | 自己決定，建議至少 1 | — |
| `L` | 資料從時間戳到查得到的延遲 | — | `time() - timestamp(m)` 的尖峰；log 用 canary 的端到端延遲 |
| `D` | vmalert 的查詢延遲 | `eval_delay` | vmalert 的 `/flags` |

## 採集

### Scrape 要在 interval 內完成 {#scrape-within-interval}

`d ≤ S`

Prometheus 和 vmagent 都是每個 target 一條迴圈，抓取、parse、寫入依序執行，沒跑完就不會開始下一輪，tick 會被丟掉。[^p-scrape-run][^vmag-ticker]所以 `d > S` 時實際間隔 `I` 會拉長，查詢視窗裡的樣本跟著變少。

`scrape_timeout` 只管 HTTP 階段。`d` 超過 timeout 卻成功時，多出來的時間都花在 Prometheus 內部，加 CPU 對單一大 target 的幫助有限。

架構：[Prometheus scrape loop](architecture/prometheus.md#scrape-loop)、[vmagent scrape](architecture/vmagent.md#scrape)。

## 查詢視窗

### Prometheus：視窗內至少要有 2 個看得到的樣本 {#range-window}

- 沒有 offset：`R ≥ (2 + k) × I + d`
- `O ≥ d`：`R ≥ (2 + k) × I`

Sample 帶的是 scrape 開始的時間，要等寫入完成才查得到。Rule 查詢時，最新那段 `d` 的資料還沒寫進去，所以視窗要多涵蓋 `d`，或用 offset 避開。

視窗內不足 2 個點時 `rate` 回傳空，pending 被刪除、`for` 歸零，alert 可能永遠不 firing，而且沒有任何錯誤。[^exp-prom-silent][^p-alert-delete]`R = 2I` 不夠安全：只要 evaluation 剛好落在 scrape 進行中，當次就只剩 1 個點。[^exp-prom-range]

2.51 沒有 `query_offset`，要把 `offset` 寫在每個 selector 上，實驗確認有效。[^exp-prom-silent]


<!-- figure: scrape-window-timeline -->

**圖**　**機制**（原始碼）：sample 的時間戳是 scrape 開始的時間，寫入後才查得到；視窗內不足 2 點時 `rate` 回傳空。**這組數據**：I = 120 秒、d ≈ 100 秒。綠點是 rule 執行當下已寫入的 sample，空心紅點是時間戳在視窗內但還沒寫入的 sample。Evaluation 落在 scrape 開始後 0–60 秒（A）看得到 2 點；60–100 秒（B）只剩 1 點；100–120 秒最新一點已寫入，又回到 2 點；加上 2 分鐘 offset（C）後，視窗移到已寫入的區段。10 組不同參數的實測見[多組數據](https://github.com/HahaSula/alert-trust/blob/main/lab/jitter/results/run-20261108.md)。[^p-scrape-report][^p-extrapolated][^exp-prom-silent][^exp-jitter]
{: .at-caption }

<!-- figure: pending-reset -->

**圖**　**機制**（原始碼）：evaluation 成功但結果裡沒有這個 alert 時，pending 被刪除，`ActiveAt` 重設。**這組數據**：`evaluation_interval` 30 秒、I = 120 秒時，每個 scrape 週期只有 4 個固定的 evaluation 時間點，看得到的 sample 數依「成立、成立、空、空」循環；`for: 3m` 需要連續 7 次成立，永遠到不了。依相位不同，空結果是 25% 或 50%（平均 33%）；多組數據中這個條件在兩個版本都量到約 50%，pending 重新開始 10–11 次，從未 firing。[^p-alert-delete][^exp-prom-silent][^exp-jitter]
{: .at-caption }

**多組數據的結論。**10 組參數在 2.51.2 與 3.13.5 各跑一次[^exp-jitter]：留有餘裕的組合（`O ≥ d`，或 `R ≥ 3I`）全部 0% 空結果並 firing；視窗剛好等於 2S 的組合即使加了 offset 仍有約 20% 空結果，因為 scrape 時間戳有毫秒級抖動；`s120-d60-r5m` 在 2.51.2 會 firing、在 3.13.5 從未 firing，這是 3.x 視窗改成左開區間造成的。

**換成 `irate` 沒有用。**兩個函式都至少要 2 個 sample，視窗內只剩 1 點時一樣回傳空。差別只在有值時怎麼算：

<!-- figure: rate-vs-irate -->

**圖**　同一個視窗裡 `rate` 與 `irate` 用到的範圍。`rate` 的外推門檻是平均間隔的 1.1 倍，最後一點離視窗終點太遠時只外推半個間隔，所以被系統性算低。`irate` 只用最後兩點、不外推，不會低估，但只反映最後一個間隔，雜訊大。能同時解決空結果、低估、事後對不上的是查詢 offset。[^p-extrapolated][^p-instantvalue]
{: .at-caption }

架構：[Scrape loop](architecture/prometheus.md#scrape-loop)、[Alert 狀態](architecture/prometheus.md#alert-state)。

### vmalert：視窗要涵蓋資料延遲 {#vm-range-window}

`R ≥ I + L − D`

vmalert 查詢時已經往前移了 `D`（`eval_delay`），所以延遲 `L` 裡有 `D` 已經被吸收；MetricsQL 的 `rate` 也會用到視窗前的最後一個點，需要的樣本比 Prometheus 少。[^vm-metricsql-prev]違反時查詢回空，`for` 永遠歸零。

實驗的 120 個組合中，失敗的格子都違反這條。有一格違反了 10 秒卻成立，是 evaluation 相位剛好避開，不能依賴，所以這條公式是偏保守的設計值。[^exp-vm-matrix]

`eval_alignment`（預設 true）會再把查詢時間按 interval 向下取整，實際的 `D` 落在 `eval_delay` 到 `eval_delay + E` 之間。[^vma-evalalign][^vma-evaldelay]

架構：[vmalert 查詢時間](architecture/vmalert.md#query-time)。

### 沒有 range 的查詢 {#lookback}

`up == 0` 這類規則沒有寫 range，要靠回看找最近一個點。兩個體系的風險方向相反：

| | Prometheus | vmalert |
|---|---|---|
| 回看上限 | lookback，預設 5m | `-datasource.queryStep`，預設 5m[^p-lookback][^vma-querystep] |
| 條件 | `I + d < 5m` | 資料停止後最多再成立 5m |
| 違反時 | 間歇**回傳空**，alert 不響 | 資料停了條件**還會繼續成立**約 6 分鐘，alert 不會 resolve[^exp-vm-faults] |

## 評估

### Rule group 要在 interval 內跑完 {#group-within-interval}

`T ≤ E`，預警門檻 `T > 0.8 × E`

同一個 group 的 rule 依序執行，兩輪不重疊。`T > E` 時下一輪被跳過，同 group 的所有 alert 都變慢，短暫事件可能完全漏掉。

實驗中真正的轉折點就是這個比值超過 1：CPU throttling 已經很高時，偵測延遲還幾乎沒變；比值一過 1 才開始跳過 evaluation。[^exp-prom-cpu][^p-group-missed]

vmalert 的 group 也是同樣的模型，但有兩點不同：查詢飽和時查詢失敗得很快，`vmalert_iteration_missed_total` 可能一直是 0；每條 rule 會同步等所有 Alertmanager 回應，Alertmanager 變慢也會拉長 `T`。[^vma-missed][^vma-exec-send]

架構：[Prometheus rule group](architecture/prometheus.md#rule-group)、[vmalert group](architecture/vmalert.md#group)、[vmalert 送通知](architecture/vmalert.md#notify)。

## 通知

### 續送中斷要短於 EndsAt {#resend-endsat}

續送中斷 `< 4 × max(resend, E)`

Prometheus 和 vmalert 送出的 alert 都帶 `EndsAt`。查詢失敗、ruler 停機或通知中斷超過這段時間，Alertmanager 就自動把 alert 標成 resolved：條件其實還成立，通知卻說解決了。

| | Prometheus | vmalert |
|---|---|---|
| EndsAt | `4 × max(--rules.alert.resend-delay, E)`[^p-alert-validuntil][^p-resenddelay] | `4 × max(E, -rule.resendDelay)`；`-rule.maxResolveDuration` 可設上限[^vma-resend] |
| 預設值下的寬限 | resend-delay 預設 1m，`E` = 30s 時是 4 分鐘 | resendDelay 預設 0，`E` = 30s 時只有 2 分鐘 |
| 拉長的代價 | Watchdog 也要更久才會斷 | 同左；Watchdog 要放在不設 resendDelay 的獨立 vmalert |

實驗中把 `-rule.resendDelay` 設成 3m 後，8 分鐘的查詢故障都沒有出現假 resolved。[^exp-vm-resend]

架構：[Prometheus notifier](architecture/prometheus.md#notifier)、[vmalert 送通知](architecture/vmalert.md#notify)。

### Alertmanager：失敗告警的視窗要跟著 group_interval {#am-failure-window}

告警視窗 `≥ 2 × 最大的 group_interval`

失敗要等整輪 flush 結束才計數。`group_interval` 是 30m 時，單一 group 持續失敗的 counter 每 30 分鐘才加 1，`rate(...[5m]) > 0` 加 `for: 5m` 完全抓不到。[^exp-am-window]

架構：[Aggregation group](architecture/alertmanager.md#aggregation-group)。

### Dead man's switch 的等待時間 {#deadman-wait}

DMS 等待 `> 4 × max(resend, E) + repeat_interval + group_interval`

等待太短會誤報。另一方面，ruler 停機後 Alertmanager 還會繼續重送 Watchdog 直到 `EndsAt`，所以 DMS 最快也要這段時間加上自己的等待才會響。[^exp-prom-heartbeat]要更快發現 ruler 停機，靠外部觀察者的 `up`。

## Log

### Log rule 的延遲 {#vl-log-delay}

`L ≤ D` 保證涵蓋；`L > D + E` 必定漏掉

沒有寫 `_time` 的 log rule，vmalert 預設只查 `[ts − interval, ts]`，視窗不重疊。Log 到得比視窗晚，事件就永遠不觸發，事後卻查得到。[^exp-vm-vl]關鍵的 log rule 改用較長的 `_time` 視窗，代價是事件結束後要等整段視窗才 resolve。

架構：[Log rule 的視窗](architecture/victorialogs.md#log-rule-window)。

## Target

### Exporter 的查詢期限要比 scrape_timeout 短 {#exporter-deadline}

`exporter 自己的查詢期限 < scrape_timeout < scrape_interval`

Exporter 收到 scrape 才去查服務。它的期限比 `scrape_timeout` 長時，Prometheus 先放棄，`up` 變 0、這個 target 的所有 series 消失，看起來像 exporter 掛了，exporter 端的查詢卻還在跑。

| Exporter | 自己的期限 | 實驗 |
|---|---|---|
| mysqld_exporter | Prometheus 送來的 scrape timeout 減 `--timeout-offset`（預設 0.25s）[^my-offset] | 資料庫停機時 scrape 耗時 3.75s，正好是 4s 減 0.25s[^exp-mysql] |
| postgres_exporter | `--collection-timeout`，**預設 1m**；`/probe` 模式沒有逾時[^pg-timeout-flag][^pg-probe] | 資料庫主機不回應時一次 scrape 要 17–30 秒以上，Prometheus 4 秒就放棄[^exp-pg] |

所以預設期限偏長的 exporter，要把期限設得比 `scrape_timeout` 短，並用 `scrape_duration_seconds > 0.8 × scrape_timeout` 提前預警。

架構：[Exporter](architecture/exporters.md)。
