---
name: prometheus
description: Prometheus pack of the alert-trust self-check. Reads the Prometheus card and contracts from the catalog and, from a collect.py snapshot, reports peaks, checks each alert's timing contract (R >= (2+k)I + d, offset, lookback, group T <= E, EndsAt), capacity margin (CPU, memory, disk, IOPS, query slots) and the upgrade path. Use for Prometheus or a Prometheus observer, alerts stuck in pending or never firing, rule review, capacity or upgrade questions. Prometheus 的盤點、rule 時間契約、資源邊際與升級路徑。
argument-hint: "[snapshot-dir] [component-id]"
---

# Prometheus pack

處理 env 檔裡每個 `type: prometheus` 的元件與觀察者。一次處理一個 id，從使用者指定的重要 alert 開始。

`$AT` 是 alert-trust 根目錄（plugin 安裝時 `${CLAUDE_PLUGIN_ROOT}`，在 repo 裡是 `${CLAUDE_SKILL_DIR}/../..`）。

## 全程規則

- **唯讀**：只用 HTTP GET 與快照。不呼叫 `/-/reload`、`/-/quit`、`/api/v1/admin/*`，不改 rule 檔，除非使用者明確要求。
- **隱私**：快照與報告含主機名與 label，留在本機；報告不重現密鑰。Token 只走環境變數。
- **數字標來源**：`量測`（快照檔與欄位）、`使用者說`、`推算`（公式與代入值）。目錄的門檻是實驗值。
- **引用證據**：每個結論附卡片訊號或契約連結、`evidence` 等級與 `refs` 註腳，格式見 `$AT/skills/selfcheck/references/evidence.md`。
- 每次最多問 3 題；用使用者的語言。

## 先讀（不要憑記憶）

| 檔案 | 用來 |
|---|---|
| `$AT/catalog/components/prometheus.yaml` | `silent_failures`、`contracts`、`signals`（tier、`signal`、`metrics`、`evaluate_on`、`rules`） |
| `$AT/catalog/contracts.yaml` 與 `$AT/docs/contracts.md` | 契約的式子、違反時的現象、證據；符號表在 contracts.md |
| `$AT/catalog/terms.yaml` | 每個 interval、耗時對應哪個設定與 metric |
| `$AT/docs/architecture/prometheus.md` | 需要解釋機制時 |
| `$AT/rules/prometheus/*.yml` | 補缺口時的範本；只挑需要的，改 job 名稱與門檻 |
| 觀察者 | 另讀 `$AT/catalog/components/observer.yaml`（`federation` 訊號：`up`、`scrape_duration_seconds`、`ALERTS_FOR_STATE` 不在 `/metrics`，要 `/federate`） |

## 快照裡要讀的檔案

快照結構見 `$AT/skills/selfcheck/references/snapshot.md`。這個 pack 讀 `<id>/` 下的：`buildinfo.json`、`flags.json`、`runtimeinfo.json`、`config.json`、`rules.json`、`targets.json`、`peaks.json`、`metric_jobs.json`、`metadata.json`、`alertmanagers.json`、`errors.json`。

沒有快照時，請使用者在 UI 查下面的查詢，一次一組：

1. **規模與版本**：版本、部署方式與 CPU／memory limit、`prometheus_tsdb_head_series`、`rate(prometheus_tsdb_head_samples_appended_total[5m])`。
2. **尖峰**（最重要）：`max by (job) (max_over_time(scrape_duration_seconds[7d]))`、`topk(10, max_over_time(prometheus_rule_group_last_duration_seconds[7d]))`、`sum by (rule_group) (increase(prometheus_rule_group_iterations_missed_total[7d])) > 0`，以及各 job 的 `scrape_interval`、各 group 的 `interval`。
3. **Rule**：rule 檔或 `/api/v1/rules`，至少最重要的幾條。

## 0. 先決條件（不能跳過）

進入下面任何一節之前，確認：

1. **監控體系已確認**：評估規則的是 Prometheus 還是 vmalert、版本、資料從哪裡來、有沒有外部觀察者。沒有 `env/<name>.yaml` 時先用 `env-profile`，或依 `rule-review` 第 1 步問清楚。
2. **基礎設施的覆蓋已檢查**：已經跑過 `check_coverage.py`（沒有 API 時先用 `tools/rules_to_snapshot.py` 把 rule 檔轉成快照），並先回報第 1 級缺口。

使用者只丟 rule 檔要你「檢查」時，改走 `rule-review` skill，不要直接從時間契約開始。

## 1. 盤點與尖峰

| 項目 | 從哪裡看 |
|---|---|
| 版本 | `buildinfo.json`；是否還在支援期見 [references/upgrade.md](references/upgrade.md) |
| 規模 | `peaks.json`: `head_series`、`samples_rate`、`scrape_samples_peak` |
| Scrape | 每個 job：`targets.json` 的 `scrapeInterval` 對 `peaks.json: scrape_duration_peak` |
| Rule group | `group_duration_peak` 對 `group_interval`、`group_missed`、`group_failures` |
| 異常計數 | `for_state_resets`、`checkpoint_failed`、`compactions_failed`、`wal_corruptions`、`wal_writes_failed`、`restarts`、`notify_errors`、`notify_dropped` |
| 資料來源 | `has_*`：自身 metric、Alertmanager、cAdvisor、kube-state-metrics、kubelet volume、node_exporter、blackbox |
| Go 設定 | `runtimeinfo.json` 的 `GOMAXPROCS`、`GOMEMLIMIT` 是否對齊 limit |

