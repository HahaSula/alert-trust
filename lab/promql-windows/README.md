# PromQL／MetricsQL range 函式的最小視窗

驗證[信任契約](../../docs/contracts.md#range-functions)裡每個 range 函式的最小視窗：在推導出的最小值「差 1 秒」與「剛好」時，結果是空還是有值。

**不依賴牆鐘。**這台機器的容器時鐘比牆鐘慢約 30%，所以兩部分都不量時間：

- `promtool test rules`：虛擬時鐘，series 由 `values` 指定，查詢在 `eval_time` 執行。
- `server/`：資料用明確時間戳匯入（2026-01-01T00:00:00Z 起），查詢指定 `time=`。

## 怎麼跑

```bash
./run.sh            # 約 2 分鐘：promtool（3.13.5、2.51.2）＋ server/run.sh
./server/run.sh     # 只跑 server 部分（compose 專案 at-pqw-server，port 33001–33003，結束時 down -v）
```

結果寫在 `results/promtool-<日期>.md`、`results/server-<日期>.md`（與 `.json`）。期望值寫在 `gen_tests.py`（promtool）與 `server/cases.py`（server），先由原始碼推導再跑，不是跑完再填。

## 模型

所有 series 的 scrape 間隔 `I` = 30s，從 0 開始。

| series | 內容 | 用來模擬 |
|---|---|---|
| `c` | 計數器 0, 10, 20, …（每 30s） | 正常 |
| `g` | Gauge 1, 2, 3, … | 正常 |
| `c_gap` | 90s 那個 sample 不存在 | 1 次 scrape 失敗（`k` = 1） |
| `c_inflight` | 只有 0–90s，120s 的 sample 不存在；查詢在 139s | `d` = 20s：120s 開始的 scrape 要到 140s 才寫入 |
| `s` | 90s 是 staleness marker | scrape 失敗後 Prometheus 寫入的 marker |
| `c_gap3` | 每 3 次少 1 次 | alert 測試的 `k` = 1 |

**`d` 怎麼模擬。**promtool 沒辦法模擬 scrape 耗時：所有 sample 一放進去就查得到。真實情況是時間戳 = scrape 開始，寫入後才查得到，所以「在 `ts` 查詢時還沒寫入的 sample」等於「在 `ts` 時不存在」。`c_inflight` 直接不放 120s 的 sample，查詢時間選在 120s 到 140s 之間（139s）。這只對那一個查詢時間成立，所以 `d` 只用 `promql_expr_test` 測，不用 alert 測試。`query_offset`／`offset` 的效果用 selector 的 `offset 20s` 測（rule 的 `query_offset` 就是把查詢時間往前移[^1]）。

**最差相位。**3.x 的視窗是左開右閉 `(t − R, t]`。evaluation 在 1m59s、sample 在 0、30、60、90s 時，`R` = 59s 的左邊界 60s 剛好不算，是 2 個 sample 的最差情況。2.x 是閉區間，1m59s 時 60s 還算，所以 2.x 的最差相位用 1m59.5s。

## 結果摘要（2026-11-10）

promtool：3.13.5 的 64 個 case＋alert 測試、2.51.2 的 12 個 case＋alert 測試全部 SUCCESS；把 2.51.2 的 alert 期望拿到 3.13.5 跑，如預期失敗。server：44 個查詢，0 個不符。

| 推導 | 差 1 秒 | 剛好 |
|---|---|---|
| `rate` 等 2 個 sample 的函式：`R ≥ 2I` | 59s 空 | 60s 有值 |
| `*_over_time` 等 1 個 sample：`R ≥ I` | 29s 空 | 30s 有值 |
| `k` = 1：`rate` `R ≥ 3I`、`last_over_time` `R ≥ 2I` | 60s 空／30s 空 | 90s／60s 有值 |
| `d` = 20s：`rate` `R ≥ 2I + d`、`last_over_time` `R ≥ I + d` | 79s 空／49s 空 | 80s／50s 有值 |
| `offset 20s`（O ≥ d）：`rate` `R ≥ 2I` | 59s 空 | 60s 有值 |
| Prometheus `anchored`：`R ≥ I` | 29s 空 | 30s 有值 |
| MetricsQL `rate`（前一個 sample 在 step 內）：`R ≥ I` | 29s 空 | 30s 有值 |
| vm-range-window `R ≥ I + L − D`，`L` = 20s，`D` = 0／20s | 49s／29s 空 | 50s／30s 有值 |

其他觀察（都在結果檔裡）：

- `changes`、`resets` 只有 1 個 sample 時回傳 0，不是空；gauge 每次都變，`changes(g[30s])` 仍是 0。
- `absent_over_time(c[29s])` 回傳 1：視窗太短時資料還在也會「消失」。
- Staleness marker 之後 instant selector 立刻變空；`last_over_time(s[2m])` 仍回傳 marker 之前的 20，而且保留 metric 名稱（2.51.2 與 3.13.5 相同）。
- Subquery 的步點用 lookback 補值：`count_over_time(c_gap[2m])` 是 3，`count_over_time(c_gap[2m:30s])` 是 4。
- 2.51.2 的 `rate(c[30s])` 在 1m（和 scrape 對齊）有值，3.13.5 是空；alert 測試 `rate(c_gap3[1m])` 在 2.51.2 會 firing，3.13.5 不會。
- MetricsQL：`deriv`、`predict_linear` 在 1 個 sample 時有值（斜率 0）；`rate_prometheus`、`increase_prometheus` 要 2 個；`rate(c)` 省略視窗時視窗等於 step，step 20s 就是空。

## 限制

- promtool 3.13.5 的 `--enable-feature` 只套用在載入 rule 檔的 parser，評估查詢時 engine 用預設 parser，所以 `anchored`、`smoothed` 與實驗函式在 promtool 裡一律 parse error[^2]。這部分改在 `server/` 用開了 feature flag 的 Prometheus 查。
- Staleness marker 不能用文字格式匯入，`server/` 沒有 `s`。
- `d > S`（scrape 比 interval 久、實際間隔拉長）沒有測：`scrape_timeout` 不能大於 `scrape_interval`，要真的大 target 才做得出來。
- 續送週期（`resend-endsat` 的最差情況）只讀碼，沒有在這裡測。

[^1]: https://github.com/prometheus/prometheus/blob/v3.13.5/rules/alerting.go#L389
[^2]: https://github.com/prometheus/prometheus/blob/v3.13.5/cmd/promtool/unittest.go#L612
