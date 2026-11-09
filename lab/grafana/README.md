# Grafana Alerting：資料來源消失、變慢、Grafana 重啟時 Alertmanager 收到什麼

驗證 [Grafana Alerting 元件卡](../../catalog/components/grafana_alerting.yaml)與[架構頁](../../docs/architecture/grafana-alerting.md)裡標為實測的結論（`exp-graf-modes`、`exp-graf-restart`、`exp-graf-slow`、`exp-graf-admin`）。

結果與數字：[results/summary.txt](results/summary.txt)。原始輸出：`results/run.log`（每 10–15 秒一次快照）、`results/run.json`、`results/grafana-log-excerpt.txt`。

## 需要

- Docker 與 docker compose v2、Python 3（只用標準函式庫）
- 映像檔：`grafana/grafana:13.3.0`、`prom/prometheus:v3.13.5`、`prom/alertmanager:v0.27.0`、`python:3.12.7-alpine`
- 主機 port：26000（Grafana）、26080（fake）、26090（Prometheus）、26093（Alertmanager）

## 怎麼跑

```bash
python3 run.py      # 約 13 分鐘；開始前與結束時都會 docker compose down -v
```

## 組成

| 檔案 | 內容 |
|---|---|
| `compose.yml` | compose project `at-graf`：Grafana、Prometheus、外部 Alertmanager、fake |
| `fake.py` | 假 exporter（`lab_value`，可關掉讓 scrape 失敗）、假 Prometheus API（可加延遲，當作「慢的資料來源」）、webhook 接收端（記錄 firing／resolved） |
| `provisioning/` | 資料來源（Prometheus、Slow、外部 Alertmanager 並設 `handleGrafanaManagedAlerts`）、rule、contact point、policy |
| `configs/` | Prometheus（每 5s 抓 fake）與外部 Alertmanager（webhook 轉回 fake，`send_resolved: true`） |
| `run.py` | 驅動程式 |

Rule（group interval 10s、`for: 0s`，條件都是 `lab_value > 0`）：

| Rule | `noDataState` | `execErrState` |
|---|---|---|
| `mode_alerting` | Alerting | Alerting |
| `mode_ok` | OK | OK |
| `mode_nodata_error` | NoData | Error |
| `mode_keeplast` | KeepLast | KeepLast |
| `slow_query`（查假 Prometheus API） | NoData | Error |

## 情境

| | 做法 | 看什麼 |
|---|---|---|
| S0 | 啟動後呼叫 `POST /api/v1/ngalert/admin_config {"alertmanagersChoice":"all"}` | 不設時外部 Alertmanager 收不到（第一次手動試跑時觀察到） |
| S1 | fake 的 `/metrics` 改回 503 → `lab_value` 消失 → NoData | 每條 rule 的狀態、外部 Alertmanager 收到的 firing／resolved |
| S2 | `docker compose stop prom` → 查詢失敗 → Error | 同上，加上 `grafana_alerting_rule_evaluation_failures_total` |
| S3 | `docker compose restart grafana`（同一個容器與 sqlite） | 狀態是否還原、`startsAt` 是否不變、有沒有送 resolved |
| S4 | 假資料來源延遲 25s（> interval 10s，< `evaluation_timeout` 30s），再改 45s（> 30s） | `grafana_alerting_schedule_rule_evaluations_missed_total`、failures、狀態 |

## 限制

- 單機、單次、單一 org、單台 Grafana；沒有測 HA（`ha_peers`）、資料庫故障、外部 Alertmanager 故障。
- 這台機器的容器時鐘不可靠：只看狀態與事件順序。`gap` 是外部 Alertmanager 的 `endsAt − updatedAt`，兩個值都用同一台主機的牆鐘，只當參考。
- 「資料來源慢」用假 Prometheus API 的 HTTP 延遲模擬，沒有用真的 Prometheus 負載。
- 沒有核對 UI 建立 rule 時的預設 `noDataState`／`execErrState`；rule 都用 provisioning 檔建立並明確寫出。
