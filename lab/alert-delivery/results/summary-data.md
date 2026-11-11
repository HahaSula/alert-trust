# alert-delivery：由 analyze.py 從 events.jsonl 算出

時間都是秒，相對於情境 (a) 開始的記號（接收端容器牆鐘）。`AM 收到` 是 Alertmanager API 的 `updatedAt`，`activeAt` 是 Prometheus API 的值。

## 事件

| 時間 | 事件 |
|---|---|
| 0.0 | 記號 a |
| 0.0 | set inst 1 = 1 |
| 2.5 | Prometheus inst 1 → pending（activeAt 1.8）（輪詢看到） |
| 6.0 | set inst 3 = 1 |
| 7.5 | Prometheus inst 3 → pending（activeAt 6.8）（輪詢看到） |
| 13.5 | Prometheus inst 1 → firing（activeAt 1.8）（輪詢看到） |
| 18.5 | Prometheus inst 3 → firing（activeAt 6.8）（輪詢看到） |
| 27.5 | 記號 b |
| 27.5 | set inst 2 = 1 |
| 38.5 | Prometheus inst 2 → pending（activeAt 36.8）（輪詢看到） |
| 49.3 | Prometheus inst 2 → firing（activeAt 36.8）（輪詢看到） |
| 54.3 | 記號 c |
| 54.3 | set inst 1 = 0 |
| 64.4 | Prometheus inst 1 → inactive（輪詢看到） |
| 84.9 | 記號 d |
| 336.1 | 記號 e-kill |
| 340.7 | Prometheus inst 2 → inactive（輪詢看到） |
| 340.7 | Prometheus inst 3 → inactive（輪詢看到） |
| 404.1 | 記號 end |

## Alertmanager A

| 通知 | 時間 | 距第一次通知 | ÷ group_interval 30s | 距上一次 | status | 內容 |
|---|---|---|---|---|---|---|
| 1 | 22.7 | 0.0 | 0.00 |  | firing | 1:firing、3:firing |
| 2 | 53.4 | 30.8 | 1.03 | 30.8 | firing | 1:firing、2:firing、3:firing |
| 3 | 84.0 | 61.3 | 2.04 | 30.6 | firing | 1:resolved、2:firing、3:firing |
| 4 | 146.1 | 123.4 | 4.11 | 62.1 | firing | 1:resolved、2:firing、3:firing |
| 5 | 209.8 | 187.1 | 6.24 | 63.7 | firing | 1:resolved、2:firing、3:firing |
| 6 | 271.2 | 248.5 | 8.28 | 61.4 | firing | 1:resolved、2:firing、3:firing |
| 7 | 335.1 | 312.5 | 10.42 | 63.9 | firing | 1:resolved、2:firing、3:firing |
| 8 | 400.6 | 377.9 | 12.60 | 65.4 | resolved | 2:resolved、3:resolved |

| inst | AM 收到（第一次 updatedAt） | startsAt | 第一次 endsAt − updatedAt |
|---|---|---|---|
| 1 | 12.7 | 11.8 | 59.1 |
| 2 | 48.4 | 46.8 | 58.3 |
| 3 | 17.7 | 16.8 | 59.1 |

API 不再列出（resolved，輪詢看到）：inst 1 64.4、inst 3 379.1、inst 2 393.7

(e) kill 在 336.1；inst 2 最後一次 updatedAt 335.1、endsAt 391.8（差 56.6）；resolved 通知 400.6，距 endsAt 8.8、距 kill 64.5、距第一次通知 12.60 × 30s

## Alertmanager B

| 通知 | 時間 | 距第一次通知 | ÷ group_interval 30s | 距上一次 | status | 內容 |
|---|---|---|---|---|---|---|
| 1 | 22.7 | 0.0 | 0.00 |  | firing | 1:firing、3:firing |
| 2 | 53.4 | 30.8 | 1.03 | 30.8 | firing | 1:firing、2:firing、3:firing |
| 3 | 84.0 | 61.3 | 2.04 | 30.6 | firing | 1:resolved、2:firing、3:firing |
| 4 | 146.0 | 123.4 | 4.11 | 62.1 | firing | 1:resolved、2:firing、3:firing |
| 5 | 209.8 | 187.1 | 6.24 | 63.7 | firing | 1:resolved、2:firing、3:firing |
| 6 | 271.2 | 248.5 | 8.28 | 61.4 | firing | 1:resolved、2:firing、3:firing |
| 7 | 335.1 | 312.5 | 10.42 | 63.9 | firing | 1:resolved、2:firing、3:firing |
| 8 | 400.6 | 377.9 | 12.60 | 65.4 | resolved | 2:resolved、3:resolved |

| inst | AM 收到（第一次 updatedAt） | startsAt | 第一次 endsAt − updatedAt |
|---|---|---|---|
| 1 | 12.7 | 11.8 | 59.1 |
| 2 | 48.4 | 46.8 | 58.3 |
| 3 | 17.7 | 16.8 | 59.1 |

API 不再列出（resolved，輪詢看到）：inst 1 64.4、inst 3 379.1、inst 2 393.7

(e) kill 在 336.1；inst 2 最後一次 updatedAt 335.1、endsAt 391.8（差 56.6）；resolved 通知 400.6，距 endsAt 8.8、距 kill 64.5、距第一次通知 12.60 × 30s

## 時鐘（接收端容器）

每 30s 單調時鐘對應的牆鐘秒數：30.91、30.76、30.56、31.11、30.95、32.73、31.00、30.80、30.64、31.20、32.73、32.71、32.72
