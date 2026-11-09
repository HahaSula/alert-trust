---
name: policy-lint
description: Evaluate a collect.py snapshot against explicit, repeatable monitoring policies derived from the alert-trust catalog contracts, for Prometheus, vmalert/VictoriaMetrics, Alertmanager and exporter targets. Names each violation (policy, object, why, suggested value) and supports team thresholds and exemptions with reasons and expiry. Use for rule review, "is this written correctly", or turning findings into a checklist. 用明確的 policy 檢查規則與設定，直接指出違反項與建議值，支援團隊門檻與豁免。
argument-hint: "[snapshot-dir] [policy-file]"
---

# Policy 檢查

把「這樣寫對不對」變成可重複的檢查：**哪一條 policy、哪個對象違反、為什麼、建議改成多少**。Policy 是目錄契約的機械化版本：每條都指回 `$AT/catalog/contracts.yaml` 的契約或某張卡的訊號，門檻與符號以目錄為準。

`$AT` 是 alert-trust 根目錄（plugin 安裝時 `${CLAUDE_PLUGIN_ROOT}`，在 repo 裡是 `${CLAUDE_SKILL_DIR}/../..`）。

## 全程規則

- **唯讀**：只讀快照，必要時用 GET 查詢補資料。不改 rule 檔或設定，除非使用者明確要求。
- **隱私**：快照與結果含 job 名稱與 label，留在本機；結果檔放 `reports/`。
- **數字標來源**：`量測`（快照檔與欄位）、`使用者說`、`推算`（公式與代入值）。建議值是公式算出的**下限**，不是最佳值。
- **引用證據**：每條違反附 policy 對應的契約或訊號連結與 `refs` 註腳，格式見 `$AT/skills/selfcheck/references/evidence.md`。
- 每次最多問 3 題；用使用者的語言。

## 輸入

1. 快照目錄（結構見 `$AT/skills/selfcheck/references/snapshot.md`）。依 `manifest.json` 的 `kind` 決定套用哪些 policy 類別。
2. Policy 清單：[references/policies.md](references/policies.md)。每條有 id、適用的 kind、嚴重度、判斷方式、對應契約或訊號。
3. 團隊設定（選用）：使用者工作目錄的 `selfcheck-policy.json`，格式見 [references/policy.example.json](references/policy.example.json)。沒有時用範例裡的預設值。`data_latency_seconds`、`log_delay_seconds` 是量不到 `L` 時使用者給的值（標「使用者說」）；兩者都是 `null` 時，`VM_RANGE_TOO_SHORT`、`LOG_RULE_DELAY` 標 `unknown`，不要猜。
4. env 檔（選用）：`critical.alerts` 決定 `CRITICAL_ALERT_IN_HEAVY_GROUP`；`observers` 決定 `SCRAPE_NOT_MONITORED` 要看哪幾台的規則。

規則的 group `query_offset` 在舊版 Prometheus 的 API 不回傳；`rules.json` 沒有 `queryOffset` 而使用者說有用時，請對方提供 rule 檔再判斷。全域 `rule_query_offset` 從 `config.json` 讀。

## 0. 先決條件（不能跳過）

進入下面任何一節之前，確認：

1. **監控體系已確認**：評估規則的是 Prometheus 還是 vmalert、版本、資料從哪裡來、有沒有外部觀察者。沒有 `env/<name>.yaml` 時先用 `env-profile`，或依 `rule-review` 第 1 步問清楚。
2. **基礎設施的覆蓋已檢查**：已經跑過 `check_coverage.py`（沒有 API 時先用 `tools/rules_to_snapshot.py` 把 rule 檔轉成快照），並先回報第 1 級缺口。

使用者只丟 rule 檔要你「檢查」時，改走 `rule-review` skill，不要直接從時間契約開始。

## 做法

1. 對每個 kind，逐條套用 policies.md 的檢查。判斷時要讀表達式本身；PromQL／MetricsQL 的解析靠你，不要只用正規表示式下結論。Recording rule 的上游延遲、`on()`、subquery 都要人工看。
2. 套用豁免：`exempt[]` 的 `policy` 相同、`object` 正規表示式比對成功、`until` 尚未過期的項目改標 `EXEMPT` 並保留理由。過期的豁免要提醒。
3. 輸出 `reports/<name>-<YYYYMMDD>/policy-lint.md`（給人）與 `policy-lint.json`（給下一次對話與 CI 比對），JSON 每筆：`{"policy","severity","kind","object","value","expected","why","contract","refs","source":"measured|user|inferred"}`。`object` 用 `<component id>/<job|group|alert>` 的形式。

## 互動

1. **先報摘要**：FAIL、WARN 數量，以及數量最多的 policy。
2. **逐類處理，FAIL 優先。**不要一次貼完所有明細。先問使用者想從哪一類開始；有指定重要 alert 時先看那些。
3. **確認數字前提**：建議值來自 `peaks.json` 的範圍。問：這段範圍涵蓋尖峰嗎？尖峰是常態還是偶發？
4. **每個建議都說代價**：range 拉長反應變慢、查詢變貴；interval 拉長解析度降低；offset 讓通知變慢；`resendDelay` 拉長讓 Watchdog 更晚斷。
5. **討論豁免**：使用者認為不適用時，把對象、理由、到期日寫進 `selfcheck-policy.json`，不要直接略過。豁免一定要有理由，建議設到期日。

## 判讀限制

- `SERIES_NOT_FOUND` 分不出打錯名稱和「平常不存在的 metric」（例如錯誤計數器、只有 recording rule 才有的 metric）。kube-prometheus-stack、VM 官方規則這類預設規則會有很多筆，先看自己寫的。
- `SCRAPE_NOT_MONITORED` 只看快照裡有的 ruler。另一台負責時，請使用者把那台也加進 collect，或寫豁免。
- 尖峰只包含抓到的資料，scrape 失敗的那幾次不在裡面。
- 這份檢查不判斷架構（故障域、告警路徑、DMS），那是 `check_coverage.py` 與各 pack 的工作。

## 修正後

重跑 `collect.py` 與本 skill，和上一份 `policy-lint.json` 比較；用 `changes(ALERTS_FOR_STATE{alertname="X"}[1h])` 確認 `for` 不再重置。
