# TiDB 健康第二路徑與官方規則

回答四個問題：一台 TiKV 停掉時 PD 怎麼描述它（store 狀態、region 健康）、PD leader 抓不到時叢集規則會怎樣、PD 失去 quorum 時 SQL 層與 `/status` 還在不在、官方規則（v8.5.8）裡讀碼發現的問題實際上會不會讓規則永遠安靜。結果用在 [TiDB 怎麼監控](../../../docs/targets/tidb.md) 與 [TiDB 元件卡](../../../catalog/targets/tidb.yaml)。

## 環境

| 元件 | 版本／設定 |
|---|---|
| PD | `pingcap/pd:v8.5.8` ×3，`schedule.max-store-down-time = "1m"`（預設 30m） |
| TiKV | `pingcap/tikv:v8.5.8` ×3，block-cache 64MB、memory-usage-limit 512MB、執行緒池 1；`raftstore.max-peer-down-duration = "40s"`（預設 10m），見 `conf/tikv.toml` |
| TiDB | `pingcap/tidb:v8.5.8` ×1，`split-table = false`（region 少） |
| Prometheus | `prom/prometheus:v3.13.5`，scrape 5 秒、timeout 4 秒；載入 `rules/targets/tidb.yml` 與官方三份規則（`official/`，由 run.sh 從 tag 下載，不修改） |
| blackbox_exporter | `prom/blackbox-exporter:v0.27.0`，timeout 4 秒；probe job：`pd-readyz`、`pd-health`、`pd-ping`、`pd-leader-api`、`tikv-status`、`tidb-status`、`tidb-sql` |
| client | `mysql:8.4.7`（只用 mysql client） |

**TiKV binary。**映像內的 `/tikv-server` 帶壓縮的 debug section，啟動時 backtrace-loader 執行緒會把它解壓到記憶體（約 1.9GB），這台機器放不下三台。`run.sh` 從同一個映像複製 binary、`objcopy --strip-debug` 後掛進容器執行（`.bin/`，結束時刪除）；程式碼沒有改，只少了 debug info（panic 時的 stack 沒有行號）。

只開 `127.0.0.1` 的 29000（SQL）、29080（TiDB status）、29001–29003（PD）、29011–29013（TiKV status）、29090（Prometheus）、29091（blackbox）。compose project 是 `at-tidb`；`./run.sh` 約 13 分鐘（加上第一次下載映像），結束時 `docker compose down -v` 並列出殘留容器（應為 0）。所有容器的記憶體上限加起來約 3.9GB，實際用量約 1.4GB。`q.py` 從主機查 Prometheus、直接打元件的 HTTP 端點，並稽核官方規則的 selector。

這台機器的 Docker 容器單調時鐘比牆鐘慢約 30%，牆鐘也會被 WSL 往回調：Prometheus 內的時間（`for`、`scrape_duration_seconds`）只當參考；SQL 與 HTTP 的耗時是主機時鐘。`PD_node_restart`、`TiDB_node_restart`、`PD_system_time_slow` 在這台機器上會一直響，是環境造成的。

## 情境

| 代號 | 做法 |
|---|---|
| A | 基線：sample 數、端點回應、叢集 metric 由哪台 PD 輸出；官方規則的 (a) apply matcher、(b) job 寫死、(d) miss_peer 底線，與每個 selector 對到幾條 |
| B | 寫入約 1,000 列，量三種元件的 `/metrics` |
| C | `docker kill tikv3`，+33、+85、+142 秒看 store 狀態、region 健康、三方交叉、alerts |
| D | TiKV 仍停著，把 PD leader 的 scrape 改到沒有在聽的 port（instance 不變），90 秒後看官方規則與我們的規則；還原後啟動 tikv3 |
| E | `docker kill` PD leader，5 秒後寫入、29 秒後看新 leader |
| F | `docker kill` 3 台 PD 中的 2 台，存活那台的 `/metrics`、`/health`、`/readyz`、`/livez`、`/pd/api/v1/*`；TiDB 的 `/status` 與 SQL（`select 1`、讀表、寫入） |

慢磁碟、write stall、熱點、GC 卡住、TiFlash、TiCDC 沒有做。

## 結果

摘要在 `results/summary.md`，原始輸出 `results/run-20261110.txt`，metric 名稱 `results/names-{pd,tikv,tidb}-8.5.8.txt`。