比值超過 1、異常計數大於 0 的標出來。提醒：尖峰只代表查詢範圍內抓到的值；`scrape_duration_seconds` 不含寫入時間，實際可見時間更晚。

單條 rule 的細節只在 API：`rules.json` 的 `health`、`lastError`、`evaluationTime`、`duration`（for）、`keepFiringFor`、group 的 `interval`、`queryOffset`（新版才有）。

## 2. Rule 時間契約

依卡片 `contracts:` 列出的每一條契約檢查（目前是 `scrape-within-interval`、`range-window`、`group-within-interval`、`lookback`、`resend-endsat`；以卡片為準）。式子與符號以 `$AT/docs/contracts.md` 為準，這裡只寫怎麼代入：

| 符號 | 從快照取 |
|---|---|
| `S` | `targets.json` 的 `scrapeInterval`（依 rule 用到的 job） |
| `d` | `peaks.json: scrape_duration_peak{job}` |
| `I` | `d ≤ S` 時是 `S`，否則約 `⌈d/S⌉ × S`（推算） |
| `E` | `rules.json` group 的 `interval`；`T` 是 `group_duration_peak` |
| `R`、`O`、`F` | 從 `rules.json` 的 `query` 讀 range 與 `offset`；group `queryOffset` 或 `config.json` 的 `rule_query_offset`；`duration` |
| rule 用到哪些 job | `metric_jobs.json` |
| `k` | 問使用者，建議至少 1 |

**Prometheus 的視窗契約：**沒有 offset 時 `R ≥ (2+k)·I + d`；`O ≥ d` 時 `R ≥ (2+k)·I`。違反時 `rate` 回傳空、pending 被刪、`for` 歸零，**不報錯**。逐條算邊際，附在報告的邊際總表。

**偵測延遲上限（推算）：**`I + max(O, d) + E' + T + F`，`E'` 是 `T > E` 時約 `⌈T/E⌉ × E`。拿它和使用者的通知時限（env 檔 `critical.notify_within`）比較，再加上 Alertmanager 的 `group_wait`／`group_interval`（`alertmanager` pack）。

要人工判斷、不要只看計算的情況：recording rule（上游延遲要往回追）、subquery、`on()`／`group_left` 複雜的表達式、`metric_jobs.json` 對不到 job 的 metric。報告前讀那條表達式。

重要結論（證據見卡片的 `silent_failures` 與 contracts.md）：

- 公式只保證不回傳空，`rate` 仍會被低估；門檻貼近實際值時還是會抖動。
- `R = 2I` 不夠：evaluation 落在 scrape 進行中時當次只剩 1 點。換成 `irate` 沒有用。
- 查詢失敗或 evaluation 被跳過時 pending **不會**被刪除；只有成功且結果沒有這個 alert 才刪。
- 事後查詢對不上 rule 當下的結果。

### 互動

1. 先報 FAIL 數與最嚴重的 3 條，優先看使用者提到的重要 alert。
2. 用 `peaks.json: for_state_resets` 或 `changes(ALERTS_FOR_STATE{alertname="X"}[1h])` 驗證：算出 FAIL 的 alert，`for` 真的有在重置嗎？
3. 問：尖峰是常態還是偶發？常態要修 scrape；偶發可以先用 offset 與 range 吸收。
4. 給修改前後的表達式與代價：offset 讓通知變慢；range 拉長會平滑短暫尖峰，查詢更貴。

### 修法

```yaml
# 2.53 以上：整個 group 設定
groups:
  - name: my-group
    query_offset: 3m
    rules:
      - alert: MyAlert
        expr: rate(my_metric_total{job="my-job"}[10m]) > 100
        for: 5m
# 2.53 以下：每個 selector 都加 offset（2.51 實測有效）
- alert: MyAlert
  expr: rate(my_metric_total{job="my-job"}[10m] offset 3m) > 100
  for: 5m
```

- 關鍵 alert 獨立成小 group，不要和昂貴的 recording rule 放一起（CPU 嚴重不足時重 group 變慢近一倍，還會漏掉短暫事件）。
- Alert 依賴 recording rule 時放同一個 group、順序在後。
- `for` 小於 `--rules.alert.for-grace-period`（預設 10m）的 alert，重啟後從零計時。

驗證：`changes(ALERTS_FOR_STATE{alertname="X"}[1h]) == 0`。

## 3. 資源邊際

見 [references/capacity.md](references/capacity.md)：CPU（轉折點是 group 耗時比值超過 1）、scrape 慢的分辨、記憶體與 replay 盲區、磁碟與 retention 卡住的兩條路徑、WAL 半筆寫入、IOPS、查詢 slot。數字從 `peaks.json`、`runtimeinfo.json`、`flags.json` 取。

## 4. 升級路徑

見 [references/upgrade.md](references/upgrade.md)。先查官方版本週期確認目前的 LTS；**不要等升級才修 rule**。

## 5. 覆蓋缺口（第 0 步已先做；這裡是 Prometheus 卡的細節）

`check_coverage.py` 的結果裡，`card: prometheus` 的每一列：

- `self_only`：卡片的 `evaluate_on` 是 `observer` 或 `federate`，規則卻在這台自己身上。建議搬到觀察者（env 檔 `observers`），需要的 series 用 `/federate` 拉。
- `rule_no_data`：多半是 job 名稱不符（上游規則依 job 名稱比對，名稱不符時照常載入、永遠回傳空）。用 `targets.json` 的實際 job 名稱改寫。
- `missing`：從卡片訊號的 `rules:` 指到的範本挑，換 job 名稱與門檻，請使用者用 `promtool check rules` 驗證。

把結果交回 `selfcheck`，或接著用 `policy-lint`。
