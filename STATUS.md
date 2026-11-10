# 狀態

最後更新：2026-11-09。

## 現況

- GitHub repo `HahaSula/alert-trust`，Pages 由 GitHub Actions 發布。
- 18 張卡（10 個監控與主機元件、8 個資料庫 target）、387 個訊號、550 條規則；`validate.py` 0 錯誤、1 個警告；`--strict` 建置通過。
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
| MySQL／MariaDB 與 PostgreSQL 的 exporter 盲點用 query-exporter 5.2.2 自訂查詢補上（lab 實測） | `queries/query-exporter/`、`lab/targets/*/custom-queries/` |
| 資料庫架構頁：關聯式（MySQL／InnoDB 對照 PostgreSQL） | `docs/architecture/db-relational.md` |
| 資料庫監控模型調查（exporter／內建 endpoint／collector／DB 內歷史／廠商方案）與官方告警規則 | `docs/targets/models.md` |
| Valkey 9.1／Redis 7.2、8 target（redis_exporter v1.97.0）與「記憶體 KV」架構頁，lab 實測 | `catalog/targets/redis_exporter.yaml`、`docs/targets/valkey*.md`、`lab/targets/valkey/` |
| etcd v3.7.3 target（第一張 `model: native`）與「共識（Raft）」架構頁，3 節點 lab 實測三方交叉判斷 | `catalog/targets/etcd.yaml`、`docs/targets/etcd*.md`、`lab/targets/etcd/` |
| TiDB v8.5.8 target（`model: native`，TiDB／TiKV／PD）與「分散式 SQL」架構頁；官方規則 5 類失效實測 | `catalog/targets/tidb.yaml`、`docs/targets/tidb*.md`、`lab/targets/tidb/` |
| YugabyteDB v2026.1.3.0 target（`model: native`），YSQL 與 PostgreSQL 監控對照（postgres_exporter 實測） | `catalog/targets/yugabytedb.yaml`、`docs/targets/yugabytedb*.md`、`lab/targets/yugabytedb/` |
| ClickHouse v26.10.2 target（內建 endpoint＋query-exporter 查 system 表）與「分析型」架構頁，2 副本＋3 Keeper lab | `catalog/targets/clickhouse.yaml`、`docs/targets/clickhouse*.md`、`queries/clickhouse/`、`lab/targets/clickhouse/` |
| MongoDB 8.0.34 target（Percona mongodb_exporter v0.54.0）與「文件型」架構頁，3 成員 replica set lab | `catalog/targets/mongodb_exporter.yaml`、`docs/targets/mongodb*.md`、`lab/targets/mongodb/` |
| 盤點工具 `tools/inventory.py`：從 Prometheus／vmagent config、rule 檔、Alertmanager config 建環境模型（每個值標來源），含 Alertmanager 路由與 inhibit；計算錯誤修正（vmagent timeout 截斷、`--baseline` 下限、d = τ、dedup、正規式 job、subquery、沒資料的 rule、exporter 預設期限、`--prom-version`、覆蓋檢查的 Watchdog 歸屬與 `partial`） | `tools/` |
| 資料庫資源對照（連線、執行緒、記憶體池、IO／fsync、複寫 worker、背景工作、CPU）與共用的主機／容器卡（node_exporter、cAdvisor、kube-state-metrics），只讀碼 | `docs/architecture/db-resources.md`、`catalog/components/host.yaml`、`rules/host/` |
| rule-review 改為建議優先：第 0 輪只看檔案就給建議、缺資料用保守假設、每輪最多 3 題但可提前問；工具修正（覆蓋檢查比對 job、`features` 判斷不適用、`fit_contracts --config`、方案 B 可行性、`for` 被 stale 重置的提示、`always_fires`、缺 label 的提問、`validate.py --lint-rules`）；新頁「從哪裡開始」（五階） | `skills/rule-review/`、`tools/`、`docs/tools/start.md` |
| 時間契約複查（原始碼＋promtool 虛擬時鐘測試，Prometheus 3.13.5／2.51.2、VictoriaMetrics）：range-window 一般化為 `(n+k)·I + max(0, d − O)`、新增 `range-window-1sample`、vm-range-window 改為 `(n+k)·I + max(0, L − D)`、resend-endsat 最差情況 `4 × max − P`；各 range 函式需要的 sample 數表；`fit_contracts.py` 依函式判斷 n；視窗要保留語意時（`keep_range`）改給縮短 S、`query_offset`、`anchored` | `docs/contracts.md#range-functions`、`lab/promql-windows/` |
| scrape 與 evaluation 的基準值：官方預設與限制、來源比較、由耗時分布推導 S／τ／exporter 期限／E／k 的步驟；`fit_contracts.py` 檢查 τ、exporter 期限、截斷、p99、E ≥ S、VM dedup，`--baseline` 輸出建議值 | `docs/tools/baseline.md`、`tools/fit_contracts.py` |
| Skill 與工具跟上新卡片：覆蓋檢查器認得所有卡片 id 與常見別名、Thanos Ruler／Grafana 算 ruler；database-targets 依 `model` 分 exporter／native 兩條流程；selfcheck 分派 Thanos、Grafana；plugin 0.3.0 | `tools/check_coverage.py`、`skills/`、`.claude-plugin/` |
| artifact 對照後的修正：VM 報告只留公開 lab 數字、VictoriaLogs storage-reachable 改規則（重跑 lab）、Prometheus retention／notifier／磁碟訊號、VM 系列第 2、3 級訊號 | `docs/reports/`、`catalog/components/`、`rules/`、`lab/vm/vl/` |
| 時間契約不合時給兩個方向：目前合格的 rule、方案 A 改 rule（最短合格視窗，可另存）、方案 B 改環境（`scrape_interval`、`query_offset`） | `tools/fit_contracts.py` |
| metric 來源檢查：每個 metric 要對到 config 的 scrape job、live 快照或使用者回答，不假定（使用者在 v0.1.0 用 opencode 時回報 AI 直接假定來源） | `tools/check_metric_sources.py`、`env/sources.example.yaml` |
| `rule-review` skill 與流程圖：先確認體系與基礎設施覆蓋，再看時間契約；離線用 `tools/rules_to_snapshot.py` | `skills/rule-review/`、`docs/tools/rule-review.md` |

