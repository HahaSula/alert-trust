# VictoriaMetrics／VictoriaLogs 輕量 lab

重做 VM 系列元件卡與信任契約引用的實驗。三個情境，各自一個 compose project，都是小容器、沒有負載。

| 目錄 | 情境 | 版本 | 時間 |
|---|---|---|---|
| `matrix/` | 延遲矩陣：資料間隔 I、寫入延遲 L 對 `rate(m[2m])` alert 的影響（契約 `R ≥ I + L − D`），另含官方 ServiceDown 的 job regex | VM single v1.155.0、vmalert v1.155.0 | 每輪 10–13 分鐘 |
| `faults/` | VM cluster 故障矩陣（3 台 vmstorage，RF=1／RF=2）與 `-rule.resendDelay` 比較 | vmstorage／vminsert／vmselect v1.155.0-cluster、vmagent、vmalert v1.155.0、Alertmanager v0.27.0 | rf1 19 分、rf1b 22 分、rf2 22 分 |
| `vl/` | VictoriaLogs：log rule 視窗、空資料的 `stats count()`、Content-Type、cluster 缺 vlstorage、single 唯讀 | VictoriaLogs v1.53.0、vmalert v1.155.0、Alertmanager v0.27.0 | 16 分＋6 分 |

假 target、log 產生器都用 `python:3.12.7-alpine` 跑標準函式庫腳本。

## 執行

需要 Docker（compose v2）與 host 上的 python3（只用標準函式庫）。Host port 只用 20010–20067。

```bash
cd lab/vm
TAG=for1m python3 matrix/run.py          # for: 1m
TAG=for2m python3 matrix/run.py          # for: 2m，另記錄 rate 回 0 的次數
TAG=debug DURATION=600 python3 matrix/run.py && python3 matrix/check_evals.py   # 逐次 evaluation 比對
python3 faults/run.py rf1                # RF=1：一台 vmstorage 掛、vmselect 掛（同時比較 resendDelay）
python3 faults/run.py rf1b               # RF=1：全部 vmstorage 唯讀、vminsert 掛
python3 faults/run.py rf2                # RF=2：一台 vmstorage 掛、vmagent 掛
python3 vl/run.py                        # VL 全部情境
python3 vl/run.py readonly               # 只重跑 VL single 唯讀
python3 vl/reachable.py && python3 vl/reachable.py summarize   # 停 1 台 vlstorage 時哪個 metric 會變（約 4 分鐘）
python3 vl/ingested.py                   # 3 種 Content-Type 的寫入請求數與收進筆數（約 30 秒）
python3 vl/summarize.py > vl/results/vl-logdelay.json
python3 transitions.py faults/results/faults-rf1.jsonl   # 把取樣轉成「故障後第幾秒什麼改變」
```

各情境可以平行跑（project 名稱與 port 不重疊）。腳本結束時會 `docker compose down -v`；中斷時手動跑
`docker compose -p <project> --profile gen down -v`，project 名稱是 `at-vm-matrix`、`at-vm-faults-rf1`、`at-vm-faults-rf1b`、`at-vm-faults-rf2`、`at-vm-vl`。

## 怎麼量

- Driver 每 5 秒抓一次 vmalert `/api/v1/alerts`、`/api/v1/rules`、Alertmanager `/api/v2/alerts`、各元件 `/metrics`，寫成 `results/*.jsonl`。時間誤差約 ±5 秒。
- 「假 resolved」＝條件其實仍成立（資料沒變），但 alert 從 Alertmanager 的 active 清單消失。
- 兩個 vmalert 查同一個 vmselect：`vmalert=default`（預設）與 `vmalert=resend3m`（`-rule.resendDelay=3m`）。
- 唯讀用調高 `-storage.minFreeDiskSpaceBytes` 後重建容器來觸發；資料放在具名 volume，重建不會遺失。
- 每個情境的數字與判讀在下方「結果」；原始取樣在各目錄的 `results/`，`*-transitions.txt` 是逐秒變化清單。

