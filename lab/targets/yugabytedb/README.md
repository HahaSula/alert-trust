# YugabyteDB 三方交叉、master quorum、YSQL 連線上限與 postgres_exporter

回答五個問題：一台 tserver 停掉時 master 與 tablet 怎麼描述它（dead 判定、under-replicated、leaderless）、master leader 停掉時多久選出新的、master 失去 quorum 時 tserver 還能不能服務讀寫、YSQL 連線用完時 probe 與 exporter 看不看得到、postgres_exporter 的 collector 在 YSQL 上哪些能用。結果用在 [YugabyteDB 怎麼監控](../../../docs/targets/yugabytedb.md) 與 [YugabyteDB 元件卡](../../../catalog/targets/yugabytedb.yaml)。

## 環境

| 元件 | 版本／設定 |
|---|---|
| yb-master | `yugabytedb/yugabyte:2026.1.3.0-b100` ×3，`memory_limit_hard_bytes` 256MB、block cache 16MB |
| yb-tserver | 同一個映像 ×3，RF=3；`memory_limit_hard_bytes` 384MB、block cache 32MB、每台 1 個 shard；`ysql_max_connections=12`；`follower_unavailable_considered_failed_sec=120`（預設 900） |
| 時鐘 | 全部 `fail_on_out_of_range_clock_skew=false`：預設值下三台 tserver 啟動 20 秒內就因牆鐘往回跳 FATAL（`results/clock-skew-crash-20261110.txt`） |
| Prometheus | `prom/prometheus:v3.13.5`，scrape 5 秒、timeout 4 秒，載入 `rules/targets/yugabytedb.yml`；預設 `honor_timestamps: true`（見結果） |
| blackbox_exporter | `prom/blackbox-exporter:v0.27.0`，timeout 4 秒；master 的 JSON API 用 body 比對（`bb/blackbox.yml`） |
| postgres_exporter | `quay.io/prometheuscommunity/postgres-exporter:v0.20.1`（和 `lab/targets/postgres` 同一版）；`pgexp` 關掉 stat_replication 與 settings，`pgexp-default` 用預設 collector、只在情境 B 啟動 |

只開 `127.0.0.1` 的 30001–30003（master web）、30011–30013（tserver web）、30021–30023（YSQL web）、30031–30033（YCQL web）、30041（YSQL）、30090（Prometheus）、30091（blackbox）、30092–30093（exporter）。compose project 是 `at-yb`；容器記憶體上限加起來約 4.1GB，實際用量約 1.7GB（tserver 各約 400MB）。`./run.sh` 約 14 分鐘加上啟動 2 分鐘，結束時 `docker compose down -v` 並列出殘留容器與 volume（這次都是 0）。

這台機器的 Docker 容器單調時鐘比牆鐘慢約 30%，牆鐘也會被 WSL 往回調。SQL 與 HTTP 的耗時用主機的 monotonic 時鐘量；Prometheus 的 `for` 與 YugabyteDB 自己報的秒數只當參考。

## 情境

| 代號 | 做法 |
|---|---|
| A | 基線：建表寫 1,000 列；端點回應；叢集 metric 由哪台 master 輸出；sample 數 |
| B | 直接執行 exporter 會用到的查詢（8 秒 timeout）；啟動預設 collector 的 exporter 約 50 秒，看連線數與 up；停掉後清掉卡住的 backend；看修正後 exporter 的 collector 與數值 |
| C1 | `docker kill` tserver-3，+20、+75、+150 秒看三方交叉、master JSON API、SQL |
| C2 | 再 kill tserver-2，+30、+150 秒（leaderless 要 120 秒）；之後兩台都啟動 |
| D | kill master leader，每秒查 `/api/v1/is-leader` 到新 leader 出現；寫入；+26 秒看三方交叉 |
| E | 先開一條長連線（每 10 秒讀寫一次），kill master leader 與一台 follower，+20、+60、+120 秒看存活那台的端點與 SQL；之後啟動 |
| F | 從 tserver-2 對 tserver-1 開 12 條 `pg_sleep(70)`，看新連線、連線 metric、tcp probe、exporter |
| G | 整段的 clock skew、scrape 耗時、out-of-order、記憶體 |

慢磁碟、write stall、記憶體壓力拒絕、xCluster、YCQL 的負載、connection manager 沒有做。

## 結果

原始輸出 `results/run-20261110.txt`。補充檔：`clock-skew-crash-20261110.txt`（第一次啟動的 FATAL）、`pgexp-startup-20261110.txt`（第一次啟動時預設 collector 的 exporter log）、`pgexp-collectors-20261110.txt`（情境 B 的補查）、`master-quorum-tserver-log-20261110.txt`（情境 E 的 tserver log）、`rule-fix-20261110.txt`（一條規則的初版錯誤）、`names-{master,tserver,ysql,ycql}-2026.1.3.0.txt`（metric 名稱）。

