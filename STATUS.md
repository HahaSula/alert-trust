# 狀態

最後更新：2026-11-08。

## 現況

- 本機 git repo，尚未建立 GitHub repo、尚未 push。GitHub Pages 要在 repo 設定把來源設成 GitHub Actions。
- 9 張卡（7 個監控元件、2 個資料庫 target）、121 個訊號、136 條規則；`validate.py` 0 錯誤、2 個警告；`--strict` 建置通過。
- 原始碼參考 105+ 筆（釘版本與行號），實驗參考多數已換成 `lab/` 的公開結果。

## 已完成

| 項目 | 位置 |
|---|---|
| 元件卡、契約、名詞表、來源與參考制度 | `catalog/` |
| 規則範本（Prometheus、Alertmanager、VM 系列、MySQL、PostgreSQL） | `rules/` |
| 網站：模型、元件卡、內部架構 8 頁、信任契約、計算器、名詞表、Target 怎麼監控、4 篇報告、10 張圖 | `docs/` |
| 覆蓋檢查器（含驗證 lab） | `tools/check_coverage.py`、`lab/coverage/` |
| 唯讀蒐集器 | `tools/collect.py` |
| Claude Code plugin：selfcheck、env-profile、prometheus、victoriametrics、alertmanager、database-targets、policy-lint | `skills/`、`.claude-plugin/` |
| Lab：metric 名稱、MySQL／MariaDB、PostgreSQL、pending 抖動（10 組）、Alertmanager、rule 並行與查詢 slot | `lab/` |

## 進行中

- **VictoriaMetrics／VictoriaLogs 實驗重做**（`lab/vm/`）：完成後把 `catalog/refs.yaml` 的 `exp-vm-matrix`、`exp-vm-faults`、`exp-vm-vl`、`exp-vm-resend` 換成公開結果，並核對 vmalert、vmagent、victoriametrics、victorialogs 卡與 `docs/architecture/` 的說法。

## 待辦（依建議順序）

1. **整合 VM lab 結果**（上一項）。
2. **Target 標準問題擴充**（待使用者確認）：在 `catalog/target-pattern.yaml` 加 `role-detection`（沒有可寫入節點／多個可寫入節點，需要 `cluster` label）、`replication-lag`（不能只靠伺服器算的延遲；MySQL 與 PG 都實測到斷線時顯示 0）、exporter 查詢逾時（PG 表鎖、MySQL 連線打滿都會讓 exporter 消失）。
3. **剩下的未公開參考**：`exp-am-compat`（版本相容）、`exp-am-rules`（告警規則測試），在 `catalog/refs.d/reports.yaml`；重做或改引用。
4. **2 個第 1 級訊號沒有規則範本**：`victorialogs#ingested`、`observer#scrapes-all-infra`。
5. **規則範本的 job selector**：`PrometheusDown`、`PrometheusCrashLooping` 用 `job=~".*prometheus.*"`，job 名稱不同的環境會「規則在但沒資料」（`lab/coverage` 實測）。要在文件與 skill 強調必須調整，或改成 label 參數化。
6. **覆蓋狀態輸出成 metric**：讓 `check_coverage.py` 定期執行並輸出每個元件、每個訊號的狀態，由外部觀察者抓取並告警，讓 monitor of monitor 的缺口本身也會通知。
7. **CI 用的 policy lint**：舊 repo 的 `policy_lint.py`、`analyze_rules.py` 尚未改寫成讀新目錄。
8. **建立 GitHub repo、push、開 Pages**；在 `prometheus-selfcheck-skills` README 指向新 repo。
9. **小項目**：MySQL `NoWritablePrimary` 修正後未重跑；PG17、`/probe`、auto-discover 只讀碼；契約頁的圖未截圖確認；Prometheus 報告的例子（30s／100s／120s）使用者尚未表示意見。

## 已做的決定

- Repo 名稱 `alert-trust`；舊 repo 保留不刪。
- Alertmanager 的長週期例子用 `group_interval` 1h、失敗告警視窗 `[2h]`。
- Plugin 以 repo 根目錄為來源，skill 才能讀目錄（代價是安裝時帶著 `lab/` 與 `docs/`）。
- 卡片依 `family` 分組（prometheus、victoriametrics、shared、database）；target 卡的訊號依 `aspect` 分組（exporter、stability、usage、ha）。
