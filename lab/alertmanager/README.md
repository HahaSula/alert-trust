# Alertmanager：通知失敗時看得到什麼

驗證 [Alertmanager 元件卡](../../catalog/components/alertmanager.yaml)與[架構頁](../../docs/architecture/alertmanager.md)裡標為實測的結論。全部用官方映像檔加上假的接收端，單機 Docker，不需要真的 SMTP 或 webhook 服務。

結果與數字：[results/summary.md](results/summary.md)。

## 需要

- Docker 與 docker compose v2、Python 3（只用標準函式庫）
- 映像檔：`prom/alertmanager:v0.27.0`、`v0.29.0`、`v0.34.2`，`prom/prometheus:v2.51.2`、`v3.13.5`，`python:3.12.7-alpine`
- 主機 port：21080–21082、21090、21093–21095、21180、21193–21195

## 怎麼跑

```bash
python3 run.py delivery        # exp-am-delivery，約 2 分鐘
python3 run.py smtp            # exp-am-smtp，約 2 分鐘
./window-promtool/run.sh       # exp-am-window 的模擬序列（promtool test rules），幾秒
python3 run.py window          # exp-am-window 的實機部分，約 9 分鐘
python3 run.py ha              # exp-am-ha，約 3 分鐘
```

每個情境結束時都會 `docker compose down -v`。原始輸出寫在 `results/<情境>/`。

## 組成

| 檔案 | 內容 |
|---|---|
| `fake.py` | 假接收端，一個行程同時開：HTTP webhook（`/status/<code>` 回指定狀態碼）、正常收信的 SMTP（:2525）、DATA 結尾回 554 的 SMTP（:2526）、收完信不回覆的 SMTP（:2527）。`/stats` 看收到幾次，`/release` 關掉卡住的連線 |
| `compose.yml` | 三個版本各一台 Alertmanager 配一個假接收端，各自在獨立的 network；`window` profile 多一台 Prometheus 3.13.5 每 1s 抓三台 |
| `compose-ha.yml` | 兩台 0.34.2 組成叢集；`mixed` profile 再加一台 0.27.0 |
| `configs/` | 各情境的 Alertmanager 設定、Prometheus 設定與告警規則 |
| `run.py`、`ha_lab.py` | 驅動程式：用 `/api/v2/alerts` 直接送 alert、定時讀 `/metrics` 與假接收端的 `/stats` |
| `window-promtool/` | 產生模擬序列與 151 個評估點的 `promtool test rules` 測試 |

Alert 直接 POST 到 Alertmanager，不經過 Prometheus，所以 alert 不會被重送；同一個 group 的 alert 變了才會有新通知。

## 設定（lab 專用，不代表任何環境）

| 情境 | group_wait | group_interval | repeat_interval |
|---|---|---|---|
| delivery | 1s | 15s | 1h |
| smtp | 1s | 10s | 1h |
| window（實機） | 1s | 120s | 1h |
| ha | 1s | 10s | 1h |

`group_interval` 刻意縮短，讓每輪 flush 在幾十秒內就看得到。window 實機部分把「`group_interval` 30m、`rate[5m]` 加 `for: 5m`」等比例縮小 15 倍成「120s、`rate[20s]` 加 `for: 20s`」。

## 限制

- 每個情境只跑一次，規模很小，沒有負載。
- 假 SMTP 只模擬三種行為（正常、DATA 結尾 554、收完信不回覆）。沒有測 STARTTLS、真實郵件伺服器、TCP 對端整個消失，也沒有測 SMTP 回 250 之後才退信。
- SMTP 不回應只觀察約 90 秒；「會一直卡住」是依原始碼推論，不是實測。
- 所有 email receiver 共用 `integration="email"` 這個 label，所以 `smtp` 情境裡卡住的 group 與模板失敗的 group 計在同一個 counter，要用差值判斷（見 summary）。
- HA 情境的停機是 `docker compose stop`（SIGTERM，peer 會正常離開）；沒有測網路分割或 SIGKILL 的 peer。
- 在這台 WSL2 主機上，`process_start_time_seconds` 沒有重啟也會以 3 秒為單位跳動，所以沒有用它驗證 `changes(process_start_time_seconds[15m])`。