## 限制（讀結果前先看）

- **容器內的時鐘**：這台機器（Docker Desktop on WSL2）容器內的 monotonic 時鐘比牆上時鐘慢，實測 10 秒的 `sleep` 在牆上是 10–13 秒。vmalert 的 30s group `interval` 在牆上約 33 秒，查詢時間 `ts` 落後牆上時間 50–77 秒，而不是設定的 30 秒（matrix 的 debug log 可以看到）。所有以「故障後幾秒」表示的數字都是牆上時間，在正常機器上應該略短。延遲矩陣因此改用逐次 evaluation 比對（`check_evals.py`），不只看 alert 有沒有 firing。
- 每個故障只跑一次，持續 3–7 分鐘，比原始研究短；只能說明機制與量級，不是統計。
- 流量極低（30 條 series、10 秒 scrape）。和流量有關的現象（例如 vmagent 的 pending bytes）不能外推。
- log 產生器被拒絕（429）時不重送，真正的 shipper 通常會重送。
- 沒有測：查詢飽和（429）、磁碟真的寫滿時的 panic、vmalert／Alertmanager 本身掛掉、VL `allow_partial_response`、operator。

## 結果：延遲矩陣（`matrix/`）

條件：永遠成立的 `rate(lab_m[2m]) > 0`（R = 120s），group `interval` 30s，`-rule.evalDelay` 30s（D）。I ∈ {30, 60, 120}s，L ∈ {0, 60, 120, 180}s，共 12 格；對照組 `rate(lab_m[5m])`。原始資料：`matrix/results/`。

**逐次 evaluation 比對**（`TAG=debug`，`matrix-debug-check.json`）：61 次 evaluation，用每筆 sample 的實際寫入時間預測「視窗 (ts − R, ts] 內有沒有已寫入的 sample」，與 vmalert 回傳筆數 **0 次不符**（另有 9 次寫入時間與 evaluation 相差不到 2 秒，不計）。Rule 回空的原因就是視窗內還沒有寫入的 sample。

**第一次 firing 的秒數**（從資料開始產生算起；— 表示整輪沒有 firing，括號是 pending 被清掉的次數）：

| I＼L | 0 | 60 | 120 | 180 |
|---|---|---|---|---|
| for 1m，13 分鐘 | | | | |
| 30 | 208 | 208 | 275 | —（2） |
| 60 | 275 | 275 | 308 ⚠ | —（1） |
| 120 | 375 | 375 ⚠ | —（3） | —（1） |
| for 2m，13 分鐘 | | | | |
| 30 | 262 | 262 | 295 | —（2） |
| 60 | 295 | 295 | 328 ⚠ | —（1） |
| 120 | 362 | 362 ⚠ | —（4） | — |
| for 1m，debug 輪，10 分鐘 | | | | |
| 30 | 195 | 195 | 262 | 562 ⚠ |
| 60 | 262 | 262 | 295 ⚠ | —（1） |
| 120 | 295 | 295 ⚠ | —（3） | — |