### 三方交叉（up／probe／健康 metric）

| 情境 | 對象 | `up` | probe | 健康 metric | 判讀 |
|---|---|---|---|---|---|
| 正常 | master ×3 | 1 | health-check 1；is-leader 只有 leader 1 | `is_raft_leader{sys.catalog}` 只有 leader 1；`num_tablet_servers_live` leader 3、follower 0 | — |
| 正常 | tserver ×3、YSQL ×3 | 1 | health-check 1、tcp 1 | master leader 看到 live 3 | — |
| 停 tserver-3，+20 秒 | tserver-3 | 0 | 0 | live 3、dead 0（還沒到 60 秒） | process 停了 |
| 停 tserver-3，+75 秒 | tserver-3 | 0 | 0 | live 2、dead 1；health-check 列出 dead node 與副本在它上面的 tablet | master 判定 dead |
| 再停 tserver-2，+150 秒 | tserver-1（存活） | 1 | health-check 1 | live 1、dead 2；`follower_lag_ms` 155,648；leaderless 列出（152 秒） | tablet 沒有過半；讀取 19–21 秒逾時 |
| 停 master leader，+26 秒 | 舊 leader | 0 | 0 | 舊 leader 的 `is_raft_leader`=1、`num_tablet_servers_live`=3 仍在（帶時間戳，沒有 staleness）；新 leader 也是 1 | 新 leader 3 秒內出現；YBMasterLeaderCountWrong 誤判 pending |
| 停 2 台 master | 存活那台 | **1** | is-leader 0；health-check 200 但內容是 `Unable to locate leader master` | `/status` `{}` | master 沒有 quorum |
| 停 2 台 master | tserver ×3 | 9000 是 1、13000 是 **0** | tserver health-check 1；YSQL tcp **0** | 約 30 秒後 `Lease has expired, killing pg sessions` | YSQL 整個停掉 |
| YSQL 連線用完 | tserver-1 | 1 | tcp **1** | `over_limit` 1 分鐘 +7.3；`connection_total` 14／上限 12；已連著的 exporter `pg_up` 1 | 新連線被拒 |

### 其他

| 項目 | 結果 |
|---|---|
| 時鐘 | 預設設定：三台 tserver 啟動約 20 秒內同時 FATAL（skew 7.0 秒）；master 只寫 ERROR。關掉之後每台 2–26 行 ERROR，`max_over_time(hybrid_clock_skew[1h])` 全是 0 |
| 時間戳 | `prometheus_target_scrapes_sample_out_of_order_total` 約 14 分鐘到 20,748 |
| master 換 leader | kill 後 3 秒另一台 is-leader 200；同時的一筆 insert 13 秒成功；raft_term 1 → 2 |
| 停 1 台 tserver | 那時的一筆 insert 9 秒成功，之後 0 秒；`/api/v1/tablet-under-replication` 整段都是空的 |
| master 失去 quorum 時的長連線 | +12 秒讀取成功，接著的 insert 15 秒 statement timeout，lease 過期時連線被結束；master 回來後新的 lease epoch 又清掉所有 session |
| postgres_exporter 預設 collector | `pg_stat_replication` 卡住（8 秒 timeout 才結束）；exporter 啟動 20 秒內 YSQL 連線 7、50 秒 14，ysqlsh 也 too many clients；36 次 scrape 只有 1 次 up=1；exporter 停掉後 11 個 backend 還在 |
| `pg_settings` | 兩個 GUC 各兩列；settings collector 讓 `/metrics` 回 500：這是探索時手動 curl 看到的，原始輸出沒有保存；之後想用單節點重現時 tserver 沒有起來，沒有重做 |
| 修正後的 exporter | 13 個 collector 都成功；`xact_commit` 有在動；`pg_database_size_bytes` 0、`n_live_tup` 0（表有 1000 列）、`tup_inserted` 0、bgwriter 0、`pg_replication_lag_seconds` 0；`pg_current_wal_lsn()` 不支援 |
| scrape | master 4,576 個 sample（leader 4,920）、tserver 3,943、YSQL 428、YCQL 416；平常 0.003–0.03 秒，停機時最長約 3.6 秒；master 沒有 leader 時 `/prometheus-metrics` 1.8MB、0.03 秒 |

### 規則的問題（這次 run 發現）

- `YBMasterLeaderProbeMissing` 初版用 `sum by (job) (...) or vector(0)`：`vector(0)` 的 label 集合和 `{job=...}` 不同，`or` 一定把它加進來，所以一直 firing。已改成 `sum(...)`。規則檔是單一檔案 bind mount，run 中途改檔沒有被容器看到，這次 run 的 alerts 列表裡這條一直是 firing（`rule-fix-20261110.txt`）。
- `YBMasterLeaderCountWrong` 在停 master leader 後 pending：時間戳讓舊 leader 的值留著。解法是 scrape 設定（`honor_timestamps: false`），沒有重跑驗證。
