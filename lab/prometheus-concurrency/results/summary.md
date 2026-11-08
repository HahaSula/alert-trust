# Prometheus 執行模式與查詢 slot：lab 結果（2026-11-08）

設定與跑法見 [README](../README.md)。原始資料：[`modes.json`](modes.json)、[`slots.json`](slots.json)、`*-run.log`。每個情境跑一次。

<a id="modes"></a>

## A、B：rule group 執行模式

每條 rule 是 `count_over_time(vector(N)[1000s:1ms])`，group interval 3s，觀察 36 秒（每個情境 12 輪，五個 group 時約 60 輪）。一次只開一台 Prometheus。

| 情境 | 版本與旗標 | rule 耗時中位數 | group 一輪中位數 | 同時執行峰值（全域） | 每個 group 的峰值 |
|---|---|---|---|---|---|
| 1 group × 8 條 | 2.55.1 預設 | 74 ms | 585 ms | 1 | 1 |
| 1 group × 8 條 | 2.55.1 `concurrent-rule-eval`，K=1 | 103 ms | 458 ms | **2** | 2 |
| 1 group × 8 條 | 2.55.1 `concurrent-rule-eval`，K=2 | 160 ms | 479 ms | **3** | 3 |
| 1 group × 8 條 | 2.55.1 `concurrent-rule-eval`，K=4 | 203 ms | 446 ms | **5** | 5 |
| 1 group × 8 條 | 3.13.5 `concurrent-rule-eval`，K=2 | 108 ms | 340 ms | **3** | 3 |
| 5 group × 8 條 | 2.55.1 預設 | 76 ms | 665 ms | 4 | 各 1 |
| 5 group × 8 條 | 2.55.1 `concurrent-rule-eval`，K=2 | 103 ms | 487 ms | 5 | 各 3 |

- 預設模式下同一個 group 內一次只跑一條，group 一輪 ≈ 8 × rule 耗時。
- 開 `concurrent-rule-eval` 後，一個 group 內的峰值是 **K + 1**（K=1、2、4 時分別是 2、3、5）：搶不到 K 個 slot 的 rule 直接在 group 自己的 goroutine 裡跑，所以 K 不是硬上限。3.13.5 也一樣。
- 五個 group 時各自並行，預設模式全域峰值 4，每個 group 自己仍是 1。開 K=2 時每個 group 的峰值都是 3，全域峰值 5；理論上限是 group 數 + K = 7，這次各 group 的起跑點錯開，沒有全部重疊。
- rule 一開並行，單條 rule 變慢（74 → 103–203 ms），因為同時搶 CPU；group 一輪只縮短約 20–40%，不是 1/(K+1)。這些毫秒數只在同一台機器、同一次實驗內比較有意義。

<a id="slots"></a>

## C：查詢 slot（`--query.max-concurrency=1`、`--query.timeout=300ms`）

### C1：關卡 ① 的等待不受 timeout 限制

H 是 15000 項的 `1+1+…+1`，前處理在關卡 ① 內花 1–5 秒；200 ms 後送 P = `vector(1)`。每個版本 5 次。

| 版本 | P 的耗時（ms） | P 的結果 | H 的結果 |
|---|---|---|---|
| 2.55.1 | 579、750、4421、1142、929 | 5 次都成功 | 都是 `query timed out in expression evaluation` |
| 3.13.5 | 620、571、524、608、577 | 5 次都成功 | 同上 |

P 等了 0.5–4.4 秒才拿到結果，遠超過 300 ms 的 timeout，仍然成功：等待發生在建立查詢時的第一次取 slot（[engine.go#L474](https://github.com/prometheus/prometheus/blob/v2.55.1/promql/engine.go#L474)），timeout 要到執行時才設定（[#L621](https://github.com/prometheus/prometheus/blob/v2.55.1/promql/engine.go#L621)）。H 本身在執行階段被 300 ms 的 timeout 中止。

### C2：關卡 ② 的等待受 timeout 限制，pending 不變

`SlotProbe`（永遠成立、`for: 1h`、interval 1s）先進入 pending，再用 6 條執行緒送重查詢佔住唯一的 slot 20 秒。

| 項目 | 2.55.1 | 3.13.5 |
|---|---|---|
| 20 秒內 rule `health=err` 的取樣次數 | 10／40 | 2／33 |
| rule 的 `lastError` | `context deadline exceeded` | `context deadline exceeded` |
| `prometheus_rule_evaluation_failures_total` | 0 → 5 | 0 → 2 |
| alert 狀態與 `activeAt` | 一直是 pending，`activeAt` 前後相同 | 同左 |
| 負載查詢本身 | 成功 177、`context deadline exceeded` 113、執行逾時 4 | 成功 148、`context deadline exceeded` 99、執行逾時 1 |

`context deadline exceeded` 是排隊時 context 到期直接回傳的錯誤（[query_logger.go#L223](https://github.com/prometheus/prometheus/blob/v2.55.1/promql/query_logger.go#L223)，在 [engine.go#L659](https://github.com/prometheus/prometheus/blob/v2.55.1/promql/engine.go#L659) 的第二次取 slot），和執行中逾時的 `query timed out in expression evaluation` 是不同訊息。`SlotProbe` 自己的查詢只要幾 ms，所以它的失敗都來自關卡 ② 的等待。失敗的那幾輪 pending 沒有被刪除，負載停止後 `health` 回到 ok，`activeAt` 仍是原來的時間。
