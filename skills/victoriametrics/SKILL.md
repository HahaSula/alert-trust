---
name: victoriametrics
description: VictoriaMetrics pack of the alert-trust self-check, covering vmagent, VictoriaMetrics single and cluster, VictoriaLogs and vmalert. Reads their cards and contracts from the catalog and, from a collect.py snapshot, checks the vmalert window contract R >= I + L - D, log rule delay L <= D, lookback (queryStep), group duration, partial responses, resendDelay and fake resolved, read-only storage and flags (via /flags and the flag metric). Use for vmalert alerts that never fire or resolve falsely, VM cluster partial results, log alert gaps. vmalert／VM／vmagent／VictoriaLogs 的契約與盲點。
argument-hint: "[snapshot-dir] [component-id]"
---

# VictoriaMetrics pack

處理 env 檔裡 `type` 是 `vmalert`、`victoriametrics`、`vmagent`、`victorialogs` 的元件。VM 系列每段各是一個元件，**每條網路路徑都可能安靜地失效**，所以先畫出 env 檔裡的路徑：vmagent → VM（寫入）、vmalert → VM／VL（查詢）、vmalert → VM（狀態寫回）、vmalert → Alertmanager（通知）。

`$AT` 是 alert-trust 根目錄（plugin 安裝時 `${CLAUDE_PLUGIN_ROOT}`，在 repo 裡是 `${CLAUDE_SKILL_DIR}/../..`）。

## 全程規則

- **唯讀**：只用 HTTP GET 與快照。不呼叫 vmalert 的 `/-/reload`、VM 的 `/api/v1/admin/*`、`/internal/*`、`/snapshot/*`、`/api/v1/import*`，不寫入任何資料，除非使用者明確要求。
- **隱私**：快照與報告含主機名與 label，留在本機。VM 會把 URL 類 flag 遮成 `secret`，不要請使用者補貼原值；token 只走環境變數。
- **數字標來源**：`量測`（快照檔與欄位）、`使用者說`、`推算`（公式與代入值）。
- **引用證據**：附卡片訊號或契約連結、`evidence` 等級與 `refs` 註腳，格式見 `$AT/skills/selfcheck/references/evidence.md`。
- 每次最多問 3 題；用使用者的語言。

## 先讀（不要憑記憶）

| 檔案 | 用來 |
|---|---|
| `$AT/catalog/components/{vmalert,victoriametrics,vmagent,victorialogs}.yaml` | 只讀 env 檔裡有的。`silent_failures`、`contracts`、`signals`（`applies_when: cluster` 只在 cluster 適用） |
| `$AT/catalog/contracts.yaml` 與 `$AT/docs/contracts.md` | 「兩個體系都適用嗎」那張表：哪些契約和 Prometheus 不同 |
| `$AT/catalog/terms.yaml` | `eval_delay`、`eval_alignment`、`-datasource.queryStep`、`-rule.resendDelay` 等設定的位置與預設值 |
| `$AT/docs/architecture/{vmalert,victoriametrics,vmagent,victorialogs}.md` | 查詢時間、送通知、寫回、重啟還原、partial response 的機制 |
| `$AT/rules/victoriametrics/*.yml` | `meta.yml`（給觀察者）、`canary.yml`（Watchdog）、`canary-logs.yml`（log 流 heartbeat） |

## 快照裡要讀的檔案

結構見 `$AT/skills/selfcheck/references/snapshot.md`。

| kind | 檔案 |
|---|---|
| vmalert | `rules.json`（group 的 `type`、`interval`、`concurrency`，以及有設定才出現的 `eval_offset`、`eval_delay`、`params`；rule 的 `query`、`duration`、`health`、`lastError`、`lastSamples`）、`flags.json`、`alerts.json`、`metrics.txt` |
| vm | `flags.json`、`metrics.txt`、`peaks.json`（`vmalert_*`、`partial_results`、`read_only_max`、`vmstorage_unreachable_min`、`select_limit_timeouts`、`rows_*`、`free_disk_min`、`vmagent_*`、`has_*`）、`metric_jobs.json` |
| vmagent、vlogs | `flags.json`、`metrics.txt` |

**Flag 的讀法：**`/flags` 只列出明確設定的 flag，完整的生效值要靠 `flag` metric；`flags.json` 的 `data` 已合併兩者，`set` 列出明確設定的 flag（`flag{is_set="true"}`）。沒在 `set` 裡就是預設值，報告寫「預設」。沒有快照時，請使用者打開 `<url>/flags`，或查 `flag{name="rule.resendDelay"}`。

