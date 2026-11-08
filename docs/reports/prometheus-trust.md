# Prometheus Rule 信任邊際

接近極限時，怎麼監控 Prometheus 才能信任 rule。涵蓋鏈路盲點、pending 抖動、CPU／記憶體／IOPS／磁碟邊際、視窗矩陣、升級與跨版本比較。

!!! info "範圍與限制"
    版本基準 2.51.2，並比對 2.55.1、3.13.5、3.15.1。實驗在單機 Docker 上跑，受測規模 30 萬 series、每秒約 2 萬 sample，另在 minikube 上用 kube-prometheus-stack 57.2 驗證。**實驗證明的是機制和訊號出現的順序；秒數與門檻來自這個小規模環境，不能直接套用。**每項結論都標了證據等級。

<span class="at-chip at-ev-tested">實測</span> 在實驗環境量到　<span class="at-chip at-ev-code">讀碼</span> 由原始碼確認　<span class="at-chip at-ev-inferred">推算</span> 由公式算出　<span class="at-chip at-ev-upstream">社群</span> 上游 issue 或公開文章

## 結論

**Rule 回報正常，不代表 alert 正確。**實測在 2.51.2 上，一條永遠成立的 alert 跑 25 分鐘都沒有 firing，而 `rule_evaluation_failures_total` 和 `iterations_missed_total` 都是 0，`up` 也是 1。[^exp-prom-silent]

**Rule 能不能信，取決於一組時間不等式是否成立。**兩邊的比值就是邊際（見[信任契約](../contracts.md)）。最常見的三種失效是：scrape 耗時超過 interval、rule group 耗時超過 interval、rule 沒有查詢 offset。任何一條不成立，rule 都照常執行、不報錯。

**監控要分三層：**外部 dead man's switch 確認鏈路活著，依賴真實資料且帶 `for` 的 canary 確認判斷正確，邊際指標提早預警。只有 `Watchdog` 不夠，它在 target 停機、查詢飽和、CPU 受限、crash loop，甚至磁碟滿到資料寫不進去時都幾乎不受影響。[^exp-prom-heartbeat]

**接近極限時，rule 放在哪裡比資源多寡更關鍵。**CPU 降到 0.15 核時，獨立小 group 裡的 alert 偵測延遲不變，同樣的 alert 放在重 group 卻變慢近一倍，還會漏掉事件。關鍵 alert 應該和重查詢分開。[^exp-prom-cpu]

**升級無法單獨解決這個問題。**沒加 offset 的 rule 在 2.51.2、2.55.1、3.13.5、3.15.1 都同樣無法 firing。2.51 不支援 `query_offset`，但在表達式內加 `offset` 實測有效，不必等升級。[^exp-prom-silent]

## 實驗清單

| 實驗 | 版本 | 結果 |
|---|---|---|
| 鏈路盲點（target、Alertmanager、Prometheus 停機） | 2.51.2 | [^exp-prom-heartbeat] |
| Pending 抖動（慢 scrape） | 2.51.2、2.55.1、3.13.5、3.15.1 | [^exp-prom-silent] |
| CPU 限制與 `auto-gomaxprocs` | 2.51.2 | [^exp-prom-cpu] |
| 查詢飽和 | 2.51.2 | [^exp-prom-query] |
| 記憶體壓縮與 OOM crash loop | 2.51.2 | [^exp-prom-mem] |
| IOPS 限制（含 compaction） | 2.51.2 | [^exp-prom-storage] |
| 真實磁碟滿 | 2.51.2、2.55.1、3.13.5、3.15.1 | [^exp-prom-storage] |
| 磁碟被 compaction 用完 | 2.51.2、3.13.5 | [^exp-prom-storage] |
| CPU 邊際掃描與實際偵測延遲 | 2.51.2 | [^exp-prom-cpu] |
| 記憶體邊際掃描與非正常重啟 | 2.51.2 | [^exp-prom-mem] |
| Rule 視窗矩陣（24 種組合） | 2.51.2、3.13.5 | [^exp-prom-range] |
| Rule 當下資料的探針 | 2.51.2 | [^exp-prom-probe] |
| Alert 恢復時間 | 2.51.2、2.55.1、3.13.5 | [^exp-prom-upgrade] |
| 跨版本回退與資料比對 | 2.51.2、2.55.1、3.13.5 | [^exp-prom-upgrade] |
| 相同負載的資源比較 | 2.51.2、2.55.1、3.13.5 | [^exp-prom-upgrade] |
| Blackbox 探測在各種故障下的結果 | 2.51.2 | [^exp-prom-blackbox] |
| K8s 驗證（kube-prometheus-stack 57.2） | 2.51.2 | [^exp-prom-k8s] |

## 1. 架構：一條 alert 經過哪些元件

Scrape、rule、查詢、通知和 TSDB 都在同一個 Prometheus 行程裡，共用 CPU、記憶體和磁碟。所以一個資源出問題，負責偵測的元件會一起受影響，這也是監控不能只放在自己身上的原因。

<!-- figure: prometheus-process -->

**圖**　資料從上方的 scrape 進入，rule 讀取後把結果寫回同一條路徑，firing 的 alert 再經 notifier 送出。紅框是實驗中最常讓 rule 失準、又不會報錯的兩個單序環節；綠色虛線是不依賴受測行程的監控路徑。編號對應下表。[^p-scrape-run][^p-wal-log][^p-group-eval][^p-engine-queue][^p-notifier-wait][^p-db-run]
{: .at-caption }

### 每一層怎麼執行：單序還是並行

Prometheus 大部分工作都是並行的，但每條路徑上都有單序的環節，**瓶頸和連鎖問題都出在這些環節**。下表依資料流排列，編號對應上圖。<span class="at-chip at-ev-code">讀碼</span>（2.51.2、Alertmanager 0.27.0）

| # | 層級 | 同一個對象內 | 不同對象之間 | 併行上限 | 卡住時影響誰 |
|---|---|---|---|---|---|
| – | **服務發現** | 每個 provider 一條 goroutine | 並行 | 無 | Target 清單更新變慢，最快 5 秒同步一次 |
| 1 | **Scrape**（單位：target） | **單序**：抓取、parse、relabel、append、commit 依序跑。沒跑完下一輪不會開始，tick 被丟掉[^p-scrape-run][^p-scrape-report] | 並行：每個 target 一條 goroutine，用 offset 錯開[^p-scrape-sync] | 沒有上限，受 CPU 限制 | 只影響該 target。除非搶 CPU、記憶體或 WAL |
| 2 | **WAL 寫入**（全域一份） | **單序**：所有 scrape 和 rule 的 commit 都經過同一把鎖（`WL.Log`）。切換 segment 的 fsync 由另一條 goroutine 非同步執行[^p-wal-log] | – | 1 | **全部寫入**。平常只寫 page cache 所以很快 |
| 8 | **Head**（單位：series） | **單序**：每條 series 有自己的鎖 | 並行：series 表分 16384 個 stripe。Chunk 落盤是單一背景佇列[^p-head-stripe] | 依 stripe | 只影響碰到相同 stripe 或 series 的操作 |
| 3 | **Rule group**（單位：group） | **單序**：rule 依定義順序一條接一條跑；相鄰兩輪不重疊[^p-group-eval] | 並行：每個 group 一條 goroutine[^p-group-run] | Group 數沒有上限；查詢受第 4 層限制 | **同 group 的所有 alert**。不影響其他 group，除非搶查詢 slot |
| 4 | **查詢引擎**（全域一份） | **單序**：一個查詢的求值在同一條 goroutine；合併多個 querier 時才另開 | 有上限並行：rule 和 API 搶同一組 slot，解析與執行各搶一次[^p-engine-queue] | `--query.max-concurrency`，預設 20[^p-maxconc] | **Rule 和 API 都排隊** |
| 5 | **Alert 狀態**（單位：rule） | **單序**：在 group 的 goroutine 裡更新，查詢結果決定 `for` | 跟隨第 3 層 | 跟隨第 3 層 | Group 慢時狀態更新跟著延後 |
| 6 | **Notifier**（全域一份） | **單序**：一條 queue、一條送出迴圈，每批最多 64 筆。同一批會同時送給所有 Alertmanager，**等全部回應才送下一批**[^p-notifier-batch][^p-notifier-sendall][^p-notifier-wait] | – | 同時 1 批 | **全部通知**。一台 Alertmanager 慢就拖累所有 alert；逾時預設 10 秒 |
| – | **Alertmanager**（單位：alert group） | **單序**：每個 alert group 自己依 `group_interval` 依序送出[^am-aggr-run] | 並行：每個 alert group 一條 goroutine，同 group 的多個 receiver 也並行送出，失敗會重試[^am-fanout][^am-retry] | 沒有上限 | 接收端慢只影響用到它的 group |
| 7 | **TSDB 維護**（全域一份） | **單序**：reload、retention、head compaction、block compaction、WAL 截斷都在同一條 goroutine（`DB.run`）[^p-db-run] | – | 1 | **全部維護工作**。Compaction 慢或失敗，retention 跟著延後或停止 |

更細的說明見 [Prometheus 架構](../architecture/prometheus.md)。

### 升級會改變這張表嗎

上表以 2.51.2 為準。**2.55.1 的單序結構完全相同；3.x 只改了 notifier，以及選用的 rule 並行。**Scrape、WAL、查詢和 TSDB 維護到 3.15.1 都沒有改變執行模型，所以這些瓶頸升級也不會消失。<span class="at-chip at-ev-code">讀碼</span>

| 層級 | 2.51.2、2.55.1 | 3.5 LTS（已結束支援） | 3.13 LTS、3.15 |
|---|---|---|---|
| **Scrape** | 相同：每個 target 內單序，target 之間並行 | 相同 | 相同 |
| **WAL 寫入** | 相同：全域單序，segment 切換的 fsync 非同步 | 相同 | 相同 |
| **Head** | 相同：16384 個 stripe，每條 series 一把鎖 | 相同 | 相同 |
| **查詢引擎** | 相同：rule 和 API 共用 slot，預設 20 | 相同 | 相同 |
| **Rule group** | 預設單序。`concurrent-rule-eval` 可讓獨立 rule 併行，但無法判斷依賴時順序可能失效 | 預設仍是單序。開 flag 時，3.1 起無法判斷依賴會退回依序，3.2 起分三段：無上游併行 → 上下游都有依序 → 無下游併行 | 同左 |
| **Notifier** | 一條 queue，每批固定 64 筆，等所有 Alertmanager 回應 | 結構相同。3.4 起批次預設 256 筆，可用 `--alertmanager.notification-batch-size` 調整 | **每台各一條 queue（3.10 起）**。一台慢不再拖累其他台[^p3-notifier-queue] |
| **TSDB 維護** | 單一 goroutine。2.55 起可選 `delayed-compaction`，用於錯開多台同時 compaction | 單一 goroutine | 單一 goroutine。3.9 起 reload 間隔可設定；3.10 起有可選的 stale series compaction。**reload timer 被重置的問題仍在**，實測 3.13.5 同樣會歸零[^exp-prom-storage] |
| **Go 執行緒** | 預設不對齊 limit，要開 feature flag | 3.0 起預設對齊 `GOMAXPROCS` 和 `GOMEMLIMIT` | 同左 |

