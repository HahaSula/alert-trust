# etcd 健康第二路徑與 alarm

回答四個問題：失去 quorum 時 `/metrics` 還在不在（`up`、`/readyz`、`etcd_server_has_leader` 三方交叉）、只有 scrape 失敗時三方怎麼分、`/metrics` 會不會被讀取或刪除卡住、NOSPACE alarm 在哪些路徑看得到。結果用在 [etcd 怎麼監控](../../../docs/targets/etcd.md) 與 [etcd 元件卡](../../../catalog/targets/etcd.yaml)。

## 環境

| 元件 | 版本／設定 |
|---|---|
| etcd | `gcr.io/etcd-development/etcd:v3.7.3` 三台（e1、e2、e3），`--quota-backend-bytes=33554432`（32MiB，故意設小），其他預設（`--metrics=basic`） |
| Prometheus | `prom/prometheus:v3.13.5`，scrape 5 秒、timeout 4 秒，載入 `rules/targets/etcd.yml` |
| blackbox_exporter | `prom/blackbox-exporter:v0.27.0`，`http_2xx` timeout 4 秒；三個 probe job：`etcd-readyz`、`etcd-livez`、`etcd-health` |

probe job 用 relabel 把 `instance` 設成和 `job="etcd"` 相同的 `host:port`（`prom/prometheus.base.yml`），規則才能 `on (instance)` 交叉比對。只開 `127.0.0.1` 的 28001–28003（etcd client port）、28090（Prometheus）、28091（blackbox）。compose project 是 `at-etcd`；`./run.sh` 約 10 分鐘，結束時 `docker compose down -v` 並列出殘留容器（應為 0）。`kv.py` 從主機查 Prometheus、直接打 etcd 的 HTTP 端點、透過 gRPC gateway 寫入與讀取。

這台機器的 Docker 容器單調時鐘比牆鐘慢約 30%：Prometheus 內的時間（`probe_duration_seconds`、`for`）只當參考；`kv.py` 印的耗時是主機時鐘。

## 情境

| 代號 | 做法 |
|---|---|
| A | 基線：sample 數、basic 有沒有 `grpc_server_handling_seconds`、`etcd_network_known_peers`、feature gate、quota |
| B | `docker kill` leader，20 秒後看三方，再啟動它 |
| C | `docker kill` 兩台，存活那台：`/metrics`、`/livez`、`/readyz`、`/health`、寫入；再等 70 秒看規則 |
| D | `job="etcd"` 加 `sample_limit: 100` 並 reload：只有 scrape 失敗 |
| E | 寫 3,000 個 2KB key；4 條執行緒反覆讀整個 prefix 20 秒、再一次刪掉，期間每 0.5 秒量 `/metrics` |
| F | 每次寫 200KB 直到 NOSPACE；compact＋defrag；alarm 還在時重啟 e2；disarm 後再等 70 秒 |

慢磁碟、TLS 憑證、learner 沒有做（慢磁碟在這台機器做不到輕量；其餘只讀原始碼）。

## 結果

摘要在 `results/summary.md`。原始輸出：`results/run-20261109.txt`（規則改版後，主要依據）、`results/run-20261109-v1.txt`（第一版規則：EtcdQuorumAtRisk 數 `has_leader == 1`、EtcdLeaderCountWrong 沒有排除抓不到的 member、EtcdAlarmActive 只看 gauge）。`results/names-etcd-3.7.3.txt` 是 `/metrics` 的 metric 名稱。