VM 的 `peaks.json` 只有在各元件的 `/metrics` 有被抓進這個 VM 時才有值；`has_vmalert_metrics` 是空的就代表 vmalert 沒被監控，這本身就是缺口（`vmalert#vmalert-down`）。

## 0. 先決條件（不能跳過）

進入下面任何一節之前，確認：

1. **監控體系已確認**：評估規則的是 Prometheus 還是 vmalert、版本、資料從哪裡來、有沒有外部觀察者。沒有 `env/<name>.yaml` 時先用 `env-profile`，或依 `rule-review` 第 1 步問清楚。
2. **基礎設施的覆蓋已檢查**：已經跑過 `check_coverage.py`（沒有 API 時先用 `tools/rules_to_snapshot.py` 把 rule 檔轉成快照），並先回報第 1 級缺口。

使用者只丟 rule 檔要你「檢查」時，改走 `rule-review` skill，不要直接從時間契約開始。

## 1. vmalert 視窗契約 `R ≥ I + L − D`

逐條 alerting rule（只看 `type` 是 `prometheus` 的 group）：

| 符號 | 從哪裡取 |
|---|---|
| `R` | `rules.json` 的 `query` 裡的 range |
| `I` | 實際的資料間隔：vmagent 的 `scrape_interval` 與尖峰 `scrape_duration_seconds`（`peaks.json: scrape_duration_peak`），`d > S` 時約 `⌈d/S⌉ × S` |
| `L` | 資料從時間戳到查得到的延遲尖峰：`max_over_time((time() - timestamp(m))[7d:1m])` 這類查詢（請使用者在 VM 上跑，或你用 GET 查），包含 vmagent 的 remote write 延遲 |
| `D` | `eval_delay`：group 的設定，否則 flag `rule.evalDelay`（預設 30s）。`eval_alignment` 會讓實際值落在 `D` 到 `D + E`；代入時用最小值 `D`（推論：取整只會讓查詢更舊） |

違反時查詢回空、`for` 永遠歸零，不報錯。這條是偏保守的設計值（120 組合實驗中失敗的格子都違反它，見契約 `vm-range-window` 的 refs）。另外提醒：vmselect 的 `-search.latencyOffset`（預設 30s）會把比它新的查詢時間改成 `now - 30s`，所以只把 `eval_delay` 調小拿不到更新的資料。

**沒有 range 的查詢**（`up == 0` 這類）風險方向和 Prometheus 相反：資料停了以後，`-datasource.queryStep`（預設 5m）內條件**還會繼續成立**約 6 分鐘，alert 不 resolve（契約 `lookback`）。

## 2. Log rule `L ≤ D`

> vmagent 的 `scrape_timeout` 大於 `scrape_interval` 時會**自動截成 S、不報錯**（Prometheus 會拒絕載入）；多台 vmagent 時 `-dedup.minScrapeInterval` 必須等於 `scrape_interval`，`-dedup.minScrapeInterval` 大於某個 job 的 S 時，那個 job 的實際間隔變成 dedup 的值，算視窗要用它（`fit_contracts.py --dedup-interval`）。見 `$AT/docs/tools/baseline.md`。
> 不合格時不要只列 FAIL：用 `$AT/tools/fit_contracts.py` 給出「目前規格哪裡不合 → 目前合格的 rule → 方案 A 改 rule／方案 B 改環境」，格式見 `$AT/skills/rule-review/SKILL.md` 第 4 步。

`rules.json` 裡 `type: vlogs` 的 group：

- 沒寫 `_time` 的 log rule 只查 `[ts − interval, ts]`，視窗不重疊。`L ≤ D` 保證涵蓋；`L > D + E` 必定漏掉，事後卻查得到（契約 `vl-log-delay`）。
- `L` 是 log 的端到端延遲：canary log 流（`rules/victoriametrics/canary-logs.yml`）的延遲尖峰；沒有 canary 就問使用者 shipper 的批次與重試設定，標「使用者說」或「未知」。
- 關鍵 log rule 改用較長的 `_time` 視窗；代價是事件結束後要等整段視窗才 resolve。
- VM 與 VL 互相收對方的資料時可能一起失明（`$AT/docs/model.md` 的「依 infra 組合選卡片」）。

## 3. Group 跑得完嗎 `T ≤ E`

