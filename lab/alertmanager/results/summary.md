# Alertmanager lab 結果（2026-11-08）

設定與跑法見 [README](../README.md)。所有 `group_interval` 都是 lab 為了縮短時間設的值，不代表任何環境。每個情境跑一次。

<a id="delivery"></a>

## exp-am-delivery：webhook 與 email 失敗時的 counter

三個版本各一台，`group_interval` 15s，每種失敗一個 group，觀察約 80 秒（約 5 輪 flush）。原始資料：[`delivery/final.json`](delivery/final.json)、[`delivery/timeline.json`](delivery/timeline.json)、`delivery/log-*.txt`。

### Webhook

| 接收端 | reason | 接收端收到（0.27.0／0.29.0／0.34.2） | `notifications_failed_total` | 何時加 1 |
|---|---|---|---|---|
| HTTP 400 | `clientError` | 6／6／6（每輪 1 次，不重試） | 6／6／6 | flush 一開始（log：`unrecoverable error after 1 attempts`） |
| HTTP 500 | `serverError` | 42／39／41（整輪重試） | 5／5／5 | 這輪 flush 結束時，約晚 15 秒（log：`retry canceled after 8 attempts`） |
| 連線被拒 | `other` | —（沒有連上） | 5／5／5 | 同上 |

同一段時間，webhook 的 `notification_requests_failed_total` 是 89／83／89，`notifications_failed_total` 三種 reason 合計 16／16／16。前者每次嘗試都加，後者一輪只加一次。

`timeline.json`（0.34.2，每 3 秒一筆）：`clientError` 在 t=4.7s 變成 1；`serverError`、`other` 到 t=19.8s 才變成 1，之後每 15 秒加 1。`notification_requests_failed_total` 在這段期間一直在加。

### 健康檢查

整段期間三個版本的 `/-/healthy`、`/-/ready` 都回 200（`final.json`）。

### Email：SMTP 在 DATA 結尾回 554

| 項目 | 0.27.0 | 0.29.0 | 0.34.2 |
|---|---|---|---|
| 假 SMTP 收到的 DATA 次數 | **1**（之後沒有再送） | 39 | 42 |
| `notification_requests_failed_total{integration="email"}` | **0** | 39 | 42 |
| `notifications_failed_total{integration="email"}` | **0** | 5（`other`） | 5（`other`） |
| `notifications_total{integration="email"}` | 1 | 6 | 6 |
| Log 裡和這個 receiver 有關的行 | **沒有** | 有 `554 5.7.1 rejected by fake` | 有 |