比對 v2.51.2、v2.55.1、v3.0.0、v3.5.0、v3.13.5、v3.15.1 的原始碼；各功能的起始版本取自 repo tag。Reload ticker 修正截至 2026-11 只在 main，還沒有正式版。Alertmanager 本身的版本不在此表，見 [Alertmanager 監控指南](alertmanager.md)。

**對選版的意義：**只想解決 notifier 互相拖累，要升到 3.10 以上，3.5 不夠；3.13 LTS 是同時滿足 LTS 與 3.10 以上的版本。2.55 這一步不會改變任何執行模型，適合當中繼站，風險也較低。

### 單序環節造成過的實際問題

| 單序環節 | 實驗看到的結果 | 能怎麼處理 |
|---|---|---|
| 單一 target 內 | Scrape 超過 interval 時 tick 被丟掉，alert 無法 firing。其他 target 不受影響 <span class="at-chip at-ev-tested">實測</span>[^exp-prom-silent] | 降低 series、拆 target；加 CPU 對單一 target 的 parse 幫助有限 |
| 單一 rule group 內 | 同一條 alert，放在重 group 延遲變近兩倍還會漏事件；獨立小 group 不受影響 <span class="at-chip at-ev-tested">實測</span>[^exp-prom-cpu] | 拆 group，關鍵 alert 獨立。`concurrent-rule-eval` 在 2.x 有依賴判斷問題，不建議優先使用 |
| 查詢 slot | 重查詢佔滿後，rule group 從幾秒變 40 秒，scrape 不受影響 <span class="at-chip at-ev-tested">實測</span>[^exp-prom-query] | 限制 Grafana 查詢；確認 queue time 真的偏高才加大 `max-concurrency` |
| WAL 全域寫入 | 10 IOPS 時 scrape 和 rule 不受影響，因為只寫 page cache；磁碟寫滿時 scrape 和 rule 同時失敗 <span class="at-chip at-ev-tested">實測</span>[^exp-prom-storage] | 預留磁碟空間，監控寫入失敗 |
| TSDB 維護 | Compaction 用完空間時 reload 歸零；長查詢卡住 Compact 時 reload 也停止 <span class="at-chip at-ev-tested">實測</span>[^exp-prom-storage] | 監控 `reloads_total` 與 `compactions_failed_total` |
| Notifier 送出迴圈 | 一台 Alertmanager 慢會拖累全部：由原始碼確認，本次沒有實測慢 Alertmanager <span class="at-chip at-ev-code">讀碼</span>[^p-notifier-wait] | 監控 `notifications_latency_seconds`。3.10 起每台各一條 queue，互不影響 |

**怎麼用這張表：**scrape 尖峰偏高時，先看是否集中在少數 target，因為單序環節不會因為加 CPU 變快。慢的 rule group 會拖累同 group 的所有 alert，把關鍵 alert 移出來最有效。Notifier 和 TSDB 維護都只有一條路徑，要各自監控。<span class="at-chip at-ev-inferred">推算</span>

### 每個元件什麼時候會出問題

| # | 元件與執行方式 | 什麼時候出問題 | 出問題時會發生什麼 | 看哪個訊號 |
|---|---|---|---|---|
| 1 | **Scrape loop**：每個 target 一條 goroutine；timeout 只管 HTTP | 耗時超過 interval | Tick 被丟掉、間隔拉長；sample 帶開始時間，但結束才查得到 | `scrape_duration_seconds`、`target_interval_length_seconds` |
| 2 | **WAL**：所有 scrape 與 rule 的 commit 都經過同一把鎖 | 磁碟滿、寫入錯誤；非正常結束 | Scrape 與 rule 結果都寫不進去；跨頁紀錄可能損毀，WAL 無法截斷 | `wal_writes_failed_total`、`checkpoint_creations_failed_total` |
| 3 | **Rule group**：每個 group 一條 goroutine，內部依序跑；不同 group 並行 | Group 耗時超過 interval | 同 group 的 alert 全部變慢，跳過 evaluation；一條慢 rule 會拖累其他 rule[^p-group-missed] | `rule_group_last_duration_seconds`、`iterations_missed_total` |
| 4 | **查詢引擎**：rule 與 API 共用 slot，預設 20 | Grafana 或 API 的重查詢佔滿 slot | Rule 排隊變慢；超過 `query.timeout` 就失敗 | `engine_queries` ÷ `queries_concurrent_max`、`queue_time` |
| 5 | **Alert 狀態**：存在記憶體，定期寫入 `ALERTS_FOR_STATE` | 查詢回傳空結果 | Pending 被刪除，`for` 重算；查詢失敗反而不影響狀態[^p-alert-delete][^p-eval-fail-return] | `changes(ALERTS_FOR_STATE[1h])` |
| 6 | **Notifier**：2.51 只有一條 queue，每批 64 筆，等所有 Alertmanager 回應 | Alertmanager 慢或停機；續送中斷超過 `EndsAt` | 一台慢會拖累全部；queue 滿時丟最舊的；Alertmanager 自動 resolve[^p-alert-validuntil] | `notifications_errors_total`、`dropped_total`、`queue_length` |
| 7 | **TSDB 維護**：compaction、reload、retention 都在同一條 goroutine | Head compaction 持續失敗；長查詢卡住；block 損毀 | Reload 停止，retention 不執行；WAL 無法截斷時磁碟持續上升 | `reloads_total`、`compactions_failed_total` |
| 8 | **Head 與 replay**：最新資料在記憶體；重啟要 replay WAL | 記憶體接近 limit；重啟 | Replay 期間 rule 不執行；可能 OOM crash loop | `data_replay_duration_seconds`、`process_start_time_seconds` |

### 為什麼問題會連鎖

- **查詢負載會拖慢 alert。**Grafana 的查詢和 rule 搶同一組 slot 與 CPU（4 → 3）。實測重查詢讓 rule group 從幾秒變成 40 秒，但 scrape 不受影響。<span class="at-chip at-ev-tested">實測</span>[^exp-prom-query]
- **Scrape 變慢會讓 rule 判斷錯誤，但不會報錯。**Rule 照常執行，只是看到的資料少了（1 → 5）。<span class="at-chip at-ev-tested">實測</span>[^exp-prom-silent]
- **磁碟問題會一路往上影響。**WAL 寫入失敗讓資料和 rule 結果都寫不進去（2 → 3、5）。WAL 無法截斷又會讓磁碟持續上升（2 → 7）。<span class="at-chip at-ev-tested">實測</span>[^exp-prom-storage]，完整連鎖仍在驗證。
- **重啟會暫時關掉偵測。**Replay 結束前 rule 不執行，之後還要再經過 `for`（8 → 3、5），`for-outage-tolerance` 預設 1 小時，`for-grace-period` 預設 10 分鐘。<span class="at-chip at-ev-code">讀碼</span>[^p-forgrace]

## 2. Rule 的信任契約

邊際小於 1 代表契約已不成立。公式與符號見[信任契約](../contracts.md)，可以用[契約計算器](../tools/calculator.md)代入自己的數字。

