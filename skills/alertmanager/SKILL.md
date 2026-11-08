---
name: alertmanager
description: Alertmanager pack of the alert-trust self-check. Reads the Alertmanager card and contracts from the catalog and, from a collect.py snapshot (/api/v2/status, alerts, silences, /metrics), checks routing, the dead man's switch route and wait time, failure-alert windows that follow group_interval, HA and cluster consistency, and delivery failures that health endpoints hide. Use for notification path review, Watchdog/DMS questions, or alerts that fire but nobody receives. Alertmanager 路由、dead man's switch、失敗告警視窗、HA 與送達失敗。
argument-hint: "[snapshot-dir] [component-id]"
---

# Alertmanager pack

處理 env 檔裡每個 `type: alertmanager` 的元件（HA 時每台一個 id）。所有 ruler 共用它，**它一壞全部通知一起斷**，而它的 `/-/healthy`、`/-/ready` 是寫死回 200 的。

`$AT` 是 alert-trust 根目錄（plugin 安裝時 `${CLAUDE_PLUGIN_ROOT}`，在 repo 裡是 `${CLAUDE_SKILL_DIR}/../..`）。

## 全程規則

- **唯讀**：只用 HTTP GET。不建立、修改或刪除 silence，不 POST alert，不呼叫 `/-/reload`。測試 DMS 會影響線上，只寫成步驟，由使用者決定並自己執行。
- **隱私**：`config.original` 可能含收件人、webhook 路徑、團隊名稱。**報告裡不重現 receiver 的 URL、token、密碼、email 位址**，用 receiver 名稱指稱。使用者貼 `alertmanager.yml` 時請先遮蔽密鑰。
- **數字標來源**：`量測`（快照檔與欄位）、`使用者說`、`推算`（公式與代入值）。
- **引用證據**：附卡片訊號或契約連結、`evidence` 等級與 `refs` 註腳，格式見 `$AT/skills/selfcheck/references/evidence.md`。
- 每次最多問 3 題；用使用者的語言。

## 先讀（不要憑記憶）

| 檔案 | 用來 |
|---|---|
| `$AT/catalog/components/alertmanager.yaml` | `silent_failures`（例如 0.27.0 的 SMTP 誤記成功、SMTP 不回應整個 group 卡住）、`contracts`、`signals`（`applies_when: replicas > 1`） |
| `$AT/catalog/components/observer.yaml` | `separate-alert-path`、`own-deadman`、`deadman-wait`：觀察者的告警不能走被監控的這台 |
| `$AT/catalog/contracts.yaml` 與 `$AT/docs/contracts.md` | `am-failure-window`、`deadman-wait`、`resend-endsat` |
| `$AT/docs/architecture/alertmanager.md` | aggregation group、flush、重試的機制 |
| `$AT/rules/alertmanager/meta.yml` | 給觀察者的規則範本；`[2 × group_interval]` 的視窗要換成實際值 |

## 快照裡要讀的檔案

結構見 `$AT/skills/selfcheck/references/snapshot.md`。`<id>/status.json`（`config.original`、`cluster.status`、`cluster.peers`、`versionInfo`）、`alerts.json`、`silences.json`、`metrics.txt`、`buildinfo.json`、`errors.json`；ruler 端的 `prom-*/alertmanagers.json`、`prom-*/peaks.json`（`notify_*`、`am_*`）、vmalert 的 `flags.json`（`notifier.url`）。沒有快照時請使用者提供遮蔽過的 `alertmanager.yml` 與 `/api/v2/status` 的 `cluster` 段。

## 1. 路由與設定

解析 `config.original`（YAML）：

| 檢查 | 怎麼看 | 為什麼 |
|---|---|---|
| Watchdog 有專屬路由 | `route.routes` 有比對 `alertname="Watchdog"` 的項目，指向外部 DMS，且在一般路由之前或 `continue` 正確 | 否則 heartbeat 進一般頻道或根本沒送到外部 |
| Watchdog 的 `group_interval`／`repeat_interval` 夠短 | 明顯小於 DMS 的等待時間 | 否則 DMS 誤報 |
| Watchdog 不會被壓掉 | `inhibit_rules` 與 `silences.json` 裡比對到它的項目 | 被壓掉就等於沒有 DMS |
| 每個 ruler 的 Watchdog 分得出來 | Watchdog 帶 ruler 識別 label，DMS 端一個 ruler 一個 check | 共用 Alertmanager 時要分得出是哪一條斷了 |
| 預設 receiver 有人看 | 頂層 `route.receiver` | 沒比對到的 alert 都去這裡 |
| `group_by` | `[...]` 或過少 label | 被併進大 group，或通知風暴 |
| `group_wait` + `group_interval` | 加進 `prometheus`／`victoriametrics` pack 算的偵測延遲上限 | 通知的額外延遲 |
| `inhibit_rules` 的 `equal` | 少了 `equal` 時範圍太大 | 壓掉不該壓的 alert |
| 長期 silence | `silences.json` 裡 `endsAt` 很遠、`status.state` 是 active 的 | 可能忘了移除 |