- `T`：`peaks.json: vmalert_iteration_duration_peak`（或 `metrics.txt` 的 `vmalert_iteration_duration_seconds`），`E`：group 的 `interval`，預設 flag `evaluationInterval`。預警門檻 0.8。
- 查詢飽和（429）時 `vmalert_iteration_missed_total` 可能一直是 0，要看 `vmalert_alerting_rules_errors_total` 的比率與 VM 的 `select_limit_timeouts`。
- 每條 rule 同步等所有 Alertmanager 回應（`-notifier.sendTimeout` 預設 10s），Alertmanager 變慢也會拉長 `T`。
- `concurrency > 1` 時 rule 之間不保證順序；alert 依賴同 group 的 recording rule 時不要調高。

## 4. 假 resolved 與 Watchdog（`resend-endsat`）

- `EndsAt = 4 × max(E, -rule.resendDelay)`。`resendDelay` 預設 0，`E` = 30s 時只有 2 分鐘寬限：**查詢失敗超過 2 分鐘，Alertmanager 就把所有 alert 標成 resolved，連 Watchdog 也一樣**。
- 讀 `flags.json`：`rule.resendDelay`、`rule.maxResolveDuration`、`notifier.blackhole`（設了就根本沒送）。
- 判斷：業務 vmalert 把 `resendDelay` 設成大於預期的查詢中斷（lab：`-rule.resendDelay=3m` 時 vmselect 停 240 秒，Alertmanager 沒有任何 alert 消失；預設值在 +101s 全部假 resolved；見 `lab/vm/README.md`）；**Watchdog 放在不設 `resendDelay` 的獨立 vmalert**（env 檔 `role: watchdog`）。兩者在同一台時，Watchdog 會被延後才斷。
- DMS 等待時間要大於 `4 × max(resend, E) + repeat_interval + group_interval`（`alertmanager` pack 一起算）。

## 5. 寫入與儲存

| 問題 | 看什麼 | 為什麼 |
|---|---|---|
| Partial response（cluster） | `peaks.json: partial_results`、`vmstorage_unreachable_min`；group 的 `params` 有沒有 `deny_partial_response=1` | 預設回 HTTP 200 的部分結果；RF=1 掉一台時 `increase` 類「太低」規則假觸發。官方文件寫的 `denyPartialResponse` 沒有作用（實測） |
| RF | env 檔 `replication_factor`、flag `replicationFactor` | RF=2 掉一台不受影響；建議 vmstorage 至少 3 台 |
| 唯讀 | `read_only_max`、`vl_read_only_max`、`free_disk_min` 對 flag `storage.minFreeDiskSpaceBytes` | 全部唯讀時寫不進去，rule 不報錯，只看到資料變舊；條件 alert 還會 firing 約 6 分鐘 |
| 寫入路徑 | `rows_ignored`、`rows_dropped_on_overload`、vmagent 的 `vmagent_rw_retries`、`vmagent_rw_dropped`、`vmagent_pq_dropped` | 資料之後補回，圖看起來正常，但 rule 不會補判 |
| vmalert 狀態寫回 | `vmalert_remotewrite_errors`；flag `remoteWrite.url`、`remoteRead.url` | queue 在記憶體，重啟遺失；沒設 `remoteRead.url` 時重啟後 `for` 從頭計時 |
| Operator | VMAlert 的兩個 rule selector 都沒設 | 一條 rule 都不選，也沒有錯誤 |

## 6. 覆蓋缺口

`check_coverage.py` 結果裡 card 是這四張的每一列：

- `self_only`：卡片要求 `observer` 的訊號（例如 `vmalert-down`、`rule-errors`、`notifier-errors`）卻在同一套 VM／vmalert 上評估。vmalert 查詢失敗時這些規則跟著失效，要搬到不同故障域的觀察者。
- `rule_no_data`：常見是 `job=~".*vmalert.*"` 這類依 job 名稱比對的上游規則與實際名稱不符，或 recording rule 才有的 `vmalert_recording_rules_errors_total` 不存在。
- `missing`：用卡片訊號 `rules:` 指到的 `rules/victoriametrics/*.yml` 範本，請使用者用 `vmalert -dryRun -rule=<file>` 驗證。
- `evaluate_on: inventory` 的訊號（`rules-loaded`、`resolve-settings`）由你直接用快照判斷：預期 rule 數對照 `rules.json`，flag 對照第 4 節。

把結果交回 `selfcheck`。
