# 監控與監控 monitor：規則放在哪一台

monitor of monitor 至少要有兩台互不依賴的 ruler：一台做**實際監控**（業務、資料庫、主機），一台做**觀察者**（監控前一台和它的 Alertmanager）。兩台的規則不一樣，放錯台就等於沒有：例如把 `PrometheusDown` 放在受測的 Prometheus 上，它停了這條規則也跟著停。

<!-- figure: mom-placement -->

**圖**　兩個故障域。受測那台放資料庫規則與 canary；觀察者放 meta 規則，透過 `/metrics` 與 `/federate` 看受測那台，通知走自己的路徑。兩邊的 Watchdog 都送到外部 dead man's switch，沒收到就叫人。
{: .at-caption }

## 每一台要裝哪些規則 {#which-rules}

| 角色 | 規則檔（`rules/` 底下） | 為什麼放這裡 |
|---|---|---|
| 受測 Prometheus | `targets/<db>.yml`（用到的資料庫各一份）、`host/meta.yml`（它抓的主機） | 資料只在這台：誰抓 exporter，誰評估 |
| 受測 Prometheus | `prometheus/targets.yml`（每個關鍵 job 的 `TargetJobDown`、`TargetJobMissing`） | `up` 不在 Prometheus 自己的 `/metrics`，只有抓 target 的那台有[^p-scrape-report]；要給觀察者看就用 `/federate` |
| 受測 Prometheus | `prometheus/canary.yml`（`Watchdog`、`DataHeartbeat*`、`KnownTrueCanary`） | 正常時一直 firing，證明「評估 → Alertmanager → 接收端」這條路活著；送到外部 dead man's switch |
| 受測 vmalert（業務） | `targets/<db>.yml`、`host/meta.yml` | 同上 |
| 獨立的 vmalert（canary） | `victoriametrics/canary.yml`（`Watchdog`、`MetricsFresh`）；用 VictoriaLogs 時加 `canary-logs.yml` | 和業務 vmalert 分開，而且不要設 `-rule.resendDelay`：設了以後查詢中斷時 Watchdog 也不會斷[^exp-vm-resend] |
| 觀察者 | `prometheus/meta.yml` | 受測 Prometheus 停掉、rule 卡住、通知送不出去，它自己評估不到 |
| 觀察者 | `alertmanager/meta.yml` | Alertmanager 壞掉時，經過它的告警送不出來；這些規則的通知不能走被監控的 Alertmanager |
| 觀察者（有用才放） | `victoriametrics/meta.yml`、`thanos/meta.yml`、`grafana/meta.yml` | 每個監控元件的 meta 規則都放在它以外的地方評估 |
| 外部 dead man's switch | 不放規則，只設「多久沒收到 Watchdog 就叫人」 | 等待時間太短會誤報：ruler 停機後 Alertmanager 仍會重送 Watchdog 到 EndsAt[^exp-prom-heartbeat]；算法見[等待時間契約](../contracts.md#deadman-wait) |

## 觀察者要抓什麼 {#observer-scrape}

| 抓什麼 | 用哪條路 | 給哪些規則 |
|---|---|---|
| 受測 Prometheus 的 `/metrics` | 一般 scrape job | `prometheus/meta.yml` 的大部分規則（rule group、notifier、TSDB） |
| `up`、`scrape_duration_seconds`、`ALERTS`、`ALERTS_FOR_STATE` | 受測 Prometheus 的 `/federate` | 這些 series 不在 `/metrics`[^p-scrape-report]；觀察者要靠它們看「受測那台抓得到 target 嗎」「它的 Watchdog 還在嗎」 |
| 受測 Alertmanager 的 `/metrics` | 一般 scrape job | `alertmanager/meta.yml` |
| vmagent、VictoriaMetrics、vmalert 的 `/metrics` | 一般 scrape job | `victoriametrics/meta.yml` |

只看 HTTP 200 的存活探測不夠：`/-/healthy`、`/-/ready` 在 8 種故障裡只明確抓到 2 種[^exp-prom-blackbox]。外部探測要驗證回應內容，細節見[外部觀察者](../components/observer.md#usage)。

## 常見的放錯 {#mistakes}

| 放錯 | 結果 | 改法 |
|---|---|---|
| `PrometheusDown`、`AlertmanagerScrapeDown` 放在受測那台 | 它停了，這些規則也停了 | 移到觀察者 |
| 觀察者的告警也送到受測的 Alertmanager | 受測 Alertmanager 壞掉時，meta 告警一起送不出去 | 觀察者用自己的 Alertmanager 或直接 webhook |
| 觀察者和受測那台在同一個節點、叢集或儲存 | 一起掛，沒有人知道 | 放到不同故障域（[外部觀察者](../components/observer.md#failure-domain)） |
| 觀察者只 federate 了 `up` 和 `ALERTS`，上面卻沒有規則用它們 | 看起來有觀察者，其實什麼都不會響 | 至少加「抓不到受測那台」「`ALERTS` 裡看不到它的 Watchdog」兩條 |
| 觀察者自己沒有 Watchdog | 觀察者停了也沒人知道 | 它的 Watchdog 也送到外部 dead man's switch |
| Watchdog 沒有專用 route，繼承主路由的 `repeat_interval` | dead man's switch 要等幾小時才會響 | 給 Watchdog 專用 route，見[從觸發到通知](alert-delivery.md#pitfalls) |
| 兩台一模一樣的 Prometheus 互當備援，就以為有 monitor of monitor | 兩台用同一份設定、同一台 Alertmanager，設定寫錯或 Alertmanager 壞掉時一起失效（推論） | 備援解決的是「單台停機」；monitor of monitor 仍要一台設定和通知路徑都獨立的觀察者 |

## 和工具的對應 {#tools}

在 `env/<name>.yaml` 寫出這張圖：`components` 列受測的 ruler 和 Alertmanager（canary 用的 vmalert 標 `role: watchdog`），`observers` 列觀察者的 `failure_domain`、`notify_via`、`scrapes`、`federates_from`，`deadman` 寫 dead man's switch 的等待時間。格式見 [env.example.yaml](https://github.com/HahaSula/alert-trust/blob/main/env/env.example.yaml)。`tools/check_coverage.py` 會依這份檔案檢查每條 meta 規則是不是放在對的那台、觀察者有沒有抓到每個元件。
