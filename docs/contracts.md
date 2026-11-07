# 信任契約

Rule 能不能信，取決於一組時間不等式。兩邊的比值就是邊際，小於 1 代表契約已經不成立，rule 會照常執行、不報錯，但結果不可信。

可以用[契約計算器](tools/calculator.md)代入自己的數字。

## 符號

| 符號 | 意思 | 從哪裡看 |
|---|---|---|
| `S` | 設定的 scrape interval | `/api/v1/targets` 的 `scrapeInterval` |
| `d` | 尖峰 scrape 耗時（不含 commit） | `max_over_time(scrape_duration_seconds[7d])` |
| `I` | 實際 scrape 間隔 | `d > S` 時約 `⌈d/S⌉ × S`，否則為 `S` |
| `E` | Rule group 的 evaluation interval | `prometheus_rule_group_interval_seconds` |
| `T` | Rule group 尖峰耗時 | `max_over_time(prometheus_rule_group_last_duration_seconds[7d])` |
| `R` | Range selector 長度 | 表達式裡的 `[5m]` |
| `O` | 查詢 offset | 表達式的 `offset`，或 2.53 起的 `query_offset` |
| `F` | `for` | Rule 定義 |
| `k` | 要容忍的 scrape 失敗次數 | 建議至少 1 |
| `L` | 資料從時間戳到可查詢的延遲（VM、VL） | `time() - timestamp(m)` 的尖峰，或 canary log 的端到端延遲 |
| `D` | vmalert 的 `eval_delay`（預設 30s） | `-rule.evalDelay` 或 group 設定 |

## Prometheus

### Scrape 要在 interval 內完成 {#scrape-within-interval}

`d ≤ S`

每個 target 只有一條 scrape goroutine，抓取、parse、relabel、append、commit 依序執行，沒跑完下一輪不會開始，tick 會被丟掉。所以 `d > S` 時實際間隔 `I` 會拉長，視窗內的樣本跟著變少。`scrape_timeout` 只管 HTTP 階段，`d` 超過 timeout 又成功時，多出來的時間都花在 Prometheus 內部。

架構位置：[Scrape loop](architecture/prometheus.md#scrape-loop)。

### 視窗內至少要有 2 個可見樣本 {#range-window}

- 沒有 offset：`R ≥ (2 + k) × I + d`
- `O ≥ d`：`R ≥ (2 + k) × I`

Sample 帶的是 scrape 開始的時間，要等 commit 後才查得到。Rule 查詢時，最新那段 `d` 的資料還沒寫入，所以視窗要再多涵蓋 `d`。視窗內不足 2 個點時 `rate` 回傳空，pending 被刪除、`for` 歸零，alert 可能永遠不 firing，而且不會有任何錯誤。實驗中 `R = 2I` 在 2.51.2 也會失敗，取決於 evaluation 的相位。

架構位置：[Scrape loop](architecture/prometheus.md#scrape-loop)、[Alert 狀態](architecture/prometheus.md#alert-state)。

### Rule group 要在 interval 內跑完 {#group-within-interval}

`T ≤ E`，預警門檻 `T > 0.8 × E`

同一個 group 的 rule 依定義順序一條接一條跑，兩輪不重疊。`T > E` 時下一輪被跳過，同 group 的所有 alert 都變慢，短暫事件可能完全漏掉。實驗中真正的轉折點就是這個比值超過 1；CPU throttling 已經很高時偵測延遲還幾乎沒變。vmalert 的 group 也是同樣的模型。

架構位置：[Rule group](architecture/prometheus.md#rule-group)。

### 不用 range 的 selector 也有回看上限 {#lookback}

`I + d < lookback`（預設 5m）

`up == 0` 這類規則沒有 range，靠 lookback 找最近一個點。實際間隔加上寫入延遲超過 5 分鐘時，它會間歇回傳空。

### 續送中斷要短於 EndsAt {#resend-endsat}

續送中斷 `< 4 × max(resend, E)`

Prometheus 與 vmalert 送出 alert 時都帶 `EndsAt = 評估時間 + 4 × max(resend, E)`。查詢失敗、ruler 停機或通知中斷超過這段時間，Alertmanager 就自動把 alert 標成 resolved。條件其實還成立，通知卻說解決了。

這也讓 dead man's switch 變慢：ruler 停機後 Alertmanager 仍會重送 Watchdog 到 `EndsAt`，見 [DMS 等待時間](#deadman-wait)。

架構位置：[Notifier](architecture/prometheus.md#notifier)。

## VictoriaMetrics 與 VictoriaLogs

### vmalert 的視窗 {#vm-range-window}

`R ≥ I + L − D`

vmalert 查詢時間會往前移 `D`（`eval_delay`），所以延遲 `L` 裡有 `D` 已經被吸收。違反時視窗內沒有可見樣本，查詢回空，`for` 永遠歸零。實驗的 120 個組合中，失敗的格子都違反這條；有一格違反 10 秒卻成立，是 evaluation 相位剛好避開，不能依賴。

### Log rule 的延遲 {#vl-log-delay}

`L ≤ D` 保證涵蓋；`L > D + E` 必定漏

沒有 `_time` 的 log rule 預設只查 `[ts − interval, ts]`，視窗不重疊。Log 到得比視窗晚，事件就永遠不觸發，事後卻查得到。關鍵 log rule 改用較長的 `_time` 視窗，代價是事件結束後要等整段視窗才 resolve。

## Alertmanager

### 失敗告警的視窗要跟著 group_interval {#am-failure-window}

告警視窗 `≥ 2 × 最大 group_interval`

失敗要等整輪 flush 結束才計數。`group_interval` 是 30m 時，單一 group 持續失敗的 counter 每 30 分鐘才加 1，`rate(...[5m]) > 0` 加 `for: 5m` 完全抓不到。

### Dead man's switch 的等待時間 {#deadman-wait}

DMS 等待 `> 4 × max(resend, E) + repeat_interval + group_interval`

等待太短會誤報；ruler 停機後 Alertmanager 還會繼續重送 Watchdog 直到 `EndsAt`，所以 DMS 最快也要這段時間加上它自己的等待才會響。要更快偵測 ruler 停機，靠外部觀察者的 `up`。