| 契約 | 違反時會怎樣 | 證據 |
|---|---|---|
| [scrape 耗時 d ≤ interval S](../contracts.md#scrape-within-interval) | tick 被丟掉，實際間隔 I 拉長 | <span class="at-chip at-ev-code">讀碼</span>[^p-scrape-run] |
| rule 只查已 commit 的資料：offset O ≥ d | rule 看到的資料少於事後查詢 | <span class="at-chip at-ev-tested">實測</span>[^exp-prom-silent] |
| [group 耗時 T ≤ evaluation interval E](../contracts.md#group-within-interval) | 跳過 evaluation，偵測變慢 | <span class="at-chip at-ev-tested">實測</span>[^exp-prom-cpu] |
| [視窗內至少 2 個可見 sample](../contracts.md#range-window)：沒有 offset 時 R ≥ 2I + d；O ≥ d 時 R ≥ 2I | `rate` 回傳空，`for` 計時歸零 | <span class="at-chip at-ev-tested">實測</span>[^exp-prom-range] |
| [Lookback](../contracts.md#lookback)：I + d < 5m | 不用 range 的 selector（如 `up == 0`）也會回傳空 | <span class="at-chip at-ev-upstream">社群</span> <span class="at-chip at-ev-inferred">推算</span>[^p-lookback-default] |
| [通知續送](../contracts.md#resend-endsat)：T 遠小於 4 × max(resend, E) | Alertmanager 自動 resolve，通知 firing／resolved 交替 | <span class="at-chip at-ev-code">讀碼</span>[^p-alert-validuntil] |

**例子**（數字是假設，用實驗的延遲條件）：`scrape_interval` 30s、`evaluation_interval` 30s、尖峰 d = 100s、沒有 offset、R = 5m。

- d ≤ S：30 ÷ 100 = **0.3**，實際間隔 I 約 ⌈100/30⌉ × 30 = 120s。
- O ≥ d：0 ÷ 100 = **0**。
- R ≥ 2I + d：300 ÷ (240 + 100) = **0.88**，`rate[5m]` 會間歇回傳空。
- Lookback：300 ÷ (120 + 100) = **1.36**，還成立，但餘裕不大。

### 這些數字代表什麼

- **Scrape 的時間大多花在 Prometheus 內部。**`scrape_timeout` 不能大於 interval，而且只管 HTTP 階段。`scrape_duration_seconds` 遠超過 timeout 又成功時，多出來的時間花在 parse、relabel 和 append。`scrape_duration_seconds` 還不含 commit，實際可見時間會更晚。<span class="at-chip at-ev-code">讀碼</span>[^p-cfg-timeout-check][^p-scrape-report]
- **單純加 CPU 可能幫助不大。**每個 target 只有一條 scrape goroutine，parse 和 append 依序執行，所以單一 target 的耗時受單核速度限制。加 CPU 只對競爭造成的延遲有效，例如 throttling 或 GC。要先分辨慢的是少數大 target，還是所有 target 一起變慢。<span class="at-chip at-ev-code">讀碼</span> <span class="at-chip at-ev-inferred">推算</span>
- **上面的例子就是實驗重現的情況。**實驗設定 2 分鐘間隔、100 秒延遲、`rate[5m]`、`for: 3m`，結果就是永遠不會 firing。<span class="at-chip at-ev-tested">實測</span>[^exp-prom-silent]
- **尖峰是否持續，會決定影響多大。**慢 scrape 只是短暫尖峰時，`for` 較短的 alert 仍可能 firing；`for` 越長，只要期間撞到一次尖峰就可能重來。

### 偵測延遲上限

加上足夠 offset 後，事件發生到通知送出的上限約為 `I + O + E' + T + F`，其中 E' 是實際 evaluation 間隔。<span class="at-chip at-ev-inferred">推算</span>

例如 I = 120s、O = 2m、E' = 30s、T = 10s 時，上限約 280 秒，**也就是 4–5 分鐘加上 `for`**。如果這比需要的反應時間長，就必須先把 d 和 T 降下來，調整 rule 參數無法再縮短。

## 3. 只靠 heartbeat 會漏掉什麼

每種故障都由外部 meta Prometheus 和通知接收端同時量測。下表的「最大間隔」是接收端兩次通知之間的最長時間，正常約 35 秒。<span class="at-chip at-ev-tested">實測</span>[^exp-prom-heartbeat]

| 故障 | `Watchdog`（`vector(1)`） | 資料 heartbeat（單一 target） | 帶 `for` 的 canary | 第一個抓到的訊號 |
|---|---|---|---|---|
| Canary 的 target 停機 | 35s，沒發現 | 240s，有發現 | 213s，有發現 | `up == 0`，70s |
| 其他大型 target 停機 | 35s，沒發現 | 35s，沒發現 | 35s，沒發現 | `up == 0`，80s |
| 慢 scrape，alert 無法 firing | 沒發現 | 沒發現 | 沒發現 | `changes(ALERTS_FOR_STATE)`，約 14 分鐘 |
| 查詢飽和，rule 跳過 evaluation | 35s，沒發現 | 35s，沒發現 | 35s，沒發現 | `iterations_missed`、`queries / concurrent_max`，65s |
| CPU 降到 0.3 核，rule 跳過 evaluation | 30s，沒發現 | 35s，沒發現 | 35s，沒發現 | throttling、`iterations_missed`，80s |
| OOM crash loop（8 分鐘內重啟 11 次） | 35s，沒發現 | 35s，沒發現 | 284s，有發現 | `process_start_time_seconds` 變動，10s |
| 磁碟滿 10 分鐘（資料和 rule 結果都寫不進去） | 35s，沒發現 | 558s，有發現 | 583s，有發現 | `wal_writes_failed`、`rule_evaluation_failures`，15s |
| Alertmanager 停機 | 240s，有發現 | 240s，有發現 | 240s，有發現 | `notifications_dropped`，15s |
| Prometheus 停機 3 分鐘 | 35s，3 分鐘內沒發現 | 35s，3 分鐘內沒發現 | 35s，3 分鐘內沒發現 | 外部 `up == 0`，35s |

「慢 scrape」一列的 heartbeat 與 canary 都掛在正常 target 上，所以不會受影響。時間是這套實驗環境的量測值。

### 三個要避開的誤區

- **Prometheus 死掉後，外部還會持續收到通知。**Alertmanager 會繼續 repeat，直到 `EndsAt` 到期，也就是 `4 × max(resend-delay, E)`；預設 resend-delay 1m、E 不超過 1m 時是 4 分鐘。所以 Prometheus 死後，dead man's switch 最快也要 4 分鐘加上它的等待時間才會響，更快的偵測要靠外部 `up`。<span class="at-chip at-ev-tested">實測</span>[^exp-prom-heartbeat][^p-alert-validuntil]
- **Crash loop 時 heartbeat 會騙人。**重啟間隔夠短時，`vector(1)` 和不帶 `for` 的規則每次啟動都馬上恢復。要帶 `for` 才會斷，所以 canary 的 `for` 不能省。<span class="at-chip at-ev-tested">實測</span>[^exp-prom-heartbeat]
- **每條關鍵 job 要有自己的 heartbeat。**單一 target 的 heartbeat 只能證明那一個 target 的路徑健康，其他 target 停機時它依然正常。<span class="at-chip at-ev-tested">實測</span>[^exp-prom-heartbeat]

## 4. 接近極限時，訊號依什麼順序出現

這一節回答「哪個訊號能提早預警」。實驗是階段式加壓，時間是加壓後第一次觸發告警的時間，已包含告警本身的 `for`。

| 壓力來源 | 依序出現的訊號 | 全程沒有異常 |
|---|---|---|
| CPU 限制 0.3 核（1 核時正常）[^exp-prom-cpu] | throttling 與 `iterations_missed` 80s → `last_duration > 0.8 × interval` 90s → group 超過 3 個 interval 未完成 220s → 降到 0.1 核後 scrape 才接近 timeout 65s | `up`、`rule_evaluation_failures_total`、heartbeat |
| 查詢飽和（`max-concurrency=2`、6 條重查詢）[^exp-prom-query] | `queries / concurrent_max` 與 `iterations_missed` 65s → `last_duration` 90s → `queue_time` 105s → group 未完成 285s | `up`、scrape、`rule_evaluation_failures_total`、heartbeat |
| 寫入限制 50 → 10 IOPS（探測確認：寫 16MB 從 0 秒變 7 秒、98 秒）[^exp-prom-storage] | 50 IOPS：WAL fsync 變慢 645s → 10 IOPS：最舊資料超過預期 110s → Head 未落成 block 615s；reload 頻率約減半 | `up`、scrape、rule 耗時、`iterations_missed`、失敗計數、heartbeat |
| 磁碟完全寫滿（4 個版本）[^exp-prom-storage] | `wal_writes_failed` 與 rule 失敗 10–15s → 帶 `for` 的 canary 中斷 145–175s → 資料 heartbeat 中斷 165–170s | `Watchdog`、`reloads_total`、`compactions_failed_total` |
| 可用空間快被 compaction 用完[^exp-prom-storage] | `compactions_failed_total` 145s → Head 堆積與 WAL 寫入失敗 360s → canary 中斷 525s → reload 停止 565s | `Watchdog`；failed 指標在 WAL 失敗前都沒有變化 |
| 記憶體壓到 working set 以下[^exp-prom-mem] | 重啟 10s → WAL 修復 205s → 通知中斷 325s | `iterations_missed`、`Watchdog`、資料 heartbeat |

- **Rule 比 scrape 更早受影響。**CPU 降到 0.3 核時，rule group 耗時已是 interval 的 2 倍以上，scrape 間隔卻還維持 15 秒。只看 `up` 或 scrape 會太晚發現。<span class="at-chip at-ev-tested">實測</span>[^exp-prom-cpu]
- **沒有任何一個「失敗」指標會動。**Rule 變慢、查詢排隊時，失敗計數都是 0，rule 的 `health` 也是 `ok`。要看比值，不能只看失敗數。<span class="at-chip at-ev-tested">實測</span>[^exp-prom-cpu][^exp-prom-query]

### GOMAXPROCS 在 2.51 要自己處理

2.51 預設用節點核心數當 `GOMAXPROCS`，不會對齊 CPU limit。在 1 核限制下的對照（30 萬 series，每秒約 2 萬 sample）：<span class="at-chip at-ev-tested">實測</span>[^exp-prom-cpu]

| 設定 | GOMAXPROCS | Throttling 比例 | 大 target scrape 平均 | 告警 |
|---|---|---|---|---|
| 預設 | 8 | 37% | 2.8s | 170s 後觸發 throttling 告警 |
| `--enable-feature=auto-gomaxprocs` | 1 | 3% | 0.7s | 無 |

開之前先看 `/api/v1/status/runtimeinfo` 的 `GOMAXPROCS` 和 throttling 比例。這個對照的規模小，效果只能當方向參考。

### CPU 邊際：偵測延遲從哪裡開始惡化

用定時翻轉的 canary 直接量「事件發生到收到通知」的時間（`for: 30s`、evaluation 15s），同一條 alert 放在兩個位置比較。<span class="at-chip at-ev-tested">實測</span>[^exp-prom-cpu]

| CPU 限制 | Throttling | 重 group 耗時 ÷ interval | 每 5 分鐘跳過 | 大 target scrape | 獨立小 group 延遲 | 放在重 group 延遲 |
|---|---|---|---|---|---|---|
| 1.0 | 35% | 0.47 | 0 | 0.7s | 49.5s | 42s |
| 0.8 | 78% | 0.69 | 0 | 0.9s | 49.5s | 47s |
| 0.6 | 90% | **1.3** | 4 | 2.7s | 49.5s | **漏掉** |
| 0.5 | 99% | **1.6** | 8 | 3.1s | 49.5s | 90s |
| 0.4 | 99% | **2.8** | 14 | 7.0s | 49.5s | 84s |
| 0.25 | 100% | **3.0** | 14 | 5.9s | 49.5s | **漏掉** |
| 0.15 | 100% | **7.7** | 21 | 11.1s | 49.5s | 90s |

每階段只有 1 次翻轉事件，延遲是單次量測，不是統計值；「漏掉」是該次事件 60 秒的成立時間內沒有發出通知。量測時機器上同時有其他實驗，獨立 group 的延遲全程穩定，表示干擾不大。

- **真正的轉折點是「group 耗時 ÷ interval 超過 1」。**0.8 核時 throttling 已有 78%，但偵測延遲幾乎沒變；到 0.6 核比值超過 1，才開始跳過 evaluation、延遲增加。Throttling 適合當早期訊號，這個比值才是 rule 失準的訊號。
- **小 group 在邊際下幾乎不受影響。**每個 group 有自己的 goroutine，一條 alert 的查詢很便宜，不用排在重查詢後面。0.15 核時兩者延遲差了將近一倍。<span class="at-chip at-ev-code">讀碼</span>[^p-group-run]
- **短暫事件在重 group 可能完全漏掉。**事件持續時間要遠大於 `for` 加上 group 的實際間隔。
- **Scrape 最晚受影響。**0.4 核時才接近 timeout，這時 rule 已經跳過很多次了。

### 記憶體邊際：多少餘裕才安全

限制以 working set（1.49GB）為基準逐步壓低，每一步執行 4 分鐘，再做一次非正常重啟並觀察 4 分鐘。<span class="at-chip at-ev-tested">實測</span>[^exp-prom-mem]

| Limit ÷ working set | 等於 working set 佔 limit | 執行中重啟次數 | 非正常重啟後再重啟次數 | 帶 `for` canary 最大中斷 |
|---|---|---|---|---|
| 1.5 | 67% | 0 | 0 | 30s |
| 1.3 | 77% | 0 | 0 | 35s |
| 1.2 | 83% | 0 | 0 | 30s |
| 1.1 | 91% | **1** | **2** | 35s |
| 1.0 | 100% | **4** | **6** | **240s** |

- **這個規模下，working set 超過 limit 的 83% 就開始有風險。**規則的門檻因此設為 80%。Go 的記憶體用量會隨查詢和 compaction 波動，不是穩定值。
- **這裡沒有看到「只有重啟才 OOM」。**實驗的 WAL 只有幾十分鐘的資料，replay 只要 8–13 秒。上游回報大型環境 replay 記憶體可達平時 2–3 倍 <span class="at-chip at-ev-upstream">社群</span>，大型環境不能用這個結果推論，請以實際重啟時的記憶體尖峰為準。
- **Crash loop 同樣騙過 `Watchdog`。**Limit 等於 working set 時，`Watchdog` 最大間隔只有 30–35 秒，帶 `for` 的 canary 才會斷。

### 儲存接近極限時的結果

**慢磁碟先傷害 TSDB 維護，不是 rule。**WAL 平常寫入 page cache，所以 10 IOPS 下 scrape 和 rule 幾乎不受影響。真正變慢的是 fsync 和 compaction，因為 reload 和 compaction 在同一條 goroutine，retention 也跟著延後。要注意，這裡記憶體充足，page cache 吃緊時結果可能不同。<span class="at-chip at-ev-tested">實測</span>[^exp-prom-storage]

| 階段 | 寫 16MB 加 fsync | WAL fsync p99（平均／最大） | 大 target scrape 平均 | Rule group 平均 | 每 5 分鐘 reload |
|---|---|---|---|---|---|
| 沒有限制 | 0s | 0.02s | 0.93s | 9.2s | 3.9 |
| 50 IOPS，4 MB/s | 7s | 0.16／0.73s | 0.77s | 10.3s | 5.1 |
| 10 IOPS，1 MB/s | 98s | 4.8／7.7s | 0.68s | 8.1s | **2.7** |

沒有限制的階段包含啟動初期，reload 次數因此偏低，5 次才是正常值。

**真實磁碟滿。**用固定大小的 tmpfs 真的寫滿，再釋放空間、最後重啟，四個版本的流程都相同：<span class="at-chip at-ev-tested">實測</span>[^exp-prom-storage]

| 階段 | 發生什麼 | 訊號 |
|---|---|---|
| **磁碟滿（10 分鐘）** | Scrape 和 rule 結果都寫不進去，Head 不再前進，也就不會產生新 block。Block 數維持不變。 | `wal_writes_failed_total` 10–15s 內增加；`rule_evaluation_failures_total` 增加；`reloads_total` 正常 |
| **釋放空間** | 寫入立刻恢復，看起來一切正常，但 WAL 已經留下半筆紀錄。下一次 compaction（約 520–525s 後）checkpoint 失敗，WAL 從 83MB 長到約 135MB。 | `checkpoint_creations_failed_total`、`compactions_failed_total`；`wal_corruptions_total` **仍是 0** |
| **重啟** | 修復 WAL 後恢復正常，WAL 回到約 40MB。 | `wal_corruptions_total` 變成 1；log 出現 `Encountered WAL read error` |

- **相同機制，訊息不一定相同。**2.51.2 和 3.13.5 印出 `unexpected full record`，2.55.1 和 3.15.1 印出 `unexpected first record`，取決於下一筆紀錄的型態。告警不要依賴特定文字。
- **OOM 也會損毀 WAL，但訊息不同。**OOM crash loop 中讀到的是 `unexpected checksum`，所以從 log 可以區分方向：`unexpected full/first record` 先查寫入失敗，`unexpected checksum` 先查行程被殺或機器當機。這只是這次實驗的對應關係，不是絕對規則。
- **釋放空間不會治好 WAL。**寫入恢復後要有 checkpoint 失敗的告警才看得出來，否則會以為問題已解決。

### 磁碟被 compaction 用完：reload 真的會停

只留 60MB 可用空間，讓 head compaction 自己寫不下 block。這時 WAL 還寫得進去，所以 commit 繼續、compaction 持續被觸發。2.51.2 和 3.13.5 結果相同：<span class="at-chip at-ev-tested">實測</span>[^exp-prom-storage]

| 時間（2.51.2／3.13.5） | 訊號 | 代表 |
|---|---|---|
| 145s／150s | `compactions_failed_total` 增加 | **最早的預警**，此時資料都還寫得進去 |
| 360s／360s | Head 超過門檻未落成 block | Head 開始堆積 |
| 360s／370s | `wal_writes_failed_total` 增加 | 空間用完，資料開始遺失 |
| 525s／515–530s | Heartbeat 與 canary 中斷 | `Watchdog` 仍正常（最大間隔 35s） |
| 565s／575s | `reloads_total` 5 分鐘內為 0 | **Reload 被餓死**，retention 不會執行 |
| 765s／770s | 最舊資料超過 retention | Retention 已實際落後 |

**Retention 卡住有兩種路徑，要分開看。**

| 路徑 | `reloads_total` | `compactions_failed_total` | 原因 | Size retention 有用嗎 |
|---|---|---|---|---|
| 磁碟突然完全寫滿 | 正常 | 不增加 | Commit 全失敗，Head 不前進、不產生新 block，time retention 的基準凍結 | 有機會，reload 仍在跑（未實測） |
| Compaction 用完空間 | **停止** | **持續增加** | Head 維持可 compact、commit 持續送訊號，reload timer 不斷重置 | 沒有用，retention 在 reload 裡（讀碼）[^p-db-run] |

釋放空間後 compaction 和 reload 都恢復了。這裡用 5 分鐘的 block 加速實驗，時間只反映相對順序。

## 5. Rule 應該怎麼寫

用尖峰的「最差值」反推參數，不要用設定值。下表的例子沿用第 2 節的假設數字（S 30s、尖峰 d ≈ 100s、I ≈ 120s）。

| 參數 | 規則 | 例子 | 依據 |
|---|---|---|---|
| Offset O | O ≥ 尖峰 d，再留一些餘裕 | `offset 2m` | <span class="at-chip at-ev-tested">實測</span> <span class="at-chip at-ev-inferred">推算</span>[^exp-prom-silent] |
| Range R | O ≥ d 時 R ≥ (2 + k) × 尖峰 I；沒有 offset 時再加上 d。k 是要容忍的 scrape 失敗次數 | `[10m]`（搭配 2m offset 可容忍 3 次） | <span class="at-chip at-ev-tested">實測</span> <span class="at-chip at-ev-inferred">推算</span>[^exp-prom-range] |
| Evaluation E | E ≥ 尖峰 T ÷ 0.8 | 最慢的 group 拉長 interval，或拆 group | <span class="at-chip at-ev-inferred">推算</span> |
| `for` F | F 要遠大於尖峰 I，不要短到只涵蓋 1–2 次 scrape | 至少 5m | <span class="at-chip at-ev-inferred">推算</span> |
| Alert 與 recording rule | 有依賴關係就放同一個 group | — | <span class="at-chip at-ev-code">讀碼</span>[^p-group-eval] |
| 關鍵 alert 的位置 | 獨立成小 group，不跟昂貴的 recording rule 放一起 | 把關鍵 alert 從慢 group 移出 | <span class="at-chip at-ev-tested">實測</span>[^exp-prom-cpu] |

2.51 不支援 `query_offset`，要把 offset 寫在表達式內，每個 selector 都要加：

```yaml
# 修改前：慢 scrape 時反覆重置 for，可能永遠不會 firing
- alert: MyAlert
  expr: rate(my_metric_total{job="my-job"}[5m]) > 100
  for: 5m

# 修改後（2.51 可用）
- alert: MyAlert
  expr: rate(my_metric_total{job="my-job"}[10m] offset 2m) > 100
  for: 5m
```

### 實測效果

同樣是永遠成立的條件，設定為 2 分鐘間隔、100 秒延遲、`for: 3m`，跑 25 分鐘：<span class="at-chip at-ev-tested">實測</span>[^exp-prom-silent]

| 版本 | 沒加 offset：是否 firing／20 分鐘重置次數 | 表達式 `offset 2m`：開始 firing | `query_offset: 2m`：開始 firing |
|---|---|---|---|
| 2.51.2 | **從未 firing**／9 次 | 540s | 不支援 |
| 2.55.1 | **從未 firing**／9 次 | 555s | 435s |
| 3.13.5 | **從未 firing**／10 次 | 510s | 375s |
| 3.15.1 | **從未 firing**／9 次 | 555s | 420s |

時間從環境啟動算起，包含初期資料累積。

### R 該設多大：視窗矩陣

每個格子都是一條永遠成立的 alert，`scrape_interval` 60s、`for: 3m`，觀察 24 分鐘。下表的 d 是 target 回應延遲。<span class="at-chip at-ev-tested">實測</span>[^exp-prom-range]

| Range | Offset | d = 5s | d = 20s | d = 40s | d = 55s |
|---|---|---|---|---|---|
| `[2m]`（2I） | 0 | 2.51 失敗／3.13 正常 | **從未 firing** | **從未 firing** | **從未 firing** |
| `[2m]`（2I） | 1m | 正常 | 正常 | 正常 | 正常 |
| `[3m]`（3I） | 0 或 1m | 正常 | 正常 | 正常 | 正常 |
| `[5m]`（5I） | 0 或 1m | 正常 | 正常 | 正常 | 正常 |

- **公式與實測一致。**視窗要裝得下 2 個已可見的 sample，所以沒有 offset 時 R ≥ 2I + d，有足夠 offset 時 R ≥ 2I。2 分鐘間隔、100 秒延遲、`[5m]` 算出需要 340 秒，實測確實無法 firing；加 `offset 2m` 後只需 240 秒，實測就恢復了。
- **d 再小，R = 2I 也可能失敗。**只要 evaluation 剛好落在 scrape 進行中，當次就只剩 1 個點。d = 5s 那一格在 2.51.2 每分鐘重置一次，在 3.13.5 正常，這應該是 scrape 和 evaluation 的相位不同，兩邊的相位都無法事先決定，不能當作版本差異。
- **公式只保證不回傳空結果。**`rate` 仍可能被低估，門檻太貼近實際值時還是可能抖動。低估的計算見 [Alert Pending 抖動分析](pending-jitter.md#rate-irate)。
- **矩陣沒有涵蓋 d 大於 interval 的組合**，那種情況只有上面 2 分鐘間隔的對照可參考。d 接近或超過 I 時要留更多餘裕。

## 6. 上線一條 target rule 前要看什麼

每條規則需要回答五個問題。很多資料只有 API 才有，metric 只到 group 層級。

| 問題 | Metric 或 PromQL | API 欄位 |
|---|---|---|
| **資料存在，而且語意正確嗎？** | `count(M)` | `/api/v1/targets/metadata` 的 `type`、`help`、`unit`。`rate` 要用在 counter 上 |
| **Target 健康嗎？** | `up`、`scrape_duration_seconds`、`scrape_samples_scraped`、`scrape_samples_post_metric_relabeling`、`scrape_series_added` | `/api/v1/targets` 的 `health`、`lastError`、`lastScrape`、`lastScrapeDuration`、`scrapeInterval`、`scrapeTimeout`、`discoveredLabels` |
| **Rule 看得到足夠的資料嗎？** | `count_over_time(M[R])`、`time() - timestamp(M)`，要用下方探針在 rule 執行時記錄 | 無 |
| **Rule 有準時跑嗎？** | `prometheus_rule_group_last_duration_seconds`、`iterations_missed_total`、`rule_evaluation_failures_total`（皆為 group 層級） | `/api/v1/rules` 的 `health`、`lastError`、`evaluationTime`、`lastEvaluation`（單條 rule 層級） |
| **Alert 的狀態穩定嗎？** | `ALERTS`、`changes(ALERTS_FOR_STATE{alertname="X"}[1h])` | `/api/v1/rules` 的 `state`、`duration`、`keepFiringFor`、`alerts[].activeAt` |

### Prometheus 自身狀態

- `/api/v1/status/runtimeinfo`：`GOMAXPROCS`、`GOMEMLIMIT`、`corruptionCount`、`storageRetention`、`reloadConfigSuccess`。
- `/api/v1/status/tsdb`：head series 數，以及 series 最多的 metric 與 label。
- `--enable-feature=extra-scrape-metrics`：多出 `scrape_timeout_seconds`、`scrape_sample_limit`、`scrape_body_size_bytes`，可以直接計算 scrape 對 timeout 的邊際。

### 記錄 rule 當下看到的資料 {#probe}

事後查 `count_over_time` 看到的是已經寫入的完整資料，無法重現 rule 當下的狀況。把探針放在 alert 同一個 group，就能記下當時的真實狀態。

```yaml
# 把下面的 M、job、[R] 換成實際 rule 用的值，放在該 alert 的同一個 group、同一個 interval。
# 查詢是在 rule 執行當下算的，所以記錄的是 rule 當時看到的狀況，事後查詢無法重現。
groups:
  - name: example-with-probes
    interval: 30s
    rules:
      - record: rule_probe:visible_samples:min      # 視窗內可見 sample 數，低於 2 時 rate 回傳空
        expr: min(count_over_time(M{job="my-job"}[10m] offset 2m))
      - record: rule_probe:data_age_seconds:max     # 最舊 series 的資料年齡，約等於 I + d
        expr: max(time() - timestamp(M{job="my-job"}))
      - record: rule_probe:series:count             # series 數掉到 0 時 alert 不會觸發
        expr: count(M{job="my-job"}) or vector(0)
      - alert: RuleProbeTooFewSamples               # 探針本身也可以告警
        expr: rule_probe:visible_samples:min < 2
        for: 5m
      - alert: MyAlert
        expr: rate(M{job="my-job"}[10m] offset 2m) > 100
        for: 5m
```

實測（2 分鐘間隔、100 秒延遲、`rate[5m]`、`for: 3m`，觀察 15 分鐘）：<span class="at-chip at-ev-tested">實測</span>[^exp-prom-probe]

| 項目 | Rule 當下記錄的探針 | 事後查詢 | 說明 |
|---|---|---|---|
| 視窗內 sample 最大值 | 2 | 3 | 事後查詢多看到 rule 當時還看不到的點，這是「事後看起來應該要觸發」的原因 |
| 視窗內 sample 最小值 | **1** | 1 | 探針低於 2 就直接解釋了 `rate` 為何回傳空；這段期間 `for` 重置 7 次、從未 firing |
| 資料年齡 | 114–219s | 無 | 最大值符合 I + d = 220s，可以直接用來設定 O 和 R |

事後查詢的最小值也是 1，是因為查詢範圍包含現在，最新的 sample 還沒寫入，不代表歷史上每一刻都只有 1 點。

## 7. Monitor of monitor 架構

| 層 | 做法 | 驗證什麼 | 放在哪裡 |
|---|---|---|---|
| **L0 外部鏈路** | `Watchdog`（`vector(1)`）送到外部 dead man's switch，等待時間大於 `repeat_interval`；停機偵測延遲約為 `4 × max(resend, E)` 加上等待時間 | Prometheus → Alertmanager → 通知管道活著 | 外部服務，不依賴這個叢集 |
| **L1 資料新鮮度** | 每個關鍵 job 一條 heartbeat，例如 `time() - max(timestamp(M)) < 門檻` | Scrape → TSDB → 查詢路徑有新資料 | 受測 Prometheus，通知送外部 |
| **L2 判斷正確性** | 已知成立的 canary，帶 `for`；再加定時翻轉的 canary 量實際偵測延遲 | `for` 不會重置、通知真的能在預期時間內到達 | 放在和真實 rule 相同負載的 group 裡 |
| **L3 邊際** | 第 8 節的規則：scrape、rule、查詢、通知、WAL、資源 | 距離失效還有多遠，提早預警 | 外部 meta Prometheus，抓 `/metrics` 並選擇性 `/federate` |

見 [Dead man's switch 的等待時間](../contracts.md#deadman-wait)。

### 從 blackbox 探測再往前一步

用另一台 Prometheus 加 blackbox 探測，可以解決「監控自己掛掉時無人知道」的問題，但它只能證明行程活著、HTTP 有回應。實驗中每 15 秒探測一次（`/-/healthy`、`/-/ready`、實際查詢、`/api/v2/status`）：<span class="at-chip at-ev-tested">實測</span>[^exp-prom-blackbox]

| 故障 | Prometheus `/-/healthy`、`/-/ready` | Prometheus 實際查詢 | Alertmanager 探測 | 通知是否受影響 | 真正抓到的訊號 |
|---|---|---|---|---|---|
| CPU 不足，rule 跳過 | 全部成功 | 全部成功 | 全部成功 | 延遲變大 | `iterations_missed`，50s |
| 查詢飽和 | 全部成功 | 10／14 成功 | 全部成功 | 延遲變大 | 查詢 slot 比值，70s |
| Target 停機 | 全部成功 | 全部成功 | 全部成功 | 依賴它的 alert 不會觸發 | `up == 0`，85s |
| **接收端停機** | 全部成功 | 全部成功 | 全部成功 | **全部通知中斷** | `alertmanager_notifications_failed_total` |
| OOM crash loop（重啟 4 次） | 幾乎全部成功（18／19） | 18／19 | 全部成功 | 曾短暫中斷 | `process_start_time_seconds`，10s |
| 磁碟完全寫滿 | 全部成功 | 全部成功 | 全部成功 | 資料不再寫入，canary 中斷 | `wal_writes_failed`，15s |
| Alertmanager 停機 | 全部成功 | 全部成功 | **全部失敗，有發現** | 全部通知中斷 | Blackbox |
| Prometheus 停機 | **全部失敗，有發現** | **全部失敗，有發現** | 全部成功 | 約 4 分鐘後中斷 | Blackbox |

**8 種故障裡，存活探測只明確抓到 2 種。**另外，慢 scrape 讓 alert 無法 firing 時，HTTP 也完全正常。所以 blackbox 要保留，但只能當最底層。建議用同一台外部 Prometheus 依序往上補：

| 順序 | 加什麼 | 補上哪個盲區 |
|---|---|---|
| 1 | 保留 blackbox，並加一個真的跑查詢的探測（下方設定） | 查詢路徑飽和 |
| 2 | 同一台外部 Prometheus 直接抓兩者的 `/metrics` | Rule 跳過、WAL、compaction、reload、通知失敗、Alertmanager 送達失敗 |
| 3 | 用 `/federate` 拉 `up`、`scrape_duration_seconds`、`ALERTS*` | 慢 scrape、target 停機、`for` 反覆重置 |
| 4 | 受測 Prometheus 加上帶 `for` 的資料 canary，送到外部接收端 | Crash loop、磁碟寫滿、端到端判斷 |
| 5 | Watchdog 送到真正外部的 dead man's switch，並定期實測 | 外部 Prometheus 和 Alertmanager 一起壞掉、網路中斷 |
| 6 | K8s 訊號：cAdvisor、kube-state-metrics、PVC 用量 | Throttling、OOMKilled 原因、磁碟剩餘空間 |

**要特別確認：外部 Prometheus 的告警走哪條路？**如果它也送到被監控的同一組 Alertmanager，Alertmanager 掛掉時它就無法通知。要嘛有獨立的 Alertmanager，要嘛用不同的接收路徑。外部 Prometheus 本身也需要 dead man's switch，否則只是把問題往外移一層。

```yaml
# blackbox.yml：查詢探測要驗證回應內容，不能只看 HTTP 200
modules:
  prometheus_query:
    prober: http
    timeout: 5s
    http:
      fail_if_body_not_matches_regexp: ['"status":"success"']

# 外部 Prometheus 的探測目標（URL 參數記得編碼）
#   http://prometheus:9090/-/ready
#   http://prometheus:9090/api/v1/query?query=vector(1)
#   http://alertmanager:9093/-/ready
#   http://alertmanager:9093/api/v2/status
```

探測間隔 15 秒，短暫重啟可能剛好落在兩次探測之間，crash loop 因此幾乎看不出來。Prometheus 停機時 Alertmanager 還會重送到 `EndsAt`，所以通知約 4 分鐘後才中斷。

### K8s 的擺放原則

- **Meta Prometheus 要和受測對象分開故障。**至少不同 node、不同儲存；能跨叢集更好。Grafana Labs 的做法是用多台跨地區的 meta Prometheus 互相監控，再用 dead man's switch 兜底。<span class="at-chip at-ev-upstream">社群</span>
- **Meta Prometheus 要輕量。**只抓 `/metrics` 和少量 `/federate`，避免同時被同一個原因拖垮。實驗中的 meta 全程穩定，但它的負載很小。
- **Prometheus 看不到的資訊，要從 K8s 補。**OOMKilled 和重啟要看 kube-state-metrics，throttling 和 working set 要看 cAdvisor，PVC 用量要看 kubelet。
- **大型 Prometheus 的重啟時間本身就是盲區。**Replay 期間 rule 不會執行。實驗環境 30 萬 series 只要 8–13 秒，大型環境無法在單機重現，請直接看 `prometheus_tsdb_data_replay_duration_seconds` 和上次重啟的時間，並把它當作偵測盲區的已知長度。

### K8s 實測結果

在 minikube 上跑 kube-prometheus-stack 57.2 和 Prometheus 2.51.2，實際載入第 8 節的規則。<span class="at-chip at-ev-tested">實測</span>[^exp-prom-k8s]

| 項目 | 結果 |
|---|---|
| CPU 限制 150m | Throttling 約 70%。`PrometheusCPUThrottled` 約 14 分鐘觸發，內建 `CPUThrottlingHigh` 約 18 分鐘（含 pod 重建） |
| OOM | 重啟 6 次、`OOMKilled`、`CrashLoopBackOff`。**Prometheus 自己查不到原因**，因為當時沒有在抓資料，所以要由外部 Prometheus 抓 kube-state-metrics |
| Alertmanager 不存在 | `NoAlertmanagerDiscovered`、`NotificationsDropped` 都有觸發 |
| 記憶體接近 limit | 比值到 0.9 又回落，當時 `for: 10m` 來不及觸發。已改為 `for: 5m`，以 `promtool test rules` 驗證，K8s 上沒有再觸發一次 |
| PVC 用量 | Minikube 的 hostPath 沒有 `kubelet_volume_stats_*`，**未驗證** |

**K8s 上容易寫錯，而且不會報錯的地方：**

- **`job="prometheus"` 對不上。**ServiceMonitor 的 job 通常是 service 名稱，規則 `health=ok` 卻永遠沒資料。規則已改用 `job=~".*prometheus"`。
- **`container_spec_memory_limit_bytes` 被 chart 預設丟棄。**記憶體比值要用 kube-state-metrics 的 `kube_pod_container_resource_limits`。
- 這兩個都是第一版規則的錯誤，也正是本文談的「看起來正常、實際上永遠不會響」。

**kube-prometheus-stack 的預設規則不夠。**依 metric 名稱比對，預設 126 條 alert 只涵蓋約一半的訊號，沒有這些：資料新鮮度 heartbeat、`for` 重置、scrape 與 rule group 耗時比值、WAL 寫入與 checkpoint 失敗、Head 未落成 block、retention 落後、記憶體接近 limit。而且它們和 Prometheus 跑在一起，Prometheus 掛掉時無法通知自己。

## 8. 告警規則 {#rules}

### 這些規則是針對什麼對象？

**沒有一套對象能涵蓋全部。**規則依 metric 的 label 分成不同層級，每條都帶 `scope` label，可以用它分流給不同負責人。要注意的是，**沒有任何內建 metric 能精確到單條 rule**，rule 只到 group 層級。

| `scope` | 監控對象 | 識別用的 label | 規則 | 放在哪裡評估 | 建議負責人 |
|---|---|---|---|---|---|
| `target` | 單一 scrape 對象 | `job`、`instance` | `ScrapeSlowerThanInterval` | 受測 Prometheus，或 `/federate` 到 meta | Target 或服務負責人 |
| `scrape_interval` | 同一 interval 的所有 target | `interval`（看不出是哪個 target） | `ScrapeIntervalStretched` | Meta | 平台 |
| `rule_group` | 一個 rule group | `rule_group`（`檔名;group 名稱`） | `RuleGroupNearInterval`、`RuleGroupMissingEvaluations`、`RuleGroupStale`、`RuleEvaluationFailures` | Meta | Rule 作者 |
| `alert` | 單一 alert 實例 | `alertname` 加上 alert 的 label | `AlertForTimerResetting` | 受測 Prometheus，或 `/federate` 到 meta | Rule 作者 |
| `prometheus` | 整個 Prometheus 行程 | `instance` | `QueryQueueing`、`NotificationsDropped`、`NoAlertmanagerDiscovered`、`PrometheusRestarted`、WAL、reload、Head、retention 規則 | Meta | 平台 |
| `alertmanager` | 單台 Alertmanager | `alertmanager` | `NotificationsFailing`、`AlertmanagerNotificationsFailing`、`AlertmanagerConfigReloadFailed` | Meta | 平台 |
| `container` | Prometheus 容器 | `namespace`、`pod`、`container` | `PrometheusCPUThrottled`、`PrometheusMemoryNearLimit` | Meta | 平台 |
| （無） | **單條 rule** | 沒有內建 metric | 用 `/api/v1/rules`，或第 6 節的探針自行記錄 | 與該 alert 同一個 group | Rule 作者 |

### 判斷一條 alert 能不能信，要沿著這條鏈查

一條 alert 依賴它讀取的 target、所在的 group、整個 Prometheus 和通知路徑。任何一層異常，這條 alert 的結果都要打折扣。

```text
# 以 alert X 為例，它的 metric 來自 job="my-job"，放在 file.yml 的 group my-group

# 1. Target：資料來源是否健康
up{job="my-job"}
scrape_duration_seconds{job="my-job"}

# 2. Rule group：是否準時
prometheus_rule_group_last_duration_seconds{rule_group="file.yml;my-group"}
increase(prometheus_rule_group_iterations_missed_total{rule_group="file.yml;my-group"}[1h])

# 3. Alert：for 是否一直重置
changes(ALERTS_FOR_STATE{alertname="X"}[1h])

# 4. 單條 rule：只有 API 才有
curl -s 'http://prometheus:9090/api/v1/rules?type=alert' | jq '.data.groups[].rules[] | select(.name=="X") | {health, lastError, evaluationTime, lastEvaluation}'

# 5. Prometheus 與通知路徑：scope 為 prometheus、alertmanager、container 的告警是否正在觸發
ALERTS{alertstate="firing", scope=~"prometheus|alertmanager|container"}
```

### 使用前要調整的地方

- **範例以 30s scrape 與 30s evaluation 為基準。**`ScrapeSlowerThanInterval` 的 25 秒只適用 30s interval，其他 interval 的 job 要排除或另外寫。開啟 `extra-scrape-metrics` 後，可以改用 `scrape_duration_seconds / scrape_timeout_seconds`。
- **不是每條規則都實際觸發過。**24 條在 K8s 上都能評估，大多有資料，但只有部分真的觸發過，門檻也要依環境調整。[^exp-prom-k8s]

### 完整規則

已通過 2.51.2 與 3.13.5 `promtool check rules`，也在 K8s 上實際載入。這裡保留原始報告的版本；本站整理後的規則範本在 repo 的 `rules/` 目錄。

```yaml
# 以 scrape/eval 30s 為基準，interval 與門檻請依環境調整。job 用 .*prometheus 比對，container 假設為 prometheus。
# scope label 標示這條規則監控的對象，可用來分流給不同負責人。
# scope=target、alert 的 series 不在 /metrics，要放在受測 Prometheus，或用 /federate 送到 meta。其餘放 meta。
groups:
  - name: pipeline-contract           # 邊際：契約是否還成立
    rules:
      - alert: ScrapeSlowerThanInterval
        expr: max by (job, instance) (scrape_duration_seconds{job!~"job-with-other-interval"}) > 25
        for: 10m
        labels: { severity: warning, scope: target }
        annotations: { summary: "scrape 耗時接近 30s interval，實際間隔即將拉長" }
      - alert: ScrapeIntervalStretched
        expr: prometheus_target_interval_length_seconds{interval="30s",quantile="0.9"} > 40
        for: 5m
        labels: { severity: critical, scope: scrape_interval }
        annotations: { summary: "實際 scrape 間隔明顯大於設定值，rate 視窗內樣本變少" }
      - alert: RuleGroupNearInterval
        expr: |
          prometheus_rule_group_last_duration_seconds
            > 0.8 * prometheus_rule_group_interval_seconds
        for: 10m
        labels: { severity: warning, scope: rule_group }
        annotations: { summary: "rule group 耗時超過 interval 的 80%" }
      - alert: RuleGroupMissingEvaluations
        expr: increase(prometheus_rule_group_iterations_missed_total[10m]) > 0
        labels: { severity: critical, scope: rule_group }
        annotations: { summary: "rule group 已跳過 evaluation，偵測延遲增加" }
      - alert: RuleGroupStale
        expr: |
          time() - prometheus_rule_group_last_evaluation_timestamp_seconds
            > 3 * prometheus_rule_group_interval_seconds
        for: 2m
        labels: { severity: critical, scope: rule_group }
        annotations: { summary: "rule group 超過 3 個 interval 沒有完成 evaluation" }
      - alert: RuleEvaluationFailures
        expr: increase(prometheus_rule_evaluation_failures_total[10m]) > 0
        labels: { severity: critical, scope: rule_group }
        annotations: { summary: "rule 查詢或寫入失敗，請查 /api/v1/rules 的 lastError" }
      - alert: AlertForTimerResetting
        expr: changes(ALERTS_FOR_STATE{alertname!~"Watchdog|Meta.*"}[1h]) > 3
        labels: { severity: warning, scope: alert }
        annotations: { summary: "alert 的 for 計時反覆重置，條件可能成立卻無法 firing；先排除 Prometheus 剛重啟" }
      - alert: QueryQueueing
        expr: prometheus_engine_query_duration_seconds{slice="queue_time",quantile="0.9"} > 1
        for: 5m
        labels: { severity: warning, scope: prometheus }
        annotations: { summary: "查詢在排隊，rule 與 API 共用同一組 slot" }

  - name: pipeline-delivery           # 通知鏈路
    rules:
      - alert: NotificationsFailing
        expr: rate(prometheus_notifications_errors_total[5m]) > 0
        for: 5m
        labels: { severity: critical, scope: alertmanager }
        annotations: { summary: "送往 Alertmanager 失敗" }
      - alert: NotificationsDropped
        expr: increase(prometheus_notifications_dropped_total[10m]) > 0
        labels: { severity: critical, scope: prometheus }
        annotations: { summary: "通知已被丟棄" }
      - alert: NoAlertmanagerDiscovered
        expr: prometheus_notifications_alertmanagers_discovered < 1
        for: 5m
        labels: { severity: critical, scope: prometheus }
        annotations: { summary: "沒有可用的 Alertmanager" }

      - alert: AlertmanagerNotificationsFailing
        expr: sum by (instance, integration) (rate(alertmanager_notifications_failed_total[5m])) > 0
        for: 5m
        labels: { severity: critical, scope: alertmanager }
        annotations: { summary: "Alertmanager 送不到接收端；探測 Alertmanager 本身仍會正常" }
      - alert: AlertmanagerConfigReloadFailed
        expr: alertmanager_config_last_reload_successful == 0
        for: 10m
        labels: { severity: warning, scope: alertmanager }
        annotations: { summary: "Alertmanager 新設定沒有生效" }

  - name: pipeline-storage            # 儲存與 WAL
    rules:
      - alert: PrometheusRestarted
        expr: changes(process_start_time_seconds{job=~".*prometheus"}[30m]) > 0
        labels: { severity: warning, scope: prometheus }
        annotations: { summary: "近期重啟過；頻繁重啟時 Watchdog 可能看不出來" }
      - alert: WALWriteFailed
        expr: increase(prometheus_tsdb_wal_writes_failed_total[15m]) > 0
        labels: { severity: critical, scope: prometheus }
        annotations: { summary: "WAL 寫入失敗，新資料與 rule 結果無法寫入" }
      - alert: WALCheckpointFailing
        expr: increase(prometheus_tsdb_checkpoint_creations_failed_total[6h]) > 0
        labels: { severity: critical, scope: prometheus }
        annotations: { summary: "checkpoint 失敗，WAL 無法截斷" }
      - alert: WALRepairedAtStartup
        expr: prometheus_tsdb_wal_corruptions_total > 0
        labels: { severity: warning, scope: prometheus }
        annotations: { summary: "本次啟動修復過 WAL，修復點之後的資料已遺失" }
      - alert: WALFsyncSlow
        expr: prometheus_tsdb_wal_fsync_duration_seconds{quantile="0.99"} > 1
        for: 10m
        labels: { severity: warning, scope: prometheus }
        annotations: { summary: "WAL fsync 變慢，compaction 與 retention 會先受影響" }
      - alert: HeadNotCompacting
        expr: prometheus_tsdb_head_max_time_seconds - prometheus_tsdb_head_min_time_seconds > 4 * 3600
        for: 30m
        labels: { severity: critical, scope: prometheus }
        annotations: { summary: "Head 超過 4 小時未落成 block（以預設 2h block range 計算）" }
      - alert: RetentionBehind
        expr: |
          (time() - prometheus_tsdb_lowest_timestamp_seconds)
            > prometheus_tsdb_retention_limit_seconds * 1.1 + 6 * 3600
          and prometheus_tsdb_retention_limit_seconds > 0
        for: 1h
        labels: { severity: critical, scope: prometheus }
        annotations: { summary: "最舊資料已超過 retention，加上 10% 與 6 小時緩衝" }
      - alert: CompactionFailing
        expr: increase(prometheus_tsdb_compactions_failed_total[3h]) > 0
        labels: { severity: warning, scope: prometheus }
        annotations: { summary: "compaction 失敗；可用空間不足時會比 WAL 失敗更早出現" }
      - alert: BlockReloadStalled
        expr: increase(prometheus_tsdb_reloads_total[15m]) == 0
        for: 5m
        labels: { severity: critical, scope: prometheus }
        annotations: { summary: "reload 停止，retention 沒有執行機會" }

  - name: pipeline-resources          # K8s 資源，需要 cAdvisor 與 kube-state-metrics
    rules:
      - alert: PrometheusCPUThrottled
        expr: |
          rate(container_cpu_cfs_throttled_periods_total{container="prometheus"}[5m])
            / rate(container_cpu_cfs_periods_total{container="prometheus"}[5m]) > 0.25
        for: 10m
        labels: { severity: warning, scope: container }
        annotations: { summary: "Prometheus 容器被 CPU throttling" }
      - alert: PrometheusMemoryNearLimit
        expr: |
          container_memory_working_set_bytes{container="prometheus"}
            / on (namespace, pod, container) group_left
          kube_pod_container_resource_limits{container="prometheus",resource="memory"} > 0.8
        for: 5m
        labels: { severity: warning, scope: container }
        annotations: { summary: "working set 接近 limit，重啟 replay 可能 OOM" }
```

## 9. 要不要升級

**升級值得做，但不能取代前面的修正。**2.51 已不受支援，升級能帶來 `query_offset` 和較好的預設值，不過它修不好 scrape 太慢，也修不好 WAL 半筆寫入。相同負載實測下，CPU 與耗時也沒有明顯改善。[^exp-prom-upgrade]

| 步驟 | 帶來什麼 | 解決不了什麼 | 支援狀態 |
|---|---|---|---|
| **留在 2.51** | 加上第 8 節監控、表達式 `offset`、拉長 range、拆慢 group、開 `auto-gomaxprocs` | 只能逐條修改 rule 的 offset | 已無支援 |
| **中繼：2.55.1** | `query_offset`（2.53+）[^p255-query-offset]、`scrape_failure_log_file`、官方支援的回退點 | Scrape 慢、WAL 半筆寫入 | 已無支援，當跳板 |
| **目標：3.13 LTS** | LTS 支援到 2027-07-31、預設自動對齊 `GOMAXPROCS` 與 `GOMEMLIMIT`、多台 Alertmanager 互不拖累（3.10 起） | Scrape 慢、WAL 半筆寫入；3.13.x 也還沒有 reload timer 修正 | 支援中 |

### 每項改善的白話說明

| 改善 | 原本的問題 | 改善後 | 版本 | 對 rule 信任的幫助 |
|---|---|---|---|---|
| `query_offset` | Rule 查到還沒寫入的資料。2.51 只能在每個 selector 寫 `offset`，容易漏改 | 可以設在整個 group 或全域，rule 的表達式不用改 | 2.53 | 高：直接處理 pending 抖動 |
| TSDB 格式相容 | 官方文件說 3.x 的資料只有 2.55 以上讀得懂 | 2.55 預先看得懂未來的 index V3，官方支援的回退點 | 2.55 | 中：只影響升級安全，平常沒有差別；實際差異見下方 |
| Alert 恢復只查一次 | 重啟時每個 alert 實例查一次，實例多就慢 | 每條 rule 只查一次，再自行比對 | 2.53 | 低：3 萬個實例實測只差不到 1 秒；`for` 小於 10m 不適用 |
| 自動對齊 `GOMAXPROCS` | Go 用節點核心數當執行緒上限，超過 CPU limit 就被 throttle | 依 CPU limit 自動設定 | 3.0 預設；2.51 可用 flag | 高：不必升級就能開 |
| 自動對齊 `GOMEMLIMIT` | Go 不知道 memory limit，接近時來不及 GC 就被 OOM | 依 memory limit 提早 GC | 3.0 預設；2.51 可用 flag | 中：本次實驗未見明顯差異（兩組都沒 OOM） |
| `scrape_failure_log_file` | Scrape 失敗只留最後一次錯誤，歷史原因查不到 | 可以把失敗原因寫到檔案 | 2.55 | 中：查 target 問題比較快 |
| 每台 Alertmanager 獨立 queue | 2.51 只有一條 queue，每批要等所有 Alertmanager 回應，一台慢會拖累全部 | 每台各自送，互不影響 | 3.10 | 依情況：多台 Alertmanager 時有幫助 |
| Rule 並行的依賴判斷 | 無法判斷依賴時仍然併行，順序可能錯 | 無法判斷時退回依序執行 | 3.1 | 依情況：只有開 `concurrent-rule-eval` 才相關 |
| `retention.percentage` | 只能用固定大小，PVC 擴容後要跟著改 | 可以設成磁碟的百分比 | 3.11 | 中：較不容易忘記留空間 |
| 新的 rule metric | 看不到恢復花多久、group 內是否真的併行 | `rule_group_last_restore_duration_seconds`（2.53）、`rule_group_last_rule_duration_sum_seconds`（3.1） | 2.53、3.1 | 低：方便觀察 |
| LTS 支援 | 2.51 不再修 bug，連 2.53 LTS 也已結束 | 3.13 LTS 支援到 2027-07-31 | 3.13 | 高：之後有問題才有修補可用 |
| `wal_corruptions_total` 修正 | Runtime 損毀時這個 metric 仍是 0 | Checkpoint 遇到損毀時也會計入 | 3.16 rc，3.13 沒有 | 中：升級前先看 checkpoint 失敗 |
| Reload ticker | Compaction 訊號不斷重置 timer，reload 被餓死 | 固定間隔，不會被重置 | 只在 main，尚無 tag | 中：要等之後的版本 |

版本依 changelog 與 repo tag 確認。實測過的只有 `query_offset`、2.51 的 `auto-gomaxprocs`，其餘多數項目只確認了原始碼或 changelog。

### TSDB 格式相容：官方怎麼說，實際上如何

**一句話：經過 2.55 是官方支援的安全路徑，但目前的格式差異比文件描述的小。**

每個 block 的 index 檔開頭記著格式版本。2.51 的讀取端只接受版本 1 和 2，看到其他版本就回報 `unknown index file version`；2.55 起多接受版本 3。官方遷移文件因此寫明：升到 3.x 後只能回退到 2.55。

但實際檢查發現，**目前沒有任何版本會寫出版本 3**：從 2.51.2、3.13.5 到 main，寫 index 時都是 `FormatV2`。2.55 只是預先讓讀取端看得懂，為未來的格式變更做準備。<span class="at-chip at-ev-code">讀碼</span>

實測跨版本切換，比對切換前後同一段時間的 sample 筆數：<span class="at-chip at-ev-tested">實測</span>[^exp-prom-upgrade]

| 切換 | 切換前 sample 數 | 切換後 sample 數 | WAL 損毀 | 警告或錯誤 log |
|---|---|---|---|---|
| 3.13.5 → 2.51.2（直接回退） | 21000 | 21000 | 0 | 無 |
| 3.13.5 → 2.55.1（官方回退） | 20500 | 20500 | 0 | 無 |
| 2.51.2 → 3.13.5（直接升級） | 20500 | 20500 | 0 | 無 |

500 條 gauge，資料寫入約 11 分鐘，正常關機後切換，每種切換各一次。沒有測 native histogram、大型 compaction block，也沒有測非正常關機後的切換。

**為什麼仍建議經過 2.55：**

- **這是官方支援的路徑。**實驗中直接回退沒有問題，但這不在官方保證內，未來的 3.x 版本也可能真的開始寫 V3，測試範圍也有限。如果真的要從 3.x 退回 2.51，建議先備份 TSDB。
- **把風險拆開。**2.51 → 2.55 主要獲得 `query_offset`，PromQL 行為不變；2.55 → 3.13 才有左開區間、正則等不相容變更。出問題時比較容易判斷原因。
- **2.55 本身有用。**可以先換成 `query_offset`，不必同時面對 3.x 的變更。

### Alert 恢復只查一次：什麼時候才有差

**一句話：Prometheus 重啟時要把 `for` 計時接回去，2.53 把這一步的查詢大幅減少。**

假設 `for: 1h` 的 alert 已經 pending 50 分鐘，這時 Prometheus 重啟。如果不恢復，計時會從零開始。所以重啟後它會讀 `ALERTS_FOR_STATE` 把 `activeAt` 接回去。

| 版本 | 一條 rule 有 120 個 alert 實例時 |
|---|---|
| 2.51 | 每個實例查一次，共 120 次幾乎相同的查詢 |
| 2.53 起 | 查 1 次拿到所有 series，再用 label 比對。上游提到啟動時記憶體可能略增 |

這一步在 rule group 自己的 goroutine 裡同步執行，完成前 alert 不會寫入 `ALERTS`，group 也不會進入下一輪。所以實例越多，重啟後這個 group 恢復得越慢。<span class="at-chip at-ev-code">讀碼</span>

**實際影響要先看兩件事：**

- **`for` 小於 10 分鐘的 alert 根本不會恢復。**`--rules.alert.for-grace-period` 預設 10m，`for` 比它短就直接跳過，重啟後從零計時。`for: 3m` 或 `5m` 的 alert 不受這項改善影響。<span class="at-chip at-ev-code">讀碼</span>[^p-forgrace]
- **實例數夠多才有感。**用 `count by (alertname) (ALERTS_FOR_STATE)` 查每條 alert 的實例數。

**實測：差異其實很小。**同一條 alert 有 3 萬個實例、`for: 30m`，重啟後量恢復花多久。<span class="at-chip at-ev-tested">實測</span>[^exp-prom-upgrade]

| 版本 | 恢復 3 萬個實例 | 恢復後 alert 數 |
|---|---|---|
| 2.51.2 | 1.5s | 30000 |
| 2.55.1 | 0.9s | 30000 |
| 3.13.5 | 0.7s | 30000 |

各量一次，時間取自 debug log 的第一筆和最後一筆，寫 log 本身也佔時間。Head 只有約 2 分鐘資料；TSDB 更大、負載更重的環境，實際時間可能更長。

**結論：3 萬個實例也只差不到 1 秒，不要把這項當成升級理由。**重啟時更該在意的是 replay 期間 rule 不會執行，以及 `for` 小於 10 分鐘的 alert 會從零計時。

### WAL 半筆寫入：升級也修不好

一批資料太大時，WAL 會把一筆紀錄拆到多個 32KB 的 page。寫到一半失敗時，剩下的部分被丟掉，下一筆資料卻照常寫入：

```text
正常：[first][middle][last][full]
半筆：[first][middle]      [full]   ← last 不見了，直接接到下一筆
```

| 階段 | 結果 |
|---|---|
| 寫入當下 | 只有一次寫入錯誤；空間恢復後寫入恢復正常，看起來沒事 |
| 下一次 checkpoint | 讀到壞 segment 而失敗，WAL 無法截斷，持續長大 |
| 重啟 | 自動修復，但損毀點之後的資料會遺失 |

2.51.2、2.55.1、3.13.5、3.15.1 真實磁碟滿都發生，main 的程式碼也還留著這個 TODO。<span class="at-chip at-ev-tested">實測</span>[^exp-prom-storage]升級後仍要監控 `prometheus_tsdb_checkpoint_creations_failed_total`，並預留磁碟空間。

### Reload timer：修的是什麼

Prometheus 每分鐘要 reload 一次 block，**retention 也是在這時候執行**。舊寫法每一輪都重新起算這 1 分鐘：[^p-db-run]

```text
舊寫法：等 1 分鐘 → 中途收到 compaction 訊號 → 處理完重新等 1 分鐘 → ...（可能永遠等不到）
新寫法：固定每 1 分鐘觸發，不受訊號影響
```

- **好處：**Retention 和 head chunk 的 mmap 都能按時執行，後者是這個 PR 原本要解決的記憶體堆積問題。
- **舊寫法確實會出事：**Compaction 用完磁碟時，2.51.2 和 3.13.5 的 reload 都歸零。<span class="at-chip at-ev-tested">實測</span>[^exp-prom-storage]
- **限制：**已合併到 main，但截至 2026-11 的 tag 都還沒有，3.13.5 和 3.16 rc 也沒有。修正版本身沒有實測。磁碟完全寫滿時 reload 本來就正常，這個修正幫不上忙。
- **現在怎麼辦：**監控 `prometheus_tsdb_compactions_failed_total` 和 `prometheus_tsdb_reloads_total`，發現後釋放空間即可恢復。

### 升級時要注意

- **回退限制：**官方文件寫明升到 3.x 後只能回退到 2.55，建議把 2.55 當成中繼站。實際格式差異見上方說明。
- **3.x 的 range 改成左開區間：**樣本少的視窗可能再少一個點。矩陣實驗中 3I 以上的設定在 2.51.2 和 3.13.5 都正常，但 R 剛好是 I 的整數倍時仍要留餘裕。<span class="at-chip at-ev-code">讀碼</span> <span class="at-chip at-ev-tested">實測</span>[^p255-range-closed][^p3-range-open][^exp-prom-range]
- **其他 3.x 不相容變更：**正則的 `.` 會比對換行、`holt_winters` 改名、scrape 協定更嚴格、部分 flag 移除、`le` 和 `quantile` 的 label 值格式調整。要對照[官方遷移文件](https://prometheus.io/docs/prometheus/latest/migration/)檢查 rule 和設定。
- **升級也要帶著監控：**先把第 8 節的規則上線，才能比較升級前後的邊際。

### 升級會變快嗎：相同負載的比較

三個版本同時跑相同負載（30 萬 series、每秒約 2 萬 sample、15 分鐘 block），量測 40 分鐘。**CPU、scrape 和 rule 耗時都沒有明顯差異，只有記憶體略低。**<span class="at-chip at-ev-tested">實測</span>[^exp-prom-upgrade]

| 指標（平均） | 2.51.2 | 2.55.1 | 3.13.5 |
|---|---|---|---|
| CPU（核） | 0.66 | 0.70 | 0.65 |
| RSS | 1.40GB | 1.25GB | 1.26GB |
| Working set | 1.25GB | 1.09GB | 1.11GB |
| 記憶體配置速率 | 67MB/s | 52MB/s | 53MB/s |
| 大 target scrape | 0.68s | 0.71s | 0.78s |
| 重 rule group | 6.5s | 6.9s | 6.8s |
| WAL 大小 | 316MB | 317MB | 316MB |
| 結束時磁碟 | 670MB | 685MB | 665MB |
| 非正常重啟的 replay | 6.1s | 4.7s | 6.0s |

每個版本只跑一次，同時期機器上還有其他實驗，幾個百分點內的差距不能當成版本差異。沒有限制 CPU，也沒有開 native histogram。

- **不要期待升級解決 scrape 或 rule 太慢。**在這個規模下，scrape 與 rule 的耗時都相近，慢 scrape 和慢 group 要靠降低 series、拆 target 和拆 group 處理。
- **2.55 以上記憶體約少 10%。**這個規模差不到 200MB。大型環境的差異比例不一定相同，要在升級時實際量測。
- **升級的主要理由是功能和支援，不是效能：**`query_offset`、LTS 修補、預設對齊 `GOMAXPROCS` 與 `GOMEMLIMIT`。

## 10. 建議順序

1. **先量：**用 `topk(10, scrape_duration_seconds)` 和 `scrape_samples_scraped` 找出慢的 target，並看 `/api/v1/status/runtimeinfo` 與 throttling。
2. **先救關鍵 alert：**挑最重要的 rule，offset 設成不小於尖峰 d，range 依第 5 節公式拉長，並用 `changes(ALERTS_FOR_STATE)` 確認不再重置。
3. **上線監控：**部署外部 dead man's switch、canary 和第 8 節規則。
4. **處理根因：**降低大 target 的 series、拆 target 或分片，把 d 降到 interval 以下；拆最慢的 rule group。
5. **升級：**2.55.1 → 3.13 LTS，每一步都比較邊際指標。
6. **收斂：**d 穩定後，把 offset 和 range 調回較短，縮短偵測延遲。

## 限制與來源

**可重現：**實驗程式、結果與自檢 skill 都在 [prometheus-selfcheck-skills](https://github.com/HahaSula/prometheus-selfcheck-skills)（`EVIDENCE.md` 與 `lab/`）。

**實驗限制：**單機 Docker，受測規模 30 萬 series、每秒約 2 萬 sample，CPU 和記憶體遠小於一般正式環境。實驗證明的是機制和訊號順序，門檻與時間不能直接套用。第一批實驗有多個情境同時執行，CPU 掃描另外檢查過機器負載。

相關頁面：[Alert Pending 抖動分析](pending-jitter.md)、[Alertmanager 監控指南](alertmanager.md)、[VictoriaMetrics Rule 信任邊際](victoriametrics-trust.md)。

- [Prometheus 版本週期與 LTS 期限](https://prometheus.io/docs/introduction/release-cycle/)
- [#19909 Alert 在 `up == 0` 時仍間歇重置（lookback 邊界）](https://github.com/prometheus/prometheus/issues/19909)
- [Discussion #11088 Metric 延遲導致 alert 被判定 recovered](https://github.com/prometheus/prometheus/discussions/11088)
- [#11807 Option to delay rule evaluations](https://github.com/prometheus/prometheus/issues/11807)
- [#13027 磁碟滿導致無法復原的 WAL 錯誤](https://github.com/prometheus/prometheus/issues/13027)
- [PR #20161 Reload ticker](https://github.com/prometheus/prometheus/pull/20161)
- [Grafana Labs 的 metamonitoring 架構](https://grafana.com/blog/2021/04/08/how-we-use-metamonitoring-prometheus-servers-to-monitor-all-other-prometheus-servers-at-grafana-labs/)
- [PromLabs：End-to-End Watchdog Alerts](https://training.promlabs.com/training/monitoring-and-debugging-prometheus/metrics-based-meta-monitoring/end-to-end-watchdog-alerts/)
- [Red Hat Bugzilla 1810111：WAL replay 記憶體尖峰](https://bugzilla.redhat.com/show_bug.cgi?id=1810111)
