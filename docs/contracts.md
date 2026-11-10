# 信任契約

Rule 能不能信，取決於一組時間不等式。不等式兩邊的比值就是**邊際**：大於 1 才成立；小於 1 時 rule 照常執行、不報錯，但結果不可信。

可以用[契約計算器](tools/calculator.md)代入自己的數字。

## 兩個體系都適用嗎

**概念相同，公式不完全相同。**Prometheus 和 VictoriaMetrics 都會遇到「scrape 太慢」「視窗太短」「group 跑不完」「續送中斷造成假 resolved」，但查詢語意和預設值不同，所以有幾條要分開寫。

| 契約 | Prometheus | VictoriaMetrics 體系 | 差在哪裡 |
|---|---|---|---|
| [Scrape 在 interval 內完成](#scrape-within-interval) | 適用 | 適用（vmagent） | 相同：兩者都是每個 target 一條迴圈，跑不完就丟 tick |
| [視窗內要有足夠樣本](#range-window) | `R ≥ (2+k)·I + max(0, d − O)`；`*_over_time` 是 [`(1+k)·I`](#range-window-1sample) 起算 | [`R ≥ (1+k)·I + L − D`](#vm-range-window) | vmalert 預設往前移 30s 查詢；MetricsQL 的 `rate` 會用視窗前的 sample，少要 1 個。各函式見[各 range 函式需要的視窗](#range-functions) |
| [Group 在 interval 內跑完](#group-within-interval) | 適用 | 適用（vmalert） | vmalert 查詢飽和時 `iteration_missed` 可能不動，要看錯誤比率 |
| [沒有 range 的查詢](#lookback) | 回看上限 5m，太慢會**回傳空** | 回看 `queryStep` 5m，資料停了**還會繼續成立** | 風險方向相反 |
| [續送中斷短於 EndsAt](#resend-endsat) | 中斷 < `4 × max(resend-delay, E) − P` | 中斷 < `4 × max(E, -rule.resendDelay) − P` | 公式相同，`P` 是實際續送週期；vmalert 查詢失敗就不續送 |
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
| `I` | 實際的資料間隔 | — | `prometheus_target_interval_length_seconds`；`d > S` 時約 `d`（讀碼，見[採集](#scrape-within-interval)），`⌈d/S⌉ × S` 是上限 |
| `E` | Rule group 的 evaluation interval | `evaluation_interval`，group 的 `interval` 覆寫 | `prometheus_rule_group_interval_seconds` |
| `T` | Rule group 耗時的尖峰 | — | Prometheus：`prometheus_rule_group_last_duration_seconds`；vmalert：`vmalert_iteration_duration_seconds` |
| `R` | Range selector 長度 | 表達式裡的 `[5m]` | 讀 rule 檔 |
| `O` | 查詢 offset | `query_offset`，或表達式的 `offset` | `/api/v1/rules` 的 `queryOffset`（2.53+） |
| `F` | Alert 的 for | `for` | `/api/v1/rules` 的 `duration` |
| `k` | 要容忍的連續 scrape 失敗次數，也就是視窗裡可以缺幾個 sample | 自己決定，建議至少 1 | `up == 0` 的連續次數 |
| `L` | 資料從時間戳到查得到的延遲（vmalert 體系的 `d`） | — | `time() - timestamp(m)` 的尖峰；log 用 canary 的端到端延遲 |
| `n` | 函式要幾個 sample 才有值 | 看函式 | [各 range 函式需要的視窗](#range-functions) |
| `D` | vmalert 的查詢延遲 | `eval_delay` | vmalert 的 `/flags` |

## 採集

### Scrape 要在 interval 內完成 {#scrape-within-interval}

`d ≤ S`

Prometheus 和 vmagent 都是每個 target 一條迴圈，抓取、parse、寫入依序執行，沒跑完就不會開始下一輪，tick 會被丟掉。[^p-scrape-run][^vmag-ticker]所以 `d > S` 時實際間隔 `I` 會拉長，查詢視窗裡的樣本跟著變少。

`I` 拉長多少（3.13.5 讀碼，沒有實測）：迴圈跑完一輪才等下一個 tick[^pqw-p3-scrape-wait-tick]，Go 的 ticker 只保留 1 個沒被收走的 tick、其餘丟掉[^pqw-go-ticker-drop][^pqw-go-ticker-buffer]，所以慢的那一輪結束時，保留的 tick 已經到了，下一輪立刻開始。Sample 的時間戳是每一輪開始的時間[^pqw-p3-scrape-time]，連續慢的時候 `I ≈ d`，不是 `⌈d/S⌉ × S`。`⌈d/S⌉ × S` 比較大，拿它算視窗是偏保守的上限。vmagent 的時間戳是 tick 的時間，和預期時間差超過 10% 才改用實際時間。[^pqw-vmag-tick-ts]

`scrape_timeout` 只管 HTTP 階段。`d` 超過 timeout 卻成功時，多出來的時間都花在 Prometheus 內部，加 CPU 對單一大 target 的幫助有限。

架構：[Prometheus scrape loop](architecture/prometheus.md#scrape-loop)、[vmagent scrape](architecture/vmagent.md#scrape)。

## 查詢視窗

### Prometheus：視窗內至少要有 2 個看得到的樣本 {#range-window}

- 沒有 offset：`R ≥ (2 + k) × I + d`
- `O ≥ d`：`R ≥ (2 + k) × I`
- 一般式：`R ≥ (n + k) × I + max(0, d − O)`，`n` 是函式要的 sample 數。這一節是 `n = 2`（`rate`、`increase`、`irate`、`delta`、`deriv` 等），`n = 1` 見[下一節](#range-window-1sample)，每個函式見[各 range 函式需要的視窗](#range-functions)。

推導，每一項都對應到原始碼：

1. Sample 的時間戳是 scrape **開始**的時間，整批寫入後才查得到。[^pqw-p3-scrape-time][^pqw-p3-scrape-append]所以 evaluation 在 `ts` 執行時，看得到的 sample 時間戳都 ≤ `ts − d`。這裡的 `d` 要用「scrape 開始到寫入完成」；`scrape_duration_seconds` 在寫入前就量完，實際的 `d` 比它略長。[^pqw-p3-scrape-duration]
2. Rule 用 `ts − O` 當查詢時間。[^pqw-p3-alert-query][^pqw-p3-record-query]視窗是 `(ts − O − R, ts − O]`，3.x 左開右閉[^pqw-p3-matrix-leftopen]（2.x 是閉區間[^pqw-p2-matrix-closed]）。
3. 兩者取交集，看得到的範圍是長度 `R − max(0, d − O)` 的左開區間。間隔 `I` 的 sample 落在長度 `W` 的左開區間裡，最差相位下有 `⌊W / I⌋` 個；要容忍 `k` 個失敗，需要 `⌊W / I⌋ ≥ n + k`。
4. Scrape 失敗時沒有 sample，Prometheus 寫的 staleness marker 在 range 裡被略過、不算數。[^pqw-p3-failed-stale][^pqw-p3-matrix-stale]所以 `k` 就是視窗裡缺的 sample 數。

`promtool test rules`（虛擬時鐘，不受這台機器的容器時鐘影響）逐一確認了每一項：`I` = 30s 時，`rate` 在最差相位 `R` = 59s 空、60s 有值；缺 1 次 scrape 時 60s 空、90s 有值；`d` = 20s（還沒寫入的 sample 不放進資料）時 79s 空、80s 有值，加 `offset 20s` 後回到 59s 空、60s 有值。[^pqw-lab-promtool]

`R` 剛好等於 `(n + k) × I` 只在時間戳完全等距時成立。Prometheus 只把 2ms 內的抖動對齊回去[^pqw-p3-scrape-tolerance]，超過就會少一個 sample，所以要留餘裕（多組數據裡 `R = 2S` 加 offset 仍有約 20% 空結果[^exp-jitter]）。

Sample 帶的是 scrape 開始的時間，要等寫入完成才查得到。Rule 查詢時，最新那段 `d` 的資料還沒寫進去，所以視窗要多涵蓋 `d`，或用 offset 避開。offset 小於 `d` 時只抵掉一部分，剩下的 `d − O` 仍要算進視窗。

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

### Prometheus：1 個 sample 就有值的函式 {#range-window-1sample}

`R ≥ (1 + k) × I + max(0, d − O)`

`avg_over_time`、`max_over_time`、`last_over_time` 等 `*_over_time`，以及 `changes`、`resets`，視窗裡有 1 個 sample 就有值。[^pqw-p3-aggr-over-time][^pqw-p3-changes-first]推導和[上一節](#range-window)相同，只是 `n = 1`。Lab 裡 `I` = 30s 時 29s 空、30s 有值；缺 1 次時 30s 空、60s 有值；`d` = 20s 時 49s 空、50s 有值。[^pqw-lab-promtool]

兩個要注意的地方：

- **`changes`、`resets` 只有 1 個 sample 時回傳 0。**結果不是空，但看不到任何變化；`changes(x[R]) > 0` 這類規則實際上要 2 個 sample，視窗照[上一節](#range-window)算。Lab 裡 gauge 每次都變，`changes(g[30s])` 仍是 0、`changes(g[60s])` 才是 1。[^pqw-lab-promtool]
- **`absent_over_time` 反過來。**視窗裡沒有 sample 就回傳 1[^pqw-p3-absent-over-time]，視窗太短時資料其實還在也會響（誤報），所以視窗下限一樣是 `(1 + k) × I + max(0, d − O)`；而 series 真的消失後要等整段 `R` 才會響。

### vmalert：視窗要涵蓋資料延遲 {#vm-range-window}

`R ≥ (1 + k) × I + max(0, L − D)`（`k` = 0 時就是原本的 `R ≥ I + L − D`）

vmalert 查詢時已經往前移了 `D`（`eval_delay`），所以延遲 `L` 裡有 `D` 已經被吸收；MetricsQL 的 `rate` 也會用到視窗前的最後一個點，需要的樣本比 Prometheus 少。[^vm-metricsql-prev]違反時查詢回空，`for` 永遠歸零。

「視窗內 1 個就夠」有前提：視窗前最後一個 sample 的時間要在 `(視窗起點 − step, 視窗起點]` 內，否則 `rate` 一樣要 2 個。[^pqw-vm-prev-value][^pqw-vm-rate-1sample]Instant query 的 step 就是 vmalert 送的 `-datasource.queryStep`（預設 5m）[^pqw-vma-step-param][^pqw-vm-maxprev-step]，`-search.minStalenessInterval` 可以把它拉長[^pqw-vm-minstale-use]。所以：

- `I`（含連續失敗 `k × I`）小於 queryStep 時：`R ≥ (1 + k) × I + max(0, L − D)`。
- queryStep 設得比 `I` 小，或用 `rate_prometheus`、`increase_prometheus` 這類照 Prometheus 算的函式：`R ≥ (2 + k) × I + max(0, L − D)`。

Lab 用明確時間戳匯入 VictoriaMetrics、指定查詢時間：`L` = 20s、`D` = 0 時 49s 空、50s 有值；`D` = 20s 時 29s 空、30s 有值；step 20s 時視窗內 1 個 sample 也是空，加 `-search.minStalenessInterval=1m` 後有值。[^pqw-lab-server]

另外，VictoriaMetrics 收到離現在不到 `-search.latencyOffset`（30s）的查詢時，會把查詢時間改成 now − 30s（沒帶 `nocache=1` 時）[^pqw-vm-latency-offset][^pqw-vm-latency-apply]，所以 `eval_delay` 設得比 30s 小時，實際的 `D` 至少是 30s（讀碼，沒有實測）。

Lab 的 12 格中，逐次比對 61 次 evaluation 與實際寫入時間，0 次不符；違反最多的格子從未 firing。只違反約 30 秒的格子仍會 firing，因為實際延遲介於 `eval_delay` 與 `eval_delay` + E 之間，成不成立看相位，所以這條公式是偏保守的設計值。[^exp-vm-matrix]

`eval_alignment`（預設 true）會再把查詢時間按 interval 向下取整，實際的 `D` 落在 `eval_delay` 到 `eval_delay + E` 之間。[^vma-evalalign][^vma-evaldelay]

架構：[vmalert 查詢時間](architecture/vmalert.md#query-time)。

### 各 range 函式需要的視窗 {#range-functions}

**結論：**最小視窗是 `R ≥ (n + k) × I + max(0, d − O)`，`n` 看函式。Prometheus 的 `rate` 這類算變化率的函式 `n = 2`，`*_over_time` `n = 1`；MetricsQL 的 `rate` 會用視窗前的 sample，多數函式 `n = 1`。表中的「最小視窗」都是 Prometheus 3.13.5；2.x 的最差情況相同，見表下說明。

| 函式 | 最少 sample | 外推 | 計數器重置 | 最小視窗（Prometheus） | MetricsQL 差異 | 證據 |
|---|---|---|---|---|---|---|
| `rate`、`increase` | 2 | 有：首尾 sample 離邊界不到平均間隔 1.1 倍就外推到邊界，否則只外推半個間隔；counter 不外推到負值 | 值下降時把前一個值加回 | `(2+k)·I + max(0, d−O)` | 視窗內 1 個＋視窗前 step 內 1 個就有值，不外推；`rate_prometheus`、`increase_prometheus` 要 2 個 | [^pqw-p3-extrap-min2][^pqw-p3-extrap-threshold][^pqw-p3-extrap-zero][^pqw-p3-rate-reset][^pqw-vm-rate][^pqw-vm-increase][^pqw-vm-rate-prometheus][^pqw-lab-promtool][^pqw-lab-server] |
| `delta` | 2 | 同上，但沒有「不到負值」的限制 | 不處理 | `(2+k)·I + max(0, d−O)` | 視窗內 1 個＋前一個 sample | [^pqw-p3-extrap-min2][^pqw-vm-increase][^pqw-lab-promtool][^pqw-lab-server] |
| `irate` | 2 | 不外推，只用最後 2 個 | 下降時用後一個值當增量 | `(2+k)·I + max(0, d−O)` | 視窗內 1 個＋前一個 sample | [^pqw-p3-instant-min2][^pqw-p3-irate-reset][^pqw-vm-irate][^pqw-lab-promtool][^pqw-lab-server] |
| `idelta` | 2 | 不外推 | 不處理 | `(2+k)·I + max(0, d−O)` | 視窗內 1 個＋前一個 sample | [^pqw-p3-instant-min2][^pqw-lab-promtool][^pqw-lab-server] |
| `deriv`、`predict_linear` | 2 | 線性迴歸；`predict_linear` 再往後推 t 秒 | 不處理 | `(2+k)·I + max(0, d−O)` | 1 個 sample 就有值：斜率當 0（`deriv` 回傳 0，不是空） | [^pqw-p3-deriv-min2][^pqw-p3-predict-min2][^pqw-vm-deriv-const][^pqw-lab-promtool][^pqw-lab-server] |
| `double_exponential_smoothing`（3.x，實驗函式）、`holt_winters`（2.x） | 2 | 平滑，不外推 | 不處理 | `(2+k)·I + max(0, d−O)` | `holt_winters` 視窗內 1 個＋前一個 sample | [^pqw-p3-des-min2][^pqw-p3-des-experimental][^pqw-p2-holt-winters][^pqw-lab-promtool][^pqw-lab-server] |
| `changes`、`resets` | 1 有值；2 才看得到變化 | — | `resets` 數值下降次數 | 有值 `(1+k)·I + max(0, d−O)`；偵測變化 `(2+k)·I + max(0, d−O)` | 把視窗前的 sample 算進去：1 個就看得到變化 | [^pqw-p3-changes][^pqw-p3-resets][^pqw-p3-changes-first][^pqw-vm-changes][^pqw-lab-promtool][^pqw-lab-server] |
| `avg`、`min`、`max`、`sum`、`count`、`last`、`quantile`、`stddev`、`stdvar`、`present_over_time`；`mad`、`first_over_time`（實驗） | 1 | — | 不處理（counter 照原值算） | `(1+k)·I + max(0, d−O)` | 相同，不用前一個 sample | [^pqw-p3-aggr-over-time][^pqw-p3-last-over-time][^pqw-p3-compare-over-time][^pqw-p3-quantile-over-time][^pqw-p3-variance-over-time][^pqw-p3-present-over-time][^pqw-p3-mad-over-time][^pqw-lab-promtool][^pqw-lab-server] |
| `absent_over_time` | 0 個時回傳 1 | — | — | 不誤報：`(1+k)·I + max(0, d−O)`；消失後要等 `R` 才響 | 相同 | [^pqw-p3-absent-over-time][^pqw-vm-absent][^pqw-lab-promtool][^pqw-lab-server] |
| `anchored` 修飾（3.x 實驗，`rate`、`increase`、`delta`、`changes`、`resets`） | 視窗內 1 個＋視窗前 lookback 內的錨點 | 不外推，用錨點的值 | 有 | `(1+k)·I + max(0, d−O)` | 和 MetricsQL 的做法相近 | [^pqw-p3-anchored-safe][^pqw-p3-anchored-lookback][^pqw-p3-extend-floats][^pqw-p3-extended-rate][^pqw-p3-ext-flag][^pqw-lab-server] |
| `smoothed` 修飾（3.x 實驗，`rate`、`increase`、`delta`） | 視窗後有 sample 時 0 個也有值；在「現在」查詢時同 `anchored` | 在兩端內插 | 有 | 沒有 offset 時同 `anchored` | — | [^pqw-p3-extend-floats][^pqw-lab-server] |
| Subquery `f(expr[R:res])` | 步點數 ⌊R / res⌋ | 看 `f` | 看 `f` | `R ≥ n_f × res`；`expr` 裡的 range 照各自的列算 | 不在這次範圍 | [^pqw-p3-subquery-start][^pqw-lab-promtool] |

**每個函式都適用的行為：**

- **視窗邊界。**3.x 與 VictoriaMetrics 都是左開右閉 `(t − R, t]`，2.x 是閉區間 `[t − R, t]`。[^pqw-p3-matrix-leftopen][^pqw-p2-matrix-closed][^pqw-vm-window-leftopen]最差相位下兩者需要的 `R` 一樣；差別在 evaluation 剛好和 scrape 對齊時，2.x 會多算邊界上那一個。Lab 裡 `rate(c[30s])` 在對齊的時間點 2.51.2 有值、3.13.5 是空；同一條 `rate(x[1m])` + 缺 1 次 scrape 的 alert，2.51.2 會 firing、3.13.5 不會。實際環境的 scrape 與 evaluation 相位是錯開的[^pqw-p3-scrape-offset][^pqw-p3-group-evalts]，不能靠對齊。[^pqw-lab-promtool]
- **Staleness marker。**Range 函式直接略過 marker，marker 之前的 sample 照算；`last_over_time(x[R])` 在 series 已經 stale 後仍回傳舊值，最多持續 `R`。VictoriaMetrics 也會先把 marker 拿掉（`default_rollup` 除外）。[^pqw-p3-matrix-stale][^pqw-vm-drop-stale][^pqw-lab-promtool]
- **Subquery 會補值。**`expr[R:res]` 的步點對齊 `res` 的整數倍；`expr` 是裸 selector 時每個步點靠 lookback（5m）拿最近的值，缺掉的 scrape 會被前一個值補上。Lab 裡 `count_over_time(c_gap[2m])` 是 3，`count_over_time(c_gap[2m:30s])` 是 4。最新的步點最多比查詢時間早 `res`。[^pqw-p3-subquery-start][^pqw-lab-promtool]

**MetricsQL（VictoriaMetrics v1.155.0）的差異：**

| 項目 | 行為 | 對視窗的影響 | 證據 |
|---|---|---|---|
| 視窗前的 sample | `rate`、`increase`、`irate`、`delta`、`idelta`、`changes`、`resets`、`holt_winters` 用視窗前最後一個 sample，條件是它在 `(起點 − step, 起點]` 內、而且視窗內至少有 1 個 sample | `n = 1`；vmalert 的 step 是 `-datasource.queryStep`（預設 5m） | [^pqw-vm-prev-value][^pqw-vm-maxprev-step][^pqw-vma-step-param][^pqw-lab-server] |
| `-search.minStalenessInterval` | 把「step 內」的下限拉長 | step 比 `I` 小時用它補救；lab 裡 step 20s 時 `rate(c[30s])` 空，設 1m 後有值 | [^pqw-vm-minstale-flag][^pqw-vm-minstale-use][^pqw-lab-server] |
| 省略視窗 `rate(x)` | 視窗 = step；step 比估計的 scrape 間隔小時拉長到它（instant query 的估計值就是 step） | vmalert 預設等於 `rate(x[5m])`；queryStep 改小時視窗跟著變小，lab 裡 step 20s 時 `rate(c)` 空 | [^pqw-vm-default-window][^pqw-vm-instant-step][^pqw-vm-default-step][^pqw-lab-server] |
| 計數器重置 | 查詢前先把整條 series 的重置修正掉 | 不影響 sample 數 | [^pqw-vm-counter-resets] |
| `deriv`、`predict_linear` | 1 個 sample 時斜率當 0 | `deriv(x[R]) > 0` 在只有 1 個 sample 時是 0，不是空 | [^pqw-vm-deriv-const][^pqw-lab-server] |

所以 [vm-range-window](#vm-range-window) 的 `R ≥ I + L − D` 對應的是 `n = 1`、`k = 0`；照 Prometheus 算的函式（`*_prometheus`）或 queryStep 比 `I` 小時要改用 `n = 2`。

### 沒有 range 的查詢 {#lookback}

`up == 0` 這類規則沒有寫 range，要靠回看找最近一個點。最近一個看得到的 sample 最舊是 `I + max(0, d − O)` 之前，3.x 要嚴格小於 lookback（`t ≤ ref − lookback` 就不算），2.x 可以等於。[^pqw-p3-lookback-open][^pqw-p2-lookback-closed]

這條只管 sample **晚到**。Scrape 失敗或 target 消失時，Prometheus 會寫 staleness marker，instant selector 在 marker 之後立刻變空，不會等 5m。[^pqw-p3-failed-stale][^pqw-p3-instant-stale]Lab 裡 marker 之前 `s` 有值、之後馬上是空。[^pqw-lab-promtool]

兩個體系的風險方向相反：

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

續送中斷 `< 4 × max(resend, E) − P`，`P = E × (⌊resend ÷ E⌋ + 1)` 是實際的續送週期

Prometheus 和 vmalert 送出的 alert 都帶 `EndsAt`。查詢失敗、ruler 停機或通知中斷超過這段時間，Alertmanager 就自動把 alert 標成 resolved：條件其實還成立，通知卻說解決了。

`EndsAt` 從**上一次送出**起算 `4 × max(resend, E)`。[^pqw-p3-validuntil][^pqw-vma-resolve-duration]下一次送出要等到 `上次送出 + resend` **之後**的第一個 evaluation（嚴格大於）[^pqw-p3-needs-sending][^pqw-vma-needs-sending]，所以實際週期 `P` 是 `E × (⌊resend ÷ E⌋ + 1)`：Prometheus 預設 resend 1m、`E` = 30s 時是 90 秒，不是 60 秒。中斷剛好從「該送的那一次」開始時，上一次送出已經是 `P` 之前，能撐的時間只剩 `4 × max(resend, E) − P`。這是讀碼的結論，lab 沒有量最差相位。

| | Prometheus | vmalert |
|---|---|---|
| EndsAt | `4 × max(--rules.alert.resend-delay, E)`[^p-alert-validuntil][^p-resenddelay] | `4 × max(E, -rule.resendDelay)`；`-rule.maxResolveDuration` 可設上限[^vma-resend] |
| 預設值下的寬限 | resend-delay 預設 1m，`E` = 30s 時 EndsAt 是 4 分鐘，最差只能撐 2.5 分鐘（4m − 90s） | resendDelay 預設 0，`E` = 30s 時 EndsAt 是 2 分鐘，最差只能撐 1.5 分鐘（每次都送，`P = E`） |
| 拉長的代價 | Watchdog 也要更久才會斷 | 同左；Watchdog 要放在不設 resendDelay 的獨立 vmalert |

實驗中把 `-rule.resendDelay` 設成 3m 後，4 分鐘的查詢故障沒有出現假 resolved；代價是部分真正的 resolved 會晚 2–3 分鐘才到。[^exp-vm-resend]

架構：[Prometheus notifier](architecture/prometheus.md#notifier)、[vmalert 送通知](architecture/vmalert.md#notify)。

### Alertmanager：失敗告警的視窗要跟著 group_interval {#am-failure-window}

告警視窗 `≥ 2 × 最大的 group_interval`

失敗要等整輪 flush 結束才計數。`group_interval` 是 1h 時，單一 group 持續失敗的 counter 每 1 小時才加 1，`rate(...[5m]) > 0` 加 `for: 5m` 完全抓不到。[^exp-am-window]

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

## Thanos

Thanos Ruler 用的是 Prometheus 的 rules manager，[視窗](#range-window)、[group](#group-within-interval)、[續送](#resend-endsat)三條契約照用（續送的公式是 `4 × max(--resend-delay, interval)`，兩者預設都是 1m）。下面兩條是 Thanos 多出來的。

### Partial response 的 abort 只管還在名單上的 store {#thanos-partial-abort}

store 斷線 `<` 一次健康檢查（`endpoint.info-timeout`，5s）時 abort 才會讓 rule 失敗

Querier 每 5 秒檢查一次每個 StoreAPI，檢查失敗的非 strict endpoint 不再查詢[^th-es-update][^th-es-queryable]。之後同一個查詢只問剩下的 store；沒有 store 涵蓋這段時間時直接回空結果，不是錯誤[^th-proxy-nomatch]。

| 情況 | warn group | abort group |
|---|---|---|
| store 斷線後 5 秒內（還在名單上） | 成功，帶 warning | 失敗 |
| store 已被移除，還有其他 store | 成功，**沒有 warning**，少資料 | 成功，**沒有 warning**，少資料 |
| 一個 store 都沒有 | 成功，空結果 | 失敗（`No StoreAPIs available`）[^th-proxy-nostores] |

Lab 裡停掉 sidecar 後，兩種 group 的 `count(up) > 0` 在下一次取樣（+22s）前就被當成真的 resolved 送到 Alertmanager，rule 的 health 一直是 `ok`。[^exp-thanos-store-loss]所以要靠 querier 的 `thanos_store_nodes_grpc_connections` 告警，或把關鍵 store 設成 strict，不能只靠 abort。

架構：[Querier 扇出](architecture/thanos.md#querier)。

### Sidecar 上傳與 Prometheus 保留時間 {#thanos-upload-retention}

上傳中斷 `+ 2h + --sync-block-duration < Prometheus 本地保留`

Sidecar 每 30 秒檢查一次有沒有新的 2h block 可以上傳[^th-sc-ship]，它對 querier 宣告的時間範圍從 Prometheus 最舊的樣本開始[^th-sc-maxt]。Prometheus 依保留時間刪掉的 block，sidecar 就不再提供；這時 block 要已經上傳、而且被 store gateway 同步到（`--sync-block-duration` 預設 15m）[^th-store-sync]，否則那段時間查不到，也沒有錯誤。官方文件建議保留至少 6h（3 個 block）。

架構：[Sidecar](architecture/thanos.md#sidecar)。

## Grafana

### 資料消失時的處理方式 {#grafana-nodata-error}

`no_data_state`、`exec_err_state` ≠ OK；選 NoData／Error 時，原 alert 在 `missing_series_evals_to_resolve` × E（預設 2E）後 resolved，`DatasourceNoData`／`DatasourceError` 要有路由

Grafana-managed rule 在查詢沒有結果（NoData）或查詢失敗（Error）時，不是固定行為，而是看每條 rule 的設定[^graf-nodata-all][^graf-error-all]：

| 設定 | 外部 Alertmanager 收到 | 風險 |
|---|---|---|
| OK | 原 alert 下一次評估就 resolved[^graf-nodata-ok][^graf-err-ok] | 看起來像問題解決了；NoData 沒有任何 counter |
| Alerting、KeepLast | 原 alert 繼續 firing | 看不出資料已經消失；API／provisioning 沒寫 `exec_err_state` 時預設就是 Alerting[^graf-api-defaults] |
| NoData／Error | `DatasourceNoData`／`DatasourceError` firing（原名在 `rulename`），原 alert 約 2 個 interval 後 resolved[^graf-missing-series][^graf-missing-default] | 用 `alertname` 做 matcher 的路由收不到新的 alert |

Lab 裡四種設定都照上表發生。[^exp-graf-modes]

架構：[Grafana NoData 與 Error](architecture/grafana-alerting.md#nodata-error)。