- 違反 `R ≥ I + L − D`（D = 30，即 I + L > 150）的有 6 格。滿足契約的 6 格三輪都 firing；(60,180)、(120,120)、(120,180) 三輪都沒有 firing。
- ⚠：違反契約仍 firing。(60,120)、(120,60) 只超出 30 秒；這台機器上查詢時間落後牆上 50.3–77.0 秒（debug log 量到），比設定的 30 秒大，可見 sample 的範圍跟著變大。逐次比對顯示這些 evaluation 也都如預測。
- vmalert 的查詢時間是「現在 − `evalDelay`」再對齊到 group `interval`（[group.go#L390](https://github.com/VictoriaMetrics/VictoriaMetrics/blob/v1.155.0/app/vmalert/rule/group.go#L390)）（[group.go#L715](https://github.com/VictoriaMetrics/VictoriaMetrics/blob/v1.155.0/app/vmalert/rule/group.go#L715)），正常機器上實際延遲也落在 D 到 D + E 之間，取決於 group 的啟動相位（由 group ID 決定（[group.go#L528](https://github.com/VictoriaMetrics/VictoriaMetrics/blob/v1.155.0/app/vmalert/rule/group.go#L528)））。所以用設定的 D 算出的契約是保證條件；違反時能不能成立看相位，不能依賴。
- `rate[5m]` 對照組 12 格全部 firing。
- for 2m 這輪另有 `rate(lab_m[2m]) == 0`，整輪 0 次成立：視窗內沒有 sample 時 VM 的 `rate` 回空，不是 0。

**官方 ServiceDown 的 job regex**：VM single 用 job 名稱 `vm` 抓自己。`up{job="vm"}` 有 1 條；官方 ServiceDown 的選擇器 `up{job=~".*(victoriametrics|vmselect|…|vmauth).*"}` 是 0 條，這條規則永遠不會觸發，也沒有錯誤。只查了選擇器，沒有讓 VM 停機。

## 結果：VM cluster 故障矩陣（`faults/`）

條件：3 台 vmstorage、30 條 series、scrape 10s、group `interval` 30s、`for: 1m`、`-datasource.queryStep` 預設 5m。時間從故障開始算（牆上時間，±5 秒）。原始資料：`faults/results/faults-*.jsonl` 與 `*-transitions.txt`。

| 故障（持續） | Rule 與 alert | 元件 metric |
|---|---|---|
| RF=1，1 台 vmstorage 停（240s） | 每條 series 的 alert 30 → 18（+11s），+139s 才回到 30；`sum(increase(m[3m])) < 4500` 假觸發 +86s 到 +152s；rule 沒有錯誤 | `vm_partial_results_total` +11s 開始增加（期間 +78）；`vm_rpc_vmstorage_is_reachable` 合計 3 → 2（+6s） |
| 同上，group `params: {denyPartialResponse: ["true"]}`（文件寫法） | 行為和預設相同：同樣假觸發（+72s 到 +139s），沒有錯誤 | |
| 同上，`params: {deny_partial_response: ["1"]}` | 沒有假觸發；+34s 起 rule 變 ERR，直到節點回來 | `vmalert_alerting_rules_errors_total` 增加 |
| RF=2，1 台 vmstorage 停（240s） | 沒有任何 alert 變化，沒有錯誤 | partial 0；reachable 3 → 2 |
| vmselect 停（240s），預設 `resendDelay` | 所有 rule +16s 變 ERR；+101s Alertmanager 把 Watchdog、CondAlive、Heartbeat、30 條 ItemUp 全部標成 resolved（假 resolved）；vmselect 回來 +5s 再 firing | 錯誤數期間 +49 |
| 同上，`-rule.resendDelay=3m` | 240s 內 Alertmanager 沒有任何 alert 消失，Watchdog 也沒斷 | |
| 全部 vmstorage 唯讀（240s） | 沒有 rule 錯誤；Heartbeat（`time() − max(timestamp(m)) < 120`）+183s resolved；條件型 alert 到唯讀結束（240s）仍 firing | `vm_storage_is_read_only` 全部 1（+2s） |
| vminsert 停（400s） | 沒有 rule 錯誤；Heartbeat +154s resolved；條件型 alert 與 30 條 ItemUp +354s 才 resolved | `vmagent_remotewrite_pending_data_bytes` 最高 1366 |
| vmagent 停（400s，RF=2） | 沒有 rule 錯誤；Heartbeat +149s；條件型 alert +349s resolved | |

補充：

- 寫入故障後資料會補回、rule 不補判：vminsert 停 400 秒後查同一段時間，`lab_item{id="0"}` 有 36 筆（scrape 10s，容器時鐘偏慢，約等於全部）；vmagent 停的那段是 0 筆，vmagent 自己停就沒有資料可補。
- 「數量太低」的 `increase()` 規則在資料完全停止時先觸發，約 4 分鐘後因為視窗內沒有 sample 而回空、自動 resolved（vmagent 停 +196s firing、+229s resolved）。
- `-rule.resendDelay=3m` 的副作用：條件消失後，有 5／23 次 Alertmanager 晚了 134–200 秒才把 alert 標成 resolved；預設值 0／23 次。推論是 vmalert 用對齊後的查詢時間當 `ResolvedAt`（[alerting.go#L585](https://github.com/VictoriaMetrics/VictoriaMetrics/blob/v1.155.0/app/vmalert/rule/alerting.go#L585)），只有比上次送出時間晚才立即送 resolved（[alerting.go#L883](https://github.com/VictoriaMetrics/VictoriaMetrics/blob/v1.155.0/app/vmalert/rule/alerting.go#L883)），否則要等下一次續送。這台機器查詢時間落後較多，比例可能偏高。
- `vmalert_iteration_missed_total` 在健康階段也每 6–7 分鐘增加 4（每個 group 1 次），是這台機器的時鐘修正造成，不是故障訊號。這次沒有測查詢飽和。

## 結果：VictoriaLogs（`vl/`）

條件：VL v1.53.0。log rule 的 group `interval` 30s（E），`-rule.evalDelay` 30s（D），沒有 `for`。事件每 67 秒一筆（不是 30 的倍數，相位會變），帶 log 延遲 L。原始資料：`vl/results/`。

| 項目 | 結果 |
|---|---|
| 空的 VL 上 `* \| stats count() c` | single、cluster 都回 `{"c":"0"}`；加 `\| filter c:>0` 回空 |
| Rule `kind:nonexistent \| stats count() c`（沒有任何符合的 log） | 整輪持續 firing；加 `\| filter c:>0` 的版本從未 firing |
| 寫入不帶 Content-Type | 200，`vl_rows_ingested_total` +5，查得到 5 筆 |
| 寫入帶 `Content-Type: application/x-www-form-urlencoded`（Python urllib 的預設） | **200，`vl_rows_ingested_total` +0，查不到** |
| 寫入帶 `application/stream+json` | 200，+5，查得到 |
| Cluster 停 1 台 vlstorage（240s） | 查詢 +7s 起回 502；cluster 的 log rule +32s 變 ERR；+122s Alertmanager 把 canary 標成 resolved（假 resolved），vmalert 裡仍是 firing；寫入仍回 200；`vl_insert_remote_is_reachable` 沒有變化（原因見下方重測：這一輪只寫 1 條 stream，剛好不在停掉的那台） |
| Single 唯讀（`-storage.minFreeDiskSpaceBytes` 調高，150s） | `vl_storage_is_read_only` = 1；寫入回 **429**（`cannot add rows into storage in read-only mode`）；rule 沒有錯誤 |

Log rule 視窗（`vl-logdelay.json`，唯讀前 11.7 分鐘）：

| Rule | L | 事件數 | 觸發次數 |
|---|---|---|---|
| `kind:event lag:0 \| stats count() c \| filter c:>0` | 0 | 9 | 8 |
| 同上，lag:20 | 20s | 9 | 8 |
| 同上，lag:45 | 45s | 9 | 8 |
| 同上，lag:90 | 90s | 8 | **0** |
| `_time:10m kind:event lag:90 …` | 90s | 8 | 1 次，持續 firing 涵蓋全部 |

- L = 90s > D + E 時一次都沒觸發，但 log 有寫進去（同一批 log 用 `_time:10m` 的版本有觸發）。沒有 `_time` 的 log rule 只查 `[ts − interval, ts]`（[client_vlogs.go#L20](https://github.com/VictoriaMetrics/VictoriaMetrics/blob/v1.155.0/app/vmalert/datasource/client_vlogs.go#L20)）。
- L = 45s 介於 D 與 D + E 之間，理論上要看相位；這台機器實際延遲 50–77 秒，所以和 L ≤ D 一樣都觸發。
- L = 0、20、45 都漏了同一筆事件（第 4 筆），原因沒有找到；VL 的 `start`／`end` 邊界另外測過是包含的。
- 第一次跑的唯讀階段因容器重建遺失資料而作廢（`vl.jsonl` 的 readonly 段），改用具名 volume 重跑（`vl-readonly.jsonl`）。

### 重測：停 1 台 vlstorage 時哪個 metric 會變（`vl/reachable.py`）

上表 `vl_insert_remote_is_reachable` 沒有變化，但 `victorialogs#storage-reachable` 原本用它當第 1 級訊號，所以單獨重測。只起 2 台 vlstorage 與 vlfront（兼 vlinsert／vlselect），driver 每 5 秒寫一批 log、查一次 `_time:5m | stats count() c`，停 vlstorage-2 各 60 秒。原始資料：`vl/results/vl-reachable.jsonl`，摘要：`vl/results/vl-reachable-summary.json`。

| 段落 | Stream 分佈 | 寫入 | 查詢 | `vl_insert_remote_is_reachable{addr=vlstorage-2}` | `vl_insert_remote_send_errors_total{addr=vlstorage-2}` | `vl_select_remote_send_errors_total{addr=vlstorage-2}` |
|---|---|---|---|---|---|---|
| A：只寫 1 條 stream（在 vlstorage-1） | 停掉的那台沒有這條 stream | 12／12 回 200 | 12／12 回 502 | **一直是 1** | 0 | +12（第一筆取樣 +4s） |
| B：寫 9 條 stream（7 條在 vlstorage-2） | 停掉的那台有 7 條 | 12／12 回 200（轉送到 vlstorage-1） | 12／12 回 502 | **0**（第一筆取樣 +4s），vlstorage-2 回來後 +16s 回到 1 | +12 | +12 |

- `vl_insert_remote_is_reachable` 是被動的：只有 vlinsert 真的送資料到那台、而且失敗時才設成 0（[netinsert.go#L311](https://github.com/VictoriaMetrics/VictoriaLogs/blob/v1.53.0/app/vlstorage/netinsert/netinsert.go#L311)）。一條 stream 的前 1000 行固定寫到 `streamHash % 節點數` 那台（[netinsert.go#L457](https://github.com/VictoriaMetrics/VictoriaLogs/blob/v1.53.0/app/vlstorage/netinsert/netinsert.go#L457)），stream 少的時候停掉的那台可能根本沒有要寫的資料。
- 回來後要等 10 秒的停用期過了、再成功寫一次才變回 1，所以 B 段晚了 16 秒。
- 查詢端每次查詢都會對停掉的那台累加 `vl_select_remote_send_errors_total`（[netselect.go#L776](https://github.com/VictoriaMetrics/VictoriaLogs/blob/v1.53.0/app/vlstorage/netselect/netselect.go#L776)），只要有 log rule 在跑就會變。所以 `MetaVLStorageUnreachable` 改成「select 錯誤增加，或 reachable == 0」。
- 兩段寫入都回 200：vlinsert 把資料轉送到剩下的節點，shipper 看不到錯誤。

### 寫入請求數與收進筆數（`vl/ingested.py`）

給 `victorialogs#ingested` 的規則用。VL single，每種 Content-Type 送 5 行。原始資料：`vl/results/vl-ingested.json`。

| Content-Type | HTTP | `vl_http_requests_total{path="/insert/jsonline"}` | `vl_rows_ingested_total` | 查得到 |
|---|---|---|---|---|
| 不帶 | 200 | +1 | +5 | 5 |
| `application/x-www-form-urlencoded` | 200 | +1 | **+0** | 0 |
| `application/stream+json` | 200 | +1 | +5 | 5 |

`vl_http_errors_total` 三次都是 +0。請求數在讀 body 之前就累加（[jsonline.go#L29](https://github.com/VictoriaMetrics/VictoriaLogs/blob/v1.53.0/app/vlinsert/jsonline/jsonline.go#L29)），所以「有請求、沒收進」可以當規則（`MetaVLRowsNotIngested`）。
