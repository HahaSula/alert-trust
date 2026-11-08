# Policy 清單

嚴重度：**FAIL** 代表 alert 可能不會正確觸發或已有實質風險；**WARN** 代表餘裕不足；**INFO** 只供確認。

「依據」欄是目錄裡的契約（`$AT/catalog/contracts.yaml`）或訊號（`<card>#<signal>`）。式子、預設值與證據以目錄為準；這份清單只規定怎麼機械化地判斷。符號見 `$AT/docs/contracts.md` 的符號表。

## Scrape（prom、vmagent）

| Policy | 嚴重度 | 判斷 | 依據 |
|---|---|---|---|
| `SCRAPE_NOT_MONITORED` | FAIL | 每個 scrape job 都要有 `up == 0` 與 `absent(up{job=...})` 的規則（任何 ruler 上） | `prometheus#job-freshness`、`vmagent#job-freshness`、target-pattern `exporter-up` |
| `SCRAPE_JOB_NO_TARGETS` | WARN | 設定的 scrape pool 要有 active target | `vmagent#scrape-health` |
| `SCRAPE_INTERVAL_TOO_SHORT` | FAIL／WARN | 尖峰 `d ≤ S` 為 FAIL 門檻，`d ≤ scrape_ratio_warn × S` 為 WARN 門檻 | `scrape-within-interval` |
| `SCRAPE_TIMEOUT_TOO_CLOSE` | WARN | 尖峰 `d < 0.8 × scrape_timeout` | `exporter-deadline` |
| `SCRAPE_TARGET_TOO_LARGE` | WARN | 單一 target 的 sample 數低於 `large_target_samples` | `prometheus#scrape-ratio` |
| `SCRAPE_NO_SAMPLE_LIMIT` | INFO | 設定 `sample_limit` | — |
| `SCRAPE_UNUSED_BY_ALERTS` | INFO | 沒有任何 alert 用到的 job（`metric_jobs.json`）是否符合預期 | — |

## Rule group（prom、vmalert）

| Policy | 嚴重度 | 判斷 | 依據 |
|---|---|---|---|
| `GROUP_INTERVAL_TOO_SHORT` | FAIL／WARN | 尖峰 `T ≤ E`；`T ≤ group_ratio_warn × E` | `group-within-interval` |
| `GROUP_EVAL_FAILING` | FAIL | 查詢範圍內沒有評估失敗（prom `group_failures`；vmalert `vmalert_rule_errors`） | `prometheus#rule-failures`、`vmalert#rule-errors` |
| `GROUP_MISSED_ZERO_BUT_ERRORS` | WARN | vmalert：`iteration_missed` 為 0 但錯誤比率大於 0（查詢飽和時 missed 不動） | `vmalert#group-ratio` |
| `RULE_UNHEALTHY` | FAIL | 每條 rule 的 `health` 為 ok，附 `lastError` | `vmalert#rules-loaded` |
| `GROUP_CONCURRENCY_WITH_DEPENDENCY` | WARN | vmalert `concurrency > 1` 的 group 裡有 alert 依賴同 group 的 recording rule | `$AT/docs/architecture/vmalert.md#group` |

## Alert（prom）

| Policy | 嚴重度 | 判斷 | 依據 |
|---|---|---|---|
| `RANGE_TOO_SHORT` | FAIL | 沒有 offset：`R ≥ 2I + d`；`O ≥ d`：`R ≥ 2I` | `range-window` |
| `RANGE_LOW_MARGIN` | WARN | 再容忍 k 次 scrape 失敗：多加 `k × I`（k = `tolerate_missed_scrapes`） | `range-window` |
| `RANGE_SHORTER_THAN_INTERVAL` | WARN | `R ≥ E` | — |
| `OFFSET_MISSING` | WARN | scrape 耗時明顯時 `O ≥ d` | `range-window` |
| `LOOKBACK_TOO_SHORT` | FAIL | 沒有 range 的 selector：`I + d < lookback`（`flags.json` 的 `query.lookback-delta`，預設 5m） | `lookback` |
| `ALERT_FOR_RESETTING` | FAIL | `for_state_resets` 不超過 `for_reset_warn` | `prometheus#for-reset` |
| `FOR_NOT_RESTORED_ON_RESTART` | INFO | `F` 小於 `rules.alert.for-grace-period` 時重啟後從零計時 | — |

## Alert（vmalert）

