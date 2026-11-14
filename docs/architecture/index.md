# 內部架構總覽

大部分元件都是「對象之間並行，對象之內單序」。單序的那一段就是瓶頸：它一慢，排在後面的工作全部跟著慢，而且通常不會報錯。寫 rule 和設計 monitor of monitor 之前，先知道每個元件的單序點在哪裡。

各元件的細節在它自己的頁面，這頁只做對照。監控和監控 monitor 的規則各要放在哪一台，見[規則放在哪一台](placement.md)。

## 一條 alert 經過的階段

以 Prometheus 為例，一條 alert 從 scrape 到 Alertmanager 要經過六段，每一段都有自己讓 alert 失準的方式，而且大多不會報錯。

<!-- figure: alert-pipeline -->

**圖**　Alert 管線，以及每一段可能讓 alert 失準的方式。最常見的根因在第一段：scrape 耗時超過 `scrape_interval`，後面每一段都跟著出錯。[^p-scrape-run][^p-scrape-report][^p-alert-delete][^p-notifier-batch][^p-alert-validuntil]
{: .at-caption }

## 單序點對照

| 元件 | 並行的單位 | 單序的地方 | 卡住時影響誰 | 詳細 |
|---|---|---|---|---|
| Prometheus scrape | 每個 target 一條 goroutine | 同一個 target 的抓取、parse、commit[^p-scrape-run] | 只有該 target；超過 interval 時 tick 被丟掉 | [Prometheus](prometheus.md#scrape-loop) |
| Prometheus WAL | 全域一份 | 所有 commit 共用一把鎖[^p-wal-log] | 全部寫入 | [Prometheus](prometheus.md#wal) |
| Prometheus 查詢 | 全域 slot，預設 20 | Rule 與 API 搶同一組 slot[^p-engine-queue][^p-maxconc] | Rule 和 Grafana 一起排隊 | [Prometheus](prometheus.md#query-engine) |
| Prometheus rule group | 每個 group 一條 goroutine | Group 內的 rule 依序執行[^p-group-eval] | 同 group 的所有 alert | [Prometheus](prometheus.md#rule-group) |
| Prometheus notifier | 2.x 到 3.9 全域一條 queue；3.10 起每台 Alertmanager 一條 | 每批要等所有 Alertmanager 回應[^p-notifier-wait][^p3-notifier-queue] | 3.10 以前：一台慢就拖累全部通知 | [Prometheus](prometheus.md#notifier) |
| Prometheus TSDB 維護 | 全域一條 goroutine | Compaction、reload、retention 依序[^p-db-run] | 全部維護工作；retention 跟著延後或停止 | [Prometheus](prometheus.md#tsdb-maintenance) |
| vmalert group | 每個 group 一條 goroutine | Group 內的 rule 預設依序（`concurrency: 1`）[^vma-conc-default][^vma-execconc] | 同 group 的所有 alert | [vmalert](vmalert.md#group) |
| vmalert 送通知 | 每條 rule 同時送給所有 Alertmanager | **每條 rule 等所有 Alertmanager 回應才換下一條**[^vma-exec-send][^vma-notify-send] | Alertmanager 慢，同 group 的評估跟著慢 | [vmalert](vmalert.md#notify) |
| vmagent scrape | 每個 target 一條 goroutine | 同一個 target 依序；超過 interval 時 tick 被丟掉[^vmag-run][^vmag-ticker] | 只有該 target | [vmagent](vmagent.md#scrape) |
| vmagent remote write | 每個 URL 多條 worker（預設 2 × CPU） | 每個 URL 一個磁碟 buffer[^vmag-queues][^vmag-maxdisk] | 該 URL；buffer 滿時丟最舊資料 | [vmagent](vmagent.md#remote-write) |
| vmselect | 一次查詢同時問所有 vmstorage | 併發上限（預設 2 × CPU），排隊超過 10s 回 429[^vms-maxconc][^vms-queue] | Rule 與 dashboard 一起被拒 | [VictoriaMetrics](victoriametrics.md#vmselect) |
| Alertmanager alert store | 全域一把鎖 | 寫入時持鎖送進每個訂閱者的 channel[^am-mem-put][^am-mem-chan] | 全部接收；channel 滿了 POST 會阻塞 | [Alertmanager](alertmanager.md#alert-store) |
| Alertmanager aggregation group | 每個 group 一條 goroutine | 同 group 依 `group_interval` 依序 flush[^am-aggr-run] | 只有該 group；SMTP 不回應時整個 group 卡住 | [Alertmanager](alertmanager.md#aggregation-group) |
| mysqld_exporter | 每個 collector 一條 goroutine | 連線池上限 2（`--exporter.max_open_connections`）[^my-pool-default][^my-scrapers-go] | 該次 scrape 的所有 collector | [Exporter](exporters.md#mysqld-exporter) |
| postgres_exporter | 每個 collector 一條 goroutine | **只有一條連線**，collector 實際上排隊執行[^pg-maxopen][^pg-goroutines] | 一個慢查詢拖住所有 collector | [Exporter](exporters.md#postgres-exporter) |

依據是各元件的原始碼，版本見各頁。<span class="at-chip at-ev-code">讀碼</span>

## 跨元件的連鎖

單看一個元件時看不到這些，要沿著 alert 鏈看：

| 起點 | 經過 | 結果 | 證據 |
|---|---|---|---|
| Grafana 重查詢 | Prometheus 查詢 slot | Rule group 從幾秒變 40 秒，scrape 不受影響 | <span class="at-chip at-ev-tested">實測</span>[^exp-prom-query] |
| Grafana 重查詢 | vmselect 併發上限 | vmalert 查詢收到 429，走查詢失敗路徑；和 vmselect 停一樣，約 2 分鐘後 alert 會被標成假 resolved | <span class="at-chip at-ev-inferred">推論</span>[^vms-queue][^vma-exec-send]（vmselect 停的假 resolved 有實測[^exp-vm-faults]，查詢飽和沒有） |
| 一台 Alertmanager 變慢 | Prometheus 3.10 以前的單一 queue | 所有 alert 的通知一起延遲 | <span class="at-chip at-ev-code">讀碼</span>[^p-notifier-wait] |
| 一台 Alertmanager 變慢 | vmalert 每條 rule 同步送通知 | 同 group 的評估時間拉長，可能跳過 evaluation | <span class="at-chip at-ev-code">讀碼</span>[^vma-exec-send] |
| SMTP 不回應 | Alertmanager 的 aggregation group | 該 group 所有通知停止，counter 不動 | <span class="at-chip at-ev-tested">實測</span>[^exp-am-smtp] |
| 資料庫一個慢查詢 | postgres_exporter 的單一連線 | Scrape 超過 timeout，`up` 變 0，所有 `pg_*` 消失 | <span class="at-chip at-ev-tested">實測</span>[^exp-pg] |
| Scrape 變慢 | Prometheus rule 的查詢視窗 | `for` 反覆重置，alert 永遠不 firing，沒有錯誤 | <span class="at-chip at-ev-tested">實測</span>[^exp-prom-silent] |

最後一列是最常見、也最難發現的一條，對應的時間條件見[信任契約](../contracts.md)。