## 進行中


使用者同意的資料庫順序已全部完成；CockroachDB（專有授權）不做。資料庫架構頁依類型分頁：關聯式、記憶體 KV、共識（Raft）、分散式 SQL、分析型、文件型。

## 待辦（依建議順序）

1. **VM lab 沒重測到的項目**（`lab/vm/README.md`）：查詢飽和 429 與 `iteration_missed`、磁碟突然 0 byte 的 panic、vmalert／Alertmanager 本身停機、VL `allow_partial_response`、operator。相關說法已降為「推論」。注意這台機器 Docker 容器的單調時鐘比牆鐘慢約 30%，時間類數字要以牆鐘解讀。
2. （已完成）Target 標準問題加入 role-detection、replication-lag、exporter-query-timeout；`aliases` 讓同義的訊號 id 也算數。
3. **剩下的未公開參考**：`exp-am-compat`（版本相容）、`exp-am-rules`（告警規則測試），在 `catalog/refs.d/reports.yaml`；重做或改引用。
4. **1 個第 1 級訊號沒有規則範本**：`observer#scrapes-all-infra`。
5. **規則範本的 job selector**：`PrometheusDown`、`PrometheusCrashLooping` 用 `job=~".*prometheus.*"`，job 名稱不同的環境會「規則在但沒資料」（`lab/coverage` 實測）。要在文件與 skill 強調必須調整，或改成 label 參數化。
6. **覆蓋狀態輸出成 metric**：讓 `check_coverage.py` 定期執行並輸出每個元件、每個訊號的狀態，由外部觀察者抓取並告警，讓 monitor of monitor 的缺口本身也會通知。
7. **CI 用的 policy lint**：舊 repo 的 `policy_lint.py`、`analyze_rules.py` 尚未改寫成讀新目錄。
8. （已完成）GitHub repo 已建立並 push，Pages 已上線；舊 repo 的實驗搬到 `lab/prometheus/`，舊 repo 已封存。
9. **待驗證**：Patroni（etcd 本身已有卡片與 lab）、pgBackRest 的規則只讀碼，可做小 lab；Galera 與 Group Replication 的 flow control 沒有觸發；MariaDB ASH 的 digest 回填未實測；query-exporter 輸出 MySQL／MariaDB 與 PostgreSQL 都已實測（PG16 只讀碼、PG18 未查）。
11. **Thanos／Grafana 沒實測的部分**：Thanos 的 sidecar 上傳失敗、Receive、HA；Grafana 的 HA、DB 中斷、外部 Alertmanager 失敗、UI 預設的 NoData／Error、Loki／SQL datasource。`docs/architecture/index.md` 的單序點對照表還沒加這兩個元件。
10. **小項目**：MySQL redo log 太小的情境沒有重現（`log_waits` 一直是 0）；`ThroughputDrop` 規則用「昨天同時段」比較，lab 沒有一天的資料無法驗證；MySQL `NoWritablePrimary` 修正後未重跑；PG17、`/probe`、auto-discover 只讀碼；契約頁的圖未截圖確認；Prometheus 報告的例子（30s／100s／120s）使用者尚未表示意見。
12. **架構頁發現的卡片缺口**：MySQL 卡沒有 checkpoint age 訊號（`innodb-pressure` 只有 `log_waits`、`wait_free`；lab 有量到 checkpoint age）；PG 卡沒有 `synchronous_commit` 訊號。
13. （已完成）內建 endpoint 型資料庫的標準問題：`target-pattern.yaml` 的 `required_signals_native`，卡片寫 `model: native`。
14. （已完成，實測）官方 TiDB 規則的失效：TiKV `name="apply_.*"`（tikv.rules.yml:173）對到 0 條；`job` 寫死；PD 規則以 `etcd_server_is_leader` 為前提，leader 抓不到時回傳空；`miss_peer_region_count` 用底線而 PD 輸出連字號；`tikv_pd_heartbeat_tick_total`、`tidb_monitor_keep_alive_total` 在 v8.5.8 不存在。TiDB 的慢磁碟、write stall、GC 卡住沒做實驗。
15. **Sentinel 沒測到**：這台機器上 Sentinel 一直進 TILT，failover、sdown、MISCONF 觸發 sdown 只讀碼。
16. **YugabyteDB lab 待補**：四個 job 改 `honor_timestamps: false` 重跑（舊值留約 5 分鐘、out-of-order 丟棄，目前只是推論可解）；`/api/v1/tablet-under-replication` 整段 lab 都空的原因；修正後的 `YBMasterLeaderProbeMissing` 只做過即時查詢，沒有整檔重跑；慢磁碟、write stall、記憶體拒絕、YCQL 在 master 失去 quorum 時沒做實驗。
17. **ClickHouse 待補**：修正後的 Keeper session 規則（5 分鐘內有變化）只看到 firing，沒等到解除；慢或滿的磁碟、慢 Keeper、卡住的 ON CLUSTER DDL、全伺服器記憶體上限只讀碼。
18. **query-exporter 排程跳過**：間隔對齊時較長間隔的查詢會被跳過（ClickHouse lab 錯開間隔後解決；MySQL lab 也看過 `misfire_grace_time` 1 秒的跳過）。PostgreSQL 的 5 分鐘查詢可能受影響，要檢查 `queries/query-exporter/postgres.yaml` 的間隔。
19. **artifact 對照的後續輕量 lab**：429 與 `iteration_missed`、帶 `for` 的 log rule、損毀 block（`reloads_failures_total`）、長查詢卡住 Compact（目前讀碼）。新增的 Prometheus／VM 規則只過 promtool／dryRun，沒有在 lab 觸發過。
20. **MongoDB 待補**：sharded cluster、WiredTiger cache／eviction 壓力、ticket 排隊、連線打滿、collStats 成本只讀碼；oplog 被釘住只讀碼。`docs/targets/models.md` 說「8.0、8.2 的 tag 沒有 OTel 程式碼」，MongoDB agent 回報 8.2.12 有 OTLP 檔案／HTTP 推送（沒有 Prometheus 檔案輸出），要附原始碼行號後修正。
21. **契約複查的後續**：`resend-endsat` 最差情況（`− P`）與 `d > S` 時 `I ≈ d` 只讀碼，要輕量 lab；Grafana Alerting 的續送是否同樣「嚴格大於」才送未查。
22. **資源調查的後續**：各 target 卡的 blind_spots 改成 `covered_by` 主機卡；`#proposed` 表的 10 個飽和訊號；host 卡要不要新的 family；target-pattern 要不要加「主機層存在」；OOM、唯讀 remount、記憶體上限的小 lab。

