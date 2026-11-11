# alert-delivery：由 analyze.py 從 events.jsonl 算出

時間都是秒，相對於情境 (a) 開始的記號（接收端容器牆鐘）。`AM 收到` 是 Alertmanager API 的 `updatedAt`，`activeAt` 是 Prometheus API 的值。

## 事件

| 時間 | 事件 |
|---|---|
| 0.0 | 記號 a |
| 0.0 | set inst 1 = 1 |
| 8.1 | Prometheus inst 1 → pending（activeAt 6.1）（輪詢看到） |
| 18.2 | Prometheus inst 1 → firing（activeAt 6.1）（輪詢看到） |
| 288.0 | 記號 end |

## Alertmanager C

| 通知 | 時間 | 距第一次通知 | ÷ group_interval 30s | 距上一次 | status | 內容 |
|---|---|---|---|---|---|---|
| 1 | 28.3 | 0.0 | 0.00 |  | firing | 1:firing |
| 2 | 120.8 | 92.6 | 3.09 | 92.6 | firing | 1:firing |
| 3 | 215.1 | 186.9 | 6.23 | 94.3 | firing | 1:firing |

| inst | AM 收到（第一次 updatedAt） | startsAt | 第一次 endsAt − updatedAt |
|---|---|---|---|

## Alertmanager D

| 通知 | 時間 | 距第一次通知 | ÷ group_interval 30s | 距上一次 | status | 內容 |
|---|---|---|---|---|---|---|
| 1 | 28.3 | 0.0 | 0.00 |  | firing | 1:firing |
| 2 | 151.8 | 123.5 | 4.12 | 123.5 | firing | 1:firing |
| 3 | 277.2 | 249.0 | 8.30 | 125.4 | firing | 1:firing |

| inst | AM 收到（第一次 updatedAt） | startsAt | 第一次 endsAt − updatedAt |
|---|---|---|---|

## 時鐘（接收端容器）

每 30s 單調時鐘對應的牆鐘秒數：31.16、30.98、30.81、30.61、31.14、30.98、32.72、30.59、31.13
