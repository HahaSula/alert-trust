# scrape 與 evaluation 的基準值怎麼定

時間契約要代入 `scrape_interval`（S）、`scrape_timeout`（τ）、尖峰 scrape 耗時（d）和 `evaluation_interval`（E）。這頁說明這些基準值怎麼決定：先看預設值與硬性限制，再看官方與社群的建議，最後是從自己的數據推導的步驟。

## 結論

- **沒有官方的「最佳值」，也沒有官方的 timeout 餘裕倍數。**官方只給預設值（Prometheus 1m／10s／1m）和一條硬性限制：`scrape_timeout` 不能大於 `scrape_interval`。[^bl-p3-default-interval][^bl-p3-default-timeout][^bl-p3-default-eval][^bl-p3-job-timeout-check]各家的預設值與建議並不一致（15s、30s、60s 都有），見[來源怎麼說](#sources)。
- **所以基準值要從契約反推**：先選 S（偵測速度與成本），再用 7 天以上的 scrape 耗時分布定 τ 和 exporter 的期限，`E = S`，最後算出每條 rule 的最短視窗。步驟見[推導步驟](#procedure)。
- **「越小越好」不成立。**S 變小，偵測快一點，但 sample 數、磁碟、exporter 對資料庫的查詢次數都等比增加；`scrape_timeout` 也跟著被壓在 S 以下，偶爾慢的 scrape 更容易逾時。
- **「平常 < 5 秒、偶爾超過」的答案**：偶爾超過本身沒問題，只要 HTTP 階段還在 τ 內、整次 scrape 還在 S 內。超過 τ 的那一次會失敗：`up` 變 0，這個 target 的所有 series 立刻被寫入 stale 標記。[^bl-p3-scrape-ctx][^bl-p3-failed-stale][^bl-p3-up-health]所以 τ 要蓋過觀察到的尖峰（不是平均），而 rule 視窗的 `k` 要涵蓋「連續失敗幾次」。
- **建議的第一組數字（例子）**：S = 30s、τ = 依數據（常見 10–20s）、`E = S`、`k = 1`、`rate` 類視窗 ≥ `3S + d`（約 2m）。這和 prometheus-operator 的 30s／30s 預設一致。[^bl-op-scrape-interval][^bl-op-eval-interval]

## 預設值與硬性限制 {#defaults}

| 項目 | Prometheus v3.16.0 | vmagent／vmalert v1.155.0 | 依據 |
|---|---|---|---|
| `scrape_interval` 預設 | 1m | 1m | [^bl-p3-default-interval][^vmag-default-interval] |
| `scrape_timeout` 預設 | 10s，但不超過該 job 的 S（`min(10s, S)`） | 10s | [^bl-p3-global-timeout-min][^bl-p3-job-timeout-min][^bl-vmag-default-timeout] |
| `scrape_timeout` > `scrape_interval` | **設定檔載入失敗**（global 與每個 job 都檢查） | **自動改成等於 S，不報錯** | [^bl-p3-global-timeout-check][^bl-p3-job-timeout-check][^bl-vmag-timeout-clamp] |
| `evaluation_interval` 預設 | 1m；group 沒寫 `interval` 時用它 | `-evaluationInterval` 1m | [^bl-p3-default-eval][^bl-p3-group-default-itv][^vma-eval-interval] |
| 查詢往前移 | `rule_query_offset` 預設 0；group 的 `query_offset` 優先 | `-rule.evalDelay` 預設 30s | [^bl-p3-default-query-offset][^bl-p3-group-query-offset][^vma-evaldelay] |
| lookback | `--query.lookback-delta` 5m | （`-datasource.queryStep` 5m，見[契約](../contracts.md#lookback)） | [^bl-p3-lookback-flag][^bl-p3-doc-staleness] |
| 每個 target 的起始時間 | 由 target 的 hash 決定，固定落在 interval 內的某個位置 | 隨機落在 `[0, scrape_interval]` | [^bl-p3-offset-hash][^bl-vm-doc-random-offset][^vmag-randsleep] |
| 送給 exporter 的期限 | `X-Prometheus-Scrape-Timeout-Seconds` = τ | 同一個 header | [^bl-p3-timeout-header][^bl-vmag-timeout-header] |
| `scrape_timeout_seconds` metric | 設定 `extra_scrape_metrics: true` 才有（3.x 已是設定項） | 一直都有 | [^bl-p3-doc-extra-metrics][^bl-vm-doc-timeout-ratio] |
| 最大回應大小 | `body_size_limit`（預設不限） | `-promscrape.maxScrapeSize` 16MiB | [^bl-vmag-maxscrapesize] |

### Scrape 逾時的後果

1. τ 只管 HTTP 請求與讀取 body。之後的 parse 與寫入不受 τ 限制，所以 `scrape_duration_seconds` 可以大於 τ 且成功。[^bl-p3-scrape-ctx][^p-scrape-report]
2. 逾時就是失敗：`up` = 0，並且當成「空的 scrape」寫入，上一次有的 series 全部拿到 stale 標記。[^bl-p3-failed-stale][^bl-p3-up-health]
3. 被標成 stale 的 series，之後的查詢**立刻**查不到（不是等 5 分鐘 lookback）。[^bl-p3-doc-staleness]所以沒有 range 的 alert（例如 `mysql_global_status_threads_running > 50`、帶 `for`）遇到一次逾時就回傳空；evaluation 成功但沒有這個 alert 時 pending 被刪除、`for` 重新計時。[^p-alert-delete][^exp-prom-silent] 兩者合起來的效果是推論，沒有單獨實驗。
4. 一個 target 的整個 scrape loop 是依序的：耗時超過 S 時多出來的 tick 被丟掉，只保留一個，下一次 scrape 在慢的那次結束後立刻開始，實際間隔約 `d`（讀碼；`⌈d/S⌉ × S` 是保守上限，見[契約](../contracts.md#scrape-within-interval)）。[^p-scrape-run][^vmag-ticker]

### Exporter 怎麼用這個期限

| Exporter | 讀 header 嗎 | 自己的期限 | 依據 |
|---|---|---|---|
| mysqld_exporter v0.21.0 | 讀 | τ − `--timeout-offset`（預設 0.25s） | [^bl-my-header][^bl-my-offset-default][^my-offset] |
| blackbox_exporter v0.29.1 | 讀 | min(module `timeout`, τ − `--timeout-offset`（預設 0.5s））；沒有 header 時 120s | [^bl-bb-header][^bl-bb-offset-flag][^bl-bb-default-120] |
| mongodb_exporter v0.54.0 | 讀 | τ − `--web.timeout-offset`（預設 1s） | [^mx-flag-timeout-offset][^mg-exp-a] |
| postgres_exporter v0.20.1 | **不讀** | `--collection-timeout` 預設 1m，用 `context.Background()` | [^bl-pg-coll-timeout-flag][^bl-pg-ctx-background][^pg-timeout] |
| redis_exporter v1.97.0 | **不讀** | `--connection-timeout` 預設 15s（比預設 τ 10s 長） | [^rx-flag-timeout][^vk-exp] |

讀 header 的 exporter 會自動滿足 [`exporter 自己的查詢期限 < scrape_timeout`](../contracts.md#exporter-deadline)；不讀的要手動把期限設得比 τ 短。上游的偏移量是 0.25–1 秒，可以當作 margin 的參考。

## 來源怎麼說 {#sources}

| 來源 | 類型 | `scrape_interval` | `evaluation_interval` | 視窗或其他 |
|---|---|---|---|---|
| Prometheus 預設值 | 官方（原始碼） | 1m | 1m | τ 10s [^bl-p3-default-interval][^bl-p3-default-eval] |
| Prometheus 範例設定檔 | 官方 | 15s | 15s | [^bl-p3-example-15s] |
| prometheus-operator CRD | 官方（上游專案） | 30s | 30s | τ > S 時拒絕資源 [^bl-op-scrape-interval][^bl-op-eval-interval][^bl-op-timeout-reject] |
| vmagent／vmalert | 官方（原始碼） | 1m | 1m | 多台 vmagent 時 `-dedup.minScrapeInterval` **必須**等於 `scrape_interval` [^vmag-default-interval][^vma-eval-interval][^bl-vm-doc-dedup-vmagent] |
| vmalert 文件 | 官方 | — | — | 防 flapping：視窗至少 2 × `scrape_interval`，`for` 遠大於 `scrape_interval` [^bl-vma-doc-flap-window] |
| Grafana data source | 官方 | 假設 15s（data source 的 Scrape interval 預設） | — | `$__rate_interval = max($__interval + scrape_interval, 4 × scrape_interval)` [^bl-graf-ds-scrape-default][^bl-graf-rate-interval] |
| Grafana Alerting | 官方 | — | 最小 10s（`min_interval`） | [^bl-graf-min-interval] |
| Grafana Cloud／Alloy | 官方（商業服務） | Alloy 預設 60s，建議 60s（1 DPM） | — | [^bl-grafana-cloud-dpm] |
| Prometheus storage 文件 | 官方 | 拉長 S 可降低 ingest，但減少 series 通常更有效 | — | 每個 sample 1–2 bytes [^bl-p3-doc-bytes-sample][^bl-p3-doc-reduce-series] |
| Robust Perception（Brian Brazil） | 社群 | 10–60s，全組織一個；慢 exporter 另一個，不超過 2m | — | `rate` 視窗至少 4 × `scrape_interval` [^bl-rp-scrape-interval][^bl-rp-rate-range] |
| Google SRE Workbook | 社群（Google 出版） | — | — | burn rate 視窗 1h／5m、6h／30m、3d／6h [^bl-sre-workbook] |

### 哪裡不一致，契約怎麼選

| 不一致的地方 | 說法 | 契約的結論 |
|---|---|---|
| 預設 S | 1m（Prometheus、VM、Alloy）、30s（operator）、15s（範例、Grafana 假設） | 沒有對錯。選定後 Grafana data source 的 Scrape interval 要改成同一個值，否則 `$__rate_interval` 可能只有 1 × S |
| `rate` 視窗 | 2 × S（vmalert 文件）、4 × S（Grafana、社群） | Prometheus 契約 `R ≥ (2+k)·I + d`。`k = 1`、`d ≤ S` 時最壞是 `4S`，和 4 × S 一致；2 × S 在 lab 中會間歇回傳空。[^exp-prom-range][^exp-jitter] |
| τ > S | Prometheus 與 operator 拒絕；vmagent 自動截成 S | 一律自己設 `τ < S`，不要依賴截斷 |
| S 的上限 | 社群 2m；契約 `I + d < 5m` | 社群的 2m 留了一次失敗的空間；契約只管 lookback。偶爾逾時的 job，用 `(1+k)·I + d < 5m` 檢查比較保守（推論） |
| E 與 S | 官方預設與範例都讓 `E = S`，但沒有文字要求 | `E < S` 時同一批資料被重算，且 pending 會依相位循環（[契約頁的圖](../contracts.md#range-window)）；`E > S` 只是慢。取 `E = S` |
| τ 的餘裕 | 沒有官方數字 | 用契約的 80% 預警線反推，見下一節 |

## 推導步驟 {#procedure}

每個 job 各做一次。數據要涵蓋尖峰時段（備份、批次、週末），至少 7 天。

| 步驟 | 量什麼／算什麼 | 查詢或公式 |
|---|---|---|
| 1. 量耗時分布 | 每個 job 的 p50、p99、max | `quantile_over_time(0.99, scrape_duration_seconds[7d])`、`max_over_time(scrape_duration_seconds[7d])`，`max by (job)` |
| 2. 量失敗 | 逾時次數與最長連續失敗 | `sum_over_time((up == bool 0)[7d:30s])`（30s 換成這個 job 的 S）算次數；連續幾次看 `up` 的圖；exporter 自己的耗時（例如 `redis_exporter_last_scrape_duration_seconds`） |
| 3. 檢查數據是否被截斷 | `d_max ≈ 現在的 τ` 代表那些 scrape 逾時了，真正的耗時不知道 | 先暫時放寬 τ（仍 < S）或看 exporter 自己的耗時 metric |
| 4. 選 S | 從偵測速度與成本決定（下一節）；硬性條件 `d_max ≤ S`，建議 `d_max ≤ 0.8 S` | [`d ≤ S`](../contracts.md#scrape-within-interval) |
| 5. 定 τ | `τ ≥ max(d_max / 0.9, d_p99 / 0.8)`，取整後必須 `< S` | 平常的 scrape 不碰 80% 預警線；觀察到的尖峰離 τ 至少 10%，否則下次量到的 d_max 分不出是真的耗時還是被逾時截斷 |
| 6. 定 exporter 期限 X | `X = τ − margin`，margin 取 1s 或 τ 的 10%（取大）；讀 header 的 exporter 用 offset 參數 | [`X < τ < S`](../contracts.md#exporter-deadline) |
| 7. 定 E | `E = S`（讀多個 job 的 group，取這些 S 的最大值或公倍數） | 再量 T：`max_over_time(prometheus_rule_group_last_duration_seconds[7d]) ≤ 0.8 E` |
| 8. 選 k | 步驟 2 的最長連續失敗，至少 1 | — |
| 9. 算最短視窗 | `R_min = (n+k)·I + max(0, d_max − O)`；`rate` 類 n = 2、`*_over_time` n = 1（[各函式](../contracts.md#range-functions)）。沒有 offset 是 `(2+k)·I + d_max`，`query_offset ≥ d_max` 是 `(2+k)·I` | [`range-window`](../contracts.md#range-window) |
| 10. 其他檢查 | lookback `I + d < 5m`；EndsAt `4 × max(resend-delay, E)` | [`lookback`](../contracts.md#lookback)、[`resend-endsat`](../contracts.md#resend-endsat) |

步驟 5 的 0.8 與契約、vmagent 文件用的預警線相同（`scrape_duration_seconds / scrape_timeout_seconds > 0.8`）。[^bl-vm-doc-timeout-ratio]這是本專案的選擇，不是官方建議的餘裕倍數。

### 例子：三種 S 的基準值

以下數字都是假設的**例子**，`k = 1`，resend-delay 用預設 1m。[^p-resenddelay]

| 例子 | S | 假設 d_p99／d_max | τ | X | E | 預警 T | R_min（無 offset） | R_min（`query_offset` = d_max） | lookback `I + d` | EndsAt |
|---|---|---|---|---|---|---|---|---|---|---|
| 例子 A | 15s | 3s／4s | 10s（預設） | 9s | 15s | 12s | 49s → `[1m]` | 45s → `[1m]` | 19s | 4m |
| 例子 B | 30s | 8s／12s | 15s | 13.5s | 30s | 24s | 102s → `[2m]` | 90s → `[2m]` | 42s | 4m |
| 例子 C | 60s | 25s／40s | 45s | 40.5s | 60s | 48s | 220s → `[4m]` | 180s → `[3m]` | 100s | 4m |
| 例子 D（慢 job 拆出） | 120s | 70s／90s | 100s | 90s | 120s | 96s | 450s → `[8m]` | 360s → `[6m]` | 210s | 8m |

- 例子 D 已接近 lookback 上限，也超過社群建議的 2m；`(1+k)·I + d = 330s` 超過 5m，沒有 range 的 rule 不適合放在這個 job 上。
- `for` 至少要涵蓋 R_min，否則 alert 在視窗還沒填滿前就判斷。

### 平常 < 5 秒、偶爾超過時

1. 先看「偶爾」有多長：例如 d_max = 8s，預設 τ = 10s 夠用，不必改。
2. d_max 超過 τ 但仍 < S：把這個 job 的 τ 調高（例如 S = 30s 時 τ = 15–20s），exporter 的期限跟著調。
3. d_max 接近或超過 S：不要讓整台 Prometheus 跟著變慢。選項依代價由小到大：

| 選項 | 效果 | 代價 |
|---|---|---|
| exporter 端縮短期限或拆 collector | 慢的部分自己失敗（`up` 仍是 1，只少那組 metric） | 那組 metric 在尖峰時缺值，要有 presence 規則 |
| exporter 端快取（例如 query-exporter 自己排程） | scrape 只讀快取，d 變小 | 資料年齡多一個排程週期 |
| 慢的 collector 拆成另一個 job，給較長的 S 和 τ | 其他 metric 保持原本的 S | 讀這個 job 的 rule 要用這個 job 的 I 算視窗（`fit_contracts.py --job-interval`） |
| 加 `query_offset ≥ d_max` | 視窗不必多涵蓋 d | 這個 group 每條 rule 都晚 O 判斷 [^p255-query-offset] |
| 拉長整台的 S | 最簡單 | 所有 rule 的視窗與偵測時間一起變長 |

## S 變小的代價

| 代價 | 怎麼算 | 依據 |
|---|---|---|
| Ingest 與磁碟 | sample／秒 = series ÷ S；磁碟 ≈ 保留秒數 × sample／秒 × 1–2 bytes | [^bl-p3-doc-bytes-sample] |
| Head 記憶體 | 主要跟 series 數走，S 減半不會讓記憶體減半；官方也說減少 series 通常比拉長 S 有效 | [^bl-p3-doc-reduce-series] |
| Exporter 對資料庫的負載 | 每次 scrape 執行一輪查詢。postgres_exporter 每次 scrape 開新連線；Valkey／Redis 的 INFO 在主執行緒上，排在慢指令後面 | [^pg-collect][^vk-exp] |
| τ 的空間 | τ < S，S 越小，可以容忍的慢 scrape 越短 | [^bl-p3-job-timeout-check] |
| Rule 成本 | `E = S` 時 evaluation 次數等比增加，要重量 T | [`group-within-interval`](../contracts.md#group-within-interval)、[^exp-prom-cpu] |
| VM 的去重 | `-dedup.minScrapeInterval` 大於某個 job 的 S 時，每個區間只留一個 sample，這個 job 的實際間隔變成 dedup 的值 | [^bl-vm-doc-dedup][^bl-vm-dedup-flag] |

## 對 fit_contracts.py 的輸入

| 參數 | 代什麼 | 不要代什麼 |
|---|---|---|
| `--scrape-interval` | 預設的 S（config 的 global） | — |
| `--job-interval job=S` | 拆出去或另設 S 的 job | — |
| `--scrape-duration`、`--job-duration` | 步驟 1 的 d_max（`collect.py` 的 `peaks.json: scrape_duration_peak`） | p99 或平均；`d_max ≈ τ` 時是被截斷的值，要在報告註明 |
| `--eval-interval` | E | — |
| `--group-duration` | T 的 7 天尖峰 | — |
| `--query-offset` | 全域 `rule_query_offset` | — |
| `--k` | 步驟 8 的最長連續失敗，至少 1 | — |

目前工具還不檢查 τ 與 exporter 的期限（`exporter-deadline`），要另外對照[Exporter 怎麼用這個期限](#defaults)。

[^bl-rp-scrape-interval]: 社群：[Robust Perception：Keep it simple, scrape_interval id（Brian Brazil，2017-12-25）](https://www.robustperception.io/keep-it-simple-scrape_interval-id/)
[^bl-rp-rate-range]: 社群：[Robust Perception：What range should I use with rate()?（Brian Brazil，2019-08-12）](https://www.robustperception.io/what-range-should-i-use-with-rate/)
[^bl-sre-workbook]: 社群：[Google SRE Workbook：Alerting on SLOs，表 5-8](https://sre.google/workbook/alerting-on-slos/)
[^bl-grafana-cloud-dpm]: 文件：[Grafana Cloud：Adjust data points per minute](https://grafana.com/docs/grafana-cloud/cost-management-and-billing/analyze-costs/reduce-costs/metrics-costs/adjust-data-points-per-minute/)