| Policy | 嚴重度 | 判斷 | 依據 |
|---|---|---|---|
| `VM_RANGE_TOO_SHORT` | FAIL | `R ≥ I + L − D`；`D` 用 group `eval_delay` 或 flag `rule.evalDelay` | `vm-range-window` |
| `VM_LOOKBACK_STICKY` | INFO | 沒有 range 的條件型 alert：資料停止後仍會成立到 `datasource.queryStep`（預設 5m） | `lookback` |
| `LOG_RULE_DELAY` | FAIL／WARN | `type: vlogs` 的 group：`L > D + E` 為 FAIL；`D < L ≤ D + E` 為 WARN；沒有 `_time` 視窗時要說明 | `vl-log-delay` |
| `RESEND_DELAY_ZERO` | WARN | 業務 vmalert 的 `rule.resendDelay` 為 0（預設）：查詢中斷 `4 × E` 就假 resolved | `resend-endsat`、`vmalert#resolve-settings` |
| `WATCHDOG_WITH_RESEND_DELAY` | FAIL | Watchdog 所在的 vmalert 設了 `rule.resendDelay`，或和業務 rule 同一台 | `vmalert#watchdog` |
| `NOTIFIER_BLACKHOLE` | FAIL | `notifier.blackhole` 為 true | `vmalert#resolve-settings` |
| `PARTIAL_RESPONSE_ALLOWED` | WARN | VM cluster 且 RF=1，計數型「太低」規則的 group 沒有 `params: {deny_partial_response: ["1"]}` | `victoriametrics#partial-results` |
| `STATE_NOT_RESTORED` | WARN | 有 `for > 0` 的 rule，但 `remoteRead.url` 沒設 | `$AT/docs/architecture/vmalert.md#restore` |

## Alert（共通）

| Policy | 嚴重度 | 判斷 | 依據 |
|---|---|---|---|
| `SERIES_NOT_FOUND` | WARN | Alert 用到的 metric 應該存在（`metric_jobs.json` 為空）；錯誤計數器這類要人工判斷 | — |
| `EXPORTER_UP_ONLY` | FAIL | 只用 exporter 自己的 `*_up`（例如 `mysql_up`、`pg_up`）判斷存活，沒有 `up == 0` 加 `absent` | target-pattern `exporter-up` |
| `COLLECTOR_EQ_ZERO` | WARN | 用 `collector_success == 0` 判斷，沒有 `unless` 寫法涵蓋整組消失 | target-pattern `collectors-present` |
| `FOR_ZERO` | WARN | 除非刻意（例如 Watchdog），否則要有 `for` | — |
| `FOR_TOO_SHORT` | WARN | `F ≥ 2I` | — |
| `CRITICAL_ALERT_IN_HEAVY_GROUP` | WARN | `critical.alerts` 或 `critical_severities` 的 alert 不放在重 group（耗時排名前段或含昂貴 recording rule） | `group-within-interval` |
| `RATE_ON_NON_COUNTER` | WARN | `rate`／`increase` 用在 counter（`metadata.json` 的 `type`） | — |
| `ALERT_MISSING_LABEL` | WARN | 必要 label（預設 `severity`） | — |
| `ALERT_MISSING_ANNOTATION` | INFO | 必要 annotation（預設 `summary`） | — |

## Alertmanager（am）

| Policy | 嚴重度 | 判斷 | 依據 |
|---|---|---|---|
| `AM_FAILURE_WINDOW_TOO_SHORT` | FAIL | 引用 `alertmanager_notifications_failed_total` 的規則視窗 `≥ 2 × 最大 group_interval` | `am-failure-window` |
| `WATCHDOG_NO_DEDICATED_ROUTE` | FAIL | `config.original` 有比對 Watchdog 的專屬路由，指向外部接收端 | `alertmanager#end-to-end` |
| `WATCHDOG_SUPPRESSED` | FAIL | Watchdog 被 `inhibit_rules` 或 active silence 比對到 | `alertmanager#end-to-end` |
| `DEADMAN_WAIT_TOO_SHORT` | WARN | DMS 等待 `> 4 × max(resend, E) + repeat_interval + group_interval` | `deadman-wait` |
| `INHIBIT_WITHOUT_EQUAL` | WARN | `inhibit_rules` 沒有 `equal` | — |
| `LONG_SILENCE` | INFO | active silence 的 `endsAt` 超過 `long_silence_days` | — |
| `AM_HA_PARTIAL_FANOUT` | WARN | `replicas > 1`，但 ruler 只送到其中一台（`alertmanagers.json`、vmalert `notifier.url`） | `alertmanager#cluster-health` |