## 2. 失敗告警的視窗（`am-failure-window`）

失敗要等整輪 flush 結束才計數。所以觀察者上的送達失敗規則，視窗要 `≥ 2 × 最大的 group_interval`：

1. 從 `config.original` 找出所有路由（含繼承）裡最大的 `group_interval`（量測）。
2. 找出觀察者上引用 `alertmanager_notifications_failed_total` 的規則（觀察者的 `rules.json`），讀它的 range 與 `for`。
3. 視窗小於 `2 × 最大 group_interval` 就是 FAIL：`group_interval` 1h 時，`rate(...[5m]) > 0` 加 `for: 5m` 完全抓不到單一 group 的持續失敗。上游 mixin 用 `rate[15m]`，同樣會漏。
4. 建議寫法見卡片 `delivery-failing` 訊號與 `rules/alertmanager/meta.yml#AlertmanagerNotificationsFailing`；排除 `reason="contextCanceled"`。

## 3. 送達失敗：健康檢查看不到

- 接收端停機時通知全斷，但 `/-/healthy`、`/-/ready`、`/api/v2/status` 全部成功，Prometheus 的 `prometheus_notifications_errors_total` 也是 0；只有 `alertmanager_notifications_failed_total` 增加（實測）。
- 從 `metrics.txt` 讀 `alertmanager_notifications_failed_total`、`alertmanager_notification_requests_failed_total`（重試中的早期訊號）、`alertmanager_notifications_total`，依 `integration` 分組。
- 0.27.0 的 SMTP 在 DATA 結尾拒收會記成成功（0.28 修正）；SMTP 不回應時整個 group 卡住、counter 不動。版本從 `buildinfo.json` 取。用 email 的關鍵通知，建議 canary 信箱加外部檢查（卡片 `end-to-end`）。

## 4. HA

適用條件：env 檔 `replicas > 1`，或 `status.json` 的 `cluster.peers` 多於 1。

- `cluster.status` 應為 `ready`，`peers` 數等於台數；`alertmanager_cluster_failed_peers`、`alertmanager_cluster_members`、`alertmanager_config_hash` 各台一致（`metrics.txt`，每台各讀一次）。
- 每個 ruler 都要送給每一台，不要透過 load balancer 只送一台：比對 `prom-*/alertmanagers.json` 的 `activeAlertmanagers` 數量、vmalert 的 `notifier.url` 數量與實際台數。
- Prometheus 3.10 以前的 notifier 只有一條 queue，一台慢拖累全部（讀碼）；vmalert 每條 rule 同步等所有 Alertmanager，一台慢會拉長 group 耗時（見 `victoriametrics` pack）。

## 5. Dead man's switch 的時間（`deadman-wait`）

| 項目 | 公式 | 代入 |
|---|---|---|
| Alert 到期 | `EndsAt = 評估時間 + 4 × max(resend, E)` | Prometheus `rules.alert.resend-delay`（`flags.json`，預設 1m）；vmalert `rule.resendDelay`（預設 0） |
| DMS 等待下限 | `> 4 × max(resend, E) + repeat_interval + group_interval` | Watchdog 路由的值；env 檔 `deadman.max_silence`（使用者說） |

等待太短會誤報；另一方面 ruler 停機後 Alertmanager 還會重送 Watchdog 到 `EndsAt`，所以 DMS 最快也要這段時間才響。要更快發現 ruler 停機，靠外部觀察者的 `up`。

## 訪談（最多 3 題一組，已知的跳過）

1. DMS 是什麼服務、沒收到多久會通知誰？上一次實際測試是什麼時候？（建議在維護時段暫停 Watchdog 路由驗證一次，**由使用者執行**。）
2. 觀察者自己的告警走哪台 Alertmanager？（走被監控的這台時，它掛了沒人知道。）
3. Alertmanager 有幾台、跨節點或跨可用區嗎？

## 回報

列出通過與未通過；未通過的附修改範例。Watchdog 路由範例：

```yaml
route:
  routes:
    - matchers: [alertname="Watchdog"]
      receiver: deadmans-switch
      group_wait: 0s
      group_interval: 1m
      repeat_interval: 1m
receivers:
  - name: deadmans-switch
    webhook_configs:
      - url_file: /etc/alertmanager/secrets/deadman-url
        send_resolved: false
```

`check_coverage.py` 結果裡 `card: alertmanager` 的 `self_only`（規則在只靠這台送通知的 ruler 上）、`missing` 依第 1 到 4 節補上，然後交回 `selfcheck`。
