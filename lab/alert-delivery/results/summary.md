# alert-delivery 結果

執行：Prometheus 3.13.5、Alertmanager 0.29.0（兩台，不組叢集）、接收端 `recv.py`，單機 Docker Desktop（WSL2）。原始資料：`events.jsonl`、`payloads.jsonl`、`snapshots/`；`analyze.py` 算出的完整表在 `summary-data.md`、`rounding/summary-data.md`。

**時鐘**：時間是秒，從情境 (a) 開始的記號起算，用接收端容器的牆鐘（`events.jsonl` 的 `t`）；Alertmanager 收到的時間用 API 的 `updatedAt`，EndsAt 用 API 的 `endsAt`，Prometheus 看到的時間用 `activeAt`。四個容器共用同一個 VM 牆鐘。

設定：`scrape_interval`、`evaluation_interval` 5s，`for: 10s`，`--rules.alert.resend-delay=15s`；`group_by: [alertname, scen]`、`group_wait: 10s`、`group_interval: 30s`；`repeat_interval` A 1m、B 45s（主要執行），C 75s、D 105s（rounding）。

## group_wait {#group-wait}

| 事件 | inst 1 | inst 3 |
|---|---|---|
| gauge 設成 1 | 0.0 | 6.0 |
| `activeAt` | 1.8 | 6.8 |
| `startsAt`（firing） | 11.8 | 16.8 |
| Alertmanager `updatedAt` | 12.7 | 17.7 |
| 通知 | 22.7 | 同一則 |

- 第一次通知在 Alertmanager 收到後 10.0s（`group_wait` 10s），兩台相同。
- inst 3 在等待期間到，同一則送出（`payloads.jsonl` 第一筆有 2 個 alert）。

## group_interval {#group-interval}

| 事件 | 時間 | 距第一次通知 |
|---|---|---|
| 第一次通知 | 22.7 | 0 |
| inst 2 設成 1 | 27.5 | |
| inst 2 `activeAt` | 36.8 | |
| inst 2 `updatedAt` | 48.4 | |
| 通知（inst 1、2、3 firing） | 53.4 | 30.8（tick 1） |
| inst 1 設成 0 | 54.3 | |
| inst 1 從 `/api/v2/alerts` 消失（1s 輪詢） | 64.4 | |
| 通知（inst 1 resolved） | 84.0 | 61.3（tick 2） |

- 加入已在響的 group：Alertmanager 收到後等 5.0s，到 tick 才送。
- Resolved：等 19.6s，到 tick 才送。
- 之後的重送仍帶著 inst 1 的 resolved：Prometheus 在 15 分鐘內會繼續送 resolved，alert 被重新寫回 group。

## repeat_interval {#repeat}

| 設定 | `repeat_interval` | 兩次通知的間隔（s） | tick 數 |
|---|---|---|---|
| B | 45s | 62.1、63.7、61.4、63.9 | 2 |
| C | 75s | 92.6、94.3 | 3 |
| D | 105s | 123.5、125.4 | 4 |
| A | 1m | 62.1、63.7、61.4、63.9 | 2 |

- 重送只落在 tick 上；45s、75s、105s 都是 ⌈R ÷ 30s⌉ 個 tick。
- A（1m，剛好 2 個 tick）：原始碼預期 3 個 tick（通知紀錄的時間戳記在送出之後才寫，比 tick 晚）。這裡量到 2 個，原因是下面的時鐘：2 個 tick 以牆鐘看是 61–64s，已經超過 60s。所以這一點只有讀碼的結論。

## EndsAt 到期 {#endsat}

| 事件 | 時間 |
|---|---|
| inst 2 的 POST（`updatedAt`） | 250.6、271.2、292.4、312.4、335.1 |
| kill Prometheus | 336.1 |
| inst 2 最後一次 `endsAt` | 391.8（距最後一次 `updatedAt` 56.6s；`endsAt` = 評估時間 + 60s） |
| inst 2 從 API 消失 | 393.7 |
| resolved 通知（inst 2、3） | 400.6（kill 後 64.5s、EndsAt 後 8.8s） |

- 續送間隔 20–23s，不是 15s：要等「上次 + 15s」之後的評估，E 5s 時是 20s。
- 續送期間 Alertmanager 只在 `repeat_interval` 到期時重送（271.2、335.1），續送本身不觸發通知。
- inst 3 最後一次 POST 在 317.4、EndsAt 376.8，379.1 消失，等到同一個 tick（400.6）送出。

## 時鐘 {#clock}

`recv.py` 每 30s（單調時鐘）記一次對應的牆鐘秒數：

- 主要執行：30.91、30.76、30.56、31.11、30.95、32.73、31.00、30.80、30.64、31.20、32.73、32.71、32.72
- rounding：31.16、30.98、30.81、30.61、31.14、30.98、32.72、30.59、31.13

Alertmanager 的 tick 也一樣：以牆鐘看每個 30s 的 tick 是 30.6–32.7s。延遲都用牆鐘量，所以 tick 間隔偏長；需要精確到秒以下的結論（`repeat_interval` 整數倍）改用原始碼。

## 清理

兩次執行結束都跑了 `docker compose down -v`；之後 `docker ps -a`、`docker network ls`、`docker volume ls` 都沒有 `at-ad` 開頭的項目。
