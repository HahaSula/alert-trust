# 狀態

最後更新：2026-11-09。

## 現況

- GitHub repo `HahaSula/alert-trust`，Pages 由 GitHub Actions 發布。
- 11 張卡（9 個監控元件、2 個資料庫 target）、201 個訊號、225 條規則；`validate.py` 0 錯誤、2 個警告；`--strict` 建置通過。
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
| Lab：metric 名稱、MySQL／MariaDB（HA、效能與故障、SQL 追蹤）、PostgreSQL（HA、效能與故障、SQL 追蹤）、pending 抖動（10 組）、Alertmanager、rule 並行與查詢 slot、VictoriaMetrics／VictoriaLogs、Prometheus（從前身 repo 搬來） | `lab/` |
| 資料庫附錄：從 DB 原始碼到 exporter SQL 到 metric（MySQL／MariaDB、PostgreSQL） | `docs/targets/*-appendix.md` |
| 資料庫 target 依 9 個面向分類，每條範例規則附證據鏈（DB 原始碼、表或變數、exporter 原始碼與 SQL） | `catalog/targets/`、`docs/targets/` |
| MariaDB 的 ASH 缺口與取樣器原型 | `docs/targets/mariadb-ash.md`、`lab/targets/mysql/ash/` |
| Thanos v0.42.5、Grafana Alerting v13.3.0 元件卡、規則、架構頁與小 lab | `catalog/components/`、`rules/thanos/`、`rules/grafana/`、`lab/thanos/`、`lab/grafana/` |
| PostgreSQL 的 exporter 盲點用 query-exporter 5.2.2 自訂查詢補上（lab 實測） | `queries/query-exporter/postgres.yaml`、`lab/targets/postgres/custom-queries/` |
| `rule-review` skill 與流程圖：先確認體系與基礎設施覆蓋，再看時間契約；離線用 `tools/rules_to_snapshot.py` | `skills/rule-review/`、`docs/tools/rule-review.md` |

## 進行中

（無）

## 待辦（依建議順序）

1. **VM lab 沒重測到的項目**（`lab/vm/README.md`）：查詢飽和 429 與 `iteration_missed`、磁碟突然 0 byte 的 panic、vmalert／Alertmanager 本身停機、VL `allow_partial_response`、operator。相關說法已降為「推論」。注意這台機器 Docker 容器的單調時鐘比牆鐘慢約 30%，時間類數字要以牆鐘解讀。
2. （已完成）Target 標準問題加入 role-detection、replication-lag、exporter-query-timeout；`aliases` 讓同義的訊號 id 也算數。
3. **剩下的未公開參考**：`exp-am-compat`（版本相容）、`exp-am-rules`（告警規則測試），在 `catalog/refs.d/reports.yaml`；重做或改引用。
4. **2 個第 1 級訊號沒有規則範本**：`victorialogs#ingested`、`observer#scrapes-all-infra`。
5. **規則範本的 job selector**：`PrometheusDown`、`PrometheusCrashLooping` 用 `job=~".*prometheus.*"`，job 名稱不同的環境會「規則在但沒資料」（`lab/coverage` 實測）。要在文件與 skill 強調必須調整，或改成 label 參數化。
6. **覆蓋狀態輸出成 metric**：讓 `check_coverage.py` 定期執行並輸出每個元件、每個訊號的狀態，由外部觀察者抓取並告警，讓 monitor of monitor 的缺口本身也會通知。
7. **CI 用的 policy lint**：舊 repo 的 `policy_lint.py`、`analyze_rules.py` 尚未改寫成讀新目錄。
8. （已完成）GitHub repo 已建立並 push，Pages 已上線；舊 repo 的實驗搬到 `lab/prometheus/`，舊 repo 已封存。
9. **待驗證**：Patroni＋etcd、pgBackRest 的規則只讀碼，可做小 lab；Galera 與 Group Replication 的 flow control 沒有觸發；MariaDB ASH 的 digest 回填未實測；query-exporter 輸出 PostgreSQL 已實測（PG16 只讀碼、PG18 未查）。
11. **Thanos／Grafana 沒實測的部分**：Thanos 的 sidecar 上傳失敗、Receive、HA；Grafana 的 HA、DB 中斷、外部 Alertmanager 失敗、UI 預設的 NoData／Error、Loki／SQL datasource。`docs/architecture/index.md` 的單序點對照表還沒加這兩個元件。
10. **小項目**：MySQL redo log 太小的情境沒有重現（`log_waits` 一直是 0）；`ThroughputDrop` 規則用「昨天同時段」比較，lab 沒有一天的資料無法驗證；MySQL `NoWritablePrimary` 修正後未重跑；PG17、`/probe`、auto-discover 只讀碼；契約頁的圖未截圖確認；Prometheus 報告的例子（30s／100s／120s）使用者尚未表示意見。

## 已做的決定

- Repo 名稱 `alert-trust`；舊 repo 保留不刪。
- Alertmanager 的長週期例子用 `group_interval` 1h、失敗告警視窗 `[2h]`。
- Plugin 以 repo 根目錄為來源，skill 才能讀目錄（代價是安裝時帶著 `lab/` 與 `docs/`）。
- 卡片依 `family` 分組（prometheus、victoriametrics、shared、database）；target 卡的訊號依 9 個 `aspect` 分組：exporter、availability、workload、saturation、replication、cluster、maintenance、protection、capacity。不適用的面向要寫 `not_applicable_aspects` 並附 why 與 refs（例如 PostgreSQL 核心沒有共識機制）。
- 檢查使用者的 rule 檔時，一律先確認監控體系與基礎設施覆蓋，最後才看時間契約（`rule-review`）。
- 網站每個分頁只放一種內容：開始使用、監控鏈（只有元件卡，依 alert 鏈順序）、資料庫（總覽＋每個 DB 的怎麼監控／監控訊號／深入頁／原始碼到 metric）、原理（契約、名詞、內部架構、來源）、實驗報告。頁內面向從大到小；深入頁先寫「要監控什麼」再寫工具與缺口。每張 target 卡必須列出 `blind_spots`（exporter 沒輸出／資料庫沒提供／要靠其他元件），資料庫總覽頁由卡片自動產生。
- 實驗數據不準時改用原始碼佐證。
