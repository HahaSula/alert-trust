# alert-delivery：從觸發到通知的每一段等待

驗證[從觸發到通知](../../docs/architecture/alert-delivery.md)裡標為實測的結論：`group_wait`、`group_interval`、`repeat_interval` 什麼時候生效，以及 Prometheus 停掉後 EndsAt 到期的 resolved 時間。

結果與數字：[results/summary.md](results/summary.md)。原始輸出：`results/events.jsonl`（所有事件）、`results/payloads.jsonl`（webhook 原始 payload）、`results/snapshots/`（各情境結束時兩台 Alertmanager 的 `/api/v2/alerts` 與 `/api/v2/alerts/groups`）、`results/compose-logs.txt`、`results/summary-data.md`（`analyze.py` 算出的表）。

## 需要

- Docker 與 docker compose v2、Python 3（只用標準函式庫）
- 映像檔：`prom/prometheus:v3.13.5`、`prom/alertmanager:v0.29.0`、`python:3.12.7-alpine`（都是 repo 其他 lab 已用過的版本）
- 主機 port：35080（接收端）、35090（Prometheus）、35093–35094（兩台 Alertmanager）

## 怎麼跑

```bash
python3 run.py            # 情境 (a)–(e)，約 10 分鐘
python3 run.py rounding   # repeat_interval 75s 與 105s，約 5 分鐘，結果在 results/rounding/
python3 analyze.py        # 只重算 results/summary-data.md（rounding 加參數 rounding）
```

兩個指令結束時都會 `docker compose down -v`（compose project `at-ad`）。

## 組成

| 檔案 | 內容 |
|---|---|
| `recv.py` | 接收端，一個行程同時是：受控的 exporter（`at_ad_flag{scen="x",inst="1..3"}`，`/set` 改值）、webhook 接收端、每 1s 輪詢兩台 Alertmanager 與 Prometheus API 的記錄器；另每 30s 記一次牆鐘與單調時鐘的差 |
| `compose.yml` | Prometheus 3.13.5（`--rules.alert.resend-delay=15s`）送給兩台 Alertmanager 0.29.0（不組叢集），兩台都送到同一個接收端 |
| `configs/rules.yml` | `DeliveryTest`：`at_ad_flag > 0`、`for: 10s`、group `interval: 5s` |
| `configs/am-a.yml` … `am-d.yml` | `group_by: [alertname, scen]`、`group_wait: 10s`、`group_interval: 30s`；`repeat_interval` 依序 1m、45s、75s、105s |
| `run.py` | 驅動程式：改 gauge、等通知、存 API 快照、最後 `docker compose kill prom` |
| `analyze.py` | 從 `events.jsonl` 算出每則通知距第一次通知幾秒、是第幾個 tick |

## 情境

| 情境 | 做什麼 | 要看的 |
|---|---|---|
| (a) 新 group | inst 1 設 1，6s 後 inst 3 設 1 | 第一次通知 ≈ AM 收到 + `group_wait`；inst 3 在等待期間到，同一則送出 |
| (b) 加入已在響的 group | 第一次通知約 5s 後 inst 2 設 1 | inst 2 等到下一個 `group_interval` tick |
| (c) resolved | inst 1 設 0 | resolved 等到下一個 tick |
| (d) 不變的 group | 等 240s | 重送只落在 tick 上；45s 與 1m 各隔幾個 tick |
| (e) ruler 停掉 | kill Prometheus | EndsAt = 最後一次送出 + 4 × max(15s, 5s)；之後的第一個 tick 送 resolved |
| rounding | 只有 inst 1，等 250s | 75s → 每 3 個 tick；105s → 每 4 個 tick |

## 時鐘

所有延遲都用**接收端容器的牆鐘**（`events.jsonl` 的 `t`）與 **Alertmanager／Prometheus API 的時間戳記**（`updatedAt`、`startsAt`、`endsAt`、`activeAt`）。四個容器在同一台 Docker Desktop VM 上，共用同一個牆鐘。主機上的 `run.py` 只下指令，不用主機時鐘算任何數字。

這台 VM 的單調時鐘比牆鐘慢（`recv.py` 每 30s 記一次，見 summary）。Alertmanager 的 `group_interval` 計時器走單調時鐘，比對 `repeat_interval` 卻用牆鐘，所以這裡每個 tick 以牆鐘看會超過 30s。這讓「`repeat_interval` 剛好是 `group_interval` 整數倍」的情況在這台機器上量不出原始碼的結果（見 summary 的 repeat 一節）；75s、105s 不是整數倍，不受影響。

## 限制

- 每個情境只跑一次，單一 group、單一 receiver、沒有負載。
- 兩台 Alertmanager 不組叢集（`--cluster.listen-address=`），沒有 HA 的等待；HA 的順位等待只讀碼。
- 沒有測 vmalert；vmalert 的部分只讀碼（v1.155.0）。
- 輪詢間隔 1s，「Prometheus 狀態改變」「API 不再列出」的時間有最多 1s 誤差；`updatedAt` 與通知時間沒有這個誤差。