0.27.0 送了一次、被拒，卻記成成功：counter 都是 0，log 沒有任何一行，之後的 flush 也沒有再送（寫進了 nflog，`repeat_interval` 1h 內不會再試）。原始碼：0.27.0 用 `defer message.Close()`，丟掉了 DATA 結尾的回應（[email.go#L245](https://github.com/prometheus/alertmanager/blob/v0.27.0/notify/email/email.go#L245)）；0.29.0 改成回傳 `message.Close()` 的錯誤（[email.go#L246](https://github.com/prometheus/alertmanager/blob/v0.29.0/notify/email/email.go#L246)）。

<a id="window"></a>

## exp-am-window：單一 group 持續失敗時的告警視窗

### 模擬序列（promtool test rules）

`G = 1h`：單一 group 持續失敗，失敗 counter 每小時加 1（scrape 30s）。在 180–330 分鐘之間每分鐘評估一次，共 151 點。程式：[`../window-promtool/`](../window-promtool/)；原始輸出：[`window/promtool.txt`](window/promtool.txt)、`window/promtool-failures-*.txt`（每一行是一個 firing 的點）。

| 寫法 | Prometheus 2.51.2 | Prometheus 3.13.5 |
|---|---|---|
| `rate(...[5m]) > 0` 加 `for: 5m` | **0／151** | **0／151** |
| `increase(...[1h]) > 0`（視窗 = G，邊界） | 151／151 | 151／151 |
| `increase(...[2h]) > 0`（視窗 = 2G，建議） | 151／151 | 151／151 |

`rate[5m]` 只在 counter 加 1 之後的 5 分鐘內大於 0，`for: 5m` 要求連續 5 分鐘成立，所以永遠到不了 firing。`[1h]` 剛好等於 G，在這份整分鐘、沒有抖動的序列上全部成立；scrape 有抖動或評估時間落在邊界上時可能短暫歸零，這份測試沒有涵蓋。

### 實機（三個版本，`group_interval` 120s）

單一 group 回 400（不重試），Prometheus 3.13.5 每 1s 抓三台，評估間隔 5s，觀察約 500 秒。規則是縮小的版本：`rate(...[20s]) > 0` 加 `for: 20s`，對照 `increase(...[240s]) > 0`（2 倍 `group_interval`）。原始資料：[`window/prometheus.json`](window/prometheus.json)、[`window/timeline.json`](window/timeline.json)。

| 項目 | 0.27.0 | 0.29.0 | 0.34.2 |
|---|---|---|---|
| `clientError` counter 改變的時間（秒） | 2、136、269 | 2、135、268 | 2、136、269 |
| `rate[20s]` 加 `for: 20s` | 只有 pending，**從未 firing** | 同左 | 同左 |
| `increase[240s]` | 第一次失敗後一直 firing | 同左 | 同左 |

Counter 一輪 flush 只加 1，三個版本相同。實際間隔是 133 秒而不是 120 秒，最後約 230 秒也沒有再加；三台同時如此，原因沒有查清楚（這台 WSL2 主機的時鐘也有其他異常，見 README），不影響「一輪只加 1、短視窗加 `for` 抓不到」的結論。

<a id="smtp"></a>

## exp-am-smtp：SMTP 不回應、模板失敗

三個版本各一台，`group_interval` 10s。原始資料：[`smtp/observations.json`](smtp/observations.json)、[`smtp/mails.json`](smtp/mails.json)、`smtp/log-*.txt`。三個版本每一步的數字相同，除非另外註明。

### SMTP 收完信不回覆

`Hang` receiver 裡有 email（連到不回覆的 SMTP）和 webhook。

| 時間 | 事件 | 觀察 |
|---|---|---|
| t=13.6s | 第一輪 flush | webhook 收到 1 次；email 連線卡住 |
| | 同 group 加第二條 alert | |
| t=47.0s | 加入後 30 秒 | webhook 仍是 1 次 |
| t=82.3s | 加入後 62 秒 | webhook **仍是 1 次**：整個 group 停止，同 receiver 的 webhook 也收不到 |
| t=82.3s | 另一個 group 30 秒前加了新 alert | 收到了（1 → 2 次），其他 group 不受影響 |
| 整段 | `/-/healthy`、`/-/ready` | 都是 200 |
| t=88.4s | `POST /-/reload`（同一份設定）後 6 秒 | reload 回 200，webhook 馬上收到第 2 次；email 又連到同一台 SMTP，再卡住一條連線 |
| t=94.4s | 假 SMTP 關掉卡住的連線後 6 秒 | 0.29.0、0.34.2：`notifications_failed_total` 多了 `contextCanceled` 1、`contextDeadlineExceeded` 1。0.27.0：只多了 `contextCanceled` 1；`notification_requests_total` 加 4 而 `requests_failed` 只加 2（那 2 次是模板 group），也就是**卡住的 2 次嘗試被記成成功** |

卡住期間 counter 有沒有動：所有 email receiver 共用 `integration="email"`。`notifications_failed_total{reason="other"}` 在 t=82.3s 是 14，和模板 group 寄出的信數（14 封）相同，所以卡住的 group 對失敗 counter 的貢獻是 0。只觀察約 90 秒；會不會一直卡下去沒有實測。

### 模板執行失敗

| Receiver | 假 SMTP 收到的信 | 失敗計數 |
|---|---|---|
| 主旨引用不存在的模板 | 每輪一封，內容只有一個空行（2 bytes），沒有任何標頭 | 每輪 `notifications_failed_total{reason="other"}` 加 1 |
| 內文引用不存在的模板 | 每輪一封，有主旨與標頭，沒有內文 | 同上 |

約 94 秒內兩個 group 共寄出 18 封（每輪各 1 封），沒有重試，也沒有寫進 nflog，所以每一輪都再寄一次。`amtool check-config` 在三個版本對這份設定（`configs/smtp.yml`）都回 SUCCESS。0.27.0 的原始碼在送出 DATA（[email.go#L241](https://github.com/prometheus/alertmanager/blob/v0.27.0/notify/email/email.go#L241)）之後才執行標頭模板（L251）。

<a id="ha"></a>

## exp-am-ha：叢集、reload、重啟

兩台 0.34.2，`group_interval` 10s。原始資料：[`ha/steps.json`](ha/steps.json)、`ha/log-*.txt`。

| 步驟 | 觀察 |
|---|---|
| 組成叢集 | 兩台 `cluster_members` = 2 |
| 同一條 alert 送給兩台 | webhook 只收到 1 次（am1 送 1、am2 送 0） |
| `docker compose stop am2` 後 5–40 秒 | am1：`cluster_members` = 1、`cluster_failed_peers` = 1、`cluster_peers_left_total` = 1，`/-/healthy` 200 |
| 套用 `AlertmanagerMembersInconsistent` 的比較 | 只剩 am1 被抓到：members [1]，count 1，1 < 1 不成立，**不會觸發** |
| am1 reload 無效設定（receiver 不存在） | 回 500 `undefined receiver "does-not-exist" used in route`；`config_last_reload_successful` = 0；`config_hash` 不變 |
| 之後送新 alert `HaB` | 照舊設定送到 `b`：舊設定繼續執行 |
| 改回正確設定再 reload | 200，`config_last_reload_successful` = 1 |
| `docker compose restart am1`（保留資料目錄），重送同樣的 alert | 20 秒內沒有再通知：nflog 從快照恢復 |
| 對照：SIGKILL 後換新的資料 volume，重送同樣的 alert | 再通知一次 |
| 重啟後的 am1（am2 仍停機） | `cluster_failed_peers` 回到 **0**，`cluster_members` = 1 |
| 混版：am1（0.34.2）加一台 0.27.0 | 兩台 `cluster_members` = 2；同一條 alert 送給兩台，webhook 收到 1 次；log 沒有錯誤 |

`cluster_failed_peers` 只記得這次行程啟動後見過的 peer。存活的那台重啟之後，停機的 peer 不再被計入（這個 lab 裡停掉的容器 DNS 名稱也解析不到），`AlertmanagerClusterFailedPeers` 會跟著解除，只剩 `up == 0` 抓得到。

沒有測：網路分割、SIGKILL 的 peer、`changes(process_start_time_seconds)`（這台主機上它沒有重啟也會跳動）。