## 已做的決定

- Repo 名稱 `alert-trust`；舊 repo 保留不刪。
- Alertmanager 的長週期例子用 `group_interval` 1h、失敗告警視窗 `[2h]`。
- Plugin 以 repo 根目錄為來源，skill 才能讀目錄（代價是安裝時帶著 `lab/` 與 `docs/`）。
- 卡片依 `family` 分組（prometheus、victoriametrics、shared、database）；target 卡的訊號依 9 個 `aspect` 分組：exporter、availability、workload、saturation、replication、cluster、maintenance、protection、capacity。不適用的面向要寫 `not_applicable_aspects` 並附 why 與 refs（例如 PostgreSQL 核心沒有共識機制）。
- 檢查使用者的 rule 檔時，一律先確認監控體系、每個 metric 的來源（不假定）、基礎設施覆蓋，最後才看時間契約（`rule-review`）。
- 網站每個分頁只放一種內容：開始使用、監控鏈（只有元件卡，依 alert 鏈順序）、資料庫（總覽＋每個 DB 的怎麼監控／監控訊號／深入頁／原始碼到 metric）、原理（契約、名詞、內部架構、來源）、實驗報告。頁內面向從大到小；深入頁先寫「要監控什麼」再寫工具與缺口。每張 target 卡必須列出 `blind_spots`（exporter 沒輸出／資料庫沒提供／要靠其他元件），資料庫總覽頁由卡片自動產生。
- 實驗數據不準時改用原始碼佐證。
